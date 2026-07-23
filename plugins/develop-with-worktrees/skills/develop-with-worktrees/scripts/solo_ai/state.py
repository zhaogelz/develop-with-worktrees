from __future__ import annotations

import copy
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

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


STATE_SCHEMA = 2
FINAL_TASK_STATES = {"finished", "abandoned"}


class StateStore:
    def __init__(self, repo: GitRepo):
        self.repo = repo
        self.path = repo.local_dir / "state.json"
        self.lock_path = repo.local_dir / "locks" / "state.lock"

    def _empty(self) -> dict[str, Any]:
        return {
            "schema_version": STATE_SCHEMA,
            "slots": {},
            "tasks": {},
            "updated_at": utc_timestamp(),
        }

    def read(self) -> dict[str, Any]:
        state = read_json(self.path, self._empty())
        if state.get("schema_version") != STATE_SCHEMA:
            raise SoloAIError(
                "Unsupported local state schema; run doctor before changing this repository"
            )
        return state

    def mutate(self, callback: Callable[[dict[str, Any]], Any]) -> Any:
        with DirectoryLock(self.lock_path):
            state = self.read()
            result = callback(state)
            state["updated_at"] = utc_timestamp()
            atomic_write_json(self.path, state)
            return result

    def _assert_slot_layout(self, state: dict[str, Any], config: RepoConfig) -> None:
        """受管槽位目录在首次采用后不可被配置文件静默迁移。"""
        for slot_id, slot in state["slots"].items():
            try:
                number = int(slot_id)
            except ValueError as exc:
                raise SoloAIError(f"Invalid managed slot id: {slot_id}") from exc
            if not 1 <= number <= 32:
                raise SoloAIError(
                    f"Managed slot id is outside supported range: {slot_id}"
                )
            expected = (
                self.repo.primary_path
                / config.worktree_directory
                / f"solo-ai-slot-{slot_id}"
            ).resolve()
            actual = Path(str(slot.get("path", ""))).resolve()
            if actual != expected:
                raise SoloAIError(
                    "worktree_directory is immutable after adoption; restore its "
                    "original value before continuing, or deinitialize and adopt "
                    "again to move managed slots"
                )

    def require_slot_layout(self, config: RepoConfig) -> dict[str, Any]:
        state = self.read()
        self._assert_slot_layout(state, config)
        return copy.deepcopy(state)

    def _ownership_path(self, slot_id: str) -> Path:
        return self.repo.local_dir / "ownership" / f"{slot_id}.json"

    def _ensure_slot_ownership(self, slot: dict[str, Any]) -> None:
        path = self._ownership_path(str(slot["id"]))
        expected = {
            "schema_version": 1,
            "slot_id": str(slot["id"]),
            "path": str(Path(str(slot["path"])).resolve()),
            "managed_root": str((self.repo.primary_path).resolve()),
        }
        existing = read_json(path, {})
        if existing:
            if any(existing.get(key) != value for key, value in expected.items()):
                raise SoloAIError(
                    f"Managed slot ownership record does not match: {slot['id']}"
                )
            return
        atomic_write_json(path, {**expected, "created_at": utc_timestamp()})

    def require_slot_ownership(self, slot_id: str, path: Path) -> dict[str, Any]:
        ownership = read_json(self._ownership_path(slot_id), {})
        if not ownership:
            raise SoloAIError(
                f"Managed slot has no ownership record and is retained: {slot_id}"
            )
        if (
            ownership.get("schema_version") != 1
            or ownership.get("slot_id") != slot_id
            or Path(str(ownership.get("path", ""))).resolve() != path.resolve()
            or Path(str(ownership.get("managed_root", ""))).resolve()
            != self.repo.primary_path.resolve()
        ):
            raise SoloAIError(
                f"Managed slot ownership record does not match and is retained: {slot_id}"
            )
        return ownership

    def ensure_slots(self, config: RepoConfig) -> dict[str, Any]:
        def update(state: dict[str, Any]) -> dict[str, Any]:
            self._assert_slot_layout(state, config)
            existing_numbers = [
                int(slot_id) for slot_id in state["slots"] if slot_id.isdigit()
            ]
            for number in range(
                1, max(config.slots, max(existing_numbers, default=0)) + 1
            ):
                slot_id = f"{number:02d}"
                path = (
                    self.repo.primary_path
                    / config.worktree_directory
                    / f"solo-ai-slot-{slot_id}"
                )
                slot = state["slots"].setdefault(
                    slot_id,
                    {
                        "id": slot_id,
                        "path": str(path.resolve()),
                        "status": "inactive" if number > config.slots else "idle",
                        "task_id": None,
                        "last_used": 0.0,
                        "quarantine_reason": None,
                    },
                )
                if number <= config.slots and slot["status"] == "inactive":
                    slot["status"] = "idle"
                if number > config.slots:
                    if slot["status"] in {"active", "starting", "ready"}:
                        slot["status"] = "draining"
                    elif slot["status"] == "idle":
                        slot["status"] = "inactive"
            return copy.deepcopy(state)

        result = self.mutate(update)
        for slot in result["slots"].values():
            self._ensure_slot_ownership(slot)
        return result

    def allocate(
        self,
        config: RepoConfig,
        *,
        name: str,
        branch: str,
        base_head: str,
        base_ref: str,
        base_worktree: Path,
    ) -> dict[str, Any]:
        task_id = f"task-{time.strftime('%Y%m%d%H%M%S', time.gmtime())}-{uuid.uuid4().hex[:8]}"
        lease = uuid.uuid4().hex

        def update(state: dict[str, Any]) -> dict[str, Any]:
            candidates = [
                slot
                for slot in state["slots"].values()
                if slot.get("status") == "idle" and int(slot["id"]) <= config.slots
            ]
            if not candidates:
                raise SoloAIError(
                    "All managed worktree slots are busy, draining, or quarantined; no task was queued"
                )
            slot = min(candidates, key=lambda item: float(item.get("last_used", 0.0)))
            task = {
                "id": task_id,
                "name": name,
                "slot_id": slot["id"],
                "worktree": slot["path"],
                "branch": branch,
                "base_head": base_head,
                "base_ref": base_ref,
                "base_worktree": str(base_worktree.resolve()),
                "candidate_head": None,
                "status": "starting",
                "lease": lease,
                "lease_owner": process_snapshot(),
                "active_operation": None,
                "created_at": utc_timestamp(),
                "updated_at": utc_timestamp(),
                "ready_proof": None,
                "baseline_paths": [],
                "processes": [],
            }
            state["tasks"][task_id] = task
            slot.update(
                {"status": "starting", "task_id": task_id, "quarantine_reason": None}
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
            if (
                task.get("worktree") == resolved
                and task.get("status") not in FINAL_TASK_STATES
            ):
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

    @contextmanager
    def operation(
        self, task_id: str, lease: str, kind: str
    ) -> Iterator[dict[str, Any]]:
        """Atomically publish an operation so recovery cannot steal an active lease."""
        operation_id = uuid.uuid4().hex

        def begin(state: dict[str, Any]) -> dict[str, Any]:
            task = state["tasks"].get(task_id)
            if not task:
                raise SoloAIError(f"Unknown task id: {task_id}")
            self.require_lease(task, lease)
            active = task.get("active_operation")
            if active and process_matches(active.get("owner", {})):
                raise SoloAIError(
                    f"Task already has a live {active.get('kind', 'unknown')} operation"
                )
            task["active_operation"] = {
                "id": operation_id,
                "kind": kind,
                "owner": process_snapshot(),
                "started_at": utc_timestamp(),
            }
            task["updated_at"] = utc_timestamp()
            return copy.deepcopy(task)

        task = self.mutate(begin)
        receipt_path = self.repo.local_dir / "operations" / f"{operation_id}.json"
        atomic_write_json(
            receipt_path,
            {
                "schema_version": 1,
                "id": operation_id,
                "task_id": task_id,
                "kind": kind,
                "status": "running",
                "started_at": task["active_operation"]["started_at"],
                "owner": task["active_operation"]["owner"],
            },
        )
        outcome = "succeeded"
        try:
            yield task
        except Exception:
            outcome = "failed"
            raise
        finally:

            def end(state: dict[str, Any]) -> None:
                current = state["tasks"].get(task_id)
                active = current.get("active_operation") if current else None
                if active and active.get("id") == operation_id:
                    current["active_operation"] = None
                    current["updated_at"] = utc_timestamp()

            self.mutate(end)
            receipt = read_json(receipt_path, {})
            if receipt.get("id") == operation_id:
                receipt.update({"status": outcome, "finished_at": utc_timestamp()})
                atomic_write_json(receipt_path, receipt)

    def quarantine(self, task_id: str, reason: str) -> None:
        def update(state: dict[str, Any]) -> None:
            task = state["tasks"][task_id]
            task["status"] = "quarantined"
            task["active_operation"] = None
            slot = state["slots"][task["slot_id"]]
            slot["status"] = "quarantined"
            slot["quarantine_reason"] = reason

        self.mutate(update)

    def quarantine_slot(self, slot_id: str, reason: str) -> None:
        def update(state: dict[str, Any]) -> None:
            slot = state["slots"].get(slot_id)
            if not slot or slot.get("task_id"):
                raise SoloAIError("Only an unassigned slot can be quarantined")
            slot.update(
                {
                    "status": "quarantined",
                    "quarantine_reason": reason,
                }
            )

        self.mutate(update)

    def restore_quarantined_slot(self, slot_id: str) -> dict[str, Any]:
        def update(state: dict[str, Any]) -> dict[str, Any]:
            slot = state["slots"].get(slot_id)
            if not slot or slot.get("status") != "quarantined" or slot.get("task_id"):
                raise SoloAIError("Only an unassigned quarantined slot can be restored")
            slot.update({"status": "idle", "quarantine_reason": None})
            return copy.deepcopy(slot)

        return self.mutate(update)

    def release(self, task_id: str, *, final_status: str) -> None:
        def update(state: dict[str, Any]) -> None:
            task = state["tasks"][task_id]
            task["status"] = final_status
            task["lease"] = None
            task["lease_owner"] = None
            task["active_operation"] = None
            slot = state["slots"][task["slot_id"]]
            slot.update(
                {
                    "status": "inactive"
                    if slot.get("status") == "draining"
                    else "idle",
                    "task_id": None,
                    "last_used": time.time(),
                    "quarantine_reason": None,
                }
            )

        self.mutate(update)

    def recover(self, task_id: str) -> dict[str, Any]:
        def update(state: dict[str, Any]) -> dict[str, Any]:
            task = state["tasks"].get(task_id)
            if not task or task.get("status") in FINAL_TASK_STATES:
                raise SoloAIError(f"Task cannot be recovered: {task_id}")
            active = task.get("active_operation")
            if active and process_matches(active.get("owner", {})):
                raise SoloAIError("Task still has a live operation; recovery is unsafe")
            live_runs: list[str] = []
            interrupted_runs: list[Path] = []
            run_root = self.repo.local_dir / "validation-runs"
            for receipt_path in run_root.glob("**/*.json") if run_root.exists() else ():
                receipt = read_json(receipt_path, {})
                if receipt.get("metadata", {}).get("task_id") != task_id:
                    continue
                if receipt.get("status") in {"running", "terminating"}:
                    if process_matches(receipt.get("process", {})):
                        live_runs.append(str(receipt_path))
                    else:
                        interrupted_runs.append(receipt_path)
            if live_runs:
                raise SoloAIError(
                    "Task still has a live validation process; recovery is unsafe:\n"
                    + "\n".join(f"- {path}" for path in live_runs)
                )
            for receipt_path in interrupted_runs:
                receipt = read_json(receipt_path, {})
                receipt.update(
                    {"status": "interrupted", "recovered_at": utc_timestamp()}
                )
                atomic_write_json(receipt_path, receipt)
            task["lease"] = uuid.uuid4().hex
            task["lease_owner"] = process_snapshot()
            task["active_operation"] = None
            if task["status"] == "quarantined":
                raise SoloAIError(
                    "Quarantined tasks require doctor and manual repair; recover will not hide them"
                )
            task["status"] = "active"
            task["updated_at"] = utc_timestamp()
            return copy.deepcopy(task)

        return self.mutate(update)

    @staticmethod
    def public_task(task: dict[str, Any]) -> dict[str, Any]:
        value = copy.deepcopy(task)
        value.pop("lease", None)
        value.pop("lease_owner", None)
        active = value.get("active_operation")
        if active:
            active.pop("owner", None)
        return value
