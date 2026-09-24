"""旧候选现场的只读排空预览；启用前必须得到全部精确 Git 事实。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .candidate_batches import CandidateBatchStore
from .cleanup import inspect_untracked
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


def preview_native_migration(repo: GitRepo, *, base_ref: str) -> dict[str, Any]:
    """列出旧状态排空和固定分支初始化的所有可观察阻塞。"""

    state = StateStore(repo).read()
    base_head = repo.ref_head(f"refs/heads/{base_ref}")
    if base_head is None:
        raise SoloAIError(f"Migration target branch does not exist: {base_ref}")
    if state["schema_version"] == STATE_SCHEMA:
        return {
            "status": "enabled",
            "base_ref": base_ref,
            "base_head": base_head,
            "blockers": [],
            "slots": [],
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
        if batch.get("status") not in {"completed", "retired", "withdrawn"}:
            blockers.append(
                {
                    "kind": "unsettled-legacy-batch",
                    "batch_id": str(batch.get("id")),
                    "status": str(batch.get("status")),
                }
            )
    workspace = pool.get("integration_workspace")
    if isinstance(workspace, dict) and workspace.get("owner") is not None:
        blockers.append(
            {
                "kind": "legacy-integration-workspace-owned",
                "batch_id": str(workspace["owner"]),
            }
        )

    registered = {item.path.resolve(): item for item in repo.worktrees()}
    slots: list[dict[str, Any]] = []
    for slot_id, slot in sorted(state["slots"].items()):
        path = Path(str(slot["path"])).resolve()
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
            and candidate.get("delivered") is True
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
            "legacy_candidate_count": checked["legacy_candidate_count"],
            "legacy_batch_count": checked["legacy_batch_count"],
            "legacy_non_ancestor_candidate_ids": non_ancestor,
        }

        def update(state: dict[str, Any]) -> dict[str, Any]:
            if state["schema_version"] != LEGACY_STATE_SCHEMA:
                raise SoloAIError("Native migration state changed before enable")
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
            state.setdefault("integration_workspace", None)
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
