from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

from .repo import GitRepo
from .util import (
    DirectoryLock,
    SoloAIError,
    atomic_copy_file,
    atomic_write_text,
    is_link_or_junction,
    sha256_file,
    utc_timestamp,
)

_ROOT_FIELDS = (
    "Root ID",
    "Original purpose",
    "Implementation target",
    "Reference baseline",
    "Scope boundary",
    "Acceptance criteria",
    "Current progress",
)
_PLAN_VERSION_FIELD = "Plan version"
_IMMUTABLE_FIELDS = ("Root ID", "Original purpose", "Reference baseline")
_ROOT_ID_PATTERN = re.compile(r"root-[A-Za-z0-9][A-Za-z0-9-]*\Z")
_FIELD_HEADER = re.compile(
    rf"^- (?P<field>{'|'.join(re.escape(field) for field in _ROOT_FIELDS)}):(?P<value>[^\r\n]*)$"
)
_INDENTED_FIELD_HEADER = re.compile(
    rf"^\s+- (?:{'|'.join(re.escape(field) for field in _ROOT_FIELDS)}):"
)
_CHILDREN_START = "<!-- dww-root-children:start -->"
_CHILDREN_END = "<!-- dww-root-children:end -->"
_CHILD_RECORD = re.compile(r"^- Child state: (?P<value>[^\r\n]+)$")
_TASK_ID_PATTERN = re.compile(r"task-[A-Za-z0-9][A-Za-z0-9-]*\Z")
_PLAN_HEADING = "## Confirmed plan"
_CHANGES_HEADING = "## User-confirmed changes"
_ACCEPTANCE_HEADING = "## Overall acceptance"
_PLAN_START = "<!-- dww-confirmed-plan:start -->"
_PLAN_END = "<!-- dww-confirmed-plan:end -->"
_CHANGES_START = "<!-- dww-user-changes:start -->"
_CHANGES_END = "<!-- dww-user-changes:end -->"
_ACCEPTANCE_START = "<!-- dww-overall-acceptance:start -->"
_ACCEPTANCE_END = "<!-- dww-overall-acceptance:end -->"
_ACCEPTANCE_VERSION_FIELD = "Accepted plan version"
_SECTION_MARKERS = {
    _PLAN_HEADING: (_PLAN_START, _PLAN_END),
    _CHANGES_HEADING: (_CHANGES_START, _CHANGES_END),
    _ACCEPTANCE_HEADING: (_ACCEPTANCE_START, _ACCEPTANCE_END),
}
_REQUEST_COMMENT = re.compile(r"<!-- dww-root-request:([0-9a-f]{64}) -->")


def root_anchor_path(repo: GitRepo, root_id: str) -> Path:
    if not _ROOT_ID_PATTERN.fullmatch(root_id):
        raise SoloAIError("Root anchor id is not safe")
    return repo.local_dir / "root-anchors" / f"{root_id}.md"


def root_anchor_history_directory(repo: GitRepo, root_id: str) -> Path:
    """返回一个根锚点的完整旧版本目录，不枚举或读取其内容。"""

    if not _ROOT_ID_PATTERN.fullmatch(root_id):
        raise SoloAIError("Root anchor id is not safe")
    return repo.local_dir / "root-anchor-history" / root_id


def root_anchor_history_path(repo: GitRepo, *, root_id: str, version: int) -> Path:
    if version < 1:
        raise SoloAIError("Root anchor history version must be positive")
    return root_anchor_history_directory(repo, root_id) / f"{root_id}.v{version}.md"


def _external_root_anchor_path(path: Path, *, root_id: str) -> tuple[Path, Path]:
    """验证一个显式外部根锚点的固定 DWW 本地布局。"""

    if not _ROOT_ID_PATTERN.fullmatch(root_id):
        raise SoloAIError("Root anchor id is not safe")
    if not path.is_absolute():
        raise SoloAIError("External root anchor path must be absolute")
    raw_path = path.absolute()
    if raw_path.name != f"{root_id}.md":
        raise SoloAIError("External root anchor path does not match the root id")
    if (
        raw_path.parent.name != "root-anchors"
        or raw_path.parent.parent.name != "solo-ai"
    ):
        raise SoloAIError(
            "External root anchor must use the DWW solo-ai/root-anchors layout"
        )
    common_dir = raw_path.parent.parent.parent
    _require_plain_file(raw_path, root=common_dir, label="External root anchor")
    return raw_path, common_dir


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


def _require_plain_directory(
    path: Path, *, root: Path, label: str, create: bool = False
) -> Path:
    """确认或逐级创建一个不经链接逃逸的本地目录。"""

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
        if current.exists():
            if is_link_or_junction(current) or not current.is_dir():
                raise SoloAIError(f"{label} must be a plain local directory")
        elif create:
            current.mkdir()
        else:
            raise SoloAIError(f"{label} is missing")
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
    try:
        return raw, raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SoloAIError("Root anchor must be valid UTF-8") from exc


def _read_external_root_anchor(path: Path, *, root_id: str) -> tuple[bytes, str]:
    raw_path, common_dir = _external_root_anchor_path(path, root_id=root_id)
    raw = _require_plain_file(
        raw_path, root=common_dir, label="External root anchor"
    ).read_bytes()
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
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SoloAIError("Root anchor update input must be valid UTF-8") from exc


def read_root_plan_input(repo: GitRepo, path: Path) -> str:
    """读取完整已确认方案；输入文件只作为创建或修订的来源。"""

    content = _read_plain_input(repo, path).strip()
    if not content:
        raise SoloAIError("Confirmed plan input must not be empty")
    return content


def read_root_change_input(repo: GitRepo, path: Path) -> str:
    """读取一份将逐字附加到现行方案的用户修订。"""

    content = _read_plain_input(repo, path)
    if not content.strip():
        raise SoloAIError("Confirmed plan change input must not be empty")
    return content


def _metadata_prefix(content: str) -> str:
    """新格式只从正文前的元数据读取字段，方案正文可自由使用列表和代码。"""

    marker = re.search(r"(?m)^## Confirmed plan\s*$", content)
    return content[: marker.start()] if marker else content


def _validated_fields(content: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    in_fence = False
    for line in _metadata_prefix(content).splitlines():
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


def _metadata_value(content: str, field: str) -> str | None:
    pattern = re.compile(rf"^- {re.escape(field)}:(?P<value>[^\r\n]*)$", re.MULTILINE)
    matches = list(pattern.finditer(_metadata_prefix(content)))
    if not matches:
        return None
    if len(matches) != 1:
        raise SoloAIError(f"Root anchor must contain at most one '{field}' field")
    value = matches[0].group("value").strip()
    if not value:
        raise SoloAIError(f"Root anchor '{field}' must not be empty")
    return value


def _plan_version(content: str) -> int | None:
    raw = _metadata_value(content, _PLAN_VERSION_FIELD)
    if raw is None:
        return None
    if not raw.isdecimal() or int(raw) < 1:
        raise SoloAIError("Root anchor plan version must be a positive integer")
    return int(raw)


def _section(content: str, heading: str) -> str | None:
    matches = list(re.finditer(rf"(?m)^{re.escape(heading)}\s*$", content))
    if not matches:
        return None
    if len(matches) != 1:
        raise SoloAIError(f"Root anchor must contain at most one '{heading}' section")
    markers = _SECTION_MARKERS.get(heading)
    if markers is None:
        raise SoloAIError(
            f"Root anchor has no registered section markers for '{heading}'"
        )
    start_marker, end_marker = markers
    starts = [match.start() for match in re.finditer(re.escape(start_marker), content)]
    ends = [match.start() for match in re.finditer(re.escape(end_marker), content)]
    if not starts and not ends and _plan_version(content) is None:
        # 旧根锚点可能把这个普通 Markdown 标题作为附注；不能因此失去可读性。
        return None
    if len(starts) != 1 or len(ends) != 1:
        raise SoloAIError(f"Root anchor must contain one marker pair for '{heading}'")
    start = starts[0]
    end = ends[0]
    if start < matches[0].end() or end <= start:
        raise SoloAIError(f"Root anchor section markers are malformed for '{heading}'")
    body_start = start + len(start_marker)
    return content[body_start:end].strip()


def _require_structured_plan(content: str) -> tuple[int, str, str, str]:
    version = _plan_version(content)
    plan = _section(content, _PLAN_HEADING)
    changes = _section(content, _CHANGES_HEADING)
    acceptance = _section(content, _ACCEPTANCE_HEADING)
    if version is None or not plan or not changes or not acceptance:
        raise SoloAIError(
            "Structured root anchors require a plan version, confirmed plan, user-confirmed changes, and overall acceptance"
        )
    return version, plan, changes, acceptance


def _replace_metadata_value(content: str, field: str, value: str) -> str:
    prefix = _metadata_prefix(content)
    pattern = re.compile(rf"^- {re.escape(field)}:[^\r\n]*$", re.MULTILINE)
    replacement = f"- {field}: {value}"
    if pattern.search(prefix):
        return pattern.sub(replacement, content, count=1)
    insertion = prefix.rstrip() + "\n" + replacement + "\n"
    return insertion + content[len(prefix) :].lstrip("\r\n")


def _replace_section(content: str, heading: str, body: str) -> str:
    matches = list(re.finditer(rf"(?m)^{re.escape(heading)}\s*$", content))
    if len(matches) != 1:
        raise SoloAIError(f"Root anchor must contain exactly one '{heading}' section")
    start_marker, end_marker = _SECTION_MARKERS[heading]
    starts = [match.start() for match in re.finditer(re.escape(start_marker), content)]
    ends = [match.start() for match in re.finditer(re.escape(end_marker), content)]
    if (
        len(starts) != 1
        or len(ends) != 1
        or starts[0] < matches[0].end()
        or ends[0] <= starts[0]
    ):
        raise SoloAIError(f"Root anchor section markers are malformed for '{heading}'")
    replacement = f"{heading}\n\n{start_marker}\n{body.strip()}\n{end_marker}\n\n"
    return (
        content[: matches[0].start()]
        + replacement
        + content[ends[0] + len(end_marker) :].lstrip("\r\n")
    )


def _contract_without_progress_or_outcome(content: str) -> str:
    normalized = _replace_metadata_value(
        content, "Current progress", "<current-progress>"
    )
    if _section(normalized, _ACCEPTANCE_HEADING) is not None:
        normalized = _replace_section(
            normalized, _ACCEPTANCE_HEADING, "<overall-acceptance>"
        )
    return normalized


def _request_fingerprint(request_id: str) -> str:
    value = request_id.strip()
    if not value or len(value) > 512:
        raise SoloAIError("Root anchor request id must be between 1 and 512 characters")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def root_id_for_request(request_id: str) -> str:
    return "root-" + _request_fingerprint(request_id)[:24]


def _normalized_newlines(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def _acceptance_status(content: str) -> str | None:
    section = _section(content, _ACCEPTANCE_HEADING)
    if section is None:
        return None
    matches = list(re.finditer(r"(?m)^- Status: (?P<value>[^\r\n]+)$", section))
    if len(matches) != 1:
        raise SoloAIError("Overall acceptance must contain exactly one status")
    value = matches[0].group("value").strip()
    if value not in {"pending", "accepted", "cancelled"}:
        raise SoloAIError(
            "Overall acceptance status must be pending, accepted, or cancelled"
        )
    return value


def _acceptance_plan_version(content: str) -> int | None:
    section = _section(content, _ACCEPTANCE_HEADING)
    if section is None:
        return None
    matches = list(
        re.finditer(
            rf"(?m)^- {re.escape(_ACCEPTANCE_VERSION_FIELD)}: (?P<value>[^\r\n]+)$",
            section,
        )
    )
    if not matches:
        return None
    if len(matches) != 1:
        raise SoloAIError(
            "Overall acceptance must contain at most one accepted plan version"
        )
    value = matches[0].group("value").strip()
    if not value.isdecimal() or int(value) < 1:
        raise SoloAIError("Overall acceptance accepted plan version must be positive")
    return int(value)


def _pending_acceptance() -> str:
    return "- Status: pending\n- Evidence: plan changed; overall acceptance must be checked again"


def _identity_value(value: str) -> str:
    return value.strip().removeprefix("`").removesuffix("`")


def _linked_children(content: str) -> tuple[str | None, list[dict[str, str]]]:
    """读取由 DWW 唯一维护的跨仓库子任务登记区。"""

    starts = [
        match.start() for match in re.finditer(re.escape(_CHILDREN_START), content)
    ]
    ends = [match.end() for match in re.finditer(re.escape(_CHILDREN_END), content)]
    if not starts and not ends:
        return None, []
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        raise SoloAIError("Root anchor child registry is malformed")
    raw_registry = content[starts[0] : ends[0]]
    body = content[starts[0] + len(_CHILDREN_START) : ends[0] - len(_CHILDREN_END)]
    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for line in body.splitlines():
        if not line.strip():
            continue
        match = _CHILD_RECORD.fullmatch(line)
        if not match:
            raise SoloAIError("Root anchor child registry contains an invalid record")
        try:
            record = json.loads(match.group("value"))
        except json.JSONDecodeError as exc:
            raise SoloAIError(
                "Root anchor child registry contains invalid JSON"
            ) from exc
        if not isinstance(record, dict) or set(record) != {
            "root_anchor_path",
            "state_path",
            "task_id",
        }:
            raise SoloAIError("Root anchor child registry record is incomplete")
        normalized = {key: record[key] for key in record}
        if any(
            not isinstance(value, str) or not value for value in normalized.values()
        ):
            raise SoloAIError("Root anchor child registry record is invalid")
        if not _TASK_ID_PATTERN.fullmatch(normalized["task_id"]):
            raise SoloAIError("Root anchor child registry task id is invalid")
        if normalized["task_id"] in seen:
            raise SoloAIError("Root anchor child registry contains a duplicate task")
        seen.add(normalized["task_id"])
        records.append(normalized)
    return raw_registry, records


def _render_linked_children(content: str, records: list[dict[str, str]]) -> str:
    lines = [_CHILDREN_START]
    for record in records:
        lines.append(
            "- Child state: "
            + json.dumps(
                record, ensure_ascii=True, sort_keys=True, separators=(",", ":")
            )
        )
    lines.append(_CHILDREN_END)
    rendered = "\n".join(lines)
    existing, _ = _linked_children(content)
    if existing is None:
        return (
            content.rstrip()
            + "\n\n## Linked child tasks (DWW-managed)\n\n"
            + rendered
            + "\n"
        )
    return content.replace(existing, rendered, 1)


def _shown_root_anchor(
    *, path: Path, root_id: str, raw: bytes, content: str
) -> dict[str, Any]:
    fields = _validated_fields(content)
    if _identity_value(fields["Root ID"]) != root_id:
        raise SoloAIError("Root anchor identity does not match its path")
    _, children = _linked_children(content)
    plan_version = _plan_version(content)
    local_dir = (
        path.parent.parent
        if path.parent.name == "root-anchors"
        else path.parent.parent.parent
    )
    return {
        "root_id": root_id,
        "root_anchor_path": str(path.resolve()),
        "history_directory": str(local_dir / "root-anchor-history" / root_id),
        "size_bytes": len(raw),
        "content": content,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "linked_child_tasks": children,
        "plan_version": plan_version,
        "confirmed_plan": _section(content, _PLAN_HEADING),
        "overall_acceptance_status": _acceptance_status(content),
        "overall_acceptance_plan_version": _acceptance_plan_version(content),
    }


def show_external_root_anchor(path: Path, *, root_id: str) -> dict[str, Any]:
    """读取一个由调用者显式指定的异仓库根锚点。"""

    raw_path, _ = _external_root_anchor_path(path, root_id=root_id)
    raw, content = _read_external_root_anchor(raw_path, root_id=root_id)
    return _shown_root_anchor(path=raw_path, root_id=root_id, raw=raw, content=content)


def resolve_root_anchor(
    repo: GitRepo, *, root_id: str, external_path: Path | None = None
) -> dict[str, Any]:
    """解析本仓库根或一个显式、固定的异仓库根。"""

    if external_path is None:
        return show_root_anchor(repo, root_id=root_id)
    shown = show_external_root_anchor(external_path, root_id=root_id)
    if (
        Path(str(shown["root_anchor_path"])).resolve()
        == root_anchor_path(repo, root_id).resolve()
    ):
        raise SoloAIError(
            "Use --root-anchor without --root-anchor-file for a local root anchor"
        )
    return shown


def root_anchor_lock(path: Path) -> DirectoryLock:
    """为根锚点的登记、更新和关闭提供单文件锁。"""

    root_id = path.stem
    raw_path, common_dir = _external_root_anchor_path(path, root_id=root_id)
    _require_plain_file(raw_path, root=common_dir, label="Root anchor")
    return DirectoryLock(raw_path.parent / f".dww-root-{root_id}.lock", wait=True)


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
    confirmed_plan: str | None = None,
    plan_source: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    if not _ROOT_ID_PATTERN.fullmatch(root_id):
        raise SoloAIError("Root anchor id is not safe")
    values = (purpose, target, base_ref, base_head, scope, acceptance)
    if any(not value.strip() for value in values):
        raise SoloAIError("Root anchor creation requires non-empty contract fields")
    if (confirmed_plan is None) != (plan_source is None):
        raise SoloAIError("Confirmed plan and its source must be provided together")
    if confirmed_plan is not None and not confirmed_plan.strip():
        raise SoloAIError("Confirmed plan must not be empty")
    if confirmed_plan is not None and request_id is None:
        raise SoloAIError("Confirmed plan roots require a stable request id")
    if confirmed_plan is not None and any(
        marker in confirmed_plan
        for marker in (
            _PLAN_START,
            _PLAN_END,
            _CHANGES_START,
            _CHANGES_END,
            _ACCEPTANCE_START,
            _ACCEPTANCE_END,
        )
    ):
        raise SoloAIError(
            "Confirmed plan must not contain reserved root anchor markers"
        )
    if plan_source is not None and (
        not plan_source.strip() or "\r" in plan_source or "\n" in plan_source
    ):
        raise SoloAIError("Confirmed plan source must be a non-empty single line")
    request_marker = (
        f"<!-- dww-root-request:{_request_fingerprint(request_id)} -->"
        if request_id is not None
        else ""
    )
    path = root_anchor_path(repo, root_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    if is_link_or_junction(path.parent) or not path.parent.is_dir():
        raise SoloAIError("Root anchor directory is not a plain local directory")
    if path.exists():
        shown = show_root_anchor(repo, root_id=root_id)
        existing_content = str(shown["content"])
        existing = _validated_fields(existing_content)
        request_matches = _REQUEST_COMMENT.findall(_metadata_prefix(existing_content))
        expected_request = _request_fingerprint(request_id) if request_id else None
        initial_change = f"- Version 1: initial confirmed plan. Source: {plan_source}"
        if (
            existing["Original purpose"] != purpose
            or existing["Implementation target"] != target
            or existing["Reference baseline"] != f"`{base_ref}` at `{base_head}`"
            or existing["Scope boundary"] != scope
            or existing["Acceptance criteria"] != acceptance
            or (expected_request is not None and request_matches != [expected_request])
            or (
                confirmed_plan is not None
                and (
                    shown.get("confirmed_plan") is None
                    or _normalized_newlines(str(shown["confirmed_plan"]))
                    != _normalized_newlines(confirmed_plan)
                    or initial_change
                    not in (_section(existing_content, _CHANGES_HEADING) or "")
                )
            )
        ):
            raise SoloAIError("Root anchor request conflicts with the existing root")
        return shown
    if confirmed_plan is None:
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
    else:
        content = f"""# Root task anchor: {purpose}

- Root ID: `{root_id}`
- Original purpose: {purpose}
- Implementation target: {target}
- Reference baseline: `{base_ref}` at `{base_head}`
- Scope boundary: {scope}
- Acceptance criteria: {acceptance}
- Plan version: 1
- Current progress: root anchor created at {utc_timestamp()}

{request_marker}

## Confirmed plan

{_PLAN_START}
{confirmed_plan.strip()}
{_PLAN_END}

## User-confirmed changes

{_CHANGES_START}
- Version 1: initial confirmed plan. Source: {plan_source}
{_CHANGES_END}

## Overall acceptance

{_ACCEPTANCE_START}
- Status: pending
- Evidence: not checked
{_ACCEPTANCE_END}

This local file is not committed. It is the objective's durable execution contract. Child tasks may read its current facts but do not alter candidate, batch, or scheduler ownership.
"""
    atomic_write_text(path, content)
    return show_root_anchor(repo, root_id=root_id)


def show_root_anchor(repo: GitRepo, *, root_id: str) -> dict[str, Any]:
    path = root_anchor_path(repo, root_id)
    raw, content = _read_plain_root_anchor(repo, path)
    return _shown_root_anchor(path=path, root_id=root_id, raw=raw, content=content)


def show_root_anchor_history(
    repo: GitRepo, *, root_id: str, version: int
) -> dict[str, Any]:
    """按确定版本读取一份历史根锚点；不会枚举全部历史。"""

    path = root_anchor_history_path(repo, root_id=root_id, version=version)
    if not path.exists():
        raise SoloAIError(f"Root anchor history version is missing: {path}")
    raw, content = _read_plain_root_anchor(repo, path)
    shown = _shown_root_anchor(path=path, root_id=root_id, raw=raw, content=content)
    shown["history_version"] = version
    return shown


def _snapshot_root_anchor_version(
    repo: GitRepo,
    *,
    root_id: str,
    version: int,
    source_path: Path,
    source_sha256: str,
) -> bool:
    """在覆盖当前根前持久化精确旧版本；同一重试只能复用同一副本。"""

    directory = _require_plain_directory(
        root_anchor_history_directory(repo, root_id),
        root=repo.local_dir,
        label="Root anchor history directory",
        create=True,
    )
    path = root_anchor_history_path(repo, root_id=root_id, version=version)
    if path.exists():
        _require_plain_file(path, root=directory, label="Root anchor history")
        if sha256_file(path) != source_sha256:
            raise SoloAIError(
                "Root anchor history version conflicts with the current root content"
            )
        return False
    atomic_copy_file(source_path, path)
    if sha256_file(path) != source_sha256:
        raise SoloAIError("Root anchor history copy did not match the current root")
    return True


def update_root_anchor(
    repo: GitRepo,
    *,
    root_id: str,
    input_path: Path,
    expected_sha256: str,
) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256 or ""):
        raise SoloAIError("Expected root anchor SHA-256 must be lowercase hexadecimal")
    content = _read_plain_input(repo, input_path)
    path = root_anchor_path(repo, root_id)
    with root_anchor_lock(path):
        return _write_root_update_locked(
            repo,
            root_id=root_id,
            content=content,
            expected_sha256=expected_sha256,
        )


def _write_root_update_locked(
    repo: GitRepo,
    *,
    root_id: str,
    content: str,
    expected_sha256: str,
    allow_acceptance_update: bool = False,
) -> dict[str, Any]:
    """在根锚点锁已持有时验证并原子写入一份完整文档。"""

    path = root_anchor_path(repo, root_id)
    current = show_root_anchor(repo, root_id=root_id)
    previous = _validated_fields(str(current["content"]))
    updated = _validated_fields(content)
    if _identity_value(updated["Root ID"]) != root_id:
        raise SoloAIError("Root anchor identity does not match the requested root")
    for field in _IMMUTABLE_FIELDS:
        if updated[field] != previous[field]:
            raise SoloAIError(f"Root anchor field cannot be changed: {field}")
    previous_registry, _ = _linked_children(str(current["content"]))
    updated_registry, _ = _linked_children(content)
    if updated_registry != previous_registry:
        raise SoloAIError("Root anchor child registry is managed only by DWW Start")
    if _REQUEST_COMMENT.findall(
        _metadata_prefix(str(current["content"]))
    ) != _REQUEST_COMMENT.findall(_metadata_prefix(content)):
        raise SoloAIError("Root anchor request identity cannot be changed")
    previous_version = _plan_version(str(current["content"]))
    updated_version = _plan_version(content)
    contract_changed = False
    if previous_version is not None or updated_version is not None:
        if previous_version is None or updated_version is None:
            raise SoloAIError(
                "Structured root anchors cannot silently drop plan versioning"
            )
        _require_structured_plan(str(current["content"]))
        _, _, updated_changes, _ = _require_structured_plan(content)
        previous_contract = _contract_without_progress_or_outcome(
            str(current["content"])
        )
        updated_contract = _contract_without_progress_or_outcome(content)
        contract_changed = previous_contract != updated_contract
        if previous_contract == updated_contract:
            if updated_version != previous_version:
                raise SoloAIError(
                    "Progress or outcome updates must not change plan version"
                )
        else:
            if updated_version != previous_version + 1:
                raise SoloAIError(
                    "Plan changes must increment plan version by exactly one"
                )
            if f"- Version {updated_version}:" not in updated_changes:
                raise SoloAIError(
                    "Plan changes must record a user-confirmed change entry"
                )
        current_acceptance = _section(str(current["content"]), _ACCEPTANCE_HEADING)
        updated_acceptance = _section(content, _ACCEPTANCE_HEADING)
        if contract_changed:
            # 所有会改变结构化目标的入口统一使既有总体验收失效；调用者
            # 不能借 generic update 保留或伪造 accepted/cancelled 状态。
            content = _replace_section(
                content, _ACCEPTANCE_HEADING, _pending_acceptance()
            )
        elif not allow_acceptance_update and updated_acceptance != current_acceptance:
            raise SoloAIError(
                "Only root-anchor accept may change structured overall acceptance"
            )
    raw = content.encode("utf-8")
    sha256 = hashlib.sha256(raw).hexdigest()
    if sha256 == current["sha256"]:
        return {**current, "changed": False}
    if current["sha256"] != expected_sha256:
        raise SoloAIError("Root anchor changed since it was read; fetch it again")
    if contract_changed:
        assert previous_version is not None
        _snapshot_root_anchor_version(
            repo,
            root_id=root_id,
            version=previous_version,
            source_path=path,
            source_sha256=str(current["sha256"]),
        )
    atomic_write_text(path, content)
    return {**show_root_anchor(repo, root_id=root_id), "changed": True}


def amend_root_anchor(
    repo: GitRepo,
    *,
    root_id: str,
    confirmed_plan: str | None,
    change_text: str | None,
    source: str,
    summary: str,
    expected_sha256: str,
    target: str | None = None,
    scope: str | None = None,
    acceptance: str | None = None,
) -> dict[str, Any]:
    """按用户已确认的修订替换或原文附加有效方案，并保留版本化来源记录。"""

    if (
        not source.strip()
        or "\r" in source
        or "\n" in source
        or not summary.strip()
        or "\r" in summary
        or "\n" in summary
    ):
        raise SoloAIError(
            "Plan amendment source and summary must be non-empty single lines"
        )
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256 or ""):
        raise SoloAIError("Expected root anchor SHA-256 must be lowercase hexadecimal")
    if (confirmed_plan is None) == (change_text is None):
        raise SoloAIError(
            "Provide exactly one complete plan or incremental plan change"
        )
    plan_input = confirmed_plan if confirmed_plan is not None else change_text
    assert plan_input is not None
    if not plan_input.strip():
        raise SoloAIError("Confirmed plan input must not be empty")
    if any(
        marker in plan_input
        for marker in (
            _PLAN_START,
            _PLAN_END,
            _CHANGES_START,
            _CHANGES_END,
            _ACCEPTANCE_START,
            _ACCEPTANCE_END,
        )
    ):
        raise SoloAIError(
            "Confirmed plan must not contain reserved root anchor markers"
        )
    path = root_anchor_path(repo, root_id)
    with root_anchor_lock(path):
        current = show_root_anchor(repo, root_id=root_id)
        if current["sha256"] != expected_sha256:
            raise SoloAIError("Root anchor changed since it was read; fetch it again")
        content = str(current["content"])
        version, current_plan, changes, _ = _require_structured_plan(content)
        next_version = version + 1
        effective_plan = (
            confirmed_plan
            if confirmed_plan is not None
            else (
                current_plan
                + f"\n\n---\n\n## User-confirmed amendment (version {next_version})\n\n"
                + str(change_text)
            )
        )
        updated = _replace_metadata_value(
            content, _PLAN_VERSION_FIELD, str(next_version)
        )
        if target is not None:
            updated = _replace_metadata_value(
                updated, "Implementation target", target.strip()
            )
        if scope is not None:
            updated = _replace_metadata_value(updated, "Scope boundary", scope.strip())
        if acceptance is not None:
            updated = _replace_metadata_value(
                updated, "Acceptance criteria", acceptance.strip()
            )
        updated = _replace_section(updated, _PLAN_HEADING, str(effective_plan))
        updated = _replace_section(
            updated,
            _CHANGES_HEADING,
            changes
            + f"\n- Version {next_version}: {summary.strip()}. Source: {source.strip()}",
        )
        updated = _replace_metadata_value(
            updated,
            "Current progress",
            f"plan version {next_version} recorded at {utc_timestamp()}",
        )
        return _write_root_update_locked(
            repo,
            root_id=root_id,
            content=updated,
            expected_sha256=expected_sha256,
        )


def update_root_progress(
    repo: GitRepo, *, root_id: str, progress: str, expected_sha256: str
) -> dict[str, Any]:
    if not progress.strip() or "\r" in progress or "\n" in progress:
        raise SoloAIError("Root progress must be a non-empty single line")
    path = root_anchor_path(repo, root_id)
    with root_anchor_lock(path):
        current = show_root_anchor(repo, root_id=root_id)
        updated = _replace_metadata_value(
            str(current["content"]), "Current progress", progress.strip()
        )
        return _write_root_update_locked(
            repo,
            root_id=root_id,
            content=updated,
            expected_sha256=expected_sha256,
        )


def record_root_acceptance(
    repo: GitRepo,
    *,
    root_id: str,
    status: str,
    evidence: str,
    expected_sha256: str,
) -> dict[str, Any]:
    if status not in {"accepted", "cancelled"}:
        raise SoloAIError("Overall acceptance result must be accepted or cancelled")
    if not evidence.strip():
        raise SoloAIError("Overall acceptance evidence must not be empty")
    path = root_anchor_path(repo, root_id)
    with root_anchor_lock(path):
        current = show_root_anchor(repo, root_id=root_id)
        content = str(current["content"])
        plan_version = _plan_version(content)
        if plan_version is None:
            raise SoloAIError(
                "Legacy root anchors cannot record structured overall acceptance"
            )
        updated = _replace_section(
            content,
            _ACCEPTANCE_HEADING,
            f"- Status: {status}\n- {_ACCEPTANCE_VERSION_FIELD}: {plan_version}\n- Evidence: {evidence.strip()}",
        )
        updated = _replace_metadata_value(
            updated,
            "Current progress",
            f"overall acceptance recorded as {status} at {utc_timestamp()}",
        )
        return _write_root_update_locked(
            repo,
            root_id=root_id,
            content=updated,
            expected_sha256=expected_sha256,
            allow_acceptance_update=True,
        )


def delete_root_anchor(repo: GitRepo, *, root_id: str, locked: bool = False) -> None:
    path = root_anchor_path(repo, root_id)
    if not locked:
        with root_anchor_lock(path):
            delete_root_anchor(repo, root_id=root_id, locked=True)
        return
    _read_plain_root_anchor(repo, path)
    history = root_anchor_history_directory(repo, root_id)
    if history.exists():
        _require_plain_directory(
            history,
            root=repo.local_dir,
            label="Root anchor history directory",
        )
        for child in history.rglob("*"):
            if is_link_or_junction(child):
                raise SoloAIError(
                    "Root anchor history must not contain links or junctions"
                )
    path.unlink()
    if history.exists():
        shutil.rmtree(history)


def register_external_root_child(
    *, root_id: str, root_anchor_file: Path, task_id: str, child_state_path: Path
) -> dict[str, Any]:
    """把一个已落盘的异仓库任务精确登记到唯一根锚点。"""

    if not _TASK_ID_PATTERN.fullmatch(task_id):
        raise SoloAIError("External root child task id is not safe")
    with root_anchor_lock(root_anchor_file):
        current = show_external_root_anchor(root_anchor_file, root_id=root_id)
        record = {
            "task_id": task_id,
            "state_path": str(child_state_path.resolve()),
            "root_anchor_path": str(current["root_anchor_path"]),
        }
        _registered_child_status(
            record,
            root_id=root_id,
            root_anchor_path=Path(str(current["root_anchor_path"])),
        )
        children = list(current["linked_child_tasks"])
        same_task = [item for item in children if item["task_id"] == task_id]
        if same_task:
            if same_task[0] != record:
                raise SoloAIError(
                    "External root child task id is already bound to another state record"
                )
            return {**current, "changed": False}
        content = _render_linked_children(str(current["content"]), [*children, record])
        atomic_write_text(Path(str(current["root_anchor_path"])), content)
        return {
            **show_external_root_anchor(root_anchor_file, root_id=root_id),
            "changed": True,
        }


def _registered_child_task(
    record: dict[str, str], *, root_id: str, root_anchor_path: Path
) -> tuple[dict[str, Any], Path]:
    """读取跨仓库登记任务；任一身份或文件歧义都会使根关闭失败。"""

    expected_root_path = str(root_anchor_path.resolve())
    if record["root_anchor_path"] != expected_root_path:
        raise SoloAIError("Registered child record belongs to a different root anchor")
    state_path = Path(record["state_path"])
    if not state_path.is_absolute() or state_path.name != "state.json":
        raise SoloAIError("Registered child state path is invalid")
    if state_path.parent.name != "solo-ai":
        raise SoloAIError(
            "Registered child state path is outside a DWW local directory"
        )
    raw_path = state_path.absolute()
    common_dir = raw_path.parent.parent
    plain_path = _require_plain_file(
        raw_path, root=common_dir, label="Registered child state"
    )
    raw = plain_path.read_bytes()
    try:
        state = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SoloAIError("Registered child state is not valid UTF-8 JSON") from exc
    if not isinstance(state, dict) or not isinstance(state.get("tasks"), dict):
        raise SoloAIError("Registered child state has an invalid DWW task map")
    task = state["tasks"].get(record["task_id"])
    if not isinstance(task, dict):
        raise SoloAIError("Registered child task is missing from its DWW state")
    if task.get("root_anchor_id") != root_id:
        raise SoloAIError("Registered child task root id is ambiguous")
    if task.get("root_anchor_file") != expected_root_path:
        raise SoloAIError("Registered child task root path is ambiguous")
    status = task.get("status")
    if not isinstance(status, str):
        raise SoloAIError("Registered child task status is invalid")
    return task, raw_path.parent


def _registered_child_status(
    record: dict[str, str], *, root_id: str, root_anchor_path: Path
) -> str:
    task, _ = _registered_child_task(
        record, root_id=root_id, root_anchor_path=root_anchor_path
    )
    return str(task["status"])


def require_candidate_delivery_terminal(
    candidates: dict[str, Any], *, task_id: str, label: str
) -> None:
    """确认根子任务的候选谱系已交付或明确撤回。"""

    if not isinstance(candidates, dict):
        raise SoloAIError(f"{label} candidate state has an invalid candidate map")
    matches = [
        (candidate_id, candidate)
        for candidate_id, candidate in candidates.items()
        if isinstance(candidate_id, str)
        and isinstance(candidate, dict)
        and candidate.get("task_id") == task_id
    ]
    if len(matches) != 1:
        raise SoloAIError(f"{label} candidate identity is missing or ambiguous")

    candidate_id, candidate = matches[0]
    seen: set[str] = set()
    while True:
        if candidate_id in seen:
            raise SoloAIError(f"{label} candidate supersession chain is cyclic")
        seen.add(candidate_id)
        if candidate.get("candidate_id") != candidate_id:
            raise SoloAIError(f"{label} candidate identity changed")
        status = candidate.get("status")
        if status in {"integrated", "withdrawn"}:
            return
        if status != "superseded":
            raise SoloAIError(
                f"{label} candidate is not delivered or withdrawn: "
                f"{candidate_id} ({status})"
            )
        successor_id = candidate.get("superseded_by")
        if not isinstance(successor_id, str) or not successor_id:
            raise SoloAIError(f"{label} superseded candidate has no exact successor")
        successor = candidates.get(successor_id)
        if not isinstance(successor, dict):
            raise SoloAIError(f"{label} candidate successor is missing")
        candidate_id, candidate = successor_id, successor


def _require_registered_child_candidate_delivery(
    record: dict[str, str], *, root_id: str, root_anchor_path: Path
) -> None:
    """跨仓库候选发布不能单独作为根锚点的关闭终态。"""

    task, local_dir = _registered_child_task(
        record, root_id=root_id, root_anchor_path=root_anchor_path
    )
    if task["status"] != "candidate-published":
        return
    candidate_path = local_dir / "candidate-batches.json"
    plain_path = _require_plain_file(
        candidate_path,
        root=local_dir.parent,
        label="Registered child candidate state",
    )
    raw = plain_path.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SoloAIError(
            "Registered child candidate state is not valid UTF-8 JSON"
        ) from exc
    if not isinstance(value, dict):
        raise SoloAIError("Registered child candidate state is invalid")
    require_candidate_delivery_terminal(
        value.get("candidates"),
        task_id=record["task_id"],
        label=f"Registered child {record['task_id']}",
    )


def nonterminal_external_root_children(
    *, root_id: str, root_anchor_file: Path, final_states: set[str]
) -> list[str]:
    """返回仍未终态的异仓库子任务；读取异常必须由调用者失败关闭。"""

    current = show_external_root_anchor(root_anchor_file, root_id=root_id)
    result: list[str] = []
    for record in current["linked_child_tasks"]:
        status = _registered_child_status(
            record, root_id=root_id, root_anchor_path=root_anchor_file
        )
        if status not in final_states:
            result.append(record["task_id"])
        elif status == "candidate-published":
            _require_registered_child_candidate_delivery(
                record, root_id=root_id, root_anchor_path=root_anchor_file
            )
    return result


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
