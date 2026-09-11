from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from .repo import GitRepo
from .util import SoloAIError, atomic_write_text, is_link_or_junction, utc_timestamp

MAX_ANCHOR_BYTES = 64 * 1024
_ANCHOR_FIELDS = (
    "Task ID",
    "Original purpose",
    "Implementation target",
    "Reference baseline",
    "Scope boundary",
    "Acceptance criteria",
    "Current progress",
)
_IMMUTABLE_FIELDS = ("Task ID", "Original purpose", "Reference baseline")


def anchor_path(repo: GitRepo, task_id: str) -> Path:
    if not task_id or any(char in task_id for char in "/\\:"):
        raise SoloAIError("Task id is not a safe anchor name")
    return repo.local_dir / "task-anchors" / f"{task_id}.md"


def _require_plain_anchor(path: Path) -> str:
    return _read_plain_anchor(path)[1]


def _read_plain_anchor(path: Path) -> tuple[bytes, str]:
    if not path.exists():
        raise SoloAIError(f"Task anchor is missing: {path}. Restore it before Ready.")
    if is_link_or_junction(path) or not path.is_file():
        raise SoloAIError("Task anchor must be a regular local UTF-8 file")
    raw = path.read_bytes()
    if len(raw) > MAX_ANCHOR_BYTES:
        raise SoloAIError("Task anchor exceeds the 64 KiB safety limit")
    try:
        return raw, raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SoloAIError("Task anchor must be valid UTF-8") from exc


def _read_plain_input(path: Path) -> str:
    if not path.exists():
        raise SoloAIError(f"Anchor update input is missing: {path}")
    if is_link_or_junction(path) or not path.is_file():
        raise SoloAIError("Anchor update input must be a regular local UTF-8 file")
    raw = path.read_bytes()
    if len(raw) > MAX_ANCHOR_BYTES:
        raise SoloAIError("Anchor update input exceeds the 64 KiB safety limit")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SoloAIError("Anchor update input must be valid UTF-8") from exc


def _field_values(content: str, field: str) -> list[str]:
    pattern = re.compile(rf"^- {re.escape(field)}:([^\r\n]*)$")
    values: list[str] = []
    in_fence = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if not in_fence:
            match = pattern.fullmatch(line)
            if match:
                values.append(match.group(1).strip())
    return values


def _identity_value(value: str) -> str:
    return value.strip().removeprefix("`").removesuffix("`")


def _validated_fields(content: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for field in _ANCHOR_FIELDS:
        values = _field_values(content, field)
        if len(values) != 1:
            raise SoloAIError(f"Task anchor must contain exactly one '{field}' field")
        fields[field] = values[0]
    return fields


def _validate_update_content(
    content: str,
    *,
    task_id: str,
    previous: dict[str, str],
    progress_only: bool,
) -> dict[str, str]:
    if len(content.encode("utf-8")) > MAX_ANCHOR_BYTES:
        raise SoloAIError("Task anchor exceeds the 64 KiB safety limit")
    fields = _validated_fields(content)
    if _identity_value(fields["Task ID"]) != task_id:
        raise SoloAIError("Task anchor identity does not match the active task")
    for field in _IMMUTABLE_FIELDS:
        if fields[field] != previous[field]:
            raise SoloAIError(f"Task anchor field cannot be changed: {field}")
    for field in (
        "Implementation target",
        "Scope boundary",
        "Acceptance criteria",
        "Current progress",
    ):
        if not fields[field]:
            raise SoloAIError(f"Task anchor field cannot be empty: {field}")
    for field in ("Implementation target", "Scope boundary", "Acceptance criteria"):
        if fields[field].lower().startswith("fill before"):
            raise SoloAIError(
                f"Task anchor field is still a template placeholder: {field}"
            )
    if progress_only:
        for field in (
            "Implementation target",
            "Scope boundary",
            "Acceptance criteria",
        ):
            if fields[field] != previous[field]:
                raise SoloAIError(
                    "A ready task may update only Current progress in its anchor"
                )
    return fields


def read_anchor(repo: GitRepo, task: dict[str, Any]) -> dict[str, Any]:
    path = anchor_path(repo, str(task["id"]))
    raw, content = _read_plain_anchor(path)
    fields = _validated_fields(content)
    if _identity_value(fields["Task ID"]) != str(task["id"]):
        raise SoloAIError("Task anchor identity does not match the active task")
    return {
        "task_id": str(task["id"]),
        "anchor_path": str(path.resolve()),
        "content": content,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def read_anchor_update(path: Path) -> str:
    return _read_plain_input(path)


def update_anchor(
    repo: GitRepo,
    task: dict[str, Any],
    *,
    content: str,
    expected_sha256: str,
    progress_only: bool = False,
) -> dict[str, Any]:
    path = anchor_path(repo, str(task["id"]))
    raw, previous_content = _read_plain_anchor(path)
    current_sha256 = hashlib.sha256(raw).hexdigest()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256 or ""):
        raise SoloAIError(
            "Expected anchor SHA-256 must be 64 lowercase hexadecimal characters"
        )
    if current_sha256 != expected_sha256:
        raise SoloAIError(
            "Task anchor changed since it was read; fetch it again before updating "
            f"(current sha256: {current_sha256})"
        )
    previous = _validated_fields(previous_content)
    _validate_update_content(
        content,
        task_id=str(task["id"]),
        previous=previous,
        progress_only=progress_only,
    )
    new_raw = content.encode("utf-8")
    new_sha256 = hashlib.sha256(new_raw).hexdigest()
    if new_sha256 == current_sha256:
        return {
            "task_id": str(task["id"]),
            "anchor_path": str(path.resolve()),
            "sha256": current_sha256,
            "changed": False,
        }
    atomic_write_text(path, content)
    _read_plain_anchor(path)
    return {
        "task_id": str(task["id"]),
        "anchor_path": str(path.resolve()),
        "sha256": new_sha256,
        "changed": True,
    }


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


def adopt_legacy_anchor(
    repo: GitRepo,
    task: dict[str, Any],
    *,
    objective: str,
    target: str,
    scope: str,
    acceptance: str,
    confirm: str,
) -> Path:
    """Create a reviewed anchor for a pre-anchor task without inventing intent."""

    if confirm != str(task["id"]):
        raise SoloAIError("Legacy anchor adoption confirmation must equal the task id")
    fields = {
        "objective": objective.strip(),
        "target": target.strip(),
        "scope": scope.strip(),
        "acceptance": acceptance.strip(),
    }
    if any(not value for value in fields.values()):
        raise SoloAIError("Legacy anchor adoption requires every reviewed field")
    if task.get("status") in {"finished", "abandoned"}:
        raise SoloAIError("A terminal task does not accept a new active anchor")
    path = anchor_path(repo, str(task["id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    if is_link_or_junction(path.parent) or not path.parent.is_dir():
        raise SoloAIError("Task anchor directory is not a plain local directory")
    if path.exists():
        return require_anchor(repo, task)
    content = f"""# Task anchor: {task["name"]}

- Task ID: `{task["id"]}`
- Original purpose: {fields["objective"]}
- Implementation target: {fields["target"]}
- Reference baseline: `{task.get("base_ref")}` at `{task.get("base_head")}`
- Scope boundary: {fields["scope"]}
- Acceptance criteria: {fields["acceptance"]}
- Current progress: legacy task anchor reviewed and adopted at {utc_timestamp()}

This local file was explicitly reconstructed for a pre-anchor task. It is not committed. Reread it before continuing changes.
"""
    atomic_write_text(path, content)
    return require_anchor(repo, task)


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
