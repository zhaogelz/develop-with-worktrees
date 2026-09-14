from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import (
    _TEST_ROOT,
    _configure_pytest_temp_root,
    _default_pytest_temp_root,
    _is_within,
    _managed_worktree_root,
    _pytest_machine_state_root,
    _pytest_fallback_temp_root,
)


def test_pytest_process_probe() -> None:
    """供子进程验证 pytest 配置钩子的最小测试。"""


def test_default_temp_root_falls_back_when_configured_root_is_managed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed = _managed_worktree_root() / "solo-ai-slot-01" / ".tmp"
    monkeypatch.setenv("PYTEST_DEBUG_TEMPROOT", str(managed))

    assert _default_pytest_temp_root() == _pytest_fallback_temp_root()


def test_default_temp_root_falls_back_when_system_temp_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import conftest

    monkeypatch.delenv("PYTEST_DEBUG_TEMPROOT", raising=False)
    monkeypatch.setattr(
        conftest.tempfile, "gettempdir", lambda: (_ for _ in ()).throw(OSError())
    )

    assert _default_pytest_temp_root() == _pytest_fallback_temp_root()


def test_default_temp_root_falls_back_when_pytest_subdirectory_is_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import conftest

    monkeypatch.delenv("PYTEST_DEBUG_TEMPROOT", raising=False)
    monkeypatch.setattr(conftest.tempfile, "gettempdir", lambda: str(_TEST_ROOT))
    monkeypatch.setattr(conftest, "_is_usable_pytest_temp_root", lambda root: False)

    assert _default_pytest_temp_root() == _pytest_fallback_temp_root()


def test_default_tmp_path_is_outside_managed_worktrees(tmp_path: Path) -> None:
    assert not _is_within(tmp_path, _managed_worktree_root())


def test_machine_state_fallback_is_outside_managed_worktrees() -> None:
    root = _pytest_machine_state_root()
    assert not _is_within(root, _managed_worktree_root())
    assert root.parent == _pytest_fallback_temp_root().parent


def test_explicit_managed_basetemp_is_rejected_before_pytest_can_clean_it(
    tmp_path: Path,
) -> None:
    unsafe = _TEST_ROOT / ".tmp" / f"pytest-unsafe-basetemp-{tmp_path.name}"
    marker = unsafe / "marker.txt"
    unsafe.mkdir(parents=True)
    marker.write_text("preserve", encoding="utf-8")
    try:
        command = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--basetemp",
            str(unsafe),
            f"{Path(__file__).resolve()}::test_pytest_process_probe",
        ]
        completed = subprocess.run(
            command,
            cwd=_TEST_ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )

        assert completed.returncode == pytest.ExitCode.USAGE_ERROR, completed.stderr
        assert "--basetemp must be outside DWW managed worktrees" in completed.stderr
        assert marker.read_text(encoding="utf-8") == "preserve"
    finally:
        shutil.rmtree(unsafe)


def test_configure_uses_fallback_without_changing_a_safe_explicit_basetemp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("PYTEST_DEBUG_TEMPROOT", raising=False)

    _configure_pytest_temp_root(tmp_path)

    assert os.environ.get("PYTEST_DEBUG_TEMPROOT") is None


def test_configure_uses_short_direct_basetemp_for_the_primary_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import conftest

    fallback = _pytest_fallback_temp_root()
    monkeypatch.delenv("PYTEST_DEBUG_TEMPROOT", raising=False)
    monkeypatch.setattr(conftest, "_default_pytest_temp_root", lambda: fallback)
    monkeypatch.setattr(conftest, "_is_usable_pytest_temp_root", lambda root: True)

    assert _configure_pytest_temp_root(None) == fallback / "p"
    assert os.environ.get("PYTEST_DEBUG_TEMPROOT") is None
