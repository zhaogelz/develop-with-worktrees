from __future__ import annotations

from pathlib import Path

from conftest import dww_test_layer


def test_known_local_contract_tests_are_fast() -> None:
    assert dww_test_layer(Path("tests/unit/test_config.py")) == "dww_fast"
    assert dww_test_layer(Path("tests/unit/test_proof.py")) == "dww_fast"


def test_new_test_modules_default_to_full() -> None:
    assert dww_test_layer(Path("tests/unit/test_new_contract.py")) == "dww_full"
