from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from .cleanup import (
    inspect_untracked,
    remove_abandoned_untracked,
    require_managed_directory_identity,
)
from .config import load_repo_config
from .repo import GitRepo
from .state import FINAL_TASK_STATES, StateStore, candidate_admission_lock
from .util import (
    SoloAIError,
    atomic_write_json,
    is_link_or_junction,
    path_identity,
    pinned_plain_directory,
    read_json,
    remove_tree_without_following_links,
    sha256_file,
    sha256_text,
    snapshot_plain_path,
    stable_json,
    utc_timestamp,
)

ABANDONMENT_SCHEMA = 1
ABANDONMENT_RECEIPT_SCHEMA = 1
RETAINED_RECLAIM_SCHEMA = 1
RETAINED_DISPOSAL_SCHEMA = 1


def _receipt_path(repo: GitRepo, task_id: str) -> Path:
    return repo.local_dir / "abandonment-receipts" / f"{task_id}.json"


def _disposal_receipt_path(repo: GitRepo, task_id: str) -> Path:
    return repo.local_dir / "retained-disposal-receipts" / f"{task_id}.json"


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


def assert_task_not_held_by_candidate_delivery(
    repo: GitRepo, *, task: dict[str, Any]
) -> None:
    """候选已交给队列或批次时，任务的本地工作树不再拥有完整交付控制权。"""
    from .candidate_batches import CandidateBatchStore

    ownership = CandidateBatchStore(repo).task_batch_ownership(str(task["id"]))
    if not ownership:
        return
    candidate = ownership["candidate"]
    batch = ownership.get("batch")
    if batch:
        raise SoloAIError(
            "Abandon blocked: candidate "
            f"{candidate['id']} is held by active {batch['kind']} batch "
            f"{batch['id']} at phase {batch['phase']}; recover, wait for, or "
            "withdraw the candidate instead"
        )
    raise SoloAIError(
        "Abandon blocked: candidate "
        f"{candidate['id']} is still queued for integration; withdraw the candidate instead"
    )


def _active_ref_snapshot(
    repo: GitRepo, store: StateStore, *, task: dict[str, Any], candidate: str
) -> dict[str, str]:
    # 任务分支若正好停在自己的起始基线，后代任务只引用了仍由 base
    # 分支保留的提交；删除这条冗余任务分支不会使后代失去祖先。
    if candidate == task["base_head"]:
        return {}
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
            raise SoloAIError(
                f"Candidate is still referenced by active task {other['id']}"
            )
        snapshot[ref] = head
    return snapshot


def _audit_metadata(*, reason: str | None, source: str, action: str) -> dict[str, Any]:
    if source not in {"api", "cli"}:
        raise SoloAIError("Abandonment audit source is unsupported")
    normalized = None if reason is None else reason.strip()
    if normalized is not None and (
        not normalized or "\r" in normalized or "\n" in normalized
    ):
        raise SoloAIError("Abandonment reason must be one non-empty line")
    if normalized is not None and len(normalized) > 240:
        raise SoloAIError("Abandonment reason must be at most 240 characters")
    return {
        "action": action,
        "reason": normalized,
        "source": source,
        "started_at": utc_timestamp(),
    }


def in_place_audit(
    task: dict[str, Any], *, reason: str | None, source: str
) -> dict[str, Any]:
    """冻结兼容路径的放弃原因；重试只能续用原始事实。"""

    existing = task.get("abandonment_audit")
    if existing:
        if existing.get("action") != "abandon":
            raise SoloAIError("In-place abandonment audit identity changed")
        return dict(existing)
    return _audit_metadata(reason=reason, source=source, action="abandon")


def assert_retained_worktree_safe(task: dict[str, Any]) -> None:
    """保留式终止不接管仍可能改写现场的受管资源。"""

    if task.get("processes"):
        raise SoloAIError("Retained abandon requires no registered development process")
    activation = task.get("runtime_activation")
    if isinstance(activation, dict) and activation.get("configured") is not False:
        raise SoloAIError(
            "Retained abandon requires a task without runtime adapter activation"
        )


def new_transaction(
    repo: GitRepo,
    store: StateStore,
    *,
    task: dict[str, Any],
    reason: str | None,
    source: str,
    retain_worktree: bool = False,
) -> dict[str, Any]:
    active = task.get("active_operation") or {}
    operation_id = str(active.get("id") or "")
    if not operation_id:
        raise SoloAIError("Abandon operation identity is missing")
    worktree = Path(str(task["worktree"]))
    managed_root = worktree.absolute().parent
    ensure_root = store.managed_worktree_root(
        load_repo_config(repo, cwd=repo.policy_path())
    ).resolve()
    try:
        managed_root.relative_to(ensure_root)
    except ValueError as exc:
        raise SoloAIError(
            "Task worktree is outside the repository managed area"
        ) from exc
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
    assert_task_not_held_by_candidate_delivery(repo, task=task)
    _assert_no_active_reference(repo, store, task=task, candidate=expected_tip)
    tracked_status = repo.git(
        ["status", "--porcelain=v1", "--untracked-files=no"], cwd=worktree
    ).stdout
    ordinary: dict[str, dict[str, Any]] = {}
    retained_status: str | None = None
    if retain_worktree:
        retained_status = repo.git(
            ["status", "--porcelain=v1", "--untracked-files=all"], cwd=worktree
        ).stdout
    else:
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
    transaction = {
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
        "audit": _audit_metadata(reason=reason, source=source, action="abandon"),
        "prepared_at": utc_timestamp(),
    }
    if retain_worktree:
        transaction.update(
            {
                "retained_worktree": True,
                "retained_status": retained_status,
            }
        )
    return transaction


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
    repo: GitRepo,
    *,
    store: StateStore,
    task: dict[str, Any],
    reason: str | None,
    source: str,
) -> dict[str, Any]:
    # 与候选封存共用准入锁：检查完成并把任务置入 abandonment 前，批次不能抢占它。
    with candidate_admission_lock(repo):
        transaction = new_transaction(
            repo, store, task=task, reason=reason, source=source
        )
        return store.prepare_abandonment(
            task["id"],
            operation_id=str(transaction["prepared_by_operation_id"]),
            abandonment=transaction,
        )


def prepare_retained(
    repo: GitRepo,
    *,
    store: StateStore,
    task: dict[str, Any],
    reason: str,
    source: str,
) -> dict[str, Any]:
    """登记保留式终止，但绝不清理、重置或删除任务工作树。"""

    with candidate_admission_lock(repo):
        transaction = new_transaction(
            repo,
            store,
            task=task,
            reason=reason,
            source=source,
            retain_worktree=True,
        )
        return store.prepare_abandonment(
            task["id"],
            operation_id=str(transaction["prepared_by_operation_id"]),
            abandonment=transaction,
        )


def resume(repo: GitRepo, *, store: StateStore, task: dict[str, Any]) -> dict[str, Any]:
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
        if (
            current_head != expected_tip
            or repo.ref_head(f"refs/heads/{transaction['branch']}") != expected_tip
        ):
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


def resume_retained(
    repo: GitRepo, *, store: StateStore, task: dict[str, Any]
) -> dict[str, Any]:
    """完成保留式终止，只变更 DWW 记录并把槽位留在隔离状态。"""

    transaction = task.get("abandonment")
    if not transaction or transaction.get("retained_worktree") is not True:
        raise SoloAIError("Task has no retained-worktree abandonment transaction")
    _assert_identity(task, transaction)
    assert_retained_worktree_safe(task)
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
        raise SoloAIError("Retained abandonment worktree is missing or unregistered")
    expected_tip = str(transaction["branch_tip"])
    if (
        repo.branch(worktree) != transaction["branch"]
        or repo.head(worktree) != expected_tip
        or repo.ref_head(f"refs/heads/{transaction['branch']}") != expected_tip
    ):
        raise SoloAIError("Task worktree or branch changed during retained abandonment")
    current_status = repo.git(
        ["status", "--porcelain=v1", "--untracked-files=all"], cwd=worktree
    ).stdout
    if current_status != transaction.get("retained_status"):
        raise SoloAIError("Worktree content changed during retained abandonment")
    completed = store.complete_retained_abandonment(
        task["id"], transaction_id=str(transaction["transaction_id"])
    )
    receipt = write_completed_receipt(repo, completed)
    return {
        "task_id": task["id"],
        "status": "abandoned",
        "transaction_id": receipt["transaction_id"],
        "retained_worktree": True,
        "quarantine_reason": completed.get("quarantine_reason"),
    }


def _assert_reclaim_task(task: dict[str, Any]) -> dict[str, Any]:
    """仅让已完整保留的终态任务进入显式回收流程。"""
    transaction = task.get("abandonment")
    if (
        task.get("status") != "abandoned"
        or not isinstance(transaction, dict)
        or transaction.get("retained_worktree") is not True
        or transaction.get("phase") != "completed"
    ):
        raise SoloAIError(
            "Retained reclaim accepts only a completed retained-worktree abandonment"
        )
    if task.get("active_operation"):
        raise SoloAIError("Retained reclaim requires no active task operation")
    _assert_identity(task, transaction)
    assert_retained_worktree_safe(task)
    return transaction


def _reclaim_worktree(
    repo: GitRepo, *, task: dict[str, Any], transaction: dict[str, Any]
) -> Path:
    worktree = Path(str(transaction["worktree"]))
    resolved = require_managed_directory_identity(
        worktree,
        managed_root=Path(str(transaction["managed_root"])),
        expected_resolved=str(transaction["worktree_resolved"]),
        expected_root_resolved=str(transaction["managed_root_resolved"]),
        expected_identity=dict(transaction["worktree_identity"]),
        expected_root_identity=dict(transaction["managed_root_identity"]),
    )
    registered = any(item.path == resolved for item in repo.worktrees())
    if not worktree.is_dir() or not registered:
        raise SoloAIError("Retained reclaim worktree is missing or unregistered")
    return worktree


def retained_reclaim_plan(
    repo: GitRepo,
    *,
    store: StateStore,
    task: dict[str, Any],
    diagnostic: bool = False,
) -> dict[str, Any]:
    """生成无副作用的回收清单；确认值绑定本次可删对象快照。"""
    transaction = _assert_reclaim_task(task)
    worktree = _reclaim_worktree(repo, task=task, transaction=transaction)
    state = store.read()
    slot = state["slots"].get(str(task["slot_id"]))
    if (
        not slot
        or slot.get("task_id") != task["id"]
        or slot.get("status") != "quarantined"
    ):
        raise SoloAIError("Retained reclaim requires its exact quarantined slot")
    expected_tip = str(transaction["branch_tip"])
    if (
        repo.branch(worktree) != transaction["branch"]
        or repo.head(worktree) != expected_tip
        or repo.ref_head(f"refs/heads/{transaction['branch']}") != expected_tip
    ):
        raise SoloAIError("Retained reclaim branch or HEAD changed")
    tracked_status = repo.git(
        ["status", "--porcelain=v1", "--untracked-files=no"], cwd=worktree
    ).stdout
    if tracked_status:
        raise SoloAIError(
            "Retained reclaim refuses tracked changes; preserve the worktree for review"
        )
    diagnostics: list[dict[str, str]] | None = [] if diagnostic else None
    inventory = inspect_untracked(repo, cwd=worktree, diagnostics=diagnostics)
    blocked = [
        *inventory["keep"],
        *inventory["protected"],
        *inventory["unknown_ignored"],
    ]
    if not diagnostic and blocked:
        raise SoloAIError(
            "Retained reclaim found protected or unknown content; files were preserved:\n"
            + "\n".join(f"- {item}" for item in blocked[:20])
        )
    blockers: list[dict[str, str]] = [
        *({"path": relative, "kind": "keep"} for relative in inventory["keep"]),
        *(
            {"path": relative, "kind": "protected"}
            for relative in inventory["protected"]
        ),
        *(
            {"path": relative, "kind": "unknown-ignored"}
            for relative in inventory["unknown_ignored"]
        ),
    ]
    if not diagnostic:
        ordinary = {
            relative: snapshot_plain_path(worktree / relative)
            for relative in inventory["ordinary"]
        }
    else:
        ordinary: dict[str, dict[str, object]] = {}
        for relative in inventory["ordinary"]:
            try:
                ordinary[relative] = snapshot_plain_path(worktree / relative)
            except (OSError, SoloAIError) as exc:
                _record_reclaim_diagnostic(diagnostics, relative, exc)
        blockers.extend(
            {
                "path": item["path"],
                "kind": item["kind"],
                "reason": item["reason"],
            }
            for item in diagnostics
        )
    if diagnostic and blockers:
        return {
            "task_id": task["id"],
            "slot_id": task["slot_id"],
            "status": "blocked",
            "scan_complete": not diagnostics,
            "blockers": blockers,
            "retained": sorted(inventory["retained"]),
        }
    identity = {
        "schema_version": RETAINED_RECLAIM_SCHEMA,
        "task_id": task["id"],
        "slot_id": task["slot_id"],
        "slot_generation": int(slot["generation"]),
        "abandonment_transaction_id": transaction["transaction_id"],
        "worktree": str(worktree),
        "branch": transaction["branch"],
        "branch_tip": expected_tip,
        "base_head": transaction["base_head"],
        "ordinary_untracked": ordinary,
    }
    confirmation = sha256_text(stable_json(identity))
    return {
        **identity,
        "confirmation": confirmation,
        "delete": sorted(ordinary),
        "retained": sorted(inventory["retained"]),
        "status": "needs-confirmation",
        "scan_complete": True,
        "blockers": [],
    }


def _record_reclaim_diagnostic(
    diagnostics: list[dict[str, str]], relative: str, exc: Exception
) -> None:
    kind = "permission" if isinstance(exc, PermissionError) else "inspection"
    diagnostics.append({"path": relative, "kind": kind, "reason": str(exc)})


def _assert_reclaim_transaction(
    task: dict[str, Any], transaction: dict[str, Any]
) -> None:
    expected = {
        "schema_version": RETAINED_RECLAIM_SCHEMA,
        "task_id": task["id"],
        "slot_id": task["slot_id"],
        "abandonment_transaction_id": task["abandonment"]["transaction_id"],
    }
    for key, value in expected.items():
        if transaction.get(key) != value:
            raise SoloAIError(f"Retained reclaim identity changed: {key}")
    if transaction.get("phase") not in {
        "prepared",
        "deleting",
        "detaching",
        "detached",
        "completed",
    }:
        raise SoloAIError("Unsupported retained reclaim transaction phase")


def _assert_reclaim_slot(
    store: StateStore, *, task: dict[str, Any], transaction: dict[str, Any]
) -> None:
    slot = store.read()["slots"].get(str(task["slot_id"]))
    if (
        not slot
        or slot.get("task_id") != task["id"]
        or slot.get("status") != "release-checking"
        or int(slot.get("generation", -1)) != int(transaction["slot_generation"])
    ):
        raise SoloAIError("Retained reclaim slot identity changed")


def _assert_reclaim_clean_before_detach(
    repo: GitRepo,
    *,
    store: StateStore,
    task: dict[str, Any],
    transaction: dict[str, Any],
) -> Path:
    """detach 前重新冻结现场，不能把中断后新到的内容交给 Git 覆盖。"""
    _assert_reclaim_slot(store, task=task, transaction=transaction)
    abandonment = dict(task["abandonment"])
    worktree = _reclaim_worktree(repo, task=task, transaction=abandonment)
    tracked_status = repo.git(
        ["status", "--porcelain=v1", "--untracked-files=no"], cwd=worktree
    ).stdout
    if tracked_status:
        raise SoloAIError("Retained reclaim received tracked changes before detach")
    inventory = inspect_untracked(repo, cwd=worktree)
    blocked = [
        *inventory["keep"],
        *inventory["protected"],
        *inventory["ordinary"],
        *inventory["unknown_ignored"],
    ]
    if blocked:
        raise SoloAIError(
            "Retained reclaim received protected, unknown, or ordinary content before detach:\n"
            + "\n".join(f"- {item}" for item in blocked[:20])
        )
    if not repo.is_clean(worktree):
        raise SoloAIError("Retained reclaim worktree is not clean before detach")
    return worktree


def resume_retained_reclaim(
    repo: GitRepo, *, store: StateStore, task: dict[str, Any]
) -> dict[str, Any]:
    """从持久阶段继续回收；只删除已确认且未改变的普通未跟踪内容。"""
    _assert_reclaim_task(task)
    transaction = task.get("retained_reclaim")
    if not isinstance(transaction, dict):
        raise SoloAIError("Retained reclaim transaction is missing")
    _assert_reclaim_transaction(task, transaction)
    if transaction.get("phase") == "completed":
        slot = store.read()["slots"].get(str(task["slot_id"]))
        if (
            not slot
            or slot.get("task_id") is not None
            or slot.get("status") != "idle"
            or int(slot.get("generation", -1)) != int(transaction["slot_generation"])
        ):
            raise SoloAIError("Completed retained reclaim slot changed")
        return {
            "task_id": task["id"],
            "slot_id": task["slot_id"],
            "status": "reclaimed",
            "idempotent": True,
            "deleted": sorted(transaction["ordinary_untracked"]),
        }
    _assert_reclaim_slot(store, task=task, transaction=transaction)
    abandonment = dict(task["abandonment"])
    worktree = _reclaim_worktree(repo, task=task, transaction=abandonment)
    expected_tip = str(transaction["branch_tip"])
    base_head = str(transaction["base_head"])
    phase = str(transaction["phase"])

    if phase in {"prepared", "deleting"}:
        if (
            repo.branch(worktree) != transaction["branch"]
            or repo.head(worktree) != expected_tip
            or repo.ref_head(f"refs/heads/{transaction['branch']}") != expected_tip
        ):
            raise SoloAIError("Retained reclaim branch or HEAD changed")
        tracked_status = repo.git(
            ["status", "--porcelain=v1", "--untracked-files=no"], cwd=worktree
        ).stdout
        if tracked_status:
            raise SoloAIError("Retained reclaim received tracked changes")
        inventory = inspect_untracked(repo, cwd=worktree)
        blocked = [
            *inventory["keep"],
            *inventory["protected"],
            *inventory["unknown_ignored"],
        ]
        if blocked:
            raise SoloAIError("Retained reclaim received protected or unknown content")
        observed = {
            relative: snapshot_plain_path(worktree / relative)
            for relative in inventory["ordinary"]
        }
        expected = dict(transaction["ordinary_untracked"])
        for relative, snapshot in observed.items():
            if expected.get(relative) != snapshot:
                raise SoloAIError("Retained reclaim ordinary content changed")
        if set(observed) - set(expected):
            raise SoloAIError("Retained reclaim ordinary content list changed")
        if phase == "prepared":
            transaction = store.advance_retained_reclaim(
                task["id"],
                transaction_id=str(transaction["transaction_id"]),
                phase="deleting",
            )
        if observed:
            remove_abandoned_untracked(
                repo,
                cwd=worktree,
                expected_ordinary=observed,
            )
        remaining = inspect_untracked(repo, cwd=worktree)
        blocked_keys = ("keep", "protected", "ordinary", "unknown_ignored")
        if any(remaining[key] for key in blocked_keys):
            raise SoloAIError("Retained reclaim content changed during deletion")
        transaction = store.advance_retained_reclaim(
            task["id"],
            transaction_id=str(transaction["transaction_id"]),
            phase="detaching",
        )
        phase = "detaching"

    if phase == "detaching":
        worktree = _assert_reclaim_clean_before_detach(
            repo, store=store, task=task, transaction=transaction
        )
        branch = repo.branch(worktree)
        if branch == transaction["branch"]:
            if (
                repo.head(worktree) != expected_tip
                or repo.ref_head(f"refs/heads/{transaction['branch']}") != expected_tip
            ):
                raise SoloAIError("Retained reclaim branch changed before detach")
            repo.git(["switch", "--detach", base_head], cwd=worktree)
        elif branch is None:
            if repo.head(worktree) != base_head:
                raise SoloAIError("Retained reclaim detached HEAD changed")
        else:
            raise SoloAIError("Retained reclaim worktree switched to another branch")
        transaction = store.advance_retained_reclaim(
            task["id"],
            transaction_id=str(transaction["transaction_id"]),
            phase="detached",
        )

    if repo.branch(worktree) is not None or repo.head(worktree) != base_head:
        raise SoloAIError("Retained reclaim worktree changed before release")
    if not repo.is_clean(worktree):
        raise SoloAIError("Retained reclaim worktree is not clean before release")
    inventory = inspect_untracked(repo, cwd=worktree)
    blocked_keys = ("keep", "protected", "ordinary", "unknown_ignored")
    if any(inventory[key] for key in blocked_keys):
        raise SoloAIError("Retained reclaim content changed before release")
    completed = store.complete_retained_reclaim(
        task["id"], transaction_id=str(transaction["transaction_id"])
    )
    return {
        "task_id": task["id"],
        "slot_id": task["slot_id"],
        "status": "reclaimed",
        "idempotent": False,
        "deleted": sorted(transaction["ordinary_untracked"]),
        "branch_preserved": completed["branch"],
    }


def retained_disposal_plan(
    repo: GitRepo, *, store: StateStore, task: dict[str, Any]
) -> dict[str, Any]:
    """冻结一次显式处置保留树的身份，不枚举获准根中的未知内容。"""
    abandonment = _assert_reclaim_task(task)
    if task.get("retained_reclaim") or task.get("retained_disposal"):
        raise SoloAIError("Retained worktree already has a reclaim or disposal record")
    worktree = _reclaim_worktree(repo, task=task, transaction=abandonment)
    slot = store.read()["slots"].get(str(task["slot_id"]))
    if (
        not slot
        or slot.get("task_id") != task["id"]
        or slot.get("status") != "quarantined"
    ):
        raise SoloAIError("Retained disposal requires its exact quarantined slot")
    expected_tip = str(abandonment["branch_tip"])
    if (
        repo.branch(worktree) != abandonment["branch"]
        or repo.head(worktree) != expected_tip
        or repo.ref_head(f"refs/heads/{abandonment['branch']}") != expected_tip
    ):
        raise SoloAIError("Retained disposal branch or HEAD changed")
    tracked_status = repo.git(
        ["status", "--porcelain=v1", "--untracked-files=no"], cwd=worktree
    ).stdout
    if tracked_status:
        raise SoloAIError(
            "Retained disposal refuses tracked changes; preserve the worktree for review"
        )
    primary, default_branch = repo.ensure_default_primary_clean()
    rebuild_head = repo.default_head()
    if primary == worktree.resolve():
        raise SoloAIError("Retained disposal cannot replace the primary worktree")
    identity = {
        "schema_version": RETAINED_DISPOSAL_SCHEMA,
        "mode": "dispose-retained-worktree",
        "task_id": task["id"],
        "slot_id": task["slot_id"],
        "slot_generation": int(slot["generation"]),
        "abandonment_transaction_id": abandonment["transaction_id"],
        "worktree": str(worktree),
        "worktree_resolved": abandonment["worktree_resolved"],
        "worktree_identity": abandonment["worktree_identity"],
        "managed_root": abandonment["managed_root"],
        "managed_root_resolved": abandonment["managed_root_resolved"],
        "managed_root_identity": abandonment["managed_root_identity"],
        "branch": abandonment["branch"],
        "branch_tip": expected_tip,
        "base_head": abandonment["base_head"],
        "rebuild_branch": default_branch,
        "rebuild_head": rebuild_head,
    }
    return {
        **identity,
        "confirmation": sha256_text(stable_json(identity)),
        "status": "needs-confirmation",
        "discard_scope": "entire-retained-worktree-root",
        "scan_complete": False,
    }


def _assert_disposal_transaction(
    task: dict[str, Any], transaction: dict[str, Any]
) -> None:
    abandonment = _assert_reclaim_task(task)
    expected = {
        "schema_version": RETAINED_DISPOSAL_SCHEMA,
        "mode": "dispose-retained-worktree",
        "task_id": task["id"],
        "slot_id": task["slot_id"],
        "abandonment_transaction_id": abandonment["transaction_id"],
    }
    for key, value in expected.items():
        if transaction.get(key) != value:
            raise SoloAIError(f"Retained disposal identity changed: {key}")
    if transaction.get("phase") not in {
        "prepared",
        "renaming",
        "staged",
        "removed",
        "recreated",
        "completed",
    }:
        raise SoloAIError("Unsupported retained disposal transaction phase")


def _assert_disposal_slot(
    store: StateStore, *, task: dict[str, Any], transaction: dict[str, Any]
) -> None:
    slot = store.read()["slots"].get(str(task["slot_id"]))
    if (
        not slot
        or slot.get("task_id") != task["id"]
        or slot.get("status") != "release-checking"
        or int(slot.get("generation", -1)) != int(transaction["slot_generation"])
    ):
        raise SoloAIError("Retained disposal slot identity changed")


def _assert_disposal_primary(repo: GitRepo, transaction: dict[str, Any]) -> Path:
    primary, branch = repo.ensure_default_primary_clean()
    if (
        branch != transaction["rebuild_branch"]
        or repo.default_head() != transaction["rebuild_head"]
    ):
        raise SoloAIError("Retained disposal rebuild base changed")
    return primary


def _disposal_stage_path(transaction: dict[str, Any]) -> Path:
    worktree = Path(str(transaction["worktree"]))
    return worktree.parent / (
        f".{worktree.name}.retained-disposal-{transaction['transaction_id']}"
    )


def _assert_disposal_parent(transaction: dict[str, Any]) -> Path:
    parent = Path(str(transaction["managed_root"]))
    require_managed_directory_identity(
        parent,
        managed_root=parent,
        expected_resolved=str(transaction["managed_root_resolved"]),
        expected_root_resolved=str(transaction["managed_root_resolved"]),
        expected_identity=dict(transaction["managed_root_identity"]),
        expected_root_identity=dict(transaction["managed_root_identity"]),
    )
    return parent


def _assert_disposal_original(
    repo: GitRepo, *, task: dict[str, Any], transaction: dict[str, Any]
) -> Path:
    worktree = _reclaim_worktree(repo, task=task, transaction=transaction)
    if (
        repo.branch(worktree) != transaction["branch"]
        or repo.head(worktree) != transaction["branch_tip"]
        or repo.ref_head(f"refs/heads/{transaction['branch']}")
        != transaction["branch_tip"]
    ):
        raise SoloAIError("Retained disposal branch or HEAD changed")
    if repo.git(
        ["status", "--porcelain=v1", "--untracked-files=no"], cwd=worktree
    ).stdout:
        raise SoloAIError("Retained disposal received tracked changes")
    return worktree


def _assert_disposal_staging(
    transaction: dict[str, Any], *, require_exists: bool = True
) -> Path:
    stage = _disposal_stage_path(transaction)
    if is_link_or_junction(stage):
        raise SoloAIError("Retained disposal staging path became a link or junction")
    if require_exists and not stage.is_dir():
        raise SoloAIError("Retained disposal staging directory is missing")
    if stage.exists() and path_identity(stage) != transaction["worktree_identity"]:
        raise SoloAIError("Retained disposal staging directory was replaced")
    return stage


def _assert_disposal_registration(
    repo: GitRepo, transaction: dict[str, Any], *, registered: bool
) -> None:
    worktree = Path(str(transaction["worktree"]))
    records = [item for item in repo.worktrees() if item.path == worktree.resolve()]
    if registered:
        if (
            len(records) != 1
            or records[0].head != transaction["branch_tip"]
            or records[0].branch != f"refs/heads/{transaction['branch']}"
            or records[0].detached
        ):
            raise SoloAIError("Retained disposal Git registration changed")
    elif records:
        raise SoloAIError("Retained disposal Git registration unexpectedly remains")


def _write_disposal_receipt(repo: GitRepo, transaction: dict[str, Any]) -> str:
    path = _disposal_receipt_path(repo, str(transaction["task_id"]))
    payload = {
        "schema_version": RETAINED_DISPOSAL_SCHEMA,
        "mode": transaction["mode"],
        "task_id": transaction["task_id"],
        "slot_id": transaction["slot_id"],
        "slot_generation": transaction["slot_generation"],
        "abandonment_transaction_id": transaction["abandonment_transaction_id"],
        "disposal_transaction_id": transaction["transaction_id"],
        "worktree": transaction["worktree"],
        "branch": transaction["branch"],
        "branch_tip": transaction["branch_tip"],
        "rebuild_branch": transaction["rebuild_branch"],
        "rebuild_head": transaction["rebuild_head"],
        "recreated_worktree_identity": transaction["recreated_worktree_identity"],
        "recreated_managed_root_identity": transaction[
            "recreated_managed_root_identity"
        ],
        "recreated_at": transaction["recreated_at"],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and read_json(path, None) != payload:
        raise SoloAIError("Existing retained disposal receipt has different facts")
    if not path.exists():
        atomic_write_json(path, payload)
    return sha256_file(path)


def resume_retained_disposal(
    repo: GitRepo, *, store: StateStore, task: dict[str, Any]
) -> dict[str, Any]:
    """继续一个已经明确授权的整树处置，再以当前默认分支重建原槽位。"""
    transaction = task.get("retained_disposal")
    if not isinstance(transaction, dict):
        raise SoloAIError("Retained disposal transaction is missing")
    _assert_disposal_transaction(task, transaction)
    if transaction["phase"] == "completed":
        slot = store.read()["slots"].get(str(task["slot_id"]))
        if (
            not slot
            or slot.get("task_id") is not None
            or slot.get("status") != "idle"
            or int(slot.get("generation", -1)) != int(transaction["slot_generation"])
        ):
            raise SoloAIError("Completed retained disposal slot changed")
        return {
            "task_id": task["id"],
            "slot_id": task["slot_id"],
            "status": "disposed-and-recreated",
            "idempotent": True,
            "branch_preserved": transaction["branch"],
            "rebuild_head": transaction["rebuild_head"],
        }

    _assert_disposal_slot(store, task=task, transaction=transaction)
    primary = _assert_disposal_primary(repo, transaction)
    parent = _assert_disposal_parent(transaction)
    worktree = Path(str(transaction["worktree"]))
    stage = _disposal_stage_path(transaction)
    phase = str(transaction["phase"])

    if phase == "prepared":
        transaction = store.advance_retained_disposal(
            task["id"],
            transaction_id=str(transaction["transaction_id"]),
            phase="renaming",
        )
        phase = "renaming"

    if phase == "renaming":
        if stage.exists() or is_link_or_junction(stage):
            if worktree.exists() or is_link_or_junction(worktree):
                raise SoloAIError(
                    "Retained disposal has both original and staging paths"
                )
            stage = _assert_disposal_staging(transaction)
            _assert_disposal_registration(repo, transaction, registered=True)
        else:
            original = _assert_disposal_original(
                repo, task=task, transaction=transaction
            )
            with pinned_plain_directory(parent, transaction["managed_root_identity"]):
                if stage.exists() or is_link_or_junction(stage):
                    raise SoloAIError(
                        "Retained disposal staging path appeared before rename"
                    )
                original.rename(stage)
            stage = _assert_disposal_staging(transaction)
        transaction = store.advance_retained_disposal(
            task["id"],
            transaction_id=str(transaction["transaction_id"]),
            phase="staged",
            staging_identity=path_identity(stage),
        )
        phase = "staged"

    if phase == "staged":
        stage = _assert_disposal_staging(transaction)
        expected_stage = transaction.get("staging_identity")
        if expected_stage != path_identity(stage):
            raise SoloAIError("Retained disposal staging directory identity changed")
        remove_tree_without_following_links(stage)
        if stage.exists() or is_link_or_junction(stage):
            raise SoloAIError("Retained disposal staging directory remains")
        _assert_disposal_registration(repo, transaction, registered=True)
        repo.git(["worktree", "remove", str(worktree)], cwd=primary)
        if worktree.exists() or is_link_or_junction(worktree):
            raise SoloAIError("Retained disposal original path reappeared")
        _assert_disposal_registration(repo, transaction, registered=False)
        transaction = store.advance_retained_disposal(
            task["id"],
            transaction_id=str(transaction["transaction_id"]),
            phase="removed",
        )
        phase = "removed"

    if phase == "removed":
        if (
            worktree.exists()
            or is_link_or_junction(worktree)
            or stage.exists()
            or is_link_or_junction(stage)
        ):
            raise SoloAIError("Retained disposal path changed before recreation")
        _assert_disposal_registration(repo, transaction, registered=False)
        repo.git(
            ["worktree", "add", "--detach", str(worktree), transaction["rebuild_head"]],
            cwd=primary,
        )
        rebuilt = require_managed_directory_identity(
            worktree,
            managed_root=parent,
            expected_root_resolved=str(transaction["managed_root_resolved"]),
            expected_root_identity=dict(transaction["managed_root_identity"]),
        )
        records = [item for item in repo.worktrees() if item.path == rebuilt]
        if (
            len(records) != 1
            or not records[0].detached
            or records[0].head != transaction["rebuild_head"]
            or repo.branch(worktree) is not None
            or repo.head(worktree) != transaction["rebuild_head"]
            or not repo.is_clean(worktree)
        ):
            raise SoloAIError(
                "Retained disposal recreation did not produce a clean detached slot"
            )
        transaction = store.advance_retained_disposal(
            task["id"],
            transaction_id=str(transaction["transaction_id"]),
            phase="recreated",
            recreated_worktree_identity=path_identity(worktree),
            recreated_worktree_resolved=str(rebuilt),
            recreated_managed_root_identity=path_identity(parent),
            recreated_managed_root_resolved=str(parent.resolve()),
        )

    rebuilt = require_managed_directory_identity(
        worktree,
        managed_root=parent,
        expected_resolved=str(transaction["recreated_worktree_resolved"]),
        expected_root_resolved=str(transaction["recreated_managed_root_resolved"]),
        expected_identity=dict(transaction["recreated_worktree_identity"]),
        expected_root_identity=dict(transaction["recreated_managed_root_identity"]),
    )
    records = [item for item in repo.worktrees() if item.path == rebuilt]
    if (
        len(records) != 1
        or not records[0].detached
        or records[0].head != transaction["rebuild_head"]
        or repo.branch(worktree) is not None
        or repo.head(worktree) != transaction["rebuild_head"]
        or not repo.is_clean(worktree)
    ):
        raise SoloAIError("Retained disposal recreated slot changed before release")
    receipt_sha = _write_disposal_receipt(repo, transaction)
    completed = store.complete_retained_disposal(
        task["id"],
        transaction_id=str(transaction["transaction_id"]),
        receipt_sha256=receipt_sha,
    )
    return {
        "task_id": task["id"],
        "slot_id": task["slot_id"],
        "status": "disposed-and-recreated",
        "idempotent": False,
        "branch_preserved": completed["branch"],
        "rebuild_head": transaction["rebuild_head"],
        "receipt_sha256": receipt_sha,
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
