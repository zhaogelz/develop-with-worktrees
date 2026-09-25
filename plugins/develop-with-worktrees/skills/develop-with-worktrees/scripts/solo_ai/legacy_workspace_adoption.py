"""将迁移前已归还的集成工作区原位绑定到原生状态。"""

from __future__ import annotations

import copy
import hashlib
import os
import re
import stat
from pathlib import Path
from typing import Any

from . import batch_workspace
from .candidate_batches import CandidateBatchStore
from .cleanup import OPAQUE_RECREATABLE_ROOTS
from .config import load_repo_config
from .integration import integration_turn
from .native_migration import _settled_failed_batch
from .repo import GitRepo
from .state import STATE_SCHEMA, StateStore, candidate_admission_lock
from .util import (
    DirectoryLock,
    SoloAIError,
    filesystem_path,
    sha256_text,
    stable_json,
    utc_timestamp,
)


_UNREADABLE_DIRECTORY = re.compile(
    r"warning: could not open directory '([^']+)/': Permission denied"
)


def _require_inventory_visible(repo: GitRepo, worktree: Path) -> None:
    """Git 漏报的拒绝访问目录只能是可核验的不透明依赖根。"""

    opaque_roots: set[str] = set()
    try:
        for child in filesystem_path(worktree).iterdir():
            name = child.name.casefold()
            if name not in OPAQUE_RECREATABLE_ROOTS:
                continue
            path = worktree / child.name
            entry = filesystem_path(path).lstat()
            if not stat.S_ISDIR(entry.st_mode) or bool(
                getattr(entry, "st_file_attributes", 0) & 0x0400
            ):
                raise SoloAIError(
                    f"Retained dependency root is not a plain directory: {path}"
                )
            opaque_roots.add(name)
    except OSError as exc:
        raise SoloAIError("Cannot inspect integration workspace roots") from exc

    for args in (
        [
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "-z",
            "--directory",
        ],
        ["ls-files", "--others", "--exclude-standard", "-z"],
    ):
        result = repo.git(
            args,
            cwd=worktree,
            env={**os.environ, "LC_ALL": "C", "LANG": "C"},
        )
        for line in result.stderr.splitlines():
            match = _UNREADABLE_DIRECTORY.fullmatch(line.strip())
            if match is None or match.group(1).casefold() not in opaque_roots:
                raise SoloAIError(
                    f"Cannot inventory integration workspace safely: {line}"
                )


def _require_legacy_pool_settled(
    pool_store: CandidateBatchStore, pool: dict[str, Any], migration: dict[str, Any]
) -> None:
    candidates = pool_store.project_candidates(
        pool["candidates"].values(),
        pool["batches"],
        all_candidates=pool["candidates"].values(),
    )
    if (
        len(candidates) != migration.get("legacy_candidate_count")
        or len(pool["batches"]) != migration.get("legacy_batch_count")
        or any(
            item.get("status") not in {"integrated", "withdrawn", "superseded"}
            for item in candidates
        )
        or any(
            batch.get("run_owner") is not None
            or (
                batch.get("status") not in {"completed", "retired", "withdrawn"}
                and not _settled_failed_batch(batch, pool["candidates"])
            )
            for batch in pool["batches"].values()
        )
    ):
        raise SoloAIError("Legacy candidate pool changed or is not settled")


def _require_exact_binding(
    repo: GitRepo,
    store: StateStore,
    pool_store: CandidateBatchStore,
    state: dict[str, Any],
    pool: dict[str, Any],
    *,
    batch_id: str,
    confirm: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    migration = state.get("native_migration")
    if state.get("schema_version") != STATE_SCHEMA or not isinstance(migration, dict):
        raise SoloAIError(
            "Legacy workspace adoption requires completed native migration"
        )
    if state.get("integration_workspace") is not None:
        raise SoloAIError("Native integration workspace already has a binding")
    if migration.get("legacy_integration_workspace_adoption") is not None:
        raise SoloAIError("Legacy integration workspace was already adopted")

    batch = state.get("batches", {}).get(batch_id)
    record = pool.get("integration_workspace")
    if not isinstance(batch, dict) or not isinstance(record, dict):
        raise SoloAIError("Exact native batch and legacy workspace record are required")
    if (
        batch.get("status") != "sealed"
        or batch.get("worktree_generation") is not None
        or batch.get("worktree_released_at") is not None
        or batch.get("applied_task_ids")
        or batch.get("merge_intent") is not None
        or record.get("owner") is not None
        or record.get("registering") is not False
    ):
        raise SoloAIError("Batch or legacy workspace is not idle at the required phase")

    config = load_repo_config(repo)
    worktree = store.managed_worktree_root(config) / "solo-ai-integration"
    generation = batch_workspace._generation(record.get("generation"))
    head = record.get("head")
    head_ref = record.get("head_ref")
    if (
        not isinstance(head, str)
        or not isinstance(head_ref, str)
        or not head_ref.startswith("refs/dww/batch-heads/")
        or not head_ref.removeprefix("refs/dww/batch-heads/")
        or record.get("worktree") != str(worktree)
        or batch.get("worktree") != str(worktree)
        or batch.get("base_ref") != migration.get("base_ref")
        or batch.get("base_before") != head
        or batch.get("integration_head") != head
        or migration.get("base_head") != head
        or repo.ref_head(f"refs/heads/{batch['base_ref']}") != head
    ):
        raise SoloAIError(
            "Legacy workspace and frozen native batch do not share an exact base"
        )
    expected = f"{batch_id}:{head}:{generation}"
    if confirm != expected:
        raise SoloAIError(f"Legacy workspace adoption requires --confirm {expected!r}")

    _require_legacy_pool_settled(pool_store, pool, migration)
    source_batch = pool["batches"].get(head_ref.removeprefix("refs/dww/batch-heads/"))
    if (
        not isinstance(source_batch, dict)
        or source_batch.get("status") != "completed"
        or source_batch.get("worktree_mode") != "reusable"
        or not source_batch.get("worktree_released_at")
        or source_batch.get("run_owner") is not None
        or source_batch.get("base_ref") != batch.get("base_ref")
        or source_batch.get("worktree_generation") != generation
        or source_batch.get("integration_ref") != head_ref
        or source_batch.get("integration_head") != head
        or source_batch.get("integrated_head") != head
        or any(
            source_batch.get(key) != record.get(key)
            for key in batch_workspace._LOCATION_KEYS
        )
    ):
        raise SoloAIError("Legacy idle head has no exact completed batch receipt")
    batch_workspace._require_saved_idle_head(repo, record)
    batch_workspace._check_directory(worktree, record)
    batch_workspace._check_git(repo, worktree, head)
    _require_inventory_visible(repo, worktree)
    batch_workspace.require_retained_contents(repo, worktree)
    return record, batch


def adopt_legacy_integration_workspace(
    repo: GitRepo, *, batch_id: str, confirm: str
) -> dict[str, Any]:
    """只导入原样空闲绑定与回执；后续由原生 batch recover 领取新代次。"""

    from .lifecycle import maintenance_lock

    store = StateStore(repo)
    pool_store = CandidateBatchStore(repo)
    with (
        maintenance_lock(repo),
        candidate_admission_lock(repo),
        integration_turn(repo, batch_id),
        DirectoryLock(pool_store.lock_path, wait=True),
    ):
        state = store.read()
        migration = state.get("native_migration")
        previous = (
            migration.get("legacy_integration_workspace_adoption")
            if isinstance(migration, dict)
            else None
        )
        if previous is not None:
            if not isinstance(previous, dict):
                raise SoloAIError("Legacy workspace adoption receipt is malformed")
            binding = state.get("integration_workspace")
            if (
                previous.get("batch_id") == batch_id
                and previous.get("confirm") == confirm
                and isinstance(binding, dict)
                and all(
                    binding.get(key) == previous.get(key)
                    for key in batch_workspace._LOCATION_KEYS
                )
                and type(binding.get("generation")) is int
                and type(previous.get("generation")) is int
                and binding["generation"] >= previous["generation"]
            ):
                return {"status": "already-adopted", "receipt": previous}
            raise SoloAIError("Legacy integration workspace was already adopted")

        pool = pool_store.read()
        pool_digest = hashlib.sha256(pool_store.path.read_bytes()).hexdigest()
        record, _ = _require_exact_binding(
            repo, store, pool_store, state, pool, batch_id=batch_id, confirm=confirm
        )
        record_digest = sha256_text(stable_json(record))

        def adopt(current: dict[str, Any]) -> dict[str, Any]:
            if hashlib.sha256(pool_store.path.read_bytes()).hexdigest() != pool_digest:
                raise SoloAIError("Legacy candidate pool changed before adoption")
            fresh_pool = pool_store.read()
            fresh_record, _ = _require_exact_binding(
                repo,
                store,
                pool_store,
                current,
                fresh_pool,
                batch_id=batch_id,
                confirm=confirm,
            )
            if sha256_text(stable_json(fresh_record)) != record_digest:
                raise SoloAIError("Legacy workspace record changed before adoption")
            receipt = {
                "schema_version": 1,
                "adopted_at": utc_timestamp(),
                "batch_id": batch_id,
                "confirm": confirm,
                "legacy_pool_sha256": pool_digest,
                "legacy_workspace_sha256": record_digest,
                "generation": fresh_record["generation"],
                "head": fresh_record["head"],
                "head_ref": fresh_record["head_ref"],
                **{
                    key: copy.deepcopy(fresh_record[key])
                    for key in batch_workspace._LOCATION_KEYS
                },
            }
            current["integration_workspace"] = copy.deepcopy(fresh_record)
            current["native_migration"]["legacy_integration_workspace_adoption"] = (
                receipt
            )
            return {"status": "adopted", "receipt": copy.deepcopy(receipt)}

        return store.mutate(adopt)
