"""只读、按对象收敛的 DWW 状态投影。

生命周期命令仍以各自锁中的持久化事实为准。这里刻意不调用回执 reconcile、
不写缓存，也不把“上次看到已交付”当作下一次查询的事实。
"""

from __future__ import annotations

import copy
from typing import Any

from .candidate_batches import (
    ACTIVE_BATCH_STATES,
    TERMINAL_CANDIDATE_STATES,
    CandidateBatchStore,
)
from .lifecycle import repository_route
from .proof import read_validation_attempt
from .repo import GitRepo
from .root_context import show_root_anchor
from .state import FINAL_TASK_STATES, StateStore
from .util import ActionableSoloAIError, process_matches
from .validation_queue import queue_ticket_snapshot


VIEW_SCHEMA = 1


def status_view(
    repo: GitRepo,
    *,
    task_id: str | None = None,
    root_id: str | None = None,
    batch_id: str | None = None,
    include_history: bool = False,
) -> dict[str, Any]:
    """返回当前、单任务、单根或单批次的无写入视图。"""

    selected = [item for item in (task_id, root_id, batch_id) if item]
    if len(selected) > 1:
        raise ActionableSoloAIError(
            "Status accepts only one of --task, --root, or --batch",
            code="INVALID_STATUS_QUERY",
            next_action={"kind": "choose_one_status_selector"},
        )
    if include_history and selected:
        raise ActionableSoloAIError(
            "--history cannot be combined with an exact status selector",
            code="INVALID_STATUS_QUERY",
            next_action={"kind": "remove_history_or_selector"},
        )

    state_store = StateStore(repo)
    state = state_store.read()
    batch_store = CandidateBatchStore(repo)
    pool = batch_store.read()
    tasks = state.get("tasks", {})
    candidates = pool.get("candidates", {})
    batches = pool.get("batches", {})
    candidate_by_task = {
        str(candidate["task_id"]): candidate
        for candidate in candidates.values()
        if candidate.get("task_id")
    }
    common = {
        "view_schema": VIEW_SCHEMA,
        "repository": str(repo.root),
        "mode": _mode(repo),
    }

    if task_id:
        task = tasks.get(task_id)
        if not task:
            raise _not_found("task", task_id)
        candidate = candidate_by_task.get(task_id)
        delivery = _project_deliveries(
            batch_store, [candidate], batches, all_candidates=candidates.values()
        ).get(task_id)
        return {
            **common,
            "scope": "task",
            "task": _task_projection(
                repo, task, delivery, state["slots"].get(str(task.get("slot_id")))
            ),
        }
    if batch_id:
        batch = batches.get(batch_id)
        if not batch:
            raise _not_found("batch", batch_id)
        members = [
            candidates[candidate_id]
            for candidate_id in batch.get("candidate_ids", [])
            if candidate_id in candidates
        ]
        deliveries = _project_deliveries(
            batch_store, members, batches, all_candidates=candidates.values()
        )
        return {
            **common,
            "scope": "batch",
            "batch": _batch_projection(repo, batch),
            "candidates": [
                _candidate_projection(delivery) for delivery in deliveries.values()
            ],
        }
    if root_id:
        try:
            root = show_root_anchor(repo, root_id=root_id)
        except Exception as exc:
            if "does not exist" in str(exc) or "Unknown" in str(exc):
                raise _not_found("root", root_id) from exc
            raise
        local_tasks = [
            task for task in tasks.values() if task.get("root_anchor_id") == root_id
        ]
        deliveries = _project_deliveries(
            batch_store,
            [candidate_by_task.get(str(task["id"])) for task in local_tasks],
            batches,
            all_candidates=candidates.values(),
        )
        local_children = [
            _task_projection(
                repo,
                task,
                deliveries.get(str(task["id"])),
                state["slots"].get(str(task.get("slot_id"))),
            )
            for task in local_tasks
        ]
        external_children = copy.deepcopy(root.get("linked_child_tasks") or [])
        return {
            **common,
            "scope": "root",
            "root": {
                "id": root_id,
                "plan_version": root.get("plan_version"),
                "overall_acceptance_status": root.get("overall_acceptance_status"),
                "overall_acceptance_plan_version": root.get(
                    "overall_acceptance_plan_version"
                ),
                "sha256": root.get("sha256"),
                "size_bytes": root.get("size_bytes"),
                "next_action": _root_next_action(
                    root, local_children, external_children
                ),
            },
            "local_children": local_children,
            "external_children": external_children,
        }

    all_tasks = list(tasks.values())
    visible_tasks = [
        task
        for task in all_tasks
        if include_history
        or _task_is_current(task, candidate_by_task.get(str(task["id"])))
    ]
    visible_candidates = [
        candidate
        for candidate in candidates.values()
        if include_history or candidate.get("status") not in TERMINAL_CANDIDATE_STATES
    ]
    visible_batches = [
        batch
        for batch in batches.values()
        if include_history or batch.get("status") in ACTIVE_BATCH_STATES
    ]
    deliveries = _project_deliveries(
        batch_store,
        visible_candidates,
        batches,
        all_candidates=candidates.values(),
    )
    return {
        **common,
        "scope": "history" if include_history else "current",
        "tasks": [
            _task_projection(
                repo,
                task,
                deliveries.get(str(task["id"])),
                state["slots"].get(str(task.get("slot_id"))),
            )
            for task in visible_tasks
        ],
        "candidates": [
            _candidate_projection(delivery) for delivery in deliveries.values()
        ],
        "batches": [_batch_projection(repo, batch) for batch in visible_batches],
        "history_counts": {
            "tasks": len(all_tasks) - len(visible_tasks),
            "candidates": len(candidates) - len(visible_candidates),
            "batches": len(batches) - len(visible_batches),
        },
        "guard_alerts": _guard_alert_summary(state_store.guard_alerts()),
    }


def _mode(repo: GitRepo) -> str:
    route = repository_route(repo)
    return "uninitialized" if route["action"] == "ask" else str(route["action"])


def _not_found(kind: str, identity: str) -> ActionableSoloAIError:
    return ActionableSoloAIError(
        f"Unknown {kind}: {identity}",
        code="OBJECT_NOT_FOUND",
        context={"object_type": kind, "object_id": identity},
        next_action={"kind": "status_current"},
    )


def _task_is_current(task: dict[str, Any], candidate: dict[str, Any] | None) -> bool:
    if task.get("status") in FINAL_TASK_STATES:
        return False
    return not (candidate and candidate.get("status") in TERMINAL_CANDIDATE_STATES)


def _project_deliveries(
    store: CandidateBatchStore,
    candidates: list[dict[str, Any] | None],
    batches: dict[str, Any],
    *,
    all_candidates: Any,
) -> dict[str, dict[str, Any]]:
    """单次投影为同一请求内的任务与候选列表共享 Git 事实。"""

    selected = [item for item in candidates if item is not None]
    return {
        str(candidate["task_id"]): candidate
        for candidate in store.project_candidates(
            selected,
            batches,
            all_candidates=all_candidates,
        )
    }


def _task_projection(
    repo: GitRepo,
    task: dict[str, Any],
    candidate_delivery: dict[str, Any] | None,
    slot: dict[str, Any] | None,
) -> dict[str, Any]:
    projected = {
        "id": task.get("id"),
        "name": task.get("name"),
        "status": task.get("status"),
        "base_ref": task.get("base_ref"),
        "ordinary_branch": task.get("branch"),
        "candidate_head": task.get("candidate_head"),
        "root_anchor_id": task.get("root_anchor_id"),
        "next_action": {"kind": "continue_task"},
    }
    if candidate_delivery:
        projected["candidate_delivery"] = _candidate_projection(candidate_delivery)
        projected["next_action"] = _candidate_next_action(candidate_delivery)
        projected["slot"] = {
            "path": task.get("worktree"),
            "reusable": bool(
                slot
                and slot.get("status") == "idle"
                and slot.get("released_candidate_task_id") == task.get("id")
            ),
            "reused": bool(
                slot
                and task.get("slot_generation") is not None
                and int(slot.get("generation", 0)) > int(task["slot_generation"])
            ),
        }
    active_operation = task.get("active_operation")
    if isinstance(active_operation, dict) and active_operation.get("kind"):
        operation = str(active_operation["kind"])
        projected["active_operation"] = {
            "kind": operation,
            "started_at": active_operation.get("started_at"),
        }
        projected["next_action"] = {
            "kind": "wait_for_operation",
            "task_id": task.get("id"),
            "operation": operation,
        }
    if validation := _validation_projection(
        repo,
        task.get("validation_attempt"),
        task.get("validation_attempts"),
    ):
        projected["validation"] = validation
    abandonment = task.get("abandonment")
    if isinstance(abandonment, dict) and abandonment.get("retained_worktree") is True:
        projected["retained_worktree"] = {
            "path": task.get("worktree"),
            "slot_id": task.get("slot_id"),
            "reason": task.get("quarantine_reason"),
        }
        if task.get("status") in FINAL_TASK_STATES:
            projected["next_action"] = {"kind": "retained_worktree_terminal"}
    return projected


def _candidate_projection(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": candidate.get("candidate_id"),
        "task_id": candidate.get("task_id"),
        "status": candidate.get("status"),
        "ordinary_branch": candidate.get("ordinary_branch"),
        "ordinary_branch_status": candidate.get("ordinary_branch_status"),
        "candidate_head": candidate.get("head"),
        "delivery_status": candidate.get("delivery_status"),
        "delivered": candidate.get("delivered"),
        "finalization_pending": candidate.get("finalization_pending"),
        "batch_ownership": copy.deepcopy(candidate.get("batch_ownership")),
        "waiting": copy.deepcopy(candidate.get("waiting")),
    }


def _candidate_next_action(candidate: dict[str, Any]) -> dict[str, Any]:
    ownership = candidate.get("batch_ownership") or {}
    state = ownership.get("state")
    if candidate.get("delivery_status") == "integrated":
        if candidate.get("finalization_pending"):
            batch = ownership.get("batch") or {}
            return {
                "kind": "wait_or_recover_batch",
                "batch_id": batch.get("id"),
            }
        return {"kind": "candidate_terminal"}
    if state in {"active_full_batch", "active_tail_batch"}:
        batch = ownership.get("batch") or {}
        return {"kind": "wait_or_recover_batch", "batch_id": batch.get("id")}
    if candidate.get("status") == "held":
        return {"kind": "recover_candidate_release"}
    if candidate.get("status") in TERMINAL_CANDIDATE_STATES:
        return {"kind": "candidate_terminal"}
    return {"kind": "await_integration"}


def _batch_projection(repo: GitRepo, batch: dict[str, Any]) -> dict[str, Any]:
    release = batch.get("runtime_release") or {}
    projected = {
        "id": batch.get("id"),
        "status": batch.get("status"),
        "trigger": batch.get("trigger"),
        "base_ref": batch.get("base_ref"),
        "base_before": batch.get("base_before"),
        "candidate_ids": list(batch.get("candidate_ids") or []),
        "validation_outcome": batch.get("validation_outcome"),
        "runtime_release_result": release.get("result"),
        "integration_head": batch.get("integration_head"),
        "next_action": _batch_next_action(batch),
    }
    if validation := _validation_projection(
        repo,
        batch.get("validation_attempt"),
        batch.get("validation_attempts"),
    ):
        projected["validation"] = validation
    return projected


def _validation_projection(
    repo: GitRepo,
    current_attempt: object,
    attempt_history: object,
) -> dict[str, Any] | None:
    """只读取调用者显式关联的尝试回执，不触发队列清理或历史扫描。"""

    attempt_id = current_attempt if isinstance(current_attempt, str) else None
    if not attempt_id and isinstance(attempt_history, list):
        attempt_id = next(
            (item for item in reversed(attempt_history) if isinstance(item, str)), None
        )
    if not attempt_id:
        return None
    attempt = read_validation_attempt(repo, attempt_id)
    if not attempt:
        return {"id": attempt_id, "state": "not_recorded"}
    profiles = [
        _validation_profile_projection(profile)
        for profile in attempt.get("profiles", [])
    ]
    state = attempt.get("state")
    if state in {"waiting", "running"}:
        active_states = {profile["state"] for profile in profiles}
        if "running" in active_states:
            state = "running"
        elif "waiting" in active_states:
            state = "waiting"
        elif "preparing" in active_states:
            state = "preparing"
        else:
            state = "unknown"
    return {
        "id": attempt_id,
        "level": attempt.get("level"),
        "full_scope": attempt.get("full_scope"),
        "state": state,
        "result": attempt.get("result"),
        "started_at": attempt.get("started_at"),
        "finished_at": attempt.get("finished_at"),
        "error": attempt.get("error"),
        "profiles": profiles,
    }


def _validation_profile_projection(profile: dict[str, Any]) -> dict[str, Any]:
    state = profile.get("state")
    error_reason = profile.get("error_reason")
    queue = copy.deepcopy(profile.get("queue"))
    current_command = profile.get("current_command")
    projected_command: dict[str, Any] | None = None
    if state == "waiting":
        ticket = queue.get("ticket") if isinstance(queue, dict) else None
        observation = queue_ticket_snapshot(ticket) if isinstance(ticket, str) else None
        if observation and observation.get("state") == "waiting":
            queue = observation
        elif observation and observation.get("state") == "active":
            state = "preparing"
            queue = observation
        else:
            state = "unknown"
            error_reason = "queue_ticket_not_confirmed"
    elif state == "running":
        process = (
            current_command.get("process")
            if isinstance(current_command, dict)
            else None
        )
        if isinstance(process, dict) and process_matches(process):
            projected_command = {
                key: current_command.get(key)
                for key in ("index", "count", "elapsed_seconds")
                if current_command.get(key) is not None
            }
        else:
            state = "unknown"
            error_reason = "running_process_not_confirmed"
    return {
        "id": profile.get("id"),
        "state": state,
        "reused": profile.get("reused"),
        "execution_reason": profile.get("execution_reason"),
        "execution_details": copy.deepcopy(profile.get("decision")),
        "error_reason": error_reason,
        "queue": queue,
        "current_command": projected_command,
        "executed_commands": len(profile.get("runs") or []),
    }


def _batch_next_action(batch: dict[str, Any]) -> dict[str, Any]:
    batch_id = batch.get("id")
    status = batch.get("status")
    if status in {"runtime_activation_pending", "runtime_release_pending"}:
        return {"kind": "recover_batch", "batch_id": batch_id}
    if status in ACTIVE_BATCH_STATES:
        return {"kind": "wait_or_recover_batch", "batch_id": batch_id}
    if status != "failed":
        return {"kind": "batch_terminal"}
    failure_kind = batch.get("failure_kind")
    if failure_kind == "composition_conflict" and batch.get("failed_candidate_id"):
        return {
            "kind": "prepare_candidate_repair",
            "batch_id": batch_id,
            "candidate_id": batch.get("failed_candidate_id"),
        }
    if failure_kind == "validation_failed":
        return {"kind": "inspect_validation_evidence", "batch_id": batch_id}
    if failure_kind == "promotion_blocked":
        return {"kind": "inspect_promotion_block", "batch_id": batch_id}
    return {"kind": "inspect_batch_failure", "batch_id": batch_id}


def _root_next_action(
    root: dict[str, Any],
    local_children: list[dict[str, Any]],
    external_children: list[dict[str, Any]],
) -> dict[str, Any]:
    if any(child.get("status") not in FINAL_TASK_STATES for child in local_children):
        return {"kind": "complete_root_children"}
    if external_children:
        # 本仓库不能读取外部子任务的完整状态，紧凑视图不应猜测根已可关闭。
        return {"kind": "check_external_children"}
    if any(_candidate_lineage_needs_check(child) for child in local_children):
        return {"kind": "check_candidate_lineage"}
    if root.get("overall_acceptance_status") not in {"accepted", "cancelled"}:
        return {"kind": "record_root_acceptance"}
    return {"kind": "close_root"}


def _candidate_lineage_needs_check(child: dict[str, Any]) -> bool:
    """根关闭对 superseded 链需要完整状态；紧凑视图不猜测其后继。"""

    if child.get("status") != "candidate-published":
        return False
    delivery = child.get("candidate_delivery") or {}
    return delivery.get("status") not in {"integrated", "withdrawn"}


def _guard_alert_summary(alerts: list[dict[str, Any]]) -> dict[str, Any]:
    """紧凑状态只保留告警存在与范围；精确路径仍在旧完整 status/doctor。"""

    latest = []
    for alert in alerts[-3:]:
        latest.append(
            {
                "kind": alert.get("kind"),
                "worktree": alert.get("worktree"),
                "observed_at": alert.get("observed_at"),
                "path_count": len(alert.get("paths") or []),
            }
        )
    return {"count": len(alerts), "latest": latest}
