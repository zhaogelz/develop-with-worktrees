import json
from pathlib import Path

import pytest

from solo_ai.config import (
    CommandSpec,
    discover_validation_commands,
    load_repo_config,
    load_verification_config,
    render_repo_config,
    render_verification_config,
)
from solo_ai.repo import GitRepo
from solo_ai.util import SoloAIError


def test_discovers_uv_pytest_as_explicit_argv(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname="x"\n', encoding="utf-8")
    (tmp_path / "tests").mkdir()
    assert discover_validation_commands(tmp_path) == [
        CommandSpec(("uv", "run", "pytest"))
    ]


def test_renders_safe_default_reuse_policy() -> None:
    rendered = render_verification_config(
        [CommandSpec(("uv", "run", "pytest"))], static_only=False
    )
    assert "cross_task_reuse = false" in rendered
    assert 'external_state = "unknown"' in rendered
    assert "{port}" in render_repo_config()


def test_rejects_cross_task_reuse_with_external_state(git_repo: Path) -> None:
    config = git_repo / ".solo-ai"
    config.mkdir()
    (config / "verification.toml").write_text(
        """schema_version = 2
static_only = false

[[profiles]]
id = "bad"
paths = ["**"]
cross_task_reuse = true
external_state = "database"
commands = [["git", "status"]]
""",
        encoding="utf-8",
    )
    with pytest.raises(SoloAIError, match="external_state"):
        load_verification_config(GitRepo(git_repo))


def test_rejects_unimplemented_command_readiness(git_repo: Path) -> None:
    config = git_repo / ".solo-ai"
    config.mkdir()
    (config / "config.toml").write_text(
        render_repo_config()
        + """\ndev_start = ["python", "-m", "http.server", "{port}"]

[lifecycle.readiness]
kind = "command"
timeout_seconds = 10
""",
        encoding="utf-8",
    )
    with pytest.raises(SoloAIError, match="tcp or http"):
        load_repo_config(GitRepo(git_repo))


def test_rejects_worktree_directory_outside_repository(git_repo: Path) -> None:
    config = git_repo / ".solo-ai"
    config.mkdir()
    for directory in ("../outside", str(git_repo.parent / "outside"), "."):
        (config / "config.toml").write_text(
            render_repo_config().replace(
                'worktree_directory = ".worktrees"',
                f"worktree_directory = {json.dumps(directory)}",
            ),
            encoding="utf-8",
        )
        with pytest.raises(SoloAIError, match="worktree_directory"):
            load_repo_config(GitRepo(git_repo))
