from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from types import TracebackType
from typing import Any, Self

import psutil


class SoloAIError(RuntimeError):
    """A user-actionable workflow error."""


class ActionableSoloAIError(SoloAIError):
    """保留旧错误文本，同时为可判定的下一步提供机器可读事实。"""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        context: dict[str, Any] | None = None,
        next_action: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.context = context or {}
        self.next_action = next_action or {}


def git_metadata_access_error(
    exc: BaseException,
    *,
    repository: Path,
    operation: str,
) -> ActionableSoloAIError | None:
    """将受限身份访问 Git 元数据的失败转为不降低保护的下一步。"""

    if isinstance(exc, ActionableSoloAIError):
        return None

    detail = str(exc)
    normalized = detail.casefold()
    ownership_rejected = "detected dubious ownership in repository" in normalized
    metadata_path = ".git\\" in normalized or ".git/" in normalized
    permission_rejected = isinstance(exc, PermissionError) or (
        isinstance(exc, OSError) and exc.errno in {errno.EACCES, errno.EPERM}
    )
    if not ownership_rejected and not (permission_rejected and metadata_path):
        return None

    reason = "git_ownership_check" if ownership_rejected else "metadata_permission"
    return ActionableSoloAIError(
        "Git metadata access was blocked. In a Codex workspace sandbox, rerun the "
        "same DWW lifecycle command through host-reviewed escalation (auto_review). "
        "Keep Git ownership checks and the sandbox boundary intact: do not add a "
        "global safe.directory exception or change filesystem ACLs to bypass this "
        "failure.",
        code="GIT_METADATA_ACCESS_REQUIRES_HOST_APPROVAL",
        context={
            "operation": operation,
            "repository": str(repository),
            "reason": reason,
        },
        next_action={
            "kind": "rerun_same_dww_command_with_host_approval",
            "operation": operation,
        },
    )


@dataclass(frozen=True)
class CommandResult:
    args: Sequence[str] | str
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class LoggedRunResult:
    """一次受控命令运行的可恢复摘要。"""

    returncode: int
    duration_seconds: float
    timed_out: bool
    process: dict[str, Any]


def run(
    args: Sequence[str],
    *,
    cwd: Path,
    check: bool = True,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
) -> CommandResult:
    completed = subprocess.run(
        args,
        cwd=str(cwd),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        env=env,
        timeout=timeout,
    )
    result = CommandResult(
        args, completed.returncode, completed.stdout, completed.stderr
    )
    if check and completed.returncode != 0:
        display = " ".join(redact_text(item) for item in args)
        detail = redact_text(completed.stderr.strip() or completed.stdout.strip())
        raise SoloAIError(
            f"Command failed ({completed.returncode}): {display}\n{detail}"
        )
    return result


def _stop_process_tree(pid: int, *, force: bool) -> bool:
    """终止已由当前调用持有的进程树，POSIX 场景中 PID 是独立进程组组长。"""
    try:
        if os.name != "nt":
            os.killpg(pid, signal.SIGKILL if force else signal.SIGTERM)
            return True
        root = psutil.Process(pid)
        processes = [*root.children(recursive=True), root]
        for process in reversed(processes):
            try:
                (process.kill if force else process.terminate)()
            except psutil.Error:
                continue
        if not force:
            _, alive = psutil.wait_procs(processes, timeout=5)
            for process in alive:
                try:
                    process.kill()
                except psutil.Error:
                    continue
        _, alive = psutil.wait_procs(processes, timeout=5)
        return not alive
    except (OSError, psutil.Error):
        return False


def _stream_reader(stream: Any, output: Queue[str | None]) -> None:
    try:
        for line in iter(stream.readline, ""):
            output.put(line)
    finally:
        output.put(None)


def run_logged(
    command: Sequence[str],
    *,
    cwd: Path,
    log_path: Path,
    timeout_seconds: float | None = None,
    heartbeat_seconds: float = 30.0,
    termination_grace_seconds: float = 5.0,
    environment: dict[str, str] | None = None,
    on_start: Callable[[dict[str, Any]], None] | None = None,
    on_heartbeat: Callable[[dict[str, Any]], None] | None = None,
    receipt_path: Path | None = None,
    receipt_metadata: dict[str, Any] | None = None,
    monotonic: Callable[[], float] | None = None,
    poll_interval_seconds: float = 0.2,
) -> LoggedRunResult:
    """运行显式 argv，并留下可恢复的日志和运行回执。

    读取输出使用独立线程，主线程始终检查超时和心跳；不会因命令沉默而永久阻塞。
    """
    if timeout_seconds is not None and timeout_seconds <= 0:
        raise SoloAIError("Command timeout_seconds must be positive")
    if heartbeat_seconds <= 0:
        raise SoloAIError("Command heartbeat_seconds must be positive")
    if termination_grace_seconds <= 0:
        raise SoloAIError("Command termination_grace_seconds must be positive")
    if poll_interval_seconds < 0:
        raise SoloAIError("Command poll_interval_seconds must not be negative")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    clock = monotonic or time.monotonic
    started = clock()
    started_at = utc_timestamp()
    creation_flags = (
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    )
    with log_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("$ " + " ".join(redact_text(item) for item in command) + "\n")
        handle.flush()
        process = subprocess.Popen(
            list(command),
            cwd=str(cwd),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=environment,
            start_new_session=os.name != "nt",
            creationflags=creation_flags,
        )
        reader: threading.Thread | None = None
        receipt: dict[str, Any] | None = None
        try:
            assert process.stdout is not None
            snapshot = process_snapshot(process.pid)
            receipt = {
                "schema_version": 1,
                "command": [redact_text(item) for item in command],
                "cwd": str(cwd),
                "status": "running",
                "started_at": started_at,
                "process": snapshot,
                "timeout_seconds": timeout_seconds,
            }
            if receipt_metadata:
                receipt["metadata"] = receipt_metadata
            if receipt_path and receipt is not None:
                atomic_write_json(receipt_path, receipt)
            if on_start:
                on_start({"status": "running", "process": snapshot})
            output: Queue[str | None] = Queue()
            reader = threading.Thread(
                target=_stream_reader, args=(process.stdout, output), daemon=True
            )
            reader.start()
            reader_finished = False
            timed_out = False
            force_deadline: float | None = None
            next_heartbeat = started + heartbeat_seconds
            while not reader_finished or process.poll() is None:
                now = clock()
                if timeout_seconds is not None and now - started >= timeout_seconds:
                    timed_out = True
                    # 当前 Popen 是本调用刚创建且仍持有的对象；不能再依赖
                    # 用于跨调用恢复的快照比对，否则 macOS 上的进程元数据差异会
                    # 让超时命令自然跑完。
                    if process.poll() is None:
                        _stop_process_tree(process.pid, force=False)
                    receipt["status"] = "terminating"
                    receipt["timeout_requested_at"] = utc_timestamp()
                    if receipt_path:
                        atomic_write_json(receipt_path, receipt)
                    handle.write(
                        "\n[timeout: owned process tree termination requested]\n"
                    )
                    handle.flush()
                    timeout_seconds = None
                    force_deadline = now + termination_grace_seconds
                if force_deadline is not None and now >= force_deadline:
                    if process.poll() is None:
                        _stop_process_tree(process.pid, force=True)
                    handle.write(
                        "[timeout: owned process tree force termination requested]\n"
                    )
                    handle.flush()
                    # 某些子进程会延迟关闭输出管道；根进程退出前持续复核，避免
                    # 忽略 SIGTERM 的进程永久卡住 Ready/verify。
                    force_deadline = now + 1.0 if process.poll() is None else None
                if now >= next_heartbeat:
                    heartbeat = {
                        "status": "running",
                        "elapsed_seconds": round(now - started, 3),
                        "process": snapshot,
                    }
                    receipt["last_heartbeat_at"] = utc_timestamp()
                    receipt["elapsed_seconds"] = heartbeat["elapsed_seconds"]
                    if receipt_path:
                        atomic_write_json(receipt_path, receipt)
                    handle.write(
                        f"[heartbeat elapsed={heartbeat['elapsed_seconds']:.3f}s]\n"
                    )
                    handle.flush()
                    if on_heartbeat:
                        on_heartbeat(heartbeat)
                    next_heartbeat = now + heartbeat_seconds
                try:
                    line = output.get(timeout=poll_interval_seconds)
                except Empty:
                    continue
                if line is None:
                    reader_finished = True
                else:
                    handle.write(redact_text(line))
                    handle.flush()
            returncode = process.wait()
            duration = clock() - started
            handle.write(
                f"\n[exit={returncode} duration={duration:.3f}s timed_out={str(timed_out).lower()}]\n"
            )
        except BaseException as error:
            # 观察失败不等于命令结束；先收束本次持有的 Popen，再把原错误交给上层。
            try:
                if process.poll() is None:
                    stopped = _stop_process_tree(process.pid, force=False)
                    try:
                        process.wait(timeout=termination_grace_seconds)
                    except subprocess.TimeoutExpired:
                        # 温和终止只是请求；仍活着的自建进程必须经过强制收束。
                        stopped = _stop_process_tree(process.pid, force=True)
                        process.wait(timeout=termination_grace_seconds)
                    if not stopped:
                        error.add_note(
                            "Owned command process-tree termination was not confirmed"
                        )
                if reader is not None:
                    reader.join(timeout=termination_grace_seconds)
                    if reader.is_alive():
                        error.add_note(
                            "Owned command output reader did not finish during cleanup"
                        )
            except BaseException as cleanup_error:
                error.add_note(
                    "Owned command cleanup failed: " + redact_text(str(cleanup_error))
                )
            if receipt_path and receipt is not None:
                receipt.update(
                    {
                        "status": "interrupted",
                        "finished_at": utc_timestamp(),
                        "duration_seconds": round(clock() - started, 3),
                        "interrupted": True,
                        "log": str(log_path),
                    }
                )
                atomic_write_json(receipt_path, receipt)
            raise
        finally:
            if process.poll() is not None and reader is not None:
                reader.join(timeout=0.2)
            if process.poll() is not None and (reader is None or not reader.is_alive()):
                if process.stdout is not None:
                    process.stdout.close()
    result = LoggedRunResult(returncode, duration, timed_out, snapshot)
    if receipt_path and receipt is not None:
        receipt.update(
            {
                "status": "timed_out" if timed_out else "finished",
                "finished_at": utc_timestamp(),
                "exit_code": returncode,
                "duration_seconds": round(duration, 3),
                "timed_out": timed_out,
                "log": str(log_path),
            }
        )
        atomic_write_json(receipt_path, receipt)
    return result


_REDACTIONS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"(?i)\b(password|passwd|token|secret|api[_-]?key)\b\s*[:=]\s*([^\s]+)"),
)


def redact_text(value: str) -> str:
    redacted = value
    for pattern in _REDACTIONS:
        if pattern.groups >= 2:
            redacted = pattern.sub(
                lambda match: f"{match.group(1)}=[REDACTED]", redacted
            )
        else:
            redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_id(prefix: str) -> str:
    return f"{prefix}-{time.strftime('%Y%m%d%H%M%S', time.gmtime())}-{uuid.uuid4().hex[:8]}"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with filesystem_path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def atomic_write_text(path: Path, value: str) -> None:
    access_path = filesystem_path(path)
    access_path.parent.mkdir(parents=True, exist_ok=True)
    # 不把长目标文件名再次拼进临时文件，避免 Windows 深层工作树超过路径限制。
    temporary = access_path.parent / f".w-{uuid.uuid4().hex[:16]}"
    try:
        temporary.write_text(value, encoding="utf-8", newline="\n")
        deadline = time.monotonic() + 1.0
        delay = 0.01
        while True:
            try:
                os.replace(temporary, access_path)
                return
            except OSError as error:
                # 只重试 Windows 暂时禁止替换的共享/锁定错误；不改权限、不降级为覆盖写。
                if sys.platform != "win32" or getattr(error, "winerror", None) not in {
                    5,
                    32,
                    33,
                }:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(delay, remaining))
                delay = min(delay * 2, 0.1)
    except BaseException as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError as cleanup_error:
            error.add_note(
                "Temporary atomic-write cleanup failed: " + str(cleanup_error)
            )
        raise


def atomic_copy_file(source: Path, destination: Path) -> None:
    """分块复制一个文件，再以原子替换发布完整副本。"""

    source_path = filesystem_path(source)
    destination_path = filesystem_path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination_path.parent / f".w-{uuid.uuid4().hex[:16]}"
    try:
        with source_path.open("rb") as source_handle, temporary.open("wb") as target:
            shutil.copyfileobj(source_handle, target, length=1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
        deadline = time.monotonic() + 1.0
        delay = 0.01
        while True:
            try:
                os.replace(temporary, destination_path)
                return
            except OSError as error:
                if sys.platform != "win32" or getattr(error, "winerror", None) not in {
                    5,
                    32,
                    33,
                }:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(delay, remaining))
                delay = min(delay * 2, 0.1)
    except BaseException as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError as cleanup_error:
            error.add_note(
                "Temporary atomic-copy cleanup failed: " + str(cleanup_error)
            )
        raise


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(
        path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SoloAIError(f"Local state is unreadable: {path}: {exc}") from exc


def safe_slug(value: str, *, maximum: int = 40) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "task"
    suffix = sha256_text(value)[:6]
    if len(normalized) > maximum:
        normalized = normalized[: maximum - 7].rstrip("-") + "-" + suffix
    return normalized


def ensure_within(path: Path, parent: Path) -> Path:
    resolved = path.resolve()
    root = parent.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise SoloAIError(f"Refusing path outside managed root: {resolved}") from exc
    return resolved


def filesystem_path(path: Path) -> Path:
    """只在文件系统访问边界使用扩展路径，不改变逻辑身份或解析链接。"""
    if os.name != "nt":
        return path
    absolute = os.path.abspath(path)
    if absolute.startswith("\\\\?\\"):
        return Path(absolute)
    if absolute.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + absolute[2:])
    return Path("\\\\?\\" + absolute)


def is_link_or_junction(path: Path) -> bool:
    """不跟随链接或 Windows junction；清理时宁可保留也不能跨边界。"""
    try:
        status = filesystem_path(path).lstat()
    except OSError:
        return False
    if stat.S_ISLNK(status.st_mode):
        return True
    return bool(getattr(status, "st_file_attributes", 0) & 0x0400)


def _unlink_windows_readonly_file(path: Path) -> None:
    """以对象句柄删除只读普通文件，不改属性或 ACL，也不跟随 reparse。"""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateFileW
    create.restype = wintypes.HANDLE
    handle = create(str(path), 0x00010080, 0x1, None, 3, 0x00200000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        basic = _windows_basic_information(wintypes.HANDLE(handle))
        attributes = basic["attributes"]
        if attributes & (0x0010 | 0x0400) or not attributes & 0x0001:
            raise PermissionError(
                f"Refusing non-readonly or linked cleanup file: {path}"
            )
        # FileDispositionInfoEx：只删除已打开的对象，忽略只读位但保留属性。
        flags = wintypes.DWORD(0x00000001 | 0x00000010)
        if not kernel32.SetFileInformationByHandle(
            wintypes.HANDLE(handle), 21, ctypes.byref(flags), ctypes.sizeof(flags)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(handle))


def remove_tree_without_following_links(path: Path) -> None:
    """删除一个已获准的根；目录链接只删除对象本身，绝不进入其目标。"""
    access_path = filesystem_path(path)
    if is_link_or_junction(path):
        if os.name == "nt" and access_path.is_dir():
            access_path.rmdir()
        else:
            access_path.unlink()
        return
    try:
        status = access_path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(status.st_mode):
        try:
            access_path.unlink()
        except PermissionError:
            if (
                os.name != "nt"
                or not stat.S_ISREG(status.st_mode)
                or not getattr(status, "st_file_attributes", 0) & 0x0001
            ):
                raise
            _unlink_windows_readonly_file(access_path)
        return
    with os.scandir(access_path) as entries:
        children = [path / entry.name for entry in entries]
    for child in children:
        remove_tree_without_following_links(child)
    access_path.rmdir()


def process_snapshot(pid: int | None = None) -> dict[str, Any]:
    actual_pid = pid or os.getpid()
    try:
        process = psutil.Process(actual_pid)
        return {
            "pid": actual_pid,
            "create_time": process.create_time(),
            "exe": process.exe(),
            "cwd": process.cwd(),
            # Command arguments can contain a credential. Persist a stable digest,
            # not the raw command line, while still detecting PID reuse.
            "cmdline_sha256": sha256_text(stable_json(process.cmdline())),
        }
    except (psutil.Error, OSError):
        return {
            "pid": actual_pid,
            "create_time": None,
            "exe": None,
            "cwd": None,
            "cmdline_sha256": None,
        }


def process_matches(snapshot: dict[str, Any]) -> bool:
    pid = snapshot.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        current = process_snapshot(pid)
    except (psutil.Error, OSError, ValueError):
        return False
    expected_time = snapshot.get("create_time")
    current_time = current.get("create_time")
    if (
        expected_time is None
        or current_time is None
        or abs(float(expected_time) - float(current_time)) > 0.01
    ):
        return False
    for key in ("exe", "cwd"):
        expected = snapshot.get(key)
        if expected and os.path.normcase(str(expected)) != os.path.normcase(
            str(current.get(key))
        ):
            return False
    expected_cmd = snapshot.get("cmdline_sha256")
    return not expected_cmd or expected_cmd == current.get("cmdline_sha256")


class DirectoryLock:
    def __init__(self, path: Path, *, wait: bool = False, report_every: float = 30.0):
        self.path = path
        self.wait = wait
        self.report_every = report_every
        self.acquired = False

    def _remove_stale(self) -> bool:
        access_path = filesystem_path(self.path)
        owner_path = access_path / "owner.json"
        try:
            owner = read_json(owner_path, {}) if owner_path.exists() else {}
        except SoloAIError as error:
            # Windows 可能在锁目录刚被另一线程删除时短暂拒绝读取。
            # 此时保守地视为锁仍有效；若目录已消失则直接重试抢锁。
            cause = error.__cause__
            if isinstance(cause, OSError) and cause.errno in {
                errno.EACCES,
                errno.ENOENT,
            }:
                return not access_path.exists()
            raise
        if owner and process_matches(owner):
            return False
        try:
            shutil.rmtree(access_path)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            return False

    def __enter__(self) -> Self:
        # 保留公开的逻辑路径；仅底层访问使用扩展路径，覆盖锁内的临时文件。
        access_path = filesystem_path(self.path)
        access_path.parent.mkdir(parents=True, exist_ok=True)
        last_report = time.monotonic()
        transient_access_deadline = time.monotonic() + 2.0
        while True:
            # 临时锁名不能再次包含目标锁名；深层 Windows 工作树很容易因此越过
            # 传统 MAX_PATH，而目标锁本身仍在可用范围内。
            prepared = access_path.parent / f".dww-p-{uuid.uuid4().hex[:16]}"
            try:
                prepared.mkdir()
                atomic_write_json(prepared / "owner.json", process_snapshot())
                prepared.rename(access_path)
                self.acquired = True
                return self
            except OSError as error:
                if error.errno == errno.EACCES and not access_path.exists():
                    # Windows 防病毒或索引器可能在准备目录刚写完后短暂占用它；
                    # 目标锁尚不存在时，这不是另一位所有者，也不能直接失败。
                    shutil.rmtree(prepared, ignore_errors=True)
                    if time.monotonic() >= transient_access_deadline:
                        raise
                    time.sleep(0.05)
                    continue
                if error.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                    shutil.rmtree(prepared, ignore_errors=True)
                    raise
                shutil.rmtree(prepared, ignore_errors=True)
                if self._remove_stale():
                    continue
                if not self.wait:
                    raise SoloAIError(f"Operation is already active: {self.path.name}")
                now = time.monotonic()
                if now - last_report >= self.report_every:
                    print(
                        f"Waiting for {self.path.name}...", file=sys.stderr, flush=True
                    )
                    last_report = now
                time.sleep(0.5)
            except Exception:
                shutil.rmtree(prepared, ignore_errors=True)
                raise

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self.acquired:
            access_path = filesystem_path(self.path)
            releasing: Path | None = (
                access_path.parent / f".dww-r-{uuid.uuid4().hex[:16]}"
            )
            deadline = time.monotonic() + 2.0
            while True:
                try:
                    access_path.rename(releasing)
                    break
                except FileNotFoundError:
                    releasing = None
                    break
                except OSError:
                    # Windows 可能仍有并发读取者短暂占用 owner.json。
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)
            self.acquired = False
            if releasing is not None:
                deadline = time.monotonic() + 2.0
                while True:
                    try:
                        shutil.rmtree(releasing)
                        break
                    except FileNotFoundError:
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.05)


def directory_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def path_identity(path: Path) -> dict[str, Any]:
    """冻结目录项对象身份；同路径替换后 inode/file-index 必须变化。"""
    access_path = filesystem_path(path)
    details = access_path.stat(follow_symlinks=False)
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        create_file = ctypes.windll.kernel32.CreateFileW
        create_file.restype = wintypes.HANDLE
        invalid = wintypes.HANDLE(-1).value
        handle = create_file(
            str(access_path),
            0x0080,
            0x00000001 | 0x00000002 | 0x00000004,
            None,
            3,
            0x00200000
            | (0x02000000 if getattr(details, "st_file_attributes", 0) & 0x0010 else 0),
            None,
        )
        if handle == invalid:
            raise SoloAIError(f"Cannot read managed path identity: {path}")
        try:
            return _windows_handle_identity(handle, mode=int(details.st_mode))
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    return {
        "device": int(details.st_dev),
        "inode": int(details.st_ino),
        "mode": int(details.st_mode),
    }


def _windows_handle_identity(
    handle: int, *, mode: int, file_metadata: bool = False
) -> dict[str, Any]:
    import ctypes
    from ctypes import wintypes

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    information = ByHandleFileInformation()
    if not ctypes.windll.kernel32.GetFileInformationByHandle(
        handle, ctypes.byref(information)
    ):
        raise SoloAIError("Cannot read Windows file identity")
    identity = {
        "device": int(information.dwVolumeSerialNumber),
        "inode": (int(information.nFileIndexHigh) << 32)
        | int(information.nFileIndexLow),
        "mode": mode,
    }
    if not file_metadata:
        return identity
    basic = _windows_basic_information(handle)
    if basic["attributes"] & (0x0010 | 0x0400):
        raise SoloAIError("Recreatable cleanup target is not a plain file")
    return {
        **identity,
        "kind": "recreatable-file",
        "size": (int(information.nFileSizeHigh) << 32) | int(information.nFileSizeLow),
        "modified_ticks": basic["modified_ticks"],
        "changed_ticks": basic["changed_ticks"],
    }


def _windows_basic_information(handle: int) -> dict[str, int]:
    import ctypes
    from ctypes import wintypes

    class FileBasicInfo(ctypes.Structure):
        _fields_ = [
            ("CreationTime", ctypes.c_longlong),
            ("LastAccessTime", ctypes.c_longlong),
            ("LastWriteTime", ctypes.c_longlong),
            ("ChangeTime", ctypes.c_longlong),
            ("FileAttributes", wintypes.DWORD),
        ]

    basic = FileBasicInfo()
    if not ctypes.windll.kernel32.GetFileInformationByHandleEx(
        handle, 0, ctypes.byref(basic), ctypes.sizeof(basic)
    ):
        raise SoloAIError("Cannot read Windows cleanup change metadata")
    return {
        "modified_ticks": int(basic.LastWriteTime),
        "changed_ticks": int(basic.ChangeTime),
        "attributes": int(basic.FileAttributes),
    }


def snapshot_recreatable_file(path: Path) -> dict[str, Any]:
    """Windows及共享硬链接用内容证明，其他单链接依赖保留元数据快路径。"""
    details = filesystem_path(path).stat(follow_symlinks=False)
    if not stat.S_ISREG(details.st_mode) or is_link_or_junction(path):
        raise SoloAIError(f"Refusing non-plain recreatable file: {path}")
    if os.name == "nt" or details.st_nlink > 1:
        # Windows时间戳可能碰撞；删除其他硬链接也会改变ctime而不改变内容。
        return snapshot_plain_path(path)
    return {
        "device": int(details.st_dev),
        "inode": int(details.st_ino),
        "mode": int(details.st_mode),
        "kind": "recreatable-file",
        "size": int(details.st_size),
        "modified_ns": int(details.st_mtime_ns),
        "changed_ns": int(details.st_ctime_ns),
    }


@contextmanager
def pinned_plain_directory(path: Path, expected: dict[str, Any]) -> Iterator[None]:
    """Windows持有可列目录句柄并拒绝重命名；单独READ_ATTRIBUTES不足以阻止替换。"""
    identity = {key: expected[key] for key in ("device", "inode", "mode")}
    if os.name != "nt":
        if is_link_or_junction(path) or path_identity(path) != identity:
            raise SoloAIError(f"Cleanup directory changed before deletion: {path}")
        yield
        return
    import ctypes
    from ctypes import wintypes

    create_file = ctypes.windll.kernel32.CreateFileW
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(filesystem_path(path)), 0x0081, 0x0003, None, 3, 0x02200000, None
    )
    if handle == wintypes.HANDLE(-1).value:
        raise SoloAIError(f"Cleanup directory is busy or changed: {path}")
    try:
        basic = _windows_basic_information(handle)
        if (
            basic["attributes"] & 0x0400
            or not basic["attributes"] & 0x0010
            or _windows_handle_identity(handle, mode=int(expected["mode"])) != identity
        ):
            raise SoloAIError(f"Cleanup directory changed before deletion: {path}")
        yield
    finally:
        ctypes.windll.kernel32.CloseHandle(wintypes.HANDLE(handle))


def snapshot_plain_path(path: Path) -> dict[str, Any]:
    if is_link_or_junction(path):
        raise SoloAIError(f"Refusing to snapshot a link: {path}")
    identity = path_identity(path)
    access_path = filesystem_path(path)
    if access_path.is_file():
        return {
            **identity,
            "size": int(access_path.stat(follow_symlinks=False).st_size),
            "kind": "file",
            "sha256": sha256_file(path),
        }
    if access_path.is_dir():
        return {**identity, "kind": "directory"}
    raise SoloAIError(f"Unsupported cleanup path type: {path}")


def _open_windows_link(path: Path, *, deleting: bool = False) -> int:
    import ctypes
    from ctypes import wintypes

    create_file = ctypes.windll.kernel32.CreateFileW
    create_file.restype = wintypes.HANDLE
    # 删除期间不共享写入和重命名；始终打开 reparse 对象，不打开目标。
    handle = create_file(
        str(filesystem_path(path)),
        0x0080 | (0x00010000 if deleting else 0),
        0x00000001 if deleting else 0x00000001 | 0x00000002 | 0x00000004,
        None,
        3,
        0x00200000 | 0x02000000,
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        raise SoloAIError(f"Dependency link is busy or changed: {path}")
    return handle


def _windows_link_snapshot(handle: int, *, mode: int) -> dict[str, Any]:
    import ctypes
    from ctypes import wintypes

    raw = ctypes.create_string_buffer(16384)
    length = wintypes.DWORD()
    if not ctypes.windll.kernel32.DeviceIoControl(
        wintypes.HANDLE(handle),
        0x000900A8,
        None,
        0,
        raw,
        len(raw),
        ctypes.byref(length),
        None,
    ):
        raise SoloAIError("Cannot inspect dependency reparse object")
    data = raw.raw[: length.value]
    if len(data) < 8 or int.from_bytes(data[:4], "little") not in {
        0xA0000003,  # junction
        0xA000000C,  # symbolic link
    }:
        raise SoloAIError("Unknown dependency reparse type; content was preserved")
    return {
        **_windows_handle_identity(wintypes.HANDLE(handle), mode=mode),
        "kind": "link",
        "reparse_sha256": hashlib.sha256(data).hexdigest(),
    }


def snapshot_link_path(path: Path) -> dict[str, Any]:
    """冻结链接对象及其指向文本，不打开或读取目标。"""
    details = filesystem_path(path).lstat()
    if os.name != "nt":
        if not stat.S_ISLNK(details.st_mode):
            raise SoloAIError(f"Expected a dependency link: {path}")
        return {
            "device": int(details.st_dev),
            "inode": int(details.st_ino),
            "mode": int(details.st_mode),
            "kind": "link",
            "target": os.readlink(path),
        }
    import ctypes
    from ctypes import wintypes

    handle = _open_windows_link(path)
    try:
        return _windows_link_snapshot(handle, mode=int(details.st_mode))
    finally:
        ctypes.windll.kernel32.CloseHandle(wintypes.HANDLE(handle))


def delete_link_path_if_unchanged(path: Path, expected: dict[str, Any]) -> None:
    """只删除已核准的链接对象；Windows 使用同一对象句柄条件删除。"""
    if expected.get("kind") != "link":
        raise SoloAIError("Dependency link deletion requires a link snapshot")
    if os.name != "nt":
        if snapshot_link_path(path) != expected:
            raise SoloAIError(f"Dependency link changed before deletion: {path}")
        path.unlink()
        return
    import ctypes
    from ctypes import wintypes

    handle = _open_windows_link(path, deleting=True)
    try:
        if _windows_link_snapshot(handle, mode=int(expected["mode"])) != expected:
            raise SoloAIError(f"Dependency link changed before deletion: {path}")
        _mark_windows_handle_for_deletion(wintypes.HANDLE(handle))
    finally:
        ctypes.windll.kernel32.CloseHandle(wintypes.HANDLE(handle))


def _mark_windows_handle_for_deletion(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    class FileDispositionInfo(ctypes.Structure):
        _fields_ = [("DeleteFile", wintypes.BOOL)]

    disposition = FileDispositionInfo(True)
    if not ctypes.windll.kernel32.SetFileInformationByHandle(
        handle, 4, ctypes.byref(disposition), ctypes.sizeof(disposition)
    ):
        raise SoloAIError("Cleanup object could not be conditionally deleted")


def delete_plain_path_if_unchanged(path: Path, expected: dict[str, Any]) -> None:
    """在 Windows 用已打开对象句柄条件删除，避免路径校验后的替换竞态。"""
    if os.name != "nt":
        snapshot = (
            snapshot_recreatable_file
            if expected.get("kind") == "recreatable-file"
            else snapshot_plain_path
        )
        if snapshot(path) != expected:
            raise SoloAIError(f"Cleanup content changed before deletion: {path}")
        if path.is_dir():
            path.rmdir()
        else:
            path.unlink()
        return

    import ctypes
    import msvcrt
    from ctypes import wintypes

    if expected.get("kind") == "recreatable-file":
        raise SoloAIError(f"Windows cleanup requires content proof: {path}")

    create_file = ctypes.windll.kernel32.CreateFileW
    create_file.restype = wintypes.HANDLE
    invalid = wintypes.HANDLE(-1).value
    generic_read = 0x80000000
    delete_access = 0x00010000
    share_read = 0x00000001
    open_existing = 3
    backup_semantics = 0x02000000
    open_reparse = 0x00200000
    flags = open_reparse | (
        backup_semantics if expected.get("kind") == "directory" else 0
    )
    handle = create_file(
        str(filesystem_path(path)),
        generic_read | delete_access,
        share_read,
        None,
        open_existing,
        flags,
        None,
    )
    if handle == invalid:
        raise SoloAIError(f"Cleanup path is busy or changed: {path}")
    fd: int | None = None
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY)
        handle = None
        observed_stat = os.fstat(fd)
        expected_mode = int(expected.get("mode", 0))
        metadata_only = expected.get("kind") == "recreatable-file"
        if not metadata_only and (
            _windows_basic_information(msvcrt.get_osfhandle(fd))["attributes"] & 0x0400
        ):
            raise SoloAIError(f"Cleanup content became a reparse point: {path}")
        # Windows 的 Path.stat 与 CRT fstat 会为同一文件给出不同的权限位；
        # 文件类型仍须一致，而冻结的 mode 用于保持后续结构等值比较。
        if stat.S_IFMT(observed_stat.st_mode) != stat.S_IFMT(expected_mode):
            raise SoloAIError(f"Cleanup content changed before deletion: {path}")
        observed = {
            **_windows_handle_identity(
                msvcrt.get_osfhandle(fd),
                mode=expected_mode,
                file_metadata=metadata_only,
            ),
            "kind": expected.get("kind"),
        }
        if expected.get("kind") == "file":
            observed["size"] = int(observed_stat.st_size)
            with os.fdopen(fd, "rb", closefd=False) as stream:
                digest = hashlib.sha256()
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
                observed["sha256"] = digest.hexdigest()
        if observed != expected:
            raise SoloAIError(f"Cleanup content changed before deletion: {path}")

        raw_handle = msvcrt.get_osfhandle(fd)
        _mark_windows_handle_for_deletion(wintypes.HANDLE(raw_handle))
    finally:
        if fd is not None:
            os.close(fd)
        elif handle not in {None, invalid}:
            ctypes.windll.kernel32.CloseHandle(handle)
