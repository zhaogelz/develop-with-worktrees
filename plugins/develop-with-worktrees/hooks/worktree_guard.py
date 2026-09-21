# /// script
# requires-python = ">=3.11"
# ///

"""Codex PreToolUse guard for develop-with-worktrees.

The optional hook is deliberately stdlib-only. It provides a hard PreToolUse
denial for local Codex tool paths that invoke hooks; lifecycle correctness does
not depend on it and it is not an operating-system sandbox. State is held under
the Git common directory, never in user files.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from datetime import date
from pathlib import Path, PureWindowsPath
from typing import Any

SKILL_SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "develop-with-worktrees"
    / "scripts"
)
if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS))

from solo_ai.command_contract import TOP_LEVEL_COMMANDS
from solo_ai.delegated import inspect_delegated
from solo_ai.routing import decide_route, detect_existing_workflows

FINAL_TASK_STATES = {"finished", "abandoned", "candidate-published"}
READ_ONLY_GIT_SUBCOMMANDS = {"status", "diff", "log", "show", "branch", "rev-parse"}
DWW_SUBCOMMANDS = TOP_LEVEL_COMMANDS
DWW_QUARANTINE_SUBCOMMANDS = {"doctor", "status", "plan", "resume-in-place"}
DWW_READ_ONLY_SUBCOMMANDS = {
    "version",
    "doctor",
    "route",
    "status",
    "plan",
    "help",
}
PATCH_SCOPE_EXTERNAL = "external"
PATCH_SCOPE_PROTECTED = "protected"
PATCH_SCOPE_SESSION_ARTIFACT = "session-artifact"
PATCH_SCOPE_INVALID = "invalid"
PATCH_SCOPE_OTHER_SESSION = "other-session-artifact"
PATCH_SCOPE_CODEX_HOME = "codex-home-protected"
PATCH_SCOPE_ARTIFACT_ESCAPE = "artifact-link-escape"
PATCH_SCOPE_MIXED = "mixed-targets"
PATCH_SCOPE_FOREIGN_REPOSITORY = "foreign-repository"
MAINTENANCE_PLUGIN = "develop-with-worktrees"
MAINTENANCE_MARKETPLACE = "dww-stable-local"


def _run_git(cwd: str, *args: str) -> str | None:
    completed = subprocess.run(
        ["git", "-C", cwd, *args],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def git_root(cwd: str) -> Path | None:
    value = _run_git(cwd, "rev-parse", "--show-toplevel")
    return Path(value).resolve() if value else None


def common_dir(root: Path) -> Path | None:
    value = _run_git(str(root), "rev-parse", "--git-common-dir")
    if not value:
        return None
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def state_path(root: Path) -> Path | None:
    common = common_dir(root)
    return common / "solo-ai" / "state.json" if common else None


def guard_state_path(root: Path) -> Path | None:
    common = common_dir(root)
    return common / "solo-ai" / "guard-state.json" if common else None


def read_state(root: Path) -> tuple[dict[str, Any], Path | None]:
    path = state_path(root)
    if path is None:
        return {}, None
    try:
        return json.loads(path.read_text(encoding="utf-8")), path
    except (OSError, json.JSONDecodeError):
        return {}, path


def read_guard_state(root: Path) -> tuple[dict[str, Any], Path | None]:
    path = guard_state_path(root)
    if path is None:
        return {}, None
    try:
        return json.loads(path.read_text(encoding="utf-8")), path
    except (OSError, json.JSONDecodeError):
        return {
            "schema_version": 1,
            "quarantines": {},
            "alerts": [],
            "root_context_refreshes": {},
        }, path


def _with_guard_lock(path: Path, update: Any) -> None:
    """只锁 hook 自己的 guard-state，绝不与 lifecycle state.lock 混用。"""
    lock = path.parent / "locks" / "guard-state.lock"
    deadline = time.monotonic() + 2.0
    while True:
        try:
            lock.parent.mkdir(parents=True, exist_ok=True)
            lock.mkdir()
            (lock / "owner.json").write_text(
                json.dumps({"pid": os.getpid(), "started_at": time.time()}),
                encoding="utf-8",
            )
            break
        except FileExistsError:
            try:
                owner = json.loads((lock / "owner.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                owner = {}
            started = owner.get("started_at")
            if isinstance(started, (int, float)) and time.time() - started > 60:
                shutil.rmtree(lock, ignore_errors=True)
                continue
            if time.monotonic() >= deadline:
                return
            time.sleep(0.02)
    try:
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {
                "schema_version": 1,
                "quarantines": {},
                "alerts": [],
                "root_context_refreshes": {},
            }
        update(state)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        shutil.rmtree(lock, ignore_errors=True)


def _record_alert(root: Path, *, kind: str, paths: list[str]) -> None:
    _, path = read_guard_state(root)
    if path is None:
        return

    def update(current: dict[str, Any]) -> None:
        alerts = current.setdefault("alerts", [])
        alert = {
            "kind": kind,
            "worktree": str(root),
            "paths": paths[:20],
            "observed_at": int(time.time()),
        }
        if not alerts or alerts[-1] != alert:
            alerts.append(alert)
        del alerts[:-20]

    _with_guard_lock(path, update)


def _quarantine(root: Path, task_id: str, reason: str) -> None:
    _, path = read_guard_state(root)
    if path is None:
        return

    def update(state: dict[str, Any]) -> None:
        quarantines = state.setdefault("quarantines", {})
        if isinstance(quarantines, dict):
            quarantines[task_id] = {"reason": reason, "observed_at": int(time.time())}

    _with_guard_lock(path, update)


def _mark_root_context_refresh(root: Path, task_id: str, *, reason: str) -> None:
    """只记录本次恢复需读取的轻量标记，不在 Hook 中读完整方案。"""

    _, path = read_guard_state(root)
    if path is None:
        return

    def update(state: dict[str, Any]) -> None:
        refreshes = state.setdefault("root_context_refreshes", {})
        if isinstance(refreshes, dict):
            refreshes[task_id] = {
                "reason": reason,
                "generation": uuid.uuid4().hex,
                "observed_at": int(time.time()),
            }

    _with_guard_lock(path, update)


def command_from(payload: dict[str, Any]) -> str:
    tool_input = (
        payload.get("tool_input")
        or payload.get("toolInput")
        or payload.get("input")
        or {}
    )
    if not isinstance(tool_input, dict):
        return ""
    value = tool_input.get("command")
    return value if isinstance(value, str) else ""


def patch_from(payload: dict[str, Any]) -> str:
    """读取补丁正文；Codex 当前契约使用 tool_input.command。"""

    values: list[str] = []
    for key in ("tool_input", "toolInput", "input"):
        if key not in payload:
            continue
        tool_input = payload[key]
        if isinstance(tool_input, str):
            values.append(tool_input)
            continue
        if not isinstance(tool_input, dict):
            return ""
        has_command = "command" in tool_input
        has_patch = "patch" in tool_input
        command = tool_input.get("command")
        patch = tool_input.get("patch")
        if has_command and not isinstance(command, str):
            return ""
        if has_patch and not isinstance(patch, str):
            return ""
        if has_command and has_patch and command != patch:
            return ""
        if has_command:
            values.append(command)
        elif has_patch:
            # 兼容已安装旧版本和早期测试夹具的字段形状。
            values.append(patch)
        else:
            return ""
    if not values or len(set(values)) != 1:
        return ""
    return values[0]


_PATCH_TARGET = re.compile(
    r"^\*\*\* (?:(?:Add|Update|Delete) File|Move (?:from|to)): (?P<path>.+?)\s*$"
)


def _nearest_existing_directory(path: Path) -> Path | None:
    current = path if path.is_dir() else path.parent
    while current != current.parent:
        if current.exists():
            return current if current.is_dir() else current.parent
        current = current.parent
    return current if current.exists() and current.is_dir() else None


def _path_within(path: Path, parent: Path) -> bool:
    """按路径组件而非字符串前缀判断包含关系。"""

    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _unsafe_patch_path(raw_target: str) -> bool:
    """拒绝 Windows 不能可靠归一化的补丁目标表示。"""

    if not raw_target or raw_target.startswith(("\\\\?\\", "\\\\.\\")):
        return True
    windows_path = PureWindowsPath(raw_target)
    if windows_path.drive.startswith("\\\\"):
        return True
    if windows_path.drive and not windows_path.root:
        return True
    if ".." in windows_path.parts:
        return True
    return any(
        ":" in part
        for part in windows_path.parts
        if part not in {windows_path.drive, windows_path.anchor}
    )


def _has_link_or_reparse_component(path: Path, boundary: Path) -> bool:
    """已有组件中的符号链接或 Windows reparse point 都不能承载例外。"""

    if not _path_within(path, boundary):
        return True
    current = boundary
    for part in path.relative_to(boundary).parts:
        current /= part
        if not current.exists():
            continue
        try:
            metadata = current.lstat()
        except OSError:
            return True
        if current.is_symlink() or bool(
            getattr(metadata, "st_file_attributes", 0) & 0x400
        ):
            return True
    return False


def _has_link_or_reparse_ancestor(path: Path) -> bool:
    """拒绝任何现有祖先中的符号链接或 junction。"""

    current = _nearest_existing_directory(path)
    if current is None:
        return True
    while True:
        try:
            metadata = current.lstat()
        except OSError:
            return True
        if current.is_symlink() or bool(
            getattr(metadata, "st_file_attributes", 0) & 0x400
        ):
            return True
        if current == current.parent:
            return False
        current = current.parent


def _directory_identity(path: Path) -> dict[str, int] | None:
    """取得与 DWW 状态相同的目录对象身份，不跟随链接。"""

    try:
        details = path.stat(follow_symlinks=False)
    except OSError:
        return None
    if os.name != "nt":
        return {
            "device": int(details.st_dev),
            "inode": int(details.st_ino),
            "mode": int(details.st_mode),
        }
    try:
        import ctypes
        from ctypes import wintypes

        class _ByHandleFileInformation(ctypes.Structure):
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

        create_file = ctypes.windll.kernel32.CreateFileW
        create_file.restype = wintypes.HANDLE
        invalid = wintypes.HANDLE(-1).value
        handle = create_file(
            str(path),
            0x0080,
            0x00000001 | 0x00000002 | 0x00000004,
            None,
            3,
            0x00200000
            | (0x02000000 if getattr(details, "st_file_attributes", 0) & 0x0010 else 0),
            None,
        )
        if handle == invalid:
            return None
        try:
            information = _ByHandleFileInformation()
            if not ctypes.windll.kernel32.GetFileInformationByHandle(
                handle, ctypes.byref(information)
            ):
                return None
            return {
                "device": int(information.dwVolumeSerialNumber),
                "inode": (int(information.nFileIndexHigh) << 32)
                | int(information.nFileIndexLow),
                "mode": int(details.st_mode),
            }
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except (AttributeError, OSError):
        return None


def _git_root_probe(cwd: Path) -> tuple[Path | None, bool]:
    """返回 Git 根与查询是否可靠；失败绝不伪装成非仓库。"""

    try:
        completed = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
    except OSError:
        return None, False
    if completed.returncode == 0 and completed.stdout.strip():
        try:
            return Path(completed.stdout.strip()).resolve(), True
        except OSError:
            return None, False
    if "not a git repository" in completed.stderr.lower():
        return None, True
    return None, False


def _trusted_codex_home() -> Path | None:
    """只从 Hook 进程环境确认 CODEX_HOME，不读取补丁载荷声明。"""

    configured = os.environ.get("CODEX_HOME")
    candidate = Path(configured) if configured else Path.home() / ".codex"
    try:
        if not candidate.is_absolute() or not candidate.is_dir():
            return None
        if candidate.is_symlink() or bool(
            getattr(candidate.lstat(), "st_file_attributes", 0) & 0x400
        ):
            return None
        home = candidate.resolve()
    except OSError:
        return None
    git_home, reliable = _git_root_probe(home)
    # CODEX_HOME 自己作为仓库是实际支持的部署形态；被更大仓库覆盖则保守拒绝。
    if not reliable or (git_home is not None and git_home != home):
        return None
    return home


def _configured_codex_home() -> Path | None:
    configured = os.environ.get("CODEX_HOME")
    candidate = Path(configured) if configured else Path.home() / ".codex"
    return candidate if candidate.is_absolute() else None


def _git_path_is_protected(repository: Path, target: Path) -> bool | None:
    """例外不能改写 CODEX_HOME Git 索引中的跟踪或暂存目标。"""

    try:
        relative = str(target.relative_to(repository))
        tracked = subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "ls-files",
                "--error-unmatch",
                "--",
                relative,
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        staged = subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "diff",
                "--cached",
                "--quiet",
                "--",
                relative,
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    if tracked.returncode == 0 or staged.returncode == 1:
        return True
    if tracked.returncode not in {0, 1} or staged.returncode not in {0, 1}:
        return None
    return False


def _session_artifact_scope(target: Path, session: str) -> str | None:
    """验证当前会话唯一的 visualizations 产物目录，返回拒绝类别或通过。"""

    home = _trusted_codex_home()
    if home is None:
        candidate = _configured_codex_home()
        return (
            PATCH_SCOPE_INVALID
            if candidate and _path_within(target, candidate)
            else None
        )
    if not _path_within(target, home):
        return None
    if _has_link_or_reparse_component(target, home):
        return PATCH_SCOPE_ARTIFACT_ESCAPE
    parts = target.relative_to(home).parts
    if not parts or parts[0].casefold() != "visualizations":
        return PATCH_SCOPE_CODEX_HOME
    if len(parts) < 5 or not session:
        return PATCH_SCOPE_OTHER_SESSION
    try:
        date(int(parts[1]), int(parts[2]), int(parts[3]))
    except ValueError:
        return PATCH_SCOPE_OTHER_SESSION
    if parts[4] != session or ".git" in parts:
        return PATCH_SCOPE_OTHER_SESSION
    artifact_root = home.joinpath(*parts[:5])
    nested_root, reliable = _git_root_probe(
        _nearest_existing_directory(target) or target
    )
    if not reliable:
        return PATCH_SCOPE_INVALID
    if nested_root is not None and nested_root != home:
        return PATCH_SCOPE_FOREIGN_REPOSITORY
    if nested_root == home:
        protected = _git_path_is_protected(home, target)
        if protected is None:
            return PATCH_SCOPE_INVALID
        if protected:
            return PATCH_SCOPE_CODEX_HOME
    # target 必须真实落在本次会话目录之下，而非目录本身。
    return (
        PATCH_SCOPE_SESSION_ARTIFACT
        if target != artifact_root
        else PATCH_SCOPE_OTHER_SESSION
    )


def _patch_execution_directory(payload: dict[str, Any], root: Path) -> Path | None:
    value = payload.get("cwd")
    if not isinstance(value, str) or not value:
        return None
    try:
        cwd = Path(value).resolve()
    except (OSError, ValueError):
        return None
    return cwd if cwd.is_dir() else None


def _apply_patch_targets(payload: dict[str, Any], root: Path) -> list[Path] | None:
    patch = patch_from(payload)
    execution_directory = _patch_execution_directory(payload, root)
    if (
        not patch
        or execution_directory is None
        or "*** Begin Patch" not in patch
        or "*** End Patch" not in patch
    ):
        return None
    raw_targets = [
        match.group("path").strip()
        for line in patch.splitlines()
        if (match := _PATCH_TARGET.fullmatch(line))
    ]
    if not raw_targets:
        return None
    targets: list[Path] = []
    for raw_target in raw_targets:
        if _unsafe_patch_path(raw_target):
            return None
        target = Path(raw_target)
        try:
            unresolved = (
                target if target.is_absolute() else execution_directory / target
            ).absolute()
            if _has_link_or_reparse_ancestor(unresolved):
                return None
            targets.append(unresolved.resolve())
        except OSError:
            return None
    return targets


def _apply_patch_scope(payload: dict[str, Any], root: Path) -> str | None:
    """区分补丁实际目标，避免把仓库外的方案/报告误判为基线写入。"""

    targets = _apply_patch_targets(payload, root)
    if targets is None:
        return None
    base = root.resolve()
    source_common = common_dir(root)
    if source_common is None:
        return None
    external = False
    protected = False
    artifact = False
    session = _session(payload)
    for resolved in targets:
        artifact_scope = _session_artifact_scope(resolved, session)
        if artifact_scope is not None:
            if artifact_scope != PATCH_SCOPE_SESSION_ARTIFACT:
                return artifact_scope
            artifact = True
            continue
        try:
            resolved.relative_to(base)
        except ValueError:
            parent = _nearest_existing_directory(resolved)
            # 允许由宿主权限已授权的、非仓库中的方案/报告目标；不能借此
            # 改写另一个仓库或让路径无法判断的补丁通过。同一 common-dir
            # 的其他工作树仍属于受保护目标，后续必须按任务归属核验。
            if parent is None:
                return None
            target_root = git_root(str(parent))
            if target_root is not None:
                if common_dir(target_root) != source_common:
                    return PATCH_SCOPE_FOREIGN_REPOSITORY
                protected = True
            else:
                external = True
        else:
            protected = True
    # 一个补丁只能在受保护工作树内，或只修改一个明确的仓库外文件；混合目标
    # 会让隔离任务借内部路径越过外部写入判断，因此保守拒绝。
    if sum((protected, external, artifact)) != 1:
        return PATCH_SCOPE_MIXED
    if protected:
        return PATCH_SCOPE_PROTECTED
    if artifact:
        return PATCH_SCOPE_SESSION_ARTIFACT
    return PATCH_SCOPE_EXTERNAL if external else PATCH_SCOPE_INVALID


def _patch_target_worktrees(payload: dict[str, Any], root: Path) -> list[Path] | None:
    """返回补丁涉及的同一 Git common-dir 下实际工作树。"""

    targets = _apply_patch_targets(payload, root)
    source_common = common_dir(root)
    if targets is None or source_common is None:
        return None
    worktrees: list[Path] = []
    for target in targets:
        parent = _nearest_existing_directory(target)
        if parent is None:
            return None
        target_root = git_root(str(parent))
        if target_root is None:
            continue
        if common_dir(target_root) != source_common:
            return None
        if target_root not in worktrees:
            worktrees.append(target_root)
    return worktrees


def _session(payload: dict[str, Any]) -> str:
    value = payload.get("session_id") or payload.get("sessionId")
    return value if isinstance(value, str) else ""


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _dirty_paths(root: Path) -> list[str]:
    output = _run_git(str(root), "status", "--porcelain=v1", "--untracked-files=all")
    if not output:
        return []
    return [line[3:] if len(line) > 3 else line for line in output.splitlines()]


def preference_disabled(root: Path) -> bool:
    common = common_dir(root)
    if common is None:
        return False
    path = common / "solo-ai" / "preferences.json"
    try:
        return not bool(
            json.loads(path.read_text(encoding="utf-8")).get("enabled", True)
        )
    except (OSError, json.JSONDecodeError):
        return False


def task_bypass_active(root: Path, payload: dict[str, Any]) -> bool:
    """仅放行已登记的当前会话，不能把临时选择扩大为仓库级时间窗。"""
    session = _session(payload)
    common = common_dir(root)
    if not session or common is None:
        return False
    path = common / "solo-ai" / "session-overrides.json"
    try:
        overrides = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    session_fingerprint = _fingerprint(session)
    return any(
        isinstance(grant, dict)
        and grant.get("worktree") == str(root.resolve())
        and session_fingerprint in grant.get("sessions", [])
        for grant in overrides.get("grants", [])
    )


def _task_for_worktree(
    state: dict[str, Any], guard: dict[str, Any], root: Path
) -> dict[str, Any] | None:
    target = root.resolve()
    quarantines = guard.get("quarantines", {})
    for task in state.get("tasks", {}).values():
        worktree = task.get("worktree")
        try:
            matches_worktree = (
                isinstance(worktree, str) and Path(worktree).resolve() == target
            )
        except OSError:
            matches_worktree = False
        if matches_worktree and task.get("status") not in FINAL_TASK_STATES:
            effective = dict(task)
            guard_quarantine = (
                quarantines.get(str(task.get("id")))
                if isinstance(quarantines, dict)
                else None
            )
            if isinstance(guard_quarantine, dict):
                effective["status"] = "quarantined"
                effective["quarantine_reason"] = guard_quarantine.get("reason")
            return effective
    return None


def _is_valid_isolated_owner(
    task: dict[str, Any], payload: dict[str, Any]
) -> tuple[bool, str]:
    if task.get("status") not in {"active", "ready"}:
        return False, "isolated task is not active"
    owner = task.get("host_origin")
    if (
        not isinstance(owner, dict)
        or set(owner) != {"kind", "thread_id"}
        or owner.get("kind") != "codex"
        or not isinstance(owner.get("thread_id"), str)
        or not owner["thread_id"]
    ):
        return False, "isolated task has no verifiable Codex host owner"
    session = _session(payload)
    if not session or session != owner["thread_id"]:
        return False, "Codex session does not own this isolated task"
    worktree = task.get("worktree")
    expected_resolved = task.get("slot_worktree_resolved")
    expected_identity = task.get("slot_worktree_identity")
    if (
        not isinstance(worktree, str)
        or not isinstance(expected_resolved, str)
        or not isinstance(expected_identity, dict)
    ):
        return False, "isolated task has no verifiable worktree identity"
    try:
        raw_worktree = Path(worktree).absolute()
        resolved_worktree = raw_worktree.resolve()
    except OSError:
        return False, "isolated task worktree is unreadable"
    if _has_link_or_reparse_ancestor(raw_worktree):
        return False, "isolated task worktree contains a link or junction"
    if str(resolved_worktree) != expected_resolved:
        return False, "isolated task worktree path identity changed"
    if _directory_identity(raw_worktree) != expected_identity:
        return False, "isolated task worktree directory object was replaced"
    branch = _run_git(str(resolved_worktree), "branch", "--show-current")
    head = _run_git(str(resolved_worktree), "rev-parse", "HEAD")
    if branch != task.get("branch"):
        return False, "isolated task checked-out branch changed"
    if head != task.get("candidate_head"):
        return False, "isolated task HEAD changed outside exact-path dww commit"
    return True, ""


def _isolated_write_denial(
    guard: dict[str, Any], task: dict[str, Any], payload: dict[str, Any]
) -> str | None:
    refreshes = guard.get("root_context_refreshes", {})
    refresh = (
        refreshes.get(str(task.get("id"))) if isinstance(refreshes, dict) else None
    )
    if isinstance(refresh, dict):
        return (
            "This task resumed with a bound root objective. Run the trusted dww "
            "anchor refresh-root command before writing so the current complete plan "
            "and task context are read again. Read-only queries remain allowed."
        )
    valid, reason = _is_valid_isolated_owner(task, payload)
    if not valid:
        return "Isolated-worktree authorization is invalid: " + reason
    return None


def _patch_target_task(
    state: dict[str, Any], guard: dict[str, Any], payload: dict[str, Any], root: Path
) -> tuple[dict[str, Any] | None, str | None]:
    worktrees = _patch_target_worktrees(payload, root)
    if not worktrees:
        return None, "apply_patch target worktree could not be determined safely"
    tasks: dict[str, dict[str, Any]] = {}
    for worktree in worktrees:
        task = _task_for_worktree(state, guard, worktree)
        if task is None:
            return None, "apply_patch target is not an active managed worktree"
        task_id = task.get("id")
        if not isinstance(task_id, str) or not task_id:
            return None, "apply_patch target task has no stable identity"
        tasks[task_id] = task
    if len(tasks) != 1:
        return None, "apply_patch mixes targets from different managed tasks"
    task = next(iter(tasks.values()))
    worktree = task.get("worktree")
    if not isinstance(worktree, str):
        return None, "apply_patch target task has no worktree path"
    try:
        task_root = Path(worktree).resolve()
    except OSError:
        return None, "apply_patch target task worktree is unreadable"
    targets = _apply_patch_targets(payload, root)
    if targets is None:
        return None, "apply_patch target paths could not be determined safely"
    for target in targets:
        if not _path_within(target, task_root):
            return None, "apply_patch target escapes its managed task worktree"
        if _path_within(target, task_root / ".git"):
            return None, "apply_patch cannot modify managed task Git metadata"
    return task, None


def _patch_payload_with_inferred_owner_cwd(
    state: dict[str, Any], payload: dict[str, Any], root: Path
) -> dict[str, Any]:
    """为未携带 cwd 的宿主补丁调用推断唯一的当前隔离工作树。

    某些宿主会把 ``apply_patch`` 包在另一层工具调用中，传到 PreToolUse
    的 payload 没有执行目录。缺少目录时不能把相对路径当作安全路径；只有
    当前会话恰好拥有同一 common-dir 下唯一一个 active/ready 隔离任务时，
    才把该任务的真实 worktree 注入到本次只读解析用的 payload 副本中。
    其他会话、多个并行任务和无效工作树继续走原有拒绝路径。
    """

    if _patch_execution_directory(payload, root) is not None:
        return payload
    source_common = common_dir(root)
    if source_common is None:
        return payload

    candidates: list[dict[str, Any]] = []
    tasks = state.get("tasks", {})
    task_values = tasks.values() if isinstance(tasks, dict) else ()
    for task in task_values:
        if not isinstance(task, dict) or task.get("mode", "isolated") != "isolated":
            continue
        if task.get("status") not in {"active", "ready"}:
            continue
        worktree = task.get("worktree")
        if not isinstance(worktree, str):
            continue
        try:
            task_root = Path(worktree).resolve()
        except OSError:
            continue
        if common_dir(task_root) != source_common:
            continue
        if _is_valid_isolated_owner(task, payload)[0]:
            candidates.append(task)

    if len(candidates) != 1:
        return payload
    effective = dict(payload)
    effective["cwd"] = str(candidates[0]["worktree"])
    return effective


def _is_valid_in_place(
    root: Path, task: dict[str, Any], payload: dict[str, Any]
) -> tuple[bool, str]:
    if task.get("status") not in {"active", "ready"}:
        return False, "in-place task is not active"
    if not _session(payload) or _fingerprint(_session(payload)) != task.get(
        "session_fingerprint"
    ):
        return False, "Codex session does not own this in-place task"
    branch = _run_git(str(root), "branch", "--show-current")
    head = _run_git(str(root), "rev-parse", "HEAD")
    if branch != task.get("branch"):
        return False, "checked-out branch changed"
    if head != task.get("expected_head"):
        return False, "HEAD changed outside exact-path dww commit"
    return True, ""


class _ReadToken:
    """保存只读命令词元及其是否来自引号，避免把字面竖线当作管道。"""

    __slots__ = ("value", "quoted")

    def __init__(self, value: str, quoted: bool = False) -> None:
        self.value = value
        self.quoted = quoted


def _tokenize_read_only(command: str) -> list[_ReadToken] | None:
    """只解析一行 PowerShell 文字和一条明确管道，不执行用户输入。"""
    if "\r" in command or "\n" in command:
        return None
    tokens: list[_ReadToken] = []
    current: list[str] = []
    quote: str | None = None
    quoted = False
    token_started = False

    def flush() -> None:
        nonlocal current, quoted, token_started
        if token_started:
            tokens.append(_ReadToken("".join(current), quoted=quoted))
        current = []
        quoted = False
        token_started = False

    index = 0
    while index < len(command):
        char = command[index]
        if quote == "'":
            if char == "'":
                if index + 1 < len(command) and command[index + 1] == "'":
                    current.append("'")
                    index += 2
                    continue
                quote = None
            else:
                current.append(char)
            index += 1
            continue
        if quote == '"':
            if char in {"$", "\x60"}:
                return None
            if char == '"':
                quote = None
            else:
                current.append(char)
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            quoted = True
            token_started = True
            index += 1
            continue
        if char.isspace():
            flush()
            index += 1
            continue
        if char == "|":
            flush()
            tokens.append(_ReadToken("|"))
            index += 1
            continue
        if char in {";", "&", "<", ">", "\x60", "$", "(", ")", "{", "}"}:
            return None
        current.append(char)
        token_started = True
        index += 1
    if quote is not None:
        return None
    flush()
    return tokens


def _command_name(argument: _ReadToken) -> str:
    return argument.value.replace("\\", "/").rsplit("/", 1)[-1].lower()


def _safe_rg(tokens: list[_ReadToken]) -> bool:
    if len(tokens) < 2:
        return False
    flags = {
        "--files",
        "-a",
        "--text",
        "-n",
        "--line-number",
        "-l",
        "--files-with-matches",
        "-F",
        "--fixed-strings",
        "-i",
        "--ignore-case",
        "-S",
        "--case-sensitive",
        "-s",
        "--smart-case",
    }
    valued = {
        "-e",
        "--regexp",
        "-g",
        "--glob",
        "-A",
        "--after-context",
        "-B",
        "--before-context",
        "-C",
        "--context",
        "-m",
        "--max-count",
    }
    numeric = {
        "-A",
        "--after-context",
        "-B",
        "--before-context",
        "-C",
        "--context",
        "-m",
        "--max-count",
    }
    no_config = False
    files_mode = False
    positions = 0
    index = 1
    while index < len(tokens):
        argument = tokens[index].value
        if argument == "--":
            positions += len(tokens[index + 1 :])
            break
        if argument == "--no-config":
            if no_config:
                return False
            no_config = True
            index += 1
            continue
        if argument in flags:
            files_mode = files_mode or argument == "--files"
            index += 1
            continue
        if argument in valued:
            if index + 1 >= len(tokens):
                return False
            value = tokens[index + 1].value
            if argument in numeric and not value.isdecimal():
                return False
            index += 2
            continue
        if argument.startswith("--regexp=") or argument.startswith("--glob="):
            if not argument.partition("=")[2]:
                return False
            index += 1
            continue
        if argument.startswith("-"):
            return False
        positions += 1
        index += 1
    return no_config and (files_mode or positions > 0)


def _safe_get_content(tokens: list[_ReadToken]) -> bool:
    if len(tokens) < 2:
        return False
    options_with_values = {"-literalpath", "-path", "-encoding", "-totalcount", "-tail"}
    seen: set[str] = set()
    path_seen = False
    index = 1
    while index < len(tokens):
        argument = tokens[index].value
        lowered = argument.lower()
        if lowered in options_with_values:
            if lowered in seen or index + 1 >= len(tokens):
                return False
            value = tokens[index + 1].value
            if lowered in {"-literalpath", "-path"}:
                if path_seen or value.startswith("-"):
                    return False
                path_seen = True
            elif lowered == "-encoding":
                if value.lower() != "utf8":
                    return False
            elif not value.isdecimal():
                return False
            seen.add(lowered)
            index += 2
            continue
        if lowered == "-raw":
            if lowered in seen:
                return False
            seen.add(lowered)
            index += 1
            continue
        if argument.startswith("-"):
            return False
        if path_seen:
            return False
        path_seen = True
        index += 1
    return path_seen


def _safe_get_child_item(tokens: list[_ReadToken]) -> bool:
    """Allow the small directory-listing subset used for repository inspection."""
    options_with_values = {"-literalpath", "-path"}
    flags = {"-name", "-file", "-directory"}
    seen: set[str] = set()
    path_seen = False
    index = 1
    while index < len(tokens):
        argument = tokens[index].value
        lowered = argument.lower()
        if lowered in options_with_values:
            if lowered in seen or index + 1 >= len(tokens):
                return False
            value = tokens[index + 1].value
            if path_seen or value.startswith("-"):
                return False
            path_seen = True
            seen.add(lowered)
            index += 2
            continue
        if lowered in flags:
            if lowered in seen:
                return False
            seen.add(lowered)
            index += 1
            continue
        if argument.startswith("-") or path_seen:
            return False
        path_seen = True
        index += 1
    return True


def _safe_get_file_hash(tokens: list[_ReadToken]) -> bool:
    """Allow exact file hashing used to compare source and installed packages."""
    options_with_values = {"-literalpath", "-path", "-algorithm"}
    algorithms = {"md5", "sha1", "sha256", "sha384", "sha512"}
    seen: set[str] = set()
    path_seen = False
    index = 1
    while index < len(tokens):
        argument = tokens[index].value
        lowered = argument.lower()
        if lowered in options_with_values:
            if lowered in seen or index + 1 >= len(tokens):
                return False
            value = tokens[index + 1].value
            if lowered in {"-literalpath", "-path"}:
                if path_seen or value.startswith("-"):
                    return False
                path_seen = True
            elif lowered == "-algorithm" and value.lower() not in algorithms:
                return False
            seen.add(lowered)
            index += 2
            continue
        if argument.startswith("-") or path_seen:
            return False
        path_seen = True
        index += 1
    return path_seen


def _safe_git_ls_files(tokens: list[_ReadToken]) -> bool:
    """Keep Git file enumeration read-only without accepting Git config injection."""
    allowed = {
        "--cached",
        "--modified",
        "--deleted",
        "--others",
        "--exclude-standard",
        "--full-name",
        "--deduplicate",
        "--directory",
        "--no-empty-directory",
    }
    return all(argument.value.lower() in allowed for argument in tokens[2:])


def _safe_select(tokens: list[_ReadToken]) -> bool:
    if len(tokens) < 3 or _command_name(tokens[0]) != "select-object":
        return False
    allowed = {"-first", "-skip", "-last"}
    seen: set[str] = set()
    index = 1
    saw_count = False
    while index < len(tokens):
        argument = tokens[index].value.lower()
        if argument not in allowed or index + 1 >= len(tokens):
            return False
        if not tokens[index + 1].value.isdecimal() or argument in seen:
            return False
        seen.add(argument)
        saw_count = True
        index += 2
    return saw_count


def _safe_read_only_command(tokens: list[_ReadToken]) -> bool:
    if not tokens:
        return False
    name = _command_name(tokens[0])
    if name in {"ls", "dir", "pwd", "get-location"}:
        return len(tokens) == 1
    if name == "rg":
        return _safe_rg(tokens)
    if name == "get-content":
        return _safe_get_content(tokens)
    if name == "get-childitem":
        return _safe_get_child_item(tokens)
    if name == "get-filehash":
        return _safe_get_file_hash(tokens)
    if name in {"where", "get-command", "test-path"}:
        return len(tokens) > 1
    if name != "git" or len(tokens) < 2:
        return False
    subcommand = tokens[1].value.lower()
    if subcommand in {"status", "diff", "log", "show", "rev-parse"}:
        blocked = (
            "--output",
            "--ext-diff",
            "--textconv",
            "--upload-pack",
            "--config",
            "--config-env",
            "-c",
            "-o",
        )
        return not any(
            argument.value.lower() == option
            or argument.value.lower().startswith(option + "=")
            for argument in tokens[2:]
            for option in blocked
        )
    if subcommand == "branch":
        return len(tokens) == 2 or all(
            argument.value.lower() in {"--show-current", "--list", "-a", "-r", "-v"}
            for argument in tokens[2:]
        )
    if subcommand == "ls-files":
        return _safe_git_ls_files(tokens)
    return subcommand == "worktree" and [token.value for token in tokens[2:]] in (
        ["list"],
        ["list", "--porcelain"],
    )


def _strict_read_only_bash(command: str) -> bool:
    tokens = _tokenize_read_only(command)
    pipes = [
        index
        for index, token in enumerate(tokens or [])
        if token.value == "|" and not token.quoted
    ]
    if not tokens or len(pipes) > 1:
        return False
    if not pipes:
        return _safe_read_only_command(tokens)
    separator = pipes[0]
    left = tokens[:separator]
    right = tokens[separator + 1 :]
    return (
        bool(left)
        and _command_name(left[0]) in {"rg", "get-content"}
        and _safe_read_only_command(left)
        and _safe_select(right)
    )


def _tokenize_plugin_maintenance(command: str) -> list[_ReadToken] | None:
    """只接受 Windows 常见的一次 ``& \"…\\codex.exe\"`` 调用。

    这不是只读命令解析器的扩展：调用符仅在这里、且只能出现在第一个
    字符位置。其余语法继续由已有的保守词元器拒绝。
    """
    value = command.strip()
    if value.startswith("&"):
        if len(value) == 1 or not value[1].isspace():
            return None
        value = value[1:].lstrip()
    tokens = _tokenize_read_only(value)
    if not tokens or any(token.value == "|" and not token.quoted for token in tokens):
        return None
    return tokens


def _trusted_codex_cli(value: str) -> bool:
    """确认 Windows 正式 Codex CLI 的绝对、非链接安装位置。

    命令名或 PATH 不足以证明来源，因此只接受用户本机 Codex 安装目录中
    某个版本目录下的 ``codex.exe``。这不是对任意可执行文件的白名单。
    """
    candidate = Path(value)
    if not candidate.is_absolute() or candidate.name.casefold() != "codex.exe":
        return False
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        local_app_data = str(Path.home() / "AppData" / "Local")
    try:
        raw = candidate.absolute()
        resolved = raw.resolve(strict=True)
        install_root = (Path(local_app_data) / "OpenAI" / "Codex" / "bin").resolve(
            strict=True
        )
        relative = resolved.relative_to(install_root)
    except (OSError, ValueError):
        return False
    # 正式布局是 bin/<build>/codex.exe；拒绝目录链接和根目录中的同名文件。
    return (
        raw == resolved
        and len(relative.parts) == 2
        and relative.name.casefold() == "codex.exe"
    )


def _trusted_powershell_cli(value: str) -> bool:
    """确认 Windows PowerShell 7 的正式、非链接安装位置。

    维护发布会写入用户级本地市场，不能把裸 ``pwsh`` 或 PATH 中同名程序
    当作身份。当前契约仅支持 PowerShell 7 的标准安装位置。
    """
    program_file_roots = [
        value
        for value in (
            os.environ.get("ProgramFiles"),
            os.environ.get("ProgramFiles(x86)"),
        )
        if value
    ]
    if not program_file_roots:
        return False
    raw = Path(value)
    try:
        resolved = raw.resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    if not raw.is_absolute() or raw != resolved:
        return False
    for program_files in program_file_roots:
        try:
            trusted = (Path(program_files) / "PowerShell" / "7" / "pwsh.exe").resolve(
                strict=True
            )
        except (OSError, RuntimeError):
            continue
        if resolved == trusted:
            return True
    return False


def _maintenance_script_path() -> Path:
    """返回与当前受信 Hook 同一已安装插件根中的维护脚本。"""
    return (Path(__file__).resolve().parents[1] / "maintain-dww-plugin.ps1").resolve()


def _main_primary_worktree(root: Path) -> Path | None:
    """从同一 common-dir 的首个工作树确认干净的 ``main`` 主根。"""
    output = _run_git(str(root), "worktree", "list", "--porcelain")
    if not output:
        return None
    first_record = output.split("\n\n", 1)[0].splitlines()
    worktree_line = next(
        (line for line in first_record if line.startswith("worktree ")), None
    )
    if worktree_line is None:
        return None
    try:
        primary = Path(worktree_line.removeprefix("worktree ")).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if common_dir(primary) != common_dir(root):
        return None
    if _run_git(str(primary), "branch", "--show-current") != "main":
        return None
    return primary


def _plugin_release_invocation(command: str, root: Path) -> bool:
    """识别唯一允许的已安装 DWW 维护脚本调用。

    这是独立的写入入口，不继承只读解析器。每个位置、参数名和参数顺序都
    固定，因而 ``-MigrateMarketplace``、包装器、追加参数或 shell 拼接都
    无法借此通过。
    """
    tokens = _tokenize_plugin_maintenance(command)
    if not tokens or not _trusted_powershell_cli(tokens[0].value):
        return False
    values = [token.value for token in tokens]
    if len(values) != 12:
        return False
    if [values[index].casefold() for index in (1, 2, 4, 6, 8, 10)] != [
        "-noprofile",
        "-file",
        "-mode",
        "-sourcerepo",
        "-sourcecommit",
        "-codexpath",
    ]:
        return False
    if values[5].casefold() != "install":
        return False
    try:
        script = Path(values[3])
        expected_script = _maintenance_script_path()
        source_repo = Path(values[7])
        source_git_root = _run_git(str(source_repo), "rev-parse", "--show-toplevel")
        if source_git_root is None:
            return False
        source_root = Path(source_git_root)
        primary_root = _main_primary_worktree(root)
        if primary_root is None:
            return False
        resolved_source = source_repo.resolve(strict=True)
        resolved_script = script.resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    if (
        not script.is_absolute()
        or script != resolved_script
        or resolved_script != expected_script
        or resolved_source != primary_root
        or source_root.resolve() != primary_root
    ):
        return False
    if not re.fullmatch(r"[0-9a-fA-F]{40}", values[9]):
        return False
    return _trusted_codex_cli(values[11])


def _plugin_maintenance_invocation(command: str) -> str | None:
    """识别一条精确的 DWW 本地插件查询或重装命令。

    首次市场迁移不能在这里完成：它必须由已核实的系统 PowerShell 发布
    脚本执行。稳定市场建立后，正常更新只需要重装这一指定插件。
    """
    tokens = _tokenize_plugin_maintenance(command)
    if not tokens or not _trusted_codex_cli(tokens[0].value):
        return None
    values = [token.value for token in tokens[1:]]
    lowered = [value.casefold() for value in values]
    if len(lowered) == 2 and lowered == ["plugin", "--help"]:
        return "query"
    if len(lowered) == 2 and lowered == ["plugin", "help"]:
        return "query"
    if len(lowered) >= 3 and lowered[:3] == ["plugin", "marketplace", "list"]:
        return "query" if lowered[3:] in ([], ["--json"]) else None
    if len(lowered) >= 3 and lowered[:3] == ["plugin", "marketplace", "--help"]:
        return "query" if lowered[3:] == [] else None
    if len(lowered) >= 3 and lowered[:3] == ["plugin", "marketplace", "help"]:
        return "query" if lowered[3:] == [] else None
    if len(lowered) >= 2 and lowered[:2] == ["plugin", "list"]:
        remaining = values[2:]
        seen_marketplace = False
        seen_json = False
        index = 0
        while index < len(remaining):
            option = remaining[index].casefold()
            if (
                option == "--marketplace"
                and not seen_marketplace
                and index + 1 < len(remaining)
            ):
                if remaining[index + 1] != MAINTENANCE_MARKETPLACE:
                    return None
                seen_marketplace = True
                index += 2
                continue
            if option == "--json" and not seen_json:
                seen_json = True
                index += 1
                continue
            return None
        return "query" if seen_marketplace else None
    if len(lowered) >= 3 and lowered[:2] == ["plugin", "add"]:
        if values[2] != f"{MAINTENANCE_PLUGIN}@{MAINTENANCE_MARKETPLACE}":
            return None
        return "install" if lowered[3:] in ([], ["--json"]) else None
    return None


def _looks_like_plugin_maintenance(command: str) -> bool:
    """保守识别伪装或拼接后的 DWW 插件维护尝试。

    此函数不扩展任何允许范围。它只在精确解析失败时阻止命令落入普通
    isolated-task 的通用放行，且避开带引号的普通文本搜索。
    """
    tokens = _tokenize_plugin_maintenance(command)
    if tokens:
        values = [token.value for token in tokens]
        for index, value in enumerate(values):
            name = PureWindowsPath(value).name.casefold()
            if name.endswith("maintain-dww-plugin.ps1") and (
                index == 0
                or values[index - 1].casefold() == "-file"
                or PureWindowsPath(values[index - 1]).name.casefold()
                in {"pwsh", "pwsh.exe"}
            ):
                return True
            if name.endswith("codex.exe") and index + 1 < len(values):
                if values[index + 1].casefold() == "plugin":
                    return True
    return bool(
        re.search(
            r"""(?ix)
            (?:
                -file\s+(?:"[^"]*maintain-dww-plugin\.ps1"|\S*maintain-dww-plugin\.ps1)
                |
                (?:^|[;&|]\s*|&\s*)
                (?:"[^"]*codex\.exe"|[^\s;&|]*codex\.exe)\s+plugin\b
            )
            """,
            command,
        )
    )


def _owned_maintenance_task(
    state: dict[str, Any],
    guard: dict[str, Any],
    root: Path,
    payload: dict[str, Any],
    *,
    require_write: bool,
) -> tuple[dict[str, Any] | None, str]:
    """维护只可绑定到唯一、仍归当前会话所有的同仓隔离任务。"""
    source_common = common_dir(root)
    if source_common is None:
        return None, "repository common directory could not be verified"
    candidates: list[dict[str, Any]] = []
    for task in state.get("tasks", {}).values():
        if not isinstance(task, dict) or task.get("mode", "isolated") != "isolated":
            continue
        worktree = task.get("worktree")
        if not isinstance(worktree, str):
            continue
        try:
            if common_dir(Path(worktree)) != source_common:
                continue
        except OSError:
            continue
        if _is_valid_isolated_owner(task, payload)[0]:
            candidates.append(task)
    if len(candidates) != 1:
        return (
            None,
            "plugin maintenance requires exactly one active isolated task owned by this Codex session",
        )
    task = candidates[0]
    if require_write:
        if denial := _isolated_write_denial(guard, task, payload):
            return None, denial
    return task, ""


def _read_only_rejection_reason(command: str) -> str:
    tokens = _tokenize_read_only(command)
    if not tokens:
        return "Bash command contains unsupported shell syntax and cannot be confirmed as read-only."
    pipes = [
        index
        for index, token in enumerate(tokens)
        if token.value == "|" and not token.quoted
    ]
    if len(pipes) > 1 or (pipes and not _safe_select(tokens[pipes[0] + 1 :])):
        return "Bash command uses an unsupported pipeline or query form; use a direct read-only command or Get-Content/rg | Select-Object with numeric line options."
    if _command_name(tokens[0]) == "rg" and not any(
        token.value == "--no-config" for token in tokens
    ):
        return "rg read-only queries must include --no-config; for example: rg --no-config --files."
    if not _safe_read_only_command(tokens):
        return "Bash command was not recognized as a supported read-only query; protected worktree writes remain blocked."
    return "Bash command was not recognized as a supported read-only query; protected worktree writes remain blocked."


def _dww_runner_present(command: str) -> bool:
    """仅用于拒绝文案：出现 runner 路径不代表命令可以执行。"""
    expected = (
        Path(__file__).resolve().parents[1]
        / "skills"
        / "develop-with-worktrees"
        / "scripts"
        / "dww.py"
    ).resolve()
    return (
        str(expected).replace("\\", "/").lower() in command.replace("\\", "/").lower()
    )


def _dww_invocation(command: str, root: Path) -> tuple[str, Path] | None:
    """Parse one literal DWW runner call and return its target worktree root.

    Hook ``cwd`` is the Codex session directory. ``--repo`` is an explicit DWW
    target, so it is accepted only after the real runner and literal
    PowerShell command shape have been checked, and only when its Git common
    directory matches the session repository.
    """
    tokens = _tokenize_read_only(command)
    if not tokens or any(token.value == "|" and not token.quoted for token in tokens):
        return None
    if len(tokens) < 4:
        return None
    if _command_name(tokens[0]) not in {"uv", "uv.exe"}:
        return None
    if tokens[1].value.lower() != "run" or tokens[2].value.lower() != "--script":
        return None
    runner = Path(tokens[3].value).resolve()
    expected_runner = (
        Path(__file__).resolve().parents[1]
        / "skills"
        / "develop-with-worktrees"
        / "scripts"
        / "dww.py"
    ).resolve()
    if runner != expected_runner:
        return None
    repo_value: str | None = None
    subcommand: str | None = None
    index = 4
    while index < len(tokens):
        argument = tokens[index].value
        lowered = argument.lower()
        if lowered == "--repo":
            if repo_value is not None or index + 1 >= len(tokens):
                return None
            value = tokens[index + 1].value
            if not value or value.startswith("-"):
                return None
            repo_value = value
            index += 2
            continue
        if lowered.startswith("--repo="):
            if repo_value is not None:
                return None
            value = argument.partition("=")[2]
            if not value:
                return None
            repo_value = value
            index += 1
            continue
        if lowered == "--json":
            index += 1
            continue
        if lowered == "--help":
            if index != len(tokens) - 1:
                return None
            subcommand = "help"
            break
        if lowered in DWW_SUBCOMMANDS:
            if any(
                token.value.lower() == "--repo"
                or token.value.lower().startswith("--repo=")
                for token in tokens[index + 1 :]
            ):
                return None
            subcommand = lowered
            break
        return None
    if subcommand is None:
        return None
    target_root = root.resolve() if repo_value is None else git_root(repo_value)
    if target_root is None:
        return None
    session_common = common_dir(root)
    target_common = common_dir(target_root)
    if session_common is None or target_common != session_common:
        return None
    return subcommand, target_root


def _dww_subcommand(command: str, root: Path) -> str | None:
    """Return the subcommand only for a real same-common-dir DWW invocation."""
    invocation = _dww_invocation(command, root)
    return invocation[0] if invocation is not None else None


def _is_git_command(command: str) -> bool:
    try:
        tokens = shlex.split(command.strip(), posix=False)
    except ValueError:
        return False
    if not tokens:
        return False
    executable = Path(tokens[0].strip('"')).name.lower()
    if executable in {"git", "git.exe", "git.cmd"}:
        return True
    if executable not in {
        "cmd",
        "cmd.exe",
        "pwsh",
        "pwsh.exe",
        "powershell",
        "powershell.exe",
    }:
        return False
    directives = {"/c", "-command", "-c"}
    for index, argument in enumerate(tokens[:-1]):
        if argument.lower() in directives:
            nested = tokens[index + 1].strip('"').strip().split(maxsplit=1)
            return bool(nested) and Path(nested[0]).name.lower() in {
                "git",
                "git.exe",
                "git.cmd",
            }
    return False


def _deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _context(event: str, message: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": message,
        }
    }


def _session_context(
    root: Path, state: dict[str, Any], guard: dict[str, Any], payload: dict[str, Any]
) -> str:
    session = _session(payload)
    base = (
        "Repository adopts develop-with-worktrees. For ordinary modifications, proactively use Start → exact-path Commit → useful development checks → Finish in the returned worktree; new default batched tasks may Finish directly from active after the exact commit. Follow the applicable batch or explicit tail until the requested work is delivered. "
        "The trusted Codex hook hard-denies writes in the current base worktree unless a task has explicit authorization."
    )
    if session:
        base += f" This Codex session identifier is `{session}`. If the user explicitly requests direct changes in this directory for this task, choose current-task first, then do not run the DWW lifecycle."
    active = _task_for_worktree(state, guard, root)
    if active and active.get("mode") == "in-place":
        base += f" Active in-place task: {active.get('id')} (status {active.get('status')})."
    return base


def decide(payload: dict[str, Any]) -> dict[str, Any] | None:
    event = str(payload.get("hook_event_name") or payload.get("hookEventName") or "")
    root = git_root(str(payload.get("cwd") or "."))
    tool = str(payload.get("tool_name") or payload.get("toolName") or "")
    if event == "PreToolUse" and tool.lower() == "apply_patch":
        # 在路由提前退出前检查实际目标；仓库外会话不能跳过目标仓库的保护。
        targets = _apply_patch_targets(payload, root or Path.cwd())
        if targets is None and root is None:
            return _deny("apply_patch target paths could not be determined safely")
        session = _session(payload)
        artifact_scopes = [
            _session_artifact_scope(target, session) for target in targets or []
        ]
        # 当前会话的交付产物不属于会话 checkout 的 common-dir。只有完整补丁
        # 的每个目标都通过既有严格核验时，才可在跨仓库预检前直接放行。
        if artifact_scopes and all(
            scope == PATCH_SCOPE_SESSION_ARTIFACT for scope in artifact_scopes
        ):
            return None
        for target in targets or []:
            parent = _nearest_existing_directory(target)
            target_root, reliable = _git_root_probe(parent) if parent else (None, False)
            if not reliable:
                return _deny("apply_patch target repository could not be verified")
            if target_root is None:
                continue
            if not (target_root / ".solo-ai" / "config.toml").is_file():
                continue
            if root is not None and common_dir(root) != common_dir(target_root):
                return _deny(
                    "apply_patch target belongs to another protected repository"
                )
            if root is None:
                root = target_root
    if root is None:
        return None
    workflows = detect_existing_workflows(root)
    adopted = (root / ".solo-ai" / "config.toml").exists()
    route = decide_route(
        workflows=workflows,
        local_enabled=not preference_disabled(root),
        current_task=task_bypass_active(root, payload),
        adopted=adopted,
        delegated=inspect_delegated(root, common_dir(root) or root / ".git"),
    )
    action = route["action"]
    if action == "defer":
        if event != "SessionStart":
            return None
        workflow_text = ", ".join(route["workflows"]) or "declared adapter"
        return _context(
            event,
            "This repository has a mature workflow ("
            + workflow_text
            + "). develop-with-worktrees silently defers: do not ask its repository-choice question or change DWW state; follow the repository's own instructions. A delegated adapter is not executable unless its exact current fingerprint is valid and locally approved.",
        )
    if action == "delegated":
        if event != "SessionStart":
            return None
        adapter = route["adapter"]
        return _context(
            event,
            "This repository owns its mature lifecycle through the locally approved delegated adapter `"
            + str(adapter["id"])
            + "`. Use `dww delegated invoke` and the repository's own instructions; do not initialize or run the managed DWW Start/Ready/Finish lifecycle. The generic guard steps aside because the repository adapter remains authoritative.",
        )
    if action == "disabled":
        return _context(
            event,
            "The user chose normal current-directory development for this repository on this machine. Do not initialize or run develop-with-worktrees for this task; follow the user's explicit direction.",
        )
    if action == "current-task":
        # 用户明确要求“像没安装一样”时，连 PostToolUse 脏基线告警也必须退出，
        # 否则仍会把本次普通开发误报为逃逸写入。
        if event == "SessionStart":
            return _context(
                event,
                "A develop-with-worktrees current-task override is active for this exact session. Do not ask the repository-choice question or run the DWW lifecycle.",
            )
        return None
    state, _ = read_state(root)
    guard, _ = read_guard_state(root)
    if event == "SessionStart":
        if action == "managed":
            active = _task_for_worktree(state, guard, root)
            if (
                active
                and active.get("mode", "isolated") == "isolated"
                and active.get("root_anchor_id")
                and _is_valid_isolated_owner(active, payload)[0]
            ):
                _mark_root_context_refresh(
                    root,
                    str(active["id"]),
                    reason="SessionStart",
                )
            return _context(event, _session_context(root, state, guard, payload))
        return _context(
            event,
            "Only when the user first intends to modify this unchosen repository, ask exactly:\n\n此仓库怎么修改？\n\n1. 每个任务使用独立目录（推荐）\n   任务互不影响，完成后自动合回。\n\n2. 这一次直接改当前目录\n   只跳过这一次，下次还会询问。\n\n3. 以后都直接改当前目录\n   记住此选择，这个仓库不再询问。\n\n只影响本机，可随时修改。\n\nAsk only this one question. After the user chooses, carry out the matching choice silently without any further confirmation. A child-agent instruction that already includes a one-time delegation code must register that code before asking the user anything.",
        )
    if event not in {"PreToolUse", "PostToolUse"}:
        return None
    tool = str(payload.get("tool_name") or payload.get("toolName") or "")
    command = command_from(payload)
    if event == "PostToolUse":
        task = _task_for_worktree(state, guard, root)
        if task and task.get("mode", "isolated") == "isolated":
            return None
        if task and task.get("mode") == "in-place":
            valid, reason = _is_valid_in_place(root, task, payload)
            if not valid:
                _quarantine(root, str(task.get("id")), reason)
                return _context(
                    event,
                    "Current-worktree identity changed after a tool call. Files were preserved and the task was quarantined: "
                    + reason
                    + ". Use dww doctor; do not continue, reset, clean, or move files automatically.",
                )
            return None
        if adopted and (dirty := _dirty_paths(root)):
            _record_alert(root, kind="unauthorized-dirty-base", paths=dirty)
            return _context(
                event,
                "Detected tracked or untracked changes in a protected base worktree. The files were preserved; do not continue, reset, clean, or move them automatically. Review dww doctor for the recorded paths and ask the user how to proceed.",
            )
        return None
    if tool.lower() == "apply_patch":
        patch_payload = _patch_payload_with_inferred_owner_cwd(state, payload, root)
        patch_scope = _apply_patch_scope(patch_payload, root)
        if patch_scope in {PATCH_SCOPE_EXTERNAL, PATCH_SCOPE_SESSION_ARTIFACT}:
            return None
        if patch_scope not in {PATCH_SCOPE_PROTECTED, PATCH_SCOPE_CODEX_HOME}:
            reasons = {
                PATCH_SCOPE_INVALID: "apply_patch 补丁字段、执行目录或可信会话产物根无法确认",
                PATCH_SCOPE_OTHER_SESSION: "apply_patch 目标属于其他或无效的 Codex 会话产物目录",
                PATCH_SCOPE_CODEX_HOME: "apply_patch 目标属于受保护的 CODEX_HOME 配置、索引或非产物路径",
                PATCH_SCOPE_ARTIFACT_ESCAPE: "apply_patch 会话产物目标经过链接或 junction，无法安全确认",
                PATCH_SCOPE_MIXED: "apply_patch 混合了会话产物、仓库或普通外部目标",
                PATCH_SCOPE_FOREIGN_REPOSITORY: "apply_patch 目标属于其他或嵌套 Git 仓库",
            }
            return _deny(
                reasons.get(
                    patch_scope,
                    "apply_patch target paths could not be determined safely",
                )
                + ". Protected base-worktree writes remain blocked."
            )
        patch_task, patch_reason = _patch_target_task(state, guard, patch_payload, root)
        if patch_task is None:
            if patch_scope == PATCH_SCOPE_CODEX_HOME:
                return _deny(
                    "apply_patch 目标属于受保护的 CODEX_HOME 配置、索引或非产物路径. "
                    "Protected worktree writes remain blocked."
                )
            return _deny(
                (patch_reason or "apply_patch target task could not be verified")
                + ". Protected worktree writes remain blocked."
            )
        if patch_task.get("mode", "isolated") == "isolated":
            if denial := _isolated_write_denial(guard, patch_task, patch_payload):
                return _deny(denial)
            return None
        patch_root = Path(str(patch_task["worktree"])).resolve()
        valid, reason = _is_valid_in_place(patch_root, patch_task, payload)
        if not valid:
            _quarantine(patch_root, str(patch_task.get("id")), reason)
            return _deny(
                "Current-worktree authorization is no longer valid; files were preserved "
                "and the in-place task was quarantined: " + reason
            )
        return None
    dww_invocation = _dww_invocation(command, root) if tool == "Bash" else None
    dww_command = dww_invocation[0] if dww_invocation is not None else None
    dww_target_root = dww_invocation[1] if dww_invocation is not None else None
    if tool == "Bash" and _strict_read_only_bash(command):
        return None
    if action == "ask":
        if dww_command in {
            "init",
            "choose",
            "doctor",
            "route",
            "status",
            "version",
            "help",
        }:
            return None
        if tool == "Bash" and dww_command is None and _dww_runner_present(command):
            return _deny(
                "A DWW runner path was detected, but the invocation could not be "
                "verified. Check literal quoting, --repo and the subcommand; "
                "the repository choice was not changed and no write was allowed."
            )
        return _deny(
            "Potential repository write is blocked until the user chooses how this repository should be modified. Show the one compact three-choice question, then use the matching trusted dww choose command."
        )
    if tool == "Bash":
        if _plugin_release_invocation(command, root):
            _task, denial = _owned_maintenance_task(
                state, guard, root, payload, require_write=True
            )
            if _task is None:
                return _deny("Plugin maintenance is blocked: " + denial)
            return None
        maintenance = _plugin_maintenance_invocation(command)
        if maintenance is not None:
            _task, denial = _owned_maintenance_task(
                state, guard, root, payload, require_write=maintenance == "install"
            )
            if _task is None:
                return _deny("Plugin maintenance is blocked: " + denial)
            return None
        if _looks_like_plugin_maintenance(command):
            return _deny(
                "DWW plugin maintenance command is blocked unless it exactly matches "
                "the trusted query, add, or Install contract."
            )
    task = (
        _task_for_worktree(state, guard, dww_target_root)
        if dww_target_root is not None
        else None
    ) or _task_for_worktree(state, guard, root)
    if task and task.get("mode", "isolated") == "isolated":
        if dww_command in DWW_READ_ONLY_SUBCOMMANDS:
            return None
        if dww_command == "anchor":
            valid, reason = _is_valid_isolated_owner(task, payload)
            if not valid:
                return _deny("Isolated-worktree authorization is invalid: " + reason)
            return None
        if denial := _isolated_write_denial(guard, task, payload):
            return _deny(denial)
        return None
    if task and task.get("mode") == "in-place":
        if task.get("status") == "quarantined":
            if dww_command in DWW_QUARANTINE_SUBCOMMANDS:
                return None
            return _deny(
                "In-place task is quarantined. Preserve the worktree and use only dww doctor/status/plan or explicit resume-in-place after manual restoration."
            )
        task_root = Path(str(task["worktree"])).resolve()
        valid, reason = _is_valid_in_place(task_root, task, payload)
        if not valid:
            if dww_command == "resume-in-place":
                return None
            _quarantine(task_root, str(task.get("id")), reason)
            return _deny(
                "Current-worktree authorization is no longer valid; files were preserved and the in-place task was quarantined: "
                + reason
            )
        if dww_command in DWW_SUBCOMMANDS:
            return None
        if tool == "Bash" and _is_git_command(command):
            return _deny(
                "Direct Git state changes are blocked in an in-place task. Use exact-path dww commit; do not run raw git add, commit, switch, reset, clean, merge, or checkout."
            )
        # Current-worktree tasks may run their test/tool commands and edit files;
        # branch and HEAD are checked again after the call.
        return None
    if dww_invocation is not None:
        return None
    if tool == "Bash" and "dww.py" in command.replace("\\", "/"):
        return _deny(
            "Only the installed develop-with-worktrees lifecycle runner with --repo set to this worktree is allowed. The supplied dww command was not recognized."
        )
    dirty = _dirty_paths(root)
    if dirty:
        _record_alert(root, kind="unauthorized-dirty-base", paths=dirty)
        detail = ", ".join(dirty[:5])
        return _deny(
            "Protected base worktree already has unowned changes. They were preserved; do not continue, reset, clean, or move them automatically. Inspect and ask the user how to proceed. Paths: "
            + detail
        )
    if tool == "Bash" and command:
        return _deny(_read_only_rejection_reason(command))
    return _deny(
        "Protected base-worktree write blocked. For ordinary work, run dww Start and edit only its returned worktree. If the user explicitly asks to change this directory for this task, first choose current-task and then follow normal development without a DWW lifecycle."
    )


def main() -> int:
    payload: dict[str, Any] = {}
    try:
        raw = json.load(sys.stdin)
        if not isinstance(raw, dict):
            raise TypeError("hook payload must be an object")
        payload = raw
        result = decide(payload)
    except Exception:  # noqa: BLE001 - a guard fault must conservatively deny writes.
        event = str(
            payload.get("hook_event_name") or payload.get("hookEventName") or ""
        )
        message = "develop-with-worktrees guard could not inspect this hook event. Treat a possible write as unsafe and retry only after dww doctor."
        if event == "PreToolUse":
            result = _deny(message)
        elif event in {"SessionStart", "PostToolUse"}:
            result = _context(event, message)
        else:
            print(message, file=sys.stderr)
            return 2
    if result:
        print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
