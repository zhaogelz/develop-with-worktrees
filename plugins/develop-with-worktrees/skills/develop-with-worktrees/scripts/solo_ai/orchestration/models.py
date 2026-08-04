from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..util import SoloAIError

SCHEMA_VERSION = 1
MAX_DEVELOPMENT_PARALLELISM = 5
TASK_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
ADAPTERS = {"dww", "delegated"}
TASK_KINDS = {"vertical", "contract", "repair"}
TASK_STATUSES = {"planned", "running", "blocked", "paused", "completed", "cancelled"}


def _string(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SoloAIError(f"{field} must be a non-empty string")
    return value.strip()


def _strings(value: Any, *, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise SoloAIError(f"{field} must be an array of non-empty strings")
    return tuple(item.strip() for item in value)


@dataclass(frozen=True)
class PlannedTask:
    """计划阶段的最小任务描述；只保存执行所需摘要，不保存对话。"""

    task_id: str
    title: str
    acceptance: tuple[str, ...]
    depends_on: tuple[str, ...] = ()
    write_scope: tuple[str, ...] = ()
    exclusive_resources: tuple[str, ...] = ()
    writer: str | None = None
    kind: str = "vertical"

    @classmethod
    def from_mapping(cls, raw: Any) -> PlannedTask:
        if not isinstance(raw, dict):
            raise SoloAIError("task must be an object")
        task_id = _string(raw.get("id"), field="task.id")
        if not TASK_ID_PATTERN.fullmatch(task_id):
            raise SoloAIError(
                "task.id must start with a lowercase letter and use only lowercase letters, numbers, _ or -"
            )
        acceptance = _strings(
            raw.get("acceptance"), field=f"task[{task_id}].acceptance"
        )
        if not acceptance:
            raise SoloAIError(f"task[{task_id}].acceptance must not be empty")
        kind = str(raw.get("kind", "vertical"))
        if kind not in TASK_KINDS:
            raise SoloAIError(
                f"task[{task_id}].kind must be one of: {', '.join(sorted(TASK_KINDS))}"
            )
        return cls(
            task_id=task_id,
            title=_string(raw.get("title"), field=f"task[{task_id}].title"),
            acceptance=acceptance,
            depends_on=_strings(
                raw.get("depends_on"), field=f"task[{task_id}].depends_on"
            ),
            write_scope=_strings(
                raw.get("write_scope"), field=f"task[{task_id}].write_scope"
            ),
            exclusive_resources=_strings(
                raw.get("exclusive_resources"),
                field=f"task[{task_id}].exclusive_resources",
            ),
            writer=(
                _string(raw["writer"], field=f"task[{task_id}].writer")
                if raw.get("writer") is not None
                else None
            ),
            kind=kind,
        )

    def to_state(self) -> dict[str, Any]:
        return {
            "id": self.task_id,
            "title": self.title,
            "kind": self.kind,
            "acceptance": list(self.acceptance),
            "depends_on": list(self.depends_on),
            "write_scope": list(self.write_scope),
            "exclusive_resources": list(self.exclusive_resources),
            "writer": self.writer or f"worker-{self.task_id}",
            "status": "planned",
            "assigned_worker": None,
            "lifecycle_task": None,
            "acceptance_evidence": [],
            "code_preserved": None,
            "blocked_reason": None,
            "repair": {
                "effective_changes": 0,
                "unchanged_failures": 0,
                "started_at": None,
            },
            "events": [],
        }


def validate_tasks(raw_tasks: list[dict[str, Any]] | list[Any]) -> list[PlannedTask]:
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise SoloAIError("tasks must be a non-empty array")
    tasks = [PlannedTask.from_mapping(raw) for raw in raw_tasks]
    ids = [task.task_id for task in tasks]
    if len(ids) != len(set(ids)):
        raise SoloAIError("task ids must be unique")
    known = set(ids)
    for task in tasks:
        if task.task_id in task.depends_on:
            raise SoloAIError(f"task[{task.task_id}] cannot depend on itself")
        unknown = sorted(set(task.depends_on) - known)
        if unknown:
            raise SoloAIError(
                f"task[{task.task_id}] depends on unknown task: {', '.join(unknown)}"
            )
    _assert_acyclic(tasks)
    return tasks


def _assert_acyclic(tasks: list[PlannedTask]) -> None:
    dependencies = {task.task_id: set(task.depends_on) for task in tasks}
    completed: set[str] = set()
    while dependencies:
        ready = sorted(
            task_id for task_id, values in dependencies.items() if not values
        )
        if not ready:
            raise SoloAIError("tasks contain a dependency cycle")
        for task_id in ready:
            dependencies.pop(task_id)
            completed.add(task_id)
        for values in dependencies.values():
            values.difference_update(completed)


def validate_batch(batch: dict[str, Any]) -> None:
    if batch.get("schema_version") != SCHEMA_VERSION:
        raise SoloAIError(
            f"Unsupported orchestration state schema; expected {SCHEMA_VERSION}"
        )
    if batch.get("adapter") not in ADAPTERS:
        raise SoloAIError("orchestration state has an unknown lifecycle adapter")
    if not isinstance(batch.get("controller"), str) or not batch["controller"]:
        raise SoloAIError("orchestration state has no controller")
    if batch.get("status") not in {
        "awaiting-confirmation",
        "running",
        "paused",
        "completed",
    }:
        raise SoloAIError("orchestration state has an invalid batch status")
    parallelism = batch.get("max_parallel")
    if (
        isinstance(parallelism, bool)
        or not isinstance(parallelism, int)
        or not 1 <= parallelism <= MAX_DEVELOPMENT_PARALLELISM
    ):
        raise SoloAIError(
            f"orchestration max_parallel must be between 1 and {MAX_DEVELOPMENT_PARALLELISM}"
        )
    tasks = batch.get("tasks")
    if not isinstance(tasks, dict) or not tasks:
        raise SoloAIError("orchestration state has no tasks")
    task_values = list(tasks.values())
    if not all(isinstance(item, dict) for item in task_values):
        raise SoloAIError("orchestration state tasks are invalid")
    validate_tasks(
        [
            {
                "id": task.get("id"),
                "title": task.get("title"),
                "acceptance": task.get("acceptance"),
                "depends_on": task.get("depends_on"),
                "write_scope": task.get("write_scope"),
                "exclusive_resources": task.get("exclusive_resources"),
                "writer": task.get("writer"),
                "kind": task.get("kind", "vertical"),
            }
            for task in task_values
        ]
    )
    if any(task.get("status") not in TASK_STATUSES for task in task_values):
        raise SoloAIError("orchestration state has an invalid task status")
