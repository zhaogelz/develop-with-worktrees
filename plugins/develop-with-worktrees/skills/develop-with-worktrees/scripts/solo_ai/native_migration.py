"""旧候选现场的只读排空预览；启用前必须得到全部精确 Git 事实。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .candidate_batches import CandidateBatchStore
from .cleanup import inspect_untracked
from .config import load_repo_config
from .repo import GitRepo
from .state import FINAL_TASK_STATES, STATE_SCHEMA, StateStore
from .util import SoloAIError


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
        }

    config = load_repo_config(repo)
    pool_store = CandidateBatchStore(repo)
    pool = pool_store.read()
    candidates = pool_store.project_candidates(
        pool["candidates"].values(),
        pool["batches"],
        all_candidates=pool["candidates"].values(),
    )
    candidate_by_task = {
        str(item["task_id"]): item for item in candidates if item.get("task_id")
    }
    blockers: list[dict[str, str]] = []
    for task in state["tasks"].values():
        if task.get("status") not in FINAL_TASK_STATES:
            blockers.append(
                {
                    "kind": "active-legacy-task",
                    "task_id": str(task.get("id")),
                    "status": str(task.get("status")),
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

    registered = {item.path: item for item in repo.worktrees()}
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
        if repo.ref_head(f"refs/heads/{fixed_branch}") is not None:
            blockers.append(
                {
                    "kind": "fixed-branch-conflict",
                    "slot_id": slot_id,
                    "branch": fixed_branch,
                }
            )
        worktree = registered.get(path)
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
        inventory = inspect_untracked(repo, cwd=path, expand_dependencies=False)
        if inventory["keep"] or inventory["protected"] or inventory["unknown_ignored"]:
            blockers.append({"kind": "unknown-slot-content", "slot_id": slot_id})
            continue
        head = repo.head(path)
        attached = repo.branch(path)
        entry["head"] = head
        entry["attached_branch"] = attached
        predecessor_id = slot.get("released_candidate_task_id")
        predecessor = (
            candidate_by_task.get(str(predecessor_id)) if predecessor_id else None
        )
        if attached is not None:
            if (
                predecessor is None
                or predecessor.get("delivered") is not True
                or predecessor.get("branch") != attached
                or predecessor.get("head") != head
            ):
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
