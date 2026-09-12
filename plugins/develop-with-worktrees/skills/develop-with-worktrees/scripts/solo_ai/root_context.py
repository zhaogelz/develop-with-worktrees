from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .repo import GitRepo
from .util import (
    DirectoryLock,
    SoloAIError,
    atomic_write_text,
    is_link_or_junction,
    utc_timestamp,
)

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
_CHILDREN_START = "<!-- dww-root-children:start -->"
_CHILDREN_END = "<!-- dww-root-children:end -->"
_CHILD_RECORD = re.compile(r"^- Child state: (?P<value>[^\r\n]+)$")
_TASK_ID_PATTERN = re.compile(r"task-[A-Za-z0-9][A-Za-z0-9-]*\Z")
MAX_CHILD_STATE_BYTES = 2 * 1024 * 1024
MAX_CHILD_CANDIDATE_BATCHES_BYTES = 2 * 1024 * 1024


def root_anchor_path(repo: GitRepo, root_id: str) -> Path:
    if not _ROOT_ID_PATTERN.fullmatch(root_id):
        raise SoloAIError("Root anchor id is not safe")
    return repo.local_dir / "root-anchors" / f"{root_id}.md"


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


def _read_external_root_anchor(path: Path, *, root_id: str) -> tuple[bytes, str]:
    raw_path, common_dir = _external_root_anchor_path(path, root_id=root_id)
    raw = _require_plain_file(
        raw_path, root=common_dir, label="External root anchor"
    ).read_bytes()
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
    return {
        "root_id": root_id,
        "root_anchor_path": str(path.resolve()),
        "content": content,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "linked_child_tasks": children,
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
    return _shown_root_anchor(path=path, root_id=root_id, raw=raw, content=content)


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
    if len(content.encode("utf-8")) > MAX_ROOT_ANCHOR_BYTES:
        raise SoloAIError("Root anchor exceeds the 64 KiB safety limit")
    path = root_anchor_path(repo, root_id)
    with root_anchor_lock(path):
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
        raw = content.encode("utf-8")
        sha256 = hashlib.sha256(raw).hexdigest()
        if sha256 == current["sha256"]:
            return {**current, "changed": False}
        if current["sha256"] != expected_sha256:
            raise SoloAIError("Root anchor changed since it was read; fetch it again")
        atomic_write_text(path, content)
        return {**show_root_anchor(repo, root_id=root_id), "changed": True}


def delete_root_anchor(repo: GitRepo, *, root_id: str, locked: bool = False) -> None:
    path = root_anchor_path(repo, root_id)
    if not locked:
        with root_anchor_lock(path):
            delete_root_anchor(repo, root_id=root_id, locked=True)
        return
    _read_plain_root_anchor(repo, path)
    path.unlink()


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
    if len(raw) > MAX_CHILD_STATE_BYTES:
        raise SoloAIError("Registered child state exceeds the 2 MiB safety limit")
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
    if len(raw) > MAX_CHILD_CANDIDATE_BATCHES_BYTES:
        raise SoloAIError(
            "Registered child candidate state exceeds the 2 MiB safety limit"
        )
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
