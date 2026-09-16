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
from .repo import GitRepo
from .root_context import show_root_anchor
from .state import FINAL_TASK_STATES, StateStore
from .util import ActionableSoloAIError


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
        delivery = _project_deliveries(batch_store, [candidate], batches).get(task_id)
        return {
            **common,
            "scope": "task",
            "task": _task_projection(task, delivery),
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
        deliveries = _project_deliveries(batch_store, members, batches)
        return {
            **common,
            "scope": "batch",
            "batch": _batch_projection(batch),
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
        )
        local_children = [
            _task_projection(task, deliveries.get(str(task["id"])))
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
    deliveries = _project_deliveries(batch_store, visible_candidates, batches)
    return {
        **common,
        "scope": "history" if include_history else "current",
        "tasks": [
            _task_projection(task, deliveries.get(str(task["id"])))
            for task in visible_tasks
        ],
        "candidates": [
            _candidate_projection(delivery) for delivery in deliveries.values()
        ],
        "batches": [_batch_projection(batch) for batch in visible_batches],
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
) -> dict[str, dict[str, Any]]:
    """单次投影为同一请求内的任务与候选列表共享 Git 事实。"""

    return {
        str(candidate["task_id"]): candidate
        for candidate in store.project_candidates(
            [item for item in candidates if item is not None], batches
        )
    }


def _task_projection(
    task: dict[str, Any], candidate_delivery: dict[str, Any] | None
) -> dict[str, Any]:
    projected = {
        "id": task.get("id"),
        "name": task.get("name"),
        "status": task.get("status"),
        "base_ref": task.get("base_ref"),
        "root_anchor_id": task.get("root_anchor_id"),
        "next_action": {"kind": "continue_task"},
    }
    if candidate_delivery:
        projected["candidate_delivery"] = _candidate_projection(candidate_delivery)
        projected["next_action"] = _candidate_next_action(candidate_delivery)
    return projected


def _candidate_projection(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": candidate.get("candidate_id"),
        "task_id": candidate.get("task_id"),
        "status": candidate.get("status"),
        "delivery_status": candidate.get("delivery_status"),
        "delivered": candidate.get("delivered"),
        "finalization_pending": candidate.get("finalization_pending"),
        "batch_ownership": copy.deepcopy(candidate.get("batch_ownership")),
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


def _batch_projection(batch: dict[str, Any]) -> dict[str, Any]:
    release = batch.get("runtime_release") or {}
    return {
        "id": batch.get("id"),
        "status": batch.get("status"),
        "trigger": batch.get("trigger"),
        "base_ref": batch.get("base_ref"),
        "base_before": batch.get("base_before"),
        "candidate_ids": list(batch.get("candidate_ids") or []),
        "validation_outcome": batch.get("validation_outcome"),
        "runtime_release_result": release.get("result"),
        "integration_head": batch.get("integration_head"),
        "next_action": (
            {"kind": "wait_or_recover_batch", "batch_id": batch.get("id")}
            if batch.get("status") in ACTIVE_BATCH_STATES
            else {"kind": "batch_terminal"}
        ),
    }


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
