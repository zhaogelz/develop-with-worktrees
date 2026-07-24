"""Own a development command with a Windows Job Object.

The parent CLI exits after `dev start`, so Windows needs a long-lived owner for
the Job Object. Closing this process closes its Job Object with
KILL_ON_JOB_CLOSE, which terminates every process the development command
started. Unix starts the declared command directly in its own session.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
from typing import Any


def _windows_job() -> int | None:
    if os.name != "nt":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")

    class BasicLimit(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class ExtendedLimit(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimit),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    info = ExtendedLimit()
    job_object_extended_limit_information = 9
    job_object_limit_kill_on_job_close = 0x00002000
    info.BasicLimitInformation.LimitFlags = job_object_limit_kill_on_job_close
    if not kernel32.SetInformationJobObject(
        handle,
        job_object_extended_limit_information,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(handle)
        raise OSError(error, "SetInformationJobObject failed")
    return int(handle)


def _assign_job(job: int | None, pid: int) -> None:
    if job is None:
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    process_all_access = 0x1F0FFF
    process = kernel32.OpenProcess(process_all_access, False, pid)
    if not process:
        raise OSError(ctypes.get_last_error(), "OpenProcess failed")
    try:
        if not kernel32.AssignProcessToJobObject(job, process):
            raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
    finally:
        kernel32.CloseHandle(process)


def main() -> int:
    try:
        payload: dict[str, Any] = json.load(sys.stdin)
        argv = payload["argv"]
        cwd = payload["cwd"]
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(item, str) and item for item in argv)
        ):
            raise ValueError("argv must be a non-empty string array")
        if not isinstance(cwd, str):
            raise TypeError("cwd must be a string")
        job = _windows_job()
        child = subprocess.Popen(
            argv,
            cwd=cwd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
        _assign_job(job, child.pid)
        return child.wait()
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"develop-with-worktrees supervisor failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
