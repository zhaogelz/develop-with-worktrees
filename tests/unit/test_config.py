import json
from pathlib import Path

import pytest
from solo_ai.config import (
    CommandSpec,
    discover_validation_commands,
    load_repo_config,
    load_verification_config,
    managed_block,
    managed_agents_status,
    read_verification_config_file,
    remove_managed_agents_block,
    render_agents,
    render_repo_config,
    render_verification_config,
)
from solo_ai.config import (
    _legacy_managed_block,
    _pre_batch_delivery_managed_block,
    _pre_objective_protocol_managed_block,
    _pre_root_output_managed_block,
    _pre_simplification_managed_block,
    _pre_refresh_root_context_managed_block,
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


def test_discovery_fallback_renders_a_conservative_integration_full_profile() -> None:
    rendered = render_verification_config(
        [CommandSpec(("uv", "run", "pytest"))],
        static_only=False,
        discovery_fallback=True,
    )

    assert 'level = "full"' in rendered
    assert 'full_scope = "integration"' in rendered
    assert "cross_task_reuse = false" in rendered
    assert 'external_state = "unknown"' in rendered


def test_reviewed_verification_file_uses_the_tracked_policy_schema(
    tmp_path: Path,
) -> None:
    source = tmp_path / "reviewed.toml"
    source.write_text(
        """schema_version = 3
static_only = false

[[profiles]]
id = "reviewed-full"
paths = ["src/**"]
input_paths = ["src/**", "uv.lock"]
input_closure = "declared"
cross_task_reuse = false
external_state = "unknown"
environment = []
timeout_seconds = 120
resource_class = "normal"
level = "full"
full_scope = "integration"
commands = [["uv", "run", "pytest"]]
""",
        encoding="utf-8",
    )

    resolved, text, loaded = read_verification_config_file(source)

    assert resolved == source.resolve()
    assert text == source.read_text(encoding="utf-8")
    assert loaded.profiles[0].profile_id == "reviewed-full"
    assert loaded.profiles[0].level == "full"


def test_known_legacy_managed_block_can_be_upgraded_or_removed_without_touching_user_text() -> (
    None
):
    existing = "# User instructions\n\n" + _legacy_managed_block() + "\nKeep this.\n"

    assert managed_agents_status(existing) == "known-legacy-0.5.0-beta.1"
    upgraded = render_agents(existing)
    assert managed_agents_status(upgraded) == "current"
    assert upgraded.startswith("# User instructions\n\n")
    assert upgraded.endswith("\nKeep this.\n")
    assert (
        remove_managed_agents_block(upgraded) == "# User instructions\n\nKeep this.\n"
    )


def test_previous_batch_delivery_managed_block_can_be_upgraded() -> None:
    existing = (
        "# User instructions\n\n"
        + _pre_batch_delivery_managed_block()
        + "\nKeep this.\n"
    )

    assert managed_agents_status(existing) == "known-legacy-batch-delivery"
    upgraded = render_agents(existing)
    assert managed_agents_status(upgraded) == "current"
    assert "Ordinary completion does not request immediate integration" in upgraded
    assert upgraded.endswith("\nKeep this.\n")


def test_previous_objective_protocol_managed_block_can_be_upgraded() -> None:
    existing = (
        "# User instructions\n\n"
        + _pre_objective_protocol_managed_block()
        + "\nKeep this.\n"
    )

    assert managed_agents_status(existing) == "known-legacy-objective-protocol"
    upgraded = render_agents(existing)
    assert managed_agents_status(upgraded) == "current"
    assert "exact host-to-root locator" in upgraded
    assert upgraded.endswith("\nKeep this.\n")


def test_immediately_previous_released_managed_block_can_be_upgraded() -> None:
    legacy_sentence = (
        "The root is the single durable objective: it keeps the complete final plan, "
        "explicit user amendments, progress, and the checked overall result; children "
        "bind it but do not duplicate it. On continuation or candidate repair, read "
        "`anchor show --with-root` and acknowledge the reviewed plan version before editing."
    )
    released_sentence = (
        "The root is the single durable objective: it keeps the complete final plan, "
        "full prior versions of plan-changing amendments, explicit user amendments, "
        "progress, and the checked overall result; children bind it but do not duplicate "
        "it. Anchors, plan inputs, historical versions, and exact cross-repository closure "
        "state have no DWW content-size quota. On continuation or candidate repair, read "
        "the complete execution basis with `anchor show --with-root --content --root-content` "
        "when needed; `acknowledge-root` is an optional review record, not a per-operation "
        "gate. For a pre-existing active or ready task that missed the normal path, use "
        "idempotent `anchor bind-root` rather than recreating the task."
    )
    released = _legacy_managed_block().replace(legacy_sentence, released_sentence)
    existing = "# User instructions\n\n" + released + "\nKeep this.\n"

    assert managed_agents_status(existing) == "known-legacy-root-review"
    upgraded = render_agents(existing)
    assert managed_agents_status(upgraded) == "current"
    assert upgraded.endswith("\nKeep this.\n")


def test_pre_refresh_root_context_managed_block_can_be_upgraded() -> None:
    existing = (
        "# User instructions\n\n"
        + _pre_refresh_root_context_managed_block()
        + "\nKeep this.\n"
    )

    assert managed_agents_status(existing) == "known-legacy-root-context-refresh"
    upgraded = render_agents(existing)
    assert managed_agents_status(upgraded) == "current"
    assert upgraded.endswith("\nKeep this.\n")


def test_pre_simplification_managed_block_can_be_upgraded() -> None:
    existing = (
        "# User instructions\n\n"
        + _pre_simplification_managed_block()
        + "\nKeep this.\n"
    )

    assert (
        managed_agents_status(existing)
        == "known-legacy-0.5.0-beta.2-pre-simplification"
    )
    upgraded = render_agents(existing)
    assert managed_agents_status(upgraded) == "current"
    assert upgraded.endswith("\nKeep this.\n")


def test_pre_root_output_managed_block_can_be_upgraded() -> None:
    existing = (
        "# User instructions\n\n" + _pre_root_output_managed_block() + "\nKeep this.\n"
    )

    assert managed_agents_status(existing) == "known-legacy-root-output"
    upgraded = render_agents(existing)
    assert managed_agents_status(upgraded) == "current"
    assert upgraded.endswith("\nKeep this.\n")


def test_user_edited_managed_block_is_not_overwritten() -> None:
    edited = _legacy_managed_block().replace(
        "Read-only analysis does not claim a slot.",
        "Read-only analysis is handled by our team.",
    )

    with pytest.raises(SoloAIError, match="user changes or an unknown version"):
        render_agents(edited)


def test_repository_managed_block_stays_in_sync_with_the_installer_template() -> None:
    repository_agents = Path(__file__).parents[2] / "AGENTS.md"

    assert (
        managed_agents_status(repository_agents.read_text(encoding="utf-8"))
        == "current"
    )


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

    assert "DWW is local-only" in policy
    assert (
        "do not fetch, pull, push, rebase, squash, amend, or rewrite history" in policy
    )
    assert "one task anchor per task" in policy
    assert "complete plan" in policy
    assert "without a content-size limit" in policy
    assert "without a separate acknowledgement step" in policy
    assert "round-complete" in policy
    assert "one short reason" in policy
    assert "heartbeat, idle time, and task counts never seal a batch" in policy
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

    with pytest.raises(SoloAIError, match="must run at level full or stress"):
        load_verification_config(GitRepo(git_repo))


def test_full_profiles_default_to_integration_and_accept_explicit_complete_scope(
    git_repo: Path,
) -> None:
    config = git_repo / ".solo-ai"
    config.mkdir()
    path = config / "verification.toml"
    path.write_text(
        """schema_version = 3
static_only = false

[[profiles]]
id = "integration"
level = "full"
frozen_base = true
paths = ["**"]
commands = [["git", "status"]]

[[profiles]]
id = "complete"
level = "full"
full_scope = "complete"
paths = ["**"]
commands = [["git", "status"]]
""",
        encoding="utf-8",
    )

    profiles = load_verification_config(GitRepo(git_repo)).profiles
    assert [profile.full_scope for profile in profiles] == ["integration", "complete"]
    assert [profile.frozen_base for profile in profiles] == [True, False]

    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'full_scope = "complete"', 'full_scope = "unexpected"'
        ),
        encoding="utf-8",
    )
    with pytest.raises(SoloAIError, match="full_scope"):
        load_verification_config(GitRepo(git_repo))

    path.write_text(
        path.read_text(encoding="utf-8")
        .replace('full_scope = "unexpected"', 'full_scope = "complete"')
        .replace(
            'level = "full"\nfrozen_base = true',
            'level = "development"\nfrozen_base = true',
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(SoloAIError, match="frozen_base"):
        load_verification_config(GitRepo(git_repo))


def test_accepts_heavy_stress_profiles(git_repo: Path) -> None:
    config = git_repo / ".solo-ai"
    config.mkdir()
    (config / "verification.toml").write_text(
        """schema_version = 3
static_only = false

[[profiles]]
id = "explicit-stress"
level = "stress"
resource_class = "heavy"
paths = ["**"]
commands = [["git", "status"]]
""",
        encoding="utf-8",
    )

    profile = load_verification_config(GitRepo(git_repo)).profiles[0]
    assert (profile.level, profile.resource_class) == ("stress", "heavy")


def test_loads_stress_profiles_from_a_supplement_without_changing_primary_policy(
    git_repo: Path,
) -> None:
    config = git_repo / ".solo-ai"
    config.mkdir()
    (config / "verification.toml").write_text(
        """schema_version = 3
static_only = false

[[profiles]]
id = "ready"
level = "ready"
paths = ["**"]
commands = [["git", "status"]]
""",
        encoding="utf-8",
    )
    (config / "stress-verification.toml").write_text(
        """schema_version = 3
static_only = false

[[profiles]]
id = "stress"
level = "stress"
resource_class = "heavy"
paths = ["**"]
commands = [["git", "status"]]
""",
        encoding="utf-8",
    )

    profiles = load_verification_config(GitRepo(git_repo)).profiles

    assert [(profile.profile_id, profile.level) for profile in profiles] == [
        ("ready", "ready"),
        ("stress", "stress"),
    ]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            """static_only = false

[[profiles]]
id = "not-stress"
level = "full"
paths = ["**"]
commands = [["git", "status"]]
""",
            "must run at level stress",
        ),
        (
            """static_only = true
""",
            "cannot enable static_only",
        ),
    ],
)
def test_rejects_invalid_stress_verification_supplement(
    git_repo: Path, body: str, message: str
) -> None:
    config = git_repo / ".solo-ai"
    config.mkdir()
    (config / "verification.toml").write_text(
        "schema_version = 3\nstatic_only = true\n", encoding="utf-8"
    )
    (config / "stress-verification.toml").write_text(
        "schema_version = 3\n" + body, encoding="utf-8"
    )

    with pytest.raises(SoloAIError, match=message):
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


def test_new_integration_defaults_are_batched_auto_full_with_three_and_ten(
    git_repo: Path,
) -> None:
    config = git_repo / ".solo-ai"
    config.mkdir()
    (config / "config.toml").write_text(render_repo_config(), encoding="utf-8")

    loaded = load_repo_config(GitRepo(git_repo))

    assert loaded.integration.mode == "batched"
    assert loaded.integration.batch_size == 3
    assert loaded.integration.candidate_capacity == 10
    assert loaded.integration.seal_policy == "auto_full"
    assert loaded.integration.candidate_validation == "batch"
    assert loaded.integration.tail_policy == "explicit"
    assert loaded.integration.tail_quiet_seconds == 30
    assert loaded.runtime_adapter.activate is None
    assert loaded.runtime_adapter.release is None
    assert loaded.runtime_adapter.batch_activate is None
    assert loaded.runtime_adapter.batch_release is None
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
batch_activate = ["uv", "run", "scripts/runtime-adapter.py", "batch-activate"]
batch_release = ["uv", "run", "scripts/runtime-adapter.py", "batch-release"]
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
    assert loaded.runtime_adapter.batch_activate is not None
    assert loaded.runtime_adapter.batch_activate.argv[-1] == "batch-activate"
    assert loaded.runtime_adapter.batch_release is not None
    assert loaded.runtime_adapter.batch_release.argv[-1] == "batch-release"
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
        (
            'batch_activate = ["uv", "run", "adapter.py"]\ninput_paths = ["adapter.py"]\n',
            "batch_activate and batch_release",
        ),
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


def test_batch_runtime_adapter_requires_space_after_all_task_port_blocks(
    git_repo: Path,
) -> None:
    config = git_repo / ".solo-ai"
    config.mkdir()
    rendered = render_repo_config().replace("port_base = 20000", "port_base = 62336")
    rendered = rendered.replace(
        "\n[lifecycle]\n",
        """
[runtime_adapter]
batch_activate = ["uv", "run", "adapter.py", "batch-activate"]
batch_release = ["uv", "run", "adapter.py", "batch-release"]
input_paths = ["adapter.py"]

[lifecycle]
""",
    )
    (config / "config.toml").write_text(rendered, encoding="utf-8")

    with pytest.raises(SoloAIError, match="dedicated batch Adapter port block"):
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
    assert loaded.integration.candidate_validation == "ready"
    assert loaded.integration.tail_policy == "explicit"


def test_batched_table_without_seal_policy_preserves_legacy_explicit_mode(
    git_repo: Path,
) -> None:
    config = git_repo / ".solo-ai"
    config.mkdir()
    rendered = render_repo_config().replace(
        ', seal_policy = "auto_full", candidate_validation = "batch", tail_policy = "explicit", tail_quiet_seconds = 30',
        "",
    )
    (config / "config.toml").write_text(rendered, encoding="utf-8")

    loaded = load_repo_config(GitRepo(git_repo))

    assert loaded.integration.mode == "batched"
    assert loaded.integration.seal_policy == "explicit"
    assert loaded.integration.candidate_validation == "ready"
    assert loaded.integration.tail_policy == "explicit"


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ('mode = "automatic"', "integration.mode"),
        ("batch_size = 6", "batch_size"),
        ("candidate_capacity = 1", "candidate_capacity"),
        ('seal_policy = "idle"', "seal_policy"),
        ('candidate_validation = "idle"', "candidate_validation"),
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
        rendered = rendered.replace("batch_size = 3", replacement)
    elif replacement.startswith("candidate_capacity"):
        rendered = rendered.replace("candidate_capacity = 10", replacement)
    elif replacement.startswith("seal_policy"):
        rendered = rendered.replace('seal_policy = "auto_full"', replacement)
    elif replacement.startswith("candidate_validation"):
        rendered = rendered.replace('candidate_validation = "batch"', replacement)
    elif replacement.startswith("tail_policy"):
        rendered = rendered.replace('tail_policy = "explicit"', replacement)
    else:
        rendered = rendered.replace("tail_quiet_seconds = 30", replacement)
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
