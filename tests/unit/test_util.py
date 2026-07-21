import errno
import signal
import socket
from pathlib import Path

import pytest

import solo_ai.lifecycle as lifecycle
from solo_ai.util import DirectoryLock, SoloAIError, redact_text


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
        with pytest.raises(SoloAIError, match="Operation is already active"):
            with DirectoryLock(path):
                raise AssertionError("lock was acquired twice")


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
