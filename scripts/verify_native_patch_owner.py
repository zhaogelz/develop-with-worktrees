"""严格核验原生 DWW apply_patch 连续性证据。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Check:
    """一项可追溯的验收结果。"""

    id: str
    status: str
    observation: str
    evidence: str


@dataclass(frozen=True)
class Verification:
    """完整离线验收结果。"""

    status: str
    checks: tuple[Check, ...]
    reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "checks": [asdict(check) for check in self.checks],
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class GitStatus:
    """Git 查询结果，非零退出不能被当作 clean。"""

    lines: tuple[str, ...]
    succeeded: bool
    error: str = ""


@dataclass(frozen=True)
class PathFormsEvidence:
    """owner 原始会话中可复核的补丁路径形式。"""

    sequence: tuple[str, ...]
    evidence: str


def _event_method(event: dict[str, Any]) -> str:
    return str(event.get("method") or "")


def _hook_feedback(run: dict[str, Any]) -> str:
    return " ".join(
        str(entry.get("text") or "")
        for entry in run.get("entries") or []
        if entry.get("kind") == "feedback"
    )


def _normalise_path(path: str) -> str:
    """统一 Windows 事件中可能出现的斜杠和大小写表示。"""

    return os.path.normcase(str(path).replace("/", "\\")).rstrip("\\")


def _item_paths(item: dict[str, Any]) -> set[str]:
    paths: set[str] = set()
    for change in item.get("changes") or []:
        path = change.get("path")
        if path:
            paths.add(_normalise_path(str(path)))
    return paths


def _apply_patch_target(raw_input: str) -> str | None:
    """从原生 custom_tool_call 的 JS 输入中提取唯一补丁目标。"""

    marker = "tools.apply_patch("
    offset = raw_input.find(marker)
    if offset < 0:
        return None
    encoded = raw_input[offset + len(marker) :].lstrip()
    if not encoded.startswith('"'):
        return None
    try:
        patch, _ = json.JSONDecoder().raw_decode(encoded)
    except json.JSONDecodeError:
        return None
    if not isinstance(patch, str):
        return None
    targets = re.findall(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", patch, re.M)
    return targets[0].strip() if len(targets) == 1 else None


def _owner_path_forms(
    session_jsonl: Path | None,
    *,
    owner_thread: str,
    owner_turn: str,
    expected_worktree: str,
) -> PathFormsEvidence | None:
    """从 owner 原始会话 JSONL 读取实际 apply_patch 输入，拒绝派生事件。"""

    if session_jsonl is None:
        return None
    try:
        raw = session_jsonl.read_bytes()
        entries = [
            json.loads(line)
            for line in raw.decode("utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(entries, list):
        return None

    session_ids = {
        str((entry.get("payload") or {}).get("session_id") or "")
        for entry in entries
        if isinstance(entry, dict) and entry.get("type") == "session_meta"
    }
    if session_ids != {owner_thread}:
        return None

    expected_target = _normalise_path(expected_worktree + "\\probe.txt")
    sequence: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("type") != "response_item":
            continue
        item = entry.get("payload") or {}
        if not isinstance(item, dict):
            continue
        metadata = item.get("internal_chat_message_metadata_passthrough") or {}
        if (
            item.get("type") != "custom_tool_call"
            or item.get("name") != "exec"
            or str(metadata.get("turn_id") or "") != owner_turn
        ):
            continue
        target = _apply_patch_target(str(item.get("input") or ""))
        if target is None:
            continue
        if target == "probe.txt":
            sequence.append("relative")
        elif _normalise_path(target) == expected_target:
            sequence.append("absolute")
        else:
            sequence.append("unexpected")

    if not sequence:
        return None
    digest = hashlib.sha256(raw).hexdigest()
    return PathFormsEvidence(
        tuple(sequence), f"owner session JSONL raw custom_tool_call SHA-256:{digest}"
    )


def verify_trace(
    events: list[dict[str, Any]],
    turn_ids: dict[str, str],
    *,
    expected_worktree: str,
    owner_session_jsonl: Path | None = None,
) -> Verification:
    """验证顺序、身份、Hook 拒绝和原始路径证据。"""

    checks: list[Check] = []
    reasons: list[str] = []
    owner_thread = str(turn_ids.get("owner_thread") or "")
    b_thread = str(turn_ids.get("b_thread") or "")
    c_thread = str(turn_ids.get("c_thread") or "")
    owner_turn = str(turn_ids.get("owner_turn") or "")
    b_turn = str(turn_ids.get("b_turn") or "")
    c_turn = str(turn_ids.get("c_turn") or "")

    identities = [(owner_thread, owner_turn), (b_thread, b_turn), (c_thread, c_turn)]
    independent = (
        all(thread_id and turn_id for thread_id, turn_id in identities)
        and len({thread_id for thread_id, _ in identities}) == 3
        and len({turn_id for _, turn_id in identities}) == 3
    )

    def completed_turn_indexes(thread_id: str, turn_id: str) -> list[int]:
        return [
            index
            for index, event in enumerate(events)
            if _event_method(event) == "turn/completed"
            and str((event.get("params") or {}).get("threadId") or "") == thread_id
            and str(
                ((event.get("params") or {}).get("turn") or {}).get("id")
                or (event.get("params") or {}).get("turnId")
                or ""
            )
            == turn_id
            and str(
                ((event.get("params") or {}).get("turn") or {}).get("status")
                or ""
            )
            == "completed"
        ]

    completed_turns = [
        completed_turn_indexes(thread_id, turn_id)
        for thread_id, turn_id in identities
    ]
    valid_turns = independent and all(len(indexes) == 1 for indexes in completed_turns)
    checks.append(
        Check(
            "TURNS",
            "passed" if valid_turns else "failed",
            "owner、B、C 均有 completed turn"
            if valid_turns
            else "owner、B、C 的 thread/turn 身份或 completed 证据不完整",
            "turn/completed",
        )
    )
    if not valid_turns:
        reasons.append("owner、B、C 的真实 thread/turn 完成证据不完整或不独立")

    barrier_completion_candidates = [
        index
        for index, event in enumerate(events)
        if _event_method(event) == "item/completed"
        and ((event.get("params") or {}).get("item") or {}).get("type")
        == "dynamicToolCall"
        and ((event.get("params") or {}).get("item") or {}).get("tool")
        == "dww_test_barrier"
    ]
    barrier_indexes = [
        index
        for index in barrier_completion_candidates
        if str((events[index].get("params") or {}).get("threadId") or "")
        == owner_thread
        and str((events[index].get("params") or {}).get("turnId") or "")
        == owner_turn
        and ((events[index].get("params") or {}).get("item") or {}).get("status")
        == "completed"
        and ((events[index].get("params") or {}).get("item") or {}).get("success")
        is True
    ]
    barrier_item_id = (
        ((events[barrier_indexes[0]].get("params") or {}).get("item") or {}).get("id")
        if len(barrier_indexes) == 1
        else ""
    )
    barrier_started = [
        index
        for index, event in enumerate(events)
        if _event_method(event) == "item/started"
        and ((event.get("params") or {}).get("item") or {}).get("type")
        == "dynamicToolCall"
        and ((event.get("params") or {}).get("item") or {}).get("tool")
        == "dww_test_barrier"
    ]
    barrier_started_ok = [
        index
        for index in barrier_started
        if str((events[index].get("params") or {}).get("threadId") or "")
        == owner_thread
        and str((events[index].get("params") or {}).get("turnId") or "")
        == owner_turn
        and ((events[index].get("params") or {}).get("item") or {}).get("id")
        == barrier_item_id
    ]
    barrier_requests = [
        index
        for index, event in enumerate(events)
        if _event_method(event) == "item/tool/call"
        and (event.get("params") or {}).get("tool") == "dww_test_barrier"
    ]
    barrier_requests_ok = [
        index
        for index in barrier_requests
        if str((events[index].get("params") or {}).get("threadId") or "")
        == owner_thread
        and str((events[index].get("params") or {}).get("turnId") or "")
        == owner_turn
        and (events[index].get("params") or {}).get("callId") == barrier_item_id
    ]
    barrier_started_support_ok = not barrier_started or (
        len(barrier_started) == 1
        and len(barrier_started_ok) == 1
        and len(barrier_indexes) == 1
        and barrier_started_ok[0] < barrier_indexes[0]
    )
    barrier_request_support_ok = not barrier_requests or (
        len(barrier_requests) == 1
        and len(barrier_requests_ok) == 1
        and len(barrier_indexes) == 1
        and barrier_requests_ok[0] < barrier_indexes[0]
    )
    barrier_support_ok = (
        bool(barrier_started or barrier_requests)
        and barrier_started_support_ok
        and barrier_request_support_ok
    )
    checks.append(
        Check(
            "BARRIER",
            "passed"
            if len(barrier_completion_candidates) == 1
            and len(barrier_indexes) == 1
            and barrier_support_ok
            else "failed",
            "存在且仅存在一个同步 barrier，且开始/请求与 owner 关联"
            if len(barrier_completion_candidates) == 1
            and len(barrier_indexes) == 1
            and barrier_support_ok
            else "同步 barrier 缺失或重复",
            "item/started + item/tool/call + item/completed dynamicToolCall",
        )
    )
    if (
        len(barrier_completion_candidates) != 1
        or len(barrier_indexes) != 1
        or not barrier_support_ok
    ):
        reasons.append("同步 barrier 缺失、身份/工具不符或开始请求未关联")

    def denied_hook_indexes(
        thread_id: str, turn_id: str, feedback: str
    ) -> list[int]:
        return [
            index
            for index, event in enumerate(events)
            if _event_method(event) == "hook/completed"
            and str((event.get("params") or {}).get("threadId") or "") == thread_id
            and str((event.get("params") or {}).get("turnId") or "") == turn_id
            and (event.get("params") or {}).get("run", {}).get("eventName")
            == "preToolUse"
            and (event.get("params") or {}).get("run", {}).get("status")
            == "blocked"
            and feedback in _hook_feedback((event.get("params") or {}).get("run", {}))
        ]

    blocked_b = denied_hook_indexes(
        b_thread, b_turn, "does not own this isolated task"
    )
    blocked_c = denied_hook_indexes(
        c_thread, c_turn, "not an active managed worktree"
    )
    b_ok = len(blocked_b) == 1
    c_ok = len(blocked_c) == 1
    checks.extend(
        (
            Check(
                "N03",
                "passed" if b_ok else "failed",
                "B 的 PreToolUse 明确拒绝" if b_ok else "B 的真实拒绝证据缺失",
                "hook/completed",
            ),
            Check(
                "N04",
                "passed" if c_ok else "failed",
                "C 的 PreToolUse 明确拒绝" if c_ok else "C 的真实拒绝证据缺失",
                "hook/completed",
            ),
        )
    )
    if not b_ok or not c_ok:
        reasons.append("B/C 没有与其真实 turn 关联的 PreToolUse 拒绝")

    target_change_indexes = [
        index
        for index, event in enumerate(events)
        if _event_method(event) == "item/completed"
        and ((event.get("params") or {}).get("item") or {}).get("type")
        == "fileChange"
        and ((event.get("params") or {}).get("item") or {}).get("status")
        == "completed"
    ]
    owner_change_indexes = [
        index
        for index in target_change_indexes
        if str((events[index].get("params") or {}).get("threadId") or "")
        == owner_thread
        and str((events[index].get("params") or {}).get("turnId") or "")
        == owner_turn
        and _item_paths((events[index].get("params") or {}).get("item") or {})
        == {_normalise_path(expected_worktree + "\\probe.txt")}
    ]
    foreign_target_change_indexes = [
        index for index in target_change_indexes if index not in owner_change_indexes
    ]
    owner_turn_index = completed_turns[0][0] if len(completed_turns[0]) == 1 else -1
    b_hook_index = blocked_b[0] if b_ok else -1
    b_turn_index = completed_turns[1][0] if len(completed_turns[1]) == 1 else -1
    c_hook_index = blocked_c[0] if c_ok else -1
    c_turn_index = completed_turns[2][0] if len(completed_turns[2]) == 1 else -1
    barrier_index = barrier_indexes[0] if len(barrier_indexes) == 1 else -1
    ordered_owner_changes = (
        len(owner_change_indexes) == 3
        and not foreign_target_change_indexes
        and len(barrier_indexes) == 1
        and owner_change_indexes[0]
        < owner_change_indexes[1]
        < b_hook_index
        < b_turn_index
        < c_hook_index
        < c_turn_index
        < barrier_index
        < owner_change_indexes[2]
        < owner_turn_index
    )
    checks.append(
        Check(
            "OWNER_CHANGES",
            "passed" if ordered_owner_changes else "failed",
            "owner 按 barrier 前两次、barrier 后一次修改目标文件"
            if ordered_owner_changes
            else (
                "目标文件存在非 owner 的成功 fileChange"
                if foreign_target_change_indexes
                else f"owner 目标文件补丁顺序或数量不符：{len(owner_change_indexes)} 次"
            ),
            "item/completed fileChange",
        )
    )
    if not ordered_owner_changes:
        reasons.append(
            "目标文件包含非 owner 成功补丁"
            if foreign_target_change_indexes
            else "owner 三次目标文件补丁证据不完整"
        )

    barrier_support_indexes = [*barrier_started, *barrier_requests]
    barrier_order_ok = (
        bool(barrier_support_indexes)
        and len(owner_change_indexes) >= 2
        and b_ok
        and owner_change_indexes[1] < min(barrier_support_indexes)
        and max(barrier_support_indexes) < b_hook_index
    )
    checks.append(
        Check(
            "BARRIER_ORDER",
            "passed" if barrier_order_ok else "failed",
            "barrier 开始/请求位于 owner 前两次写入之后、B 拒绝之前"
            if barrier_order_ok
            else "barrier 开始/请求缺失或不在 B 拒绝前的等待区间",
            "item/started + item/tool/call ordering",
        )
    )
    if not barrier_order_ok:
        reasons.append("barrier 开始/请求没有覆盖 B/C 的等待区间")

    path_forms = _owner_path_forms(
        owner_session_jsonl,
        owner_thread=owner_thread,
        owner_turn=owner_turn,
        expected_worktree=expected_worktree,
    )
    forms_ok = path_forms is not None and path_forms.sequence == (
        "relative",
        "absolute",
        "absolute",
    )
    checks.append(
        Check(
            "PATH_FORMS",
            "passed" if forms_ok else "unverified",
            "owner 原始会话已记录相对、绝对、绝对三次补丁形式"
            if forms_ok
            else "缺少可绑定 owner thread/turn 的原始补丁输入，无法证明路径形式",
            path_forms.evidence if forms_ok and path_forms else "missing",
        )
    )
    if not forms_ok:
        reasons.append("没有可信的 owner 原始会话相对/绝对路径形式证据")

    if any(check.status == "failed" for check in checks):
        status = "FAIL_ASSERTION" if valid_turns else "INVALID_RUN"
    elif not forms_ok:
        status = "BLOCKED_HOST"
    else:
        status = "PASS"
    return Verification(status, tuple(checks), tuple(reasons))


def _git_status(path: Path) -> GitStatus:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except OSError as error:
        return GitStatus((), False, str(error))
    return GitStatus(
        tuple(line for line in result.stdout.splitlines() if line),
        result.returncode == 0,
        result.stderr.strip(),
    )


def verify_snapshot(repo: Path, worktree: Path) -> tuple[Check, ...]:
    """检查最终文件和 Git 现场，拒绝用模型口头结果替代字节断言。"""

    checks: list[Check] = []
    probe = worktree / "probe.txt"
    base_probe = repo / "probe.txt"
    try:
        worktree_text = probe.read_text(encoding="utf-8")
        worktree_read_error = ""
    except OSError as error:
        worktree_text = ""
        worktree_read_error = str(error)
    try:
        base_text = base_probe.read_text(encoding="utf-8")
        base_read_error = ""
    except OSError as error:
        base_text = ""
        base_read_error = str(error)
    checks.append(
        Check(
            "W_CONTENT",
            "passed"
            if worktree_text == "owner-absolute\nowner-after-denials\n"
            else "failed",
            "W 内容符合最终断言"
            if not worktree_read_error
            and worktree_text == "owner-absolute\nowner-after-denials\n"
            else "W 文件读取失败或内容不符合最终断言"
            + (f": {worktree_read_error}" if worktree_read_error else ""),
            str(probe),
        )
    )
    checks.append(
        Check(
            "R_CONTENT",
            "passed" if base_text == "baseline\n" else "failed",
            "R 保持 baseline"
            if not base_read_error and base_text == "baseline\n"
            else "R 文件读取失败或内容发生非预期变化"
            + (f": {base_read_error}" if base_read_error else ""),
            str(base_probe),
        )
    )
    worktree_status = _git_status(worktree)
    repo_status = _git_status(repo)
    worktree_paths = {
        Path(line[3:]).as_posix()
        for line in worktree_status.lines
        if len(line) >= 4
    }
    repo_paths = {
        Path(line[3:]).as_posix() for line in repo_status.lines if len(line) >= 4
    }
    git_clean = (
        worktree_status.succeeded
        and repo_status.succeeded
        and worktree_paths == {"probe.txt"}
        and not repo_paths
    )
    git_observation = (
        "W 仅 probe.txt 修改且 R Git clean"
        if git_clean
        else "Git 查询失败或发现不允许的 porcelain 状态"
    )
    if not worktree_status.succeeded or not repo_status.succeeded:
        errors = [
            error
            for error in (worktree_status.error, repo_status.error)
            if error
        ]
        if errors:
            git_observation += ": " + " | ".join(errors)
    checks.append(
        Check(
            "NO_INTRUSION",
            "passed" if git_clean else "failed",
            git_observation,
            "git status --porcelain",
        )
    )
    return tuple(checks)


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--turn-ids", type=Path, required=True)
    parser.add_argument("--owner-session-jsonl", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--worktree", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args(argv)
    events = _load_json(args.events)
    turn_ids = _load_json(args.turn_ids)
    if not isinstance(events, list) or not isinstance(turn_ids, dict):
        raise SystemExit("事件或 turn-ids 不是预期 JSON")
    trace = verify_trace(
        events,
        turn_ids,
        expected_worktree=str(args.worktree.resolve()),
        owner_session_jsonl=args.owner_session_jsonl,
    )
    snapshot = verify_snapshot(args.repo.resolve(), args.worktree.resolve())
    checks = trace.checks + snapshot
    status = trace.status
    if any(check.status == "failed" for check in snapshot):
        status = "INVALID_RUN" if trace.status == "INVALID_RUN" else "FAIL_ASSERTION"
    result = Verification(status, checks, trace.reasons).as_dict()
    args.result.parent.mkdir(parents=True, exist_ok=True)
    args.result.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
