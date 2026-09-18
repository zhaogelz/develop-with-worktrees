"""严格核验原生 DWW apply_patch 连续性证据。"""

from __future__ import annotations

import argparse
import json
import os
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


def _event_method(event: dict[str, Any]) -> str:
    return str(event.get("method") or "")


def _turn_statuses(events: list[dict[str, Any]]) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for event in events:
        if _event_method(event) != "turn/completed":
            continue
        params = event.get("params") or {}
        turn = params.get("turn") or {}
        turn_id = turn.get("id") or params.get("turnId")
        if turn_id:
            statuses[str(turn_id)] = str(turn.get("status") or "unknown")
    return statuses


def _hook_runs(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for event in events:
        if _event_method(event) != "hook/completed":
            continue
        params = event.get("params") or {}
        source_run = params.get("run") or {}
        run = dict(source_run)
        # App Server 将身份放在 params，而不是嵌套的 run；归一化到副本后再判定。
        run.setdefault("threadId", params.get("threadId"))
        run.setdefault("turnId", params.get("turnId"))
        if run.get("eventName") == "preToolUse":
            runs.append(run)
    return runs


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


def _path_forms(events: list[dict[str, Any]]) -> list[str]:
    """只接受控制器明确记录的原始路径形式，不能从归一化结果猜测。"""

    for event in events:
        if _event_method(event) != "dww/nativePathForms":
            continue
        forms = (event.get("params") or {}).get("forms")
        if isinstance(forms, list) and all(
            form in {"relative", "absolute"} for form in forms
        ):
            return [str(form) for form in forms]
    return []


def verify_trace(
    events: list[dict[str, Any]],
    turn_ids: dict[str, str],
    *,
    expected_worktree: str,
) -> Verification:
    """验证顺序、身份、Hook 拒绝和原始路径证据。"""

    checks: list[Check] = []
    reasons: list[str] = []
    turns = _turn_statuses(events)
    hooks = _hook_runs(events)
    owner_turn = str(turn_ids.get("owner_turn") or "")
    b_turn = str(turn_ids.get("b_turn") or "")
    c_turn = str(turn_ids.get("c_turn") or "")

    valid_turns = all(
        turn_id and turns.get(turn_id) == "completed"
        for turn_id in (owner_turn, b_turn, c_turn)
    )
    checks.append(
        Check(
            "TURNS",
            "passed" if valid_turns else "failed",
            "owner、B、C 均有 completed turn"
            if valid_turns
            else "缺少 owner/B/C completed turn",
            "turn/completed",
        )
    )
    if not valid_turns:
        reasons.append("owner、B、C 的真实 turn 完成证据不完整")

    barrier_indexes = [
        index
        for index, event in enumerate(events)
        if _event_method(event) == "item/completed"
        and ((event.get("params") or {}).get("item") or {}).get("type")
        == "dynamicToolCall"
    ]
    checks.append(
        Check(
            "BARRIER",
            "passed" if len(barrier_indexes) == 1 else "failed",
            "存在且仅存在一个同步 barrier"
            if len(barrier_indexes) == 1
            else "同步 barrier 缺失或重复",
            "item/completed dynamicToolCall",
        )
    )
    if len(barrier_indexes) != 1:
        reasons.append("同步 barrier 证据不唯一")

    blocked_b = [
        run
        for run in hooks
        if run.get("status") == "blocked"
        and "does not own this isolated task" in _hook_feedback(run)
    ]
    blocked_c = [
        run
        for run in hooks
        if run.get("status") == "blocked"
        and "not an active managed worktree" in _hook_feedback(run)
    ]
    b_ok = any(str(run.get("turnId") or "") == b_turn for run in blocked_b)
    c_ok = any(str(run.get("turnId") or "") == c_turn for run in blocked_c)
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

    owner_change_indexes = [
        index
        for index, event in enumerate(events)
        if _event_method(event) == "item/completed"
        and ((event.get("params") or {}).get("item") or {}).get("type") == "fileChange"
        and ((event.get("params") or {}).get("item") or {}).get("status") == "completed"
        and _item_paths((event.get("params") or {}).get("item") or {})
        == {_normalise_path(expected_worktree + "\\probe.txt")}
    ]
    ordered_owner_changes = (
        len(owner_change_indexes) == 3
        and len(barrier_indexes) == 1
        and owner_change_indexes[1] < barrier_indexes[0] < owner_change_indexes[2]
    )
    checks.append(
        Check(
            "OWNER_CHANGES",
            "passed" if ordered_owner_changes else "failed",
            "owner 按 barrier 前两次、barrier 后一次修改目标文件"
            if ordered_owner_changes
            else f"owner 目标文件补丁顺序或数量不符：{len(owner_change_indexes)} 次",
            "item/completed fileChange",
        )
    )
    if not ordered_owner_changes:
        reasons.append("owner 三次目标文件补丁证据不完整")

    forms = _path_forms(events)
    forms_ok = forms == ["relative", "absolute"]
    checks.append(
        Check(
            "PATH_FORMS",
            "passed" if forms_ok else "unverified",
            "原始补丁形式已由控制器明确记录"
            if forms_ok
            else "宿主只提供归一化目标路径，无法证明相对/绝对输入形式",
            "dww/nativePathForms" if forms_ok else "missing",
        )
    )
    if not forms_ok:
        reasons.append("宿主事件没有可信的原始相对/绝对路径形式证据")

    if any(check.status == "failed" for check in checks):
        status = "FAIL_ASSERTION" if valid_turns else "INVALID_RUN"
    elif not forms_ok:
        status = "BLOCKED_HOST"
    else:
        status = "PASS"
    return Verification(status, tuple(checks), tuple(reasons))


def _git_status(path: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(path), "status", "--porcelain"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return [line for line in result.stdout.splitlines() if line]


def verify_snapshot(repo: Path, worktree: Path) -> tuple[Check, ...]:
    """检查最终文件和 Git 现场，拒绝用模型口头结果替代字节断言。"""

    checks: list[Check] = []
    probe = worktree / "probe.txt"
    base_probe = repo / "probe.txt"
    worktree_text = probe.read_text(encoding="utf-8") if probe.is_file() else ""
    base_text = base_probe.read_text(encoding="utf-8") if base_probe.is_file() else ""
    checks.append(
        Check(
            "W_CONTENT",
            "passed"
            if worktree_text == "owner-absolute\nowner-after-denials\n"
            else "failed",
            "W 内容符合最终断言"
            if worktree_text == "owner-absolute\nowner-after-denials\n"
            else "W 内容不符合最终断言",
            str(probe),
        )
    )
    checks.append(
        Check(
            "R_CONTENT",
            "passed" if base_text == "baseline\n" else "failed",
            "R 保持 baseline" if base_text == "baseline\n" else "R 内容发生非预期变化",
            str(base_probe),
        )
    )
    worktree_paths = {
        Path(line[3:]).as_posix() for line in _git_status(worktree) if len(line) >= 4
    }
    repo_paths = {
        Path(line[3:]).as_posix() for line in _git_status(repo) if len(line) >= 4
    }
    checks.append(
        Check(
            "NO_INTRUSION",
            "passed"
            if "intruder-W.txt" not in worktree_paths and "probe.txt" not in repo_paths
            else "failed",
            "没有入侵文件且 R Git clean"
            if "intruder-W.txt" not in worktree_paths and "probe.txt" not in repo_paths
            else "发现入侵文件或 R 非 clean",
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
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--worktree", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args(argv)
    events = _load_json(args.events)
    turn_ids = _load_json(args.turn_ids)
    if not isinstance(events, list) or not isinstance(turn_ids, dict):
        raise SystemExit("事件或 turn-ids 不是预期 JSON")
    trace = verify_trace(
        events, turn_ids, expected_worktree=str(args.worktree.resolve())
    )
    snapshot = verify_snapshot(args.repo.resolve(), args.worktree.resolve())
    checks = trace.checks + snapshot
    status = trace.status
    if any(check.status == "failed" for check in snapshot):
        status = "FAIL_ASSERTION"
    result = Verification(status, checks, trace.reasons).as_dict()
    args.result.parent.mkdir(parents=True, exist_ok=True)
    args.result.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
