from __future__ import annotations

import os
import hashlib
import re
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from statistics import median
from typing import Any

import psutil

from .util import (
    DirectoryLock,
    SoloAIError,
    atomic_write_json,
    new_id,
    process_matches,
    process_snapshot,
    read_json,
    stable_json,
    utc_timestamp,
)

SETTINGS_SCHEMA = 1
QUEUE_SCHEMA = 1
MAX_CAPACITY = 4
LIGHT_CAPACITY = 1
SLOW_VALIDATION_SECONDS = 10 * 60
INHERITED_CLAIM_ENV = "DWW_VALIDATION_PARENT_CLAIM"


def _machine_root() -> Path:
    """返回跨仓库共享、但只属于当前机器用户的运行目录。"""
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "develop-with-worktrees"
    if os.environ.get("XDG_STATE_HOME"):
        return Path(os.environ["XDG_STATE_HOME"]) / "develop-with-worktrees"
    return Path.home() / ".local" / "state" / "develop-with-worktrees"


def _settings_path() -> Path:
    return _machine_root() / "validation-settings.json"


def _metrics_path() -> Path:
    return _machine_root() / "validation-metrics.json"


def _queue_root() -> Path:
    return _machine_root() / "validation-queue"


def _queue_state_path() -> Path:
    return _queue_root() / "state.json"


def _ticket_root() -> Path:
    return _queue_root() / "tickets"


def _queue_lock() -> Path:
    return _queue_root() / "lock"


def _default_settings() -> dict[str, Any]:
    return {"schema_version": SETTINGS_SCHEMA, "validation_capacity": "auto"}


def get_settings() -> dict[str, Any]:
    settings = read_json(_settings_path(), _default_settings())
    if settings.get("schema_version") != SETTINGS_SCHEMA:
        raise SoloAIError("Unsupported local validation settings schema")
    mode = settings.get("validation_capacity", "auto")
    if mode != "auto" and (isinstance(mode, bool) or not isinstance(mode, int)):
        raise SoloAIError("Local validation_capacity must be auto or an integer")
    if isinstance(mode, int) and not 1 <= mode <= MAX_CAPACITY:
        raise SoloAIError(
            f"Local validation_capacity must be between 1 and {MAX_CAPACITY}"
        )
    return settings


def set_capacity(value: str) -> dict[str, Any]:
    normalized: str | int
    if value == "auto":
        normalized = "auto"
    else:
        try:
            normalized = int(value)
        except ValueError as exc:
            raise SoloAIError("validation capacity must be auto or an integer") from exc
        if not 1 <= normalized <= MAX_CAPACITY:
            raise SoloAIError(
                f"validation capacity must be between 1 and {MAX_CAPACITY}"
            )
    settings = {"schema_version": SETTINGS_SCHEMA, "validation_capacity": normalized}
    atomic_write_json(_settings_path(), settings)
    return queue_status()


def automatic_capacity() -> dict[str, Any]:
    """使用稳定硬件信息，避免随瞬时负载在任务间抖动。"""
    warnings: list[str] = []
    try:
        physical_cores = psutil.cpu_count(logical=False)
    except psutil.Error:
        physical_cores = None
    try:
        total_memory = psutil.virtual_memory().total
    except psutil.Error:
        total_memory = None
    if not physical_cores or not total_memory:
        warnings.append("无法读取物理核心或总内存，已保守使用 1 个验证容量")
        return {
            "capacity": 1,
            "reason": "hardware-detection-unavailable",
            "physical_cores": physical_cores,
            "total_memory_gib": None
            if total_memory is None
            else round(total_memory / 2**30, 2),
            "warnings": warnings,
        }
    memory_gib = total_memory / 2**30
    capacity = max(1, min(MAX_CAPACITY, int(physical_cores) // 4, int(memory_gib) // 8))
    return {
        "capacity": capacity,
        "reason": "min(floor(physical_cores/4), floor(total_memory_gib/8)), clamped to 1..4",
        "physical_cores": int(physical_cores),
        "total_memory_gib": round(memory_gib, 2),
        "warnings": warnings,
    }


def capacity_details() -> dict[str, Any]:
    settings = get_settings()
    detected = automatic_capacity()
    configured = settings["validation_capacity"]
    if configured == "auto":
        return {"mode": "auto", **detected}
    return {
        "mode": "fixed",
        "capacity": configured,
        "reason": "machine-local explicit setting",
        "physical_cores": detected["physical_cores"],
        "total_memory_gib": detected["total_memory_gib"],
        "warnings": detected["warnings"],
    }


def _default_queue_state() -> dict[str, Any]:
    return {"schema_version": QUEUE_SCHEMA, "active": {}}


def _live_validation_processes() -> list[int]:
    """检查可见的领取环境与 DWW 验证命令；不可见进程由人工确认兜底。"""
    live: list[int] = []
    for process in psutil.process_iter():
        if process.pid == os.getpid():
            continue
        try:
            environment = process.environ()
            command = [str(part).casefold() for part in process.cmdline()]
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            continue
        runner = any(part.endswith("dww.py") for part in command)
        if environment.get(INHERITED_CLAIM_ENV) or (
            runner
            and ("verify" in command or ("batch" in command and "recover" in command))
        ):
            live.append(process.pid)
    return live


def recover_zeroed_queue(
    *, expected_sha256: str, confirm_no_live_validation: bool
) -> dict[str, Any]:
    """保留全零损坏原件并在锁内恢复空队列；不接受普通 JSON 或未知票据。"""
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise SoloAIError("Queue recovery requires an exact lowercase SHA-256")
    if not confirm_no_live_validation:
        raise SoloAIError(
            "Queue recovery requires reviewed no-live-validation confirmation"
        )
    state_path = _queue_state_path()
    with DirectoryLock(_queue_lock(), wait=True):
        if state_path.is_symlink() or not state_path.exists():
            raise SoloAIError("Queue recovery requires one existing plain state file")
        if not stat.S_ISREG(state_path.lstat().st_mode):
            raise SoloAIError("Queue recovery state path is not a plain file")
        damaged = state_path.read_bytes()
        digest = hashlib.sha256(damaged).hexdigest()
        if (
            digest != expected_sha256
            or not damaged
            or len(damaged) > 1024 * 1024
            or any(damaged)
        ):
            raise SoloAIError("Queue recovery accepts only the exact zeroed state")
        ticket_root = _ticket_root()
        tickets: list[str] = []
        if ticket_root.exists():
            if ticket_root.is_symlink() or not ticket_root.is_dir():
                raise SoloAIError("Queue recovery ticket root changed")
            for path in ticket_root.iterdir():
                if path.is_symlink() or not path.is_file() or path.suffix != ".json":
                    raise SoloAIError("Queue recovery found unknown ticket content")
                ticket = _read_ticket(path)
                if ticket is None or ticket["id"] != path.stem:
                    raise SoloAIError("Queue recovery found an unreadable ticket")
                if process_matches(ticket.get("owner", {})):
                    raise SoloAIError("Queue recovery found a live ticket owner")
                tickets.append(ticket["id"])
        live = _live_validation_processes()
        if live:
            raise SoloAIError("Queue recovery found live validation processes")
        recovery_root = _queue_root() / "recovery"
        if recovery_root.is_symlink():
            raise SoloAIError("Queue recovery audit directory is linked")
        recovery_root.mkdir(exist_ok=True)
        backup = recovery_root / f"{digest}.state.bin"
        if backup.exists():
            if backup.is_symlink() or backup.read_bytes() != damaged:
                raise SoloAIError("Queue recovery audit backup changed")
        else:
            with backup.open("xb") as handle:
                handle.write(damaged)
                handle.flush()
                os.fsync(handle.fileno())
        receipt = recovery_root / f"{digest}.json"
        audit = {
            "schema_version": 1,
            "kind": "zeroed-queue-recovery",
            "damaged_sha256": digest,
            "damaged_bytes": len(damaged),
            "stale_ticket_ids": sorted(tickets),
            "prepared_at": utc_timestamp(),
            "status": "prepared",
        }
        atomic_write_json(receipt, audit)
        atomic_write_json(state_path, _default_queue_state())
        atomic_write_json(
            receipt, {**audit, "status": "recovered", "recovered_at": utc_timestamp()}
        )
    return {
        "recovered": True,
        "damaged_sha256": digest,
        "backup": str(backup),
        "receipt": str(receipt),
        "stale_ticket_count": len(tickets),
    }


def _read_ticket(path: Path) -> dict[str, Any] | None:
    ticket = read_json(path, {})
    if (
        ticket.get("schema_version") != QUEUE_SCHEMA
        or not isinstance(ticket.get("id"), str)
        or not isinstance(ticket.get("created_monotonic"), (int, float))
    ):
        return None
    return ticket


def _cleanup_stale_locked(state: dict[str, Any]) -> list[dict[str, Any]]:
    active = state.setdefault("active", {})
    for ticket_id, claim in list(active.items()):
        if not process_matches(claim.get("owner", {})):
            active.pop(ticket_id, None)
    waiting: list[dict[str, Any]] = []
    root = _ticket_root()
    if root.exists():
        for path in root.glob("*.json"):
            ticket = _read_ticket(path)
            if ticket is None or not process_matches(ticket.get("owner", {})):
                path.unlink(missing_ok=True)
                continue
            waiting.append(ticket)
    return sorted(waiting, key=lambda item: (item["created_monotonic"], item["id"]))


def _active_units(active: dict[str, Any]) -> int:
    return sum(int(item.get("units", 0)) for item in active.values())


def _active_units_for(active: dict[str, Any], *, resource_class: str) -> int:
    return sum(
        int(item.get("units", 0))
        for item in active.values()
        if item.get("resource_class") == resource_class
    )


def inherited_claim_environment(claim: dict[str, Any]) -> dict[str, str]:
    """把当前活动票据显式传给其受控子进程。"""

    return {INHERITED_CLAIM_ENV: str(claim["id"])}


def _is_current_process_descendant(owner: dict[str, Any]) -> bool:
    """仅凭活的 OS 进程祖先链确认票据继承关系。"""

    if not process_matches(owner):
        return False
    try:
        current = psutil.Process()
        lineage = [current, *current.parents()]
    except (psutil.Error, OSError):
        return False
    owner_pid = owner.get("pid")
    return any(process.pid == owner_pid for process in lineage)


def _inherited_claim(
    state: dict[str, Any], *, resource_class: str
) -> dict[str, Any] | None:
    claim_id = os.environ.get(INHERITED_CLAIM_ENV)
    if not claim_id:
        return None
    claim = state.setdefault("active", {}).get(claim_id)
    if not claim or not _is_current_process_descendant(claim.get("owner", {})):
        return None
    if claim.get("resource_class") != "heavy" and resource_class == "heavy":
        raise SoloAIError(
            "Nested heavy validation cannot inherit a normal parent claim; declare the outer validation heavy"
        )
    return {
        **claim,
        "units": 0,
        "wait_seconds": 0.0,
        "inherited": True,
        "inherited_from": claim_id,
    }


def queue_status() -> dict[str, Any]:
    """读取全机队列，不会占用任何仓库的生命周期锁。"""
    with DirectoryLock(_queue_lock(), wait=True):
        state = read_json(_queue_state_path(), _default_queue_state())
        if state.get("schema_version") != QUEUE_SCHEMA:
            raise SoloAIError("Unsupported machine validation queue schema")
        waiting = _cleanup_stale_locked(state)
        atomic_write_json(_queue_state_path(), state)
    details = capacity_details()
    active = list(state["active"].values())
    return {
        "scope": "machine-global",
        "capacity": details,
        "active_units": _active_units(state["active"]),
        "active_light_units": _active_units_for(
            state["active"], resource_class="light"
        ),
        "active": [
            {
                "id": item["id"],
                "resource_class": item["resource_class"],
                "units": item["units"],
                "acquired_at": item["acquired_at"],
            }
            for item in active
        ],
        "waiting": [
            {
                "id": item["id"],
                "resource_class": item["resource_class"],
                "queued_at": item["queued_at"],
            }
            for item in waiting
        ],
    }


def queue_ticket_snapshot(ticket_id: str) -> dict[str, Any] | None:
    """只读确认一个精确票据仍在等待或占用资源，不清理任何队列状态。"""
    if not ticket_id:
        return None
    state = read_json(_queue_state_path(), _default_queue_state())
    if state.get("schema_version") != QUEUE_SCHEMA:
        raise SoloAIError("Unsupported machine validation queue schema")
    active = state.get("active", {}).get(ticket_id)
    if isinstance(active, dict) and process_matches(active.get("owner", {})):
        return {
            "state": "active",
            "resource_class": active.get("resource_class"),
            "queued_at": active.get("queued_at"),
            "acquired_at": active.get("acquired_at"),
            "wait_seconds": active.get("wait_seconds"),
        }
    ticket = _read_ticket(_ticket_root() / f"{ticket_id}.json")
    if ticket and process_matches(ticket.get("owner", {})):
        return {
            "state": "waiting",
            "resource_class": ticket.get("resource_class"),
            "queued_at": ticket.get("queued_at"),
        }
    return None


@contextmanager
def claim_validation_slot(resource_class: str) -> Iterator[dict[str, Any]]:
    """按全机 FIFO 领取验证资源；等待期间不持有仓库状态锁。"""
    if resource_class not in {"light", "normal", "heavy"}:
        raise SoloAIError("Validation resource_class must be light, normal, or heavy")
    with DirectoryLock(_queue_lock(), wait=True):
        state = read_json(_queue_state_path(), _default_queue_state())
        if state.get("schema_version") != QUEUE_SCHEMA:
            raise SoloAIError("Unsupported machine validation queue schema")
        _cleanup_stale_locked(state)
        inherited = _inherited_claim(state, resource_class=resource_class)
        atomic_write_json(_queue_state_path(), state)
    if inherited is not None:
        yield inherited
        return
    ticket_id = new_id("validation")
    ticket = {
        "schema_version": QUEUE_SCHEMA,
        "id": ticket_id,
        "resource_class": resource_class,
        "owner": process_snapshot(),
        "queued_at": utc_timestamp(),
        "created_monotonic": time.monotonic(),
    }
    ticket_path = _ticket_root() / f"{ticket_id}.json"
    atomic_write_json(ticket_path, ticket)
    claim: dict[str, Any] | None = None
    try:
        while claim is None:
            with DirectoryLock(_queue_lock(), wait=True):
                state = read_json(_queue_state_path(), _default_queue_state())
                if state.get("schema_version") != QUEUE_SCHEMA:
                    raise SoloAIError("Unsupported machine validation queue schema")
                waiting = _cleanup_stale_locked(state)
                details = capacity_details()
                capacity = int(details["capacity"])
                first_non_light = next(
                    (item for item in waiting if item["resource_class"] != "light"),
                    None,
                )
                first_light = next(
                    (item for item in waiting if item["resource_class"] == "light"),
                    None,
                )
                active = state.setdefault("active", {})
                units = capacity if resource_class == "heavy" else 1
                if resource_class == "light":
                    can_acquire = (
                        first_light is not None
                        and first_light["id"] == ticket_id
                        and _active_units_for(active, resource_class="light")
                        < LIGHT_CAPACITY
                    )
                else:
                    # 普通与重型检查继续共享容量并保持 FIFO；轻量检查走独立名额。
                    main_units = _active_units(active) - _active_units_for(
                        active, resource_class="light"
                    )
                    can_acquire = (
                        first_non_light is not None
                        and first_non_light["id"] == ticket_id
                        and main_units + units <= capacity
                    )
                if can_acquire:
                    claim = {
                        **ticket,
                        "units": units,
                        "acquired_at": utc_timestamp(),
                        "wait_seconds": round(
                            time.monotonic() - ticket["created_monotonic"], 3
                        ),
                    }
                    active[ticket_id] = claim
                    ticket_path.unlink(missing_ok=True)
                atomic_write_json(_queue_state_path(), state)
            if claim is None:
                time.sleep(0.2)
        yield claim
    finally:
        ticket_path.unlink(missing_ok=True)
        with DirectoryLock(_queue_lock(), wait=True):
            state = read_json(_queue_state_path(), _default_queue_state())
            if state.get("schema_version") == QUEUE_SCHEMA:
                state.setdefault("active", {}).pop(ticket_id, None)
                _cleanup_stale_locked(state)
                atomic_write_json(_queue_state_path(), state)


def _metric_key(profile_id: str, command_digests: list[str]) -> str:
    return stable_json({"profile_id": profile_id, "commands": command_digests})


def record_profile_duration(
    *, profile_id: str, command_digests: list[str], duration_seconds: float
) -> None:
    if duration_seconds < 0:
        return
    path = _metrics_path()
    metrics = read_json(path, {"schema_version": 1, "profiles": {}})
    if metrics.get("schema_version") != 1:
        return
    key = _metric_key(profile_id, command_digests)
    values = metrics.setdefault("profiles", {}).setdefault(key, [])
    values.append(round(float(duration_seconds), 3))
    metrics["profiles"][key] = values[-20:]
    atomic_write_json(path, metrics)


def estimated_profile_seconds(
    *, profile_id: str, command_digests: list[str]
) -> float | None:
    metrics = read_json(_metrics_path(), {"schema_version": 1, "profiles": {}})
    if metrics.get("schema_version") != 1:
        return None
    values = metrics.get("profiles", {}).get(
        _metric_key(profile_id, command_digests), []
    )
    numeric = [float(value) for value in values if isinstance(value, (int, float))]
    return round(float(median(numeric)), 3) if numeric else None


def estimate_validation(records: list[tuple[str, list[str]]]) -> dict[str, Any]:
    estimates = [
        estimated_profile_seconds(profile_id=profile_id, command_digests=digests)
        for profile_id, digests in records
    ]
    known = [value for value in estimates if value is not None]
    total = round(sum(known), 3) if len(known) == len(estimates) else None
    advisory = None
    if total is not None and total > SLOW_VALIDATION_SECONDS:
        advisory = (
            "预计验证超过 10 分钟；请评估是否拆分路径映射或减少重复准备，"
            "但不要因此自动降低覆盖范围。"
        )
    return {
        "profile_seconds": estimates,
        "estimated_seconds": total,
        "advisory": advisory,
    }
