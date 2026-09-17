from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from solo_ai.config import CommandSpec
from solo_ai.lifecycle import create_root_task_anchor, initialize, start
from solo_ai.repo import GitRepo
from solo_ai.state import StateStore

ROOT = Path(__file__).parents[2]
HOOKS = ROOT / "plugins" / "develop-with-worktrees" / "hooks" / "hooks.json"
PLUGIN_ROOT = HOOKS.parent.parent
DWW_RUNNER = PLUGIN_ROOT / "skills" / "develop-with-worktrees" / "scripts" / "dww.py"


def _hook_command() -> str:
    definition = json.loads(HOOKS.read_text(encoding="utf-8"))
    return str(definition["hooks"]["SessionStart"][0]["hooks"][0]["commandWindows"])


def _run_windows_hook(
    command: str, payload: dict[str, object]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["pwsh", "-NoProfile", "-Command", command],
        input=json.dumps(payload, ensure_ascii=False),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=30,
        env={**os.environ, "PLUGIN_ROOT": str(PLUGIN_ROOT)},
    )


@pytest.mark.skipif(os.name != "nt", reason="commandWindows only runs on Windows")
def test_windows_hook_command_enforces_and_clears_root_recovery_marker(
    git_repo: Path,
) -> None:
    """用 hooks.json 的 commandWindows 复现新会话关键链路，避免只测 Python 函数。"""

    repo = GitRepo(git_repo)
    initialized = initialize(
        repo,
        slots=1,
        commands=[CommandSpec(("git", "diff", "--check", "main...HEAD"))],
        accept=True,
        accept_static_only=False,
    )
    assert initialized["decision"] == "adopted"
    plan = git_repo / "final-plan.md"
    plan.write_text(
        "# Final plan\n\n- Restore the current objective.\n", encoding="utf-8"
    )
    root = create_root_task_anchor(
        repo,
        purpose="verify the Windows hook command",
        target="deny writes until a recovered root is refreshed",
        scope="one managed child and its exact hook payloads",
        acceptance="the root refresh clears exactly one session recovery marker",
        plan_input_path=plan,
        plan_source="user confirmed the final plan",
        request_id="windows-hook-objective-protocol",
    )
    task = start(repo, name="windows hook child", root_anchor_id=str(root["root_id"]))
    worktree = Path(str(task["worktree"]))
    command = _hook_command()

    started = _run_windows_hook(
        command,
        {
            "cwd": str(worktree),
            "hook_event_name": "SessionStart",
            "session_id": "windows-objective-session",
        },
    )
    assert started.returncode == 0, started.stderr
    assert json.loads(started.stdout)["hookSpecificOutput"]["additionalContext"]
    assert StateStore(repo).root_context_refresh_required(str(task["id"])) is not None

    protected_patch = {
        "cwd": str(worktree),
        "hook_event_name": "PreToolUse",
        "tool_name": "apply_patch",
        "session_id": "windows-objective-session",
        "tool_input": {
            "patch": "*** Begin Patch\n*** Update File: README.md\n@@\n-old\n+new\n*** End Patch"
        },
    }
    denied = _run_windows_hook(command, protected_patch)
    assert denied.returncode == 0, denied.stderr
    assert (
        json.loads(denied.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    )

    read_only = _run_windows_hook(
        command,
        {
            "cwd": str(worktree),
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "session_id": "windows-objective-session",
            "tool_input": {"command": "git status --short"},
        },
    )
    assert read_only.returncode == 0, read_only.stderr
    assert read_only.stdout == ""

    refresh_command = (
        f'uv run --script "{DWW_RUNNER}" --repo "{worktree}" '
        f"anchor refresh-root --task {task['id']} --lease {task['lease']}"
    )
    refresh_permission = _run_windows_hook(
        command,
        {
            "cwd": str(worktree),
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "session_id": "windows-objective-session",
            "tool_input": {"command": refresh_command},
        },
    )
    assert refresh_permission.returncode == 0, refresh_permission.stderr
    assert refresh_permission.stdout == ""

    refreshed = subprocess.run(
        [
            sys.executable,
            str(DWW_RUNNER),
            "--repo",
            str(worktree),
            "--json",
            "anchor",
            "refresh-root",
            "--task",
            str(task["id"]),
            "--lease",
            str(task["lease"]),
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert refreshed.returncode == 0, refreshed.stderr
    assert json.loads(refreshed.stdout)["result"]["root_id"] == root["root_id"]
    assert StateStore(repo).root_context_refresh_required(str(task["id"])) is None

    allowed = _run_windows_hook(command, protected_patch)
    assert allowed.returncode == 0, allowed.stderr
    assert allowed.stdout == ""
