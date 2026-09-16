from __future__ import annotations

import copy
import os
import shutil
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config import RepoConfig
from .repo import GitRepo
from .util import (
    DirectoryLock,
    SoloAIError,
    atomic_write_json,
    path_identity,
    process_matches,
    process_snapshot,
    read_json,
    sha256_text,
    utc_timestamp,
)

STATE_SCHEMA = 9
FINAL_TASK_STATES = {"finished", "abandoned", "candidate-published"}
IN_PLACE_MODE = "in-place"


def candidate_admission_lock(repo: GitRepo) -> DirectoryLock:
    """串行化候选生产者登记、终态发布与尾批冻结。"""

    return DirectoryLock(
        repo.local_dir / "locks" / "candidate-admission.lock", wait=True
    )


ISOLATED_MODE = "isolated"


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
            "pending_operation_outcomes": {},
            "updated_at": utc_timestamp(),
        }

    @property
    def guard_path(self) -> Path:
        return self.repo.local_dir / "guard-state.json"

    @property
    def guard_lock_path(self) -> Path:
        return self.repo.local_dir / "locks" / "guard-state.lock"

    def _guard(self) -> dict[str, Any]:
        return read_json(
            self.guard_path,
            {"schema_version": 1, "quarantines": {}, "alerts": []},
        )

    def _mutate_guard(self, callback: Callable[[dict[str, Any]], Any]) -> Any:
        """与 stdlib hook 共用的轻量锁；绝不占用 lifecycle 的 state.lock。"""
        deadline = time.monotonic() + 2.0
        while True:
            try:
                self.guard_lock_path.parent.mkdir(parents=True, exist_ok=True)
                self.guard_lock_path.mkdir()
                break
            except FileExistsError:
                owner = read_json(self.guard_lock_path / "owner.json", {})
                started = owner.get("started_at")
                if isinstance(started, (int, float)) and time.time() - started > 60:
                    shutil.rmtree(self.guard_lock_path, ignore_errors=True)
                    continue
                if time.monotonic() >= deadline:
                    raise SoloAIError(
                        "Guard state is busy; retry without changing files"
                    )
                time.sleep(0.02)
        try:
            atomic_write_json(
                self.guard_lock_path / "owner.json",
                {"pid": os.getpid(), "started_at": time.time()},
            )
            guard = self._guard()
            result = callback(guard)
            atomic_write_json(self.guard_path, guard)
            return result
        finally:
            shutil.rmtree(self.guard_lock_path, ignore_errors=True)

    def _apply_guard_quarantines(self, state: dict[str, Any]) -> None:
        quarantines = self._guard().get("quarantines", {})
        if not isinstance(quarantines, dict):
            return
        for task_id, value in quarantines.items():
            task = state.get("tasks", {}).get(task_id)
            if not task or task.get("status") in FINAL_TASK_STATES:
                continue
            if not isinstance(value, dict):
                continue
            task["status"] = "quarantined"
            task["active_operation"] = None
            task["quarantine_reason"] = str(
                value.get("reason") or "Codex guard quarantined this in-place task"
            )

    def read(self) -> dict[str, Any]:
        state = read_json(self.path, self._empty())
        version = state.get("schema_version")
        if version == 2:
            # beta2 只给既有隔离任务补充显式模式，不改变其租约、槽位或分支。
            for task in state.get("tasks", {}).values():
                task.setdefault("mode", ISOLATED_MODE)
                task.setdefault("integration", None)
                task.setdefault("abandonment", None)
            state["schema_version"] = STATE_SCHEMA
        elif version == 3:
            for task in state.get("tasks", {}).values():
                task.setdefault("integration", None)
                task.setdefault("abandonment", None)
            state["schema_version"] = STATE_SCHEMA
        elif version == 4:
            for task in state.get("tasks", {}).values():
                task.setdefault("request_id", None)
                task.setdefault("supersedes", None)
                task.setdefault("candidate_publication", None)
            state["schema_version"] = STATE_SCHEMA
        elif version == 5:
            for task in state.get("tasks", {}).values():
                task.setdefault("integration_policy", None)
            state["schema_version"] = STATE_SCHEMA
        elif version == 6:
            for task in state.get("tasks", {}).values():
                task.setdefault("root_anchor_file", None)
            state["schema_version"] = STATE_SCHEMA
        elif version == 7:
            for task in state.get("tasks", {}).values():
                task.setdefault("host_origin", None)
            state["schema_version"] = STATE_SCHEMA
        elif version == 8:
            # 已经启动的旧任务仍必须保留 Ready 作为候选发布门禁；新策略只
            # 对新建的 schema 3 policy 生效，不能在活动现场热替换语义。
            for task in state.get("tasks", {}).values():
                policy = task.get("integration_policy")
                if isinstance(policy, dict):
                    policy.setdefault("candidate_validation", "ready")
            state["schema_version"] = STATE_SCHEMA
        elif version != STATE_SCHEMA:
            raise SoloAIError(
                "Unsupported local state schema; run doctor before changing this repository"
            )
        for slot in state.get("slots", {}).values():
            slot.setdefault("generation", 0)
        for task in state.get("tasks", {}).values():
            task.setdefault("integration_policy", None)
            task.setdefault("root_anchor_file", None)
            task.setdefault("host_origin", None)
            policy = task.get("integration_policy")
            if isinstance(policy, dict):
                policy.setdefault("candidate_validation", "ready")
        state.setdefault("pending_operation_outcomes", {})
        self._apply_guard_quarantines(state)
        return state

    @staticmethod
    def integration_policy(
        config: RepoConfig, *, legacy_explicit: bool = False
    ) -> dict[str, Any]:
        seal_policy = "explicit" if legacy_explicit else config.integration.seal_policy
        mode = config.integration.mode
        if mode == "direct":
            seal_policy = "explicit"
        tail_policy = "explicit" if legacy_explicit else config.integration.tail_policy
        if mode == "direct":
            tail_policy = "explicit"
        candidate_validation = (
            "ready"
            if legacy_explicit or mode == "direct"
            else config.integration.candidate_validation
        )
        identity = (
            f"candidate-policy-v3:{mode}:{config.integration.batch_size}:"
            f"{config.integration.candidate_capacity}:{seal_policy}:"
            f"{candidate_validation}:{tail_policy}:{config.integration.tail_quiet_seconds:g}"
        )
        worktree_mode = config.integration.worktree_mode
        if worktree_mode == "reusable":
            identity += ":worktree-reusable-v1"
        return {
            "schema_version": 3,
            "mode": mode,
            "batch_size": config.integration.batch_size,
            "candidate_capacity": config.integration.candidate_capacity,
            "seal_policy": seal_policy,
            "candidate_validation": candidate_validation,
            "tail_policy": tail_policy,
            "tail_quiet_seconds": config.integration.tail_quiet_seconds,
            "worktree_mode": worktree_mode,
            "activation_epoch": sha256_text(identity),
        }

    def candidate_producer_snapshot(
        self, *, base_ref: str, base_head: str, activation_epoch: str
    ) -> dict[str, Any]:
        """从任务事实投影一个候选通道的生产者状态，不猜测宿主活动。"""

        matching: list[dict[str, Any]] = []
        active: list[dict[str, Any]] = []
        for task in self.read()["tasks"].values():
            policy = task.get("integration_policy") or {}
            in_place_blocker = (
                self.mode(task) == IN_PLACE_MODE
                and task.get("base_ref") == base_ref
                and task.get("base_head") == base_head
            )
            if not in_place_blocker and (
                self.mode(task) != ISOLATED_MODE
                or task.get("base_ref") != base_ref
                or task.get("base_head") != base_head
                or policy.get("mode") != "batched"
                or policy.get("activation_epoch") != activation_epoch
            ):
                continue
            matching.append(task)
            if task.get("status") not in FINAL_TASK_STATES:
                active.append(task)
        quiet_since = None
        if matching and not active:
            quiet_since = max(str(task.get("updated_at") or "") for task in matching)
        return {
            "base_ref": base_ref,
            "base_head": base_head,
            "activation_epoch": activation_epoch,
            "active_count": len(active),
            "active_task_ids": sorted(str(task["id"]) for task in active),
            "quiet_since": quiet_since or None,
        }

    def ensure_task_integration_policy(
        self, task_id: str, config: RepoConfig
    ) -> dict[str, Any]:
        """Freeze a safe legacy policy before repository config can affect a task."""

        def update(state: dict[str, Any]) -> dict[str, Any]:
            task = state["tasks"].get(task_id)
            if not task:
                raise SoloAIError(f"Unknown task: {task_id}")
            if not task.get("integration_policy"):
                task["integration_policy"] = self.integration_policy(
                    config, legacy_explicit=True
                )
                task["updated_at"] = utc_timestamp()
            return copy.deepcopy(task)

        return self.mutate(update)

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
                        "generation": 0,
                        "released_worktree_identity": None,
                        "released_managed_root_identity": None,
                        "released_worktree_resolved": None,
                        "released_managed_root_resolved": None,
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
        anchor_contract: dict[str, str],
        request_id: str | None = None,
        supersedes: str | None = None,
        root_anchor_id: str | None = None,
        root_anchor_file: str | None = None,
        host_origin: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        task_id = f"task-{time.strftime('%Y%m%d%H%M%S', time.gmtime())}-{uuid.uuid4().hex[:8]}"
        lease = uuid.uuid4().hex

        def update(state: dict[str, Any]) -> dict[str, Any]:
            if request_id:
                matches = [
                    task
                    for task in state["tasks"].values()
                    if task.get("request_id") == request_id
                ]
                if matches:
                    existing = matches[0]
                    if (
                        existing.get("name") != name
                        or existing.get("base_ref") != base_ref
                        or existing.get("root_anchor_id") != root_anchor_id
                        or existing.get("root_anchor_file") != root_anchor_file
                        or existing.get("host_origin") != host_origin
                        or existing.get("anchor_contract", anchor_contract)
                        != anchor_contract
                    ):
                        raise SoloAIError(
                            "The request id is already bound to a different task"
                        )
                    if not existing.get("integration_policy"):
                        existing["integration_policy"] = self.integration_policy(
                            config, legacy_explicit=True
                        )
                    return {**copy.deepcopy(existing), "request_reused": True}
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
            slot["generation"] = int(slot.get("generation", 0)) + 1
            task = {
                "id": task_id,
                "name": name,
                "mode": ISOLATED_MODE,
                "slot_id": slot["id"],
                "worktree": slot["path"],
                "branch": branch,
                "base_head": base_head,
                "base_ref": base_ref,
                "base_worktree": str(base_worktree.resolve()),
                "base_worktree_resolved": str(base_worktree.resolve()),
                "base_worktree_identity": path_identity(base_worktree),
                "anchor_origin": {
                    "schema_version": "1",
                    "task_id": task_id,
                    "original_purpose": name,
                    "reference_baseline": f"`{base_ref}` at `{base_head}`",
                },
                "anchor_contract": copy.deepcopy(anchor_contract),
                "root_anchor_id": root_anchor_id,
                "root_anchor_file": root_anchor_file,
                "host_origin": copy.deepcopy(host_origin),
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
                "integration": None,
                "abandonment": None,
                "request_id": request_id,
                "supersedes": supersedes,
                "candidate_publication": None,
                "integration_policy": self.integration_policy(config),
                "slot_worktree_identity": copy.deepcopy(
                    slot.get("released_worktree_identity")
                ),
                "slot_managed_root_identity": copy.deepcopy(
                    slot.get("released_managed_root_identity")
                ),
                "slot_worktree_resolved": slot.get("released_worktree_resolved"),
                "slot_managed_root_resolved": slot.get(
                    "released_managed_root_resolved"
                ),
            }
            state["tasks"][task_id] = task
            slot.update(
                {"status": "starting", "task_id": task_id, "quarantine_reason": None}
            )
            return {**copy.deepcopy(task), "request_reused": False}

        return self.mutate(update)

    def allocate_in_place(
        self,
        *,
        name: str,
        branch: str,
        head: str,
        base_worktree: Path,
        session_id: str,
        anchor_contract: dict[str, str],
        root_anchor_id: str | None = None,
        root_anchor_file: str | None = None,
    ) -> dict[str, Any]:
        """登记一次性当前工作树任务；不占槽位、不创建分支。"""
        if not session_id:
            raise SoloAIError("In-place tasks require a Codex session identifier")
        task_id = f"task-{time.strftime('%Y%m%d%H%M%S', time.gmtime())}-{uuid.uuid4().hex[:8]}"
        lease = uuid.uuid4().hex
        resolved = str(base_worktree.resolve())

        def update(state: dict[str, Any]) -> dict[str, Any]:
            active_in_place = [
                task
                for task in state["tasks"].values()
                if task.get("mode", ISOLATED_MODE) == IN_PLACE_MODE
                and task.get("status") not in FINAL_TASK_STATES
            ]
            if active_in_place:
                raise SoloAIError(
                    "An in-place task is already active for this repository; preserve it, finish it, or explicitly resume it before starting another"
                )
            task = {
                "id": task_id,
                "name": name,
                "mode": IN_PLACE_MODE,
                "slot_id": None,
                "worktree": resolved,
                "branch": branch,
                "base_head": head,
                "start_head": head,
                "expected_head": head,
                "base_ref": branch,
                "base_worktree": resolved,
                "base_worktree_resolved": resolved,
                "base_worktree_identity": path_identity(base_worktree),
                "slot_worktree_resolved": resolved,
                "slot_worktree_identity": path_identity(base_worktree),
                "anchor_origin": {
                    "schema_version": "1",
                    "task_id": task_id,
                    "original_purpose": name,
                    "reference_baseline": f"`{branch}` at `{head}`",
                },
                "anchor_contract": copy.deepcopy(anchor_contract),
                "root_anchor_id": root_anchor_id,
                "root_anchor_file": root_anchor_file,
                "candidate_head": head,
                "status": "active",
                "lease": lease,
                "lease_owner": process_snapshot(),
                "session_fingerprint": sha256_text(session_id),
                "active_operation": None,
                "created_at": utc_timestamp(),
                "updated_at": utc_timestamp(),
                "ready_proof": None,
                "baseline_paths": [],
                "processes": [],
                "integration": None,
                "abandonment": None,
                "request_id": None,
                "supersedes": None,
                "candidate_publication": None,
                "integration_policy": {
                    "schema_version": 1,
                    "mode": "direct",
                    "batch_size": 1,
                    "candidate_capacity": 1,
                    "seal_policy": "explicit",
                    "tail_policy": "explicit",
                    "tail_quiet_seconds": 90,
                    "activation_epoch": sha256_text(
                        "candidate-policy-v1:in-place-direct"
                    ),
                },
            }
            state["tasks"][task_id] = task
            return copy.deepcopy(task)

        return self.mutate(update)

    @staticmethod
    def mode(task: dict[str, Any]) -> str:
        return str(task.get("mode", ISOLATED_MODE))

    def active_in_place(self) -> dict[str, Any] | None:
        for task in self.read()["tasks"].values():
            if (
                self.mode(task) == IN_PLACE_MODE
                and task.get("status") not in FINAL_TASK_STATES
            ):
                return copy.deepcopy(task)
        return None

    def guard_alerts(self) -> list[dict[str, Any]]:
        alerts = self._guard().get("alerts", [])
        if not isinstance(alerts, list):
            return []
        return copy.deepcopy(alerts[-20:])

    def clear_guard_quarantine(self, task_id: str) -> None:
        def update(guard: dict[str, Any]) -> None:
            quarantines = guard.setdefault("quarantines", {})
            if isinstance(quarantines, dict):
                quarantines.pop(task_id, None)

        self._mutate_guard(update)

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

    def activate_started_task(
        self,
        task_id: str,
        *,
        runtime_activation: dict[str, Any],
        runtime_adapter_repair: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        def update(state: dict[str, Any]) -> dict[str, Any]:
            task = state["tasks"].get(task_id)
            if not task or task.get("status") != "starting":
                raise SoloAIError("Only an exact starting task can become active")
            slot = state["slots"].get(str(task.get("slot_id")))
            if (
                not slot
                or slot.get("task_id") != task_id
                or slot.get("status") != "starting"
            ):
                raise SoloAIError("Starting task lost its exact managed slot")
            task.update(
                {
                    "status": "active",
                    "runtime_activation": copy.deepcopy(runtime_activation),
                    "runtime_activation_pending": False,
                    "runtime_adapter_repair": copy.deepcopy(runtime_adapter_repair),
                    "updated_at": utc_timestamp(),
                }
            )
            slot["status"] = "active"
            return copy.deepcopy(task)

        return self.mutate(update)

    def resume_quarantined_start(
        self,
        task_id: str,
        *,
        operation_id: str | None,
        candidate_head: str,
        baseline_paths: list[str],
        worktree_identity: dict[str, object],
        managed_root_identity: dict[str, object],
        worktree_resolved: str,
        managed_root_resolved: str,
    ) -> dict[str, Any]:
        """在 Git 现场完成复核后，原子续接尚未激活的隔离 Start。"""

        def update(state: dict[str, Any]) -> dict[str, Any]:
            task = state["tasks"].get(task_id)
            if (
                not task
                or self.mode(task) != ISOLATED_MODE
                or task.get("status") != "quarantined"
                or task.get("candidate_publication")
                or task.get("integration")
                or task.get("abandonment")
                or task.get("runtime_activation") is not None
                or task.get("runtime_activation_pending") is False
            ):
                raise SoloAIError(
                    "Only a quarantined pre-activation Start can be resumed"
                )
            active = task.get("active_operation") or {}
            if operation_id is not None:
                if active.get("id") != operation_id or active.get("kind") != "recover":
                    raise SoloAIError(
                        "Quarantined Start recovery lost its exact operation"
                    )
            elif active and process_matches(active.get("owner", {})):
                raise SoloAIError("A live operation blocks Start recovery")
            slot = state["slots"].get(str(task.get("slot_id")))
            if (
                not slot
                or slot.get("task_id") != task_id
                or slot.get("status") != "quarantined"
            ):
                raise SoloAIError("Quarantined Start lost its exact managed slot")
            task.update(
                {
                    "status": "starting",
                    "lease": uuid.uuid4().hex,
                    "lease_owner": process_snapshot(),
                    "candidate_head": candidate_head,
                    "baseline_paths": list(baseline_paths),
                    "slot_worktree_identity": copy.deepcopy(worktree_identity),
                    "slot_managed_root_identity": copy.deepcopy(managed_root_identity),
                    "slot_worktree_resolved": worktree_resolved,
                    "slot_managed_root_resolved": managed_root_resolved,
                    "runtime_activation_pending": True,
                    "quarantine_reason": None,
                    "updated_at": utc_timestamp(),
                }
            )
            slot.update({"status": "starting", "quarantine_reason": None})
            return copy.deepcopy(task)

        return self.mutate(update)

    def prepare_integration(
        self,
        task_id: str,
        *,
        operation_id: str | None,
        integration: dict[str, Any],
    ) -> dict[str, Any]:
        def update(state: dict[str, Any]) -> dict[str, Any]:
            task = state["tasks"].get(task_id)
            if not task or task.get("status") not in {"ready", "finishing"}:
                raise SoloAIError("Only a ready task can prepare integration")
            active = task.get("active_operation") or {}
            if operation_id is not None:
                if active.get("id") != operation_id or active.get("kind") != "finish":
                    raise SoloAIError(
                        "Integration preparation lost its Finish operation"
                    )
            elif (
                active
                and active.get("kind") != "recover"
                and process_matches(active.get("owner", {}))
            ):
                raise SoloAIError("A live operation blocks integration recovery")
            existing = task.get("integration")
            if existing and existing != integration:
                raise SoloAIError("A different integration transaction already exists")
            task["integration"] = copy.deepcopy(integration)
            task["status"] = "finishing"
            task["updated_at"] = utc_timestamp()
            slot = state["slots"][task["slot_id"]]
            if slot.get("task_id") != task_id:
                raise SoloAIError(
                    "Slot ownership changed before integration preparation"
                )
            slot["status"] = "finishing"
            return copy.deepcopy(task)

        return self.mutate(update)

    def prepare_candidate_publication(
        self,
        task_id: str,
        *,
        operation_id: str,
        publication: dict[str, Any],
    ) -> dict[str, Any]:
        def update(state: dict[str, Any]) -> dict[str, Any]:
            task = state["tasks"].get(task_id)
            policy = task.get("integration_policy") if task else {}
            source_candidate = (
                isinstance(policy, dict)
                and policy.get("mode") == "batched"
                and policy.get("candidate_validation") == "batch"
            )
            allowed_statuses = {"ready", "publishing"}
            if source_candidate:
                allowed_statuses.add("active")
            if not task or task.get("status") not in allowed_statuses:
                raise SoloAIError("Only an eligible task can publish a candidate")
            active = task.get("active_operation") or {}
            if active.get("id") != operation_id or active.get("kind") != "finish":
                raise SoloAIError("Candidate publication lost its Finish operation")
            existing = task.get("candidate_publication")
            if existing and existing != publication:
                raise SoloAIError("A different candidate publication already exists")
            task["candidate_publication"] = copy.deepcopy(publication)
            task["status"] = "publishing"
            task["updated_at"] = utc_timestamp()
            slot = state["slots"][task["slot_id"]]
            if slot.get("task_id") != task_id:
                raise SoloAIError("Slot ownership changed before candidate publication")
            slot["status"] = "publishing"
            return copy.deepcopy(task)

        return self.mutate(update)

    def record_candidate_delivery_intent(
        self,
        task_id: str,
        *,
        operation_id: str,
        delivery_intent: dict[str, Any],
    ) -> dict[str, Any]:
        """为已准备的候选补录第一次交付意图，重试不得改写它。"""

        def update(state: dict[str, Any]) -> dict[str, Any]:
            task = state["tasks"].get(task_id)
            publication = task.get("candidate_publication") if task else None
            active = task.get("active_operation") if task else None
            policy = task.get("integration_policy") if task else None
            if (
                not isinstance(publication, dict)
                or task.get("status") != "publishing"
                or not isinstance(active, dict)
                or active.get("id") != operation_id
                or active.get("kind") != "finish"
                or not isinstance(policy, dict)
                or policy.get("mode") != "batched"
            ):
                raise SoloAIError(
                    "Candidate delivery intent requires a prepared batched Finish publication"
                )
            existing = publication.get("delivery_intent")
            if existing is not None and existing != delivery_intent:
                raise SoloAIError(
                    "Candidate delivery intent changed; recover with the originally recorded intent"
                )
            if existing is None:
                publication["schema_version"] = 3
                publication["delivery_intent"] = copy.deepcopy(delivery_intent)
                task["updated_at"] = utc_timestamp()
            return copy.deepcopy(task)

        return self.mutate(update)

    def complete_candidate_publication(
        self, task_id: str, *, candidate_id: str
    ) -> dict[str, Any]:
        def update(state: dict[str, Any]) -> dict[str, Any]:
            task = state["tasks"].get(task_id)
            publication = task.get("candidate_publication") if task else None
            if (
                not publication
                or publication.get("candidate_id") != candidate_id
                or task.get("status") not in {"publishing", "candidate-published"}
            ):
                raise SoloAIError("Candidate publication identity changed")
            slot = state["slots"][task["slot_id"]]
            if slot.get("task_id") not in {task_id, None}:
                raise SoloAIError("Candidate slot was reallocated before release")
            now = utc_timestamp()
            publication.update(
                {
                    "phase": "completed",
                    "completed_at": publication.get("completed_at") or now,
                }
            )
            task.update(
                {
                    "status": "candidate-published",
                    "lease": None,
                    "lease_owner": None,
                    "active_operation": None,
                    "updated_at": now,
                }
            )
            slot.update(
                {
                    "status": "idle",
                    "task_id": None,
                    "last_used": time.time(),
                    "quarantine_reason": None,
                    "released_worktree_identity": copy.deepcopy(
                        publication["worktree_identity"]
                    ),
                    "released_managed_root_identity": copy.deepcopy(
                        publication["managed_root_identity"]
                    ),
                    "released_worktree_resolved": publication["worktree_resolved"],
                    "released_managed_root_resolved": publication[
                        "managed_root_resolved"
                    ],
                }
            )
            return copy.deepcopy(task)

        return self.mutate(update)

    def record_candidate_runtime_release(
        self,
        task_id: str,
        *,
        candidate_id: str,
        receipt: dict[str, Any],
    ) -> dict[str, Any]:
        """把候选冻结后的成功 Adapter release 固化到既有发布事务。"""

        def update(state: dict[str, Any]) -> dict[str, Any]:
            task = state["tasks"].get(task_id)
            publication = task.get("candidate_publication") if task else None
            if (
                not publication
                or publication.get("candidate_id") != candidate_id
                or task.get("status") != "publishing"
                or receipt.get("operation") != "release"
                or receipt.get("result") != "passed"
            ):
                raise SoloAIError("Candidate Runtime Adapter release identity changed")
            existing = publication.get("runtime_release")
            if existing is not None and existing != receipt:
                raise SoloAIError("Candidate Runtime Adapter release receipt changed")
            publication["runtime_release"] = copy.deepcopy(receipt)
            task["updated_at"] = utc_timestamp()
            return copy.deepcopy(task)

        return self.mutate(update)

    def mark_integration_promoted(
        self, task_id: str, *, transaction_id: str, observed_base_head: str
    ) -> dict[str, Any]:
        def update(state: dict[str, Any]) -> dict[str, Any]:
            task = state["tasks"].get(task_id)
            integration = task.get("integration") if task else None
            if not integration or integration.get("transaction_id") != transaction_id:
                raise SoloAIError("Integration transaction identity changed")
            if integration.get("phase") not in {"prepared", "promoted"}:
                raise SoloAIError("Integration transaction cannot be promoted")
            integration.update(
                {
                    "phase": "promoted",
                    "base_head_observed": observed_base_head,
                    "promoted_at": integration.get("promoted_at") or utc_timestamp(),
                }
            )
            task["updated_at"] = utc_timestamp()
            return copy.deepcopy(task)

        return self.mutate(update)

    def cancel_prepared_integration(
        self, task_id: str, *, transaction_id: str
    ) -> dict[str, Any]:
        def update(state: dict[str, Any]) -> dict[str, Any]:
            task = state["tasks"].get(task_id)
            integration = task.get("integration") if task else None
            if not integration or integration.get("transaction_id") != transaction_id:
                raise SoloAIError("Integration transaction identity changed")
            if integration.get("phase") != "prepared":
                raise SoloAIError("Only an unpromoted integration can be cancelled")
            slot = state["slots"][task["slot_id"]]
            if slot.get("task_id") != task_id:
                raise SoloAIError("Slot ownership changed during integration recovery")
            task.update(
                {
                    "integration": None,
                    "status": "active",
                    "ready_proof": None,
                    "updated_at": utc_timestamp(),
                }
            )
            slot["status"] = "active"
            return copy.deepcopy(task)

        return self.mutate(update)

    def complete_integration(
        self, task_id: str, *, transaction_id: str
    ) -> dict[str, Any]:
        """记录集成终态，但保持槽位不可分配，直到外部现场终检完成。"""

        def update(state: dict[str, Any]) -> dict[str, Any]:
            task = state["tasks"].get(task_id)
            integration = task.get("integration") if task else None
            if not integration or integration.get("transaction_id") != transaction_id:
                raise SoloAIError("Integration transaction identity changed")
            if integration.get("phase") not in {"promoted", "completed"}:
                raise SoloAIError("Only a promoted integration can complete")
            slot = state["slots"][task["slot_id"]]
            if slot.get("task_id") != task_id:
                raise SoloAIError(
                    "Slot ownership changed before integration completion"
                )
            now = utc_timestamp()
            integration.update(
                {
                    "phase": "completed",
                    "completed_at": integration.get("completed_at") or now,
                    "cleanup": {
                        "worktree_detached": True,
                        "branch_deleted": True,
                        "slot_released": False,
                    },
                }
            )
            task.update(
                {
                    "status": "finished",
                    "lease": None,
                    "lease_owner": None,
                    "active_operation": None,
                    "updated_at": now,
                }
            )
            slot.update(
                {
                    "status": "release-checking",
                    "quarantine_reason": None,
                }
            )
            return copy.deepcopy(task)

        return self.mutate(update)

    def publish_integration_release(
        self, task_id: str, *, transaction_id: str
    ) -> dict[str, Any]:
        def update(state: dict[str, Any]) -> dict[str, Any]:
            task = state["tasks"].get(task_id)
            integration = task.get("integration") if task else None
            if (
                not task
                or task.get("status") != "finished"
                or not integration
                or integration.get("transaction_id") != transaction_id
                or integration.get("phase") != "completed"
            ):
                raise SoloAIError("Integration completion identity changed")
            slot = state["slots"][task["slot_id"]]
            if (
                slot.get("task_id") != task_id
                or slot.get("status") != "release-checking"
            ):
                raise SoloAIError("Integration slot release state changed")
            integration.setdefault("cleanup", {})["slot_released"] = True
            slot.update(
                {
                    "status": "idle",
                    "task_id": None,
                    "last_used": time.time(),
                    "quarantine_reason": None,
                    "released_worktree_identity": copy.deepcopy(
                        integration["worktree_identity"]
                    ),
                    "released_managed_root_identity": copy.deepcopy(
                        integration["managed_root_identity"]
                    ),
                    "released_worktree_resolved": integration["worktree_resolved"],
                    "released_managed_root_resolved": integration[
                        "managed_root_resolved"
                    ],
                }
            )
            return copy.deepcopy(task)

        return self.mutate(update)

    def migrate_legacy_integration(
        self,
        task_id: str,
        *,
        integration: dict[str, Any],
        completed: bool,
    ) -> dict[str, Any]:
        """把已严格核验的旧回执一次性写入新状态，不重放槽位释放。"""

        def update(state: dict[str, Any]) -> dict[str, Any]:
            task = state["tasks"].get(task_id)
            if not task:
                raise SoloAIError(f"Unknown task id: {task_id}")
            existing = task.get("integration")
            if existing:
                if existing != integration:
                    raise SoloAIError(
                        "A different integration transaction already exists"
                    )
                return copy.deepcopy(task)
            if completed:
                if task.get("status") != "finished":
                    raise SoloAIError(
                        "Only a finished legacy task can import completed integration"
                    )
                task["integration"] = copy.deepcopy(integration)
            else:
                if task.get("status") != "ready":
                    raise SoloAIError(
                        "Only a ready legacy task can import promoted integration"
                    )
                slot = state["slots"][task["slot_id"]]
                if slot.get("task_id") != task_id:
                    raise SoloAIError("Legacy integration slot ownership changed")
                task["integration"] = copy.deepcopy(integration)
                task["status"] = "finishing"
                slot["status"] = "finishing"
            task["updated_at"] = utc_timestamp()
            return copy.deepcopy(task)

        return self.mutate(update)

    def prepare_abandonment(
        self, task_id: str, *, operation_id: str, abandonment: dict[str, Any]
    ) -> dict[str, Any]:
        def update(state: dict[str, Any]) -> dict[str, Any]:
            task = state["tasks"].get(task_id)
            if not task or task.get("status") in FINAL_TASK_STATES:
                raise SoloAIError("Task cannot enter abandonment")
            if task.get("integration"):
                raise SoloAIError("An integration transaction blocks abandonment")
            active = task.get("active_operation") or {}
            if active.get("id") != operation_id or active.get("kind") != "abandon":
                raise SoloAIError("Abandonment lost its active operation")
            existing = task.get("abandonment")
            if existing and existing != abandonment:
                raise SoloAIError("A different abandonment transaction exists")
            slot = state["slots"][task["slot_id"]]
            if slot.get("task_id") != task_id:
                raise SoloAIError("Slot ownership changed before abandonment")
            task.update(
                {
                    "abandonment": copy.deepcopy(abandonment),
                    "status": "abandoning",
                    "updated_at": utc_timestamp(),
                }
            )
            slot["status"] = "abandoning"
            return copy.deepcopy(task)

        return self.mutate(update)

    def complete_abandonment(
        self, task_id: str, *, transaction_id: str
    ) -> dict[str, Any]:
        """记录业务终态，但保持槽位不可分配，直到外部现场终检完成。"""

        def update(state: dict[str, Any]) -> dict[str, Any]:
            task = state["tasks"].get(task_id)
            abandonment = task.get("abandonment") if task else None
            if not abandonment or abandonment.get("transaction_id") != transaction_id:
                raise SoloAIError("Abandonment transaction identity changed")
            slot = state["slots"][task["slot_id"]]
            if slot.get("task_id") != task_id:
                raise SoloAIError("Slot ownership changed before abandonment completed")
            now = utc_timestamp()
            abandonment.update(
                {
                    "phase": "completed",
                    "completed_at": abandonment.get("completed_at") or now,
                }
            )
            task.update(
                {
                    "status": "abandoned",
                    "lease": None,
                    "lease_owner": None,
                    "active_operation": None,
                    "updated_at": now,
                }
            )
            slot.update(
                {
                    "status": "release-checking",
                    "quarantine_reason": None,
                    "released_worktree_identity": copy.deepcopy(
                        abandonment["worktree_identity"]
                    ),
                    "released_managed_root_identity": copy.deepcopy(
                        abandonment["managed_root_identity"]
                    ),
                    "released_worktree_resolved": abandonment["worktree_resolved"],
                    "released_managed_root_resolved": abandonment[
                        "managed_root_resolved"
                    ],
                }
            )
            return copy.deepcopy(task)

        return self.mutate(update)

    def publish_abandonment_release(
        self, task_id: str, *, transaction_id: str
    ) -> dict[str, Any]:
        def update(state: dict[str, Any]) -> dict[str, Any]:
            task = state["tasks"].get(task_id)
            abandonment = task.get("abandonment") if task else None
            if (
                not task
                or task.get("status") != "abandoned"
                or not abandonment
                or abandonment.get("transaction_id") != transaction_id
                or abandonment.get("phase") != "completed"
            ):
                raise SoloAIError("Abandonment completion identity changed")
            slot = state["slots"][task["slot_id"]]
            if (
                slot.get("task_id") != task_id
                or slot.get("status") != "release-checking"
            ):
                raise SoloAIError("Abandonment slot release state changed")
            slot.update(
                {
                    "status": "idle",
                    "task_id": None,
                    "last_used": time.time(),
                    "quarantine_reason": None,
                }
            )
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
        try:
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
        except Exception:

            def rollback_begin(state: dict[str, Any]) -> None:
                current = state["tasks"].get(task_id)
                active = current.get("active_operation") if current else None
                if active and active.get("id") == operation_id:
                    current["active_operation"] = None
                    current["updated_at"] = utc_timestamp()

            self.mutate(rollback_begin)
            raise
        outcome = "succeeded"
        try:
            yield task
        except BaseException as exc:
            outcome = (
                "interrupted"
                if isinstance(exc, (KeyboardInterrupt, SystemExit))
                else "failed"
            )
            raise
        finally:
            finished_at = utc_timestamp()

            def end(state: dict[str, Any]) -> None:
                current = state["tasks"].get(task_id)
                active = current.get("active_operation") if current else None
                if active and active.get("id") == operation_id:
                    current["active_operation"] = None
                    current["updated_at"] = utc_timestamp()
                state.setdefault("pending_operation_outcomes", {})[operation_id] = {
                    "schema_version": 1,
                    "id": operation_id,
                    "task_id": task_id,
                    "kind": kind,
                    "status": outcome,
                    "finished_at": finished_at,
                }

            try:
                self.mutate(end)
            except Exception:
                current = self.task(task_id)
                if not (
                    current.get("status") in FINAL_TASK_STATES
                    and not current.get("active_operation")
                ):
                    raise
            receipt = read_json(receipt_path, {})
            receipt.update(
                {
                    "schema_version": 1,
                    "id": operation_id,
                    "task_id": task_id,
                    "kind": kind,
                    "status": outcome,
                    "finished_at": finished_at,
                }
            )
            receipt_written = False
            try:
                atomic_write_json(receipt_path, receipt)
                receipt_written = True
            except Exception:
                # state 已预写待补终态，后续 status/recover 会修复该投影。
                pass
            if receipt_written:

                def clear_pending(state: dict[str, Any]) -> None:
                    state.setdefault("pending_operation_outcomes", {}).pop(
                        operation_id, None
                    )

                try:
                    self.mutate(clear_pending)
                except Exception:
                    # 回执已是终态，遗留 pending 只会被后续 reconcile 幂等清掉。
                    pass

    def reconcile_operation_receipts(self) -> int:
        """把 state 中待补的操作终态投影回审计文件。"""
        state = self.read()
        pending_ids = set(state.get("pending_operation_outcomes", {}))
        for path in (self.repo.local_dir / "operations").glob("*.json"):
            receipt = read_json(path, {})
            if (
                receipt.get("status") != "running"
                or process_matches(receipt.get("owner", {}))
                or str(receipt.get("id") or path.stem) in pending_ids
            ):
                continue
            kind = receipt.get("kind")
            outcome = {
                "schema_version": 1,
                "id": receipt.get("id") or path.stem,
                "task_id": receipt.get("task_id"),
                "kind": kind,
                "status": "interrupted",
                "finished_at": utc_timestamp(),
                "recovered": True,
            }

            def record(state_value: dict[str, Any]) -> None:
                state_value.setdefault("pending_operation_outcomes", {})[
                    str(outcome["id"])
                ] = copy.deepcopy(outcome)

            self.mutate(record)
        state = self.read()
        pending = copy.deepcopy(state.get("pending_operation_outcomes", {}))
        completed: list[str] = []
        for operation_id, outcome in pending.items():
            path = self.repo.local_dir / "operations" / f"{operation_id}.json"
            receipt = read_json(path, {})
            receipt.update(outcome)
            atomic_write_json(path, receipt)
            completed.append(operation_id)
        if completed:

            def clear(state: dict[str, Any]) -> None:
                values = state.setdefault("pending_operation_outcomes", {})
                for operation_id in completed:
                    values.pop(operation_id, None)

            self.mutate(clear)
        return len(completed)

    @contextmanager
    def recovery_operation(self, task_id: str) -> Iterator[dict[str, Any]]:
        """发布无租约 Recover 操作，阻止开发进程与恢复并发修改同一任务。"""
        operation_id = uuid.uuid4().hex

        def begin(state: dict[str, Any]) -> dict[str, Any]:
            task = state["tasks"].get(task_id)
            if not task or task.get("status") in FINAL_TASK_STATES:
                raise SoloAIError(f"Task cannot be recovered: {task_id}")
            active = task.get("active_operation") or {}
            if active and process_matches(active.get("owner", {})):
                raise SoloAIError("Task still has a live operation; recovery is unsafe")
            task["active_operation"] = {
                "id": operation_id,
                "kind": "recover",
                "owner": process_snapshot(),
                "started_at": utc_timestamp(),
            }
            task["updated_at"] = utc_timestamp()
            return copy.deepcopy(task)

        task = self.mutate(begin)
        try:
            yield task
        finally:

            def end(state: dict[str, Any]) -> None:
                current = state["tasks"].get(task_id)
                active = current.get("active_operation") if current else None
                if active and active.get("id") == operation_id:
                    current["active_operation"] = None
                    current["updated_at"] = utc_timestamp()

            self.mutate(end)

    def quarantine(self, task_id: str, reason: str) -> None:
        def update(state: dict[str, Any]) -> None:
            task = state["tasks"][task_id]
            task["status"] = "quarantined"
            task["active_operation"] = None
            task["quarantine_reason"] = reason
            if self.mode(task) == ISOLATED_MODE:
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

    def quarantine_idle_slot_generation(
        self, slot_id: str, *, generation: int, reason: str
    ) -> None:
        def update(state: dict[str, Any]) -> None:
            slot = state["slots"].get(slot_id)
            if (
                not slot
                or slot.get("status") not in {"idle", "inactive"}
                or slot.get("task_id")
                or int(slot.get("generation", 0)) != generation
            ):
                raise SoloAIError("Cleanup slot generation changed before quarantine")
            slot.update({"status": "quarantined", "quarantine_reason": reason})

        self.mutate(update)

    def quarantine_released_slot(self, task_id: str, reason: str) -> None:
        """终态后发现晚到内容时保留任务终态，只隔离尚未重新分配的槽位。"""

        def update(state: dict[str, Any]) -> None:
            task = state["tasks"].get(task_id)
            if not task or task.get("status") not in FINAL_TASK_STATES:
                raise SoloAIError("Only a final task can quarantine its released slot")
            slot = state["slots"].get(task["slot_id"])
            if not slot or slot.get("task_id") not in {None, task_id}:
                raise SoloAIError(
                    "Released slot was already reallocated; preserve it manually"
                )
            slot.update({"status": "quarantined", "quarantine_reason": reason})

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
            task["updated_at"] = utc_timestamp()
            if self.mode(task) == ISOLATED_MODE:
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

    def recover(
        self, task_id: str, *, operation_id: str | None = None
    ) -> dict[str, Any]:
        def update(state: dict[str, Any]) -> dict[str, Any]:
            task = state["tasks"].get(task_id)
            if not task or task.get("status") in FINAL_TASK_STATES:
                raise SoloAIError(f"Task cannot be recovered: {task_id}")
            if self.mode(task) == IN_PLACE_MODE:
                raise SoloAIError(
                    "In-place tasks require resume-in-place; ordinary recovery cannot change their session binding"
                )
            active = task.get("active_operation")
            if (
                active
                and active.get("id") != operation_id
                and process_matches(active.get("owner", {}))
            ):
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
            if not operation_id:
                task["active_operation"] = None
            if task["status"] == "quarantined":
                raise SoloAIError(
                    "Quarantined tasks require doctor and manual repair; recover will not hide them"
                )
            task["status"] = "active"
            task["updated_at"] = utc_timestamp()
            return copy.deepcopy(task)

        return self.mutate(update)

    def handoff_isolated_task(
        self,
        task_id: str,
        *,
        operation_id: str,
        expected_branch: str,
        expected_head: str,
    ) -> dict[str, Any]:
        """在生命周期已复核脏工作树现场后，原子轮换隔离任务租约。"""

        def update(state: dict[str, Any]) -> dict[str, Any]:
            task = state["tasks"].get(task_id)
            if not task or self.mode(task) != ISOLATED_MODE:
                raise SoloAIError("Only isolated tasks can be handed off")
            if task.get("status") not in {"active", "ready"}:
                raise SoloAIError("Only active or ready tasks can be handed off")
            if (
                task.get("candidate_publication")
                or task.get("integration")
                or task.get("abandonment")
            ):
                raise SoloAIError(
                    "Published, finishing, or abandonment tasks cannot be handed off"
                )
            active = task.get("active_operation") or {}
            if active.get("id") != operation_id or active.get("kind") != "recover":
                raise SoloAIError("Task handoff lost its recovery operation")
            if (
                task.get("branch") != expected_branch
                or task.get("candidate_head") != expected_head
            ):
                raise SoloAIError("Task branch or HEAD changed before handoff")
            slot = state["slots"].get(str(task.get("slot_id")))
            if (
                not slot
                or slot.get("task_id") != task_id
                or slot.get("status") not in {"active", "ready"}
            ):
                raise SoloAIError("Task lost its managed slot before handoff")
            task.update(
                {
                    "lease": uuid.uuid4().hex,
                    "lease_owner": process_snapshot(),
                    "updated_at": utc_timestamp(),
                }
            )
            return copy.deepcopy(task)

        return self.mutate(update)

    def resume_in_place(self, task_id: str, *, session_id: str) -> dict[str, Any]:
        """在生命周期已复核 Git 身份后，仅轮换直改任务的会话与租约。"""
        if not session_id:
            raise SoloAIError("In-place resume requires a Codex session identifier")
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
                "Task still has a live validation process; resume is unsafe:\n"
                + "\n".join(f"- {path}" for path in live_runs)
            )
        for receipt_path in interrupted_runs:
            receipt = read_json(receipt_path, {})
            receipt.update({"status": "interrupted", "recovered_at": utc_timestamp()})
            atomic_write_json(receipt_path, receipt)

        def update(state: dict[str, Any]) -> dict[str, Any]:
            task = state["tasks"].get(task_id)
            if not task or self.mode(task) != IN_PLACE_MODE:
                raise SoloAIError(f"Task is not an in-place task: {task_id}")
            if task.get("status") in FINAL_TASK_STATES:
                raise SoloAIError(f"Task cannot be resumed: {task_id}")
            if task.get("status") not in {"active", "ready", "quarantined"}:
                raise SoloAIError(
                    "resume-in-place only transfers an active, ready, or quarantined in-place task"
                )
            active = task.get("active_operation")
            if active and process_matches(active.get("owner", {})):
                raise SoloAIError("Task still has a live operation; resume is unsafe")
            task.update(
                {
                    "status": "active",
                    "lease": uuid.uuid4().hex,
                    "lease_owner": process_snapshot(),
                    "session_fingerprint": sha256_text(session_id),
                    "active_operation": None,
                    "ready_proof": None,
                    "quarantine_reason": None,
                    "updated_at": utc_timestamp(),
                }
            )
            return copy.deepcopy(task)

        result = self.mutate(update)
        self.clear_guard_quarantine(task_id)
        return result

    @staticmethod
    def public_task(task: dict[str, Any]) -> dict[str, Any]:
        value = copy.deepcopy(task)
        value.pop("lease", None)
        value.pop("lease_owner", None)
        value.pop("session_fingerprint", None)
        active = value.get("active_operation")
        if active:
            active.pop("owner", None)
        return value
