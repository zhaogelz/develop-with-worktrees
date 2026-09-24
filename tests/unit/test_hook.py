from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

from conftest import declare_delegated_adapter, git
from solo_ai.cli import _doctor, _parser
from solo_ai.command_contract import TOP_LEVEL_COMMANDS
from solo_ai.config import CommandSpec
from solo_ai.lifecycle import (
    abandon,
    choose,
    create_root_task_anchor,
    handoff,
    initialize,
    refresh_root_context,
    resume_in_place,
    start,
)
from solo_ai.repo import GitRepo
from solo_ai.state import LEGACY_STATE_SCHEMA, StateStore

HOOK_PATH = (
    Path(__file__).parents[2]
    / "plugins"
    / "develop-with-worktrees"
    / "hooks"
    / "worktree_guard.py"
)
HOOK_DEFINITION_PATH = HOOK_PATH.parent / "hooks.json"
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
    repo: Path,
    *,
    tool: str,
    command: str = "",
    patch: str = "*** Begin Patch\n*** Update File: README.md\n@@\n-old\n+new\n*** End Patch",
    session: str = "",
    patch_transport: str = "command",
) -> dict[str, object]:
    if tool == "Bash":
        tool_input: object = {"command": command}
    elif patch_transport == "command":
        tool_input = {"command": patch}
    elif patch_transport == "patch":
        tool_input = {"patch": patch}
    elif patch_transport == "raw":
        tool_input = patch
    else:
        raise ValueError(f"unknown patch transport: {patch_transport}")
    return {
        "cwd": str(repo),
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "session_id": session,
        "tool_input": tool_input,
    }


def _initialized(path: Path, *, slots: int = 1) -> GitRepo:
    repo = GitRepo(path)
    result = initialize(
        repo,
        slots=slots,
        commands=[CommandSpec(("git", "diff", "--check", "main...HEAD"))],
        accept=True,
        accept_static_only=False,
    )
    assert result["decision"] == "adopted"
    StateStore(repo).mutate(
        lambda state: state.update(schema_version=LEGACY_STATE_SCHEMA)
    )
    return repo


def test_dww_subcommand_accepts_single_double_and_unquoted_paths(
    git_repo: Path,
) -> None:
    for quote in ("", "'", '"'):
        runner = f"{quote}{RUNNER_PATH}{quote}"
        repo_value = f"{quote}{git_repo}{quote}"
        command = f"uv run --script {runner} --repo {repo_value} --json status"
        assert HOOK._dww_subcommand(command, git_repo) == "status"


def test_dww_subcommand_accepts_repo_equals_and_read_only_help(
    git_repo: Path,
) -> None:
    command = f"uv run --script '{RUNNER_PATH}' --json --repo='{git_repo}' --help"
    assert HOOK._dww_subcommand(command, git_repo) == "help"


def test_dww_subcommand_rejects_fake_runner_and_argument_text(
    git_repo: Path,
) -> None:
    fake = git_repo / "dww.py"
    assert (
        HOOK._dww_subcommand(
            f"uv run --script '{fake}' --repo '{git_repo}' status", git_repo
        )
        is None
    )
    assert (
        HOOK._dww_subcommand(
            f"uv run --script '{RUNNER_PATH}' --repo '{git_repo}' --name choose",
            git_repo,
        )
        is None
    )


def test_dww_parse_error_does_not_repeat_repository_choice_prompt(
    git_repo: Path,
) -> None:
    command = f"uv run --script '{RUNNER_PATH}' --repo '{git_repo}' --unknown"
    result = HOOK.decide(_payload(git_repo, tool="Bash", command=command))
    assert result is not None
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "invocation could not be verified" in reason
    assert "three-choice" not in reason

    valid_start = (
        f"uv run --script '{RUNNER_PATH}' --repo '{git_repo}' start --name test"
    )
    choice = HOOK.decide(_payload(git_repo, tool="Bash", command=valid_start))
    assert choice is not None
    assert "three-choice" in choice["hookSpecificOutput"]["permissionDecisionReason"]


def test_doctor_describes_stable_hook_trust_without_repeated_user_work(
    git_repo: Path,
) -> None:
    report = _doctor(GitRepo(git_repo))

    assert "exact hook definition" in report["hook_trust"]
    assert "need no repeated review" in report["hook_trust"]
    assert "ask once" in report["hook_trust"]
    assert "Run /hooks" not in report["hook_trust"]


def test_windows_hook_command_uses_the_host_powershell_environment(
    git_repo: Path,
) -> None:
    config = json.loads(HOOK_DEFINITION_PATH.read_text(encoding="utf-8"))
    command = config["hooks"]["SessionStart"][0]["hooks"][0]["commandWindows"]
    assert "%PLUGIN_ROOT%" not in command
    assert "$env:PLUGIN_ROOT" in command
    if os.name != "nt":
        return

    marker = git_repo / "scripts" / "worktree-flow.ps1"
    marker.parent.mkdir()
    marker.write_text("# existing\n", encoding="utf-8")
    payload = {
        "cwd": str(git_repo),
        "hook_event_name": "SessionStart",
        "session_id": "windows-hook-test",
    }
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", command],
        input=json.dumps(payload, ensure_ascii=False),
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        timeout=15,
        env={**os.environ, "PLUGIN_ROOT": str(HOOK_PATH.parent.parent)},
    )

    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    assert "silently defers" in response["hookSpecificOutput"]["additionalContext"]


def test_hook_defers_to_existing_workflow_without_writing(git_repo: Path) -> None:
    marker = git_repo / "scripts" / "worktree-flow.ps1"
    marker.parent.mkdir()
    marker.write_text("# existing\n", encoding="utf-8")
    before = git(git_repo, "status", "--porcelain")
    result = HOOK.decide(
        {
            **_payload(git_repo, tool="apply_patch"),
            "hook_event_name": "SessionStart",
        }
    )
    assert result is not None
    assert "silently defers" in result["hookSpecificOutput"]["additionalContext"]
    assert HOOK.decide(_payload(git_repo, tool="apply_patch")) is None
    assert git(git_repo, "status", "--porcelain") == before
    assert not (git_repo / ".solo-ai").exists()


def test_hook_parses_documented_apply_patch_payload_and_all_move_targets(
    git_repo: Path, tmp_path: Path
) -> None:
    external = tmp_path / "draft.md"
    raw_patch = "\n".join(
        (
            "*** Begin Patch",
            f"*** Add File: {external}",
            "+draft",
            "*** End Patch",
        )
    )
    payload = {
        "cwd": str(git_repo),
        "hook_event_name": "PreToolUse",
        "tool_name": "apply_patch",
        "tool_input": {"command": raw_patch},
    }

    assert HOOK.patch_from(payload) == raw_patch
    assert HOOK._apply_patch_scope(payload, git_repo) == "external"
    assert HOOK.decide(payload) is None
    completed = subprocess.run(
        ["uv", "run", "--script", str(HOOK_PATH)],
        input=json.dumps(payload),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ""

    move_patch = "\n".join(
        (
            "*** Begin Patch",
            f"*** Update File: {external}",
            "*** Move to: README.md",
            "@@",
            "-draft",
            "+moved",
            "*** End Patch",
        )
    )
    mixed = {**payload, "tool_input": {"command": move_patch}}
    assert HOOK._apply_patch_scope(mixed, git_repo) == "mixed-targets"
    assert HOOK.decide(mixed)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_hook_keeps_explicit_legacy_patch_compatibility_and_rejects_conflicts(
    git_repo: Path,
) -> None:
    patch = "*** Begin Patch\n*** Add File: legacy.md\n+legacy\n*** End Patch"
    legacy = _payload(
        git_repo,
        tool="apply_patch",
        patch=patch,
        patch_transport="patch",
    )
    assert HOOK.patch_from(legacy) == patch

    direct = _payload(
        git_repo,
        tool="apply_patch",
        patch=patch,
        patch_transport="raw",
    )
    assert HOOK.patch_from(direct) == patch

    conflicting = {
        **_payload(git_repo, tool="apply_patch", patch=patch),
        "tool_input": {"command": patch, "patch": patch + "\n"},
    }
    assert HOOK.patch_from(conflicting) == ""
    denied = HOOK.decide(conflicting)
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"

    invalid = {
        **_payload(git_repo, tool="apply_patch", patch=patch),
        "tool_input": {"command": None},
    }
    assert HOOK.patch_from(invalid) == ""
    assert HOOK.decide(invalid)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_hook_steps_aside_only_for_an_approved_delegated_adapter(
    git_repo: Path,
) -> None:
    declare_delegated_adapter(git_repo, max_parallel=2)

    result = HOOK.decide(
        {
            **_payload(git_repo, tool="apply_patch"),
            "hook_event_name": "SessionStart",
        }
    )

    assert result is not None
    context = result["hookSpecificOutput"]["additionalContext"]
    assert "locally approved delegated adapter" in context
    assert "example-worktree-flow" in context
    assert HOOK.decide(_payload(git_repo, tool="apply_patch")) is None


def test_hook_recognizes_local_orchestration_commands_in_a_managed_repository(
    git_repo: Path,
) -> None:
    _initialized(git_repo)
    command = f'uv run --script "{RUNNER_PATH}" --repo "{git_repo}" orchestrate status'
    assert HOOK.decide(_payload(git_repo, tool="Bash", command=command)) is None


def test_hook_mature_workflow_precedes_current_task_choice(git_repo: Path) -> None:
    repo = GitRepo(git_repo)
    choose(
        repo,
        mode="current-task",
        slots=3,
        commands=None,
        session_id="current-session",
    )
    marker = git_repo / "scripts" / "worktree-flow.ps1"
    marker.parent.mkdir()
    marker.write_text("# existing\n", encoding="utf-8")
    local_state_before = (repo.local_dir / "session-overrides.json").read_bytes()

    result = HOOK.decide(
        {
            **_payload(
                git_repo,
                tool="apply_patch",
                session="current-session",
            ),
            "hook_event_name": "SessionStart",
        }
    )

    assert result is not None
    assert "silently defers" in result["hookSpecificOutput"]["additionalContext"]
    assert (
        repo.local_dir / "session-overrides.json"
    ).read_bytes() == local_state_before
    assert (
        HOOK.decide(_payload(git_repo, tool="apply_patch", session="current-session"))
        is None
    )


def test_hook_mature_workflow_precedes_long_term_direct_choice(
    git_repo: Path,
) -> None:
    repo = GitRepo(git_repo)
    choose(repo, mode="current-repository", slots=3, commands=None)
    marker = git_repo / "scripts" / "worktree-flow.ps1"
    marker.parent.mkdir()
    marker.write_text("# existing\n", encoding="utf-8")
    preference_before = (repo.local_dir / "preferences.json").read_bytes()

    result = HOOK.decide(
        {
            **_payload(git_repo, tool="apply_patch"),
            "hook_event_name": "SessionStart",
        }
    )

    assert result is not None
    assert "silently defers" in result["hookSpecificOutput"]["additionalContext"]
    assert (repo.local_dir / "preferences.json").read_bytes() == preference_before
    assert HOOK.decide(_payload(git_repo, tool="apply_patch")) is None


def test_hook_session_start_reports_current_task_without_reasking(
    git_repo: Path,
) -> None:
    repo = GitRepo(git_repo)
    choose(
        repo,
        mode="current-task",
        slots=3,
        commands=None,
        session_id="current-session",
    )

    result = HOOK.decide(
        {
            **_payload(
                git_repo,
                tool="apply_patch",
                session="current-session",
            ),
            "hook_event_name": "SessionStart",
        }
    )

    assert result is not None
    message = result["hookSpecificOutput"]["additionalContext"]
    assert "current-task" in message
    assert "此仓库怎么修改？" not in message


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


def test_hook_allows_only_the_session_that_chose_current_task(git_repo: Path) -> None:
    repo = GitRepo(git_repo)
    choice = choose(
        repo,
        mode="current-task",
        slots=3,
        commands=None,
        session_id="parent-session",
    )

    for tool, command in (("apply_patch", ""), ("Bash", "git add README.md")):
        assert (
            HOOK.decide(
                _payload(git_repo, tool=tool, command=command, session="parent-session")
            )
            is None
        )
    (git_repo / "ordinary.txt").write_text("ordinary\n", encoding="utf-8")
    post = HOOK.decide(
        {
            **_payload(git_repo, tool="apply_patch", session="parent-session"),
            "hook_event_name": "PostToolUse",
        }
    )
    assert post is None
    assert _doctor(repo)["guard_alerts"] == []

    denied = HOOK.decide(
        _payload(git_repo, tool="apply_patch", session="unrelated-session")
    )
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"

    choose(
        repo,
        mode="current-task",
        slots=3,
        commands=None,
        session_id="child-session",
        delegation_code=choice["delegation_code"],
    )
    assert (
        HOOK.decide(_payload(git_repo, tool="apply_patch", session="child-session"))
        is None
    )


def test_hook_allows_choose_as_the_only_unadopted_write_entrypoint(
    git_repo: Path,
) -> None:
    allowed = HOOK.decide(
        _payload(
            git_repo,
            tool="Bash",
            command=(
                f'uv run --script "{RUNNER_PATH}" --repo "{git_repo}" '
                "choose --mode current-task --session session-a"
            ),
        )
    )
    assert allowed is None


def test_hook_long_term_current_directory_choice_steps_aside(git_repo: Path) -> None:
    repo = GitRepo(git_repo)
    choose(repo, mode="current-repository", slots=3, commands=None)

    result = HOOK.decide(_payload(git_repo, tool="apply_patch"))
    assert result is not None
    output = result["hookSpecificOutput"]
    assert "Do not initialize" in output["additionalContext"]
    assert not (git_repo / ".solo-ai").exists()


def test_hook_session_start_uses_the_single_plain_language_choice(
    git_repo: Path,
) -> None:
    result = HOOK.decide(
        {
            **_payload(git_repo, tool="apply_patch", session="session-a"),
            "hook_event_name": "SessionStart",
        }
    )
    message = result["hookSpecificOutput"]["additionalContext"]
    assert "此仓库怎么修改？" in message
    assert "每个任务使用独立目录（推荐）" in message
    assert "这一次直接改当前目录" in message
    assert "以后都直接改当前目录" in message
    assert "static mode" not in message


def test_hook_hard_denies_adopted_base_write_and_allows_isolated_owner(
    git_repo: Path,
) -> None:
    repo = _initialized(git_repo)
    denied = HOOK.decide(_payload(git_repo, tool="apply_patch"))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"

    task = start(
        repo,
        name="isolated",
        host_origin={"kind": "codex", "thread_id": "isolated-owner"},
    )
    task_patch = (
        "*** Begin Patch\n"
        "*** Add File: task-local.md\n"
        "+isolated task content\n"
        "*** End Patch"
    )
    allowed = HOOK.decide(
        _payload(
            Path(task["worktree"]),
            tool="apply_patch",
            patch=task_patch,
            session="isolated-owner",
        )
    )
    assert allowed is None


def test_hook_applies_formal_isolated_handoff_owner_transfer(git_repo: Path) -> None:
    repo = _initialized(git_repo)
    original_host = {"kind": "codex", "thread_id": "formal-source"}
    receiving_host = {"kind": "codex", "thread_id": "formal-recipient"}
    task = start(repo, name="formal isolated handoff", host_origin=original_host)
    worktree = Path(task["worktree"])

    received = handoff(
        repo,
        task_id=task["id"],
        confirm=f"{task['id']}:{task['branch']}:{task['candidate_head']}",
        host_origin=receiving_host,
    )

    denied = HOOK.decide(
        _payload(worktree, tool="apply_patch", session=original_host["thread_id"])
    )
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert (
        HOOK.decide(
            _payload(worktree, tool="apply_patch", session=receiving_host["thread_id"])
        )
        is None
    )
    abandon(repo, task_id=task["id"], lease=received["lease"], confirm=task["id"])


def test_hook_requires_one_root_refresh_after_an_actual_session_recovery(
    git_repo: Path,
) -> None:
    repo = _initialized(git_repo)
    plan = git_repo / "hook-root-plan.md"
    plan.write_text("# Hook root\n\n- Refresh before writing.\n", encoding="utf-8")
    root = create_root_task_anchor(
        repo,
        purpose="recover root context on a new session",
        target="block supported writes until refresh-root reads the current context",
        scope="one isolated task and one SessionStart event",
        acceptance="read-only and refresh remain available while writes wait",
        plan_input_path=plan,
        plan_source="user confirmed the root plan",
        request_id="hook-root-refresh-test",
    )
    task = start(
        repo,
        name="hook root child",
        root_anchor_id=root["root_id"],
        host_origin={"kind": "codex", "thread_id": "root-owner"},
    )
    worktree = Path(task["worktree"])

    wrong_session_start = HOOK.decide(
        {
            **_payload(worktree, tool="apply_patch", session="unrelated-owner"),
            "hook_event_name": "SessionStart",
        }
    )
    assert wrong_session_start is not None
    assert StateStore(repo).root_context_refresh_required(task["id"]) is None

    started = HOOK.decide(
        {
            **_payload(worktree, tool="apply_patch", session="root-owner"),
            "hook_event_name": "SessionStart",
        }
    )
    assert started is not None
    marker = StateStore(repo).root_context_refresh_required(task["id"])
    assert marker is not None and marker["reason"] == "SessionStart"
    HOOK.decide(
        {
            **_payload(worktree, tool="apply_patch", session="root-owner"),
            "hook_event_name": "SessionStart",
        }
    )
    newer_marker = StateStore(repo).root_context_refresh_required(task["id"])
    assert (
        newer_marker is not None and newer_marker["generation"] != marker["generation"]
    )
    assert (
        StateStore(repo).clear_root_context_refresh(
            task["id"], generation=marker["generation"]
        )
        is False
    )
    denied = HOOK.decide(_payload(worktree, tool="apply_patch", session="root-owner"))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert (
        HOOK.decide(
            _payload(
                worktree,
                tool="Bash",
                command="git status --short",
                session="root-owner",
            )
        )
        is None
    )
    assert (
        HOOK.decide(
            _payload(
                worktree,
                tool="Bash",
                session="root-owner",
                command=(
                    f'uv run --script "{RUNNER_PATH}" --repo "{worktree}" '
                    f"anchor refresh-root --task {task['id']} --lease {task['lease']}"
                ),
            )
        )
        is None
    )

    wrong_refresh = HOOK.decide(
        _payload(
            worktree,
            tool="Bash",
            session="unrelated-owner",
            command=(
                f'uv run --script "{RUNNER_PATH}" --repo "{worktree}" '
                f"anchor refresh-root --task {task['id']} --lease {task['lease']}"
            ),
        )
    )
    assert wrong_refresh["hookSpecificOutput"]["permissionDecision"] == "deny"

    refresh_root_context(repo, task_id=task["id"], lease=task["lease"])
    assert StateStore(repo).root_context_refresh_required(task["id"]) is None
    assert (
        HOOK.decide(_payload(worktree, tool="apply_patch", session="root-owner"))
        is None
    )


def test_hook_checks_actual_patch_targets_and_isolated_owner_before_writing(
    git_repo: Path,
    monkeypatch,
) -> None:
    repo = _initialized(git_repo, slots=2)
    first = start(
        repo,
        name="first owner",
        host_origin={"kind": "codex", "thread_id": "first-session"},
    )
    second = start(
        repo,
        name="second owner",
        host_origin={"kind": "codex", "thread_id": "second-session"},
    )
    first_worktree = Path(first["worktree"])
    second_worktree = Path(second["worktree"])

    own_absolute = (
        "*** Begin Patch\n"
        f"*** Add File: {first_worktree / 'owner-only.md'}\n"
        "+owned\n"
        "*** End Patch"
    )
    assert (
        HOOK.decide(
            _payload(
                git_repo,
                tool="apply_patch",
                patch=own_absolute,
                session="first-session",
            )
        )
        is None
    )

    no_cwd_absolute = _payload(
        git_repo,
        tool="apply_patch",
        patch=own_absolute,
        session="first-session",
    )
    no_cwd_absolute.pop("cwd")
    assert HOOK._apply_patch_targets(no_cwd_absolute, git_repo) == [
        (first_worktree / "owner-only.md").resolve()
    ]
    with monkeypatch.context() as isolated_cwd:
        isolated_cwd.chdir(git_repo.parent)
        assert HOOK.git_root(str(git_repo.parent)) is None
        assert HOOK.decide(no_cwd_absolute) is None

    no_cwd_relative = _payload(
        git_repo,
        tool="apply_patch",
        patch="*** Begin Patch\n*** Add File: owner-relative.md\n+owned\n*** End Patch",
        session="first-session",
    )
    no_cwd_relative.pop("cwd")
    assert HOOK._apply_patch_targets(no_cwd_relative, git_repo) is None

    nested = first_worktree / "nested"
    nested.mkdir()
    relative = _payload(
        nested,
        tool="apply_patch",
        patch=(
            "*** Begin Patch\n*** Add File: owner-relative.md\n+owned\n*** End Patch"
        ),
        session="first-session",
    )
    assert HOOK._apply_patch_targets(relative, first_worktree) == [
        (nested / "owner-relative.md").resolve()
    ]
    assert HOOK.decide(relative) is None

    wrong_session = HOOK.decide(
        _payload(
            first_worktree,
            tool="apply_patch",
            patch=(
                "*** Begin Patch\n"
                "*** Add File: denied.md\n"
                "+must not write\n"
                "*** End Patch"
            ),
            session="second-session",
        )
    )
    assert wrong_session["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert (
        "does not own this isolated task"
        in wrong_session["hookSpecificOutput"]["permissionDecisionReason"]
    )

    mixed_targets = (
        "*** Begin Patch\n"
        f"*** Add File: {first_worktree / 'first.md'}\n"
        "+first\n"
        f"*** Add File: {second_worktree / 'second.md'}\n"
        "+second\n"
        "*** End Patch"
    )
    mixed = HOOK.decide(
        _payload(
            git_repo,
            tool="apply_patch",
            patch=mixed_targets,
            session="first-session",
        )
    )
    assert mixed["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "mixes targets" in mixed["hookSpecificOutput"]["permissionDecisionReason"]
    assert git(first_worktree, "status", "--porcelain") == ""
    assert git(second_worktree, "status", "--porcelain") == ""

    cross_task_move = (
        "*** Begin Patch\n"
        f"*** Update File: {first_worktree / 'README.md'}\n"
        f"*** Move to: {second_worktree / 'README.md'}\n"
        "@@\n"
        "-fixture\n"
        "+fixture\n"
        "*** End Patch"
    )
    moved = HOOK.decide(
        _payload(
            git_repo,
            tool="apply_patch",
            patch=cross_task_move,
            session="first-session",
        )
    )
    assert moved["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "mixes targets" in moved["hookSpecificOutput"]["permissionDecisionReason"]


def test_hook_checks_managed_patch_targets_from_outside_a_repository(
    git_repo: Path, tmp_path: Path
) -> None:
    repo = _initialized(git_repo)
    task = start(
        repo, name="external entry", host_origin={"kind": "codex", "thread_id": "owner"}
    )
    worktree = Path(task["worktree"])
    outside = tmp_path / "outside"
    outside.mkdir()
    assert HOOK.git_root(str(outside)) is None

    def patch(target: Path, session: str = "owner", extra: str = ""):
        return HOOK.decide(
            _payload(
                outside,
                tool="apply_patch",
                session=session,
                patch=f"*** Begin Patch\n*** Add File: {target}\n+probe\n{extra}*** End Patch",
            )
        )

    target = worktree / "probe.txt"
    assert patch(target) is None
    for session in ("other", ""):
        denied = patch(target, session)
        assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert patch(target) is None
    for target in (git_repo / "base.txt", worktree / ".git"):
        assert patch(target)["hookSpecificOutput"]["permissionDecision"] == "deny"
    mixed = patch(
        worktree / "probe.txt",
        extra=f"*** Add File: {outside / 'mixed.txt'}\n+x\n",
    )
    assert mixed["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert patch(outside / "ordinary.txt") is None
    assert git(worktree, "status", "--porcelain") == ""


def test_hook_infers_unique_owner_worktree_when_patch_payload_omits_cwd(
    git_repo: Path, monkeypatch
) -> None:
    repo = _initialized(git_repo, slots=1)
    task = start(
        repo,
        name="nested apply_patch owner",
        host_origin={"kind": "codex", "thread_id": "nested-owner"},
    )
    worktree = Path(task["worktree"])
    monkeypatch.chdir(git_repo)

    payload = _payload(
        git_repo,
        tool="apply_patch",
        patch="*** Begin Patch\n*** Add File: inferred-owner.md\n+owned\n*** End Patch",
        session="nested-owner",
    )
    payload.pop("cwd")

    assert HOOK.decide(payload) is None
    assert not (worktree / "inferred-owner.md").exists()

    denied = dict(payload)
    denied["session_id"] = "another-session"
    result = HOOK.decide(denied)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert (
        "could not be determined safely"
        in result["hookSpecificOutput"]["permissionDecisionReason"]
    )


def test_hook_allows_only_owned_task_targets_inside_codex_home(
    tmp_path: Path, monkeypatch
) -> None:
    codex_home = tmp_path / "codex-home"
    project = codex_home / "project"
    project.mkdir(parents=True)
    git(project, "init")
    (project / "README.md").write_text("fixture\n", encoding="utf-8")
    git(project, "add", "README.md")
    git(project, "commit", "-m", "test: create project inside codex home")
    repo = _initialized(project)
    task = start(
        repo,
        name="codex-home task",
        host_origin={"kind": "codex", "thread_id": "current-owner"},
    )
    worktree = Path(task["worktree"])
    owned = worktree / "owned.md"
    moved = worktree / "moved.md"
    owned.write_text("old\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    def decide(
        patch: str, *, session: str = "current-owner"
    ) -> dict[str, object] | None:
        return HOOK.decide(
            _payload(project, tool="apply_patch", patch=patch, session=session)
        )

    allowed_patches = (
        f"*** Begin Patch\n*** Add File: {worktree / 'added.md'}\n+added\n*** End Patch",
        f"*** Begin Patch\n*** Update File: {owned}\n@@\n-old\n+new\n*** End Patch",
        f"*** Begin Patch\n*** Delete File: {owned}\n*** End Patch",
        (
            "*** Begin Patch\n"
            f"*** Update File: {owned}\n"
            f"*** Move to: {moved}\n"
            "@@\n-old\n+new\n*** End Patch"
        ),
    )
    for patch in allowed_patches:
        assert decide(patch) is None

    wrong_owner = decide(allowed_patches[0], session="other-owner")
    assert wrong_owner is not None
    assert wrong_owner["hookSpecificOutput"]["permissionDecision"] == "deny"

    for patch in (
        f"*** Begin Patch\n*** Update File: {worktree / '.git'}\n@@\n-old\n+new\n*** End Patch",
        (
            "*** Begin Patch\n"
            f"*** Add File: {worktree / 'allowed.md'}\n+allowed\n"
            f"*** Add File: {codex_home / 'config.toml'}\nblocked\n"
            "*** End Patch"
        ),
    ):
        denied = decide(patch)
        assert denied is not None
        assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"

    nested = worktree / "nested"
    nested.mkdir()
    git(nested, "init")
    nested_denied = decide(
        f"*** Begin Patch\n*** Add File: {nested / 'report.md'}\nblocked\n*** End Patch"
    )
    assert nested_denied is not None
    assert nested_denied["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_hook_denies_codex_home_task_when_registered_identity_drifts(
    tmp_path: Path, monkeypatch
) -> None:
    codex_home = tmp_path / "codex-home"
    project = codex_home / "project"
    project.mkdir(parents=True)
    git(project, "init")
    (project / "README.md").write_text("fixture\n", encoding="utf-8")
    git(project, "add", "README.md")
    git(project, "commit", "-m", "test: create project inside codex home")
    repo = _initialized(project)
    task = start(
        repo,
        name="identity drift task",
        host_origin={"kind": "codex", "thread_id": "current-owner"},
    )
    StateStore(repo).update_task(
        task["id"], slot_worktree_resolved=str(tmp_path / "replaced-worktree")
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    denied = HOOK.decide(
        _payload(
            project,
            tool="apply_patch",
            patch=(
                "*** Begin Patch\n"
                f"*** Add File: {Path(task['worktree']) / 'owned.md'}\n"
                "+owned\n"
                "*** End Patch"
            ),
            session="current-owner",
        )
    )

    assert denied is not None
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert (
        "worktree path identity changed"
        in denied["hookSpecificOutput"]["permissionDecisionReason"]
    )


def test_hook_fails_closed_for_an_isolated_task_without_host_owner(
    git_repo: Path,
) -> None:
    repo = _initialized(git_repo)
    task = start(repo, name="legacy isolated task")

    denied = HOOK.decide(
        _payload(
            Path(task["worktree"]),
            tool="apply_patch",
            session="unrelated-session",
        )
    )

    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert (
        "no verifiable Codex host owner"
        in denied["hookSpecificOutput"]["permissionDecisionReason"]
    )
    assert (
        HOOK.decide(
            _payload(
                Path(task["worktree"]),
                tool="Bash",
                command=(
                    f'uv run --script "{RUNNER_PATH}" --repo "{task["worktree"]}" '
                    "status"
                ),
                session="unrelated-session",
            )
        )
        is None
    )


def test_hook_uses_actual_apply_patch_targets_for_external_structural_files(
    git_repo: Path, tmp_path: Path
) -> None:
    _initialized(git_repo)
    external_plan = tmp_path / "confirmed-plan.md"
    external_patch = (
        "*** Begin Patch\n"
        f"*** Add File: {external_plan}\n"
        "+# Confirmed plan\n"
        "*** End Patch"
    )
    assert (
        HOOK.decide(_payload(git_repo, tool="apply_patch", patch=external_patch))
        is None
    )

    protected_patch = (
        "*** Begin Patch\n*** Update File: README.md\n@@\n-old\n+new\n*** End Patch"
    )
    denied = HOOK.decide(_payload(git_repo, tool="apply_patch", patch=protected_patch))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"

    unknown = HOOK.decide(_payload(git_repo, tool="apply_patch", patch="x"))
    assert unknown["hookSpecificOutput"]["permissionDecision"] == "deny"

    foreign = tmp_path / "foreign-repository"
    foreign.mkdir()
    git(foreign, "init")
    foreign_patch = (
        "*** Begin Patch\n"
        f"*** Add File: {foreign / 'report.md'}\n"
        "+must stay protected\n"
        "*** End Patch"
    )
    foreign_denied = HOOK.decide(
        _payload(git_repo, tool="apply_patch", patch=foreign_patch)
    )
    assert foreign_denied["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_hook_allows_only_the_current_codex_session_artifact_root(
    git_repo: Path, tmp_path: Path, monkeypatch
) -> None:
    _initialized(git_repo)
    codex_home = tmp_path / "custom codex home"
    codex_home.mkdir()
    git(codex_home, "init")
    adopted_config = codex_home / ".solo-ai" / "config.toml"
    adopted_config.parent.mkdir()
    adopted_config.write_text('mode = "managed"\n', encoding="utf-8")
    session = "session-2026-中文"
    artifact = codex_home / "visualizations" / "2026" / "09" / "17" / session
    artifact.mkdir(parents=True)
    allowed = artifact / "方案 报告.md"
    patch = f"*** Begin Patch\n*** Add File: {allowed}\n+# 验收\n*** End Patch"
    payload = {
        "cwd": str(git_repo),
        "hook_event_name": "PreToolUse",
        "tool_name": "apply_patch",
        "session_id": session,
        "tool_input": {"command": patch},
    }
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    assert HOOK.patch_from(payload) == patch
    assert HOOK._apply_patch_scope(payload, git_repo) == "session-artifact"
    assert HOOK.decide(payload) is None

    second_allowed = artifact / "第二份 报告.md"
    all_current_artifacts = "\n".join(
        (
            "*** Begin Patch",
            f"*** Add File: {allowed}",
            "+第一份",
            f"*** Add File: {second_allowed}",
            "+第二份",
            "*** End Patch",
        )
    )
    assert (
        HOOK.decide({**payload, "tool_input": {"command": all_current_artifacts}})
        is None
    )

    other = artifact.parent / "other-session" / "报告.md"
    denied = HOOK.decide(
        {
            **payload,
            "tool_input": {"command": patch.replace(str(allowed), str(other))},
        }
    )
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"

    mixed = "\n".join(
        (
            "*** Begin Patch",
            f"*** Add File: {allowed}",
            "+artifact",
            "*** Add File: README.md",
            "+protected",
            "*** End Patch",
        )
    )
    assert (
        HOOK._apply_patch_scope({**payload, "tool_input": {"command": mixed}}, git_repo)
        == "mixed-targets"
    )
    mixed_denied = HOOK.decide({**payload, "tool_input": {"command": mixed}})
    assert mixed_denied["hookSpecificOutput"]["permissionDecision"] == "deny"

    ordinary = tmp_path / "ordinary-report.md"
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing-codex-home"))
    assert (
        HOOK._apply_patch_scope(
            {
                **payload,
                "tool_input": {
                    "command": "*** Begin Patch\n"
                    f"*** Add File: {ordinary}\n+report\n*** End Patch"
                },
            },
            git_repo,
        )
        == "external"
    )


def test_hook_keeps_codex_home_git_content_and_nested_repositories_protected(
    git_repo: Path, tmp_path: Path, monkeypatch
) -> None:
    _initialized(git_repo)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    git(codex_home, "init")
    session = "current-session"
    artifact = codex_home / "visualizations" / "2026" / "09" / "18" / session
    artifact.mkdir(parents=True)
    tracked = artifact / "tracked.md"
    tracked.write_text("tracked\n", encoding="utf-8")
    git(codex_home, "add", "--", str(tracked.relative_to(codex_home)))
    git(codex_home, "commit", "-m", "test: track artifact")
    nested = artifact / "nested"
    nested.mkdir()
    git(nested, "init")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    def payload_for(target: Path) -> dict[str, object]:
        return {
            "cwd": str(git_repo),
            "hook_event_name": "PreToolUse",
            "tool_name": "apply_patch",
            "session_id": session,
            "tool_input": {
                "command": "*** Begin Patch\n"
                f"*** Update File: {target}\n@@\n-old\n+new\n*** End Patch"
            },
        }

    for target, expected in (
        (tracked, "CODEX_HOME"),
        (nested / "report.md", "其他或嵌套 Git 仓库"),
        (codex_home / "config.toml", "CODEX_HOME"),
    ):
        denied = HOOK.decide(payload_for(target))
        assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert expected in denied["hookSpecificOutput"]["permissionDecisionReason"]


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


def test_hook_accepts_runner_targeting_same_common_dir_task_from_session_checkout(
    git_repo: Path, tmp_path: Path
) -> None:
    repo = _initialized(git_repo)
    task = start(
        repo,
        name="runner target from session checkout",
        host_origin={"kind": "codex", "thread_id": "task-owner"},
    )
    worktree = Path(task["worktree"])

    for command in (
        f'uv run --script "{RUNNER_PATH}" --repo "{worktree}" status',
        f"uv run --script '{RUNNER_PATH}' --repo={worktree} status",
    ):
        assert (
            HOOK.decide(
                _payload(
                    git_repo,
                    tool="Bash",
                    command=command,
                    session="task-owner",
                )
            )
            is None
        )

    mutation = HOOK.decide(
        _payload(
            git_repo,
            tool="Bash",
            command=(
                f'uv run --script "{RUNNER_PATH}" --repo "{worktree}" '
                f"anchor show --task {task['id']}"
            ),
            session="other-session",
        )
    )
    assert mutation is not None
    assert mutation["hookSpecificOutput"]["permissionDecision"] == "deny"

    foreign = tmp_path / "foreign-repository"
    foreign.mkdir()
    git(foreign, "init")
    denied = HOOK.decide(
        _payload(
            git_repo,
            tool="Bash",
            command=f'uv run --script "{RUNNER_PATH}" --repo "{foreign}" status',
            session="task-owner",
        )
    )
    assert denied is not None
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_dww_runner_parser_requires_literal_shape_and_supports_power_shell_quotes(
    git_repo: Path, tmp_path: Path
) -> None:
    spaced = tmp_path / "中文 仓库"
    spaced.mkdir()
    git(spaced, "init")

    assert (
        HOOK._dww_subcommand(
            f"uv run --script {RUNNER_PATH} --repo='{spaced}' status", spaced
        )
        == "status"
    )
    assert (
        HOOK._dww_subcommand(
            f'uv run --script "{RUNNER_PATH}" --repo "{spaced}" --json version',
            spaced,
        )
        == "version"
    )
    assert (
        HOOK._dww_subcommand(
            f"uv run --script {RUNNER_PATH} --repo {git_repo} status", git_repo
        )
        == "status"
    )

    for command in (
        f'python x/dww.py --repo "{git_repo}" status',
        f'Write-Output "{RUNNER_PATH}" --repo "{git_repo}" status',
        (
            f'uv run --script "{RUNNER_PATH}" --repo "{git_repo}" '
            "status; Get-Content README.md"
        ),
        f'uv run --script "{RUNNER_PATH}" --repo "$env:DWW_REPO" status',
        f'uv run --script "{RUNNER_PATH}" --repo "{git_repo}',
        f'uv run --script "{RUNNER_PATH}" --repo "{git_repo}" status --repo "{spaced}"',
        f'uv run --script "{RUNNER_PATH}" --repo "{git_repo}" status --repo={spaced}',
    ):
        assert HOOK._dww_subcommand(command, git_repo) is None


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


def test_read_only_parser_accepts_quoted_search_text_and_limited_pipeline() -> None:
    assert HOOK._strict_read_only_bash("rg --no-config -n 'anchor|scope' README.md")
    assert HOOK._strict_read_only_bash(
        "Get-Content -LiteralPath 'README.md' -Encoding UTF8 | Select-Object -First 40"
    )
    assert HOOK._strict_read_only_bash("rg --no-config -n 'it''s|scope' README.md")
    assert HOOK._strict_read_only_bash(
        "rg --no-config -n -A 3 'anchor|scope' README.md"
    )


def test_read_only_parser_accepts_common_repository_enumeration() -> None:
    assert HOOK._strict_read_only_bash("Get-Location")
    assert HOOK._strict_read_only_bash("Get-ChildItem -Name")
    assert HOOK._strict_read_only_bash("Get-ChildItem -LiteralPath docs -Name")
    assert HOOK._strict_read_only_bash("git ls-files")
    assert HOOK._strict_read_only_bash("git ls-files --cached --full-name")
    assert HOOK._strict_read_only_bash("git worktree list --porcelain")
    assert HOOK._strict_read_only_bash("Get-FileHash -LiteralPath README.md")
    assert HOOK._strict_read_only_bash("Get-FileHash -Path README.md -Algorithm SHA256")


def test_read_only_parser_rejects_writes_and_external_rg_preprocessors() -> None:
    assert not HOOK._strict_read_only_bash(
        "Get-Content -LiteralPath README.md | Set-Content copy.md"
    )
    assert not HOOK._strict_read_only_bash("rg --pre cat -n anchor README.md")
    assert not HOOK._strict_read_only_bash("git branch -D old-branch")
    assert not HOOK._strict_read_only_bash("Get-Content README.md; Set-Content copy.md")
    assert not HOOK._strict_read_only_bash("git diff --output=artifact.patch")
    assert not HOOK._strict_read_only_bash("git show --ext-diff HEAD")
    assert not HOOK._strict_read_only_bash("Get-Content -Path -Force")


def test_read_only_parser_rejects_newline_command_chaining(git_repo: Path) -> None:
    command = "rg --no-config needle README.md\nSet-Content escaped.txt value"

    assert not HOOK._strict_read_only_bash(command)
    result = HOOK.decide(_payload(git_repo, tool="Bash", command=command))
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_read_only_parser_keeps_quoted_pipe_as_search_text() -> None:
    assert HOOK._strict_read_only_bash("rg --no-config -F '|' README.md")
    assert HOOK._strict_read_only_bash("rg --no-config -- '--pre' README.md")
    assert HOOK._strict_read_only_bash("rg --no-config -e '--pre' README.md")
    assert not HOOK._strict_read_only_bash("git status | Select-Object -First 1")


def test_read_only_parser_requires_rg_config_isolation_and_known_options() -> None:
    assert not HOOK._strict_read_only_bash("rg -n 'anchor|scope' README.md")
    assert not HOOK._strict_read_only_bash(
        "rg --no-config --unrecognized-option needle README.md"
    )
    assert not HOOK._strict_read_only_bash(
        "rg --no-config --pre helper needle README.md"
    )
    assert "--no-config" in HOOK._read_only_rejection_reason(
        "rg -n 'anchor|scope' README.md"
    )


def test_read_only_parser_limits_content_and_select_arguments() -> None:
    assert not HOOK._strict_read_only_bash("Get-Content README.md CHANGELOG.md")
    assert not HOOK._strict_read_only_bash(
        "Get-Content -LiteralPath README.md -Delimiter ,"
    )
    assert not HOOK._strict_read_only_bash(
        "Get-Content README.md | Select-Object -First 1_0"
    )
    assert not HOOK._strict_read_only_bash("Get-ChildItem -Recurse")
    assert not HOOK._strict_read_only_bash("git ls-files --with-tree=HEAD")
    assert not HOOK._strict_read_only_bash("Get-FileHash -Recurse README.md")
    assert not HOOK._strict_read_only_bash(
        "Get-FileHash -Algorithm SHA256 README.md other.md"
    )


def test_maintenance_powershell_identity_rejects_a_sibling_executable(
    tmp_path: Path, monkeypatch
) -> None:
    program_files = tmp_path / "Program Files"
    trusted = program_files / "PowerShell" / "7" / "pwsh.exe"
    trusted.parent.mkdir(parents=True)
    trusted.write_text("official test marker\n", encoding="utf-8")
    fake = trusted.parent / "pwsh-copy.exe"
    fake.write_text("official test marker\n", encoding="utf-8")
    monkeypatch.setenv("ProgramFiles", str(program_files))
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)

    assert HOOK._trusted_powershell_cli(str(trusted))
    assert not HOOK._trusted_powershell_cli(str(fake))


def test_plugin_maintenance_allows_only_owned_dww_commands(
    git_repo: Path, monkeypatch
) -> None:
    """插件维护不能借只读解析器或任意任务扩大为通用 shell 权限。"""
    repo = _initialized(git_repo)
    task = start(
        repo,
        name="plugin maintenance",
        host_origin={"kind": "codex", "thread_id": "maintenance-owner"},
    )
    codex = git_repo / "official-codex.exe"
    codex.write_text("official test marker\n", encoding="utf-8")
    pwsh = git_repo / "official-pwsh.exe"
    pwsh.write_text("official test marker\n", encoding="utf-8")
    monkeypatch.setattr(
        HOOK,
        "_trusted_codex_cli",
        lambda value: Path(value).resolve() == codex.resolve(),
    )
    monkeypatch.setattr(
        HOOK,
        "_trusted_powershell_cli",
        lambda value: Path(value).resolve() == pwsh.resolve(),
    )
    task_worktree = Path(task["worktree"])

    def decide(command: str, *, session: str = "maintenance-owner") -> dict | None:
        return HOOK.decide(
            _payload(task_worktree, tool="Bash", command=command, session=session)
        )

    quoted = f'& "{codex}"'
    assert decide(f"{quoted} plugin marketplace list --json") is None
    assert decide(f"{quoted} plugin list --marketplace dww-stable-local --json") is None
    assert (
        decide(f"{quoted} plugin add develop-with-worktrees@dww-stable-local --json")
        is None
    )

    source_commit = git(git_repo, "rev-parse", "HEAD")
    script = HOOK._maintenance_script_path()
    release = (
        f'& "{pwsh}" -NoProfile -File "{script}" -Mode Install '
        f'-SourceRepo "{git_repo}" -SourceCommit {source_commit} '
        f'-CodexPath "{codex}"'
    )
    assert decide(release) is None
    assert decide(release.replace("-Mode Install", "-Mode RecoveryInstall")) is None
    denied_recovery = decide(
        release.replace("-Mode Install", "-Mode RecoveryInstall"),
        session="other-session",
    )
    assert denied_recovery["hookSpecificOutput"]["permissionDecision"] == "deny"

    for command in (
        f'& "{git_repo / "fake-codex.exe"}" plugin marketplace list --json',
        f"{quoted} plugin add another-plugin@dww-stable-local --json",
        f"{quoted} plugin add develop-with-worktrees@other-market --json",
        f"{quoted} plugin marketplace add C:\\untrusted --json",
        f"{quoted} plugin marketplace remove dww-stable-local --json",
        f"{quoted} plugin list --config injected --json",
        f"{quoted} plugin marketplace list --json; Remove-Item README.md",
        release.replace(str(pwsh), str(git_repo / "fake-pwsh.exe")),
        release.replace(str(script), str(git_repo / "fake-maintain-dww-plugin.ps1")),
        release.replace(str(codex), str(git_repo / "fake-codex.exe")),
        release.replace(
            f'-SourceRepo "{git_repo}"',
            f'-SourceRepo "{git_repo / ".worktrees"}"',
        ),
        release.replace("-CodexPath", "-MigrateMarketplace -CodexPath"),
        release + " -MarketplaceRoot C:\\untrusted",
        release + "; Remove-Item README.md",
    ):
        denied = decide(command)
        assert denied is not None, command
        assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert (
            "plugin maintenance command is blocked"
            in denied["hookSpecificOutput"]["permissionDecisionReason"]
        )

    assert (
        decide(f"{quoted} plugin marketplace list --json", session="other-session")
        is None
    )
    not_owner_release = decide(release, session="other-session")
    assert not_owner_release is not None
    assert not_owner_release["hookSpecificOutput"]["permissionDecision"] == "deny"
    abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])
    assert decide(f"{quoted} plugin list --marketplace dww-stable-local --json") is None
    denied_install = decide(
        f"{quoted} plugin add develop-with-worktrees@dww-stable-local --json"
    )
    assert denied_install is not None
    assert denied_install["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_hook_allows_every_contract_command_and_rejects_a_spoofed_runner(
    git_repo: Path,
) -> None:
    _initialized(git_repo)
    for command_name in TOP_LEVEL_COMMANDS:
        allowed = HOOK.decide(
            _payload(
                git_repo,
                tool="Bash",
                command=(
                    f'uv run --script "{RUNNER_PATH}" --repo "{git_repo}" '
                    f"{command_name}"
                ),
            )
        )
        assert allowed is None, command_name
    denied = HOOK.decide(
        _payload(
            git_repo,
            tool="Bash",
            command=(f'python x/dww.py --repo "{git_repo}" runtime'),
        )
    )

    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_cli_and_hook_share_the_same_top_level_command_contract() -> None:
    parser = _parser()
    command_action = next(
        action for action in parser._actions if action.dest == "command"
    )
    assert frozenset(command_action.choices) == TOP_LEVEL_COMMANDS
