from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
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


def _wait_for(path: Path, *, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert path.exists(), f"timed out waiting for {path}"


def test_pytest_process_probe(tmp_path: Path) -> None:
    """供两个 pytest 子进程验证各自临时目录不会互相清理。"""

    probe_root = os.environ.get("DWW_PYTEST_PROBE_ROOT")
    role = os.environ.get("DWW_PYTEST_PROBE_ROLE")
    if not probe_root or role not in {"first", "second"}:
        return
    root = Path(probe_root)
    own_marker = tmp_path / f"{role}.txt"
    own_marker.write_text(role, encoding="utf-8")
    if role == "first":
        (root / "first-ready").write_text(str(own_marker), encoding="utf-8")
        _wait_for(root / "second-ready")
        assert own_marker.read_text(encoding="utf-8") == "first"
        (root / "first-checked").touch()
        return
    _wait_for(root / "first-ready")
    (root / "second-ready").touch()
    _wait_for(root / "first-checked")
    assert own_marker.read_text(encoding="utf-8") == "second"


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
    unsafe = _managed_worktree_root() / f"pytest-unsafe-basetemp-{tmp_path.name}"
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

    first = _configure_pytest_temp_root(None)
    second = _configure_pytest_temp_root(None)
    try:
        assert first is not None
        assert second is not None
        assert first.parent == fallback
        assert second.parent == fallback
        assert first.name.startswith("p")
        assert second.name.startswith("p")
        assert first != second
    finally:
        if first is not None:
            shutil.rmtree(first, ignore_errors=True)
        if second is not None:
            shutil.rmtree(second, ignore_errors=True)
    assert os.environ.get("PYTEST_DEBUG_TEMPROOT") is None


def test_parallel_pytest_fallbacks_keep_each_process_temporary_files(
    tmp_path: Path,
) -> None:
    probe_root = tmp_path / "probe"
    probe_root.mkdir()
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        f"{Path(__file__).resolve()}::test_pytest_process_probe",
    ]
    shared_environment = {
        **os.environ,
        "PYTEST_DEBUG_TEMPROOT": str(_managed_worktree_root() / "forced-fallback"),
        "DWW_PYTEST_PROBE_ROOT": str(probe_root),
    }
    first = subprocess.Popen(
        command,
        cwd=_TEST_ROOT,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**shared_environment, "DWW_PYTEST_PROBE_ROLE": "first"},
    )
    second: subprocess.Popen[str] | None = None
    try:
        _wait_for(probe_root / "first-ready")
        second = subprocess.Popen(
            command,
            cwd=_TEST_ROOT,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**shared_environment, "DWW_PYTEST_PROBE_ROLE": "second"},
        )
        second_output, second_error = second.communicate(timeout=30)
        first_output, first_error = first.communicate(timeout=30)
    except BaseException:
        for process in (first, second):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=10)
        raise
    assert first.returncode == 0, first_output + first_error
    assert second.returncode == 0, second_output + second_error
