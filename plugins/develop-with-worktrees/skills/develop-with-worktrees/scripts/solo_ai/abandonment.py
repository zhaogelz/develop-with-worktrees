from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from .cleanup import (
    inspect_untracked,
    remove_abandoned_untracked,
    require_managed_directory_identity,
)
from .repo import GitRepo
from .state import FINAL_TASK_STATES, StateStore
from .util import SoloAIError, atomic_write_json, read_json, utc_timestamp
from .util import path_identity, snapshot_plain_path

ABANDONMENT_SCHEMA = 1
ABANDONMENT_RECEIPT_SCHEMA = 1


def _receipt_path(repo: GitRepo, task_id: str) -> Path:
    return repo.local_dir / "abandonment-receipts" / f"{task_id}.json"


def _assert_no_active_reference(
    repo: GitRepo, store: StateStore, *, task: dict[str, Any], candidate: str
) -> None:
    if candidate == task["base_head"]:
        return
    for other in store.read()["tasks"].values():
        if other.get("id") == task["id"] or other.get("status") in FINAL_TASK_STATES:
            continue
        branch = other.get("branch")
        if not branch:
            continue
        head = repo.ref_head(f"refs/heads/{branch}")
        if head and repo.is_ancestor(candidate, head):
            raise SoloAIError(
                f"Candidate is still referenced by active task {other['id']}"
            )


def _active_ref_snapshot(
    repo: GitRepo, store: StateStore, *, task: dict[str, Any], candidate: str
) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for other in store.read()["tasks"].values():
        if other.get("id") == task["id"] or other.get("status") in FINAL_TASK_STATES:
            continue
        branch = other.get("branch")
        if not branch:
            continue
        ref = f"refs/heads/{branch}"
        head = repo.ref_head(ref)
        if head is None:
            raise SoloAIError(f"Active task branch is missing: {other['id']}")
        if repo.is_ancestor(candidate, head):
            raise SoloAIError(f"Candidate is still referenced by active task {other['id']}")
        snapshot[ref] = head
    return snapshot


def new_transaction(
    repo: GitRepo, store: StateStore, *, task: dict[str, Any]
) -> dict[str, Any]:
    active = task.get("active_operation") or {}
    operation_id = str(active.get("id") or "")
    if not operation_id:
        raise SoloAIError("Abandon operation identity is missing")
    worktree = Path(str(task["worktree"]))
    managed_root = worktree.absolute().parent
    ensure_root = repo.primary_path.resolve()
    try:
        managed_root.relative_to(ensure_root)
    except ValueError as exc:
        raise SoloAIError("Task worktree is outside the repository managed area") from exc
    resolved_worktree = require_managed_directory_identity(
        worktree, managed_root=managed_root
    )
    if not any(item.path == resolved_worktree for item in repo.worktrees()):
        raise SoloAIError("Task worktree is no longer registered")
    branch_ref = f"refs/heads/{task['branch']}"
    expected_tip = repo.ref_head(branch_ref)
    if expected_tip is None:
        raise SoloAIError("Task branch is missing before abandonment")
    if repo.branch(worktree) != task["branch"] or repo.head(worktree) != expected_tip:
        raise SoloAIError("Task worktree or branch identity changed before abandonment")
    base_head = repo.ref_head(f"refs/heads/{task['base_ref']}")
    if base_head is None:
        raise SoloAIError("Recorded base branch is missing")
    if expected_tip != task["base_head"] and repo.is_ancestor(expected_tip, base_head):
        raise SoloAIError(
            "Task candidate is already integrated; Recover must finish cleanup"
        )
    _assert_no_active_reference(repo, store, task=task, candidate=expected_tip)
    tracked_status = repo.git(
        ["status", "--porcelain=v1", "--untracked-files=no"], cwd=worktree
    ).stdout
    if tracked_status:
        raise SoloAIError(
            "Tracked worktree changes block abandon; preserve or commit them first"
        )
    inventory = inspect_untracked(repo, cwd=worktree)
    blocked = [
        *inventory["keep"],
        *inventory["protected"],
        *inventory["unknown_ignored"],
    ]
    if blocked:
        raise SoloAIError(
            "Retained, protected, or unknown ignored content blocks abandon:\n"
            + "\n".join(f"- {item}" for item in blocked[:20])
        )
    ordinary = {
        relative: snapshot_plain_path(worktree / relative)
        for relative in inventory["ordinary"]
    }
    return {
        "schema_version": ABANDONMENT_SCHEMA,
        "transaction_id": uuid.uuid4().hex,
        "phase": "prepared",
        "prepared_by_operation_id": operation_id,
        "task_id": task["id"],
        "slot_id": task["slot_id"],
        "worktree": task["worktree"],
        "worktree_resolved": str(resolved_worktree),
        "managed_root": str(managed_root),
        "managed_root_resolved": str(managed_root.resolve()),
        "managed_root_identity": path_identity(managed_root),
        "worktree_identity": path_identity(worktree),
        "branch": task["branch"],
        "branch_tip": expected_tip,
        "base_ref": task["base_ref"],
        "base_head": base_head,
        "tracked_status": tracked_status,
        "ordinary_untracked": ordinary,
        "prepared_at": utc_timestamp(),
    }


def _assert_identity(task: dict[str, Any], transaction: dict[str, Any]) -> None:
    expected = {
        "task_id": task["id"],
        "slot_id": task["slot_id"],
        "worktree": task["worktree"],
        "branch": task["branch"],
        "base_ref": task["base_ref"],
    }
    if transaction.get("schema_version") != ABANDONMENT_SCHEMA:
        raise SoloAIError("Unsupported abandonment transaction schema")
    if transaction.get("phase") not in {"prepared", "completed"}:
        raise SoloAIError("Unsupported abandonment transaction phase")
    for key, value in expected.items():
        if transaction.get(key) != value:
            raise SoloAIError(f"Abandonment transaction identity changed: {key}")


def write_completed_receipt(repo: GitRepo, task: dict[str, Any]) -> dict[str, Any]:
    transaction = dict(task["abandonment"])
    _assert_identity(task, transaction)
    if task.get("status") != "abandoned" or transaction.get("phase") != "completed":
        raise SoloAIError("Abandonment state is not completed")
    expected = {
        **transaction,
        "schema_version": ABANDONMENT_RECEIPT_SCHEMA,
        "status": "completed",
        "updated_at": utc_timestamp(),
    }
    path = _receipt_path(repo, task["id"])
    existing = read_json(path, {})
    if existing:
        for key in ("transaction_id", "task_id", "branch", "branch_tip"):
            if existing.get(key) != expected.get(key):
                raise SoloAIError(f"Abandonment receipt conflicts with state: {key}")
    atomic_write_json(path, expected)
    return expected


def prepare(
    repo: GitRepo, *, store: StateStore, task: dict[str, Any]
) -> dict[str, Any]:
    transaction = new_transaction(repo, store, task=task)
    return store.prepare_abandonment(
        task["id"],
        operation_id=str(transaction["prepared_by_operation_id"]),
        abandonment=transaction,
    )


def resume(
    repo: GitRepo, *, store: StateStore, task: dict[str, Any]
) -> dict[str, Any]:
    transaction = task.get("abandonment")
    if not transaction:
        raise SoloAIError("Task has no abandonment transaction")
    _assert_identity(task, transaction)
    worktree = Path(str(transaction["worktree"]))
    resolved_worktree = require_managed_directory_identity(
        worktree,
        managed_root=Path(str(transaction["managed_root"])),
        expected_resolved=str(transaction["worktree_resolved"]),
        expected_root_resolved=str(transaction["managed_root_resolved"]),
        expected_identity=dict(transaction["worktree_identity"]),
        expected_root_identity=dict(transaction["managed_root_identity"]),
    )
    if not worktree.is_dir() or not any(
        item.path == resolved_worktree for item in repo.worktrees()
    ):
        raise SoloAIError("Abandonment worktree is missing or unregistered")
    expected_tip = str(transaction["branch_tip"])
    base_head = str(transaction["base_head"])
    current_branch = repo.branch(worktree)
    current_head = repo.head(worktree)
    if current_branch == transaction["branch"]:
        if current_head != expected_tip or repo.ref_head(
            f"refs/heads/{transaction['branch']}"
        ) != expected_tip:
            raise SoloAIError("Task branch changed during abandonment")
        current_tracked_status = repo.git(
            ["status", "--porcelain=v1", "--untracked-files=no"], cwd=worktree
        ).stdout
        if current_tracked_status != transaction.get("tracked_status", ""):
            raise SoloAIError("Tracked worktree content changed during abandonment")
        remove_abandoned_untracked(
            repo,
            cwd=worktree,
            expected_ordinary=dict(transaction.get("ordinary_untracked") or {}),
        )
        current_tracked_status = repo.git(
            ["status", "--porcelain=v1", "--untracked-files=no"], cwd=worktree
        ).stdout
        if current_tracked_status != transaction.get("tracked_status", ""):
            raise SoloAIError("Tracked worktree content changed during abandonment")
        inventory = inspect_untracked(repo, cwd=worktree)
        if any(
            inventory[key]
            for key in ("keep", "protected", "ordinary", "unknown_ignored")
        ):
            raise SoloAIError("Worktree content changed during abandonment")
        # 先从任务分支脱离，再在 detached HEAD 上丢弃已登记的旧改动；
        # 这样外部并发推进任务 ref 时不会被 reset 回旧提交。
        repo.git(["switch", "--detach", base_head], cwd=worktree)
    elif current_branch is None:
        if current_head != base_head:
            raise SoloAIError("Detached abandonment worktree HEAD changed")
    else:
        raise SoloAIError("Abandonment worktree switched to another branch")
    _assert_releasable_worktree(repo, worktree=worktree, transaction=transaction)
    branch_ref = f"refs/heads/{transaction['branch']}"
    branch_head = repo.ref_head(branch_ref)
    if branch_head not in {None, expected_tip}:
        raise SoloAIError("Task branch advanced during abandonment")
    if branch_head is not None:
        verifications = _active_ref_snapshot(
            repo, store, task=task, candidate=expected_tip
        )
        repo.delete_ref_with_verifications(
            branch_ref,
            expected=expected_tip,
            verifications=verifications,
        )
        if repo.ref_head(branch_ref) is not None:
            raise SoloAIError("Task branch still exists after atomic deletion")
    _assert_releasable_worktree(repo, worktree=worktree, transaction=transaction)
    completed = store.complete_abandonment(
        task["id"], transaction_id=str(transaction["transaction_id"])
    )
    try:
        _assert_releasable_worktree(repo, worktree=worktree, transaction=transaction)
    except Exception as exc:
        store.quarantine_released_slot(
            task["id"], f"Worktree changed at abandonment release: {exc}"
        )
        raise
    completed = store.publish_abandonment_release(
        task["id"], transaction_id=str(transaction["transaction_id"])
    )
    try:
        _assert_releasable_worktree(repo, worktree=worktree, transaction=transaction)
    except Exception as exc:
        store.quarantine_released_slot(
            task["id"], f"Worktree changed after abandonment release: {exc}"
        )
        raise
    receipt = write_completed_receipt(repo, completed)
    return {
        "task_id": task["id"],
        "status": "abandoned",
        "transaction_id": receipt["transaction_id"],
    }


def _assert_releasable_worktree(
    repo: GitRepo, *, worktree: Path, transaction: dict[str, Any]
) -> None:
    require_managed_directory_identity(
        worktree,
        managed_root=Path(str(transaction["managed_root"])),
        expected_resolved=str(transaction["worktree_resolved"]),
        expected_root_resolved=str(transaction["managed_root_resolved"]),
        expected_identity=dict(transaction["worktree_identity"]),
        expected_root_identity=dict(transaction["managed_root_identity"]),
    )
    if (
        repo.branch(worktree) is not None
        or repo.head(worktree) != str(transaction["base_head"])
        or not repo.is_clean(worktree)
    ):
        raise SoloAIError("Abandonment worktree changed before release")
    inventory = inspect_untracked(repo, cwd=worktree)
    blocked = [
        *inventory["keep"],
        *inventory["protected"],
        *inventory["ordinary"],
        *inventory["unknown_ignored"],
    ]
    if blocked:
        raise SoloAIError(
            "Worktree content changed during abandonment; files were preserved:\n"
            + "\n".join(f"- {item}" for item in blocked[:20])
        )
