# /// script
# requires-python = ">=3.11"
# ///

"""Codex PreToolUse guard for develop-with-worktrees.

The hook is deliberately stdlib-only.  It provides a hard PreToolUse denial for
the local Codex tool paths that invoke hooks; it is not an operating-system
sandbox.  State is held under the Git common directory, never in user files.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

WORKFLOW_MARKERS = (
    ".config/wt.toml",
    ".conductor",
    ".parallel-code",
    "scripts/worktree-flow.ps1",
    ".sdd",
)
FINAL_TASK_STATES = {"finished", "abandoned"}
READ_ONLY_GIT_SUBCOMMANDS = {"status", "diff", "log", "show", "branch", "rev-parse"}
DWW_SUBCOMMANDS = {
    "version",
    "init",
    "choose",
    "approve",
    "disable",
    "enable",
    "settings",
    "doctor",
    "start",
    "commit",
    "ready",
    "finish",
    "retarget",
    "plan",
    "verify",
    "status",
    "recover",
    "abandon",
    "resume-in-place",
    "warm-slot",
    "dev",
    "prune-proofs",
    "prune-logs",
    "prune-slot",
    "deinit",
}
DWW_QUARANTINE_SUBCOMMANDS = {"doctor", "status", "plan", "resume-in-place"}
SHELL_CONTROL = (";", "|", "&", "`", "$", "(", ")", "<", ">", "\n", "\r")


def _run_git(cwd: str, *args: str) -> str | None:
    completed = subprocess.run(
        ["git", "-C", cwd, *args],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def git_root(cwd: str) -> Path | None:
    value = _run_git(cwd, "rev-parse", "--show-toplevel")
    return Path(value).resolve() if value else None


def common_dir(root: Path) -> Path | None:
    value = _run_git(str(root), "rev-parse", "--git-common-dir")
    if not value:
        return None
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def state_path(root: Path) -> Path | None:
    common = common_dir(root)
    return common / "solo-ai" / "state.json" if common else None


def guard_state_path(root: Path) -> Path | None:
    common = common_dir(root)
    return common / "solo-ai" / "guard-state.json" if common else None


def read_state(root: Path) -> tuple[dict[str, Any], Path | None]:
    path = state_path(root)
    if path is None:
        return {}, None
    try:
        return json.loads(path.read_text(encoding="utf-8")), path
    except (OSError, json.JSONDecodeError):
        return {}, path


def read_guard_state(root: Path) -> tuple[dict[str, Any], Path | None]:
    path = guard_state_path(root)
    if path is None:
        return {}, None
    try:
        return json.loads(path.read_text(encoding="utf-8")), path
    except (OSError, json.JSONDecodeError):
        return {"schema_version": 1, "quarantines": {}, "alerts": []}, path


def _with_guard_lock(path: Path, update: Any) -> None:
    """只锁 hook 自己的 guard-state，绝不与 lifecycle state.lock 混用。"""
    lock = path.parent / "locks" / "guard-state.lock"
    deadline = time.monotonic() + 2.0
    while True:
        try:
            lock.parent.mkdir(parents=True, exist_ok=True)
            lock.mkdir()
            (lock / "owner.json").write_text(
                json.dumps({"pid": os.getpid(), "started_at": time.time()}),
                encoding="utf-8",
            )
            break
        except FileExistsError:
            try:
                owner = json.loads((lock / "owner.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                owner = {}
            started = owner.get("started_at")
            if isinstance(started, (int, float)) and time.time() - started > 60:
                shutil.rmtree(lock, ignore_errors=True)
                continue
            if time.monotonic() >= deadline:
                return
            time.sleep(0.02)
    try:
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {"schema_version": 1, "quarantines": {}, "alerts": []}
        update(state)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        shutil.rmtree(lock, ignore_errors=True)


def _record_alert(root: Path, *, kind: str, paths: list[str]) -> None:
    _, path = read_guard_state(root)
    if path is None:
        return

    def update(current: dict[str, Any]) -> None:
        alerts = current.setdefault("alerts", [])
        alert = {
            "kind": kind,
            "worktree": str(root),
            "paths": paths[:20],
            "observed_at": int(time.time()),
        }
        if not alerts or alerts[-1] != alert:
            alerts.append(alert)
        del alerts[:-20]

    _with_guard_lock(path, update)


def _quarantine(root: Path, task_id: str, reason: str) -> None:
    _, path = read_guard_state(root)
    if path is None:
        return

    def update(state: dict[str, Any]) -> None:
        quarantines = state.setdefault("quarantines", {})
        if isinstance(quarantines, dict):
            quarantines[task_id] = {"reason": reason, "observed_at": int(time.time())}

    _with_guard_lock(path, update)


def command_from(payload: dict[str, Any]) -> str:
    tool_input = (
        payload.get("tool_input")
        or payload.get("toolInput")
        or payload.get("input")
        or {}
    )
    if not isinstance(tool_input, dict):
        return ""
    value = tool_input.get("command")
    return value if isinstance(value, str) else ""


def _session(payload: dict[str, Any]) -> str:
    value = payload.get("session_id") or payload.get("sessionId")
    return value if isinstance(value, str) else ""


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _dirty_paths(root: Path) -> list[str]:
    output = _run_git(str(root), "status", "--porcelain=v1", "--untracked-files=all")
    if not output:
        return []
    return [line[3:] if len(line) > 3 else line for line in output.splitlines()]


def preference_disabled(root: Path) -> bool:
    common = common_dir(root)
    if common is None:
        return False
    path = common / "solo-ai" / "preferences.json"
    try:
        return not bool(
            json.loads(path.read_text(encoding="utf-8")).get("enabled", True)
        )
    except (OSError, json.JSONDecodeError):
        return False


def task_bypass_active(root: Path, payload: dict[str, Any]) -> bool:
    """仅放行已登记的当前会话，不能把临时选择扩大为仓库级时间窗。"""
    session = _session(payload)
    common = common_dir(root)
    if not session or common is None:
        return False
    path = common / "solo-ai" / "session-overrides.json"
    try:
        overrides = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    session_fingerprint = _fingerprint(session)
    return any(
        isinstance(grant, dict)
        and grant.get("worktree") == str(root.resolve())
        and session_fingerprint in grant.get("sessions", [])
        for grant in overrides.get("grants", [])
    )


def _task_for_worktree(
    state: dict[str, Any], guard: dict[str, Any], root: Path
) -> dict[str, Any] | None:
    target = str(root.resolve())
    quarantines = guard.get("quarantines", {})
    for task in state.get("tasks", {}).values():
        if (
            task.get("worktree") == target
            and task.get("status") not in FINAL_TASK_STATES
        ):
            effective = dict(task)
            guard_quarantine = (
                quarantines.get(str(task.get("id")))
                if isinstance(quarantines, dict)
                else None
            )
            if isinstance(guard_quarantine, dict):
                effective["status"] = "quarantined"
                effective["quarantine_reason"] = guard_quarantine.get("reason")
            return effective
    return None


def _is_valid_in_place(
    root: Path, task: dict[str, Any], payload: dict[str, Any]
) -> tuple[bool, str]:
    if task.get("status") not in {"active", "ready"}:
        return False, "in-place task is not active"
    if not _session(payload) or _fingerprint(_session(payload)) != task.get(
        "session_fingerprint"
    ):
        return False, "Codex session does not own this in-place task"
    branch = _run_git(str(root), "branch", "--show-current")
    head = _run_git(str(root), "rev-parse", "HEAD")
    if branch != task.get("branch"):
        return False, "checked-out branch changed"
    if head != task.get("expected_head"):
        return False, "HEAD changed outside exact-path dww commit"
    return True, ""


def _strict_read_only_bash(command: str) -> bool:
    value = command.strip().lower()
    if not value or any(token in command for token in SHELL_CONTROL):
        return False
    try:
        tokens = shlex.split(value, posix=False)
    except ValueError:
        return False
    if not tokens:
        return False
    first = tokens[0].lower()
    if first in {"ls", "dir", "pwd"}:
        return True
    if first in {"rg", "get-content", "where", "get-command", "test-path"}:
        return len(tokens) > 1
    if first != "git" or len(tokens) < 2:
        return False
    if tokens[1].lower() in READ_ONLY_GIT_SUBCOMMANDS:
        return True
    return (
        tokens[1].lower() == "worktree"
        and len(tokens) >= 3
        and tokens[2].lower() == "list"
    )


def _dww_subcommand(command: str, root: Path) -> str | None:
    """只识别已安装插件的真实 runner、当前工作树和已知子命令。"""
    value = command.strip()
    if not value or any(token in value for token in SHELL_CONTROL):
        return None
    try:
        tokens = shlex.split(value, posix=False)
    except ValueError:
        return None
    runner_index = next(
        (
            index
            for index, argument in enumerate(tokens)
            if argument.strip('"').replace("\\", "/").lower().endswith("/dww.py")
        ),
        None,
    )
    if runner_index is None:
        return None
    runner = Path(tokens[runner_index].strip('"')).resolve()
    expected_runner = (
        Path(__file__).resolve().parents[1]
        / "skills"
        / "develop-with-worktrees"
        / "scripts"
        / "dww.py"
    ).resolve()
    if runner != expected_runner:
        return None
    repo_value: str | None = None
    for index, argument in enumerate(tokens):
        if argument == "--repo" and index + 1 < len(tokens):
            repo_value = tokens[index + 1].strip('"')
            break
        if argument.startswith("--repo="):
            repo_value = argument.split("=", 1)[1].strip('"')
            break
    if repo_value is None or git_root(repo_value) != root.resolve():
        return None
    for argument in tokens[runner_index + 1 :]:
        if argument in DWW_SUBCOMMANDS:
            return argument
    return None


def _is_git_command(command: str) -> bool:
    try:
        tokens = shlex.split(command.strip(), posix=False)
    except ValueError:
        return False
    if not tokens:
        return False
    executable = Path(tokens[0].strip('"')).name.lower()
    if executable in {"git", "git.exe", "git.cmd"}:
        return True
    if executable not in {
        "cmd",
        "cmd.exe",
        "pwsh",
        "pwsh.exe",
        "powershell",
        "powershell.exe",
    }:
        return False
    directives = {"/c", "-command", "-c"}
    for index, argument in enumerate(tokens[:-1]):
        if argument.lower() in directives:
            nested = tokens[index + 1].strip('"').strip().split(maxsplit=1)
            return bool(nested) and Path(nested[0]).name.lower() in {
                "git",
                "git.exe",
                "git.cmd",
            }
    return False


def _deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _context(event: str, message: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": message,
        }
    }


def _session_context(
    root: Path, state: dict[str, Any], guard: dict[str, Any], payload: dict[str, Any]
) -> str:
    session = _session(payload)
    base = (
        "Repository adopts develop-with-worktrees. For ordinary modifications, proactively use Start → exact-path Commit → Ready → Finish in the returned worktree. "
        "The trusted Codex hook hard-denies writes in the current base worktree unless a task has explicit authorization."
    )
    if session:
        base += f" This Codex session identifier is `{session}`. If the user explicitly requests direct changes in this directory for this task, choose current-task first, then do not run the DWW lifecycle."
    active = _task_for_worktree(state, guard, root)
    if active and active.get("mode") == "in-place":
        base += f" Active in-place task: {active.get('id')} (status {active.get('status')})."
    return base


def decide(payload: dict[str, Any]) -> dict[str, Any] | None:
    event = str(payload.get("hook_event_name") or payload.get("hookEventName") or "")
    root = git_root(str(payload.get("cwd") or "."))
    if root is None:
        return None
    if preference_disabled(root):
        return _context(
            event,
            "The user chose normal current-directory development for this repository on this machine. Do not initialize or run develop-with-worktrees for this task; follow the user's explicit direction.",
        )
    if task_bypass_active(root, payload):
        # 用户明确要求“像没安装一样”时，连 PostToolUse 脏基线告警也必须退出，
        # 否则仍会把本次普通开发误报为逃逸写入。
        return None
    markers = [marker for marker in WORKFLOW_MARKERS if (root / marker).exists()]
    if markers:
        return _context(
            event,
            "This repository has a mature worktree/orchestration workflow ("
            + ", ".join(markers)
            + "). develop-with-worktrees defers and must not modify its workflow files.",
        )
    adopted = (root / ".solo-ai" / "config.toml").exists()
    state, _ = read_state(root)
    guard, _ = read_guard_state(root)
    if event == "SessionStart":
        if adopted:
            return _context(event, _session_context(root, state, guard, payload))
        return _context(
            event,
            "Only when the user first intends to modify this unchosen repository, ask exactly:\n\n此仓库怎么修改？\n\n1. 每个任务使用独立目录（推荐）\n   任务互不影响，完成后自动合回。\n\n2. 这一次直接改当前目录\n   只跳过这一次，下次还会询问。\n\n3. 以后都直接改当前目录\n   记住此选择，这个仓库不再询问。\n\n只影响本机，可随时修改。\n\nAsk only this one question. After the user chooses, carry out the matching choice silently without any further confirmation. A child-agent instruction that already includes a one-time delegation code must register that code before asking the user anything.",
        )
    if event not in {"PreToolUse", "PostToolUse"}:
        return None
    tool = str(payload.get("tool_name") or payload.get("toolName") or "")
    command = command_from(payload)
    if event == "PostToolUse":
        task = _task_for_worktree(state, guard, root)
        if task and task.get("mode", "isolated") == "isolated":
            return None
        if task and task.get("mode") == "in-place":
            valid, reason = _is_valid_in_place(root, task, payload)
            if not valid:
                _quarantine(root, str(task.get("id")), reason)
                return _context(
                    event,
                    "Current-worktree identity changed after a tool call. Files were preserved and the task was quarantined: "
                    + reason
                    + ". Use dww doctor; do not continue, reset, clean, or move files automatically.",
                )
            return None
        if adopted and (dirty := _dirty_paths(root)):
            _record_alert(root, kind="unauthorized-dirty-base", paths=dirty)
            return _context(
                event,
                "Detected tracked or untracked changes in a protected base worktree. The files were preserved; do not continue, reset, clean, or move them automatically. Review dww doctor for the recorded paths and ask the user how to proceed.",
            )
        return None
    dww_command = _dww_subcommand(command, root) if tool == "Bash" else None
    if tool == "Bash" and _strict_read_only_bash(command):
        return None
    if not adopted:
        if dww_command in {"init", "choose", "doctor", "status", "version"}:
            return None
        return _deny(
            "Potential repository write is blocked until the user chooses how this repository should be modified. Show the one compact three-choice question, then use the matching trusted dww choose command."
        )
    task = _task_for_worktree(state, guard, root)
    if task and task.get("mode", "isolated") == "isolated":
        return None
    if task and task.get("mode") == "in-place":
        if task.get("status") == "quarantined":
            if dww_command in DWW_QUARANTINE_SUBCOMMANDS:
                return None
            return _deny(
                "In-place task is quarantined. Preserve the worktree and use only dww doctor/status/plan or explicit resume-in-place after manual restoration."
            )
        valid, reason = _is_valid_in_place(root, task, payload)
        if not valid:
            if dww_command == "resume-in-place":
                return None
            _quarantine(root, str(task.get("id")), reason)
            return _deny(
                "Current-worktree authorization is no longer valid; files were preserved and the in-place task was quarantined: "
                + reason
            )
        if dww_command in DWW_SUBCOMMANDS:
            return None
        if tool == "Bash" and _is_git_command(command):
            return _deny(
                "Direct Git state changes are blocked in an in-place task. Use exact-path dww commit; do not run raw git add, commit, switch, reset, clean, merge, or checkout."
            )
        # Current-worktree tasks may run their test/tool commands and edit files;
        # branch and HEAD are checked again after the call.
        return None
    if dww_command in DWW_SUBCOMMANDS:
        return None
    if tool == "Bash" and "dww.py" in command.replace("\\", "/"):
        return _deny(
            "Only the installed develop-with-worktrees lifecycle runner with --repo set to this worktree is allowed. The supplied dww command was not recognized."
        )
    dirty = _dirty_paths(root)
    if dirty:
        _record_alert(root, kind="unauthorized-dirty-base", paths=dirty)
        detail = ", ".join(dirty[:5])
        return _deny(
            "Protected base worktree already has unowned changes. They were preserved; do not continue, reset, clean, or move them automatically. Inspect and ask the user how to proceed. Paths: "
            + detail
        )
    return _deny(
        "Protected base-worktree write blocked. For ordinary work, run dww Start and edit only its returned worktree. If the user explicitly asks to change this directory for this task, first choose current-task and then follow normal development without a DWW lifecycle."
    )


def main() -> int:
    payload: dict[str, Any] = {}
    try:
        raw = json.load(sys.stdin)
        if not isinstance(raw, dict):
            raise TypeError("hook payload must be an object")
        payload = raw
        result = decide(payload)
    except Exception:  # noqa: BLE001 - a guard fault must conservatively deny writes.
        event = str(
            payload.get("hook_event_name") or payload.get("hookEventName") or ""
        )
        message = "develop-with-worktrees guard could not inspect this hook event. Treat a possible write as unsafe and retry only after dww doctor."
        if event == "PreToolUse":
            result = _deny(message)
        elif event in {"SessionStart", "PostToolUse"}:
            result = _context(event, message)
        else:
            print(message, file=sys.stderr)
            return 2
    if result:
        print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
