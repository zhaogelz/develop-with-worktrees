"""旧候选现场的只读排空预览；启用前必须得到全部精确 Git 事实。"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from . import batch_workspace
from .candidate_batches import CANDIDATE_REF_PREFIX, CandidateBatchStore
from .cleanup import inspect_untracked, is_link_or_junction
from .config import load_repo_config
from .repo import GitRepo
from .state import (
    FINAL_TASK_STATES,
    LEGACY_STATE_SCHEMA,
    STATE_SCHEMA,
    StateStore,
    candidate_admission_lock,
)
from .util import SoloAIError, path_identity, utc_timestamp


def _settled_failed_batch(
    batch: dict[str, Any], candidates: dict[str, dict[str, Any]]
) -> bool:
    """失败证据仍保留；仅在资源已归还且所有来源都有终态时视为排空。"""

    if batch.get("status") != "failed" or batch.get("run_owner") is not None:
        return False
    mode = batch.get("worktree_mode", "dedicated")
    if mode == "reusable":
        released = bool(batch.get("worktree_released_at"))
    elif mode == "dedicated":
        released = bool(batch.get("worktree_retired_at"))
    else:
        return False
    ids = batch.get("candidate_ids")
    return bool(
        released
        and isinstance(ids, list)
        and ids
        and all(
            isinstance(candidate_id, str)
            and candidate_id in candidates
            and candidates[candidate_id].get("status")
            in {"integrated", "withdrawn", "superseded"}
            for candidate_id in ids
        )
    )


def _delivered_supersession(
    repo: GitRepo,
    candidate: dict[str, Any],
    by_id: dict[str, dict[str, Any]],
    *,
    base_ref: str,
) -> bool:
    """核实附着的旧分支已被同一目标上的交付候选逐代替换。"""

    current = candidate
    seen: set[str] = set()
    while current.get("status") == "superseded":
        candidate_id = str(current.get("candidate_id"))
        ref = current.get("ref")
        if (
            candidate_id in seen
            or current.get("base_ref") != base_ref
            or not isinstance(ref, str)
            or repo.ref_head(ref) != current.get("head")
        ):
            return False
        seen.add(candidate_id)
        successor = by_id.get(str(current.get("superseded_by")))
        if successor is None or successor.get("supersedes") != candidate_id:
            return False
        current = successor
    return (
        current.get("base_ref") == base_ref
        and current.get("status") == "integrated"
        and current.get("delivered") is True
    )


def _withdrawn_ancestor(
    repo: GitRepo, candidate: dict[str, Any], *, base_head: str
) -> bool:
    """仅在撤回记录、保留引用及目标祖先关系均可核实时接受旧分支。"""

    candidate_id = candidate.get("candidate_id")
    head = candidate.get("head")
    ref = candidate.get("ref")
    withdrawal = candidate.get("withdrawal")
    withdrawn_at = candidate.get("withdrawn_at")
    return (
        candidate.get("status") == "withdrawn"
        and isinstance(candidate_id, str)
        and bool(candidate_id)
        and isinstance(head, str)
        and bool(head)
        and ref == f"{CANDIDATE_REF_PREFIX}{candidate_id}"
        and isinstance(withdrawal, dict)
        and withdrawal.get("ref_retention") == "preserved"
        and withdrawal.get("source") in {"api", "cli"}
        and isinstance(withdrawal.get("started_at"), str)
        and bool(withdrawal["started_at"])
        and isinstance(withdrawn_at, str)
        and bool(withdrawn_at)
        and repo.ref_head(ref) == head
        and repo.is_ancestor(head, base_head)
    )


def _retained_slot_verified(
    repo: GitRepo,
    state: dict[str, Any],
    slot_id: str,
    slot: dict[str, Any],
    path: Path,
    registered: dict[Path, Any],
) -> bool:
    """保留终态隔离工位原样；只核验其记录和当前 Git 身份。"""

    task_id = slot.get("task_id")
    task = state["tasks"].get(str(task_id)) if task_id else None
    abandonment = task.get("abandonment") if isinstance(task, dict) else None
    if (
        slot.get("status") != "quarantined"
        or not isinstance(task, dict)
        or task.get("status") != "abandoned"
        or task.get("slot_id") != slot_id
        or task.get("slot_generation") != slot.get("generation")
        or task.get("quarantine_reason") != slot.get("quarantine_reason")
        or task.get("active_operation") is not None
        or task.get("processes")
        or not isinstance(abandonment, dict)
        or abandonment.get("phase") != "completed"
        or abandonment.get("retained_worktree") is not True
        or abandonment.get("task_id") != task_id
        or abandonment.get("slot_id") != slot_id
        or path not in registered
        or not path.is_dir()
        or not repo.is_clean(path)
    ):
        return False
    branch = abandonment.get("branch")
    tip = abandonment.get("branch_tip")
    if not branch or not tip:
        return False
    if (
        repo.branch(path) != branch
        or repo.head(path) != tip
        or repo.ref_head(f"refs/heads/{branch}") != tip
    ):
        return False
    for record, prefix in (
        (slot, "released_"),
        (task, "slot_"),
        (abandonment, ""),
    ):
        if (
            record.get(f"{prefix}worktree_resolved") != str(path)
            or record.get(f"{prefix}worktree_identity") != path_identity(path)
            or record.get(f"{prefix}managed_root_resolved") != str(path.parent)
            or record.get(f"{prefix}managed_root_identity")
            != path_identity(path.parent)
        ):
            return False
    return task.get("worktree") == str(path) and abandonment.get("worktree") == str(
        path
    )


def _inspect_legacy_integration_workspace(
    repo: GitRepo, store: StateStore, pool: dict[str, Any]
) -> tuple[str, dict[str, Any] | None, dict[str, str] | None]:
    """只核验旧空闲登记；旧成果可来自另一目标分支。"""

    config = load_repo_config(repo)
    worktree = store.managed_worktree_root(config) / "solo-ai-integration"
    record = pool.get("integration_workspace")
    registered = any(item.path == worktree for item in repo.worktrees())
    exists = worktree.exists() or is_link_or_junction(worktree)
    if record is None:
        if exists or registered:
            return (
                "unowned",
                None,
                {"kind": "legacy-integration-workspace-unowned"},
            )
        return "absent", None, None
    if not isinstance(record, dict):
        return "blocked", None, {"kind": "legacy-integration-workspace-invalid"}
    if record.get("owner") is not None or record.get("registering") is not False:
        return (
            "blocked",
            None,
            {"kind": "legacy-integration-workspace-owned"},
        )
    try:
        generation = batch_workspace._generation(record.get("generation"))
        head = record.get("head")
        head_ref = record.get("head_ref")
        if (
            not exists
            or not registered
            or record.get("worktree") != str(worktree)
            or not isinstance(head, str)
            or not isinstance(head_ref, str)
            or not head_ref.startswith("refs/dww/batch-heads/")
        ):
            raise SoloAIError("Old integration workspace identity is incomplete")
        source_id = head_ref.removeprefix("refs/dww/batch-heads/")
        source = pool.get("batches", {}).get(source_id)
        if (
            not source_id
            or not isinstance(source, dict)
            or source.get("status") != "completed"
            or source.get("worktree_mode") != "reusable"
            or not source.get("worktree_released_at")
            or source.get("run_owner") is not None
            or source.get("worktree_generation") != generation
            or source.get("integration_ref") != head_ref
            or source.get("integration_head") != head
            or source.get("integrated_head") != head
            or any(
                source.get(key) != record.get(key)
                for key in batch_workspace._LOCATION_KEYS
            )
        ):
            raise SoloAIError("Old integration result has no exact completed receipt")
        batch_workspace._require_saved_idle_head(repo, record)
        batch_workspace._check_directory(worktree, record)
        batch_workspace._check_git(repo, worktree, head)
        batch_workspace.require_retained_contents(repo, worktree)
    except (OSError, SoloAIError) as exc:
        return (
            "blocked",
            None,
            {"kind": "legacy-integration-workspace-unverified", "reason": str(exc)},
        )
    return "verified-idle", record, None


def _native_integration_workspace_status(
    repo: GitRepo, store: StateStore, state: dict[str, Any]
) -> tuple[str, list[dict[str, str]]]:
    """已升级也核查下一次集成会用到的工作树登记。"""

    binding = state.get("integration_workspace")
    if binding is None:
        pool = CandidateBatchStore(repo).read()
        old_status, _, problem = _inspect_legacy_integration_workspace(
            repo, store, pool
        )
        if old_status == "absent":
            return "available", []
        if old_status == "verified-idle":
            return "legacy-adoption-required", [
                {"kind": "legacy-integration-workspace-unbound"}
            ]
        return old_status, [problem] if problem else []
    if not isinstance(binding, dict):
        return "blocked", [{"kind": "native-integration-workspace-invalid"}]
    if binding.get("owner") is not None:
        return "managed-active", []
    try:
        worktree = Path(str(binding["worktree"]))
        batch_workspace._require_saved_idle_head(repo, binding)
        batch_workspace._check_directory(worktree, binding)
        batch_workspace._check_git(repo, worktree, str(binding["head"]))
        batch_workspace.require_retained_contents(repo, worktree)
    except (KeyError, OSError, SoloAIError) as exc:
        return "blocked", [
            {"kind": "native-integration-workspace-unverified", "reason": str(exc)}
        ]
    return "managed-idle", []


def preview_native_migration(repo: GitRepo, *, base_ref: str) -> dict[str, Any]:
    """列出旧状态排空和固定分支初始化的所有可观察阻塞。"""

    store = StateStore(repo)
    state = store.read()
    base_head = repo.ref_head(f"refs/heads/{base_ref}")
    if base_head is None:
        raise SoloAIError(f"Migration target branch does not exist: {base_ref}")
    if state["schema_version"] == STATE_SCHEMA:
        workspace_status, blockers = _native_integration_workspace_status(
            repo, store, state
        )
        return {
            "status": "enabled",
            "base_ref": base_ref,
            "base_head": base_head,
            "blockers": blockers,
            "slots": [],
            "integration_workspace_status": workspace_status,
            "migration": state.get("native_migration"),
        }

    config = load_repo_config(repo)
    pool_store = CandidateBatchStore(repo)
    pool = pool_store.read()
    candidates = pool_store.project_candidates(
        pool["candidates"].values(),
        pool["batches"],
        all_candidates=pool["candidates"].values(),
    )
    candidates_by_task: dict[str, list[dict[str, Any]]] = {}
    for item in candidates:
        if item.get("task_id"):
            candidates_by_task.setdefault(str(item["task_id"]), []).append(item)
    candidates_by_id = {str(item["candidate_id"]): item for item in candidates}
    blockers: list[dict[str, str]] = []
    if state.get("batches") or state.get("integration_workspace"):
        blockers.append({"kind": "native-state-already-present"})
    for task in state["tasks"].values():
        if task.get("status") not in FINAL_TASK_STATES:
            blockers.append(
                {
                    "kind": "active-legacy-task",
                    "task_id": str(task.get("id")),
                    "status": str(task.get("status")),
                }
            )
        if task.get("processes"):
            blockers.append(
                {
                    "kind": "legacy-runtime-not-released",
                    "task_id": str(task.get("id")),
                }
            )
    for candidate in candidates:
        status = str(candidate.get("status"))
        if status in {"integrated", "withdrawn", "superseded"}:
            if status == "integrated" and not candidate.get("delivered"):
                blockers.append(
                    {
                        "kind": "legacy-delivery-unverified",
                        "candidate_id": str(candidate["candidate_id"]),
                    }
                )
            continue
        blockers.append(
            {
                "kind": "unsettled-legacy-candidate",
                "candidate_id": str(candidate["candidate_id"]),
                "status": status,
            }
        )
    for batch in pool["batches"].values():
        if batch.get("status") not in {"completed", "retired", "withdrawn"} and not (
            _settled_failed_batch(batch, pool["candidates"])
        ):
            blockers.append(
                {
                    "kind": "unsettled-legacy-batch",
                    "batch_id": str(batch.get("id")),
                    "status": str(batch.get("status")),
                }
            )
    workspace_status, _, workspace_problem = _inspect_legacy_integration_workspace(
        repo, store, pool
    )
    if workspace_problem:
        blockers.append(workspace_problem)

    registered = {item.path.resolve(): item for item in repo.worktrees()}
    slots: list[dict[str, Any]] = []
    retained_slot_ids: list[str] = []
    for slot_id, slot in sorted(state["slots"].items()):
        path = Path(str(slot["path"])).resolve()
        if slot.get("status") == "quarantined" and slot.get("task_id"):
            if _retained_slot_verified(repo, state, slot_id, slot, path, registered):
                retained_slot_ids.append(slot_id)
            else:
                blockers.append(
                    {"kind": "retained-slot-unverified", "slot_id": slot_id}
                )
            continue
        fixed_branch = f"{config.branch_prefix}slot-{slot_id}"
        entry = {
            "slot_id": slot_id,
            "path": str(path),
            "status": slot.get("status"),
            "fixed_branch": fixed_branch,
        }
        slots.append(entry)
        if slot.get("status") not in {"idle", "inactive"} or slot.get("task_id"):
            blockers.append(
                {
                    "kind": "slot-not-idle",
                    "slot_id": slot_id,
                    "status": str(slot.get("status")),
                }
            )
            continue
        worktree = registered.get(path)
        fixed_head = repo.ref_head(f"refs/heads/{fixed_branch}")
        staged = fixed_head == base_head and (
            (worktree is None and not path.exists())
            or (
                worktree is not None
                and repo.branch(path) == fixed_branch
                and repo.head(path) == base_head
            )
        )
        if fixed_head is not None and not staged:
            blockers.append(
                {
                    "kind": "fixed-branch-conflict",
                    "slot_id": slot_id,
                    "branch": fixed_branch,
                }
            )
        if worktree is None:
            if path.exists():
                blockers.append(
                    {
                        "kind": "unregistered-slot-path",
                        "slot_id": slot_id,
                        "path": str(path),
                    }
                )
            continue
        if not path.is_dir() or not repo.is_clean(path):
            blockers.append({"kind": "slot-not-clean", "slot_id": slot_id})
            continue
        recorded_path = slot.get("released_worktree_resolved")
        recorded_root = slot.get("released_managed_root_resolved")
        if (
            (recorded_path and Path(str(recorded_path)).resolve() != path)
            or (
                slot.get("released_worktree_identity") is not None
                and path_identity(path) != slot["released_worktree_identity"]
            )
            or (recorded_root and Path(str(recorded_root)).resolve() != path.parent)
            or (
                slot.get("released_managed_root_identity") is not None
                and path_identity(path.parent) != slot["released_managed_root_identity"]
            )
        ):
            blockers.append({"kind": "slot-identity-mismatch", "slot_id": slot_id})
            continue
        inventory = inspect_untracked(repo, cwd=path, expand_dependencies=False)
        if inventory["keep"] or inventory["protected"] or inventory["unknown_ignored"]:
            blockers.append({"kind": "unknown-slot-content", "slot_id": slot_id})
            continue
        head = repo.head(path)
        attached = repo.branch(path)
        entry["head"] = head
        entry["attached_branch"] = attached
        predecessor_id = slot.get("released_candidate_task_id")
        matching = [
            candidate
            for candidate in candidates_by_task.get(str(predecessor_id), [])
            if candidate.get("branch") == attached
            and candidate.get("head") == head
            and (
                candidate.get("delivered") is True
                or _delivered_supersession(
                    repo, candidate, candidates_by_id, base_ref=base_ref
                )
                or _withdrawn_ancestor(repo, candidate, base_head=base_head)
            )
        ]
        predecessor = matching[0] if len(matching) == 1 else None
        if attached is not None:
            if staged:
                entry["staged"] = True
            elif predecessor is None:
                blockers.append(
                    {"kind": "attached-legacy-slot-unsettled", "slot_id": slot_id}
                )
        elif not repo.is_ancestor(head, base_head):
            blockers.append(
                {"kind": "detached-slot-head-not-in-target", "slot_id": slot_id}
            )

    return {
        "status": "blocked" if blockers else "ready",
        "base_ref": base_ref,
        "base_head": base_head,
        "blockers": blockers,
        "slots": slots,
        "integration_workspace_status": workspace_status,
        "retained_slot_ids": retained_slot_ids,
        "legacy_candidate_count": len(candidates),
        "legacy_batch_count": len(pool["batches"]),
    }


def enable_native_migration(
    repo: GitRepo, *, base_ref: str, confirm: str
) -> dict[str, Any]:
    """排空旧现场后在原工位建立固定分支；中断后可用同一命令续完。"""

    from .lifecycle import _config_and_mode, maintenance_lock

    _config_and_mode(repo)
    with maintenance_lock(repo), candidate_admission_lock(repo):
        preview = preview_native_migration(repo, base_ref=base_ref)
        if preview["status"] == "enabled":
            return preview
        expected = f"{base_ref}:{preview['base_head']}"
        if confirm != expected:
            raise SoloAIError(f"Native migration requires --confirm {expected!r}")
        if preview["blockers"]:
            return preview

        store = StateStore(repo)
        base_head = str(preview["base_head"])
        for slot in preview["slots"]:
            path = Path(str(slot["path"]))
            branch = str(slot["fixed_branch"])
            if repo.ref_head(f"refs/heads/{branch}") is None:
                if path.exists():
                    repo.git(["switch", "-c", branch, base_head], cwd=path)
                else:
                    repo.git(["branch", branch, base_head])
            if repo.ref_head(f"refs/heads/{branch}") != base_head:
                raise SoloAIError(
                    f"Fixed slot branch changed during migration: {branch}"
                )
            if path.exists() and (
                repo.branch(path) != branch or repo.head(path) != base_head
            ):
                raise SoloAIError(
                    f"Fixed slot worktree changed during migration: {path}"
                )

        checked = preview_native_migration(repo, base_ref=base_ref)
        if checked["status"] != "ready" or checked["base_head"] != base_head:
            return checked
        pool = CandidateBatchStore(repo).read()
        workspace_status, idle_workspace, workspace_problem = (
            _inspect_legacy_integration_workspace(repo, store, pool)
        )
        if workspace_problem:
            raise SoloAIError(
                f"Legacy integration workspace changed: {workspace_problem['kind']}"
            )
        candidates = CandidateBatchStore(repo).project_candidates(
            pool["candidates"].values(),
            pool["batches"],
            all_candidates=pool["candidates"].values(),
        )
        non_ancestor = sorted(
            str(candidate["candidate_id"])
            for candidate in candidates
            if candidate.get("delivered") is True
            and candidate.get("base_ref") == base_ref
            and not repo.is_ancestor(str(candidate["head"]), base_head)
        )
        receipt = {
            "schema_version": 1,
            "enabled_at": utc_timestamp(),
            "base_ref": base_ref,
            "base_head": base_head,
            "slot_ids": [str(slot["slot_id"]) for slot in checked["slots"]],
            "retained_slot_ids": checked["retained_slot_ids"],
            "legacy_candidate_count": checked["legacy_candidate_count"],
            "legacy_batch_count": checked["legacy_batch_count"],
            "legacy_non_ancestor_candidate_ids": non_ancestor,
            "legacy_integration_workspace": (
                {
                    "generation": idle_workspace["generation"],
                    "head": idle_workspace["head"],
                    "head_ref": idle_workspace["head_ref"],
                }
                if idle_workspace is not None
                else None
            ),
        }

        def update(state: dict[str, Any]) -> dict[str, Any]:
            if state["schema_version"] != LEGACY_STATE_SCHEMA:
                raise SoloAIError("Native migration state changed before enable")
            if repo.ref_head(f"refs/heads/{base_ref}") != base_head:
                raise SoloAIError("Native migration target changed before enable")
            current_workspace_status, current_workspace, current_problem = (
                _inspect_legacy_integration_workspace(
                    repo, store, CandidateBatchStore(repo).read()
                )
            )
            if (
                current_problem
                or current_workspace_status != workspace_status
                or current_workspace != idle_workspace
                or state.get("integration_workspace") is not None
            ):
                raise SoloAIError("Legacy integration workspace changed before enable")
            for item in checked["slots"]:
                slot_id = str(item["slot_id"])
                slot = state["slots"][slot_id]
                path = Path(str(item["path"]))
                slot["fixed_branch"] = str(item["fixed_branch"])
                slot["released_task_id"] = None
                if path.exists():
                    slot["released_worktree_resolved"] = str(path.resolve())
                    slot["released_worktree_identity"] = path_identity(path)
                    managed_root = path.parent
                    slot["released_managed_root_resolved"] = str(managed_root.resolve())
                    slot["released_managed_root_identity"] = path_identity(managed_root)
            state["schema_version"] = STATE_SCHEMA
            state.setdefault("batches", {})
            state["integration_workspace"] = copy.deepcopy(idle_workspace)
            state["native_migration"] = receipt
            return {
                "status": "enabled",
                "base_ref": base_ref,
                "base_head": base_head,
                "blockers": [],
                "slots": checked["slots"],
                "migration": receipt,
            }

        return store.mutate(update)
