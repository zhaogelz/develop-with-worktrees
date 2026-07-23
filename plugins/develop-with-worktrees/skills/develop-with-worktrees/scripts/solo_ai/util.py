from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Callable, Sequence

import psutil


class SoloAIError(RuntimeError):
    """A user-actionable workflow error."""


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


def _stop_owned_process_tree(snapshot: dict[str, Any]) -> bool:
    """只终止身份仍匹配的根进程及其后代，避免误杀 PID 复用的进程。"""
    if not process_matches(snapshot):
        return False
    pid = snapshot["pid"]
    try:
        if os.name != "nt":
            os.killpg(pid, signal.SIGTERM)
            return True
        root = psutil.Process(pid)
        children = root.children(recursive=True)
        for child in reversed(children):
            try:
                child.terminate()
            except psutil.Error:
                continue
        root.terminate()
        _, alive = psutil.wait_procs([*children, root], timeout=5)
        for process in alive:
            try:
                process.kill()
            except psutil.Error:
                continue
        return True
    except (OSError, psutil.Error):
        return False


def _force_stop_owned_process_tree(snapshot: dict[str, Any]) -> bool:
    """宽限期后仅强制终止仍能证明为同一进程组的验证进程。"""
    if not process_matches(snapshot):
        return False
    pid = snapshot["pid"]
    try:
        if os.name != "nt":
            os.killpg(pid, signal.SIGKILL)
            return True
        root = psutil.Process(pid)
        processes = [*root.children(recursive=True), root]
        for process in reversed(processes):
            try:
                process.kill()
            except psutil.Error:
                continue
        return True
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
    on_heartbeat: Callable[[dict[str, Any]], None] | None = None,
    receipt_path: Path | None = None,
    receipt_metadata: dict[str, Any] | None = None,
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
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
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
        assert process.stdout is not None
        snapshot = process_snapshot(process.pid)
        receipt: dict[str, Any] = {
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
        if receipt_path:
            atomic_write_json(receipt_path, receipt)
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
            now = time.monotonic()
            if timeout_seconds is not None and now - started >= timeout_seconds:
                timed_out = True
                _stop_owned_process_tree(snapshot)
                receipt["status"] = "terminating"
                receipt["timeout_requested_at"] = utc_timestamp()
                if receipt_path:
                    atomic_write_json(receipt_path, receipt)
                handle.write("\n[timeout: owned process tree termination requested]\n")
                handle.flush()
                timeout_seconds = None
                force_deadline = now + termination_grace_seconds
            if force_deadline is not None and now >= force_deadline:
                _force_stop_owned_process_tree(snapshot)
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
                line = output.get(timeout=0.2)
            except Empty:
                continue
            if line is None:
                reader_finished = True
            else:
                handle.write(redact_text(line))
                handle.flush()
        returncode = process.wait()
        duration = time.monotonic() - started
        handle.write(
            f"\n[exit={returncode} duration={duration:.3f}s timed_out={str(timed_out).lower()}]\n"
        )
    result = LoggedRunResult(returncode, duration, timed_out, snapshot)
    if receipt_path:
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
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # 不把长目标文件名再次拼进临时文件，避免 Windows 深层工作树超过路径限制。
    temporary = path.parent / f".{uuid.uuid4().hex}.tmp"
    temporary.write_text(value, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


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


def is_link_or_junction(path: Path) -> bool:
    """不跟随链接或 Windows junction；清理时宁可保留也不能跨边界。"""
    try:
        status = path.lstat()
    except OSError:
        return False
    if path.is_symlink():
        return True
    return bool(getattr(status, "st_file_attributes", 0) & 0x0400)


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
        owner_path = self.path / "owner.json"
        owner = read_json(owner_path, {}) if owner_path.exists() else {}
        if owner and process_matches(owner):
            return False
        try:
            shutil.rmtree(self.path)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            return False

    def __enter__(self) -> "DirectoryLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        last_report = time.monotonic()
        while True:
            prepared = self.path.with_name(
                f".{self.path.name}.{uuid.uuid4().hex}.pending"
            )
            try:
                prepared.mkdir()
                atomic_write_json(prepared / "owner.json", process_snapshot())
                prepared.rename(self.path)
                self.acquired = True
                return self
            except OSError as error:
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

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.acquired:
            try:
                shutil.rmtree(self.path)
            except FileNotFoundError:
                pass
            self.acquired = False


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
