"""将迁移前已归还的集成工作区原位绑定到原生状态。"""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any

from . import batch_workspace
from .candidate_batches import CandidateBatchStore
from .config import load_repo_config, load_verification_config
from .integration import integration_turn
from .native_migration import (
    _inspect_legacy_integration_workspace,
    _settled_failed_batch,
)
from .proof import (
    frozen_validation_environment,
    proof_inputs,
    read_validation_attempt,
    require_exact_passed_proof,
)
from .repo import GitRepo
from .state import STATE_SCHEMA, StateStore, candidate_admission_lock
from .util import (
    DirectoryLock,
    SoloAIError,
    path_identity,
    read_json,
    sha256_text,
    stable_json,
    utc_timestamp,
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
        or (
            batch is None
            and repo.ref_head(f"refs/heads/{native_base_ref}") != migration_head
        )
    ):
        raise SoloAIError("Legacy workspace or native migration identity changed")
    if batch is not None:
        if (
            batch.get("worktree") != str(worktree)
            or batch.get("base_ref") != native_base_ref
            or not isinstance(batch.get("base_before"), str)
            or batch.get("integration_head") != batch.get("base_before")
            or repo.ref_head(f"refs/heads/{native_base_ref}")
            != batch.get("base_before")
            or not repo.is_ancestor(migration_head, batch["base_before"])
            or any(
                other_id != batch_id
                and isinstance(other, dict)
                and other.get("worktree") == str(worktree)
                and not other.get("worktree_released_at")
                and (
                    other.get("worktree_generation") is not None
                    or other.get("merge_intent") is not None
                    or other.get("applied_task_ids")
                )
                for other_id, other in batches.items()
            )
        ):
            raise SoloAIError(
                "Frozen native batch no longer owns an exact unchanged target base"
            )
        expected = f"{batch_id}:{head}:{generation}"
    else:
        expected = f"{native_base_ref}:{migration_head}:{head}:{generation}"
    if confirm != expected:
        raise SoloAIError(f"Legacy workspace adoption requires --confirm {expected!r}")

    _require_legacy_pool_settled(pool_store, pool, migration)
    workspace_status, verified_record, problem = _inspect_legacy_integration_workspace(
        repo, store, pool, target_base_ref=native_base_ref
    )
    if workspace_status != "verified-idle" or verified_record != record:
        raise SoloAIError(
            f"Legacy idle workspace is not verified: {problem or workspace_status}"
        )
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
    if not passed:
        raise SoloAIError("Pre-Full maintenance source needs a passed task Full")
    verification = load_verification_config(repo, cwd=worktree)
    verified: tuple[dict[str, Any], str] | None = None
    for attempt in reversed(passed):
        try:
            proof_id = attempt.get("proof")
            if not isinstance(proof_id, str) or not proof_id:
                raise SoloAIError("Pre-Full maintenance Full proof is missing")
            proof = read_json(repo.local_dir / "proofs" / f"{proof_id}.json", {})
            require_exact_passed_proof(
                proof, fingerprint=proof_id, candidate_head=commit, base_head=base
            )
            inputs = proof.get("inputs") or {}
            profiles = attempt.get("profiles")
            full_scope = attempt.get("full_scope")
            if (
                inputs.get("levels") != ["ready", "full"]
                or sha256_text(stable_json(inputs)) != proof_id
                or not isinstance(profiles, list)
                or not profiles
                or full_scope not in {"integration", "complete"}
                or any(
                    not isinstance(item, dict)
                    or item.get("state") not in {"passed", "reused"}
                    or not isinstance(item.get("fingerprint"), str)
                    for item in profiles
                )
            ):
                raise SoloAIError("Pre-Full maintenance proof is not Full")
            execution_ids: set[str] = set()
            for item in profiles:
                profile_id = item["fingerprint"]
                profile_proof = read_json(
                    repo.local_dir / "profile-proofs" / f"{profile_id}.json", {}
                )
                profile_inputs = profile_proof.get("inputs")
                if (
                    profile_proof.get("fingerprint") != profile_id
                    or profile_proof.get("result") != "passed"
                    or not isinstance(profile_inputs, dict)
                    or sha256_text(stable_json(profile_inputs)) != profile_id
                ):
                    raise SoloAIError("Pre-Full maintenance profile proof changed")
                execution = profile_inputs.get("full_execution")
                if execution is not None:
                    if not isinstance(execution, str) or not execution:
                        raise SoloAIError(
                            "Pre-Full maintenance execution identity changed"
                        )
                    execution_ids.add(execution)
            if len(execution_ids) > 1:
                raise SoloAIError("Pre-Full maintenance execution identity changed")
            validation_environment = frozen_validation_environment(
                repo,
                cwd=worktree,
                base=base,
                validation_base_ref="main",
                expected_base_head=base,
                full_scope=full_scope,
            )
            current_inputs, records = proof_inputs(
                repo,
                cwd=worktree,
                base=base,
                verification=verification,
                task_id=task_id,
                levels=("ready", "full"),
                full_scopes=(
                    ("integration", "complete")
                    if full_scope == "complete"
                    else ("integration",)
                ),
                force_task_scope=task.get("mode") == "in-place",
                expected_candidate_head=commit,
                full_execution_id=next(iter(execution_ids), None),
                validation_environment=validation_environment,
            )
            if (
                current_inputs != inputs
                or [item.get("id") for item in profiles]
                != [record[0].profile_id for record in records]
                or [item.get("fingerprint") for item in profiles]
                != [record[2] for record in records]
            ):
                raise SoloAIError("Pre-Full maintenance Full inputs changed")
            verified = (attempt, proof_id)
            break
        except SoloAIError:
            continue
    if verified is None:
        raise SoloAIError("Pre-Full maintenance source has no current exact task Full")
    attempt, proof_id = verified
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
