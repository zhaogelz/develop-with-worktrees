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


class AdvancingClock:
    """用确定性单调时间验证超时状态机，不把测试时长交给真实睡眠。"""

    def __init__(self, start: float = 0.0, step: float = 1.0) -> None:
        self.current = start
        self.step = step

    def __call__(self) -> float:
        value = self.current
        self.current += self.step
        return value


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 共享删除语义")
def test_atomic_write_survives_a_short_windows_reader(tmp_path: Path) -> None:
    import ctypes
    from ctypes import wintypes

    target = tmp_path / "receipt.json"
    util.atomic_write_json(target, {"generation": 1})
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    close = kernel32.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    # 与普通只读观察者相同：允许读写，但暂不允许替换该目录项。
    reader = create(str(target), 0x80000000, 0x3, None, 3, 0x80, None)
    assert reader != wintypes.HANDLE(-1).value
    released = threading.Event()

    def release_reader() -> None:
        time.sleep(0.15)
        close(reader)
        released.set()

    thread = threading.Thread(target=release_reader)
    thread.start()
    try:
        util.atomic_write_json(target, {"generation": 2})
    finally:
        thread.join(timeout=2)
    assert released.is_set()
    assert json.loads(target.read_text(encoding="utf-8")) == {"generation": 2}
    assert not list(tmp_path.glob(".w-*"))


def test_atomic_write_keeps_old_value_when_windows_denial_persists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "receipt.json"
    util.atomic_write_json(target, {"generation": 1})
    attempts = []

    def deny_replace(source: Path, destination: Path) -> None:
        attempts.append(source)
        error = PermissionError(errno.EACCES, "persistent-denial")
        error.winerror = 5
        raise error

    monkeypatch.setattr(util.sys, "platform", "win32")
    monkeypatch.setattr(util.os, "replace", deny_replace)
    started = time.monotonic()
    with pytest.raises(PermissionError, match="persistent-denial"):
        util.atomic_write_json(target, {"generation": 2})
    assert time.monotonic() - started < 3
    assert len(attempts) > 1
    assert len(set(attempts)) == 1
    assert json.loads(target.read_text(encoding="utf-8")) == {"generation": 1}
    assert not list(tmp_path.glob(".w-*"))


def test_atomic_write_does_not_retry_unrelated_io_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "receipt.json"
    util.atomic_write_json(target, {"generation": 1})
    attempts = []

    def disk_full(source: Path, destination: Path) -> None:
        attempts.append(source)
        raise OSError(errno.ENOSPC, "fixture-disk-full")

    monkeypatch.setattr(util.os, "replace", disk_full)
    with pytest.raises(OSError, match="fixture-disk-full"):
        util.atomic_write_json(target, {"generation": 2})
    assert len(attempts) == 1
    assert json.loads(target.read_text(encoding="utf-8")) == {"generation": 1}
    assert not list(tmp_path.glob(".w-*"))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows MAX_PATH 真实回归")
def test_atomic_write_uses_extended_paths_for_deep_parent(tmp_path: Path) -> None:
    parent = tmp_path
    while len(str(parent)) < 310:
        parent /= "nested-atomic-write-0123456789"
    util.filesystem_path(parent).mkdir(parents=True)
    target = parent / "state.json"

    util.atomic_write_json(target, {"generation": 1})

    assert json.loads(util.filesystem_path(target).read_text(encoding="utf-8")) == {
        "generation": 1
    }
    assert not list(util.filesystem_path(parent).glob(".w-*"))


@pytest.mark.parametrize(
    "failure_stage",
    [
        "initial-receipt",
        "heartbeat",
        "callback",
        "process-snapshot",
        "final-receipt",
        "log",
    ],
)
def test_logged_run_does_not_orphan_its_process_on_observation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    created = []
    original_popen = util.subprocess.Popen
    original_write = util.atomic_write_json
    original_open = Path.open

    def capture_process(*args: object, **kwargs: object):
        process = original_popen(*args, **kwargs)
        created.append(process)
        return process

    def fail_receipt(path: Path, receipt: object) -> None:
        is_heartbeat = "last_heartbeat_at" in receipt
        if (
            failure_stage == "initial-receipt"
            or (failure_stage == "heartbeat" and is_heartbeat)
            or (
                failure_stage == "final-receipt" and receipt.get("status") == "finished"
            )
        ):
            raise PermissionError("fixture-observation-denied")
        original_write(path, receipt)

    def fail_callback(_heartbeat: object) -> None:
        if failure_stage == "callback":
            raise PermissionError("fixture-observation-denied")

    monkeypatch.setattr(util.subprocess, "Popen", capture_process)
    monkeypatch.setattr(util, "atomic_write_json", fail_receipt)
    if failure_stage == "process-snapshot":

        def denied_snapshot(_pid: int) -> None:
            raise PermissionError("fixture-observation-denied")

        monkeypatch.setattr(util, "process_snapshot", denied_snapshot)
    if failure_stage == "log":

        class FailedLog:
            def __init__(self, handle):
                self.handle = handle
                self.writes = 0

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.handle.close()

            def write(self, value):
                self.writes += 1
                if self.writes > 1:
                    raise PermissionError("fixture-observation-denied")
                return self.handle.write(value)

            def flush(self):
                self.handle.flush()

        def open_log(path, *args, **kwargs):
            handle = original_open(path, *args, **kwargs)
            return FailedLog(handle) if path == tmp_path / "run.log" else handle

        monkeypatch.setattr(Path, "open", open_log)
    try:
        with pytest.raises(PermissionError, match="fixture-observation-denied"):
            run_logged(
                [
                    sys.executable,
                    "-c",
                    "pass"
                    if failure_stage == "final-receipt"
                    else "import time; time.sleep(30)",
                ],
                cwd=tmp_path,
                log_path=tmp_path / "run.log",
                receipt_path=tmp_path / "receipt.json",
                heartbeat_seconds=0.05,
                on_heartbeat=fail_callback,
            )
        assert len(created) == 1
        assert created[0].poll() is not None, "观察失败后遗留了本次验证进程"
    finally:
        # 失败的旧实现也不能让测试夹具泄漏进程；只收束本测试持有的 Popen。
        for process in created:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


def test_observation_failure_escalates_when_graceful_stop_does_not_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = []
    stops = []
    original_popen = util.subprocess.Popen
    original_stop = util._stop_process_tree

    def capture_process(*args: object, **kwargs: object):
        process = original_popen(*args, **kwargs)
        created.append(process)
        return process

    def ignore_graceful_stop(pid: int, *, force: bool) -> bool:
        stops.append(force)
        return original_stop(pid, force=True) if force else True

    def fail_callback(_heartbeat: object) -> None:
        raise PermissionError("fixture-escalation-denied")

    monkeypatch.setattr(util.subprocess, "Popen", capture_process)
    monkeypatch.setattr(util, "_stop_process_tree", ignore_graceful_stop)
    try:
        with pytest.raises(PermissionError, match="fixture-escalation-denied"):
            run_logged(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                cwd=tmp_path,
                log_path=tmp_path / "run.log",
                heartbeat_seconds=0.05,
                termination_grace_seconds=0.1,
                on_heartbeat=fail_callback,
            )
        assert stops == [False, True]
        assert len(created) == 1
        assert created[0].poll() is not None
    finally:
        for process in created:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


def test_observation_failure_stops_the_owned_descendant_too(tmp_path: Path) -> None:
    child_marker = tmp_path / "child.txt"
    children = []

    def fail_after_child_started(_heartbeat: object) -> None:
        if child_marker.exists():
            children.append(util.psutil.Process(int(child_marker.read_text())))
            raise PermissionError("fixture-child-observation-denied")

    command = (
        "import subprocess, sys, time; from pathlib import Path; "
        "child=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        "Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(30)"
    )
    try:
        with pytest.raises(PermissionError, match="fixture-child-observation-denied"):
            run_logged(
                [sys.executable, "-c", command, str(child_marker)],
                cwd=tmp_path,
                log_path=tmp_path / "run.log",
                heartbeat_seconds=0.1,
                timeout_seconds=5,
                on_heartbeat=fail_after_child_started,
            )
        assert len(children) == 1
        assert (
            not children[0].is_running()
            or children[0].status() == util.psutil.STATUS_ZOMBIE
        )
    finally:
        for child in children:
            if child.is_running() and child.status() != util.psutil.STATUS_ZOMBIE:
                child.kill()
                child.wait(timeout=5)


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
        if self.name.startswith(".dww-p-"):
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
        if (
            target == util.filesystem_path(path / "owner.json")
            and not owner_read_failed.is_set()
        ):
            owner_read_failed.set()
            cause = PermissionError(
                errno.EACCES, "Windows transient owner read failure"
            )
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


def test_directory_lock_retries_transient_acquire_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "lock"
    original_rename = Path.rename
    acquire_attempts = 0

    def transient_acquire(self: Path, target: Path) -> Path:
        nonlocal acquire_attempts
        if (
            self.name.startswith(".dww-p-")
            and target == util.filesystem_path(path)
            and acquire_attempts == 0
        ):
            acquire_attempts += 1
            raise PermissionError(errno.EACCES, "Windows transient acquire failure")
        return original_rename(self, target)

    monkeypatch.setattr(Path, "rename", transient_acquire)
    with DirectoryLock(path):
        assert path.is_dir()

    assert acquire_attempts == 1
    assert not path.exists()


def test_directory_lock_retries_transient_release_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "lock"
    original_rename = Path.rename
    release_attempts = 0

    def transient_release(self: Path, target: Path) -> Path:
        nonlocal release_attempts
        if (
            self == util.filesystem_path(path)
            and target.name.startswith(".dww-r-")
            and release_attempts == 0
        ):
            release_attempts += 1
            raise PermissionError(errno.EACCES, "Windows transient release failure")
        return original_rename(self, target)

    with DirectoryLock(path):
        monkeypatch.setattr(Path, "rename", transient_release)

    assert release_attempts == 1
    assert not path.exists()
    assert not list(tmp_path.glob(".dww-r-*"))


@pytest.mark.parametrize("parent_length", [205, 217, 260, 320])
@pytest.mark.parametrize("stale_owner", [False, True])
def test_directory_lock_uses_bounded_internal_names_in_a_deep_path(
    tmp_path: Path, parent_length: int, stale_owner: bool
) -> None:
    parent = tmp_path
    while parent_length - len(str(parent)) > 180:
        parent /= "deep-segment"
    parent /= "p" * max(1, parent_length - len(str(parent)) - 1)
    util.filesystem_path(parent).mkdir(parents=True)
    path = parent / "batch-123456789012345678901234.lock"
    access_path = util.filesystem_path(path)
    if stale_owner:
        access_path.mkdir()
        util.atomic_write_json(access_path / "owner.json", {"pid": 0})

    with DirectoryLock(path) as lock:
        assert lock.path == path
        assert access_path.is_dir()
        assert (access_path / "owner.json").is_file()
        assert util.process_matches(util.read_json(access_path / "owner.json", {}))
        with (
            pytest.raises(SoloAIError, match="Operation is already active"),
            DirectoryLock(path),
        ):
            raise AssertionError("lock was acquired twice")

    assert not access_path.exists()
    assert not list(util.filesystem_path(parent).glob(".dww-*-*"))


@pytest.mark.skipif(
    sys.platform != "win32", reason="Windows path stat and handle fstat modes differ"
)
def test_conditional_delete_accepts_same_windows_file_identity(tmp_path: Path) -> None:
    path = tmp_path / "generated.bat"
    path.write_text("@echo off\r\n", encoding="utf-8")
    expected = util.snapshot_plain_path(path)

    util.delete_plain_path_if_unchanged(path, expected)

    assert not path.exists()


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
    clock = AdvancingClock()
    wall_started = time.monotonic()

    result = run_logged(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        cwd=tmp_path,
        log_path=log_path,
        timeout_seconds=3,
        heartbeat_seconds=1,
        on_heartbeat=heartbeats.append,
        receipt_path=receipt_path,
        monotonic=clock,
        poll_interval_seconds=0,
    )

    assert result.timed_out is True
    assert result.duration_seconds >= 3
    assert time.monotonic() - wall_started < 3
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
        timeout_seconds=10,
        heartbeat_seconds=0.05,
    )
    assert result.returncode == 0
    assert result.timed_out is False
    # Windows 进程创建和身份采集在杀毒扫描或高负载下可能超过一秒；这里验证的
    # 契约是静默且已退出的进程不会一直等到十秒超时，而不是调度器的亚秒性能。
    assert time.monotonic() - started < 5


def test_logged_run_marks_receipt_interrupted_when_observation_raises(
    tmp_path: Path,
) -> None:
    receipt_path = tmp_path / "interrupted-receipt.json"

    def stop_after_heartbeat(_heartbeat: object) -> None:
        raise RuntimeError("fixture stops observation")

    with pytest.raises(RuntimeError, match="fixture stops observation"):
        run_logged(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=tmp_path,
            log_path=tmp_path / "interrupted.log",
            receipt_path=receipt_path,
            heartbeat_seconds=0.01,
            termination_grace_seconds=0.1,
            on_heartbeat=stop_after_heartbeat,
        )

    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "interrupted"
    assert receipt["interrupted"] is True
    assert receipt["duration_seconds"] >= 0


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
