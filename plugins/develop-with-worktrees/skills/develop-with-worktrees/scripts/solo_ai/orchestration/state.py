from __future__ import annotations

import copy
import time
from pathlib import Path
from typing import Any, Callable

from ..repo import GitRepo
from ..util import DirectoryLock, SoloAIError, atomic_write_json, new_id, read_json, utc_timestamp
from .adapters import adapter_for
from .models import (
    MAX_DEVELOPMENT_PARALLELISM,
    SCHEMA_VERSION,
    PlannedTask,
    validate_batch,
    validate_tasks,
)
from .scheduler import acceptance_summary, frontier

ORCHESTRATION_DIRECTORY = "solo-ai-orchestration"


def _event(task: dict[str, Any], *, kind: str, summary: str) -> None:
    task.setdefault("events", []).append(
        {"at": utc_timestamp(), "kind": kind, "summary": summary}
    )
    # 只保留恢复和审计所需的近期摘要，不能积累完整对话。
    del task["events"][:-20]


def _repair_policy(
    *, max_effective_changes: int, max_repair_minutes: int
) -> dict[str, int]:
    if not 1 <= max_effective_changes <= 20:
        raise SoloAIError("max_effective_changes must be between 1 and 20")
    if not 1 <= max_repair_minutes <= 240:
        raise SoloAIError("max_repair_minutes must be between 1 and 240")
    return {
        "max_effective_changes": max_effective_changes,
        "max_repair_minutes": max_repair_minutes,
    }


def create_batch(
    repo: GitRepo,
    *,
    goal: str,
    tasks: list[dict[str, Any]] | list[Any],
    controller: str,
    adapter: str = "dww",
    max_parallel: int = MAX_DEVELOPMENT_PARALLELISM,
    max_effective_changes: int = 3,
    max_repair_minutes: int = 20,
) -> dict[str, Any]:
    if not isinstance(goal, str) or not goal.strip():
        raise SoloAIError("goal must be a non-empty string")
    if not isinstance(controller, str) or not controller.strip():
        raise SoloAIError("controller must be a non-empty opaque identifier")
    if not 1 <= max_parallel <= MAX_DEVELOPMENT_PARALLELISM:
        raise SoloAIError(
            f"max_parallel must be between 1 and {MAX_DEVELOPMENT_PARALLELISM}"
        )
    adapter_for(adapter).assert_available(repo)
    planned = validate_tasks(tasks)
    batch_id = new_id("batch")
    now = utc_timestamp()
    batch = {
        "schema_version": SCHEMA_VERSION,
        "id": batch_id,
        "goal": goal.strip(),
        "adapter": adapter,
        "controller": controller.strip(),
        "max_parallel": max_parallel,
        "repair_policy": _repair_policy(
            max_effective_changes=max_effective_changes,
            max_repair_minutes=max_repair_minutes,
        ),
        "status": "awaiting-confirmation",
        "created_at": now,
        "updated_at": now,
        "tasks": {task.task_id: task.to_state() for task in planned},
        "events": [
            {
                "at": now,
                "kind": "planned",
                "summary": "任务计划已生成，等待一次用户确认",
            }
        ],
    }
    validate_batch(batch)
    store = BatchStore(repo)
    path = store.path(batch_id)
    with DirectoryLock(store.root / "locks" / f"{batch_id}.lock", wait=True):
        if path.exists():
            raise SoloAIError("Generated orchestration batch id already exists")
        atomic_write_json(path, batch)
    return copy.deepcopy(batch)


class BatchStore:
    """编排状态与 DWW 生命周期状态完全隔离，二者只通过引用关联。"""

    def __init__(self, repo: GitRepo):
        self.repo = repo
        self.root = repo.common_dir / ORCHESTRATION_DIRECTORY

    def path(self, batch_id: str) -> Path:
        return self.root / "batches" / f"{batch_id}.json"

    def batch(self, batch_id: str) -> dict[str, Any]:
        batch = read_json(self.path(batch_id), None)
        if not isinstance(batch, dict):
            raise SoloAIError(f"Unknown orchestration batch: {batch_id}")
        validate_batch(batch)
        return copy.deepcopy(batch)

    def list(self) -> list[dict[str, Any]]:
        directory = self.root / "batches"
        if not directory.exists():
            return []
        return sorted(
            (self.batch(path.stem) for path in directory.glob("*.json")),
            key=lambda item: str(item["created_at"]),
            reverse=True,
        )

    def _mutate(
        self, batch_id: str, update: Callable[[dict[str, Any]], dict[str, Any] | None]
    ) -> dict[str, Any]:
        path = self.path(batch_id)
        with DirectoryLock(self.root / "locks" / f"{batch_id}.lock", wait=True):
            current = read_json(path, None)
            if not isinstance(current, dict):
                raise SoloAIError(f"Unknown orchestration batch: {batch_id}")
            validate_batch(current)
            outcome = update(current)
            current["updated_at"] = utc_timestamp()
            validate_batch(current)
            atomic_write_json(path, current)
            if current.get("status") == "completed":
                self._write_receipt(current)
            return copy.deepcopy(outcome if outcome is not None else current)

    def _write_receipt(self, batch: dict[str, Any]) -> None:
        """完成批次只额外留一张紧凑回执，避免把运行过程误当成长久聊天记录。"""
        receipt = {
            "schema_version": 1,
            "batch_id": batch["id"],
            "goal": batch["goal"],
            "adapter": batch["adapter"],
            "completed_at": batch["updated_at"],
            "tasks": [
                {
                    "id": task["id"],
                    "title": task["title"],
                    "lifecycle_task": task.get("lifecycle_task"),
                    "evidence": task.get("acceptance_evidence", []),
                }
                for task in batch["tasks"].values()
                if task.get("status") == "completed"
            ],
        }
        atomic_write_json(self.root / "receipts" / f"{batch['id']}.json", receipt)

    @staticmethod
    def _controller(batch: dict[str, Any], controller: str) -> None:
        if controller != batch.get("controller"):
            raise SoloAIError("Only the recorded central controller can schedule this batch")

    @staticmethod
    def _task(batch: dict[str, Any], task_id: str) -> dict[str, Any]:
        task = batch["tasks"].get(task_id)
        if not isinstance(task, dict):
            raise SoloAIError(f"Unknown orchestration task: {task_id}")
        return task

    @staticmethod
    def _refresh_batch_status(batch: dict[str, Any]) -> None:
        summary = acceptance_summary(batch)
        if summary["complete"]:
            batch["status"] = "completed"

    def confirm(self, batch_id: str, *, controller: str) -> dict[str, Any]:
        def update(batch: dict[str, Any]) -> dict[str, Any]:
            self._controller(batch, controller)
            if batch["status"] != "awaiting-confirmation":
                raise SoloAIError("Batch is not waiting for confirmation")
            batch["status"] = "running"
            batch["events"].append(
                {"at": utc_timestamp(), "kind": "confirmed", "summary": "用户已确认计划"}
            )
            return batch

        return self._mutate(batch_id, update)

    def pause(self, batch_id: str, *, controller: str) -> dict[str, Any]:
        def update(batch: dict[str, Any]) -> dict[str, Any]:
            self._controller(batch, controller)
            if batch["status"] != "running":
                raise SoloAIError("Only a running batch can be paused")
            batch["status"] = "paused"
            batch["events"].append(
                {"at": utc_timestamp(), "kind": "paused", "summary": "停止派发，已有任务保留"}
            )
            return batch

        return self._mutate(batch_id, update)

    def resume(self, batch_id: str, *, controller: str) -> dict[str, Any]:
        def update(batch: dict[str, Any]) -> dict[str, Any]:
            self._controller(batch, controller)
            if batch["status"] != "paused":
                raise SoloAIError("Only a paused batch can be resumed")
            batch["status"] = "running"
            batch["events"].append(
                {"at": utc_timestamp(), "kind": "resumed", "summary": "继续派发未完成任务"}
            )
            return batch

        return self._mutate(batch_id, update)

    def take_over(
        self, batch_id: str, *, controller: str, confirm: str
    ) -> dict[str, Any]:
        """新总控会话接手已保留的本地批次；不会触碰任何 worker 文件。"""
        if confirm != batch_id:
            raise SoloAIError("Controller handoff confirmation must exactly match the batch id")
        if not isinstance(controller, str) or not controller.strip():
            raise SoloAIError("controller must be a non-empty opaque identifier")

        def update(batch: dict[str, Any]) -> dict[str, Any]:
            if batch["status"] == "completed":
                raise SoloAIError("Completed batches do not need a controller handoff")
            batch["controller"] = controller.strip()
            batch["events"].append(
                {"at": utc_timestamp(), "kind": "controller-handoff", "summary": "新总控会话已接手"}
            )
            return batch

        return self._mutate(batch_id, update)

    def frontier(self, batch_id: str, *, available_slots: int) -> list[dict[str, Any]]:
        batch = self.batch(batch_id)
        return copy.deepcopy(frontier(batch, available_slots=available_slots))

    def claim(
        self,
        batch_id: str,
        *,
        task_id: str,
        worker: str,
        controller: str,
        available_slots: int = MAX_DEVELOPMENT_PARALLELISM,
    ) -> dict[str, Any]:
        if not isinstance(worker, str) or not worker.strip():
            raise SoloAIError("worker must be a non-empty identifier")

        def update(batch: dict[str, Any]) -> dict[str, Any]:
            self._controller(batch, controller)
            task = self._task(batch, task_id)
            if task not in frontier(batch, available_slots=available_slots):
                raise SoloAIError("Task is not in the current schedulable frontier")
            task.update({"status": "running", "assigned_worker": worker.strip()})
            _event(task, kind="claimed", summary="中央控制器已派发唯一写入者")
            return {"batch_id": batch_id, "task": copy.deepcopy(task)}

        return self._mutate(batch_id, update)

    def link_lifecycle_task(
        self,
        batch_id: str,
        *,
        task_id: str,
        lifecycle_task: str,
        controller: str,
    ) -> dict[str, Any]:
        if not isinstance(lifecycle_task, str) or not lifecycle_task.strip():
            raise SoloAIError("lifecycle_task must be a non-empty task reference")

        def update(batch: dict[str, Any]) -> dict[str, Any]:
            self._controller(batch, controller)
            task = self._task(batch, task_id)
            if task["status"] != "running":
                raise SoloAIError("Only a running task can link a lifecycle task")
            task["lifecycle_task"] = lifecycle_task.strip()
            _event(task, kind="lifecycle-linked", summary="已关联底层生命周期任务")
            return {"batch_id": batch_id, "task": copy.deepcopy(task)}

        return self._mutate(batch_id, update)

    def complete(
        self,
        batch_id: str,
        *,
        task_id: str,
        evidence: list[dict[str, Any]],
        controller: str,
    ) -> dict[str, Any]:
        if not evidence or not all(
            isinstance(item, dict)
            and isinstance(item.get("kind"), str)
            and item["kind"]
            and isinstance(item.get("ref"), str)
            and item["ref"]
            for item in evidence
        ):
            raise SoloAIError("acceptance evidence must contain kind and ref")

        def update(batch: dict[str, Any]) -> dict[str, Any]:
            self._controller(batch, controller)
            task = self._task(batch, task_id)
            if task["status"] != "running":
                raise SoloAIError("Only a running task can be completed")
            task.update(
                {
                    "status": "completed",
                    "acceptance_evidence": copy.deepcopy(evidence),
                    "blocked_reason": None,
                }
            )
            _event(task, kind="completed", summary="已记录可复核验收证据")
            for source_id in task.get("repairs_tasks", []):
                source = self._task(batch, str(source_id))
                source.update(
                    {
                        "status": "completed",
                        "blocked_reason": None,
                        "acceptance_evidence": [
                            {"kind": "repair-task", "ref": task["id"]},
                            *copy.deepcopy(evidence),
                        ],
                    }
                )
                _event(
                    source,
                    kind="repaired",
                    summary=f"由新修复任务 {task['id']} 替代旧分支结果",
                )
            self._refresh_batch_status(batch)
            return {
                "batch_id": batch_id,
                "task": copy.deepcopy(task),
                "status": batch["status"],
                "acceptance": acceptance_summary(batch),
            }

        return self._mutate(batch_id, update)

    def block(
        self, batch_id: str, *, task_id: str, reason: str, controller: str
    ) -> dict[str, Any]:
        if not isinstance(reason, str) or not reason.strip():
            raise SoloAIError("reason must be a non-empty summary")

        def update(batch: dict[str, Any]) -> dict[str, Any]:
            self._controller(batch, controller)
            task = self._task(batch, task_id)
            if task["status"] not in {"planned", "running", "paused"}:
                raise SoloAIError("Only an unfinished task can be blocked")
            task.update({"status": "blocked", "blocked_reason": reason.strip()})
            _event(task, kind="blocked", summary=reason.strip())
            return {"batch_id": batch_id, "task": copy.deepcopy(task)}

        return self._mutate(batch_id, update)

    def record_attempt(
        self,
        batch_id: str,
        *,
        task_id: str,
        changed: bool,
        summary: str,
        controller: str,
    ) -> dict[str, Any]:
        if not isinstance(summary, str) or not summary.strip():
            raise SoloAIError("summary must be a non-empty redacted failure summary")

        def update(batch: dict[str, Any]) -> dict[str, Any]:
            self._controller(batch, controller)
            task = self._task(batch, task_id)
            if task["status"] != "running":
                raise SoloAIError("Only a running task can record a repair attempt")
            repair = task["repair"]
            if repair["started_at"] is None:
                repair["started_at"] = time.time()
            if changed:
                repair["effective_changes"] += 1
                repair["unchanged_failures"] = 0
            else:
                repair["unchanged_failures"] += 1
            policy = batch["repair_policy"]
            elapsed_minutes = (time.time() - float(repair["started_at"])) / 60
            should_block = (
                repair["unchanged_failures"] >= 2
                or repair["effective_changes"] >= policy["max_effective_changes"]
                or elapsed_minutes > policy["max_repair_minutes"]
            )
            if should_block:
                task.update({"status": "blocked", "blocked_reason": summary.strip()})
                _event(task, kind="repair-escalated", summary=summary.strip())
            else:
                _event(task, kind="repair-attempt", summary=summary.strip())
            return {
                "batch_id": batch_id,
                "task": copy.deepcopy(task),
                "decision": "needs-human" if should_block else "continue",
            }

        return self._mutate(batch_id, update)

    def cancel(
        self,
        batch_id: str,
        *,
        task_id: str,
        confirm: str,
        controller: str,
    ) -> dict[str, Any]:
        if confirm != task_id:
            raise SoloAIError("Cancellation confirmation must exactly match the task id")

        def update(batch: dict[str, Any]) -> dict[str, Any]:
            self._controller(batch, controller)
            task = self._task(batch, task_id)
            if task["status"] in {"completed", "cancelled"}:
                raise SoloAIError("Completed or cancelled tasks cannot be cancelled again")
            task.update({"status": "cancelled", "code_preserved": True})
            _event(task, kind="cancelled", summary="已取消调度，分支和文件保留")
            return {"batch_id": batch_id, "task": copy.deepcopy(task)}

        return self._mutate(batch_id, update)

    def add_task(
        self,
        batch_id: str,
        *,
        raw_task: dict[str, Any],
        inside_approved_goal: bool,
        controller: str,
    ) -> dict[str, Any]:
        """只允许总控为已确认目标补充内部任务，不替产品范围作判断。"""
        if not inside_approved_goal:
            raise SoloAIError(
                "Adding a task requires inside_approved_goal; new user-visible scope needs a new central decision"
            )
        planned = validate_tasks([raw_task])[0]

        def update(batch: dict[str, Any]) -> dict[str, Any]:
            self._controller(batch, controller)
            if batch["status"] not in {"running", "paused"}:
                raise SoloAIError("Tasks can be added only after the approved batch has started")
            if planned.task_id in batch["tasks"]:
                raise SoloAIError("An orchestration task already uses this id")
            unknown = sorted(set(planned.depends_on) - set(batch["tasks"]))
            if unknown:
                raise SoloAIError(
                    "New task depends on unknown task: " + ", ".join(unknown)
                )
            batch["tasks"][planned.task_id] = planned.to_state()
            validate_batch(batch)
            batch["events"].append(
                {"at": utc_timestamp(), "kind": "task-added", "summary": "已在原批准目标内补充内部任务"}
            )
            return {"batch_id": batch_id, "task": copy.deepcopy(batch["tasks"][planned.task_id])}

        return self._mutate(batch_id, update)

    def create_repair(
        self,
        batch_id: str,
        *,
        source_ids: list[str],
        raw_task: dict[str, Any],
        reason: str,
        controller: str,
    ) -> dict[str, Any]:
        """为已归因问题建立新任务；绝不重开旧完成分支。"""
        if not source_ids:
            raise SoloAIError("A repair task needs at least one source task")
        if len(source_ids) != len(set(source_ids)):
            raise SoloAIError("Repair source task ids must be unique")
        if not isinstance(reason, str) or not reason.strip():
            raise SoloAIError("reason must be a non-empty redacted summary")
        repair_raw = dict(raw_task)
        repair_raw["kind"] = "repair"
        repair = PlannedTask.from_mapping(repair_raw)

        def update(batch: dict[str, Any]) -> dict[str, Any]:
            self._controller(batch, controller)
            if batch["status"] not in {"running", "paused"}:
                raise SoloAIError("Repairs require a running or paused approved batch")
            if repair.task_id in batch["tasks"]:
                raise SoloAIError("An orchestration task already uses this repair id")
            sources = [self._task(batch, source_id) for source_id in source_ids]
            if any(source["status"] == "running" for source in sources):
                raise SoloAIError("A running task must be blocked before creating its repair")
            dependencies = sorted(
                {
                    dependency
                    for source in sources
                    for dependency in source.get("depends_on", [])
                }
            )
            repair_state = repair.to_state()
            repair_state.update(
                {
                    "depends_on": dependencies,
                    "repairs_tasks": list(source_ids),
                }
            )
            batch["tasks"][repair.task_id] = repair_state
            for source in sources:
                source.update({"status": "blocked", "blocked_reason": reason.strip()})
                _event(
                    source,
                    kind="repair-created",
                    summary=f"将由新修复任务 {repair.task_id} 从最新基线处理",
                )
            validate_batch(batch)
            batch["events"].append(
                {"at": utc_timestamp(), "kind": "repair-created", "summary": reason.strip()}
            )
            return {"batch_id": batch_id, "task": copy.deepcopy(repair_state)}

        return self._mutate(batch_id, update)
