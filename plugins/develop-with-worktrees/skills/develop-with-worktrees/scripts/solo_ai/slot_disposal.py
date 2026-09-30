from __future__ import annotations

import os
import re
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from .cleanup import require_managed_directory_identity
from .config import load_repo_config
from .repo import GitRepo
from .state import FINAL_TASK_STATES, StateStore
from .util import (
    SoloAIError,
    atomic_write_json,
    delete_link_path_if_unchanged,
    delete_plain_path_if_unchanged,
    is_link_or_junction,
    new_id,
    path_identity,
    pinned_plain_directory,
    read_json,
    sha256_text,
    snapshot_link_path,
    snapshot_plain_path,
    stable_json,
    utc_timestamp,
)


def _entries(root: Path) -> dict[str, dict[str, Any]]:
    """冻结整槽中的每个对象；链接只记录对象本身，不遍历目标。"""
    if is_link_or_junction(root) or not root.is_dir():
        raise SoloAIError("Slot disposal requires a plain worktree directory")
    found = {".": snapshot_plain_path(root)}
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as children:
            paths = [directory / child.name for child in children]
        for path in sorted(paths):
            relative = path.relative_to(root).as_posix()
            if is_link_or_junction(path):
                found[relative] = snapshot_link_path(path)
            else:
                found[relative] = snapshot_plain_path(path)
                if path.is_dir():
                    pending.append(path)
    return found


def _registration(repo: GitRepo, root: Path, branch: str | None, head: str) -> None:
    matches = [item for item in repo.worktrees() if item.path == root]
    if (
        len(matches) != 1
        or matches[0].head != head
        or matches[0].detached != (branch is None)
        or matches[0].branch != (f"refs/heads/{branch}" if branch else None)
    ):
        raise SoloAIError("Slot disposal Git registration changed")


def _slot(
    repo: GitRepo, slot_id: str, *, plan_id: str | None = None
) -> tuple[StateStore, dict[str, Any], Path]:
    store = StateStore(repo)
    config = load_repo_config(repo, cwd=repo.policy_path())
    state = store.require_slot_layout(config)
    slot = state["slots"].get(slot_id)
    allowed = {"idle", "inactive"} if plan_id is None else {"disposing"}
    if (
        not slot
        or slot.get("status") not in allowed
        or slot.get("task_id")
        or (plan_id is not None and slot.get("disposal_plan_id") != plan_id)
        or any(
            task.get("slot_id") == slot_id
            and task.get("status") not in FINAL_TASK_STATES
            for task in state["tasks"].values()
        )
    ):
        raise SoloAIError(
            "Slot disposal requires the exact idle generation without an active owner"
        )
    root = Path(str(slot["path"]))
    expected = store.managed_worktree_root(config) / f"solo-ai-slot-{slot_id}"
    if root != expected or root == repo.root or root == repo.primary_path:
        raise SoloAIError(
            "Slot disposal cannot change the primary or an unexpected worktree"
        )
    store.require_slot_ownership(slot_id, root)
    return store, slot, root


def _payload(repo: GitRepo, slot_id: str) -> dict[str, Any]:
    store, slot, root = _slot(repo, slot_id)
    parent = root.parent
    require_managed_directory_identity(parent, managed_root=parent)
    require_managed_directory_identity(
        root,
        managed_root=parent,
        expected_identity=slot.get("released_worktree_identity"),
        expected_root_identity=slot.get("released_managed_root_identity"),
        expected_resolved=slot.get("released_worktree_resolved"),
        expected_root_resolved=slot.get("released_managed_root_resolved"),
    )
    head = repo.head(root)
    branch = repo.branch(root)
    if branch and repo.ref_head(f"refs/heads/{branch}") != head:
        raise SoloAIError("Idle slot branch or HEAD changed")
    _registration(repo, root, branch, head)
    if repo.git(["status", "--porcelain=v1", "--untracked-files=no"], cwd=root).stdout:
        raise SoloAIError("Tracked changes block whole-slot disposal")
    predecessor_id = slot.get("released_task_id") or slot.get(
        "released_candidate_task_id"
    )
    if predecessor_id:
        task = store.task(str(predecessor_id))
        delivery = (task or {}).get("native_delivery") or {}
        abandonment = (task or {}).get("abandonment") or {}
        preactivation = (task or {}).get("preactivation_release") or {}
        publication = (task or {}).get("candidate_publication") or {}
        if (
            not task
            or task.get("slot_id") != slot_id
            or task.get("slot_generation") != slot.get("generation")
            or task.get("worktree") != str(root)
            or not (
                (
                    task.get("status") == "finished"
                    and isinstance(delivery.get("delivery"), dict)
                )
                or (
                    task.get("status") == "abandoned"
                    and abandonment.get("phase") == "completed"
                    and not abandonment.get("retained_worktree")
                )
                or (
                    task.get("status") == "abandoned"
                    and preactivation.get("phase") == "completed"
                )
                or (
                    task.get("status") == "candidate-published"
                    and publication.get("phase") == "completed"
                )
            )
        ):
            raise SoloAIError("Idle slot has no exact released predecessor")
        base_ref = str(task["base_ref"])
    else:
        # 迁移前的空闲槽可能没有前驱指针；只接受已登记且 HEAD 在当前默认基线内的现场。
        base_ref = repo.default_branch()
    base_head = repo.ref_head(f"refs/heads/{base_ref}")
    if not base_head or not repo.is_ancestor(head, base_head):
        raise SoloAIError("Idle slot HEAD is not in its recorded base")
    return {
        "mode": "dispose",
        "slot": slot_id,
        "slot_status": slot["status"],
        "slot_generation": int(slot["generation"]),
        "released_task_id": slot.get("released_task_id"),
        "released_candidate_task_id": slot.get("released_candidate_task_id"),
        "fixed_branch": slot.get("fixed_branch"),
        "worktree": str(root),
        "worktree_identity": path_identity(root),
        "managed_root_identity": path_identity(parent),
        "branch": branch,
        "detached": branch is None,
        "head": head,
        "base_ref": base_ref,
        "base_head": base_head,
        "entries": _entries(root),
    }


def _stage(payload: dict[str, Any], plan_id: str) -> Path:
    root = Path(str(payload["worktree"]))
    return root.parent / f".{root.name}.dww-dispose-{plan_id}"


def _check_slot(repo: GitRepo, payload: dict[str, Any], plan_id: str) -> Path:
    if payload.get("slot_status") not in {"idle", "inactive"}:
        raise SoloAIError("Whole-slot plan has an invalid original slot status")
    _, slot, root = _slot(repo, str(payload["slot"]), plan_id=plan_id)
    if payload.get("worktree") != str(root):
        raise SoloAIError("Whole-slot plan worktree path changed")
    current_base = repo.ref_head(f"refs/heads/{payload['base_ref']}")
    if (
        slot.get("generation") != payload["slot_generation"]
        or slot.get("released_task_id") != payload["released_task_id"]
        or slot.get("released_candidate_task_id")
        != payload["released_candidate_task_id"]
        or slot.get("fixed_branch") != payload["fixed_branch"]
        or not current_base
        or not repo.is_ancestor(str(payload["base_head"]), current_base)
        or not repo.is_ancestor(str(payload["head"]), current_base)
        or (
            payload["branch"] is not None
            and repo.ref_head(f"refs/heads/{payload['branch']}") != payload["head"]
        )
    ):
        raise SoloAIError("Slot disposal generation, branch, or base changed")
    require_managed_directory_identity(
        root.parent,
        managed_root=root.parent,
        expected_identity=payload["managed_root_identity"],
        expected_root_identity=payload["managed_root_identity"],
    )
    return root


def _claim(repo: GitRepo, payload: dict[str, Any], plan_id: str) -> None:
    store = StateStore(repo)

    def update(state: dict[str, Any]) -> None:
        slot = state["slots"].get(str(payload["slot"]))
        if (
            not slot
            or slot.get("generation") != payload["slot_generation"]
            or slot.get("task_id")
            or slot.get("path") != payload["worktree"]
            or slot.get("released_task_id") != payload["released_task_id"]
            or slot.get("released_candidate_task_id")
            != payload["released_candidate_task_id"]
            or slot.get("fixed_branch") != payload["fixed_branch"]
        ):
            raise SoloAIError("Slot disposal generation or owner changed")
        if (
            slot.get("status") == "disposing"
            and slot.get("disposal_plan_id") == plan_id
        ):
            return
        if slot.get("status") != payload["slot_status"] or slot.get("disposal_plan_id"):
            raise SoloAIError("Slot disposal claim changed")
        slot["status"] = "disposing"
        slot["disposal_plan_id"] = plan_id

    store.mutate(update)


def _release(repo: GitRepo, payload: dict[str, Any], plan_id: str) -> None:
    store = StateStore(repo)

    def update(state: dict[str, Any]) -> None:
        slot = state["slots"].get(str(payload["slot"]))
        if (
            not slot
            or slot.get("generation") != payload["slot_generation"]
            or slot.get("task_id")
        ):
            raise SoloAIError("Completed disposal slot changed")
        if slot.get("status") != "disposing" or slot.get("disposal_plan_id") != plan_id:
            raise SoloAIError("Completed disposal claim changed")
        slot["status"] = payload["slot_status"]
        slot["disposal_plan_id"] = None
        slot["released_worktree_identity"] = None
        slot["released_worktree_resolved"] = None

    store.mutate(update)


def _cancel_before_move(
    repo: GitRepo, *, plan: dict[str, Any], plan_path: Path
) -> None:
    """仅在原树尚未移动且身份完整时撤销占槽；内容留在原位。"""
    payload = plan["payload"]
    plan_id = str(plan["id"])
    root = _check_slot(repo, payload, plan_id)
    stage = _stage(payload, plan_id)
    if stage.exists() or is_link_or_junction(stage):
        raise SoloAIError("Whole-slot staging has started; cancellation is unsafe")
    require_managed_directory_identity(
        root,
        managed_root=root.parent,
        expected_identity=payload["worktree_identity"],
        expected_root_identity=payload["managed_root_identity"],
    )
    _registration(repo, root, payload["branch"], payload["head"])
    if repo.branch(root) != payload["branch"] or repo.head(root) != payload["head"]:
        raise SoloAIError("Whole-slot Git state changed; cancellation is unsafe")
    if repo.git(["status", "--porcelain=v1", "--untracked-files=no"], cwd=root).stdout:
        raise SoloAIError("Whole-slot tracked files changed; cancellation is unsafe")
    if plan["status"] != "cancelling":
        plan["status"] = "cancelling"
        atomic_write_json(plan_path, plan)

    def update(state: dict[str, Any]) -> None:
        slot = state["slots"].get(str(payload["slot"]))
        if (
            not slot
            or slot.get("generation") != payload["slot_generation"]
            or slot.get("status") != "disposing"
            or slot.get("disposal_plan_id") != plan_id
            or slot.get("task_id")
        ):
            raise SoloAIError("Whole-slot cancellation claim changed")
        slot["status"] = payload["slot_status"]
        slot["disposal_plan_id"] = None

    StateStore(repo).mutate(update)
    plan["status"] = "cancelled"
    plan["cancelled_at"] = utc_timestamp()
    atomic_write_json(plan_path, plan)


def _remaining(
    stage: Path, expected: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    observed = _entries(stage)
    for relative, snapshot in observed.items():
        if relative not in expected or snapshot != expected[relative]:
            raise SoloAIError("Staged slot content changed after review")
    return observed


def dispose_slot(
    repo: GitRepo, *, slot: str, plan_id: str | None, confirm: str | None
) -> dict[str, Any]:
    """经确认后整槽暂存、逐对象删除，再注销Git登记。"""
    if re.fullmatch(r"(?:0[1-9]|[12][0-9]|3[0-2])", slot) is None:
        raise SoloAIError("Whole-slot disposal requires an exact slot id")
    if plan_id is None and confirm is None:
        payload = _payload(repo, slot)
        digest = sha256_text(stable_json(payload))
        plan_id = new_id(f"cleanup-slot-{slot}")
        plan = {
            "schema_version": 2,
            "id": plan_id,
            "digest": digest,
            "payload": payload,
            "status": "planned",
            "created_at": utc_timestamp(),
        }
        atomic_write_json(repo.local_dir / "cleanup-plans" / f"{plan_id}.json", plan)
        return {
            "status": "planned",
            "plan_id": plan_id,
            "digest": digest,
            **payload,
            "next": f"prune-slot --dispose --slot {slot} --plan {plan_id} --confirm {digest}",
        }
    if not plan_id or not confirm:
        raise SoloAIError("Whole-slot disposal requires both --plan and --confirm")
    if re.fullmatch(rf"cleanup-slot-{slot}-\d{{14}}-[0-9a-f]{{8}}", plan_id) is None:
        raise SoloAIError("Whole-slot disposal plan id is not an exact filename")
    plan_path = repo.local_dir / "cleanup-plans" / f"{plan_id}.json"
    plan = read_json(plan_path, {})
    if (
        plan.get("schema_version") != 2
        or plan.get("payload", {}).get("slot") != slot
        or plan.get("payload", {}).get("mode") != "dispose"
    ):
        raise SoloAIError("Unknown whole-slot disposal plan")
    if (
        plan.get("digest") != confirm
        or sha256_text(stable_json(plan["payload"])) != confirm
    ):
        raise SoloAIError("Whole-slot disposal confirmation changed")
    payload = plan["payload"]
    phase = str(plan.get("status"))
    root = Path(str(payload["worktree"]))
    stage = _stage(payload, plan_id)
    if phase == "cancelled":
        raise SoloAIError("Whole-slot disposal plan was cancelled; generate a new plan")
    if phase == "cancelling":
        state_slot = StateStore(repo).read()["slots"].get(slot) or {}
        if (
            state_slot.get("status") == "disposing"
            and state_slot.get("disposal_plan_id") == plan_id
        ):
            _cancel_before_move(repo, plan=plan, plan_path=plan_path)
        elif (
            state_slot.get("generation") == payload["slot_generation"]
            and state_slot.get("status") == payload["slot_status"]
            and not state_slot.get("disposal_plan_id")
            and not state_slot.get("task_id")
        ):
            plan["status"] = "cancelled"
            plan["cancelled_at"] = utc_timestamp()
            atomic_write_json(plan_path, plan)
        else:
            raise SoloAIError("Whole-slot cancellation slot changed")
        raise SoloAIError("Whole-slot disposal plan was cancelled; generate a new plan")
    if phase == "completed":
        state_slot = StateStore(repo).read()["slots"].get(slot) or {}
        if (
            state_slot.get("status") == "disposing"
            and state_slot.get("disposal_plan_id") == plan_id
        ):
            _check_slot(repo, payload, plan_id)
            if (
                root.exists()
                or is_link_or_junction(root)
                or stage.exists()
                or is_link_or_junction(stage)
                or any(item.path == root for item in repo.worktrees())
            ):
                raise SoloAIError(
                    "Completed disposal path or Git registration reappeared"
                )
            _release(repo, payload, plan_id)
            return {
                "status": "disposed",
                "slot": slot,
                "plan_id": plan_id,
                "removed": str(root),
                "git_unregistered": True,
                "slot_generation": payload["slot_generation"],
            }
        raise SoloAIError("Whole-slot disposal plan was already completed")
    if phase not in {
        "planned",
        "claiming",
        "renaming",
        "staged",
        "deleting",
        "unregistering",
    }:
        raise SoloAIError("Unsupported whole-slot disposal recovery state")
    if phase == "planned":
        current = _payload(repo, slot)
        current_base = str(current["base_head"])
        if (
            not repo.is_ancestor(str(payload["base_head"]), current_base)
            or not repo.is_ancestor(str(payload["head"]), current_base)
        ):
            raise SoloAIError("Whole-slot disposal base changed after review")
        current["base_head"] = payload["base_head"]
        if current != payload:
            raise SoloAIError(
                "Whole-slot disposal content or identity changed after review"
            )
        if stage.exists() or is_link_or_junction(stage):
            raise SoloAIError("Whole-slot disposal staging path already exists")
        plan["status"] = "claiming"
        atomic_write_json(plan_path, plan)
        phase = "claiming"
    if phase == "claiming":
        _claim(repo, payload, plan_id)
        _check_slot(repo, payload, plan_id)
        plan["status"] = "renaming"
        atomic_write_json(plan_path, plan)
        phase = "renaming"
    if phase == "renaming":
        _check_slot(repo, payload, plan_id)
        _registration(repo, root, payload["branch"], payload["head"])
        if stage.exists() or is_link_or_junction(stage):
            if root.exists() or is_link_or_junction(root):
                raise SoloAIError("Both original and staged slot paths exist")
            if path_identity(stage) != payload["worktree_identity"]:
                raise SoloAIError("Whole-slot staging directory was replaced")
        else:
            try:
                unchanged = _entries(root) == payload["entries"]
            except SoloAIError:
                unchanged = False
            if not unchanged:
                _cancel_before_move(repo, plan=plan, plan_path=plan_path)
                raise SoloAIError(
                    "Whole-slot content changed before staging; plan cancelled"
                )
            with pinned_plain_directory(root.parent, payload["managed_root_identity"]):
                if stage.exists() or is_link_or_junction(stage):
                    raise SoloAIError("Whole-slot staging path appeared")
                root.rename(stage)
        if _entries(stage) != payload["entries"]:
            raise SoloAIError("Whole-slot content changed while staging")
        plan["status"] = "staged"
        atomic_write_json(plan_path, plan)
        phase = "staged"
    if phase == "staged":
        _check_slot(repo, payload, plan_id)
        if root.exists() or is_link_or_junction(root) or is_link_or_junction(stage):
            raise SoloAIError("Whole-slot path changed after staging")
        if _entries(stage) != payload["entries"]:
            raise SoloAIError("Staged slot content changed after review")
        plan["status"] = "deleting"
        atomic_write_json(plan_path, plan)
        phase = "deleting"
    if phase == "deleting":
        _check_slot(repo, payload, plan_id)
        if root.exists() or is_link_or_junction(root) or is_link_or_junction(stage):
            raise SoloAIError("Whole-slot path changed during deletion")
        _registration(repo, root, payload["branch"], payload["head"])
        expected = payload["entries"]
        if stage.exists():
            if path_identity(stage) != payload["worktree_identity"]:
                raise SoloAIError("Whole-slot staging directory was replaced")
            observed = _remaining(stage, expected)
            for relative in sorted(
                observed, key=lambda key: (len(Path(key).parts), key), reverse=True
            ):
                path = stage if relative == "." else stage / relative
                snapshot = expected[relative]
                # 删除目标前固定完整的普通父目录链，避免替换为链接后重定向路径。
                with ExitStack() as pinned:
                    pinned.enter_context(
                        pinned_plain_directory(
                            stage.parent, payload["managed_root_identity"]
                        )
                    )
                    parent = stage
                    ancestors = [] if relative == "." else [stage]
                    for part in Path(relative).parts[:-1]:
                        parent = parent / part
                        ancestors.append(parent)
                    for ancestor in ancestors:
                        key = (
                            "."
                            if ancestor == stage
                            else ancestor.relative_to(stage).as_posix()
                        )
                        frozen = expected.get(key)
                        if not frozen or frozen["kind"] != "directory":
                            raise SoloAIError(
                                "Whole-slot parent was not a frozen directory"
                            )
                        pinned.enter_context(pinned_plain_directory(ancestor, frozen))
                    if snapshot["kind"] == "link":
                        delete_link_path_if_unchanged(path, snapshot)
                    else:
                        delete_plain_path_if_unchanged(path, snapshot)
        if stage.exists() or is_link_or_junction(stage):
            raise SoloAIError("Whole-slot staging directory remains")
        plan["status"] = "unregistering"
        atomic_write_json(plan_path, plan)
        phase = "unregistering"
    if phase == "unregistering":
        _check_slot(repo, payload, plan_id)
        if (
            root.exists()
            or is_link_or_junction(root)
            or stage.exists()
            or is_link_or_junction(stage)
        ):
            raise SoloAIError("Whole-slot path reappeared before Git removal")
        matches = [item for item in repo.worktrees() if item.path == root]
        if matches:
            _registration(repo, root, payload["branch"], payload["head"])
            repo.git(["worktree", "remove", str(root)], cwd=repo.primary_path)
        if any(item.path == root for item in repo.worktrees()):
            raise SoloAIError("Whole-slot Git registration remains")
        plan["status"] = "completed"
        plan["completed_at"] = utc_timestamp()
        atomic_write_json(plan_path, plan)
    _release(repo, payload, plan_id)
    return {
        "status": "disposed",
        "slot": slot,
        "plan_id": plan_id,
        "removed": str(root),
        "git_unregistered": True,
        "slot_generation": payload["slot_generation"],
    }
