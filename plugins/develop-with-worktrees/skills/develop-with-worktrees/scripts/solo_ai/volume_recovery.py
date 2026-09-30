"""克隆磁盘后显式重绑当前资源身份，不改变代码、任务状态或历史证据。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .cleanup import require_managed_directory_identity
from .config import load_repo_config
from .repo import GitRepo
from .state import FINAL_TASK_STATES, STATE_SCHEMA, StateStore, candidate_admission_lock
from .util import (
    DirectoryLock,
    SoloAIError,
    atomic_write_json,
    path_identity,
    read_json,
    sha256_text,
    stable_json,
    utc_timestamp,
)
from .validation_queue import QUEUE_SCHEMA, _queue_state_path, _ticket_root


def _queue_snapshot() -> dict[str, Any]:
    """保守读取队列，不在恢复预览中清理票据或重写机器状态。"""
    state = read_json(
        _queue_state_path(), {"schema_version": QUEUE_SCHEMA, "active": {}}
    )
    if state.get("schema_version") != QUEUE_SCHEMA:
        raise SoloAIError("Unsupported machine validation queue schema")
    return {
        "active": state.get("active"),
        "waiting": list(_ticket_root().glob("*.json")),
    }


def _inspect(
    repo: GitRepo, state: dict[str, Any]
) -> tuple[dict[str, Any], list[tuple[dict[str, Any], str, dict[str, Any]]]]:
    if state["schema_version"] != STATE_SCHEMA:
        raise SoloAIError("Volume recovery requires native state schema 12")
    queue = _queue_snapshot()
    if queue.get("active") or queue.get("waiting"):
        raise SoloAIError("Stop live validation before volume recovery")
    if any(
        batch.get("status") != "completed"
        for batch in state.get("batches", {}).values()
    ):
        raise SoloAIError("Unfinished native batches block volume recovery")
    config = load_repo_config(repo)
    root = StateStore(repo).managed_worktree_root(config).absolute()
    registered = {item.path: item for item in repo.worktrees()}
    changes: list[dict[str, Any]] = []
    replacements: list[tuple[dict[str, Any], str, dict[str, Any]]] = []
    devices: set[tuple[int, int]] = set()
    facts: dict[str, Any] = {}
    seen_fields: set[tuple[int, str]] = set()

    def identity(
        record: dict[str, Any], key: str, resolved_key: str, path: Path, label: str
    ) -> None:
        field = (id(record), key)
        if field in seen_fields:
            return
        seen_fields.add(field)
        expected = record.get(key)
        if not isinstance(expected, dict) or record.get(resolved_key) != str(
            path.resolve()
        ):
            raise SoloAIError(f"Missing or changed recorded path: {label}.{key}")
        current = path_identity(path)
        # 磁盘编号之外的所有字段必须原样相等；绝不接受重新创建的目录。
        if (
            set(expected) != set(current)
            or any(expected[k] != current[k] for k in current if k != "device")
            or not isinstance(expected.get("device"), int)
        ):
            raise SoloAIError(f"Directory object changed beyond its volume: {path}")
        facts[f"identity:{path}"] = current
        if expected["device"] != current["device"]:
            devices.add((expected["device"], current["device"]))
            changes.append({"record": label, "field": key, "path": str(path)})
            replacements.append((record, key, current))

    def workspace(path: Path) -> None:
        require_managed_directory_identity(path, managed_root=root)
        if (
            path not in registered
            or GitRepo(path).common_dir != repo.common_dir
            or not repo.is_clean(path)
        ):
            raise SoloAIError(f"Unregistered or dirty worktree: {path}")
        facts[str(path)] = {
            "identity": path_identity(path),
            "head": repo.head(path),
            "branch": repo.branch(path),
        }

    for task in state["tasks"].values():
        if task.get("status") not in FINAL_TASK_STATES and (
            task.get("active_operation") is not None or task.get("processes")
        ):
            raise SoloAIError("Running task operation blocks volume recovery")

    for slot_id, slot in sorted(state["slots"].items()):
        path = Path(slot["path"]).absolute()
        if path.parent != root:
            raise SoloAIError("Slot escaped the configured managed root")
        workspace(path)
        label = f"slots.{slot_id}"
        identity(
            slot,
            "released_worktree_identity",
            "released_worktree_resolved",
            path,
            label,
        )
        identity(
            slot,
            "released_managed_root_identity",
            "released_managed_root_resolved",
            root,
            label,
        )
        ids = {slot.get("task_id"), slot.get("released_task_id")} - {None}
        for task_id in sorted(ids):
            task = state["tasks"].get(task_id)
            if not task or Path(task["worktree"]).absolute() != path:
                raise SoloAIError("Slot task or released predecessor changed")
            label = f"tasks.{task_id}"
            identity(
                task, "slot_worktree_identity", "slot_worktree_resolved", path, label
            )
            identity(
                task,
                "slot_managed_root_identity",
                "slot_managed_root_resolved",
                root,
                label,
            )
            base = Path(task["base_worktree"]).absolute()
            if (
                base not in registered
                or GitRepo(base).common_dir != repo.common_dir
                or not repo.is_clean(base)
            ):
                raise SoloAIError("Recorded base worktree is missing or dirty")
            require_managed_directory_identity(base, managed_root=base)
            identity(
                task, "base_worktree_identity", "base_worktree_resolved", base, label
            )
            facts[f"base:{base}"] = {
                "head": repo.head(base),
                "branch": repo.branch(base),
            }
            if slot.get("task_id") == task_id:
                if task.get("slot_generation") != slot.get("generation"):
                    raise SoloAIError("Current task slot generation changed")
                head = task.get("candidate_head")
                if head is not None:
                    if (
                        repo.head(path) != head
                        or repo.branch(path) != task["branch"]
                        or repo.ref_head(f"refs/heads/{task['branch']}") != head
                    ):
                        raise SoloAIError("Current task branch or HEAD changed")
                elif (
                    task.get("status") != "quarantined"
                    or task.get("quarantine_reason")
                    != "Managed root directory object was replaced"
                    or repo.branch(path) is not None
                    or repo.ref_head(f"refs/heads/{task['branch']}") is not None
                    or task.get("runtime_activation")
                    or (repo.local_dir / "task-anchors" / f"{task_id}.md").exists()
                    or not repo.is_ancestor(repo.head(path), task["base_head"])
                ):
                    raise SoloAIError("Ambiguous pre-activation task")
            abandonment = task.get("abandonment")
            if abandonment:
                if abandonment.get("phase") != "completed":
                    raise SoloAIError("Unfinished abandonment blocks volume recovery")
                for key, resolved_key, target in (
                    ("worktree_identity", "worktree_resolved", path),
                    ("managed_root_identity", "managed_root_resolved", root),
                ):
                    if key in abandonment:
                        identity(
                            abandonment,
                            key,
                            resolved_key,
                            target,
                            label + ".abandonment",
                        )

    integration = state.get("integration_workspace")
    if integration:
        if integration.get("owner") is not None or integration.get("registering"):
            raise SoloAIError("Owned integration worktree blocks volume recovery")
        path = Path(integration["worktree"]).absolute()
        workspace(path)
        if (
            repo.head(path) != integration["head"]
            or repo.branch(path) is not None
            or repo.ref_head(integration["head_ref"]) != integration["head"]
        ):
            raise SoloAIError("Integration worktree Git identity changed")
        identity(
            integration,
            "worktree_identity",
            "worktree_resolved",
            path,
            "integration_workspace",
        )
        identity(
            integration,
            "managed_root_identity",
            "managed_root_resolved",
            root,
            "integration_workspace",
        )
    if len(devices) > 1:
        raise SoloAIError("More than one volume transition; preserve the scene")
    digest = sha256_text(
        stable_json({"state": state, "facts": facts, "changes": changes})
    )
    return {
        "confirmation": digest if changes else None,
        "observed": digest,
        "volume_transition": list(next(iter(devices))) if devices else None,
        "changes": changes,
        "worktrees_preserved": True,
    }, replacements


def recover_volume(
    repo: GitRepo, *, confirm: str | None = None, confirm_no_live_work: bool = False
) -> dict[str, Any]:
    """预览只读；确认时在已有锁内重核 Git 和目录事实后原子保存状态。"""
    store = StateStore(repo)
    if confirm is None:
        plan, _ = _inspect(repo, store.read())
        return {"changed": False, **plan}
    if not confirm_no_live_work:
        raise SoloAIError("Confirm stopped host work before applying volume recovery")

    if not re.fullmatch(r"[0-9a-f]{64}", confirm):
        raise SoloAIError("Invalid volume recovery confirmation")
    backup = repo.local_dir / "volume-recovery" / f"{confirm}.before.json"
    receipt = repo.local_dir / "volume-recovery" / f"{confirm}.receipt.json"
    with candidate_admission_lock(repo), DirectoryLock(store.lock_path):
        state = store.read()
        plan, replacements = _inspect(repo, state)
        if receipt.exists():
            prior = json.loads(receipt.read_text(encoding="utf-8"))
            if (
                prior.get("confirmation") == confirm
                and prior.get("after_confirmation") == plan["observed"]
                and backup.is_file()
                and prior.get("before_sha256")
                == sha256_text(
                    stable_json(json.loads(backup.read_text(encoding="utf-8")))
                )
            ):
                completed = {**prior, "status": "completed"}
                atomic_write_json(receipt, completed)
                return {**completed, "changed": False, "already_applied": True}
        if confirm != plan["confirmation"]:
            raise SoloAIError("Volume recovery preview changed; inspect again")
        if backup.exists():
            if json.loads(backup.read_text(encoding="utf-8")) != state:
                raise SoloAIError(
                    "Volume recovery backup differs; preserve its receipt"
                )
        else:
            atomic_write_json(backup, state)
        before_sha256 = sha256_text(stable_json(state))
        for record, key, current in replacements:
            record[key] = current
        state["updated_at"] = utc_timestamp()
        after, _ = _inspect(repo, state)
        result = {
            "changed": True,
            **plan,
            "backup": str(backup),
            "receipt": str(receipt),
            "before_sha256": before_sha256,
            "after_confirmation": after["observed"],
            "status": "prepared",
        }
        atomic_write_json(receipt, result)
        atomic_write_json(store.path, state)
        result["status"] = "completed"
        atomic_write_json(receipt, result)
        return result
