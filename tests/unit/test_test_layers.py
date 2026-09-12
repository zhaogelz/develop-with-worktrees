from __future__ import annotations

import tomllib
from pathlib import Path

from conftest import dww_test_layer


_REPOSITORY_ROOT = Path(__file__).parents[2]


def test_known_local_contract_tests_are_fast() -> None:
    assert dww_test_layer(Path("tests/unit/test_config.py")) == "dww_fast"
    assert dww_test_layer(Path("tests/unit/test_proof.py")) == "dww_fast"


def test_new_test_modules_default_to_full() -> None:
    assert dww_test_layer(Path("tests/unit/test_new_contract.py")) == "dww_full"


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
