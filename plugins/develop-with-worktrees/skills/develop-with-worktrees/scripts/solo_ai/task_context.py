from __future__ import annotations

from pathlib import Path
from typing import Any

from .repo import GitRepo
from .util import SoloAIError, atomic_write_text, is_link_or_junction, utc_timestamp

MAX_ANCHOR_BYTES = 64 * 1024


def anchor_path(repo: GitRepo, task_id: str) -> Path:
    return repo.local_dir / "task-anchors" / f"{task_id}.md"


def _require_plain_anchor(path: Path) -> str:
    if not path.exists():
        raise SoloAIError(f"Task anchor is missing: {path}. Restore it before Ready.")
    if is_link_or_junction(path) or not path.is_file():
        raise SoloAIError("Task anchor must be a regular local UTF-8 file")
    if path.stat().st_size > MAX_ANCHOR_BYTES:
        raise SoloAIError("Task anchor exceeds the 64 KiB safety limit")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise SoloAIError("Task anchor must be valid UTF-8") from exc


def create_anchor(repo: GitRepo, task: dict[str, Any]) -> Path:
    path = anchor_path(repo, str(task["id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    if is_link_or_junction(path.parent) or not path.parent.is_dir():
        raise SoloAIError("Task anchor directory is not a plain local directory")
    if path.exists():
        require_anchor(repo, task)
        return path
    content = f"""# Task anchor: {task["name"]}

- Task ID: `{task["id"]}`
- Original purpose: {task["name"]}
- Implementation target: fill before editing
- Reference baseline: `{task.get("base_ref")}` at `{task.get("base_head")}`
- Scope boundary: fill before editing
- Acceptance criteria: fill before Ready
- Current progress: task started at {utc_timestamp()}

This local file is not committed. Keep it current, and reread it after context loss or continuation.
"""
    atomic_write_text(path, content)
    require_anchor(repo, task)
    return path


def require_anchor(repo: GitRepo, task: dict[str, Any]) -> Path:
    path = anchor_path(repo, str(task["id"]))
    content = _require_plain_anchor(path)
    if f"`{task['id']}`" not in content:
        raise SoloAIError("Task anchor identity does not match the active task")
    return path


def delete_anchor(repo: GitRepo, task_id: str) -> None:
    path = anchor_path(repo, task_id)
    if not path.exists():
        return
    _require_plain_anchor(path)
    path.unlink()


def list_anchors(repo: GitRepo) -> list[dict[str, Any]]:
    root = repo.local_dir / "task-anchors"
    if not root.exists():
        return []
    if is_link_or_junction(root) or not root.is_dir():
        raise SoloAIError("Task anchor directory is not a plain local directory")
    result: list[dict[str, Any]] = []
    for path in sorted(root.glob("task-*.md")):
        _require_plain_anchor(path)
        result.append(
            {
                "task_id": path.stem,
                "path": str(path.resolve()),
                "bytes": path.stat().st_size,
            }
        )
    return result
