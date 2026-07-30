from __future__ import annotations

from typing import Any

from .models import MAX_DEVELOPMENT_PARALLELISM


def frontier(batch: dict[str, Any], *, available_slots: int) -> list[dict[str, Any]]:
    """返回可派发前沿；同文件默认乐观并行，显式高风险资源才独占。"""
    if batch.get("status") != "running" or available_slots <= 0:
        return []
    tasks = batch["tasks"]
    running = [task for task in tasks.values() if task.get("status") == "running"]
    remaining = min(
        int(batch["max_parallel"]), MAX_DEVELOPMENT_PARALLELISM, available_slots
    ) - len(running)
    if remaining <= 0:
        return []
    occupied = {
        resource
        for task in running
        for resource in task.get("exclusive_resources", [])
    }
    ready: list[dict[str, Any]] = []
    for task_id in sorted(tasks):
        task = tasks[task_id]
        if task.get("status") != "planned":
            continue
        dependencies = [tasks[item] for item in task.get("depends_on", [])]
        if not all(item.get("status") == "completed" for item in dependencies):
            continue
        resources = set(task.get("exclusive_resources", []))
        if resources & occupied:
            continue
        ready.append(task)
        occupied.update(resources)
        if len(ready) >= remaining:
            break
    return ready


def acceptance_summary(batch: dict[str, Any]) -> dict[str, Any]:
    missing: list[dict[str, str]] = []
    for task in batch["tasks"].values():
        if task.get("status") != "completed":
            missing.append({"task_id": str(task["id"]), "reason": "task-not-completed"})
        elif not task.get("acceptance_evidence"):
            missing.append({"task_id": str(task["id"]), "reason": "missing-evidence"})
    return {"complete": not missing, "missing": missing}
