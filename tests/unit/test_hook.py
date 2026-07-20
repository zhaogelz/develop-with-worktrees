from __future__ import annotations

import importlib.util
from pathlib import Path

from conftest import git


HOOK_PATH = Path(__file__).parents[2] / "hooks" / "worktree_guard.py"
SPEC = importlib.util.spec_from_file_location("worktree_guard", HOOK_PATH)
assert SPEC and SPEC.loader
HOOK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HOOK)


def test_hook_defers_to_existing_workflow_without_writing(git_repo: Path) -> None:
    marker = git_repo / "scripts" / "worktree-flow.ps1"
    marker.parent.mkdir()
    marker.write_text("# existing\n", encoding="utf-8")
    before = git(git_repo, "status", "--porcelain")
    result = HOOK.message(
        {
            "cwd": str(git_repo),
            "hook_event_name": "PreToolUse",
            "tool_name": "apply_patch",
            "tool_input": {"patch": "x"},
        }
    )
    assert "must defer" in result
    assert git(git_repo, "status", "--porcelain") == before
    assert not (git_repo / ".solo-ai").exists()


def test_hook_warns_for_unadopted_write_but_not_read(git_repo: Path) -> None:
    write = HOOK.message(
        {
            "cwd": str(git_repo),
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "git add README.md"},
        }
    )
    read = HOOK.message(
        {
            "cwd": str(git_repo),
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "git status --short"},
        }
    )
    assert "one explicit acceptance" in write
    assert read is None
