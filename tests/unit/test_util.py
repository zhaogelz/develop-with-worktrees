import errno
import json
import signal
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
from solo_ai import lifecycle, util
from solo_ai.util import DirectoryLock, SoloAIError, redact_text, run_logged


def test_redacts_common_secret_shapes() -> None:
    raw = "token=super-secret sk-proj-abcdefghijklmnopqrstuvwxyz123456"
    result = redact_text(raw)
    assert "super-secret" not in result
    assert "sk-proj" not in result
    assert result.count("[REDACTED]") == 2


def test_directory_lock_rejects_live_owner(tmp_path: Path) -> None:
    path = tmp_path / "lock"
    with DirectoryLock(path):
        try:
            with DirectoryLock(path):
                raise AssertionError("lock was acquired twice")
        except SoloAIError:
            pass


def test_directory_lock_normalizes_nonempty_destination_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "lock"
    original_rename = Path.rename

    def raise_nonempty_for_pending(self: Path, target: Path) -> Path:
        if self.name.endswith(".pending"):
            raise OSError(errno.ENOTEMPTY, "Directory not empty")
        return original_rename(self, target)

    with DirectoryLock(path):
        monkeypatch.setattr(Path, "rename", raise_nonempty_for_pending)
        with (
            pytest.raises(SoloAIError, match="Operation is already active"),
            DirectoryLock(path),
        ):
            raise AssertionError("lock was acquired twice")


def test_directory_lock_retries_transient_owner_read_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "lock"
    original_read_json = util.read_json
    owner_read_failed = threading.Event()
    acquired = threading.Event()
    errors: list[Exception] = []

    def transient_owner_read(target: Path, default: object) -> object:
        if target == path / "owner.json" and not owner_read_failed.is_set():
            owner_read_failed.set()
            cause = PermissionError(errno.EACCES, "Windows transient owner read failure")
            raise SoloAIError("Local state is temporarily unreadable") from cause
        return original_read_json(target, default)

    def wait_for_lock() -> None:
        try:
            with DirectoryLock(path, wait=True):
                acquired.set()
        except Exception as exc:  # noqa: BLE001 - 断言等待线程不会泄漏异常。
            errors.append(exc)

    with DirectoryLock(path):
        monkeypatch.setattr(util, "read_json", transient_owner_read)
        thread = threading.Thread(target=wait_for_lock)
        thread.start()
        assert owner_read_failed.wait(timeout=2)
        assert not acquired.is_set()

    thread.join(timeout=2)
    assert not thread.is_alive()
    assert errors == []
    assert acquired.is_set()


def test_directory_lock_retries_transient_release_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "lock"
    original_rename = Path.rename
    release_attempts = 0

    def transient_release(self: Path, target: Path) -> Path:
        nonlocal release_attempts
        if self == path and target.name.endswith(".releasing") and release_attempts == 0:
            release_attempts += 1
            raise PermissionError(errno.EACCES, "Windows transient release failure")
        return original_rename(self, target)

    with DirectoryLock(path):
        monkeypatch.setattr(Path, "rename", transient_release)

    assert release_attempts == 1
    assert not path.exists()
    assert not list(tmp_path.glob("*.releasing"))


def test_unix_process_group_stops_with_term_before_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, signal.Signals]] = []

    class ExitedProcess:
        pid = 321

        def is_running(self) -> bool:
            return False

    monkeypatch.setattr(
        lifecycle.os,
        "killpg",
        lambda pid, value: calls.append((pid, value)),
        raising=False,
    )

    assert lifecycle._stop_unix_process_group(ExitedProcess()) is True
    assert calls == [(321, signal.SIGTERM)]


def test_tcp_readiness_uses_a_bounded_connection_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]

        def blocking_connection_was_used(*args: object, **kwargs: object) -> None:
            raise AssertionError("TCP readiness must not use create_connection")

        monkeypatch.setattr(
            lifecycle.socket, "create_connection", blocking_connection_was_used
        )
        assert lifecycle._ready("tcp", f"127.0.0.1:{port}", port=port) is True


def test_logged_run_times_out_with_heartbeat_and_receipt(tmp_path: Path) -> None:
    log_path = tmp_path / "run.log"
    receipt_path = tmp_path / "receipt.json"
    heartbeats: list[dict[str, object]] = []

    result = run_logged(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        cwd=tmp_path,
        log_path=log_path,
        timeout_seconds=0.3,
        heartbeat_seconds=0.05,
        on_heartbeat=heartbeats.append,
        receipt_path=receipt_path,
    )

    assert result.timed_out is True
    assert result.duration_seconds < 3
    assert heartbeats
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "timed_out"
    assert receipt["process"]["pid"] == result.process["pid"]
    assert "timeout: owned process tree termination requested" in log_path.read_text(
        encoding="utf-8"
    )


def test_logged_run_timeout_owns_its_popen_without_snapshot_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(util, "process_matches", lambda snapshot: False)

    result = run_logged(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        cwd=tmp_path,
        log_path=tmp_path / "owned-timeout.log",
        timeout_seconds=0.1,
        heartbeat_seconds=0.02,
    )

    assert result.timed_out is True
    assert result.duration_seconds < 3


def test_logged_run_finishes_when_output_is_silent(tmp_path: Path) -> None:
    started = time.monotonic()
    result = run_logged(
        [sys.executable, "-c", "import time; time.sleep(0.15)"],
        cwd=tmp_path,
        log_path=tmp_path / "silent.log",
        timeout_seconds=1,
        heartbeat_seconds=0.05,
    )
    assert result.returncode == 0
    assert result.timed_out is False
    assert time.monotonic() - started < 1


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM ignore is POSIX-specific")
def test_logged_run_force_stops_a_process_that_ignores_sigterm(tmp_path: Path) -> None:
    result = run_logged(
        [
            sys.executable,
            "-c",
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)",
        ],
        cwd=tmp_path,
        log_path=tmp_path / "ignored-term.log",
        timeout_seconds=0.1,
        heartbeat_seconds=0.02,
        termination_grace_seconds=0.1,
    )

    assert result.timed_out is True
    assert result.duration_seconds < 3
    assert "force termination requested" in (tmp_path / "ignored-term.log").read_text(
        encoding="utf-8"
    )
