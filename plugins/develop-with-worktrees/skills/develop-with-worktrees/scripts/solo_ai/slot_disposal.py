from __future__ import annotations

import os
import re
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from .cleanup import require_managed_directory_identity
from .config import load_repo_config
from .repo import GitRepo
from .state import FINAL_TASK_STATES, LEGACY_STATE_SCHEMA, StateStore
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


def _same_volume_object_fields(recorded: Any, current: Any) -> bool:
    """目录身份字段须为精确整数，inode 与 mode 不容许漂移。"""
    return (
        isinstance(recorded, dict)
        and isinstance(current, dict)
        and set(recorded) == set(current)
        and all(
            type(recorded.get(field)) is int and type(current.get(field)) is int
            for field in ("device", "inode", "mode")
        )
        and recorded["inode"] == current["inode"]
        and recorded["mode"] == current["mode"]
    )


def _released_identity(
    slot: dict[str, Any], root: Path, *, recover_volume: bool
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    parent = root.parent
    require_managed_directory_identity(root, managed_root=parent)
    current_worktree = path_identity(root)
    current_parent = path_identity(parent)
    recorded_worktree = slot.get("released_worktree_identity")
    recorded_parent = slot.get("released_managed_root_identity")
    resolved_worktree = slot.get("released_worktree_resolved")
    resolved_parent = slot.get("released_managed_root_resolved")
    if resolved_worktree and resolved_worktree != str(root.resolve()):
        raise SoloAIError("Released slot worktree resolved path changed")
    if resolved_parent and resolved_parent != str(parent.resolve()):
        raise SoloAIError("Released managed root resolved path changed")
    for label, recorded, current in (
        ("Managed root directory", recorded_parent, current_parent),
        ("Managed worktree directory", recorded_worktree, current_worktree),
    ):
        if recorded is not None:
            if not _same_volume_object_fields(recorded, current) or (
                recorded["device"] != current["device"] and not recover_volume
            ):
                raise SoloAIError(
                    f"{label} object was replaced: expected={recorded!r}; "
                    f"observed={current!r}"
                )
    if not recover_volume:
        return current_worktree, current_parent, None
    if not (
        isinstance(recorded_worktree, dict)
        and isinstance(recorded_parent, dict)
        and resolved_worktree == str(root.resolve())
        and resolved_parent == str(parent.resolve())
    ):
        raise SoloAIError(
            "Volume-only slot disposal requires complete released identities"
        )
    transition = (recorded_parent["device"], current_parent["device"])
    if transition != (recorded_worktree["device"], current_worktree["device"]):
        raise SoloAIError(
            "Released slot and managed root disagree on volume transition: "
            f"worktree expected={recorded_worktree!r}, observed={current_worktree!r}; "
            f"parent expected={recorded_parent!r}, observed={current_parent!r}"
        )
    if transition[0] == transition[1]:
        raise SoloAIError("Volume-only slot disposal requires a changed device")
    return (
        current_worktree,
        current_parent,
        {
            "released_worktree_identity": recorded_worktree,
            "released_managed_root_identity": recorded_parent,
            "released_worktree_resolved": resolved_worktree,
            "released_managed_root_resolved": resolved_parent,
            "from_device": transition[0],
            "to_device": transition[1],
        },
    )


def _legacy_reclaimed_predecessor(
    repo: GitRepo,
    state: dict[str, Any],
    slot_id: str,
    slot: dict[str, Any],
    root: Path,
    head: str,
    branch: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """只证明 schema 11 同代保留工位已经正式回收，旧候选指针不能充当前驱。"""
    generation = slot.get("generation")
    stale_id = slot.get("released_candidate_task_id")
    stale = state["tasks"].get(str(stale_id))
    if (
        state.get("schema_version") != LEGACY_STATE_SCHEMA
        or slot.get("released_task_id") is not None
        or not stale_id
        or branch is not None
        or type(generation) is not int
        or not isinstance(stale, dict)
        or stale.get("slot_id") != slot_id
        or stale.get("worktree") != str(root)
        or type(stale.get("slot_generation")) is not int
        or stale["slot_generation"] >= generation
    ):
        raise SoloAIError("Idle slot has no exact released predecessor")
    matching = [
        (task_id, task)
        for task_id, task in state["tasks"].items()
        if isinstance(task, dict)
        and task.get("slot_id") == slot_id
        and type(task.get("slot_generation")) is int
        and task.get("slot_generation") == generation
    ]
    if len(matching) != 1:
        raise SoloAIError("Idle slot has no unique reclaimed predecessor")
    task_id, task = matching[0]
    abandonment = task.get("abandonment")
    reclaim = task.get("retained_reclaim")
    if not isinstance(abandonment, dict) or not isinstance(reclaim, dict):
        raise SoloAIError("Idle slot has no completed retained reclaim")
    task_branch = task.get("branch")
    branch_ref_head = (
        repo.ref_head(f"refs/heads/{task_branch}")
        if isinstance(task_branch, str) and task_branch
        else None
    )
    if (
        task.get("id") != task_id
        or type(task.get("slot_generation")) is not int
        or task.get("worktree") != str(root)
        or task.get("status") != "abandoned"
        or task.get("active_operation") is not None
        or task.get("lease") is not None
        or task.get("retained_disposal")
        or abandonment.get("schema_version") != 1
        or abandonment.get("phase") != "completed"
        or not abandonment.get("completed_at")
        or abandonment.get("retained_worktree") is not True
        or abandonment.get("task_id") != task_id
        or abandonment.get("slot_id") != slot_id
        or (
            "slot_generation" in abandonment
            and (
                type(abandonment["slot_generation"]) is not int
                or abandonment["slot_generation"] != generation
            )
        )
        or abandonment.get("worktree") != str(root)
        or abandonment.get("managed_root") != str(root.parent)
        or abandonment.get("base_ref") != task.get("base_ref")
        or not isinstance(task.get("base_ref"), str)
        or not task["base_ref"]
        or abandonment.get("branch") != task_branch
        or reclaim.get("schema_version") != 1
        or reclaim.get("phase") != "completed"
        or not reclaim.get("completed_at")
        or not reclaim.get("transaction_id")
        or reclaim.get("task_id") != task_id
        or reclaim.get("slot_id") != slot_id
        or type(reclaim.get("slot_generation")) is not int
        or reclaim.get("slot_generation") != generation
        or reclaim.get("abandonment_transaction_id")
        != abandonment.get("transaction_id")
        or not abandonment.get("transaction_id")
        or reclaim.get("worktree") != str(root)
        or reclaim.get("branch") != task_branch
        or reclaim.get("branch_tip") != abandonment.get("branch_tip")
        or reclaim.get("base_head") != abandonment.get("base_head")
        or reclaim.get("base_head") != head
        or not isinstance(task_branch, str)
        or not task_branch
        or (
            branch_ref_head is not None
            and branch_ref_head != abandonment.get("branch_tip")
        )
        or (branch_ref_head is None and reclaim.get("branch_tip") != head)
    ):
        raise SoloAIError("Idle slot retained reclaim proof changed")
    for slot_field, abandonment_field in (
        ("released_worktree_identity", "worktree_identity"),
        ("released_managed_root_identity", "managed_root_identity"),
    ):
        released = slot.get(slot_field)
        frozen = abandonment.get(abandonment_field)
        if not _same_volume_object_fields(released, frozen) or released != frozen:
            raise SoloAIError("Idle slot retained reclaim directory identity changed")
    for slot_field, abandonment_field in (
        ("released_worktree_resolved", "worktree_resolved"),
        ("released_managed_root_resolved", "managed_root_resolved"),
    ):
        released = slot.get(slot_field)
        if (
            not isinstance(released, str)
            or not released
            or released != abandonment.get(abandonment_field)
        ):
            raise SoloAIError("Idle slot retained reclaim resolved path changed")
    return task, {
        "task_id": task_id,
        "branch": task_branch,
        "base_ref": task["base_ref"],
        "branch_ref_head": branch_ref_head,
        "abandonment_transaction_id": abandonment["transaction_id"],
        "reclaim_transaction_id": reclaim["transaction_id"],
        "branch_tip": reclaim["branch_tip"],
        "base_head": head,
        "released_worktree_identity": slot["released_worktree_identity"],
        "released_managed_root_identity": slot["released_managed_root_identity"],
        "released_worktree_resolved": slot["released_worktree_resolved"],
        "released_managed_root_resolved": slot["released_managed_root_resolved"],
    }


def _check_legacy_reclaim(
    repo: GitRepo,
    state: dict[str, Any],
    slot: dict[str, Any],
    root: Path,
    payload: dict[str, Any],
) -> None:
    if "legacy_retained_reclaim" not in payload:
        return
    _, proof = _legacy_reclaimed_predecessor(
        repo,
        state,
        str(payload["slot"]),
        slot,
        root,
        str(payload["head"]),
        payload["branch"],
    )
    if proof != payload["legacy_retained_reclaim"]:
        raise SoloAIError("Whole-slot retained reclaim proof changed")


def _payload(
    repo: GitRepo, slot_id: str, *, recover_volume: bool = False
) -> dict[str, Any]:
    store, slot, root = _slot(repo, slot_id)
    worktree_identity, parent_identity, recovery = _released_identity(
        slot, root, recover_volume=recover_volume
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
    legacy_reclaim = None
    if predecessor_id:
        task = store.task(str(predecessor_id))
        if task.get("slot_generation") != slot.get("generation"):
            task, legacy_reclaim = _legacy_reclaimed_predecessor(
                repo, store.read(), slot_id, slot, root, head, branch
            )
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
                or legacy_reclaim is not None
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
    payload = {
        "mode": "dispose",
        "slot": slot_id,
        "slot_status": slot["status"],
        "slot_generation": int(slot["generation"]),
        "released_task_id": slot.get("released_task_id"),
        "released_candidate_task_id": slot.get("released_candidate_task_id"),
        "fixed_branch": slot.get("fixed_branch"),
        "worktree": str(root),
        "worktree_identity": worktree_identity,
        "managed_root_identity": parent_identity,
        "branch": branch,
        "detached": branch is None,
        "head": head,
        "base_ref": base_ref,
        "base_head": base_head,
        "entries": _entries(root),
    }
    if recovery is not None:
        payload["volume_recovery"] = recovery
    if legacy_reclaim is not None:
        payload["legacy_retained_reclaim"] = legacy_reclaim
    return payload


def _stage(payload: dict[str, Any], plan_id: str) -> Path:
    root = Path(str(payload["worktree"]))
    return root.parent / f".{root.name}.dww-dispose-{plan_id}"


def _check_slot(repo: GitRepo, payload: dict[str, Any], plan_id: str) -> Path:
    if payload.get("slot_status") not in {"idle", "inactive"}:
        raise SoloAIError("Whole-slot plan has an invalid original slot status")
    store, slot, root = _slot(repo, str(payload["slot"]), plan_id=plan_id)
    if payload.get("worktree") != str(root):
        raise SoloAIError("Whole-slot plan worktree path changed")
    _check_volume_record(slot, payload)
    _check_legacy_reclaim(repo, store.read(), slot, root, payload)
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


def _check_volume_record(slot: dict[str, Any], payload: dict[str, Any]) -> None:
    recovery = payload.get("volume_recovery")
    if recovery is None:
        return
    if not isinstance(recovery, dict):
        raise SoloAIError("Whole-slot volume recovery record changed")
    for field in (
        "released_worktree_identity",
        "released_managed_root_identity",
        "released_worktree_resolved",
        "released_managed_root_resolved",
    ):
        if slot.get(field) != recovery.get(field):
            raise SoloAIError(f"Whole-slot released identity changed: {field}")
    old_worktree = recovery["released_worktree_identity"]
    old_parent = recovery["released_managed_root_identity"]
    current_worktree = payload["worktree_identity"]
    current_parent = payload["managed_root_identity"]
    if not (
        isinstance(old_worktree, dict)
        and isinstance(old_parent, dict)
        and isinstance(current_worktree, dict)
        and isinstance(current_parent, dict)
        and recovery.get("from_device")
        == old_worktree.get("device")
        == old_parent.get("device")
        and recovery.get("to_device")
        == current_worktree.get("device")
        == current_parent.get("device")
        and recovery["from_device"] != recovery["to_device"]
        and all(
            _same_volume_object_fields(old, current)
            for old, current in (
                (old_worktree, current_worktree),
                (old_parent, current_parent),
            )
        )
    ):
        raise SoloAIError("Whole-slot volume-only identity proof changed")


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
        _check_legacy_reclaim(
            repo, state, slot, Path(str(payload["worktree"])), payload
        )
        _check_volume_record(slot, payload)
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
        slot["released_managed_root_identity"] = None
        slot["released_managed_root_resolved"] = None

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


def _delete_observed_windows(
    stage: Path,
    payload: dict[str, Any],
    observed: dict[str, dict[str, Any]],
) -> None:
    """沿冻结目录树持有父目录句柄，每个普通目录只固定一次。"""
    expected = payload["entries"]
    children: dict[str, list[str]] = {}
    for relative in observed:
        if relative != ".":
            parent = Path(relative).parent.as_posix()
            children.setdefault(parent, []).append(relative)

    with pinned_plain_directory(stage.parent, payload["managed_root_identity"]):
        # 显式进入/退出栈保持父链句柄；目录自身关闭后才条件删除。
        pending: list[tuple[str, bool]] = [(".", False)]
        held: list[ExitStack] = []
        try:
            while pending:
                relative, leaving = pending.pop()
                path = stage if relative == "." else stage / relative
                snapshot = expected[relative]
                if leaving:
                    held.pop().close()
                    delete_plain_path_if_unchanged(
                        path, snapshot, posix_disposition=True
                    )
                elif snapshot["kind"] == "directory":
                    current = ExitStack()
                    current.enter_context(pinned_plain_directory(path, snapshot))
                    held.append(current)
                    pending.append((relative, True))
                    pending.extend(
                        (child, False)
                        for child in reversed(sorted(children.get(relative, [])))
                    )
                elif snapshot["kind"] == "link":
                    delete_link_path_if_unchanged(
                        path, snapshot, posix_disposition=True
                    )
                else:
                    delete_plain_path_if_unchanged(
                        path, snapshot, posix_disposition=True
                    )
        finally:
            for current in reversed(held):
                current.close()


def dispose_slot(
    repo: GitRepo,
    *,
    slot: str,
    plan_id: str | None,
    confirm: str | None,
    recover_volume: bool = False,
) -> dict[str, Any]:
    """经确认后整槽暂存、逐对象删除，再注销Git登记。"""
    if re.fullmatch(r"(?:0[1-9]|[12][0-9]|3[0-2])", slot) is None:
        raise SoloAIError("Whole-slot disposal requires an exact slot id")
    if plan_id is None and confirm is None:
        payload = _payload(repo, slot, recover_volume=recover_volume)
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
            "next": (
                f"prune-slot --dispose {'--recover-volume ' if recover_volume else ''}"
                f"--slot {slot} --plan {plan_id} --confirm {digest}"
            ),
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
    if bool(payload.get("volume_recovery")) != recover_volume:
        raise SoloAIError("Whole-slot volume recovery mode changed from its plan")
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
        current = _payload(repo, slot, recover_volume=recover_volume)
        current_base = str(current["base_head"])
        if not repo.is_ancestor(
            str(payload["base_head"]), current_base
        ) or not repo.is_ancestor(str(payload["head"]), current_base):
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
            if os.name == "nt":
                _delete_observed_windows(stage, payload, observed)
            else:
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
                            pinned.enter_context(
                                pinned_plain_directory(ancestor, frozen)
                            )
                        if snapshot["kind"] == "link":
                            delete_link_path_if_unchanged(
                                path, snapshot, posix_disposition=True
                            )
                        else:
                            delete_plain_path_if_unchanged(
                                path, snapshot, posix_disposition=True
                            )
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
