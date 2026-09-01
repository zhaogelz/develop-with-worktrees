import json
from pathlib import Path

import pytest
from solo_ai.config import (
    CommandSpec,
    discover_validation_commands,
    load_repo_config,
    load_verification_config,
    managed_block,
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
    assert "cleanup = { owned_paths = [] }" in render_repo_config()


@pytest.mark.parametrize(("port_base", "valid"), [(62336, True), (62337, False)])
def test_port_base_leaves_room_for_the_32nd_hundred_port_block(
    git_repo: Path, port_base: int, valid: bool
) -> None:
    config = git_repo / ".solo-ai"
    config.mkdir()
    (config / "config.toml").write_text(
        render_repo_config().replace("port_base = 20000", f"port_base = {port_base}"),
        encoding="utf-8",
    )

    if valid:
        assert load_repo_config(GitRepo(git_repo)).port_base == port_base
    else:
        with pytest.raises(SoloAIError, match="leave room"):
            load_repo_config(GitRepo(git_repo))


def test_rejects_casefold_duplicate_cleanup_paths(git_repo: Path) -> None:
    config = git_repo / ".solo-ai"
    config.mkdir()
    (config / "config.toml").write_text(
        render_repo_config().replace(
            "cleanup = { owned_paths = [] }",
            'cleanup = { owned_paths = [".venv", ".VENV"] }',
        ),
        encoding="utf-8",
    )
    with pytest.raises(SoloAIError, match="duplicate paths"):
        load_repo_config(GitRepo(git_repo))


def test_managed_policy_separates_local_lifecycle_from_explicit_publish() -> None:
    policy = managed_block()

    assert "The DWW lifecycle is local-only" in policy
    assert "After a successful Finish, an explicit user request" in policy
    assert "ordinary non-force push" in policy
    assert "separate from DWW" in policy
    assert "batch reconcile" in policy
    assert "There is no candidate-age auto-seal" in policy
    assert "Candidate publication is not delivery" in policy


def test_rejects_schema_two_verification_policy(git_repo: Path) -> None:
    config = git_repo / ".solo-ai"
    config.mkdir()
    (config / "verification.toml").write_text(
        "schema_version = 2\nstatic_only = true\n", encoding="utf-8"
    )
    with pytest.raises(SoloAIError, match="expected 3"):
        load_verification_config(GitRepo(git_repo))


def test_rejects_cross_task_reuse_with_external_state(git_repo: Path) -> None:
    config = git_repo / ".solo-ai"
    config.mkdir()
    (config / "verification.toml").write_text(
        """schema_version = 3
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


def test_verification_schema_three_requires_complete_inputs_for_cross_task_reuse(
    git_repo: Path,
) -> None:
    config = git_repo / ".solo-ai"
    config.mkdir()
    path = config / "verification.toml"
    path.write_text(
        """schema_version = 3
static_only = false

[[profiles]]
id = "shared"
paths = ["src/**"]
cross_task_reuse = true
external_state = "none"
input_paths = ["src/**", "uv.lock"]
input_closure = "declared"
timeout_seconds = 12.5
resource_class = "heavy"
level = "full"
environment = ["CI"]
commands = [["git", "status", "--short"]]
""",
        encoding="utf-8",
    )
    with pytest.raises(SoloAIError, match="input_closure"):
        load_verification_config(GitRepo(git_repo))

    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'input_closure = "declared"', 'input_closure = "complete"'
        ),
        encoding="utf-8",
    )
    profile = load_verification_config(GitRepo(git_repo)).profiles[0]
    assert profile.timeout_seconds == 12.5
    assert profile.resource_class == "heavy"
    assert profile.input_closure == "complete"


def test_rejects_heavy_ready_profiles(git_repo: Path) -> None:
    config = git_repo / ".solo-ai"
    config.mkdir()
    (config / "verification.toml").write_text(
        """schema_version = 3
static_only = false

[[profiles]]
id = "too-heavy-for-ready"
level = "ready"
resource_class = "heavy"
paths = ["**"]
commands = [["git", "status"]]
""",
        encoding="utf-8",
    )

    with pytest.raises(SoloAIError, match="must run at level full"):
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


def test_accepts_up_to_thirty_two_configured_slots(git_repo: Path) -> None:
    config = git_repo / ".solo-ai"
    config.mkdir()
    (config / "config.toml").write_text(render_repo_config(slots=32), encoding="utf-8")
    assert load_repo_config(GitRepo(git_repo)).slots == 32


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


def test_new_integration_defaults_are_batched_auto_full_with_five_and_ten(
    git_repo: Path,
) -> None:
    config = git_repo / ".solo-ai"
    config.mkdir()
    (config / "config.toml").write_text(render_repo_config(), encoding="utf-8")

    loaded = load_repo_config(GitRepo(git_repo))

    assert loaded.integration.mode == "batched"
    assert loaded.integration.batch_size == 5
    assert loaded.integration.candidate_capacity == 10
    assert loaded.integration.seal_policy == "auto_full"
    assert loaded.integration.tail_policy == "quiet_or_explicit"
    assert loaded.integration.tail_quiet_seconds == 90
    assert loaded.runtime_adapter.activate is None
    assert loaded.runtime_adapter.release is None
    assert loaded.runtime_adapter.verify_effective is None
    assert loaded.runtime_adapter.input_paths == ()
    assert loaded.runtime_adapter.timeout_seconds == 300


def test_loads_bounded_runtime_adapter_commands(git_repo: Path) -> None:
    config = git_repo / ".solo-ai"
    config.mkdir()
    rendered = render_repo_config().replace(
        "\n[lifecycle]\n",
        """
[runtime_adapter]
activate = ["uv", "run", "scripts/runtime-adapter.py", "activate"]
release = ["uv", "run", "scripts/runtime-adapter.py", "release"]
verify_effective = ["uv", "run", "scripts/runtime-adapter.py", "verify"]
input_paths = ["scripts/runtime-adapter.py", "deploy/**"]
timeout_seconds = 120

[lifecycle]
""",
    )
    (config / "config.toml").write_text(rendered, encoding="utf-8")

    loaded = load_repo_config(GitRepo(git_repo))

    assert loaded.runtime_adapter.activate is not None
    assert loaded.runtime_adapter.activate.argv[-1] == "activate"
    assert loaded.runtime_adapter.release is not None
    assert loaded.runtime_adapter.release.argv[-1] == "release"
    assert loaded.runtime_adapter.verify_effective is not None
    assert loaded.runtime_adapter.verify_effective.argv[-1] == "verify"
    assert loaded.runtime_adapter.input_paths == (
        "scripts/runtime-adapter.py",
        "deploy/**",
    )
    assert loaded.runtime_adapter.timeout_seconds == 120


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("activate = []\n", "runtime_adapter.activate"),
        ("release = []\n", "runtime_adapter.release"),
        ("timeout_seconds = 0\n", "runtime_adapter.timeout_seconds"),
        ('release = ["uv", "run", "adapter.py"]\n', "input_paths"),
    ],
)
def test_rejects_unsafe_runtime_adapter_settings(
    git_repo: Path, body: str, message: str
) -> None:
    config = git_repo / ".solo-ai"
    config.mkdir()
    rendered = render_repo_config().replace(
        "\n[lifecycle]\n", f"\n[runtime_adapter]\n{body}\n[lifecycle]\n"
    )
    (config / "config.toml").write_text(rendered, encoding="utf-8")

    with pytest.raises(SoloAIError, match=message):
        load_repo_config(GitRepo(git_repo))


def test_missing_integration_table_preserves_legacy_direct_policy(
    git_repo: Path,
) -> None:
    config = git_repo / ".solo-ai"
    config.mkdir()
    rendered = render_repo_config()
    rendered = "\n".join(
        line for line in rendered.splitlines() if not line.startswith("integration =")
    )
    (config / "config.toml").write_text(rendered, encoding="utf-8")

    loaded = load_repo_config(GitRepo(git_repo))

    assert loaded.integration.mode == "direct"
    assert loaded.integration.seal_policy == "explicit"
    assert loaded.integration.tail_policy == "explicit"


def test_batched_table_without_seal_policy_preserves_legacy_explicit_mode(
    git_repo: Path,
) -> None:
    config = git_repo / ".solo-ai"
    config.mkdir()
    rendered = render_repo_config().replace(
        ', seal_policy = "auto_full", tail_policy = "quiet_or_explicit", tail_quiet_seconds = 90',
        "",
    )
    (config / "config.toml").write_text(rendered, encoding="utf-8")

    loaded = load_repo_config(GitRepo(git_repo))

    assert loaded.integration.mode == "batched"
    assert loaded.integration.seal_policy == "explicit"
    assert loaded.integration.tail_policy == "explicit"


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ('mode = "automatic"', "integration.mode"),
        ("batch_size = 6", "batch_size"),
        ("candidate_capacity = 4", "candidate_capacity"),
        ('seal_policy = "idle"', "seal_policy"),
        ('tail_policy = "idle"', "tail_policy"),
        ("tail_quiet_seconds = 0", "tail_quiet_seconds"),
    ],
)
def test_rejects_unsafe_integration_settings(
    git_repo: Path, replacement: str, message: str
) -> None:
    config = git_repo / ".solo-ai"
    config.mkdir()
    rendered = render_repo_config()
    if replacement.startswith("mode"):
        rendered = rendered.replace('mode = "batched"', replacement)
    elif replacement.startswith("batch_size"):
        rendered = rendered.replace("batch_size = 5", replacement)
    elif replacement.startswith("candidate_capacity"):
        rendered = rendered.replace("candidate_capacity = 10", replacement)
    elif replacement.startswith("seal_policy"):
        rendered = rendered.replace('seal_policy = "auto_full"', replacement)
    elif replacement.startswith("tail_policy"):
        rendered = rendered.replace('tail_policy = "quiet_or_explicit"', replacement)
    else:
        rendered = rendered.replace("tail_quiet_seconds = 90", replacement)
    (config / "config.toml").write_text(rendered, encoding="utf-8")

    with pytest.raises(SoloAIError, match=message):
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
        "schema_version = 3\n" + body,
        encoding="utf-8",
    )

    with pytest.raises(SoloAIError, match=message):
        load_verification_config(GitRepo(git_repo))
