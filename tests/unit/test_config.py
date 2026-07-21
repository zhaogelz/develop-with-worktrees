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


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ('branch_prefix = "bad..prefix/"', "branch_prefix"),
        ('sensitive_allowlist = "*"', "sensitive_allowlist"),
        ('sensitive_allowlist = ["*"]', "sensitive_allowlist"),
        ('agents_file_created = "false"', "agents_file_created"),
    ],
)
def test_rejects_unsafe_or_ambiguous_repository_config_types(
    git_repo: Path, replacement: str, message: str
) -> None:
    config = git_repo / ".solo-ai"
    config.mkdir()
    original = render_repo_config()
    if replacement.startswith("branch_prefix"):
        rendered = original.replace('branch_prefix = "codex/"', replacement)
    elif replacement.startswith("sensitive_allowlist"):
        rendered = original.replace("sensitive_allowlist = []", replacement)
    else:
        rendered = original.replace("agents_file_created = false", replacement)
    (config / "config.toml").write_text(rendered, encoding="utf-8")

    with pytest.raises(SoloAIError, match=message):
        load_repo_config(GitRepo(git_repo))


def test_rejects_empty_declared_secret_scanner(git_repo: Path) -> None:
    config = git_repo / ".solo-ai"
    config.mkdir()
    (config / "config.toml").write_text(
        render_repo_config().replace(
            "\n[lifecycle]\n", "\nsecret_scanner = []\n\n[lifecycle]\n"
        ),
        encoding="utf-8",
    )

    with pytest.raises(SoloAIError, match="secret_scanner"):
        load_repo_config(GitRepo(git_repo))


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ('static_only = "false"\n', "static_only"),
        (
            """static_only = false

[[profiles]]
id = "bad-paths"
paths = "**"
commands = [["git", "status"]]
""",
            "paths",
        ),
        (
            """static_only = false

[[profiles]]
id = "bad-reuse"
paths = ["**"]
cross_task_reuse = "false"
external_state = "none"
commands = [["git", "status"]]
""",
            "cross_task_reuse",
        ),
    ],
)
def test_rejects_ambiguous_verification_config_types(
    git_repo: Path, body: str, message: str
) -> None:
    config = git_repo / ".solo-ai"
    config.mkdir()
    (config / "verification.toml").write_text(
        "schema_version = 2\n" + body,
        encoding="utf-8",
    )

    with pytest.raises(SoloAIError, match=message):
        load_verification_config(GitRepo(git_repo))
