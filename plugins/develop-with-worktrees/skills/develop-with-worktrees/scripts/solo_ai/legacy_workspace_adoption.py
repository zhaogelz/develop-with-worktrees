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
from .proof import read_validation_attempt, require_exact_passed_proof
from .repo import GitRepo
from .runtime_adapter import require_exact_passed_batch_release
from .state import STATE_SCHEMA, StateStore, candidate_admission_lock
from .util import (
    DirectoryLock,
    SoloAIError,
    filesystem_path,
    path_identity,
    read_json,
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
            or (
                item.get("status") == "integrated" and item.get("delivered") is not True
            )
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
    batch_id: str | None,
    base_ref: str | None,
    confirm: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    migration = state.get("native_migration")
    if state.get("schema_version") != STATE_SCHEMA or not isinstance(migration, dict):
        raise SoloAIError(
            "Legacy workspace adoption requires completed native migration"
        )
    if state.get("integration_workspace") is not None:
        raise SoloAIError("Native integration workspace already has a binding")
    if migration.get("legacy_integration_workspace_adoption") is not None:
        raise SoloAIError("Legacy integration workspace was already adopted")
    if (batch_id is None) == (base_ref is None):
        raise SoloAIError("Choose exactly one native batch or unchanged base")

    batches = state.get("batches")
    record = pool.get("integration_workspace")
    if not isinstance(batches, dict) or not isinstance(record, dict):
        raise SoloAIError("Exact native state and legacy workspace record are required")
    batch = batches.get(batch_id) if batch_id is not None else None
    if batch_id is not None:
        if not isinstance(batch, dict) or (
            batch.get("status") != "sealed"
            or batch.get("worktree_generation") is not None
            or batch.get("worktree_released_at") is not None
            or batch.get("applied_task_ids")
            or batch.get("merge_intent") is not None
        ):
            raise SoloAIError("Native batch is not sealed before workspace admission")
        native_base_ref = batch.get("base_ref")
    else:
        if batches:
            raise SoloAIError("Batch-free adoption requires no native batch")
        native_base_ref = base_ref
    if record.get("owner") is not None or record.get("registering") is not False:
        raise SoloAIError("Legacy integration workspace is not idle")

    config = load_repo_config(repo)
    worktree = store.managed_worktree_root(config) / "solo-ai-integration"
    generation = batch_workspace._generation(record.get("generation"))
    head = record.get("head")
    head_ref = record.get("head_ref")
    migration_head = migration.get("base_head")
    if (
        not isinstance(head, str)
        or not isinstance(head_ref, str)
        or not head_ref.startswith("refs/dww/batch-heads/")
        or not head_ref.removeprefix("refs/dww/batch-heads/")
        or record.get("worktree") != str(worktree)
        or not isinstance(native_base_ref, str)
        or native_base_ref != migration.get("base_ref")
        or not isinstance(migration_head, str)
        or repo.ref_head(f"refs/heads/{native_base_ref}") != migration_head
    ):
        raise SoloAIError("Legacy workspace or native migration identity changed")
    if batch is not None:
        if (
            batch.get("worktree") != str(worktree)
            or batch.get("base_before") != head
            or batch.get("integration_head") != head
            or migration_head != head
        ):
            raise SoloAIError(
                "Legacy workspace and frozen native batch do not share an exact base"
            )
        expected = f"{batch_id}:{head}:{generation}"
    else:
        expected = f"{native_base_ref}:{migration_head}:{head}:{generation}"
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
    if batch is not None and source_batch.get("base_ref") != native_base_ref:
        raise SoloAIError("Legacy completed batch belongs to another target")
    if batch is None:
        source_ref = source_batch.get("base_ref")
        source_base = source_batch.get("base_before")
        fingerprint = source_batch.get("proof")
        source_current = (
            repo.ref_head(f"refs/heads/{source_ref}")
            if isinstance(source_ref, str)
            else None
        )
        if (
            not isinstance(source_ref, str)
            or (source_current is not None and not repo.is_ancestor(head, source_current))
            or not isinstance(source_base, str)
            or not repo.is_ancestor(source_base, head)
            or source_batch.get("validation_outcome") != "passed"
            or not source_batch.get("promoted_at")
            or not source_batch.get("completed_at")
            or not isinstance(fingerprint, str)
            or not fingerprint
        ):
            raise SoloAIError("Cross-target legacy delivery is not exact and complete")
        proof = read_json(repo.local_dir / "proofs" / f"{fingerprint}.json", {})
        require_exact_passed_proof(
            proof, fingerprint=fingerprint, candidate_head=head, base_head=source_base
        )
        if "full" not in (proof.get("inputs") or {}).get("levels", []):
            raise SoloAIError("Cross-target legacy delivery requires passed Full")
        require_exact_passed_batch_release(
            repo, receipt=copy.deepcopy(source_batch.get("runtime_release") or {})
        )
    batch_workspace._require_saved_idle_head(repo, record)
    batch_workspace._check_directory(worktree, record)
    batch_workspace._check_git(repo, worktree, head)
    _require_inventory_visible(repo, worktree)
    batch_workspace.require_retained_contents(repo, worktree)
    return record, batch


def adopt_legacy_integration_workspace(
    repo: GitRepo,
    *,
    batch_id: str | None = None,
    base_ref: str | None = None,
    confirm: str,
) -> dict[str, Any]:
    """只导入原样空闲绑定与回执；不切换旧工作区 HEAD。"""

    from .lifecycle import maintenance_lock

    store = StateStore(repo)
    pool_store = CandidateBatchStore(repo)
    with (
        maintenance_lock(repo),
        candidate_admission_lock(repo),
        integration_turn(repo, batch_id or "legacy-workspace-adoption"),
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
                and previous.get("base_ref") == base_ref
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
            repo,
            store,
            pool_store,
            state,
            pool,
            batch_id=batch_id,
            base_ref=base_ref,
            confirm=confirm,
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
                base_ref=base_ref,
                confirm=confirm,
            )
            if sha256_text(stable_json(fresh_record)) != record_digest:
                raise SoloAIError("Legacy workspace record changed before adoption")
            receipt = {
                "schema_version": 1,
                "adopted_at": utc_timestamp(),
                "batch_id": batch_id,
                "base_ref": base_ref,
                "source_batch_id": fresh_record["head_ref"].removeprefix(
                    "refs/dww/batch-heads/"
                ),
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


def verified_pre_full_maintenance_source(
    repo: GitRepo, *, commit: str
) -> dict[str, Any]:
    """核验尚未启动原生 Full 的 DWW 维护提交及其独立 Full 证明。"""

    from .task_context import require_anchor

    store = StateStore(repo)
    state_digest = hashlib.sha256(store.path.read_bytes()).hexdigest()
    state = store.read()
    base = repo.ref_head("refs/heads/main")
    if (
        state.get("schema_version") != STATE_SCHEMA
        or not isinstance(base, str)
        or base == commit
        or not repo.is_ancestor(base, commit)
    ):
        raise SoloAIError("Maintenance source has no unchanged main base")

    tasks = [
        item
        for item in state.get("tasks", {}).values()
        if isinstance(item, dict)
        and item.get("candidate_head") == commit
        and item.get("status") == "ready"
        and (item.get("native_delivery") or {}).get("ready_head") == commit
    ]
    if len(tasks) != 1:
        raise SoloAIError("Pre-Full maintenance source needs one exact Ready task")
    task = tasks[0]
    anchor = require_anchor(repo, task, require_verified_origin=True)
    anchor_text = anchor.read_text(encoding="utf-8")
    contract = task.get("anchor_contract")
    if not isinstance(contract, dict):
        raise SoloAIError("Recovery source task has no maintenance contract")
    purpose = f"{task.get('name', '')} {contract.get('implementation_target', '')}"
    if (
        "dww" not in purpose.casefold()
        and "develop-with-worktrees" not in purpose.casefold()
    ):
        raise SoloAIError("Recovery source task is not anchored to DWW maintenance")
    task_id = task.get("id")
    worktree = Path(str(task.get("worktree") or ""))
    slot = state.get("slots", {}).get(str(task.get("slot_id")), {})
    if (
        task.get("base_ref") != "main"
        or task.get("base_head") != base
        or (task.get("native_delivery") or {}).get("batch_id") is not None
        or (task.get("runtime_activation") or {}).get("configured") is not False
        or task.get("runtime_activation_pending") is True
        or task.get("processes")
        or not isinstance(slot, dict)
        or slot.get("task_id") != task_id
        or slot.get("generation") != task.get("slot_generation")
        or slot.get("status") != "ready"
        or task.get("branch") != repo.branch(worktree)
        or repo.ref_head(f"refs/heads/{task.get('branch')}") != commit
        or repo.head(worktree) != commit
        or not repo.is_clean(worktree)
        or not any(item.path == worktree.resolve() for item in repo.worktrees())
        or task.get("slot_worktree_identity") != path_identity(worktree)
    ):
        raise SoloAIError("Pre-Full maintenance task or worktree identity changed")
    attempts = [
        read_validation_attempt(repo, attempt_id)
        for attempt_id in task.get("validation_attempts", [])
        if isinstance(attempt_id, str) and attempt_id
    ]
    passed = [
        attempt
        for attempt in attempts
        if attempt.get("level") == "full"
        and attempt.get("state") == "completed"
        and attempt.get("result") == "passed"
        and attempt.get("task_id") == task_id
        and attempt.get("owner") == {"kind": "task", "id": task_id}
        and attempt.get("candidate_head") == commit
        and attempt.get("base_head") == base
    ]
    if len(passed) != 1:
        raise SoloAIError("Pre-Full maintenance source needs one exact task Full")
    attempt = passed[0]
    proof_id = attempt.get("proof")
    if not isinstance(proof_id, str) or not proof_id:
        raise SoloAIError("Pre-Full maintenance Full proof is missing")
    proof = read_json(repo.local_dir / "proofs" / f"{proof_id}.json", {})
    require_exact_passed_proof(
        proof, fingerprint=proof_id, candidate_head=commit, base_head=base
    )
    inputs = proof.get("inputs") or {}
    if (
        "full" not in inputs.get("levels", [])
        or sha256_text(stable_json(inputs)) != proof_id
        or not attempt.get("profiles")
        or any(
            item.get("state") not in {"passed", "reused"}
            for item in attempt["profiles"]
        )
    ):
        raise SoloAIError("Pre-Full maintenance proof is not Full")
    paths = [
        item
        for item in repo.git(
            ["diff", "--no-renames", "--name-only", "-z", f"{base}..{commit}"]
        ).stdout.split("\0")
        if item
    ]
    if (
        not paths
        or not any(item.startswith("plugins/develop-with-worktrees/") for item in paths)
        or any(
            not item.startswith(("plugins/develop-with-worktrees/", "tests/"))
            for item in paths
        )
    ):
        raise SoloAIError("Pre-Full maintenance source changes unrelated paths")
    if (
        hashlib.sha256(store.path.read_bytes()).hexdigest() != state_digest
        or anchor.read_text(encoding="utf-8") != anchor_text
        or repo.ref_head("refs/heads/main") != base
        or repo.ref_head(f"refs/heads/{task['branch']}") != commit
    ):
        raise SoloAIError("Pre-Full maintenance source changed during verification")
    return {
        "source_task_id": task_id,
        "source_commit": commit,
        "base_head": base,
        "proof": proof_id,
        "validation_attempt": attempt["id"],
        "purpose": "pre-full-maintenance-review",
    }
