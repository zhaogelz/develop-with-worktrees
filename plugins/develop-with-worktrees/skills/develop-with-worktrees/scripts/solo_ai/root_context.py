from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from .repo import GitRepo
from .util import SoloAIError, atomic_write_text, is_link_or_junction, utc_timestamp

MAX_ROOT_ANCHOR_BYTES = 64 * 1024
_ROOT_FIELDS = (
    "Root ID",
    "Original purpose",
    "Implementation target",
    "Reference baseline",
    "Scope boundary",
    "Acceptance criteria",
    "Current progress",
)
_IMMUTABLE_FIELDS = ("Root ID", "Original purpose", "Reference baseline")
_ROOT_ID_PATTERN = re.compile(r"root-[A-Za-z0-9][A-Za-z0-9-]*\Z")
_FIELD_HEADER = re.compile(
    rf"^- (?P<field>{'|'.join(re.escape(field) for field in _ROOT_FIELDS)}):(?P<value>[^\r\n]*)$"
)
_INDENTED_FIELD_HEADER = re.compile(
    rf"^\s+- (?:{'|'.join(re.escape(field) for field in _ROOT_FIELDS)}):"
)


def root_anchor_path(repo: GitRepo, root_id: str) -> Path:
    if not _ROOT_ID_PATTERN.fullmatch(root_id):
        raise SoloAIError("Root anchor id is not safe")
    return repo.local_dir / "root-anchors" / f"{root_id}.md"


def _require_plain_file(path: Path, *, root: Path, label: str) -> Path:
    raw_root = root.absolute()
    raw_path = path.absolute()
    if is_link_or_junction(raw_root) or not raw_root.is_dir():
        raise SoloAIError(f"{label} root is not a plain local directory")
    try:
        relative = raw_path.relative_to(raw_root)
    except ValueError as exc:
        raise SoloAIError(f"{label} is outside the allowed local directory") from exc
    current = raw_root
    for part in relative.parts:
        current = current / part
        if is_link_or_junction(current):
            raise SoloAIError(f"{label} must not be a link or junction")
    if not raw_path.is_file():
        raise SoloAIError(f"{label} must be a regular local UTF-8 file")
    try:
        raw_path.resolve().relative_to(raw_root.resolve())
    except ValueError as exc:
        raise SoloAIError(f"{label} escaped the allowed local directory") from exc
    return raw_path


def _read_plain_root_anchor(repo: GitRepo, path: Path) -> tuple[bytes, str]:
    if not path.exists():
        raise SoloAIError(f"Root anchor is missing: {path}")
    plain_path = _require_plain_file(path, root=repo.local_dir, label="Root anchor")
    raw = plain_path.read_bytes()
    if len(raw) > MAX_ROOT_ANCHOR_BYTES:
        raise SoloAIError("Root anchor exceeds the 64 KiB safety limit")
    try:
        return raw, raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SoloAIError("Root anchor must be valid UTF-8") from exc


def _read_plain_input(repo: GitRepo, path: Path) -> str:
    if not path.exists():
        raise SoloAIError(f"Root anchor update input is missing: {path}")
    plain_path = _require_plain_file(
        path, root=repo.root, label="Root anchor update input"
    )
    raw = plain_path.read_bytes()
    if len(raw) > MAX_ROOT_ANCHOR_BYTES:
        raise SoloAIError("Root anchor update input exceeds the 64 KiB safety limit")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SoloAIError("Root anchor update input must be valid UTF-8") from exc


def _validated_fields(content: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    in_fence = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if _INDENTED_FIELD_HEADER.match(line):
            raise SoloAIError("Root anchor must not contain indented duplicate fields")
        match = _FIELD_HEADER.fullmatch(line)
        if not match:
            continue
        field = match.group("field")
        if field in fields:
            raise SoloAIError(f"Root anchor must contain exactly one '{field}' field")
        fields[field] = match.group("value").strip()
    for field in _ROOT_FIELDS:
        if not fields.get(field):
            raise SoloAIError(f"Root anchor must contain a non-empty '{field}' field")
    if fields["Implementation target"].lower().startswith("fill before"):
        raise SoloAIError("Root anchor implementation target is still a placeholder")
    if fields["Scope boundary"].lower().startswith("fill before"):
        raise SoloAIError("Root anchor scope boundary is still a placeholder")
    if fields["Acceptance criteria"].lower().startswith("fill before"):
        raise SoloAIError("Root anchor acceptance criteria is still a placeholder")
    return fields


def _identity_value(value: str) -> str:
    return value.strip().removeprefix("`").removesuffix("`")


def create_root_anchor(
    repo: GitRepo,
    *,
    root_id: str,
    purpose: str,
    target: str,
    base_ref: str,
    base_head: str,
    scope: str,
    acceptance: str,
) -> dict[str, Any]:
    if not _ROOT_ID_PATTERN.fullmatch(root_id):
        raise SoloAIError("Root anchor id is not safe")
    values = (purpose, target, base_ref, base_head, scope, acceptance)
    if any(not value.strip() for value in values):
        raise SoloAIError("Root anchor creation requires non-empty contract fields")
    path = root_anchor_path(repo, root_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    if is_link_or_junction(path.parent) or not path.parent.is_dir():
        raise SoloAIError("Root anchor directory is not a plain local directory")
    if path.exists():
        return show_root_anchor(repo, root_id=root_id)
    content = f"""# Root task anchor: {purpose}

- Root ID: `{root_id}`
- Original purpose: {purpose}
- Implementation target: {target}
- Reference baseline: `{base_ref}` at `{base_head}`
- Scope boundary: {scope}
- Acceptance criteria: {acceptance}
- Current progress: root anchor created at {utc_timestamp()}

This local file is not committed. It is the coordinator's durable execution contract. Child tasks may read its current facts but do not alter candidate, batch, or scheduler ownership.
"""
    atomic_write_text(path, content)
    return show_root_anchor(repo, root_id=root_id)


def show_root_anchor(repo: GitRepo, *, root_id: str) -> dict[str, Any]:
    path = root_anchor_path(repo, root_id)
    raw, content = _read_plain_root_anchor(repo, path)
    fields = _validated_fields(content)
    if _identity_value(fields["Root ID"]) != root_id:
        raise SoloAIError("Root anchor identity does not match its path")
    return {
        "root_id": root_id,
        "root_anchor_path": str(path.resolve()),
        "content": content,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def update_root_anchor(
    repo: GitRepo,
    *,
    root_id: str,
    input_path: Path,
    expected_sha256: str,
) -> dict[str, Any]:
    current = show_root_anchor(repo, root_id=root_id)
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256 or ""):
        raise SoloAIError("Expected root anchor SHA-256 must be lowercase hexadecimal")
    content = _read_plain_input(repo, input_path)
    if len(content.encode("utf-8")) > MAX_ROOT_ANCHOR_BYTES:
        raise SoloAIError("Root anchor exceeds the 64 KiB safety limit")
    previous = _validated_fields(str(current["content"]))
    updated = _validated_fields(content)
    if _identity_value(updated["Root ID"]) != root_id:
        raise SoloAIError("Root anchor identity does not match the requested root")
    for field in _IMMUTABLE_FIELDS:
        if updated[field] != previous[field]:
            raise SoloAIError(f"Root anchor field cannot be changed: {field}")
    raw = content.encode("utf-8")
    sha256 = hashlib.sha256(raw).hexdigest()
    if sha256 == current["sha256"]:
        return {**current, "changed": False}
    if current["sha256"] != expected_sha256:
        raise SoloAIError("Root anchor changed since it was read; fetch it again")
    atomic_write_text(root_anchor_path(repo, root_id), content)
    return {**show_root_anchor(repo, root_id=root_id), "changed": True}


def delete_root_anchor(repo: GitRepo, *, root_id: str) -> None:
    path = root_anchor_path(repo, root_id)
    _read_plain_root_anchor(repo, path)
    path.unlink()


def list_root_anchors(repo: GitRepo) -> list[dict[str, str]]:
    root = repo.local_dir / "root-anchors"
    if not root.exists():
        return []
    if is_link_or_junction(root) or not root.is_dir():
        raise SoloAIError("Root anchor directory is not a plain local directory")
    result: list[dict[str, str]] = []
    for path in sorted(root.glob("root-*.md")):
        root_id = path.stem
        shown = show_root_anchor(repo, root_id=root_id)
        result.append(
            {
                "root_id": root_id,
                "root_anchor_path": str(path.resolve()),
                "sha256": str(shown["sha256"]),
            }
        )
    return result
