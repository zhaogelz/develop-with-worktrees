from __future__ import annotations

import copy
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .config import RepoConfig
from .repo import GitRepo
from .util import (
    DirectoryLock,
    SoloAIError,
    atomic_write_json,
    process_matches,
    process_snapshot,
    read_json,
    utc_timestamp,
)


class StateStore:
    def __init__(self, repo: GitRepo):
        self.repo = repo
        self.path = repo.local_dir / "state.json"
        self.lock_path = repo.local_dir / "locks" / "state.lock"

    def _empty(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "slots": {},
            "tasks": {},
            "updated_at": utc_timestamp(),
        }

    def read(self) -> dict[str, Any]:
        state = read_json(self.path, self._empty())
        if state.get("schema_version") != 1:
            raise SoloAIError("Unsupported local state schema")
        return state

    def mutate(self, callback: Callable[[dict[str, Any]], Any]) -> Any:
        with DirectoryLock(self.lock_path):
            state = self.read()
            result = callback(state)
            state["updated_at"] = utc_timestamp()
            atomic_write_json(self.path, state)
            return result

    def ensure_slots(self, config: RepoConfig) -> dict[str, Any]:
        def update(state: dict[str, Any]) -> dict[str, Any]:
            for number in range(1, config.slots + 1):
                slot_id = f"{number:02d}"
                slot_path = (
                    self.repo.root
                    / config.worktree_directory
                    / f"solo-ai-slot-{slot_id}"
                )
                state["slots"].setdefault(
                    slot_id,
                    {
                        "id": slot_id,
                        "path": str(slot_path.resolve()),
                        "status": "idle",
                        "task_id": None,
                        "last_used": 0.0,
                        "quarantine_reason": None,
                    },
                )
            for slot_id in list(state["slots"]):
                if (
                    int(slot_id) > config.slots
                    and state["slots"][slot_id].get("status") != "idle"
                ):
                    raise SoloAIError(
                        f"Cannot reduce slots while slot {slot_id} is active"
                    )
            return copy.deepcopy(state)

        return self.mutate(update)

    def allocate(
        self, config: RepoConfig, *, name: str, branch: str, base_head: str
    ) -> dict[str, Any]:
        task_id = f"task-{time.strftime('%Y%m%d%H%M%S', time.gmtime())}-{uuid.uuid4().hex[:8]}"
        lease = uuid.uuid4().hex

        def update(state: dict[str, Any]) -> dict[str, Any]:
            candidates = [
                slot for slot in state["slots"].values() if slot.get("status") == "idle"
            ]
            if not candidates:
                raise SoloAIError("All managed worktree slots are busy or quarantined")
            slot = min(candidates, key=lambda item: float(item.get("last_used", 0.0)))
            task = {
                "id": task_id,
                "name": name,
                "slot_id": slot["id"],
                "worktree": slot["path"],
                "branch": branch,
                "base_head": base_head,
                "candidate_head": None,
                "status": "starting",
                "lease": lease,
                "lease_owner": process_snapshot(),
                "created_at": utc_timestamp(),
                "updated_at": utc_timestamp(),
                "ready_proof": None,
                "registered_outputs": [],
                "processes": [],
            }
            state["tasks"][task_id] = task
            slot.update(
                {"status": "active", "task_id": task_id, "quarantine_reason": None}
            )
            return copy.deepcopy(task)

        return self.mutate(update)

    def task(self, task_id: str) -> dict[str, Any]:
        task = self.read()["tasks"].get(task_id)
        if not task:
            raise SoloAIError(f"Unknown task id: {task_id}")
        return copy.deepcopy(task)

    def task_for_worktree(self, path: Path) -> dict[str, Any]:
        resolved = str(path.resolve())
        for task in self.read()["tasks"].values():
            if task.get("worktree") == resolved and task.get("status") not in {
                "finished",
                "abandoned",
            }:
                return copy.deepcopy(task)
        raise SoloAIError(f"No active task owns worktree: {resolved}")

    def require_lease(self, task: dict[str, Any], lease: str) -> None:
        if not lease or task.get("lease") != lease:
            raise SoloAIError("A current task lease is required for this operation")

    def update_task(self, task_id: str, **changes: Any) -> dict[str, Any]:
        def update(state: dict[str, Any]) -> dict[str, Any]:
            task = state["tasks"].get(task_id)
            if not task:
                raise SoloAIError(f"Unknown task id: {task_id}")
            task.update(changes)
            task["updated_at"] = utc_timestamp()
            return copy.deepcopy(task)

        return self.mutate(update)

    def quarantine(self, task_id: str, reason: str) -> None:
        def update(state: dict[str, Any]) -> None:
            task = state["tasks"][task_id]
            task["status"] = "quarantined"
            slot = state["slots"][task["slot_id"]]
            slot["status"] = "quarantined"
            slot["quarantine_reason"] = reason

        self.mutate(update)

    def release(self, task_id: str, *, final_status: str) -> None:
        def update(state: dict[str, Any]) -> None:
            task = state["tasks"][task_id]
            task["status"] = final_status
            task["lease"] = None
            task["lease_owner"] = None
            slot = state["slots"][task["slot_id"]]
            slot.update(
                {
                    "status": "idle",
                    "task_id": None,
                    "last_used": time.time(),
                    "quarantine_reason": None,
                }
            )

        self.mutate(update)

    def recover(self, task_id: str) -> dict[str, Any]:
        def update(state: dict[str, Any]) -> dict[str, Any]:
            task = state["tasks"].get(task_id)
            if not task or task.get("status") in {"finished", "abandoned"}:
                raise SoloAIError(f"Task cannot be recovered: {task_id}")
            active = task.get("active_operation")
            if active and process_matches(active):
                raise SoloAIError(
                    "Task still has an active operation; recovery is unsafe"
                )
            task["lease"] = uuid.uuid4().hex
            task["lease_owner"] = process_snapshot()
            task["status"] = "active"
            task["updated_at"] = utc_timestamp()
            return copy.deepcopy(task)

        return self.mutate(update)
