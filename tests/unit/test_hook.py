from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

from conftest import git
from solo_ai.cli import _doctor
from solo_ai.config import CommandSpec
from solo_ai.lifecycle import initialize, resume_in_place, start
from solo_ai.repo import GitRepo
from solo_ai.state import StateStore

HOOK_PATH = (
    Path(__file__).parents[2]
    / "plugins"
    / "develop-with-worktrees"
    / "hooks"
    / "worktree_guard.py"
)
RUNNER_PATH = (
    Path(__file__).parents[2]
    / "plugins"
    / "develop-with-worktrees"
    / "skills"
    / "develop-with-worktrees"
    / "scripts"
    / "dww.py"
)
SPEC = importlib.util.spec_from_file_location("worktree_guard", HOOK_PATH)
assert SPEC and SPEC.loader
HOOK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HOOK)


def _payload(
    repo: Path, *, tool: str, command: str = "", session: str = ""
) -> dict[str, object]:
    tool_input = {"command": command} if tool == "Bash" else {"patch": "x"}
    return {
        "cwd": str(repo),
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "session_id": session,
        "tool_input": tool_input,
    }


def _initialized(path: Path) -> GitRepo:
    repo = GitRepo(path)
    result = initialize(
        repo,
        slots=1,
        commands=[CommandSpec(("git", "diff", "--check", "main...HEAD"))],
        accept=True,
        accept_static_only=False,
    )
    assert result["decision"] == "adopted"
    return repo


def test_hook_defers_to_existing_workflow_without_writing(git_repo: Path) -> None:
    marker = git_repo / "scripts" / "worktree-flow.ps1"
    marker.parent.mkdir()
    marker.write_text("# existing\n", encoding="utf-8")
    before = git(git_repo, "status", "--porcelain")
    result = HOOK.decide(_payload(git_repo, tool="apply_patch"))
    assert result is not None
    assert "defers" in result["hookSpecificOutput"]["additionalContext"]
    assert git(git_repo, "status", "--porcelain") == before
    assert not (git_repo / ".solo-ai").exists()


def test_hook_denies_unadopted_write_and_permits_strict_read(git_repo: Path) -> None:
    write = HOOK.decide(_payload(git_repo, tool="Bash", command="git add README.md"))
    read = HOOK.decide(_payload(git_repo, tool="Bash", command="git status --short"))
    compound_read_then_write = HOOK.decide(
        _payload(git_repo, tool="Bash", command="git status && Remove-Item README.md")
    )
    alias_like = HOOK.decide(
        _payload(git_repo, tool="Bash", command="git statusx --short")
    )
    assert write["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert read is None
    assert (
        compound_read_then_write["hookSpecificOutput"]["permissionDecision"] == "deny"
    )
    assert alias_like["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_hook_hard_denies_adopted_base_write_and_allows_isolated_task(
    git_repo: Path,
) -> None:
    repo = _initialized(git_repo)
    denied = HOOK.decide(_payload(git_repo, tool="apply_patch"))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"

    task = start(repo, name="isolated")
    allowed = HOOK.decide(_payload(Path(task["worktree"]), tool="apply_patch"))
    assert allowed is None


def test_hook_allows_only_bound_in_place_session_and_quarantines_mismatch(
    git_repo: Path,
) -> None:
    repo = _initialized(git_repo)
    task = start(repo, name="current state", in_place=True, session_id="session-a")

    assert (
        HOOK.decide(_payload(git_repo, tool="apply_patch", session="session-a")) is None
    )
    denied = HOOK.decide(_payload(git_repo, tool="apply_patch", session="session-b"))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert StateStore(repo).task(task["id"])["status"] == "quarantined"

    resumed = resume_in_place(
        repo,
        task_id=task["id"],
        session_id="session-c",
        confirm=f"{task['id']}:main:{task['expected_head']}",
    )
    assert resumed["status"] == "active"
    assert (
        HOOK.decide(_payload(git_repo, tool="apply_patch", session="session-c")) is None
    )


def test_hook_rejects_raw_git_state_changes_in_bound_in_place_task(
    git_repo: Path,
) -> None:
    repo = _initialized(git_repo)
    task = start(repo, name="current state", in_place=True, session_id="session-a")

    for status in ("active", "ready"):
        StateStore(repo).update_task(task["id"], status=status)
        for command in (
            "git add .",
            "git commit -m bypass",
            "git switch other",
            "git reset --hard",
            "git clean -fd",
            "cmd /c git commit -m bypass",
            'pwsh -Command "git commit -m bypass"',
        ):
            denied = HOOK.decide(
                _payload(git_repo, tool="Bash", command=command, session="session-a")
            )
            assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_hook_allows_bound_dww_finish_after_ready(git_repo: Path) -> None:
    repo = _initialized(git_repo)
    task = start(
        repo, name="ready current state", in_place=True, session_id="session-a"
    )
    StateStore(repo).update_task(task["id"], status="ready")

    allowed = HOOK.decide(
        _payload(
            git_repo,
            tool="Bash",
            session="session-a",
            command=f'uv run --script "{RUNNER_PATH}" --repo "{git_repo}" finish --task {task["id"]} --lease private',
        )
    )
    assert allowed is None


def test_hook_allows_explicit_resume_after_a_stalled_codex_task(
    git_repo: Path,
) -> None:
    repo = _initialized(git_repo)
    task = start(repo, name="stalled current state", in_place=True, session_id="old")

    allowed = HOOK.decide(
        _payload(
            git_repo,
            tool="Bash",
            session="new",
            command=(
                f'uv run --script "{RUNNER_PATH}" --repo "{git_repo}" '
                f"resume-in-place --task {task['id']} "
                f"--confirm {task['id']}:main:{task['expected_head']} --session new"
            ),
        )
    )

    assert allowed is None
    assert StateStore(repo).task(task["id"])["status"] == "active"


def test_hook_posttooluse_quarantines_clean_head_drift(git_repo: Path) -> None:
    repo = _initialized(git_repo)
    task = start(repo, name="current state", in_place=True, session_id="session-a")
    git(git_repo, "commit", "--allow-empty", "-m", "external head drift")

    result = HOOK.decide(
        {
            **_payload(git_repo, tool="apply_patch", session="session-a"),
            "hook_event_name": "PostToolUse",
        }
    )
    assert result["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert StateStore(repo).task(task["id"])["status"] == "quarantined"


def test_hook_allows_only_a_real_dww_runner_for_this_worktree(git_repo: Path) -> None:
    _initialized(git_repo)
    valid = HOOK.decide(
        _payload(
            git_repo,
            tool="Bash",
            command=f'uv run --script "{RUNNER_PATH}" --repo "{git_repo}" start --name task',
        )
    )
    invalid = HOOK.decide(
        _payload(
            git_repo,
            tool="Bash",
            command=f'python x/dww.py --repo "{git_repo}" start --name task',
        )
    )
    assert valid is None
    assert invalid["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_hook_dirty_base_alert_is_visible_in_doctor(git_repo: Path) -> None:
    repo = _initialized(git_repo)
    (git_repo / "escaped.txt").write_text("preserve\n", encoding="utf-8")
    HOOK.decide(
        {
            **_payload(git_repo, tool="apply_patch"),
            "hook_event_name": "PostToolUse",
        }
    )

    alerts = _doctor(repo)["guard_alerts"]
    assert alerts[-1]["kind"] == "unauthorized-dirty-base"
    assert alerts[-1]["paths"] == ["escaped.txt"]
    assert "lease" not in json.dumps(alerts)
    assert "session" not in json.dumps(alerts)


def test_hook_script_emits_official_pretooluse_deny_protocol(git_repo: Path) -> None:
    completed = subprocess.run(
        ["uv", "run", "--script", str(HOOK_PATH)],
        input=json.dumps(_payload(git_repo, tool="apply_patch")),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    output = result["hookSpecificOutput"]
    assert set(output) == {
        "hookEventName",
        "permissionDecision",
        "permissionDecisionReason",
    }
    assert output["hookEventName"] == "PreToolUse"
    assert output["permissionDecision"] == "deny"
    assert output["permissionDecisionReason"]
