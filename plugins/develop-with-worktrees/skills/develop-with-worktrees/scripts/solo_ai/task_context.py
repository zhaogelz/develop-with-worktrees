from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from .repo import GitRepo
from .root_context import resolve_root_anchor
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
_TASK_ID_PATTERN = re.compile(r"task-[A-Za-z0-9][A-Za-z0-9-]*\Z")
_ROOT_REFERENCE_HEADER = re.compile(r"^- Root anchor:(?P<value>[^\r\n]*)$")
_LEGACY_REPAIR_REFERENCE_BASELINE = re.compile(
    r"source `(?P<source_base>[0-9a-fA-F]{40,64})` → "
    r"`(?P<source_head>[0-9a-fA-F]{40,64})`; repair base "
    r"`(?P<repair_base_ref>[^`\r\n]+)` at "
    r"`(?P<repair_base_head>[0-9a-fA-F]{40,64})`"
)


def anchor_origin(task: dict[str, Any]) -> dict[str, str]:
    """从任务创建时的事实生成可持久化的锚点不变量。"""

    task_id = str(task.get("id") or "")
    purpose = str(task.get("name") or "").strip()
    base_ref = str(task.get("base_ref") or "").strip()
    base_head = str(task.get("base_head") or "").strip()
    if not task_id or not purpose or not base_ref or not base_head:
        raise SoloAIError("Task lacks the original facts required for an anchor")
    return {
        "schema_version": "1",
        "task_id": task_id,
        "original_purpose": purpose,
        "reference_baseline": f"`{base_ref}` at `{base_head}`",
    }


def anchor_path(repo: GitRepo, task_id: str) -> Path:
    if not _TASK_ID_PATTERN.fullmatch(task_id):
        raise SoloAIError("Task id is not a safe anchor name")
    return repo.local_dir / "task-anchors" / f"{task_id}.md"


def _require_plain_file(path: Path, *, root: Path, label: str) -> Path:
    """不跟随链接地确认文件位于已知本地根中。"""

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


def _require_plain_directory(path: Path, *, root: Path, label: str) -> Path:
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
    if not raw_path.is_dir():
        raise SoloAIError(f"{label} must be a plain local directory")
    try:
        raw_path.resolve().relative_to(raw_root.resolve())
    except ValueError as exc:
        raise SoloAIError(f"{label} escaped the allowed local directory") from exc
    return raw_path


def _require_plain_anchor(repo: GitRepo, path: Path) -> str:
    return _read_plain_anchor(repo, path)[1]


def _read_plain_anchor(repo: GitRepo, path: Path) -> tuple[bytes, str]:
    if not path.exists():
        raise SoloAIError(f"Task anchor is missing: {path}. Restore it before Ready.")
    plain_path = _require_plain_file(path, root=repo.local_dir, label="Task anchor")
    raw = plain_path.read_bytes()
    if len(raw) > MAX_ANCHOR_BYTES:
        raise SoloAIError("Task anchor exceeds the 64 KiB safety limit")
    try:
        return raw, raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SoloAIError("Task anchor must be valid UTF-8") from exc


def _read_plain_input(repo: GitRepo, path: Path) -> str:
    if not path.exists():
        raise SoloAIError(f"Anchor update input is missing: {path}")
    plain_path = _require_plain_file(path, root=repo.root, label="Anchor update input")
    raw = plain_path.read_bytes()
    if len(raw) > MAX_ANCHOR_BYTES:
        raise SoloAIError("Anchor update input exceeds the 64 KiB safety limit")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SoloAIError("Anchor update input must be valid UTF-8") from exc


_FIELD_HEADER = re.compile(
    rf"^- (?P<field>{'|'.join(re.escape(field) for field in _ANCHOR_FIELDS)}):(?P<value>[^\r\n]*)$"
)
_INDENTED_FIELD_HEADER = re.compile(
    rf"^\s+- (?:{'|'.join(re.escape(field) for field in _ANCHOR_FIELDS)}):"
)


def _validated_anchor(content: str) -> tuple[dict[str, str], tuple[int, int]]:
    """提取顶层锚点字段，并保留完整 Current progress 块的精确范围。"""

    fields: dict[str, str] = {}
    lines = content.splitlines(keepends=True)
    offsets: list[int] = []
    offset = 0
    for line in lines:
        offsets.append(offset)
        offset += len(line)
    in_fence = False
    progress_index: int | None = None
    for index, line in enumerate(lines):
        plain = line.rstrip("\r\n")
        stripped = plain.strip()
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if _INDENTED_FIELD_HEADER.match(plain):
            raise SoloAIError("Task anchor must not contain indented duplicate fields")
        match = _FIELD_HEADER.fullmatch(plain)
        if not match:
            continue
        field = match.group("field")
        if field in fields:
            raise SoloAIError(f"Task anchor must contain exactly one '{field}' field")
        fields[field] = match.group("value").strip()
        if field == "Current progress":
            progress_index = index
    for field in _ANCHOR_FIELDS:
        if field not in fields:
            raise SoloAIError(f"Task anchor must contain exactly one '{field}' field")
    assert progress_index is not None
    progress_end = progress_index + 1
    progress_lines = [fields["Current progress"]]
    while progress_end < len(lines):
        continuation = lines[progress_end].rstrip("\r\n")
        if not continuation.startswith(("  ", "\t")):
            break
        progress_lines.append(continuation.strip())
        progress_end += 1
    fields["Current progress"] = "\n".join(
        value for value in progress_lines if value
    ).strip()
    progress_start_offset = offsets[progress_index]
    progress_end_offset = (
        offsets[progress_end] if progress_end < len(lines) else len(content)
    )
    return fields, (progress_start_offset, progress_end_offset)


def _identity_value(value: str) -> str:
    return value.strip().removeprefix("`").removesuffix("`")


def _validated_fields(content: str) -> dict[str, str]:
    return _validated_anchor(content)[0]


def _root_reference(content: str) -> str | None:
    references: list[str] = []
    in_fence = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _ROOT_REFERENCE_HEADER.fullmatch(line)
        if match:
            references.append(_identity_value(match.group("value")))
    if len(references) > 1:
        raise SoloAIError("Task anchor must contain at most one Root anchor reference")
    return references[0] if references else None


def _require_root_reference(repo: GitRepo, task: dict[str, Any], content: str) -> None:
    expected = task.get("root_anchor_id")
    recorded = _root_reference(content)
    if expected is None:
        if recorded is not None:
            raise SoloAIError(
                "Task anchor root reference is not recorded in task state"
            )
        return
    if not isinstance(expected, str) or not expected:
        raise SoloAIError("Task root anchor reference is invalid")
    if recorded != expected:
        raise SoloAIError("Task anchor root reference does not match task state")
    external_root = task.get("root_anchor_file")
    if external_root is not None and (
        not isinstance(external_root, str) or not external_root
    ):
        raise SoloAIError("Task root anchor file reference is invalid")
    resolve_root_anchor(
        repo,
        root_id=expected,
        external_path=Path(external_root) if external_root else None,
    )


def _origin_is_verified(task: dict[str, Any], fields: dict[str, str]) -> bool:
    origin = task.get("anchor_origin")
    if origin is None:
        return False
    if not isinstance(origin, dict):
        raise SoloAIError("Task anchor origin record is invalid")
    required = ("schema_version", "task_id", "original_purpose", "reference_baseline")
    if any(not isinstance(origin.get(key), str) or not origin[key] for key in required):
        raise SoloAIError("Task anchor origin record is incomplete")
    if origin["schema_version"] != "1" or origin["task_id"] != str(task["id"]):
        raise SoloAIError("Task anchor origin record does not match the active task")
    if fields["Original purpose"] != origin["original_purpose"]:
        raise SoloAIError("Task anchor original purpose does not match task origin")
    if fields["Reference baseline"] != origin["reference_baseline"]:
        raise SoloAIError("Task anchor reference baseline does not match task origin")
    return True


def _validate_update_content(
    content: str,
    *,
    task_id: str,
    previous: dict[str, str],
    previous_content: str,
    progress_only: bool,
) -> dict[str, str]:
    if len(content.encode("utf-8")) > MAX_ANCHOR_BYTES:
        raise SoloAIError("Task anchor exceeds the 64 KiB safety limit")
    fields, progress_span = _validated_anchor(content)
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
        _, previous_progress_span = _validated_anchor(previous_content)
        previous_without_progress = (
            previous_content[: previous_progress_span[0]]
            + "<current-progress>"
            + previous_content[previous_progress_span[1] :]
        )
        updated_without_progress = (
            content[: progress_span[0]]
            + "<current-progress>"
            + content[progress_span[1] :]
        )
        if updated_without_progress != previous_without_progress:
            raise SoloAIError(
                "A ready task may update only the full Current progress block"
            )
    return fields


def read_anchor(repo: GitRepo, task: dict[str, Any]) -> dict[str, Any]:
    path = anchor_path(repo, str(task["id"]))
    raw, content = _read_plain_anchor(repo, path)
    fields = _validated_fields(content)
    if _identity_value(fields["Task ID"]) != str(task["id"]):
        raise SoloAIError("Task anchor identity does not match the active task")
    _require_root_reference(repo, task, content)
    origin_verified = _origin_is_verified(task, fields)
    return {
        "task_id": str(task["id"]),
        "anchor_path": str(path.resolve()),
        "content": content,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "origin_verified": origin_verified,
    }


def read_anchor_update(repo: GitRepo, path: Path) -> str:
    return _read_plain_input(repo, path)


def update_anchor(
    repo: GitRepo,
    task: dict[str, Any],
    *,
    content: str,
    expected_sha256: str,
    progress_only: bool = False,
) -> dict[str, Any]:
    path = anchor_path(repo, str(task["id"]))
    raw, previous_content = _read_plain_anchor(repo, path)
    current_sha256 = hashlib.sha256(raw).hexdigest()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256 or ""):
        raise SoloAIError(
            "Expected anchor SHA-256 must be 64 lowercase hexadecimal characters"
        )
    previous = _validated_fields(previous_content)
    if not _origin_is_verified(task, previous):
        raise SoloAIError(
            "Task anchor origin is unverified; explicitly adopt the legacy anchor before updating it"
        )
    _validate_update_content(
        content,
        task_id=str(task["id"]),
        previous=previous,
        previous_content=previous_content,
        progress_only=progress_only,
    )
    _require_root_reference(repo, task, content)
    new_raw = content.encode("utf-8")
    new_sha256 = hashlib.sha256(new_raw).hexdigest()
    if new_sha256 == current_sha256:
        return {
            "task_id": str(task["id"]),
            "anchor_path": str(path.resolve()),
            "sha256": current_sha256,
            "changed": False,
        }
    if current_sha256 != expected_sha256:
        raise SoloAIError(
            "Task anchor changed since it was read; fetch it again before updating "
            f"(current sha256: {current_sha256})"
        )
    atomic_write_text(path, content)
    _read_plain_anchor(repo, path)
    return {
        "task_id": str(task["id"]),
        "anchor_path": str(path.resolve()),
        "sha256": new_sha256,
        "changed": True,
    }


def create_anchor(repo: GitRepo, task: dict[str, Any]) -> Path:
    path = anchor_path(repo, str(task["id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    _require_plain_directory(
        path.parent, root=repo.local_dir, label="Task anchor directory"
    )
    if path.exists():
        require_anchor(repo, task)
        return path
    origin = anchor_origin(task)
    root_reference = task.get("root_anchor_id")
    if root_reference is not None:
        if not isinstance(root_reference, str) or not root_reference:
            raise SoloAIError("Task root anchor reference is invalid")
        external_root = task.get("root_anchor_file")
        if external_root is not None and (
            not isinstance(external_root, str) or not external_root
        ):
            raise SoloAIError("Task root anchor file reference is invalid")
        resolve_root_anchor(
            repo,
            root_id=root_reference,
            external_path=Path(external_root) if external_root else None,
        )
    root_line = f"- Root anchor: `{root_reference}`\n" if root_reference else ""
    content = f"""# Task anchor: {task["name"]}

- Task ID: `{task["id"]}`
- Original purpose: {origin["original_purpose"]}
{root_line}- Implementation target: fill before editing
- Reference baseline: {origin["reference_baseline"]}
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
) -> tuple[Path, dict[str, str]]:
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
    _require_plain_directory(
        path.parent, root=repo.local_dir, label="Task anchor directory"
    )
    if path.exists():
        existing_content = _require_plain_anchor(repo, path)
        existing = _validated_fields(existing_content)
        expected = {
            "Task ID": f"`{task['id']}`",
            "Original purpose": fields["objective"],
            "Implementation target": fields["target"],
            "Scope boundary": fields["scope"],
            "Acceptance criteria": fields["acceptance"],
        }
        if any(existing[key] != value for key, value in expected.items()):
            raise SoloAIError(
                "Existing legacy anchor does not match the reviewed adoption facts"
            )
        existing_reference_baseline = existing["Reference baseline"]
        reference_baseline = _verify_legacy_reference_baseline(
            repo, task=task, reference_baseline=existing_reference_baseline
        )
        if reference_baseline != existing_reference_baseline:
            old_line = f"- Reference baseline: {existing_reference_baseline}"
            if existing_content.count(old_line) != 1:
                raise SoloAIError(
                    "Existing legacy repair anchor baseline cannot be normalized safely"
                )
            atomic_write_text(
                path,
                existing_content.replace(
                    old_line,
                    f"- Reference baseline: {reference_baseline}",
                    1,
                ),
            )
        return require_anchor(repo, task), _reviewed_anchor_origin(
            task,
            original_purpose=fields["objective"],
            reference_baseline=reference_baseline,
        )
    reference_baseline = f"`{task.get('base_ref')}` at `{task.get('base_head')}`"
    content = f"""# Task anchor: {task["name"]}

- Task ID: `{task["id"]}`
- Original purpose: {fields["objective"]}
- Implementation target: {fields["target"]}
- Reference baseline: {reference_baseline}
- Scope boundary: {fields["scope"]}
- Acceptance criteria: {fields["acceptance"]}
- Current progress: legacy task anchor reviewed and adopted at {utc_timestamp()}

This local file was explicitly reconstructed for a pre-anchor task. It is not committed. Reread it before continuing changes.
"""
    atomic_write_text(path, content)
    return require_anchor(repo, task), _reviewed_anchor_origin(
        task,
        original_purpose=fields["objective"],
        reference_baseline=reference_baseline,
    )


def _reviewed_anchor_origin(
    task: dict[str, Any], *, original_purpose: str, reference_baseline: str
) -> dict[str, str]:
    return {
        "schema_version": "1",
        "task_id": str(task["id"]),
        "original_purpose": original_purpose,
        "reference_baseline": reference_baseline,
    }


def _verify_legacy_reference_baseline(
    repo: GitRepo, *, task: dict[str, Any], reference_baseline: str
) -> str:
    """验证旧锚点记录的原始基线仍是当前任务历史的一部分。"""

    match = re.fullmatch(r"`[^`\r\n]+` at `([0-9a-fA-F]{40,64})`", reference_baseline)
    if match:
        original_head = match.group(1)
        normalized_baseline = reference_baseline
    else:
        repair_match = _LEGACY_REPAIR_REFERENCE_BASELINE.fullmatch(reference_baseline)
        if not repair_match:
            raise SoloAIError(
                "Existing legacy anchor has an invalid reference baseline"
            )
        original_head = repair_match.group("source_base")
        _verify_legacy_repair_reference_baseline(
            repo,
            task=task,
            source_base=original_head,
            source_head=repair_match.group("source_head"),
            repair_base_ref=repair_match.group("repair_base_ref"),
            repair_base_head=repair_match.group("repair_base_head"),
        )
        normalized_baseline = f"`{task.get('base_ref')}` at `{task.get('base_head')}`"
    resolved = repo.git(
        ["rev-parse", "--verify", f"{original_head}^{{commit}}"], check=False
    )
    if (
        resolved.returncode != 0
        or resolved.stdout.strip().lower() != original_head.lower()
    ):
        raise SoloAIError(
            "Existing legacy anchor references an unknown baseline commit"
        )
    current_base = str(task.get("base_head") or "")
    branch = str(task.get("branch") or "")
    branch_head = repo.ref_head(f"refs/heads/{branch}") if branch else None
    if (
        not current_base
        or branch_head is None
        or not repo.is_ancestor(original_head, current_base)
        or not repo.is_ancestor(original_head, branch_head)
    ):
        raise SoloAIError(
            "Existing legacy anchor baseline is not an ancestor of the active task"
        )
    return normalized_baseline


def _verify_legacy_repair_reference_baseline(
    repo: GitRepo,
    *,
    task: dict[str, Any],
    source_base: str,
    source_head: str,
    repair_base_ref: str,
    repair_base_head: str,
) -> None:
    """仅接管可由任务与候选事实交叉验证的旧版修复锚点。"""

    preparation = task.get("repair_preparation")
    if not isinstance(preparation, dict):
        raise SoloAIError("Existing legacy repair anchor lacks repair facts")
    candidate_id = preparation.get("candidate_id")
    source_ref = preparation.get("source_ref")
    expected_source_head = preparation.get("source_head")
    changed_paths = preparation.get("changed_paths")
    conflict_paths = preparation.get("conflict_paths")
    repair_attempt = preparation.get("repair_attempt")
    if (
        not isinstance(candidate_id, str)
        or not candidate_id
        or task.get("supersedes") != candidate_id
        or source_ref != f"refs/dww/candidates/{candidate_id}"
        or not isinstance(expected_source_head, str)
        or expected_source_head.lower() != source_head.lower()
        or preparation.get("base_ref") != repair_base_ref
        or preparation.get("base_head") != repair_base_head
        or task.get("base_ref") != repair_base_ref
        or task.get("base_head") != repair_base_head
        or preparation.get("outcome") != "conflicted"
        or preparation.get("manual_notification_required") is not False
        or not isinstance(repair_attempt, int)
        or isinstance(repair_attempt, bool)
        or repair_attempt < 1
        or not isinstance(changed_paths, list)
        or not changed_paths
        or any(not isinstance(path, str) or not path for path in changed_paths)
        or len(set(changed_paths)) != len(changed_paths)
        or not isinstance(conflict_paths, list)
        or not conflict_paths
        or any(not isinstance(path, str) or not path for path in conflict_paths)
        or not set(conflict_paths).issubset(set(changed_paths))
    ):
        raise SoloAIError(
            "Existing legacy repair anchor facts do not match the active task"
        )
    for head in (source_base, source_head, repair_base_head):
        resolved = repo.git(
            ["rev-parse", "--verify", f"{head}^{{commit}}"], check=False
        )
        if resolved.returncode != 0 or resolved.stdout.strip().lower() != head.lower():
            raise SoloAIError(
                "Existing legacy repair anchor references an unknown commit"
            )
    source_paths = [
        path
        for path in repo.git(
            ["diff", "--name-only", "-z", source_base, source_head]
        ).stdout.split("\0")
        if path
    ]
    if sorted(changed_paths) != sorted(source_paths):
        raise SoloAIError(
            "Existing legacy repair anchor source path facts do not match"
        )
    source_ref_head = repo.ref_head(source_ref)
    if source_ref_head is not None and source_ref_head != source_head:
        raise SoloAIError("Existing legacy repair anchor source ref changed")
    if not repo.is_ancestor(source_base, source_head) or not repo.is_ancestor(
        source_base, repair_base_head
    ):
        raise SoloAIError("Existing legacy repair anchor source history is invalid")
    branch = str(task.get("branch") or "")
    branch_head = repo.ref_head(f"refs/heads/{branch}") if branch else None
    candidate_head = task.get("candidate_head")
    if (
        not isinstance(candidate_head, str)
        or branch_head is None
        or candidate_head != branch_head
        or not repo.is_ancestor(repair_base_head, branch_head)
    ):
        raise SoloAIError(
            "Existing legacy repair anchor repair base is not an ancestor of the task"
        )


def require_anchor(
    repo: GitRepo, task: dict[str, Any], *, require_verified_origin: bool = False
) -> Path:
    path = anchor_path(repo, str(task["id"]))
    shown = read_anchor(repo, task)
    if require_verified_origin and not shown["origin_verified"]:
        raise SoloAIError(
            "Task anchor origin is unverified; explicitly adopt the legacy anchor before Ready"
        )
    return path


def delete_anchor(repo: GitRepo, task_id: str) -> None:
    path = anchor_path(repo, task_id)
    if not path.exists():
        return
    _require_plain_anchor(repo, path)
    path.unlink()


def list_anchors(repo: GitRepo) -> list[dict[str, Any]]:
    root = repo.local_dir / "task-anchors"
    if not root.exists():
        return []
    if is_link_or_junction(root) or not root.is_dir():
        raise SoloAIError("Task anchor directory is not a plain local directory")
    result: list[dict[str, Any]] = []
    for path in sorted(root.glob("task-*.md")):
        _require_plain_anchor(repo, path)
        result.append(
            {
                "task_id": path.stem,
                "path": str(path.resolve()),
                "bytes": path.stat().st_size,
            }
        )
    return result
