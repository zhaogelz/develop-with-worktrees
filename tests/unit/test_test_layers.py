from __future__ import annotations

import tomllib
from pathlib import Path

from conftest import dww_test_layer


_REPOSITORY_ROOT = Path(__file__).parents[2]


def test_known_local_contract_tests_are_fast() -> None:
    assert dww_test_layer(Path("tests/unit/test_config.py")) == "dww_fast"
    assert dww_test_layer(Path("tests/unit/test_proof.py")) == "dww_fast"
    assert dww_test_layer(Path("tests/unit/test_hook.py")) == "dww_fast"


def test_new_test_modules_default_to_full() -> None:
    assert dww_test_layer(Path("tests/unit/test_new_contract.py")) == "dww_full"


def test_fast_proof_covers_its_real_configuration_inputs_and_development_lint_is_locked() -> (
    None
):
    with (_REPOSITORY_ROOT / ".solo-ai" / "verification.toml").open("rb") as handle:
        primary = tomllib.load(handle)

    profiles = primary["profiles"]
    fast = next(profile for profile in profiles if profile["id"] == "dww-fast-ready")
    assert {
        "AGENTS.md",
        ".solo-ai/verification.toml",
        ".solo-ai/stress-verification.toml",
        "tests/**",
    } <= set(fast["input_paths"])
    ids = [profile["id"] for profile in profiles]
    lint = next(
        profile for profile in profiles if profile["id"] == "dww-lint-development"
    )
    assert ids.index(lint["id"]) < ids.index("dww-fast-ready")
    assert lint["level"] == "development"
    assert lint["commands"] == [
        ["uv", "run", "ruff", "check", "."],
        ["uv", "run", "ruff", "format", "--check", "."],
    ]
    assert lint["resource_class"] == "light"
    assert lint["input_paths"] == ["**"]
    assert lint["input_closure"] == "complete"
    assert lint["cross_task_reuse"] is True


def test_complete_full_profile_excludes_the_explicit_stress_layer() -> None:
    """完整回归与显式 Stress 必须互斥，避免一次里程碑验证重复长测。"""

    with (_REPOSITORY_ROOT / ".solo-ai" / "verification.toml").open("rb") as handle:
        primary = tomllib.load(handle)
    with (_REPOSITORY_ROOT / ".solo-ai" / "stress-verification.toml").open(
        "rb"
    ) as handle:
        stress = tomllib.load(handle)

    full = next(
        profile
        for profile in primary["profiles"]
        if profile["id"] == "dww-core-complete"
    )
    explicit_stress = next(
        profile for profile in stress["profiles"] if profile["id"] == "dww-stress"
    )

    assert full["commands"] == [
        [
            "uv",
            "run",
            "pytest",
            "-m",
            "dww_full and not dww_stress",
            "-vv",
            "-x",
            "--durations=30",
        ]
    ]
    assert full["full_scope"] == "complete"
    assert explicit_stress["commands"] == [
        ["uv", "run", "pytest", "-m", "dww_stress", "-vv", "-x", "--durations=30"]
    ]


def test_integration_full_keeps_the_fast_refactor_safety_boundaries() -> None:
    """本轮没有缩小 Full；新增的查询和恢复边界必须实际经过候选门禁。"""

    with (_REPOSITORY_ROOT / ".solo-ai" / "verification.toml").open("rb") as handle:
        primary = tomllib.load(handle)
    integration = next(
        profile
        for profile in primary["profiles"]
        if profile["id"] == "dww-batch-integration"
    )
    command = integration["commands"][0]

    assert integration["full_scope"] == "integration"
    assert (
        "tests/integration/test_candidate_batches.py::test_compact_exact_task_query_ignores_unrelated_terminal_history"
        in command
    )
    assert (
        "tests/integration/test_candidate_batches.py::test_finish_delivery_intent_is_validated_persisted_and_recovered_once"
        in command
    )
    assert (
        "tests/integration/test_lifecycle.py::test_structured_root_review_recovers_automatically_before_commit_and_ready"
        in command
    )
    # Full 会先包含 Ready；快速层已经覆盖此配置契约，组合命令不能再跑一次。
    assert "tests/unit/test_config.py" not in command
    assert "tests/unit/test_config.py" not in integration["input_paths"]
    assert (
        "tests/integration/test_lifecycle.py::test_ready_does_not_blindly_rerun_an_unchanged_deterministic_failure"
        in command
    )
    assert [
        "uv",
        "run",
        "pytest",
        "-q",
        "tests/integration/test_candidate_batches.py::test_failed_combined_validation_preserves_base_and_generation_is_not_rerun",
    ] in integration["commands"]
