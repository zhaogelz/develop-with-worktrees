from __future__ import annotations

import errno
import json
import os
import platform
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any

import psutil

from .config import (
    CommandSpec,
    VerificationConfig,
    discover_validation_commands,
    load_repo_config,
    load_verification_config,
    managed_block,
    remove_managed_agents_block,
    render_agents,
    render_repo_config,
    render_verification_config,
)
from .cleanup import inspect_untracked, require_managed_directory_identity
from .abandonment import prepare as prepare_abandonment
from .abandonment import resume as resume_abandonment
from .abandonment import write_completed_receipt as write_abandonment_receipt
from .integration import integration_turn, prepare as prepare_integration
from .integration import legacy_transaction as legacy_integration_transaction
from .integration import migrate_legacy_receipt
from .integration import resume_prepared as resume_integration
from .integration import write_completed_receipt
from .proof import (
    ValidationBaseChanged,
    approval_plan,
    require_exact_passed_proof,
    validate,
)
from .repo import GitRepo
from .routing import decide_route, detect_existing_workflows
from .safety import require_safe
from .state import FINAL_TASK_STATES, IN_PLACE_MODE, ISOLATED_MODE, StateStore
from .util import (
    DirectoryLock,
    SoloAIError,
    atomic_write_json,
    ensure_within,
    process_matches,
    process_snapshot,
    path_identity,
    read_json,
    redact_text,
    run_logged,
    safe_slug,
    sha256_text,
    stable_json,
    utc_timestamp,
)

BOOTSTRAP_SCHEMA = 1
TASK_GRANT_SCHEMA = 1
MAX_READY_CONVERGENCE_RETRIES = 5
LOCKFILE_NAMES = (
    "uv.lock",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "Cargo.lock",
    "go.sum",
)


def _preferences(repo: GitRepo) -> dict[str, Any]:
    return read_json(
        repo.local_dir / "preferences.json", {"schema_version": 1, "enabled": True}
    )


def _task_grants_path(repo: GitRepo) -> Path:
    """临时直改授权只保存在 Git common-dir，绝不写入工作区。"""
    return repo.local_dir / "session-overrides.json"


def _task_grants(repo: GitRepo) -> dict[str, Any]:
    return read_json(
        _task_grants_path(repo), {"schema_version": TASK_GRANT_SCHEMA, "grants": []}
    )


def _session_fingerprint(session_id: str) -> str:
    if not session_id:
        raise SoloAIError("Current-task choice requires the Codex session identifier")
    return sha256_text(session_id)


def task_bypass_active(repo: GitRepo, *, session_id: str) -> bool:
    """判断本次 Codex 会话是否被明确授权完全跳过本技能。"""
    if not session_id:
        return False
    session = _session_fingerprint(session_id)
    worktree = str(repo.root.resolve())
    payload = _task_grants(repo)
    return any(
        isinstance(grant, dict)
        and grant.get("worktree") == worktree
        and session in grant.get("sessions", [])
        for grant in payload.get("grants", [])
    )


def _grant_current_task(
    repo: GitRepo, *, session_id: str, delegation_code: str | None
) -> dict[str, Any]:
    """登记一次会话授权；子智能体只能凭父任务的委托码加入同一授权。"""
    session = _session_fingerprint(session_id)
    worktree = str(repo.root.resolve())
    active_here = [
        task["id"]
        for task in StateStore(repo).read()["tasks"].values()
        if task.get("status") not in FINAL_TASK_STATES
        and Path(str(task.get("worktree", ""))).resolve() == repo.root.resolve()
    ]
    if active_here:
        raise SoloAIError(
            "Finish or abandon the active DWW task in this directory before choosing normal current-directory development: "
            + ", ".join(active_here)
        )
    with DirectoryLock(repo.local_dir / "locks" / "session-overrides.lock", wait=True):
        payload = _task_grants(repo)
        grants = payload.get("grants")
        if not isinstance(grants, list):
            raise SoloAIError(
                "Current-task local authorization state is invalid; preserve it and run doctor"
            )

        if delegation_code:
            code_hash = sha256_text(delegation_code)
            for grant in grants:
                if (
                    isinstance(grant, dict)
                    and grant.get("worktree") == worktree
                    and secrets.compare_digest(
                        str(grant.get("delegation_hash", "")), code_hash
                    )
                ):
                    sessions = grant.setdefault("sessions", [])
                    if session not in sessions:
                        sessions.append(session)
                    grant["updated_at"] = utc_timestamp()
                    atomic_write_json(_task_grants_path(repo), payload)
                    return {"choice": "current-task", "delegated": True}
            raise SoloAIError(
                "The current-task delegation code is invalid for this directory"
            )

        code = secrets.token_urlsafe(24)
        grants.append(
            {
                "worktree": worktree,
                "delegation_hash": sha256_text(code),
                "sessions": [session],
                "created_at": utc_timestamp(),
            }
        )
        payload["schema_version"] = TASK_GRANT_SCHEMA
        atomic_write_json(_task_grants_path(repo), payload)
        return {
            "choice": "current-task",
            "delegated": False,
            "delegation_code": code,
        }


def set_local_enabled(repo: GitRepo, *, enabled: bool) -> dict[str, Any]:
    result = {"schema_version": 1, "enabled": enabled, "updated_at": utc_timestamp()}
    atomic_write_json(repo.local_dir / "preferences.json", result)
    return result


def disable(repo: GitRepo) -> dict[str, Any]:
    """只允许在没有在途任务时停用，避免阻断后续清理。"""
    with maintenance_lock(repo):
        store = StateStore(repo)
        state = store.read()
        if any(
            task.get("status") not in FINAL_TASK_STATES
            for task in state["tasks"].values()
        ):
            raise SoloAIError("Active or quarantined tasks block disabling")
        if any((repo.local_dir / "queue").glob("*.json")):
            raise SoloAIError("Integration queue tickets block disabling")
        _require_no_lifecycle_lock(repo)
        return set_local_enabled(repo, enabled=False)


def local_enabled(repo: GitRepo) -> bool:
    return bool(_preferences(repo).get("enabled", True))


def choose(
    repo: GitRepo,
    *,
    mode: str,
    slots: int,
    commands: list[CommandSpec] | None,
    session_id: str | None = None,
    delegation_code: str | None = None,
) -> dict[str, Any]:
    """将首次三选一交互收敛为唯一入口，避免把内部初始化细节暴露给用户。"""
    route = repository_route(repo, session_id=session_id)
    if route["action"] == "defer":
        return {
            "choice": mode,
            "decision": "deferred",
            "reason": route["reason"],
            "workflows": route["workflows"],
        }
    if mode == "current-task":
        if not session_id:
            raise SoloAIError(
                "Current-task choice requires --session from the trusted Codex hook"
            )
        return _grant_current_task(
            repo, session_id=session_id, delegation_code=delegation_code
        )
    if session_id or delegation_code:
        raise SoloAIError("--session and --delegate only apply to --mode current-task")
    if mode == "current-repository":
        preference = disable(repo)
        return {"choice": "current-repository", "local_preference": preference}
    if mode != "isolated":
        raise SoloAIError(f"Unknown repository choice: {mode}")
    if not local_enabled(repo):
        # 用户明确重新选择隔离开发时，安全地撤销本机长期退出。
        set_local_enabled(repo, enabled=True)
    if (repo.root / ".solo-ai" / "config.toml").exists():
        return {"choice": "isolated", "decision": "already-adopted"}
    result = initialize(
        repo,
        slots=slots,
        commands=commands,
        accept=True,
        accept_static_only=True,
    )
    return {"choice": "isolated", **result}


def _bootstrap(repo: GitRepo) -> dict[str, Any]:
    return read_json(repo.local_dir / "bootstrap.json", {})


def repository_route(repo: GitRepo, *, session_id: str | None = None) -> dict[str, Any]:
    workflows = detect_existing_workflows(repo.root)
    return decide_route(
        workflows=workflows,
        local_enabled=local_enabled(repo),
        current_task=bool(
            session_id and task_bypass_active(repo, session_id=session_id)
        ),
        adopted=(repo.policy_path() / ".solo-ai" / "config.toml").exists(),
    )


def _effective_mode(repo: GitRepo) -> str:
    action = repository_route(repo)["action"]
    return "uninitialized" if action == "ask" else str(action)


def _approval_path(repo: GitRepo) -> Path:
    return repo.local_dir / "approvals.json"


def _approval_fingerprint(
    repo: GitRepo, verification: VerificationConfig, *, cwd: Path
) -> tuple[str, dict[str, Any]]:
    plan = approval_plan(repo, cwd=cwd, verification=verification)
    return sha256_text(stable_json(plan)), plan


def approve(
    repo: GitRepo, verification: VerificationConfig, *, cwd: Path | None = None
) -> dict[str, Any]:
    policy = cwd or repo.policy_path()
    fingerprint, plan = _approval_fingerprint(repo, verification, cwd=policy)
    approvals = read_json(_approval_path(repo), {"schema_version": 2, "accepted": {}})
    approvals["accepted"][fingerprint] = {"accepted_at": utc_timestamp(), "plan": plan}
    atomic_write_json(_approval_path(repo), approvals)
    return {"fingerprint": fingerprint, "plan": plan}


def require_approval(
    repo: GitRepo, verification: VerificationConfig, *, cwd: Path | None = None
) -> None:
    policy = cwd or repo.policy_path()
    fingerprint, _ = _approval_fingerprint(repo, verification, cwd=policy)
    if fingerprint not in read_json(_approval_path(repo), {"accepted": {}}).get(
        "accepted", {}
    ):
        raise SoloAIError(
            "This machine has not approved the full normalized validation plan. Review `doctor` then run `approve --accept`."
        )


def _init_lock(repo: GitRepo) -> DirectoryLock:
    return DirectoryLock(repo.local_dir / "locks" / "initialize.lock")


def maintenance_lock(repo: GitRepo) -> DirectoryLock:
    """串行化会修改空闲槽位或清理本地状态的维护操作。"""
    return DirectoryLock(repo.common_dir / "solo-ai-maintenance.lock")


def _require_no_lifecycle_lock(repo: GitRepo) -> None:
    locks = repo.local_dir / "locks"
    if locks.exists() and any(path.name != "state.lock" for path in locks.iterdir()):
        raise SoloAIError("An active lifecycle lock blocks this maintenance operation")


def _initialization_plan(
    repo: GitRepo, commands: list[CommandSpec], *, slots: int
) -> dict[str, Any]:
    return {
        "slots": slots,
        "profiles": (
            [
                {
                    "id": "default",
                    "paths": ["**"],
                    "commands": [command.redacted() for command in commands],
                    "cross_task_reuse": False,
                    "external_state": "unknown",
                }
            ]
            if commands
            else []
        ),
        "static_only": not commands,
        "dependency_inputs": [
            name for name in LOCKFILE_NAMES if (repo.root / name).exists()
        ],
        "platform_condition": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "note": "Exact executable paths and versions are captured and require local approval after bootstrap.",
        },
        "cross_task_policy": "disabled by default; only explicit external_state = none with declared closed inputs may reuse",
        "tracked_bootstrap_files": [
            ".solo-ai/config.toml",
            ".solo-ai/verification.toml",
            "AGENTS.md managed block",
        ],
    }


def initialize(
    repo: GitRepo,
    *,
    slots: int,
    commands: list[CommandSpec] | None,
    accept: bool,
    accept_static_only: bool,
    decline: bool = False,
) -> dict[str, Any]:
    """Create an isolated policy commit, never touching dirty primary content."""
    if decline:
        if accept or accept_static_only:
            raise SoloAIError("--decline cannot be combined with acceptance flags")
        return {
            "decision": "declined",
            "local_preference": disable(repo),
        }
    if not local_enabled(repo):
        raise SoloAIError(
            "This repository is locally disabled; run enable before adopting it"
        )
    if not 1 <= slots <= 32:
        raise SoloAIError("--slots must be between 1 and 32")
    existing = detect_existing_workflows(repo.root)
    if existing:
        # Do this before acquiring a local lifecycle lock: defer mode must not
        # create a .git/solo-ai directory either.
        return {
            "decision": "deferred",
            "reason": "existing-workflow",
            "workflows": existing,
        }
    with _init_lock(repo):
        existing = detect_existing_workflows(repo.root)
        if existing:
            # Deliberately no tracked or local workflow state is written in defer mode.
            return {
                "decision": "deferred",
                "reason": "existing-workflow",
                "workflows": existing,
            }
        if (repo.root / ".solo-ai" / "config.toml").exists() or _bootstrap(repo):
            raise SoloAIError(
                "Repository is already adopted or has a pending bootstrap; run doctor"
            )
        primary, default = repo.ensure_primary_default()
        selected = (
            commands
            if commands is not None
            else discover_validation_commands(repo.root)
        )
        static_only = not selected
        if static_only and not accept_static_only and accept:
            raise SoloAIError(
                "No validation command was discovered. Re-run with --accept-static-only only after reviewing the limitation."
            )
        if (selected and not accept) or (static_only and not accept_static_only):
            return {
                "decision": "needs-approval",
                "plan": _initialization_plan(repo, selected, slots=slots),
            }
        bootstrap_id = uuid.uuid4().hex[:8]
        branch = f"solo-ai/bootstrap-{bootstrap_id}"
        worktree = repo.local_dir / "bootstrap" / bootstrap_id / "worktree"
        repo.git(["worktree", "add", "-b", branch, str(worktree), default], cwd=primary)
        try:
            config_dir = worktree / ".solo-ai"
            config_dir.mkdir(parents=True, exist_ok=True)
            agents = worktree / "AGENTS.md"
            agents_existed = agents.exists()
            (config_dir / "config.toml").write_text(
                render_repo_config(slots=slots, agents_file_created=not agents_existed),
                encoding="utf-8",
                newline="\n",
            )
            (config_dir / "verification.toml").write_text(
                render_verification_config(selected, static_only=static_only),
                encoding="utf-8",
                newline="\n",
            )
            previous = agents.read_text(encoding="utf-8") if agents.exists() else ""
            agents.write_text(render_agents(previous), encoding="utf-8", newline="\n")
            repo.git(
                [
                    "add",
                    "--",
                    ".solo-ai/config.toml",
                    ".solo-ai/verification.toml",
                    "AGENTS.md",
                ],
                cwd=worktree,
            )
            # The caller supplies a project-conventional message in a real adoption.
            # This fallback is only the generic bootstrap, not a task-change commit.
            repo.git(
                ["commit", "-m", "chore: adopt local worktree workflow"], cwd=worktree
            )
        except Exception as exc:
            raise SoloAIError(
                f"Bootstrap was preserved at {worktree} for inspection. Cause: {exc}"
            ) from exc
        repo.add_local_exclude("/.worktrees/")
        clean_primary = repo.is_clean(primary)
        bootstrap = {
            "schema_version": BOOTSTRAP_SCHEMA,
            "branch": branch,
            "worktree": str(worktree),
            "default_branch": default,
            "bootstrap_head": repo.head(worktree),
            "created_at": utc_timestamp(),
        }
        if clean_primary:
            repo.git(["merge", "--ff-only", branch], cwd=primary)
            repo.git(["worktree", "remove", str(worktree)], cwd=primary)
            repo.git(["branch", "-d", branch], cwd=primary)
        else:
            atomic_write_json(repo.local_dir / "bootstrap.json", bootstrap)
        policy = repo.policy_path()
        verification = load_verification_config(repo, cwd=policy)
        approval = approve(repo, verification, cwd=policy)
        StateStore(repo).ensure_slots(load_repo_config(repo, cwd=policy))
        return {
            "decision": "adopted" if clean_primary else "pending-primary-clean",
            "slots": slots,
            "static_only": static_only,
            "commands": [command.redacted() for command in selected],
            "approval": approval["fingerprint"],
            "primary_dirty_excluded": not clean_primary,
        }


def _config_and_mode(repo: GitRepo) -> tuple[Any, VerificationConfig, Path]:
    mode = _effective_mode(repo)
    if mode == "disabled":
        raise SoloAIError(
            "develop-with-worktrees is disabled on this machine; run enable to opt in again"
        )
    if mode == "defer":
        raise SoloAIError(
            "An existing mature workflow governs this repository; develop-with-worktrees will make no managed changes"
        )
    if mode != "managed":
        raise SoloAIError(
            "Repository is not set up for isolated tasks. Record the repository choice with `choose` first"
        )
    policy = repo.policy_path()
    config = load_repo_config(repo, cwd=policy)
    verification = load_verification_config(repo, cwd=policy)
    require_approval(repo, verification, cwd=policy)
    return config, verification, policy


def _base_ref(repo: GitRepo) -> str:
    """保留给空闲槽位等没有调用方上下文的场景。"""
    bootstrap = _bootstrap(repo)
    return str(bootstrap.get("branch") or repo.default_branch())


def _checked_out_branch_worktree(repo: GitRepo, branch: str) -> Path:
    for item in repo.worktrees():
        if repo.branch(item.path) == branch:
            return item.path
    raise SoloAIError(
        f"Base branch {branch!r} must be checked out in a local worktree before starting a task"
    )


def _resolve_start_base(
    repo: GitRepo, store: StateStore, explicit_base: str | None
) -> tuple[str, str, Path]:
    """确定任务基线；默认以调用处当前分支为准，不把 main 当成隐含前提。"""
    try:
        owner = store.task_for_worktree(repo.root)
    except SoloAIError:
        owner = None
    if owner and StateStore.mode(owner) == ISOLATED_MODE:
        raise SoloAIError(
            f"Cannot start a child task from active managed task {owner['id']}; finish, abandon, or use its recorded base worktree"
        )
    bootstrap = _bootstrap(repo)
    if explicit_base:
        base_ref = explicit_base
    elif bootstrap.get("branch") and not (repo.root / ".solo-ai").exists():
        # 脏主工作树的首次采用尚未合入策略时，唯一可验证的基线是 bootstrap。
        base_ref = str(bootstrap["branch"])
    else:
        base_ref = repo.branch(repo.root)
        if base_ref is None:
            raise SoloAIError(
                "Current worktree is detached; pass --base with a checked-out local branch"
            )
    exists = repo.git(
        ["show-ref", "--verify", "--quiet", f"refs/heads/{base_ref}"], check=False
    )
    if exists.returncode != 0:
        raise SoloAIError(f"Base branch does not exist locally: {base_ref}")
    base_worktree = _checked_out_branch_worktree(repo, base_ref)
    active_paths = {
        Path(str(task["worktree"])).resolve()
        for task in store.read()["tasks"].values()
        if StateStore.mode(task) == ISOLATED_MODE
        if task.get("status") not in FINAL_TASK_STATES
    }
    if base_worktree.resolve() in active_paths:
        raise SoloAIError(
            "Base branch is checked out by an active managed task; use a stable base worktree instead"
        )
    return (
        base_ref,
        repo.git(["rev-parse", base_ref], cwd=base_worktree).stdout.strip(),
        base_worktree,
    )


def start(
    repo: GitRepo,
    *,
    name: str,
    base: str | None = None,
    in_place: bool = False,
    session_id: str | None = None,
) -> dict[str, Any]:
    with maintenance_lock(repo):
        config, _, _ = _config_and_mode(repo)
        store = StateStore(repo)
        store.ensure_slots(config)
        if in_place:
            # 与隔离任务的实际合入使用同一把锁，避免刚登记直改后基线被并发推进。
            with DirectoryLock(
                repo.local_dir / "locks" / "integration.lock", wait=True
            ):
                if base:
                    raise SoloAIError(
                        "In-place tasks always use the current checked-out branch"
                    )
                if not session_id:
                    raise SoloAIError(
                        "In-place tasks require --session from the trusted Codex hook"
                    )
                try:
                    owner = store.task_for_worktree(repo.root)
                except SoloAIError:
                    owner = None
                if owner and StateStore.mode(owner) == ISOLATED_MODE:
                    raise SoloAIError(
                        "Cannot start an in-place task inside an active isolated task worktree"
                    )
                branch = repo.branch(repo.root)
                if branch is None:
                    raise SoloAIError("In-place tasks require an attached local branch")
                if not repo.is_clean(repo.root):
                    raise SoloAIError(
                        "In-place Start requires a clean Git worktree; ignored test data may remain, but tracked or untracked changes must be preserved and handled first"
                    )
                return store.allocate_in_place(
                    name=name,
                    branch=branch,
                    head=repo.head(repo.root),
                    base_worktree=repo.root,
                    session_id=session_id,
                )
        base_ref, base_head, base_worktree = _resolve_start_base(repo, store, base)
        branch = f"{config.branch_prefix}{safe_slug(name)}-{uuid.uuid4().hex[:6]}"
        task = store.allocate(
            config,
            name=name,
            branch=branch,
            base_head=base_head,
            base_ref=base_ref,
            base_worktree=base_worktree,
        )
        worktree = ensure_within(
            Path(task["worktree"]), repo.primary_path / config.worktree_directory
        )
        managed_root = worktree.absolute().parent
        try:
            activation_worktree_identity: dict[str, object]
            activation_root_identity: dict[str, object]
            registered = next(
                (item for item in repo.worktrees() if item.path == worktree), None
            )
            if registered is None:
                if worktree.exists() and any(worktree.iterdir()):
                    raise SoloAIError(f"Unregistered non-empty slot path: {worktree}")
                repo.git(["worktree", "add", "--detach", str(worktree), base_ref])
                activation_worktree_identity = path_identity(worktree)
                activation_root_identity = path_identity(managed_root)
            else:
                require_managed_directory_identity(
                    worktree,
                    managed_root=managed_root,
                    expected_resolved=task.get("slot_worktree_resolved"),
                    expected_root_resolved=task.get("slot_managed_root_resolved"),
                    expected_identity=task.get("slot_worktree_identity"),
                    expected_root_identity=task.get("slot_managed_root_identity"),
                )
                if not repo.is_clean(worktree):
                    raise SoloAIError(f"Idle slot is not clean: {worktree}")
                if unknown := _unknown_ignored(repo, worktree):
                    raise SoloAIError(
                        "Idle slot contains protected or unknown ignored content:\n"
                        + "\n".join(f"- {item}" for item in unknown[:20])
                    )
                if repo.branch(worktree) is not None:
                    raise SoloAIError(
                        f"Idle slot is unexpectedly attached to a branch: {worktree}"
                    )
                activation_worktree_identity = path_identity(worktree)
                activation_root_identity = path_identity(managed_root)
                repo.git(["reset", "--hard", base_ref], cwd=worktree)
            repo.git(["switch", "-c", branch, base_ref], cwd=worktree)
            resolved = require_managed_directory_identity(
                worktree,
                managed_root=managed_root,
                expected_resolved=str(worktree.resolve()),
                expected_root_resolved=str(managed_root.resolve()),
                expected_identity=activation_worktree_identity,
                expected_root_identity=activation_root_identity,
            )
            if not repo.is_clean(worktree) or repo.branch(worktree) != branch:
                raise SoloAIError("Slot changed while Start was activating it")
            if unknown := _unknown_ignored(repo, worktree):
                raise SoloAIError(
                    "Slot received protected or unknown ignored content during Start:\n"
                    + "\n".join(f"- {item}" for item in unknown[:20])
                )
            return store.update_task(
                task["id"],
                status="active",
                candidate_head=repo.head(worktree),
                baseline_paths=repo.changed_paths(worktree),
                slot_worktree_identity=activation_worktree_identity,
                slot_managed_root_identity=activation_root_identity,
                slot_worktree_resolved=str(resolved),
                slot_managed_root_resolved=str(managed_root.resolve()),
            )
        except Exception as exc:
            store.quarantine(task["id"], str(exc))
            raise


def _path_is_safe(path: str) -> bool:
    candidate = Path(path)
    return (
        not candidate.is_absolute()
        and ".." not in candidate.parts
        and path not in {"", "."}
    )


def _is_in_place(task: dict[str, Any]) -> bool:
    return StateStore.mode(task) == IN_PLACE_MODE


def _verification_base(task: dict[str, Any]) -> str:
    """直改分支会前进，验证必须始终相对不可变的起点。"""
    return str(task.get("start_head") if _is_in_place(task) else task["base_ref"])


def _assert_in_place_binding(
    repo: GitRepo,
    store: StateStore,
    task: dict[str, Any],
    *,
    session_id: str | None,
) -> None:
    """拒绝会话、工作树、分支或 HEAD 漂移；只隔离现场，绝不回滚。"""
    if task.get("status") not in {"active", "ready"}:
        raise SoloAIError(
            "In-place task is not active. Preserve the current worktree and use resume-in-place only after manually restoring its recorded identity."
        )
    failures: list[str] = []
    worktree = Path(str(task["worktree"])).resolve()
    if repo.root.resolve() != worktree:
        failures.append("invocation worktree changed")
    if not session_id or sha256_text(session_id) != task.get("session_fingerprint"):
        failures.append("Codex session changed")
    if repo.branch(worktree) != task.get("branch"):
        failures.append("checked-out branch changed")
    if repo.head(worktree) != task.get("expected_head"):
        failures.append("HEAD changed outside exact-path dww commit")
    if failures:
        reason = "; ".join(failures)
        store.quarantine(task["id"], reason)
        raise SoloAIError(
            "In-place task was quarantined; files were preserved without rollback: "
            + reason
            + ". Restore the recorded branch and expected HEAD manually, then use resume-in-place."
        )


def _assert_no_in_place_integration_conflict(
    store: StateStore, task: dict[str, Any]
) -> None:
    """直改会话绑定当前分支；任何其他任务都不能在其完成前推进该基线。"""
    blocker = store.active_in_place()
    if not blocker or blocker.get("id") == task.get("id"):
        return
    if Path(str(blocker.get("base_worktree"))).resolve() == Path(
        str(task.get("base_worktree"))
    ).resolve() and blocker.get("base_ref") == task.get("base_ref"):
        raise SoloAIError(
            "An in-place task is active on this base branch. Its current-worktree identity must remain stable, so this isolated task cannot Finish yet. Finish, abandon, or explicitly resume the in-place task first."
        )


def _run_declared_secret_scanner(
    repo: GitRepo, *, cwd: Path, scanner: CommandSpec | None
) -> None:
    if scanner is None:
        return
    pending = (
        repo.local_dir / "logs" / "pending" / f"secret-scan-{uuid.uuid4().hex}.log"
    )
    result = run_logged(scanner.argv, cwd=cwd, log_path=pending)
    if result.returncode:
        raise SoloAIError(
            f"Repository-declared secret scanner failed. Review its local redacted log: {pending}"
        )


def commit_task(
    repo: GitRepo,
    *,
    task_id: str,
    lease: str,
    message: str,
    paths: list[str],
    session_id: str | None = None,
) -> dict[str, Any]:
    config, _, _ = _config_and_mode(repo)
    store = StateStore(repo)
    with store.operation(task_id, lease, "commit") as task:
        worktree = Path(task["worktree"])
        if _is_in_place(task):
            if task.get("status") not in {"active", "ready"}:
                raise SoloAIError("In-place Commit requires an active or ready task")
            _assert_in_place_binding(repo, store, task, session_id=session_id)
        if repo.branch(worktree) != task["branch"]:
            raise SoloAIError(
                "Task branch identity no longer matches its recorded task"
            )
        if not paths:
            raise SoloAIError(
                "Commit requires one or more exact --path values; inspect the task diff before staging"
            )
        if len(paths) != len(set(paths)) or any(
            not _path_is_safe(path) for path in paths
        ):
            raise SoloAIError(
                "Commit paths must be unique, repository-relative exact paths"
            )
        changed = set(repo.changed_paths(worktree))
        requested = set(paths)
        if changed != requested:
            missing = sorted(changed - requested)
            extra = sorted(requested - changed)
            detail = [
                *(f"unstaged or unreviewed: {item}" for item in missing),
                *(f"not changed: {item}" for item in extra),
            ]
            raise SoloAIError(
                "Exact staging manifest does not match task changes:\n"
                + "\n".join(detail)
            )
        # 先更新所有已跟踪路径的删除和修改；前置精确清单检查保证它们都已审阅。
        repo.git(["add", "-u"], cwd=worktree)
        existing_paths = [
            path
            for path in paths
            if (worktree / path).exists() or (worktree / path).is_symlink()
        ]
        if existing_paths:
            repo.git(["add", "--", *existing_paths], cwd=worktree)
        if set(repo.changed_paths(worktree)) != requested:
            raise SoloAIError(
                "Task changes changed while staging; inspect the task diff and retry"
            )
        _run_declared_secret_scanner(repo, cwd=worktree, scanner=config.secret_scanner)
        require_safe(
            repo,
            cwd=worktree,
            base=None,
            staged=True,
            allowlist=config.sensitive_allowlist,
        )
        repo.git(["commit", "-m", message, "--", *paths], cwd=worktree)
        changes: dict[str, Any] = {
            "candidate_head": repo.head(worktree),
            "ready_proof": None,
            "status": "active",
        }
        if _is_in_place(task):
            changes["expected_head"] = changes["candidate_head"]
        updated = store.update_task(task_id, **changes)
        overlaps: list[dict[str, Any]] = []
        if _is_in_place(task):
            requested = set(paths)
            for other in store.read()["tasks"].values():
                if (
                    other.get("id") == task_id
                    or StateStore.mode(other) != ISOLATED_MODE
                    or other.get("status") in FINAL_TASK_STATES
                ):
                    continue
                other_paths = set(repo.changed_paths(Path(str(other["worktree"]))))
                shared = sorted(requested & other_paths)
                if shared:
                    overlaps.append({"task_id": other["id"], "paths": shared})
        return {**updated, "overlaps": overlaps}


def _sync_base(repo: GitRepo, task: dict[str, Any]) -> dict[str, Any]:
    worktree = Path(task["worktree"])
    base_ref = str(task["base_ref"])
    current = repo.git(
        ["rev-parse", "--verify", f"refs/heads/{base_ref}"],
        cwd=worktree,
        check=False,
    )
    if current.returncode != 0:
        raise SoloAIError(
            f"Recorded base branch {base_ref!r} no longer exists; explicitly retarget this task before Ready or Finish"
        )
    base_head = current.stdout.strip()
    recorded_head = str(task["base_head"])
    if not repo.is_ancestor(recorded_head, base_head, cwd=worktree):
        raise SoloAIError(
            f"Recorded base branch {base_ref!r} was rewritten or moved backward; explicitly retarget this task before Ready or Finish"
        )
    if not repo.is_ancestor(base_head, "HEAD", cwd=worktree):
        prediction = repo.git(
            ["merge-tree", "--write-tree", base_ref, "HEAD"],
            cwd=worktree,
            check=False,
        )
        if prediction.returncode != 0:
            raise SoloAIError(
                "Read-only merge prediction found a conflict. Resolve it in this task worktree; no automatic semantic merge was attempted."
            )
        repo.git(["merge", "--no-edit", base_ref], cwd=worktree)
    task["candidate_head"] = repo.head(worktree)
    task["base_head"] = base_head
    return task


def _recorded_base_worktree(repo: GitRepo, task: dict[str, Any]) -> Path:
    value = task.get("base_worktree")
    if not value:
        raise SoloAIError(
            "Task has no recorded base worktree; recover it by explicitly retargeting before Finish"
        )
    path = Path(str(value)).resolve()
    if not any(item.path == path for item in repo.worktrees()):
        raise SoloAIError(
            "Recorded base worktree no longer exists; explicitly retarget"
        )
    if repo.branch(path) != task.get("base_ref"):
        raise SoloAIError(
            "Recorded base worktree no longer has the recorded base branch"
        )
    if not repo.is_clean(path):
        raise SoloAIError("Recorded base worktree must be clean before integration")
    return path


def retarget(
    repo: GitRepo,
    *,
    task_id: str,
    lease: str,
    base: str,
    confirm: str,
) -> dict[str, Any]:
    """在用户显式确认后重绑基线；不替用户改写历史或猜测合并策略。"""
    expected = f"{task_id}:{base}"
    if confirm != expected:
        raise SoloAIError(f"Retarget requires --confirm {expected!r}")
    _, _, _ = _config_and_mode(repo)
    store = StateStore(repo)
    with store.operation(task_id, lease, "retarget") as task:
        if _is_in_place(task):
            raise SoloAIError(
                "In-place tasks keep their original branch and start point; use resume-in-place only after manually restoring that identity"
            )
        if task.get("status") not in {"active", "ready"}:
            raise SoloAIError("Only active or ready tasks can be retargeted")
        worktree = Path(str(task["worktree"]))
        if not repo.is_clean(worktree):
            raise SoloAIError("Commit task changes before retargeting its base")
        exists = repo.git(
            ["show-ref", "--verify", "--quiet", f"refs/heads/{base}"],
            check=False,
        )
        if exists.returncode != 0:
            raise SoloAIError(f"Base branch does not exist locally: {base}")
        base_worktree = _checked_out_branch_worktree(repo, base)
        active_paths = {
            Path(str(item["worktree"])).resolve()
            for item in store.read()["tasks"].values()
            if item.get("id") != task_id and item.get("status") not in FINAL_TASK_STATES
        }
        if base_worktree.resolve() in active_paths:
            raise SoloAIError(
                "New base worktree is owned by another active managed task"
            )
        base_head = repo.git(["rev-parse", base], cwd=base_worktree).stdout.strip()
        if not repo.is_ancestor(base_head, "HEAD", cwd=worktree):
            raise SoloAIError(
                "The task does not yet contain the chosen base. Resolve or merge it manually, then retry retarget; history is never rewritten automatically."
            )
        return store.update_task(
            task_id,
            base_ref=base,
            base_head=base_head,
            base_worktree=str(base_worktree.resolve()),
            status="active",
            ready_proof=None,
        )


def ready(
    repo: GitRepo, *, task_id: str, lease: str, session_id: str | None = None
) -> dict[str, Any]:
    _, _, _ = _config_and_mode(repo)
    store = StateStore(repo)
    with store.operation(task_id, lease, "ready") as task:
        worktree = Path(task["worktree"])
        if task.get("status") not in {"active", "ready"}:
            raise SoloAIError(f"Task cannot enter Ready from {task.get('status')}")
        if _is_in_place(task):
            _assert_in_place_binding(repo, store, task, session_id=session_id)
        if not repo.is_clean(worktree):
            raise SoloAIError("Commit all task changes before Ready")
        convergence_retries = 0
        while True:
            if not _is_in_place(task):
                task = _sync_base(repo, task)
            if not repo.is_clean(worktree):
                raise SoloAIError("Base-branch synchronization left the task dirty")
            expected_candidate_head = repo.head(worktree)
            _assert_exact_candidate(
                repo, task, candidate_head=expected_candidate_head
            )
            # 每次同步都可能带入新的受管策略；必须按本轮候选重新确认和验证。
            config = load_repo_config(repo, cwd=worktree)
            store.require_slot_layout(config)
            verification = load_verification_config(repo, cwd=worktree)
            require_approval(repo, verification, cwd=worktree)
            base_ref = _verification_base(task)
            _run_declared_secret_scanner(
                repo, cwd=worktree, scanner=config.secret_scanner
            )
            _assert_exact_candidate(
                repo, task, candidate_head=expected_candidate_head
            )
            require_safe(
                repo, cwd=worktree, base=base_ref, allowlist=config.sensitive_allowlist
            )
            _assert_exact_candidate(
                repo, task, candidate_head=expected_candidate_head
            )
            expected_base_head = None if _is_in_place(task) else str(task["base_head"])
            try:
                proof = validate(
                    repo,
                    cwd=worktree,
                    base=base_ref,
                    verification=verification,
                    task_id=task_id,
                    force_task_scope=_is_in_place(task),
                    expected_base_head=expected_base_head,
                    expected_candidate_head=expected_candidate_head,
                )
            except ValidationBaseChanged as exc:
                convergence_retries += 1
                if convergence_retries > MAX_READY_CONVERGENCE_RETRIES:
                    raise SoloAIError(
                        "Ready could not converge because the base kept advancing; "
                        "the task is preserved and can be retried after integration activity settles"
                    ) from exc
                continue

            if not _is_in_place(task):
                current_base_head = repo.git(
                    ["rev-parse", "--verify", f"refs/heads/{task['base_ref']}"],
                    cwd=worktree,
                ).stdout.strip()
                if current_base_head != task["base_head"]:
                    convergence_retries += 1
                    if convergence_retries > MAX_READY_CONVERGENCE_RETRIES:
                        raise SoloAIError(
                            "Ready could not converge because the base kept advancing; "
                            "the task is preserved and can be retried after integration activity settles"
                        )
                    continue

            updates: dict[str, Any] = {
                "status": "ready",
                "candidate_head": expected_candidate_head,
                "ready_proof": proof["fingerprint"],
            }
            if not _is_in_place(task):
                updates["base_head"] = task["base_head"]
            updated = store.update_task(task_id, **updates)
            return {**updated, "convergence_retries": convergence_retries}


def _unknown_ignored(repo: GitRepo, worktree: Path) -> list[str]:
    known_roots = {
        ".venv",
        "node_modules",
        ".tmp",
        ".cache",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
    }
    unknown: list[str] = []
    inventory = inspect_untracked(repo, cwd=worktree)
    protected = set(inventory["protected"])
    for item in repo.ignored_untracked(worktree):
        parts = Path(item).parts
        if item in protected or Path(item).name.casefold() == ".env" or Path(
            item
        ).name.casefold().startswith(".env.") or (
            not any(part in known_roots for part in parts)
            and Path(item).name != "uv.toml"
        ):
            unknown.append(item)
    return unknown


def _assert_removable_managed_slot(repo: GitRepo, path: Path) -> bool:
    """只允许删除干净、已登记且没有受保护忽略内容的受管槽位。"""
    if not path.exists():
        return False
    if not any(item.path == path for item in repo.worktrees()):
        raise SoloAIError(
            f"Managed slot path is no longer registered with Git and is retained: {path}"
        )
    if not repo.is_clean(path):
        raise SoloAIError(f"Dirty managed slot blocks removal: {path}")
    if unknown := _unknown_ignored(repo, path):
        raise SoloAIError(
            "Unknown or protected files block removal of managed slot:\n"
            + "\n".join(f"- {item}" for item in unknown[:20])
        )
    return True


def _preflight_deinit_slots(
    repo: GitRepo, *, config: Any, state: dict[str, Any]
) -> list[Path]:
    """在写入任何策略清理提交前验证全部槽位，避免半卸载。"""
    removable: list[Path] = []
    for slot in state["slots"].values():
        path = ensure_within(
            Path(slot["path"]), repo.primary_path / config.worktree_directory
        )
        if _assert_removable_managed_slot(repo, path):
            removable.append(path)
    return removable


def _restore_removed_slots(
    repo: GitRepo, *, primary: Path, base: str, removed: list[Path]
) -> list[str]:
    """仅在后续释放失败时恢复本轮已删除的干净槽位。"""
    failures: list[str] = []
    for path in reversed(removed):
        if path.exists():
            failures.append(f"slot path was recreated externally: {path}")
            continue
        result = repo.git(
            ["worktree", "add", "--detach", str(path), base],
            cwd=primary,
            check=False,
        )
        if result.returncode:
            failures.append(f"could not restore {path}: {result.stderr.strip()}")
    return failures


def _process_has_exited(process: psutil.Process) -> bool:
    try:
        return not process.is_running() or process.status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def _stop_unix_process_group(root: psutil.Process) -> bool:
    """停止由本插件创建的 Unix 会话，避免依赖 psutil 的进程回收语义。"""
    try:
        os.killpg(root.pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if _process_has_exited(root):
            return True
        time.sleep(0.1)
    try:
        os.killpg(root.pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if _process_has_exited(root):
            return True
        time.sleep(0.1)
    return _process_has_exited(root)


def _stop_registered_processes(store: StateStore, task: dict[str, Any]) -> None:
    for snapshot in task.get("processes", []):
        if not process_matches(snapshot):
            raise SoloAIError(
                f"Registered process identity changed or is unknown: PID {snapshot.get('pid')}"
            )
        root = psutil.Process(snapshot["pid"])
        if os.name != "nt" and snapshot.get("role") == "command":
            if not _stop_unix_process_group(root):
                raise SoloAIError(
                    "Owned development process did not stop; task remains preserved"
                )
            continue
        processes = [*root.children(recursive=True), root]
        for process in reversed(processes):
            try:
                process.terminate()
            except psutil.NoSuchProcess:
                continue
        _, alive = psutil.wait_procs(processes, timeout=10)
        if alive:
            raise SoloAIError(
                "Owned development process did not stop; task remains preserved"
            )
    if task.get("processes"):
        store.update_task(task["id"], processes=[])


def _integrate_pending_bootstrap(repo: GitRepo, primary: Path) -> dict[str, str] | None:
    pending = _bootstrap(repo)
    if not pending:
        return None
    branch = str(pending["branch"])
    repo.git(["merge", "--ff-only", branch], cwd=primary)
    target_ref = repo.branch(primary)
    if target_ref is None:
        raise SoloAIError("Primary worktree detached while integrating bootstrap")
    result = {
        "bootstrap_branch": branch,
        "base_ref": target_ref,
        "base_head": repo.head(primary),
        "base_worktree": str(primary.resolve()),
    }
    worktree = Path(str(pending["worktree"]))
    if worktree.exists():
        repo.git(["worktree", "remove", str(worktree)], cwd=primary)
    repo.git(["branch", "-d", branch], cwd=primary)
    (repo.local_dir / "bootstrap.json").unlink(missing_ok=True)
    return result


def _in_place_receipt_path(repo: GitRepo, task_id: str) -> Path:
    return repo.local_dir / "in-place-receipts" / f"{task_id}.json"


def _write_in_place_receipt(repo: GitRepo, receipt: dict[str, Any]) -> None:
    receipt["updated_at"] = utc_timestamp()
    atomic_write_json(_in_place_receipt_path(repo, str(receipt["task_id"])), receipt)


def _validate_in_place_receipt(
    repo: GitRepo, *, task: dict[str, Any], receipt: dict[str, Any]
) -> None:
    if receipt.get("schema_version") != 1 or receipt.get("stage") not in {
        "completed",
        "released",
    }:
        raise SoloAIError("Unsupported in-place completion receipt")
    expected = {
        "task_id": task["id"],
        "mode": IN_PLACE_MODE,
        "branch": task["branch"],
        "head": task.get("expected_head"),
        "start_head": task.get("start_head"),
        "proof": task.get("ready_proof"),
    }
    for key, value in expected.items():
        if not value or receipt.get(key) != value:
            raise SoloAIError(
                f"In-place completion receipt does not match task state: {key}"
            )
    proof = read_json(repo.local_dir / "proofs" / f"{receipt['proof']}.json", {})
    require_exact_passed_proof(
        proof,
        fingerprint=str(receipt["proof"]),
        candidate_head=str(receipt["head"]),
        base_head=str(receipt["start_head"]),
    )
    if proof.get("kind") != receipt.get("proof_kind"):
        raise SoloAIError("In-place completion receipt proof kind changed")


def _finish_in_place(
    repo: GitRepo,
    *,
    store: StateStore,
    task: dict[str, Any],
    lease: str,
    session_id: str | None,
) -> dict[str, Any]:
    """完成当前工作树任务；不合并、不切换、不删除分支或测试数据。"""
    if task.get("status") != "ready":
        raise SoloAIError("Finish requires a successful Ready")
    _assert_in_place_binding(repo, store, task, session_id=session_id)
    worktree = Path(str(task["worktree"]))
    if not repo.is_clean(worktree) or repo.head(worktree) != task.get("candidate_head"):
        raise SoloAIError(
            "Candidate changed after Ready; commit exact paths and run Ready again"
        )
    receipt = read_json(_in_place_receipt_path(repo, task["id"]), {})
    if receipt:
        _validate_in_place_receipt(repo, task=task, receipt=receipt)
        store.release(task["id"], final_status="finished")
        receipt["stage"] = "released"
        _write_in_place_receipt(repo, receipt)
        return {
            "task_id": task["id"],
            "integrated_head": receipt["head"],
            "proof": receipt["proof"],
            "proof_kind": receipt["proof_kind"],
            "proof_reused": bool(receipt.get("proof_reused", False)),
            "mode": IN_PLACE_MODE,
        }
    config = load_repo_config(repo, cwd=worktree)
    store.require_slot_layout(config)
    verification = load_verification_config(repo, cwd=worktree)
    require_approval(repo, verification, cwd=worktree)
    _run_declared_secret_scanner(repo, cwd=worktree, scanner=config.secret_scanner)
    _assert_in_place_binding(repo, store, task, session_id=session_id)
    require_safe(
        repo,
        cwd=worktree,
        base=_verification_base(task),
        allowlist=config.sensitive_allowlist,
    )
    _assert_in_place_binding(repo, store, task, session_id=session_id)
    proof = validate(
        repo,
        cwd=worktree,
        base=_verification_base(task),
        verification=verification,
        task_id=task["id"],
        force_task_scope=True,
        expected_candidate_head=str(task["candidate_head"]),
    )
    _assert_in_place_binding(repo, store, task, session_id=session_id)
    if not repo.is_clean(worktree):
        raise SoloAIError(
            "In-place validation left tracked or nonignored changes. They were preserved; commit exact paths and run Ready again before Finish."
        )
    receipt = {
        "schema_version": 1,
        "task_id": task["id"],
        "mode": IN_PLACE_MODE,
        "branch": task["branch"],
        "start_head": task["start_head"],
        "head": task["expected_head"],
        "proof": proof["fingerprint"],
        "proof_kind": proof["kind"],
        "proof_reused": proof.get("reused", False),
        "stage": "completed",
        "created_at": utc_timestamp(),
    }
    _write_in_place_receipt(repo, receipt)
    store.release(task["id"], final_status="finished")
    receipt["stage"] = "released"
    _write_in_place_receipt(repo, receipt)
    return {
        "task_id": task["id"],
        "integrated_head": task["expected_head"],
        "proof": proof["fingerprint"],
        "proof_kind": proof["kind"],
        "proof_reused": proof.get("reused", False),
        "mode": IN_PLACE_MODE,
    }


def _assert_exact_candidate(
    repo: GitRepo, task: dict[str, Any], *, candidate_head: str
) -> None:
    worktree = Path(str(task["worktree"]))
    if not any(item.path == worktree.resolve() for item in repo.worktrees()):
        raise SoloAIError("Task worktree is no longer registered")
    if not repo.is_clean(worktree) or repo.head(worktree) != candidate_head:
        raise SoloAIError("Candidate changed during Finish; run Ready again")
    if repo.branch(worktree) != task.get("branch"):
        raise SoloAIError("Task worktree changed branch during Finish")
    branch_head = repo.ref_head(f"refs/heads/{task['branch']}")
    if branch_head != candidate_head:
        raise SoloAIError("Task branch changed during Finish")


def finish(
    repo: GitRepo, *, task_id: str, lease: str, session_id: str | None = None
) -> dict[str, Any]:
    _, _, _ = _config_and_mode(repo)
    store = StateStore(repo)
    with store.operation(task_id, lease, "finish") as active_task:
        if _is_in_place(active_task):
            return _finish_in_place(
                repo,
                store=store,
                task=active_task,
                lease=lease,
                session_id=session_id,
            )
        if active_task.get("status") not in {"ready", "finishing"}:
            raise SoloAIError("Finish requires a successful Ready")
        with maintenance_lock(repo), integration_turn(repo, task_id):
            pending = _bootstrap(repo)
            if pending:
                primary, _ = repo.ensure_default_primary_clean()
                bootstrap_result = _integrate_pending_bootstrap(repo, primary)
                task = store.task(task_id)
                if (
                    bootstrap_result
                    and task.get("base_ref") == bootstrap_result["bootstrap_branch"]
                ):
                    task = store.update_task(
                        task_id,
                        base_ref=bootstrap_result["base_ref"],
                        base_head=bootstrap_result["base_head"],
                        base_worktree=bootstrap_result["base_worktree"],
                        ready_proof=None,
                    )
            task = store.task(task_id)
            store.require_lease(task, lease)
            _assert_no_in_place_integration_conflict(store, task)
            if task.get("integration"):
                return resume_integration(
                    repo, store=store, task=task, allow_stale=False
                )
            if task.get("status") != "ready":
                raise SoloAIError("Finish requires a successful Ready")
            _recorded_base_worktree(repo, task)
            worktree = Path(str(task["worktree"]))
            _assert_exact_candidate(
                repo, task, candidate_head=str(task["candidate_head"])
            )
            task = _sync_base(repo, task)
            candidate_head = repo.head(worktree)
            task = store.update_task(
                task_id,
                candidate_head=candidate_head,
                base_head=task["base_head"],
                ready_proof=None,
            )
            _assert_exact_candidate(repo, task, candidate_head=candidate_head)
            # 候选必须在任何策略、敏感内容和验证门禁之前冻结。
            config = load_repo_config(repo, cwd=worktree)
            store.require_slot_layout(config)
            verification = load_verification_config(repo, cwd=worktree)
            require_approval(repo, verification, cwd=worktree)
            _run_declared_secret_scanner(
                repo, cwd=worktree, scanner=config.secret_scanner
            )
            _assert_exact_candidate(repo, task, candidate_head=candidate_head)
            require_safe(
                repo,
                cwd=worktree,
                base=str(task["base_ref"]),
                allowlist=config.sensitive_allowlist,
            )
            _assert_exact_candidate(repo, task, candidate_head=candidate_head)
            proof = validate(
                repo,
                cwd=worktree,
                base=str(task["base_ref"]),
                verification=verification,
                task_id=task_id,
                expected_base_head=str(task["base_head"]),
                expected_candidate_head=candidate_head,
            )
            _assert_exact_candidate(repo, task, candidate_head=candidate_head)
            if unknown := _unknown_ignored(repo, worktree):
                raise SoloAIError(
                    "Unknown or protected ignored files block slot release:\n"
                    + "\n".join(f"- {item}" for item in unknown[:20])
                )
            _stop_registered_processes(store, task)
            _assert_exact_candidate(repo, task, candidate_head=candidate_head)
            task = store.update_task(
                task_id,
                candidate_head=candidate_head,
                base_head=task["base_head"],
                ready_proof=proof["fingerprint"],
            )
            prepared = prepare_integration(repo, store=store, task=task, proof=proof)
            return resume_integration(
                repo, store=store, task=prepared, allow_stale=False
            )


def recover(repo: GitRepo, *, task_id: str) -> dict[str, Any]:
    """根据持久化事务和 Git 事实恢复；失败时不轮换租约或改变现场。"""
    _, _, _ = _config_and_mode(repo)
    store = StateStore(repo)
    store.reconcile_operation_receipts()
    task = store.task(task_id)
    if _is_in_place(task):
        receipt = read_json(_in_place_receipt_path(repo, task_id), {})
        if task.get("status") == "finished" and receipt.get("stage") in {
            "completed",
            "released",
        }:
            _validate_in_place_receipt(repo, task=task, receipt=receipt)
            receipt["stage"] = "released"
            _write_in_place_receipt(repo, receipt)
            return {"id": task_id, "status": "completed", "mode": IN_PLACE_MODE}
        raise SoloAIError(
            "In-place tasks require resume-in-place; ordinary recovery cannot change their binding"
        )
    active = task.get("active_operation") or {}
    if active and process_matches(active.get("owner", {})):
        raise SoloAIError("Task still has a live operation; recovery is unsafe")
    transaction = task.get("integration") or {}
    if task.get("status") == "finished" and transaction.get("phase") == "completed":
        slot = store.read()["slots"][task["slot_id"]]
        if slot.get("task_id") == task_id and slot.get("status") == "release-checking":
            with maintenance_lock(repo), integration_turn(repo, task_id):
                return resume_integration(
                    repo, store=store, task=store.task(task_id), allow_stale=False
                )
        receipt = write_completed_receipt(repo, task)
        return {
            "id": task_id,
            "status": "completed",
            "transaction_id": receipt["transaction_id"],
            "candidate_head": receipt["candidate_head"],
        }
    abandonment = task.get("abandonment") or {}
    if task.get("status") == "abandoned" and abandonment.get("phase") == "completed":
        slot = store.read()["slots"][task["slot_id"]]
        if slot.get("task_id") == task_id and slot.get("status") == "release-checking":
            with maintenance_lock(repo), integration_turn(repo, task_id):
                return resume_abandonment(repo, store=store, task=store.task(task_id))
        receipt = write_abandonment_receipt(repo, task)
        return {
            "id": task_id,
            "status": "abandoned",
            "transaction_id": receipt["transaction_id"],
        }
    if task.get("status") == "finished":
        with integration_turn(repo, task_id):
            migrated = migrate_legacy_receipt(repo, store=store, task=store.task(task_id))
            if not migrated:
                raise SoloAIError(f"Task cannot be recovered: {task_id}")
            receipt = write_completed_receipt(repo, migrated)
            return {
                "id": task_id,
                "status": "completed",
                "transaction_id": receipt["transaction_id"],
                "candidate_head": receipt["candidate_head"],
            }
    with store.recovery_operation(task_id) as recovery_task:
        recovery_operation_id = str(recovery_task["active_operation"]["id"])
        with maintenance_lock(repo), integration_turn(repo, task_id):
            task = store.task(task_id)
            active = task.get("active_operation") or {}
            if active.get("id") != recovery_operation_id:
                raise SoloAIError("Task recovery operation identity changed while waiting")
            if task.get("abandonment"):
                _stop_registered_processes(store, task)
                return resume_abandonment(repo, store=store, task=store.task(task_id))
            integration = task.get("integration")
            if not integration:
                migrated = migrate_legacy_receipt(repo, store=store, task=task)
                if migrated:
                    task = migrated
                    integration = task.get("integration")
            if not integration and task.get("status") == "ready" and task.get(
                "candidate_head"
            ):
                candidate = str(task["candidate_head"])
                base_head = repo.ref_head(f"refs/heads/{task['base_ref']}")
                if base_head and repo.is_ancestor(candidate, base_head):
                    proof = read_json(
                        repo.local_dir / "proofs" / f"{task['ready_proof']}.json", {}
                    )
                    require_exact_passed_proof(
                        proof,
                        fingerprint=str(task["ready_proof"]),
                        candidate_head=candidate,
                        base_head=str(task["base_head"]),
                    )
                    transaction = legacy_integration_transaction(task, proof=proof)
                    task = store.prepare_integration(
                        task_id, operation_id=None, integration=transaction
                    )
                    integration = task["integration"]
            if integration:
                result = resume_integration(
                    repo, store=store, task=task, allow_stale=True
                )
                if result.get("status") == "active":
                    return store.recover(
                        task_id, operation_id=recovery_operation_id
                    )
                return result
            if task.get("status") in FINAL_TASK_STATES:
                raise SoloAIError(f"Task cannot be recovered: {task_id}")
            worktree = Path(str(task["worktree"]))
            if not worktree.is_dir() or not any(
                item.path == worktree.resolve() for item in repo.worktrees()
            ):
                raise SoloAIError("Task worktree is missing or unregistered; preserve state")
            if not repo.is_clean(worktree):
                raise SoloAIError("Dirty task worktree blocks recovery")
            branch_head = repo.ref_head(f"refs/heads/{task['branch']}")
            current_branch = repo.branch(worktree)
            current_head = repo.head(worktree)
            if current_branch not in {None, task["branch"]}:
                raise SoloAIError("Task worktree is on another branch; preserve it")
            if branch_head is None:
                if current_branch is not None or current_head != task.get("base_head"):
                    raise SoloAIError(
                        "Missing task branch can only be rebuilt at the exact recorded base"
                    )
                repo.git(["switch", "-c", task["branch"], task["base_head"]], cwd=worktree)
            elif current_branch is None:
                if current_head != branch_head:
                    raise SoloAIError("Detached task HEAD differs from its branch; preserve it")
                repo.git(["switch", task["branch"]], cwd=worktree)
            elif current_head != branch_head:
                raise SoloAIError("Task branch and worktree HEAD differ; preserve them")
            return store.recover(task_id, operation_id=recovery_operation_id)


def resume_in_place(
    repo: GitRepo,
    *,
    task_id: str,
    session_id: str,
    confirm: str,
) -> dict[str, Any]:
    """显式接管未漂移的直改任务；绝不接纳外部 HEAD。"""
    _, _, _ = _config_and_mode(repo)
    store = StateStore(repo)
    task = store.task(task_id)
    if not _is_in_place(task):
        raise SoloAIError("resume-in-place only applies to an in-place task")
    expected = f"{task_id}:{task['branch']}:{task['expected_head']}"
    if confirm != expected:
        raise SoloAIError(f"resume-in-place requires --confirm {expected!r}")
    worktree = Path(str(task["worktree"])).resolve()
    if repo.root.resolve() != worktree:
        raise SoloAIError("Run resume-in-place from the recorded in-place worktree")
    if repo.branch(worktree) != task.get("branch") or repo.head(worktree) != task.get(
        "expected_head"
    ):
        raise SoloAIError(
            "In-place branch or HEAD still differs from the recorded identity. Files were preserved; inspect and restore it manually before resuming."
        )
    return store.resume_in_place(task_id, session_id=session_id)


def abandon(
    repo: GitRepo,
    *,
    task_id: str,
    lease: str,
    confirm: str,
    session_id: str | None = None,
) -> dict[str, Any]:
    if confirm != task_id:
        raise SoloAIError("Abandon requires --confirm with the exact task id")
    config, _, _ = _config_and_mode(repo)
    store = StateStore(repo)
    with store.operation(task_id, lease, "abandon") as task:
        if _is_in_place(task):
            _assert_in_place_binding(repo, store, task, session_id=session_id)
            worktree = Path(str(task["worktree"]))
            if not repo.is_clean(worktree):
                raise SoloAIError(
                    "In-place abandon never resets or cleans the current worktree. Commit exact paths and Finish, or preserve and handle the changes manually."
                )
            store.release(task_id, final_status="abandoned")
            return {
                "task_id": task_id,
                "status": "abandoned",
                "mode": IN_PLACE_MODE,
                "preserved": True,
            }
        with maintenance_lock(repo), integration_turn(repo, task_id):
            task = store.task(task_id)
            store.require_lease(task, lease)
            if task.get("integration"):
                raise SoloAIError(
                    "An integration transaction exists; Recover must resolve it before Abandon"
                )
            if task.get("abandonment"):
                return resume_abandonment(repo, store=store, task=task)
            ensure_within(
                Path(task["worktree"]),
                repo.primary_path / config.worktree_directory,
            )
            _stop_registered_processes(store, task)
            task = store.task(task_id)
            prepared = prepare_abandonment(repo, store=store, task=task)
            return resume_abandonment(repo, store=store, task=prepared)


def _port_free(port: int) -> bool:
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _format_argv(command: CommandSpec, *, port: int, slot: str) -> list[str]:
    return [
        item.replace("{port}", str(port)).replace("{slot}", slot)
        for item in command.argv
    ]


def _ready(kind: str, target: str | None, *, port: int) -> bool:
    rendered = (target or "").replace("{port}", str(port))
    try:
        if kind == "tcp":
            host, _, raw_port = rendered.partition(":")
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
                connection.settimeout(0.2)
                return connection.connect_ex((host or "127.0.0.1", int(raw_port))) in {
                    0,
                    errno.EISCONN,
                }
        if kind == "http":
            with urllib.request.urlopen(rendered, timeout=2) as response:
                return 200 <= response.status < 400
    except (OSError, ValueError, urllib.error.URLError):
        return False
    return False


def dev_start(repo: GitRepo, *, task_id: str, lease: str) -> dict[str, Any]:
    config, _, _ = _config_and_mode(repo)
    if not config.dev_start or not config.readiness:
        raise SoloAIError(
            "No lifecycle.dev_start plus readiness configuration is declared"
        )
    store = StateStore(repo)
    with store.operation(task_id, lease, "dev-start") as task:
        if _is_in_place(task):
            raise SoloAIError(
                "In-place tasks intentionally do not claim a managed dev-server slot; run the project's current-worktree command explicitly if needed"
            )
        if task.get("status") not in {"active", "ready"}:
            raise SoloAIError("Development processes cannot start during task finalization or recovery")
        if task.get("processes"):
            raise SoloAIError("Task already has a registered development process")
        block = config.port_base + (int(task["slot_id"]) - 1) * 100
        for port in range(block, block + 100):
            if not _port_free(port):
                continue
            command = _format_argv(config.dev_start, port=port, slot=task["slot_id"])
            log_path = repo.local_dir / "logs" / f"{task_id}-dev.log"
            if os.name == "nt":
                supervisor = subprocess.Popen(
                    [sys.executable, str(Path(__file__).with_name("supervisor.py"))],
                    cwd=task["worktree"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                    text=True,
                    encoding="utf-8",
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                )
                assert supervisor.stdin is not None
                supervisor.stdin.write(
                    json.dumps({"argv": command, "cwd": task["worktree"]})
                )
                supervisor.stdin.close()
                role = "supervisor"
            else:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with log_path.open("w", encoding="utf-8", newline="\n") as log:
                    supervisor = subprocess.Popen(
                        command,
                        cwd=task["worktree"],
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        shell=False,
                        start_new_session=True,
                    )
                role = "command"
            deadline = time.monotonic() + config.readiness.timeout_seconds
            while time.monotonic() < deadline:
                if supervisor.poll() is not None:
                    break
                if _ready(config.readiness.kind, config.readiness.target, port=port):
                    snapshot = process_snapshot(supervisor.pid)
                    snapshot["role"] = role
                    store.update_task(task_id, processes=[snapshot], port=port)
                    return {
                        "task_id": task_id,
                        "supervisor_pid": supervisor.pid,
                        "port": port,
                    }
                time.sleep(0.2)
            if supervisor.poll() is None:
                supervisor.terminate()
                try:
                    supervisor.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    supervisor.kill()
            detail = ""
            if os.name != "nt" and log_path.exists():
                log_tail = redact_text(
                    log_path.read_text(encoding="utf-8", errors="replace")
                )[-2000:].strip()
                if log_tail:
                    detail = f"\nRecent local development log:\n{log_tail}"
            raise SoloAIError(
                f"Development command did not become ready on port {port} within "
                f"{config.readiness.timeout_seconds:g} seconds; task remains preserved."
                + detail
            )
        raise SoloAIError(
            f"No development process became ready in slot port block {block}-{block + 99}"
        )


def dev_stop(repo: GitRepo, *, task_id: str, lease: str) -> dict[str, Any]:
    _, _, _ = _config_and_mode(repo)
    store = StateStore(repo)
    with store.operation(task_id, lease, "dev-stop") as task:
        _stop_registered_processes(store, task)
        return {"task_id": task_id, "status": "stopped"}


def warm_slot(repo: GitRepo, *, slot_id: str) -> dict[str, Any]:
    with maintenance_lock(repo):
        config, _, _ = _config_and_mode(repo)
        if slot_id not in {f"{number:02d}" for number in range(1, config.slots + 1)}:
            raise SoloAIError("WarmSlot requires an active configured slot id")
        store = StateStore(repo)
        state = store.ensure_slots(config)
        slot = state["slots"][slot_id]
        if slot["status"] not in {"idle", "quarantined"}:
            raise SoloAIError(
                "WarmSlot only runs on an idle or repairable quarantined slot"
            )
        worktree = Path(slot["path"])
        base = _base_ref(repo)
        registered = next(
            (item for item in repo.worktrees() if item.path == worktree), None
        )
        if not worktree.exists():
            if registered is not None:
                raise SoloAIError(
                    "WarmSlot found a registered slot whose path is missing"
                )
            repo.git(["worktree", "add", "--detach", str(worktree), base])
        else:
            if registered is None:
                raise SoloAIError(
                    "WarmSlot found an unregistered slot path and retained it"
                )
            if not repo.is_clean(worktree):
                raise SoloAIError("WarmSlot found a dirty slot and will not modify it")
            if unknown := _unknown_ignored(repo, worktree):
                raise SoloAIError(
                    "WarmSlot found protected or unknown ignored files and will not modify it:\n"
                    + "\n".join(f"- {item}" for item in unknown[:20])
                )
            if repo.branch(worktree) is not None:
                raise SoloAIError("WarmSlot found an unexpectedly attached idle slot")
            repo.git(["reset", "--hard", base], cwd=worktree)
        if slot["status"] == "quarantined":
            store.restore_quarantined_slot(slot_id)
        results: list[dict[str, Any]] = []
        failed_log: Path | None = None
        for command in config.warm_commands:
            pending = (
                repo.local_dir
                / "logs"
                / "pending"
                / f"warm-{slot_id}-{uuid.uuid4().hex}.log"
            )
            result = run_logged(command.argv, cwd=worktree, log_path=pending)
            results.append(
                {
                    "command": command.redacted(),
                    "exit_code": result.returncode,
                    "duration_seconds": round(result.duration_seconds, 3),
                    "log": str(pending),
                }
            )
            if result.returncode:
                failed_log = pending
                break
        changed = repo.changed_paths(worktree)
        unknown = _unknown_ignored(repo, worktree)
        if changed or unknown:
            details = [
                *(f"- {item}" for item in changed),
                *(f"- {item}" for item in unknown),
            ]
            store.quarantine_slot(
                slot_id,
                "WarmSlot modified source or protected files: "
                + ", ".join([*changed, *unknown]),
            )
            raise SoloAIError(
                "WarmSlot modified source or protected files; slot quarantined for "
                "manual inspection:\n" + "\n".join(details[:20])
            )
        if failed_log is not None:
            raise SoloAIError(
                f"WarmSlot command failed; preserved local log: {failed_log}"
            )
        return {"slot": slot_id, "commands": results}


def deinit(repo: GitRepo, *, confirm: str, message: str) -> dict[str, Any]:
    with maintenance_lock(repo):
        return _deinit_locked(repo, confirm=confirm, message=message)


def _deinit_locked(repo: GitRepo, *, confirm: str, message: str) -> dict[str, Any]:
    if confirm != "DEINIT":
        raise SoloAIError("Deinit requires --confirm DEINIT")
    if _effective_mode(repo) != "managed":
        raise SoloAIError("Only an adopted managed repository can be deinitialized")
    config, _, policy = _config_and_mode(repo)
    if policy != repo.root:
        raise SoloAIError(
            "Pending dirty-primary bootstrap must be integrated before deinitialization"
        )
    store = StateStore(repo)
    state = store.require_slot_layout(config)
    if any(
        task.get("status") not in FINAL_TASK_STATES for task in state["tasks"].values()
    ) or any((repo.local_dir / "queue").glob("*.json")):
        raise SoloAIError("Active tasks or integration tickets block deinitialization")
    _require_no_lifecycle_lock(repo)
    primary, _ = repo.ensure_default_primary_clean()
    agents = repo.root / "AGENTS.md"
    existing = agents.read_text(encoding="utf-8") if agents.exists() else ""
    cleaned_agents = remove_managed_agents_block(existing)
    if managed_block() not in existing.replace("\r\n", "\n"):
        raise SoloAIError(
            "Managed AGENTS.md block differs; deinit refuses to remove ambiguous policy text"
        )
    # 先检查全部槽位；任何一个不安全都不得创建或合入策略删除提交。
    slots_to_remove = _preflight_deinit_slots(repo, config=config, state=state)
    # 在临时分支准备策略删除，但只在槽位全部安全释放后才合入默认分支。
    cleanup = repo.local_dir / "deinit" / uuid.uuid4().hex / "worktree"
    branch = f"solo-ai/deinit-{uuid.uuid4().hex[:8]}"
    default = repo.default_branch()
    repo.git(["worktree", "add", "-b", branch, str(cleanup), default], cwd=primary)
    policy_integrated = False
    removed_slot_paths: list[Path] = []
    try:
        (cleanup / ".solo-ai" / "config.toml").unlink()
        (cleanup / ".solo-ai" / "verification.toml").unlink()
        try:
            (cleanup / ".solo-ai").rmdir()
        except OSError:
            pass
        cleanup_agents = cleanup / "AGENTS.md"
        if config.agents_file_created and not cleaned_agents:
            cleanup_agents.unlink()
        else:
            cleanup_agents.write_text(cleaned_agents, encoding="utf-8", newline="\n")
        repo.git(["add", "--update", "--", ".solo-ai", "AGENTS.md"], cwd=cleanup)
        repo.git(["commit", "-m", message], cwd=cleanup)
        removed_slots: list[str] = []
        for path in slots_to_remove:
            # 预检后仍重验，避免用户或其他工具在清理过程中写入槽位。
            if _assert_removable_managed_slot(repo, path):
                repo.git(["worktree", "remove", "--force", str(path)], cwd=primary)
                removed_slots.append(str(path))
                removed_slot_paths.append(path)
        # 槽位释放成功后再次确认主工作区，随后才提交受管策略删除。
        primary, _ = repo.ensure_default_primary_clean()
        repo.git(["merge", "--ff-only", branch], cwd=primary)
        policy_integrated = True
    except Exception as exc:
        restoration_failures = _restore_removed_slots(
            repo,
            primary=primary,
            base=default,
            removed=removed_slot_paths,
        )
        if restoration_failures:
            raise SoloAIError(
                f"{exc}\nPreviously removed slots could not be restored:\n"
                + "\n".join(f"- {item}" for item in restoration_failures)
            ) from exc
        raise
    finally:
        repo.git(
            ["worktree", "remove", "--force", str(cleanup)], cwd=primary, check=False
        )
        repo.git(
            ["branch", "-d" if policy_integrated else "-D", branch],
            cwd=primary,
            check=False,
        )
    # Only the exact local state root is removed after all managed slots are gone.
    shutil.rmtree(repo.local_dir)
    return {
        "status": "deinitialized",
        "removed_slots": removed_slots,
        "next": "Plugin may now be uninstalled from Codex; no repository scan is performed.",
    }
