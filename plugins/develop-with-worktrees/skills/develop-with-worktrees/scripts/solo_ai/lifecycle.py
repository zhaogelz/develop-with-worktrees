from __future__ import annotations

import errno
import fnmatch
import json
import os
import platform
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import psutil

from .abandonment import (
    assert_retained_worktree_safe,
    assert_task_not_held_by_candidate_delivery,
    in_place_audit,
    prepare as prepare_abandonment,
    prepare_retained as prepare_retained_abandonment,
)
from .abandonment import resume as resume_abandonment
from .abandonment import resume_retained as resume_retained_abandonment
from .abandonment import retained_reclaim_plan
from .abandonment import resume_retained_reclaim
from .abandonment import write_completed_receipt as write_abandonment_receipt
from .cleanup import inspect_untracked, require_managed_directory_identity
from .config import (
    CommandSpec,
    STRESS_VERIFICATION_FILENAME,
    VerificationConfig,
    discover_validation_commands,
    load_repo_config,
    load_verification_config,
    read_verification_config_file,
    remove_managed_agents_block,
    render_agents,
    render_repo_config,
    render_verification_config,
    verification_config_from_text,
)
from .delegated import inspect_delegated
from .integration import (
    integration_turn,
    migrate_legacy_receipt,
    write_completed_receipt,
)
from .host_context import normalize_host_reference
from .integration import legacy_transaction as legacy_integration_transaction
from .integration import prepare as prepare_integration
from .integration import resume_prepared as resume_integration
from .proof import (
    ValidationBaseChanged,
    approval_plan,
    new_validation_attempt_id,
    require_approved_plan,
    selected_profile_ids,
    require_exact_passed_proof,
    validate,
)
from .repo import GitRepo
from .root_context import (
    amend_root_anchor,
    create_root_anchor,
    delete_root_anchor,
    list_root_anchors,
    nonterminal_external_root_children,
    read_root_acceptance_evidence_input,
    read_root_acceptance_index_input,
    reindex_root_acceptance,
    require_candidate_delivery_terminal,
    read_root_change_input,
    read_root_plan_input,
    record_root_acceptance,
    register_external_root_child,
    resolve_root_anchor,
    root_anchor_path,
    root_anchor_lock,
    root_id_for_request,
    show_root_anchor,
    show_root_anchor_history,
    update_root_anchor,
    update_root_progress,
    upgrade_root_to_objective_protocol,
    write_root_close_receipt,
    read_external_root_close_receipt,
    read_root_close_receipt,
)
from .routing import decide_route, detect_existing_workflows
from .safety import require_safe
from .state import (
    FINAL_TASK_STATES,
    IN_PLACE_MODE,
    ISOLATED_MODE,
    ROOT_BINDING_PROTOCOL_VERSION,
    StateStore,
    candidate_admission_lock,
)
from .task_context import (
    adopt_legacy_anchor,
    bind_root_reference,
    create_anchor,
    delete_anchor,
    initial_anchor_contract,
    read_anchor,
    read_anchor_update,
    require_anchor,
    update_anchor,
)
from .util import (
    ActionableSoloAIError,
    DirectoryLock,
    SoloAIError,
    atomic_write_json,
    ensure_within,
    path_identity,
    process_matches,
    process_snapshot,
    read_json,
    redact_text,
    run_logged,
    safe_slug,
    sha256_file,
    sha256_text,
    stable_json,
    utc_timestamp,
)

BOOTSTRAP_SCHEMA = 2
TASK_GRANT_SCHEMA = 1
PREACTIVATION_RELEASE_SCHEMA = 2
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
    verification_file: Path | None = None,
    session_id: str | None = None,
    delegation_code: str | None = None,
) -> dict[str, Any]:
    """将首次三选一交互收敛为唯一入口，避免把内部初始化细节暴露给用户。"""
    route = repository_route(repo, session_id=session_id)
    if route["action"] in {"defer", "delegated"}:
        return {
            "choice": mode,
            "decision": "delegated" if route["action"] == "delegated" else "deferred",
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
        verification_file=verification_file,
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
        delegated=inspect_delegated(repo.root, repo.common_dir),
    )


def _effective_mode(repo: GitRepo) -> str:
    action = repository_route(repo)["action"]
    return "uninitialized" if action == "ask" else str(action)


def _approval_path(repo: GitRepo) -> Path:
    return repo.local_dir / "approvals.json"


def _approval_fingerprint(
    repo: GitRepo,
    verification: VerificationConfig,
    *,
    cwd: Path,
    scope: str = "all",
    profile_ids: tuple[str, ...] | None = None,
    include_secret_scanner: bool = False,
    include_warm_commands: bool = False,
    include_dev_start: bool = False,
    adapter_operations: tuple[str, ...] = (),
) -> tuple[str, dict[str, Any]]:
    plan = approval_plan(
        repo,
        cwd=cwd,
        verification=verification,
        scope=scope,
        profile_ids=profile_ids,
        include_secret_scanner=include_secret_scanner,
        include_warm_commands=include_warm_commands,
        include_dev_start=include_dev_start,
        adapter_operations=adapter_operations,
    )
    return sha256_text(stable_json(plan)), plan


def approve(
    repo: GitRepo,
    verification: VerificationConfig,
    *,
    cwd: Path | None = None,
    scope: str = "all",
    profile_ids: tuple[str, ...] | None = None,
    include_secret_scanner: bool = False,
    include_warm_commands: bool = False,
    include_dev_start: bool = False,
    adapter_operations: tuple[str, ...] = (),
) -> dict[str, Any]:
    policy = cwd or repo.policy_path()
    fingerprint, plan = _approval_fingerprint(
        repo,
        verification,
        cwd=policy,
        scope=scope,
        profile_ids=profile_ids,
        include_secret_scanner=include_secret_scanner,
        include_warm_commands=include_warm_commands,
        include_dev_start=include_dev_start,
        adapter_operations=adapter_operations,
    )
    approvals = read_json(_approval_path(repo), {"schema_version": 2, "accepted": {}})
    approvals["accepted"][fingerprint] = {"accepted_at": utc_timestamp(), "plan": plan}
    atomic_write_json(_approval_path(repo), approvals)
    return {"fingerprint": fingerprint, "plan": plan}


def require_approval(
    repo: GitRepo,
    verification: VerificationConfig,
    *,
    cwd: Path | None = None,
    scope: str = "all",
    profile_ids: tuple[str, ...] | None = None,
    include_secret_scanner: bool = False,
    include_warm_commands: bool = False,
    include_dev_start: bool = False,
    adapter_operations: tuple[str, ...] = (),
    approval_target: dict[str, str] | None = None,
) -> None:
    policy = cwd or repo.policy_path()
    require_approved_plan(
        repo,
        cwd=policy,
        verification=verification,
        message="This machine has not approved the commands required by this lifecycle step.",
        scope=scope,
        profile_ids=profile_ids,
        include_secret_scanner=include_secret_scanner,
        include_warm_commands=include_warm_commands,
        include_dev_start=include_dev_start,
        adapter_operations=adapter_operations,
        approval_target=approval_target,
    )


def _require_validation_approval(
    repo: GitRepo,
    *,
    cwd: Path,
    base: str,
    verification: VerificationConfig,
    level: str,
    full_scope: str | None,
    scope: str,
    include_secret_scanner: bool,
    approval_target: dict[str, str],
) -> None:
    levels = (
        ("ready",)
        if level == "ready"
        else (("stress",) if level == "stress" else ("ready", "full"))
    )
    full_scopes = (
        ("integration", "complete")
        if level == "full" and full_scope == "complete"
        else ((full_scope,) if level == "full" and full_scope else None)
    )
    require_approval(
        repo,
        verification,
        cwd=cwd,
        scope=scope,
        profile_ids=selected_profile_ids(
            repo,
            cwd=cwd,
            base=base,
            verification=verification,
            levels=levels,
            full_scopes=full_scopes,
        ),
        include_secret_scanner=include_secret_scanner,
        approval_target=approval_target,
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


def _needs_empty_baseline(repo: GitRepo, target: Path) -> bool:
    """确认调用目标是否是真正的无提交、无内容 unborn 仓库。"""
    head = repo.git(
        ["rev-parse", "--verify", "HEAD^{commit}"],
        cwd=target,
        check=False,
    )
    if head.returncode == 0:
        return False
    symbolic = repo.git(
        ["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=target, check=False
    )
    refs = repo.git(["show-ref"], cwd=target, check=False)
    if symbolic.returncode != 0 or refs.returncode != 1 or refs.stdout.strip():
        raise SoloAIError(
            "Current target worktree has no readable HEAD but is not a clean unborn repository; "
            "preserve it and repair the Git refs before adoption."
        )
    dirty = repo.git(
        ["status", "--porcelain=v1", "--untracked-files=all", "--ignored"],
        cwd=target,
    ).stdout.splitlines()
    if dirty:
        raise SoloAIError(
            "Cannot create the initial empty baseline while the current target worktree has "
            "staged, modified, untracked, or ignored files: " + ", ".join(dirty[:5])
        )
    return True


def _target_is_clean(repo: GitRepo, target: Path) -> bool:
    """策略写入前同时保留暂存、未暂存、未跟踪与忽略的用户内容。"""

    ignored = set(repo.ignored_untracked(target, directories=True))
    # `.worktrees/` 是 DWW 在该目标下创建并登记的受管槽位根；它不会代表
    # 用户脏内容。其余忽略内容仍必须保留并阻断 bootstrap 合入。
    ignored.discard(".worktrees/")
    return repo.is_clean(target) and not ignored


def _create_empty_baseline(repo: GitRepo, target: Path) -> None:
    """用临时 index 建立无文件、无父提交的首个基线，保留用户 index。"""
    index_fd, index_name = tempfile.mkstemp(
        prefix="dww-empty-index-", dir=str(repo.local_dir)
    )
    os.close(index_fd)
    index_path = Path(index_name)
    try:
        index_path.unlink()
        env = os.environ.copy()
        env["GIT_INDEX_FILE"] = str(index_path)
        repo.git(["read-tree", "--empty"], cwd=target, env=env)
        repo.git(
            ["commit", "--allow-empty", "-m", "chore: 建立空仓库初始基线"],
            cwd=target,
            env=env,
        )
    except Exception as exc:
        raise SoloAIError(
            f"Cannot create the initial empty Git baseline; no bootstrap was attempted: {exc}"
        ) from exc
    finally:
        try:
            index_path.unlink()
        except FileNotFoundError:
            pass


def _initialization_plan(
    repo: GitRepo,
    verification: VerificationConfig,
    *,
    slots: int,
    validation_source: str,
    initial_empty_baseline: bool,
) -> dict[str, Any]:
    return {
        "slots": slots,
        "profiles": (
            [
                {
                    "id": profile.profile_id,
                    "paths": list(profile.paths),
                    "commands": [command.redacted() for command in profile.commands],
                    "cross_task_reuse": profile.cross_task_reuse,
                    "external_state": profile.external_state,
                    "input_paths": list(profile.input_paths),
                    "input_closure": profile.input_closure,
                    "level": profile.level,
                    "full_scope": profile.full_scope,
                }
                for profile in verification.profiles
            ]
            if verification.profiles
            else []
        ),
        "static_only": verification.static_only,
        "validation_source": validation_source,
        "initial_empty_baseline": initial_empty_baseline,
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
    verification_file: Path | None = None,
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
    if commands is not None and verification_file is not None:
        raise SoloAIError("--verify cannot be combined with --verification-file")
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
        target, target_ref = repo.checked_out_local_branch()
        initial_empty_baseline = _needs_empty_baseline(repo, target)
        if verification_file is not None:
            source, rendered_verification, verification = read_verification_config_file(
                verification_file
            )
            selected = list(verification.commands)
            static_only = verification.static_only
            validation_source = f"reviewed verification file: {source}"
        else:
            selected = (
                commands
                if commands is not None
                else discover_validation_commands(repo.root)
            )
            static_only = not selected
            discovery_fallback = commands is None
            rendered_verification = render_verification_config(
                selected,
                static_only=static_only,
                discovery_fallback=discovery_fallback,
            )
            verification = verification_config_from_text(
                rendered_verification,
                source=repo.root / ".solo-ai" / "verification.toml",
            )
            validation_source = (
                "automatic command discovery; generated conservative integration Full profile"
                if discovery_fallback
                else "explicit --verify command argv"
            )
        if static_only and not accept_static_only and accept:
            raise SoloAIError(
                "No validation command was discovered. Re-run with --accept-static-only only after reviewing the limitation."
            )
        if (selected and not accept) or (static_only and not accept_static_only):
            return {
                "decision": "needs-approval",
                "plan": _initialization_plan(
                    repo,
                    verification,
                    slots=slots,
                    validation_source=validation_source,
                    initial_empty_baseline=initial_empty_baseline,
                ),
            }
        # 审阅后重新检查：仅为空 unborn 仓库建一次基线，用户内容变化则保留并拒绝。
        if _needs_empty_baseline(repo, target):
            _create_empty_baseline(repo, target)
        target_head = repo.head(target)
        target_identity = path_identity(target)
        repo.require_checked_out_branch_target(
            target,
            target_ref,
            expected_head=target_head,
            expected_identity=target_identity,
        )
        bootstrap_id = uuid.uuid4().hex[:8]
        branch = f"solo-ai/bootstrap-{bootstrap_id}"
        worktree = repo.local_dir / "bootstrap" / bootstrap_id / "worktree"
        repo.git(
            ["worktree", "add", "-b", branch, str(worktree), target_ref],
            cwd=target,
        )
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
                rendered_verification,
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
        clean_target = _target_is_clean(repo, target)
        bootstrap = {
            "schema_version": BOOTSTRAP_SCHEMA,
            "branch": branch,
            "worktree": str(worktree),
            # default_branch 保留给 schema-1 恢复；新记录必须显式绑定调用目标。
            "default_branch": target_ref,
            "target_ref": target_ref,
            "target_head": target_head,
            "target_worktree": str(target.resolve()),
            "target_worktree_resolved": str(target.resolve()),
            "target_worktree_identity": target_identity,
            "bootstrap_head": repo.head(worktree),
            "created_at": utc_timestamp(),
        }
        if clean_target:
            repo.require_checked_out_branch_target(
                target,
                target_ref,
                expected_head=target_head,
                expected_identity=target_identity,
            )
            repo.git(["merge", "--ff-only", branch], cwd=target)
            repo.git(["worktree", "remove", str(worktree)], cwd=target)
            repo.git(["branch", "-d", branch], cwd=target)
        else:
            atomic_write_json(repo.local_dir / "bootstrap.json", bootstrap)
        policy = repo.policy_path()
        verification = load_verification_config(repo, cwd=policy)
        approval = approve(repo, verification, cwd=policy)
        StateStore(repo).ensure_slots(load_repo_config(repo, cwd=policy))
        return {
            "decision": "adopted" if clean_target else "pending-primary-clean",
            "slots": slots,
            "static_only": static_only,
            "commands": [command.redacted() for command in selected],
            "approval": approval["fingerprint"],
            "target_branch": target_ref,
            "target_worktree": str(target.resolve()),
            "target_dirty_excluded": not clean_target,
            # 保留旧响应字段，供既有调用方平滑升级。
            "primary_dirty_excluded": not clean_target,
        }


def _require_managed_mode(repo: GitRepo) -> None:
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


def _require_anchor_caller(repo: GitRepo, task: dict[str, Any]) -> None:
    caller = repo.root.resolve()
    registered_worktrees = {item.path.resolve() for item in repo.worktrees()}
    locations = (
        (
            "worktree",
            task.get("worktree"),
            task.get("slot_worktree_resolved"),
            task.get("slot_worktree_identity"),
            task.get("slot_managed_root_resolved"),
            task.get("slot_managed_root_identity"),
        ),
        (
            "base worktree",
            task.get("base_worktree"),
            task.get("base_worktree_resolved"),
            task.get("base_worktree_identity"),
            None,
            None,
        ),
    )
    for label, stored, expected, identity, root_expected, root_identity in locations:
        if not stored:
            continue
        target = Path(str(stored))
        if caller != target.resolve():
            continue
        if target.resolve() not in registered_worktrees:
            raise SoloAIError("Recorded anchor caller is no longer a Git worktree")
        managed_root = target.absolute().parent
        require_managed_directory_identity(
            target,
            managed_root=managed_root,
            expected_resolved=str(expected) if expected else None,
            expected_identity=identity if isinstance(identity, dict) else None,
            expected_root_resolved=(str(root_expected) if root_expected else None),
            expected_root_identity=(
                root_identity if isinstance(root_identity, dict) else None
            ),
        )
        if label == "worktree" and StateStore.mode(task) == ISOLATED_MODE:
            slots = StateStore(repo).read().get("slots", {})
            slot = slots.get(str(task.get("slot_id")))
            if not slot or slot.get("task_id") != task.get("id"):
                raise SoloAIError(
                    "Task worktree no longer belongs to its recorded slot"
                )
        return
    raise SoloAIError(
        "Anchor operations must run from the task worktree or its recorded base worktree"
    )


def _config_and_mode(repo: GitRepo) -> tuple[Any, VerificationConfig, Path]:
    """读取受管配置，不把纯读取或锚点维护误当成一次验证执行。"""

    _require_managed_mode(repo)
    policy = repo.policy_path()
    config = load_repo_config(repo, cwd=policy)
    verification = load_verification_config(repo, cwd=policy)
    return config, verification, policy


def _base_ref(repo: GitRepo) -> str:
    """保留给空闲槽位等没有调用方上下文的场景。"""
    bootstrap = _bootstrap(repo)
    return str(bootstrap.get("branch") or repo.default_branch())


def adopt_task_anchor(
    repo: GitRepo,
    *,
    task_id: str,
    objective: str,
    target: str,
    scope: str,
    acceptance: str,
    confirm: str,
) -> dict[str, Any]:
    _config_and_mode(repo)
    with maintenance_lock(repo):
        task = StateStore(repo).task(task_id)
        if task.get("anchor_origin") is not None:
            raise SoloAIError(
                "anchor adopt is only for a pre-origin legacy task; restore the original anchor facts instead"
            )
        path, origin = adopt_legacy_anchor(
            repo,
            task,
            objective=objective,
            target=target,
            scope=scope,
            acceptance=acceptance,
            confirm=confirm,
        )
        StateStore(repo).update_task(
            task_id,
            anchor_origin=origin,
        )
        return {"task_id": task_id, "anchor_path": str(path.resolve())}


def _anchor_view(value: dict[str, Any], *, include_content: bool) -> dict[str, Any]:
    """默认返回轻量身份摘要，正文只在调用者明确需要时保留。"""

    result = dict(value)
    if include_content:
        # 完整锚点正文已含确认方案，不能再用独立字段重复传输同一大段内容。
        result.pop("confirmed_plan", None)
    else:
        result.pop("content", None)
        result.pop("confirmed_plan", None)
    return result


def _read_root_context(
    repo: GitRepo, *, store: StateStore, task: dict[str, Any]
) -> dict[str, Any]:
    """返回一次完整根正文，并把这次读取记为连续工作的内部事实。"""

    root_id = task.get("root_anchor_id")
    if not root_id:
        return {}
    root = resolve_root_anchor(
        repo,
        root_id=str(root_id),
        external_path=(
            Path(str(task["root_anchor_file"]))
            if task.get("root_anchor_file")
            else None
        ),
    )
    version = root.get("plan_version")
    sha256 = str(root["sha256"])
    if version is not None and (
        task.get("reviewed_root_plan_version") != version
        or task.get("reviewed_root_plan_sha256") != sha256
    ):
        store.update_task(
            str(task["id"]),
            reviewed_root_plan_version=version,
            reviewed_root_plan_sha256=sha256,
        )
    return {
        "root_anchor": _anchor_view(root, include_content=True),
        "root_plan_review": {
            "current_version": version,
            "reviewed_version": version,
            "requires_review": False,
            "record_kind": "read",
        },
        "reviewed_root_plan_version": version,
        "reviewed_root_plan_sha256": sha256 if version is not None else None,
    }


def show_task_anchor(
    repo: GitRepo,
    *,
    task_id: str,
    with_root: bool = False,
    include_content: bool = True,
    include_root_content: bool = True,
) -> dict[str, Any]:
    _require_managed_mode(repo)
    task = StateStore(repo).task(task_id)
    _require_anchor_caller(repo, task)
    result = read_anchor(repo, task)
    result["status"] = task.get("status")
    if with_root and task.get("root_anchor_id"):
        root = resolve_root_anchor(
            repo,
            root_id=str(task["root_anchor_id"]),
            external_path=(
                Path(str(task["root_anchor_file"]))
                if task.get("root_anchor_file")
                else None
            ),
        )
        current_version = root.get("plan_version")
        reviewed_version = task.get("reviewed_root_plan_version")
        result["root_anchor"] = _anchor_view(root, include_content=include_root_content)
        result["root_plan_review"] = {
            "current_version": current_version,
            "reviewed_version": reviewed_version,
            "requires_review": current_version is not None
            and current_version != reviewed_version,
        }
    return _anchor_view(result, include_content=include_content)


def bind_task_root_anchor(
    repo: GitRepo,
    *,
    task_id: str,
    lease: str,
    root_id: str,
    root_anchor_file: Path | None = None,
) -> dict[str, Any]:
    """为已启动任务补齐一个精确主锚点绑定；重复调用可收敛。"""

    _require_managed_mode(repo)
    store = StateStore(repo)
    with store.operation(task_id, lease, "root-anchor-bind") as task:
        _require_anchor_caller(repo, task)
        status = str(task.get("status"))
        if status not in {"active", "ready"}:
            raise SoloAIError(
                "Only an active or ready task can bind a root anchor "
                f"(current status: {status})"
            )
        root = resolve_root_anchor(
            repo, root_id=root_id, external_path=root_anchor_file
        )
        external_root_file = (
            str(root["root_anchor_path"]) if root_anchor_file is not None else None
        )
        existing_id = task.get("root_anchor_id")
        existing_file = task.get("root_anchor_file")
        expected_id = task.get("expected_root_anchor_id")
        expected_file = task.get("expected_root_anchor_file")
        if expected_id is not None and (
            expected_id != root_id or expected_file != external_root_file
        ):
            raise SoloAIError("Task expects a different root anchor")
        if existing_id is not None:
            if existing_id != root_id or existing_file != external_root_file:
                raise SoloAIError("Task is already bound to a different root anchor")
        else:
            task = store.update_task(
                task_id,
                root_anchor_id=root_id,
                root_anchor_file=external_root_file,
                expected_root_anchor_id=root_id,
                expected_root_anchor_file=external_root_file,
                root_binding_protocol=ROOT_BINDING_PROTOCOL_VERSION,
                root_binding_exception=None,
                reviewed_root_plan_version=None,
                reviewed_root_plan_sha256=None,
            )
        if external_root_file is not None:
            register_external_root_child(
                root_id=root_id,
                root_anchor_file=Path(external_root_file),
                task_id=task_id,
                child_state_path=store.path,
            )
        anchor = bind_root_reference(repo, task)
        return {
            "task_id": task_id,
            "root_id": root_id,
            "root_anchor_path": str(root["root_anchor_path"]),
            "anchor_path": anchor["anchor_path"],
            "status": status,
            **_read_root_context(repo, store=store, task=task),
        }


def acknowledge_root_plan(
    repo: GitRepo, *, task_id: str, lease: str, root_version: int, root_sha256: str
) -> dict[str, Any]:
    _require_managed_mode(repo)
    if root_version < 1 or not re.fullmatch(r"[0-9a-f]{64}", root_sha256 or ""):
        raise SoloAIError("Root plan acknowledgement identity is invalid")
    store = StateStore(repo)
    with store.operation(task_id, lease, "root-plan-acknowledge") as task:
        _require_anchor_caller(repo, task)
        if task.get("status") not in {"active", "ready"}:
            raise SoloAIError(
                "Only an active or ready task can acknowledge its root plan"
            )
        root_id = task.get("root_anchor_id")
        if not root_id:
            raise SoloAIError("Task is not bound to a root anchor")
        root = resolve_root_anchor(
            repo,
            root_id=str(root_id),
            external_path=(
                Path(str(task["root_anchor_file"]))
                if task.get("root_anchor_file")
                else None
            ),
        )
        if (
            root.get("plan_version") != root_version
            or root.get("sha256") != root_sha256
        ):
            raise SoloAIError("Root plan changed before acknowledgement; read it again")
        store.update_task(
            task_id,
            reviewed_root_plan_version=root_version,
            reviewed_root_plan_sha256=root_sha256,
        )
        return {
            "task_id": task_id,
            "root_id": root_id,
            "reviewed_root_plan_version": root_version,
        }


def refresh_root_context(repo: GitRepo, *, task_id: str, lease: str) -> dict[str, Any]:
    """供宿主续作时一次读取并登记根方案，免除搬运版本和摘要参数。"""

    _require_managed_mode(repo)
    store = StateStore(repo)
    with store.operation(task_id, lease, "root-context-refresh") as task:
        _require_anchor_caller(repo, task)
        if task.get("status") not in {"active", "ready"}:
            raise SoloAIError(
                "Only an active or ready task can refresh its root context"
            )
        task_anchor = read_anchor(repo, task)
        task_anchor["status"] = task.get("status")
        refresh_marker = store.root_context_refresh_required(task_id)
        context = _read_root_context(repo, store=store, task=task)
        if not context:
            raise SoloAIError("Task is not bound to a root anchor")
        if refresh_marker is not None:
            store.clear_root_context_refresh(
                task_id, generation=str(refresh_marker["generation"])
            )
        return {
            "task_id": task_id,
            "root_id": task["root_anchor_id"],
            "task_anchor": _anchor_view(task_anchor, include_content=True),
            **context,
        }


def _require_current_structured_root_review(
    repo: GitRepo, task: dict[str, Any], *, store: StateStore
) -> None:
    """关键动作前只核验已记录的根方案版本，不把它变成人工批准。"""

    root_id = task.get("root_anchor_id")
    root_file = task.get("root_anchor_file")
    expected_root_id = task.get("expected_root_anchor_id")
    expected_root_file = task.get("expected_root_anchor_file")
    if (
        task.get("root_binding_protocol") == ROOT_BINDING_PROTOCOL_VERSION
        and expected_root_id is not None
        and (root_id != expected_root_id or root_file != expected_root_file)
    ):
        raise ActionableSoloAIError(
            "This task lost or changed its expected root anchor binding. Restore the exact recorded binding before continuing.",
            code="ROOT_BINDING_MISSING",
            context={
                "task_id": str(task["id"]),
                "expected_root_id": str(expected_root_id),
            },
            next_action={
                "kind": "bind_expected_root",
                "task_id": str(task["id"]),
                "root_id": str(expected_root_id),
                "retry": "after_action",
            },
        )
    if not root_id:
        return
    if refresh := store.root_context_refresh_required(str(task["id"])):
        raise ActionableSoloAIError(
            "This task resumed after its root context was last read. Refresh the complete root context before changing files or continuing lifecycle work.",
            code="ROOT_CONTEXT_REFRESH_REQUIRED",
            context={
                "task_id": str(task["id"]),
                "root_id": str(root_id),
                "reason": refresh["reason"],
            },
            next_action={
                "kind": "refresh_root",
                "task_id": str(task["id"]),
                "retry": "after_action",
            },
        )
    root = resolve_root_anchor(
        repo,
        root_id=str(root_id),
        external_path=(Path(str(root_file)) if root_file else None),
    )
    current_version = root.get("plan_version")
    if current_version is None:
        return
    if task.get("reviewed_root_plan_version") == current_version:
        return
    raise ActionableSoloAIError(
        "The bound structured root plan has not been reviewed at its current version. "
        "Automatically recover through the host one-step root-context refresh; it reads "
        "and records the current version without a new user confirmation.",
        code="ROOT_CONTEXT_STALE",
        context={"task_id": str(task["id"]), "root_id": str(root_id)},
        next_action={
            "kind": "refresh_root",
            "task_id": str(task["id"]),
            "retry": "after_action",
        },
    )


def update_task_anchor(
    repo: GitRepo,
    *,
    task_id: str,
    lease: str,
    input_path: Path,
    expected_sha256: str,
) -> dict[str, Any]:
    _require_managed_mode(repo)
    store = StateStore(repo)
    with store.operation(task_id, lease, "anchor-update") as task:
        _require_anchor_caller(repo, task)
        status = str(task.get("status"))
        if status not in {"active", "ready"}:
            raise SoloAIError(
                "Only an active or ready task can update its task anchor "
                f"(current status: {status})"
            )
        with maintenance_lock(repo):
            content = read_anchor_update(repo, input_path)
            result = update_anchor(
                repo,
                task,
                content=content,
                expected_sha256=expected_sha256,
                progress_only=status == "ready",
            )
        result["status"] = status
        return result


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


def _assert_starting_task_identity(repo: GitRepo, task: dict[str, Any]) -> None:
    worktree = Path(str(task["worktree"]))
    managed_root = worktree.absolute().parent
    require_managed_directory_identity(
        worktree,
        managed_root=managed_root,
        expected_resolved=str(task["slot_worktree_resolved"]),
        expected_root_resolved=str(task["slot_managed_root_resolved"]),
        expected_identity=dict(task["slot_worktree_identity"]),
        expected_root_identity=dict(task["slot_managed_root_identity"]),
    )
    if (
        task.get("status") != "starting"
        or not repo.is_clean(worktree)
        or repo.head(worktree) != task.get("candidate_head")
        or repo.branch(worktree) != task.get("branch")
        or _unknown_ignored(repo, worktree)
    ):
        raise SoloAIError(
            "Task worktree changed while the runtime Adapter was activating it"
        )


def _complete_runtime_activation(
    repo: GitRepo, *, store: StateStore, task: dict[str, Any]
) -> dict[str, Any]:
    request_reused = bool(task.get("request_reused"))
    task = store.task(str(task["id"]))
    try:
        _assert_starting_task_identity(repo, task)
    except Exception as exc:
        store.quarantine(str(task["id"]), str(exc))
        raise
    from .runtime_adapter import activate_task_runtime

    runtime_activation = activate_task_runtime(repo, task=task)
    refreshed = store.task(str(task["id"]))
    try:
        _assert_starting_task_identity(repo, refreshed)
    except Exception as exc:
        store.quarantine(str(task["id"]), str(exc))
        raise
    activated = store.activate_started_task(
        str(task["id"]), runtime_activation=runtime_activation
    )
    anchor = require_anchor(repo, activated)
    return {
        **activated,
        "anchor_path": str(anchor.resolve()),
        "runtime_activation": runtime_activation,
        "request_reused": request_reused,
        **_read_root_context(repo, store=store, task=activated),
    }


def _resume_quarantined_start(
    repo: GitRepo,
    *,
    store: StateStore,
    task: dict[str, Any],
    operation_id: str | None,
    request_reused: bool,
) -> dict[str, Any]:
    """只按原始 task/slot/base 身份续接尚未激活的 Start。"""

    if (
        _is_in_place(task)
        or task.get("status") != "quarantined"
        or task.get("candidate_publication")
        or task.get("integration")
        or task.get("abandonment")
        or task.get("runtime_activation") is not None
        or task.get("runtime_activation_pending") is False
    ):
        raise SoloAIError(
            "Only a quarantined pre-activation Start can use this recovery path"
        )
    worktree = Path(str(task["worktree"]))
    managed_root = worktree.absolute().parent
    identity_fields = (
        task.get("slot_worktree_identity"),
        task.get("slot_managed_root_identity"),
        task.get("slot_worktree_resolved"),
        task.get("slot_managed_root_resolved"),
    )
    if all(identity_fields):
        resolved = require_managed_directory_identity(
            worktree,
            managed_root=managed_root,
            expected_resolved=str(task["slot_worktree_resolved"]),
            expected_root_resolved=str(task["slot_managed_root_resolved"]),
            expected_identity=dict(task["slot_worktree_identity"]),
            expected_root_identity=dict(task["slot_managed_root_identity"]),
        )
    elif any(identity_fields):
        raise SoloAIError("Quarantined Start has incomplete directory identity")
    else:
        if not worktree.is_dir():
            raise SoloAIError(
                "Quarantined Start worktree is missing; preserve state for inspection"
            )
        resolved = require_managed_directory_identity(
            worktree, managed_root=managed_root
        )
    if not any(item.path == resolved for item in repo.worktrees()):
        raise SoloAIError("Quarantined Start worktree is no longer registered")
    if not repo.is_clean(worktree):
        raise SoloAIError("Dirty task worktree blocks quarantined Start recovery")
    if unknown := _unknown_ignored(repo, worktree):
        raise SoloAIError(
            "Quarantined Start still contains protected or unknown ignored content:\n"
            + "\n".join(f"- {item}" for item in unknown[:20])
        )

    branch = str(task["branch"])
    branch_head = repo.ref_head(f"refs/heads/{branch}")
    if branch_head is None:
        if (
            task.get("candidate_head")
            or task.get("runtime_activation_pending")
            or (repo.local_dir / "task-anchors" / f"{task['id']}.md").exists()
            or repo.branch(worktree) is not None
        ):
            raise SoloAIError(
                "Quarantined Start has ambiguous partial activation facts"
            )
        repo.git(["reset", "--hard", str(task["base_head"])], cwd=worktree)
        repo.git(["switch", "-c", branch, str(task["base_head"])], cwd=worktree)
        branch_head = repo.head(worktree)
    elif (
        repo.branch(worktree) != branch
        or repo.head(worktree) != branch_head
        or task.get("candidate_head") not in {None, branch_head}
    ):
        raise SoloAIError("Quarantined Start branch or worktree identity is ambiguous")

    if not repo.is_clean(worktree) or repo.head(worktree) != branch_head:
        raise SoloAIError("Quarantined Start worktree changed during recovery")
    if unknown := _unknown_ignored(repo, worktree):
        raise SoloAIError(
            "Quarantined Start received protected or unknown ignored content:\n"
            + "\n".join(f"- {item}" for item in unknown[:20])
        )
    prepared = store.resume_quarantined_start(
        str(task["id"]),
        operation_id=operation_id,
        candidate_head=branch_head,
        baseline_paths=repo.changed_paths(worktree),
        worktree_identity=path_identity(worktree),
        managed_root_identity=path_identity(managed_root),
        worktree_resolved=str(resolved),
        managed_root_resolved=str(managed_root.resolve()),
    )
    prepared["request_reused"] = request_reused
    create_anchor(repo, prepared)
    return _complete_runtime_activation(repo, store=store, task=prepared)


def _is_dirty_preactivation_failure(task: dict[str, Any]) -> bool:
    """只识别尚未创建任务分支、由旧脏槽位直接阻断的 Start。"""

    return (
        not _is_in_place(task)
        and task.get("status") == "quarantined"
        and str(task.get("quarantine_reason") or "").startswith(
            "Idle slot is not clean:"
        )
        and not task.get("candidate_head")
        and not task.get("candidate_publication")
        and not task.get("integration")
        and not task.get("abandonment")
        and not task.get("preactivation_release")
        and task.get("ready_proof") is None
        and task.get("runtime_activation") is None
        and task.get("runtime_activation_pending") is not True
    )


def _preactivation_release_result(task: dict[str, Any]) -> dict[str, Any]:
    recovery = dict(task["preactivation_release"])
    return {
        "id": task["id"],
        "status": "abandoned",
        "recovery": "preactivation-dirty-slot-release",
        "transaction_id": recovery["transaction_id"],
        "release_head": recovery["release_head"],
        "released_slot": task["slot_id"],
    }


def _preactivation_release_worktree(
    repo: GitRepo, task: dict[str, Any]
) -> tuple[Path, Path, Path]:
    worktree = Path(str(task["worktree"]))
    managed_root = worktree.absolute().parent
    identity_fields = (
        task.get("slot_worktree_identity"),
        task.get("slot_managed_root_identity"),
        task.get("slot_worktree_resolved"),
        task.get("slot_managed_root_resolved"),
    )
    if not all(identity_fields):
        raise SoloAIError(
            "Pre-activation release requires complete recorded directory identity"
        )
    resolved = require_managed_directory_identity(
        worktree,
        managed_root=managed_root,
        expected_resolved=str(task["slot_worktree_resolved"]),
        expected_root_resolved=str(task["slot_managed_root_resolved"]),
        expected_identity=dict(task["slot_worktree_identity"]),
        expected_root_identity=dict(task["slot_managed_root_identity"]),
    )
    if not any(item.path == resolved for item in repo.worktrees()):
        raise SoloAIError("Pre-activation release worktree is no longer registered")
    return worktree, managed_root, resolved


def _preactivation_history_blob_commit(
    repo: GitRepo,
    *,
    task_base: str,
    release_head: str,
    path: str,
    worktree_blob: str,
) -> str | None:
    """只接受当前 base 第一父历史中、任务基线之后的逐文件精确副本。"""

    commits = repo.git(
        ["rev-list", "--first-parent", f"{task_base}..{release_head}"]
    ).stdout.splitlines()
    for commit in commits:
        blob = repo.git(["rev-parse", f"{commit}:{path}"], check=False)
        if blob.returncode == 0 and blob.stdout.strip() == worktree_blob:
            return commit
    return None


def _preactivation_release_paths(
    repo: GitRepo,
    worktree: Path,
    *,
    task_base: str,
    release_head: str,
) -> list[dict[str, str]]:
    if repo.git(["diff", "--cached", "--quiet"], cwd=worktree, check=False).returncode:
        raise SoloAIError(
            "Pre-activation release refuses staged content; preserve it in a new task"
        )
    changed = repo.git(["diff", "--name-only", "-z"], cwd=worktree).stdout
    paths = [path for path in changed.split("\0") if path]
    if not paths:
        raise SoloAIError(
            "Pre-activation release requires tracked dirty content; use ordinary recovery"
        )
    records: list[dict[str, str]] = []
    for path in paths:
        relative = Path(path)
        if relative.is_absolute() or ".." in relative.parts:
            raise SoloAIError("Pre-activation release found an unsafe tracked path")
        expected = repo.git(
            ["rev-parse", f"{release_head}:{path}"], cwd=worktree, check=False
        )
        if expected.returncode:
            raise SoloAIError(
                f"Pre-activation content is not present in the current base: {path}"
            )
        actual = repo.git(["hash-object", "--", path], cwd=worktree).stdout.strip()
        base_blob = expected.stdout.strip()
        accepted_commit = release_head
        acceptance = "current-base"
        if actual != base_blob:
            accepted_commit = _preactivation_history_blob_commit(
                repo,
                task_base=task_base,
                release_head=release_head,
                path=path,
                worktree_blob=actual,
            )
            acceptance = "post-baseline-first-parent"
        if accepted_commit is None:
            raise SoloAIError(
                "Dirty pre-activation content is not already accepted by current base "
                "or its post-baseline first-parent history: " + path
            )
        records.append(
            {
                "path": path,
                "worktree_blob": actual,
                "base_blob": base_blob,
                "accepted_blob": actual,
                "accepted_commit": accepted_commit,
                "acceptance": acceptance,
            }
        )
    return records


def _new_preactivation_release(
    repo: GitRepo, *, store: StateStore, task: dict[str, Any], operation_id: str
) -> dict[str, Any]:
    if not _is_dirty_preactivation_failure(task):
        raise SoloAIError("Task is not an eligible dirty pre-activation Start failure")
    worktree, managed_root, resolved = _preactivation_release_worktree(repo, task)
    if repo.branch(worktree) is not None:
        raise SoloAIError(
            "Pre-activation release requires the old slot to stay detached"
        )
    if repo.ref_head(f"refs/heads/{task['branch']}") is not None:
        raise SoloAIError("Pre-activation release found an unexpected task branch")
    if (repo.local_dir / "task-anchors" / f"{task['id']}.md").exists():
        raise SoloAIError("Pre-activation release found an unexpected task anchor")
    if repo.is_clean(worktree):
        raise SoloAIError("Pre-activation slot is clean; use ordinary Start recovery")
    if ordinary := repo.git(
        ["ls-files", "--others", "--exclude-standard"], cwd=worktree
    ).stdout.splitlines():
        raise SoloAIError(
            "Pre-activation release refuses ordinary untracked content:\n"
            + "\n".join(f"- {item}" for item in ordinary[:20])
        )
    if unknown := _unknown_ignored(repo, worktree):
        raise SoloAIError(
            "Pre-activation release found protected or unknown ignored content:\n"
            + "\n".join(f"- {item}" for item in unknown[:20])
        )
    release_head = repo.ref_head(f"refs/heads/{task['base_ref']}")
    if release_head is None:
        raise SoloAIError("Pre-activation release base branch is missing")
    if not repo.is_ancestor(str(task["base_head"]), release_head):
        raise SoloAIError(
            "Pre-activation release requires the current base to descend from task baseline"
        )
    state = store.read()
    slot = state["slots"].get(str(task["slot_id"]))
    if (
        not slot
        or slot.get("task_id") != task["id"]
        or slot.get("status") != "quarantined"
    ):
        raise SoloAIError("Pre-activation release lost its exact quarantined slot")
    return {
        "schema_version": PREACTIVATION_RELEASE_SCHEMA,
        "transaction_id": uuid.uuid4().hex,
        "phase": "prepared",
        "task_id": task["id"],
        "slot_id": task["slot_id"],
        "slot_generation": int(slot["generation"]),
        "worktree": str(worktree),
        "worktree_resolved": str(resolved),
        "managed_root": str(managed_root),
        "managed_root_resolved": str(managed_root.resolve()),
        "worktree_identity": path_identity(worktree),
        "managed_root_identity": path_identity(managed_root),
        "base_ref": task["base_ref"],
        "task_base_head": task["base_head"],
        "release_head": release_head,
        "worktree_head": repo.head(worktree),
        "tracked_status": repo.git(
            ["status", "--porcelain=v1", "--untracked-files=all"], cwd=worktree
        ).stdout,
        "tracked_files": _preactivation_release_paths(
            repo,
            worktree,
            task_base=str(task["base_head"]),
            release_head=release_head,
        ),
        "prepared_by_operation_id": operation_id,
        "prepared_at": utc_timestamp(),
    }


def _assert_preactivation_release_transaction(
    repo: GitRepo, *, store: StateStore, task: dict[str, Any]
) -> tuple[dict[str, Any], Path]:
    transaction = task.get("preactivation_release")
    if not isinstance(transaction, dict):
        raise SoloAIError("Pre-activation release transaction is missing")
    expected = {
        "task_id": task["id"],
        "slot_id": task["slot_id"],
        "worktree": task["worktree"],
        "base_ref": task["base_ref"],
        "task_base_head": task["base_head"],
    }
    if transaction.get("schema_version") != PREACTIVATION_RELEASE_SCHEMA:
        raise SoloAIError("Unsupported pre-activation release transaction schema")
    if transaction.get("phase") not in {"prepared", "reset", "completed"}:
        raise SoloAIError("Unsupported pre-activation release transaction phase")
    for key, value in expected.items():
        if transaction.get(key) != value:
            raise SoloAIError(f"Pre-activation release identity changed: {key}")
    worktree = Path(str(transaction["worktree"]))
    resolved = require_managed_directory_identity(
        worktree,
        managed_root=Path(str(transaction["managed_root"])),
        expected_resolved=str(transaction["worktree_resolved"]),
        expected_root_resolved=str(transaction["managed_root_resolved"]),
        expected_identity=dict(transaction["worktree_identity"]),
        expected_root_identity=dict(transaction["managed_root_identity"]),
    )
    if not any(item.path == resolved for item in repo.worktrees()):
        raise SoloAIError("Pre-activation release worktree is missing or unregistered")
    slot = store.read()["slots"].get(str(transaction["slot_id"]))
    if not slot or int(slot.get("generation", -1)) != int(
        transaction["slot_generation"]
    ):
        raise SoloAIError("Pre-activation release slot generation changed")
    return transaction, worktree


def _assert_preactivation_content_unchanged(
    repo: GitRepo, *, transaction: dict[str, Any], worktree: Path
) -> None:
    if (
        repo.ref_head(f"refs/heads/{transaction['base_ref']}")
        != transaction["release_head"]
    ):
        raise SoloAIError("Pre-activation release base head changed during recovery")
    if (
        repo.branch(worktree) is not None
        or repo.head(worktree) != transaction["worktree_head"]
    ):
        raise SoloAIError(
            "Pre-activation release worktree HEAD changed during recovery"
        )
    current_status = repo.git(
        ["status", "--porcelain=v1", "--untracked-files=all"], cwd=worktree
    ).stdout
    if current_status != transaction["tracked_status"]:
        raise SoloAIError(
            "Pre-activation release file fingerprint changed during recovery"
        )
    if repo.git(["diff", "--cached", "--quiet"], cwd=worktree, check=False).returncode:
        raise SoloAIError(
            "Pre-activation release received staged content during recovery"
        )
    if repo.git(["ls-files", "--others", "--exclude-standard"], cwd=worktree).stdout:
        raise SoloAIError(
            "Pre-activation release received untracked content during recovery"
        )
    if _unknown_ignored(repo, worktree):
        raise SoloAIError("Pre-activation release received protected ignored content")
    for item in transaction["tracked_files"]:
        path = str(item["path"])
        actual = repo.git(["hash-object", "--", path], cwd=worktree).stdout.strip()
        if actual != item["worktree_blob"] or actual != item["accepted_blob"]:
            raise SoloAIError(
                "Pre-activation release file fingerprint changed during recovery"
            )
        accepted_commit = str(item["accepted_commit"])
        acceptance = str(item["acceptance"])
        if acceptance == "current-base":
            if accepted_commit != transaction["release_head"]:
                raise SoloAIError(
                    "Pre-activation release current-base evidence changed"
                )
        elif acceptance == "post-baseline-first-parent":
            if not repo.is_ancestor(accepted_commit, transaction["release_head"]):
                raise SoloAIError(
                    "Pre-activation release accepted-history evidence is no longer reachable"
                )
            first_parent = repo.git(
                [
                    "rev-list",
                    "--first-parent",
                    f"{transaction['task_base_head']}..{transaction['release_head']}",
                ]
            ).stdout.splitlines()
            if accepted_commit not in first_parent:
                raise SoloAIError(
                    "Pre-activation release accepted-history evidence left the first-parent path"
                )
        else:
            raise SoloAIError("Pre-activation release has unknown acceptance evidence")
        accepted_blob = repo.git(
            ["rev-parse", f"{accepted_commit}:{path}"], check=False
        )
        if accepted_blob.returncode or accepted_blob.stdout.strip() != actual:
            raise SoloAIError(
                "Pre-activation release accepted-history evidence changed during recovery"
            )


def _assert_preactivation_release_reset(
    repo: GitRepo, *, transaction: dict[str, Any], worktree: Path
) -> None:
    if (
        repo.ref_head(f"refs/heads/{transaction['base_ref']}")
        != transaction["release_head"]
    ):
        raise SoloAIError("Pre-activation release base head changed during recovery")
    if (
        repo.branch(worktree) is not None
        or repo.head(worktree) != transaction["release_head"]
        or not repo.is_clean(worktree)
    ):
        raise SoloAIError("Pre-activation release worktree changed before finalization")
    if repo.git(["ls-files", "--others", "--exclude-standard"], cwd=worktree).stdout:
        raise SoloAIError("Pre-activation release found untracked content after reset")
    if _unknown_ignored(repo, worktree):
        raise SoloAIError(
            "Pre-activation release found protected ignored content after reset"
        )


def _resume_preactivation_release(
    repo: GitRepo, *, store: StateStore, task: dict[str, Any]
) -> dict[str, Any]:
    transaction, worktree = _assert_preactivation_release_transaction(
        repo, store=store, task=task
    )
    phase = transaction["phase"]
    if task.get("status") == "preactivation-releasing":
        if phase == "prepared":
            if (
                repo.branch(worktree) is None
                and repo.head(worktree) == transaction["release_head"]
                and repo.is_clean(worktree)
            ):
                _assert_preactivation_release_reset(
                    repo, transaction=transaction, worktree=worktree
                )
            else:
                _assert_preactivation_content_unchanged(
                    repo, transaction=transaction, worktree=worktree
                )
                repo.git(
                    ["reset", "--hard", str(transaction["release_head"])], cwd=worktree
                )
                _assert_preactivation_release_reset(
                    repo, transaction=transaction, worktree=worktree
                )
            task = store.mark_preactivation_release_reset(
                task["id"], transaction_id=str(transaction["transaction_id"])
            )
            transaction = dict(task["preactivation_release"])
            phase = transaction["phase"]
        if phase == "reset":
            _assert_preactivation_release_reset(
                repo, transaction=transaction, worktree=worktree
            )
            task = store.complete_preactivation_release(
                task["id"], transaction_id=str(transaction["transaction_id"])
            )
            transaction = dict(task["preactivation_release"])
    if task.get("status") != "abandoned" or transaction.get("phase") != "completed":
        raise SoloAIError(
            "Pre-activation release did not reach a resumable terminal state"
        )
    _assert_preactivation_release_reset(
        repo, transaction=transaction, worktree=worktree
    )
    slot = store.read()["slots"].get(str(task["slot_id"]))
    if (
        slot
        and slot.get("status") == "release-checking"
        and slot.get("task_id") == task["id"]
    ):
        store.publish_preactivation_release(
            task["id"], transaction_id=str(transaction["transaction_id"])
        )
    elif not (slot and slot.get("status") == "idle" and slot.get("task_id") is None):
        raise SoloAIError("Pre-activation release slot changed before publication")
    return _preactivation_release_result(task)


def start(
    repo: GitRepo,
    *,
    name: str,
    base: str | None = None,
    in_place: bool = False,
    bind_branch: str | None = None,
    session_id: str | None = None,
    request_id: str | None = None,
    supersedes: str | None = None,
    root_anchor_id: str | None = None,
    root_anchor_file: Path | None = None,
    independent_reason: str | None = None,
    target: str | None = None,
    scope: str | None = None,
    acceptance: str | None = None,
    host_origin: dict[str, str] | None = None,
) -> dict[str, Any]:
    with maintenance_lock(repo):
        config, _, _ = _config_and_mode(repo)
        host_origin = normalize_host_reference(host_origin)
        anchor_contract = initial_anchor_contract(
            name=name,
            target=target,
            scope=scope,
            acceptance=acceptance,
        )
        store = StateStore(repo)
        store.ensure_slots(config)
        if root_anchor_file is not None and root_anchor_id is None:
            raise SoloAIError("--root-anchor-file requires --root-anchor")
        if independent_reason is not None and (
            not independent_reason.strip()
            or "\r" in independent_reason
            or "\n" in independent_reason
        ):
            raise SoloAIError("Independent task reason must be a non-empty single line")
        if independent_reason is not None and root_anchor_id is not None:
            raise SoloAIError(
                "An independent task cannot also supply a root anchor reference"
            )
        host_context = (
            host_root_context(repo, host_origin=host_origin)
            if host_origin is not None
            else None
        )
        if host_context is not None and host_context.get("status") == "unverifiable":
            raise ActionableSoloAIError(
                "The exact host objective cannot be verified. Preserve the existing association and restore or verify its root before starting a writable task.",
                code="ROOT_BINDING_UNVERIFIABLE",
                context={"reason": str(host_context["reason"])},
                next_action=host_context["next_action"],
            )
        host_binding = (
            host_context.get("binding")
            if host_context is not None and host_context.get("associated")
            else None
        )
        if host_binding is not None and host_binding.get("status") != "available":
            raise ActionableSoloAIError(
                "This host objective is still being registered. Retry the same root-anchor create request before starting a writable task.",
                code="ROOT_BINDING_PENDING",
                context={"root_id": str(host_binding["root_anchor_id"])},
                next_action={"kind": "retry_root_create", "retry": "after_action"},
            )
        if independent_reason is not None and host_binding is None:
            raise SoloAIError(
                "An independent task reason is only valid when this exact host has an active objective"
            )
        if (
            root_anchor_id is None
            and host_binding is not None
            and independent_reason is None
        ):
            root_anchor_id = str(host_binding["root_anchor_id"])
            root_anchor_file = (
                Path(str(host_binding["root_anchor_file"]))
                if host_binding.get("root_anchor_file")
                else None
            )
        elif root_anchor_id is not None and host_binding is not None:
            bound_file = host_binding.get("root_anchor_file")
            supplied_file = (
                str(root_anchor_file.resolve()) if root_anchor_file else None
            )
            if (
                host_binding.get("root_anchor_id") != root_anchor_id
                or bound_file != supplied_file
            ):
                raise SoloAIError(
                    "The supplied root anchor conflicts with this host's active objective"
                )
        root_binding: dict[str, Any] | None = None
        external_root_file: str | None = None
        if root_anchor_id is not None:
            root_binding = resolve_root_anchor(
                repo, root_id=root_anchor_id, external_path=root_anchor_file
            )
            if root_anchor_file is not None:
                external_root_file = str(root_binding["root_anchor_path"])
        root_binding_protocol = (
            ROOT_BINDING_PROTOCOL_VERSION if root_anchor_id is not None else 0
        )
        if in_place:
            if root_anchor_file is not None:
                raise SoloAIError(
                    "In-place tasks cannot bind an external root anchor; use an isolated managed task"
                )
            if request_id or supersedes:
                raise SoloAIError(
                    "In-place tasks do not support managed request or candidate-repair identities"
                )
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
                if bind_branch and not bind_branch.startswith(config.branch_prefix):
                    raise SoloAIError(
                        "--bind-branch must use this repository's configured task branch prefix"
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
                if branch is None and bind_branch:
                    registered = next(
                        (item for item in repo.worktrees() if item.path == repo.root),
                        None,
                    )
                    if registered is None or not registered.detached:
                        raise SoloAIError(
                            "--bind-branch is limited to a registered detached linked worktree"
                        )
                    if repo.root == repo.primary_path:
                        raise SoloAIError(
                            "--bind-branch cannot attach the primary worktree"
                        )
                    checked = repo.git(
                        ["check-ref-format", "--branch", bind_branch], check=False
                    )
                    if checked.returncode != 0 or checked.stdout.strip() != bind_branch:
                        raise SoloAIError(
                            "--bind-branch must be a valid local branch name"
                        )
                    ref = f"refs/heads/{bind_branch}"
                    if any(item.branch == ref for item in repo.worktrees()):
                        raise SoloAIError(
                            "--bind-branch is already checked out by another worktree"
                        )
                    head = repo.head(repo.root)
                    existing = repo.ref_head(ref)
                    if existing is not None and existing != head:
                        raise SoloAIError(
                            "--bind-branch already points at a different commit; preserve it"
                        )
                    switch_args = (
                        ["switch", bind_branch]
                        if existing
                        else ["switch", "-c", bind_branch]
                    )
                    repo.git(switch_args, cwd=repo.root)
                    branch = repo.branch(repo.root)
                    if branch != bind_branch or repo.head(repo.root) != head:
                        raise SoloAIError(
                            "Detached worktree branch binding did not preserve its exact HEAD"
                        )
                elif bind_branch:
                    raise SoloAIError(
                        "--bind-branch is only valid while the current worktree is detached"
                    )
                if branch is None:
                    raise SoloAIError(
                        "In-place tasks require an attached local branch; a trusted detached linked worktree may use --bind-branch <task-branch>"
                    )
                if not repo.is_clean(repo.root):
                    raise SoloAIError(
                        "In-place Start requires a clean Git worktree; ignored test data may remain, but tracked or untracked changes must be preserved and handled first"
                    )
                with candidate_admission_lock(repo):
                    task = store.allocate_in_place(
                        name=name,
                        branch=branch,
                        head=repo.head(repo.root),
                        base_worktree=repo.root,
                        session_id=session_id,
                        anchor_contract=anchor_contract,
                        root_anchor_id=root_anchor_id,
                        root_anchor_file=external_root_file,
                        expected_root_anchor_id=root_anchor_id,
                        expected_root_anchor_file=external_root_file,
                        root_binding_protocol=root_binding_protocol,
                        root_binding_exception=(
                            independent_reason.strip()
                            if independent_reason is not None
                            else None
                        ),
                    )
                anchor = create_anchor(repo, task)
                return {
                    **task,
                    "anchor_path": str(anchor.resolve()),
                    **_read_root_context(repo, store=store, task=task),
                }
        if supersedes and config.integration.mode != "batched":
            raise SoloAIError("--supersedes requires integration.mode = batched")
        with candidate_admission_lock(repo):
            base_ref, base_head, base_worktree = _resolve_start_base(repo, store, base)
            branch = f"{config.branch_prefix}{safe_slug(name)}-{uuid.uuid4().hex[:6]}"
            task = store.allocate(
                config,
                name=name,
                branch=branch,
                base_head=base_head,
                base_ref=base_ref,
                base_worktree=base_worktree,
                anchor_contract=anchor_contract,
                request_id=request_id,
                supersedes=supersedes,
                root_anchor_id=root_anchor_id,
                root_anchor_file=external_root_file,
                expected_root_anchor_id=root_anchor_id,
                expected_root_anchor_file=external_root_file,
                root_binding_protocol=root_binding_protocol,
                root_binding_exception=(
                    independent_reason.strip()
                    if independent_reason is not None
                    else None
                ),
                host_origin=host_origin,
            )
        if external_root_file is not None:
            try:
                register_external_root_child(
                    root_id=str(root_anchor_id),
                    root_anchor_file=Path(external_root_file),
                    task_id=str(task["id"]),
                    child_state_path=store.path,
                )
            except Exception:
                if not task.get("request_reused"):
                    store.update_task(
                        str(task["id"]), root_anchor_id=None, root_anchor_file=None
                    )
                    store.quarantine(
                        str(task["id"]), "external root anchor registration failed"
                    )
                raise
        if task.get("request_reused"):
            if task.get("status") == "quarantined":
                return _resume_quarantined_start(
                    repo,
                    store=store,
                    task=task,
                    operation_id=None,
                    request_reused=True,
                )
            if task.get("status") == "starting" and task.get(
                "runtime_activation_pending"
            ):
                create_anchor(repo, task)
                return _complete_runtime_activation(repo, store=store, task=task)
            anchor = require_anchor(repo, task)
            return {
                **task,
                "anchor_path": str(anchor.resolve()),
                **_read_root_context(repo, store=store, task=task),
            }
        worktree = ensure_within(
            Path(task["worktree"]), store.managed_worktree_root(config)
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
            task = store.update_task(
                task["id"],
                slot_worktree_identity=activation_worktree_identity,
                slot_managed_root_identity=activation_root_identity,
                slot_worktree_resolved=str(worktree.resolve()),
                slot_managed_root_resolved=str(managed_root.resolve()),
            )
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
            prepared = store.update_task(
                task["id"],
                candidate_head=repo.head(worktree),
                baseline_paths=repo.changed_paths(worktree),
                slot_worktree_identity=activation_worktree_identity,
                slot_managed_root_identity=activation_root_identity,
                slot_worktree_resolved=str(resolved),
                slot_managed_root_resolved=str(managed_root.resolve()),
                runtime_activation_pending=True,
            )
            create_anchor(repo, prepared)
        except Exception as exc:
            store.quarantine(task["id"], str(exc))
            raise
        return _complete_runtime_activation(repo, store=store, task=prepared)


def _path_is_safe(path: str) -> bool:
    candidate = Path(path)
    return (
        not candidate.is_absolute()
        and ".." not in candidate.parts
        and path not in {"", "."}
    )


def _is_in_place(task: dict[str, Any]) -> bool:
    return StateStore.mode(task) == IN_PLACE_MODE


def _candidate_requires_ready(task: dict[str, Any]) -> bool:
    """旧任务和直接集成保留 Ready；新批次只在组合后验收。"""

    policy = task.get("integration_policy") or {}
    return (
        policy.get("mode") != "batched"
        or policy.get("candidate_validation", "ready") != "batch"
    )


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
    _, _, _ = _config_and_mode(repo)
    store = StateStore(repo)
    with store.operation(task_id, lease, "commit") as task:
        worktree = Path(task["worktree"])
        if _is_in_place(task):
            if task.get("status") not in {"active", "ready"}:
                raise SoloAIError("In-place Commit requires an active or ready task")
            _assert_in_place_binding(repo, store, task, session_id=session_id)
        elif task.get("status") not in {"active", "ready"}:
            raise SoloAIError(
                "Commit requires a task whose runtime activation has completed"
            )
        _require_current_structured_root_review(repo, task, store=store)
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
        merge_head = repo.git(
            ["rev-parse", "--verify", "-q", "MERGE_HEAD"],
            cwd=worktree,
            check=False,
        )
        changed = set(repo.changed_paths(worktree))
        requested = set(paths)
        repair = task.get("runtime_adapter_repair") or {}
        if repair:
            allowed_paths = set(repair.get("allowed_paths") or ())
            disallowed = sorted(requested - allowed_paths)
            if disallowed:
                raise SoloAIError(
                    "Runtime Adapter repair may only commit its approved config and input_paths:\n"
                    + "\n".join(f"- {path}" for path in disallowed)
                )
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
        staged_changes = set(repo.changed_paths(worktree))
        if staged_changes != requested and not (
            merge_head.returncode == 0 and staged_changes.issubset(requested)
        ):
            raise SoloAIError(
                "Task changes changed while staging; inspect the task diff and retry"
            )
        task_config = load_repo_config(repo, cwd=worktree)
        require_approval(
            repo,
            load_verification_config(repo, cwd=worktree),
            cwd=worktree,
            scope="commit",
            include_secret_scanner=task_config.secret_scanner is not None,
            approval_target={"task": task_id},
        )
        _run_declared_secret_scanner(
            repo, cwd=worktree, scanner=task_config.secret_scanner
        )
        require_safe(
            repo,
            cwd=worktree,
            base=None,
            staged=True,
            allowlist=task_config.sensitive_allowlist,
        )
        if merge_head.returncode == 0:
            preparation = task.get("repair_preparation") or {}
            merge_head_value = merge_head.stdout.strip()
            repair_merge = bool(
                task.get("supersedes")
                and merge_head_value == preparation.get("source_head")
            )
            current_base_head = repo.ref_head(
                f"refs/heads/{task['base_ref']}", cwd=worktree
            )
            base_merge = bool(
                current_base_head
                and merge_head_value == current_base_head
                and repo.is_ancestor(
                    str(task["base_head"]), current_base_head, cwd=worktree
                )
            )
            if not repair_merge and not base_merge:
                raise SoloAIError(
                    "Only the current recorded base or a recorded candidate repair may complete a prepared merge"
                )
            unresolved = repo.git(
                ["diff", "--name-only", "--diff-filter=U"],
                cwd=worktree,
                check=False,
            ).stdout.splitlines()
            if unresolved:
                raise SoloAIError(
                    "Prepared merge still has unresolved paths:\n"
                    + "\n".join(f"- {path}" for path in unresolved)
                )
            repo.git(["commit", "-m", message], cwd=worktree)
        else:
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
    path = Path(str(value))
    stored_resolved = task.get("base_worktree_resolved")
    stored_identity = task.get("base_worktree_identity")
    if (stored_resolved is None) != (stored_identity is None):
        raise SoloAIError(
            "Recorded base worktree has incomplete identity; explicitly retarget"
        )
    if stored_resolved is not None and str(path.resolve()) != str(stored_resolved):
        raise SoloAIError("Recorded base worktree path changed; explicitly retarget")
    identity = dict(stored_identity) if isinstance(stored_identity, dict) else None
    path = repo.require_checked_out_branch_target(
        path,
        str(task["base_ref"]),
        expected_identity=identity,
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
        require_anchor(repo, task, require_verified_origin=True)
        worktree = Path(task["worktree"])
        if task.get("status") not in {"active", "ready"}:
            raise SoloAIError(f"Task cannot enter Ready from {task.get('status')}")
        if _is_in_place(task):
            _assert_in_place_binding(repo, store, task, session_id=session_id)
        _require_current_structured_root_review(repo, task, store=store)
        if not repo.is_clean(worktree):
            raise SoloAIError("Commit all task changes before Ready")
        convergence_retries = 0
        while True:
            if not _is_in_place(task):
                task = _sync_base(repo, task)
            if not repo.is_clean(worktree):
                raise SoloAIError("Base-branch synchronization left the task dirty")
            expected_candidate_head = repo.head(worktree)
            _assert_exact_candidate(repo, task, candidate_head=expected_candidate_head)
            # 每次同步都可能带入新的受管策略；必须按本轮候选重新确认和验证。
            config = load_repo_config(repo, cwd=worktree)
            store.require_slot_layout(config)
            verification = load_verification_config(repo, cwd=worktree)
            base_ref = _verification_base(task)
            _require_validation_approval(
                repo,
                cwd=worktree,
                base=base_ref,
                verification=verification,
                level="ready",
                full_scope=None,
                scope="ready",
                include_secret_scanner=config.secret_scanner is not None,
                approval_target={"task": task_id},
            )
            _run_declared_secret_scanner(
                repo, cwd=worktree, scanner=config.secret_scanner
            )
            _assert_exact_candidate(repo, task, candidate_head=expected_candidate_head)
            require_safe(
                repo, cwd=worktree, base=base_ref, allowlist=config.sensitive_allowlist
            )
            _assert_exact_candidate(repo, task, candidate_head=expected_candidate_head)
            expected_base_head = None if _is_in_place(task) else str(task["base_head"])
            attempt_id = new_validation_attempt_id("ready")
            attempts = [
                str(item)
                for item in task.get("validation_attempts", [])
                if isinstance(item, str) and item
            ]
            attempts.append(attempt_id)
            task = store.update_task(
                task_id,
                validation_attempt=attempt_id,
                validation_attempts=attempts,
                # _sync_base 只更新本轮内存任务；记录验证尝试时必须一并
                # 持久化已经同步的基线，否则验证后的收敛检查会重新读取旧值，
                # 将已稳定的基线误判为再次推进。
                base_head=task["base_head"],
                candidate_head=expected_candidate_head,
            )
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
                    validation_base_ref=str(task["base_ref"]),
                    attempt_id=attempt_id,
                    attempt_owner={"kind": "task", "id": task_id},
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
    inventory = inspect_untracked(repo, cwd=worktree)
    return sorted(
        {*inventory["keep"], *inventory["protected"], *inventory["unknown_ignored"]}
    )


def _unknown_content_error(
    *, task_id: str, phase: str, paths: list[str]
) -> ActionableSoloAIError:
    """未知或受保护内容必须保留，并给调用方一个不会扩大清理范围的下一步。"""

    return ActionableSoloAIError(
        f"Unknown or protected ignored files block {phase}:\n"
        + "\n".join(f"- {item}" for item in paths[:20]),
        code="UNKNOWN_CONTENT",
        context={"task_id": task_id, "phase": phase, "paths": paths[:20]},
        next_action={
            "kind": "preserve_and_inspect_worktree",
            "task_id": task_id,
        },
    )


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
    managed_root = StateStore(repo).managed_worktree_root(config)
    for slot in state["slots"].values():
        path = ensure_within(Path(slot["path"]), managed_root)
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


def _pending_bootstrap_target(
    repo: GitRepo, pending: dict[str, Any]
) -> tuple[Path, str, str, dict[str, Any] | None]:
    """恢复 bootstrap 的原调用目标，schema-1 只接受可唯一验证的事实。"""

    branch = str(pending.get("branch") or "")
    bootstrap_head = str(pending.get("bootstrap_head") or "")
    if not branch or not bootstrap_head:
        raise SoloAIError(
            "Pending bootstrap lacks its branch identity; preserve it and run doctor"
        )
    if repo.ref_head(f"refs/heads/{branch}") != bootstrap_head:
        raise SoloAIError(
            "Pending bootstrap branch changed or disappeared; preserve it for inspection"
        )

    target_fields = (
        "target_ref",
        "target_head",
        "target_worktree",
        "target_worktree_resolved",
        "target_worktree_identity",
    )
    present = [field for field in target_fields if pending.get(field) is not None]
    if present and len(present) != len(target_fields):
        raise SoloAIError(
            "Pending bootstrap has incomplete recorded target identity; preserve it and run doctor"
        )
    if len(present) == len(target_fields):
        target = Path(str(pending["target_worktree"]))
        expected_resolved = str(pending["target_worktree_resolved"])
        if str(target.resolve()) != expected_resolved:
            raise SoloAIError(
                "Pending bootstrap target path changed; preserve it for inspection"
            )
        target_ref = str(pending["target_ref"])
        target_head = str(pending["target_head"])
        identity = pending["target_worktree_identity"]
        if not isinstance(identity, dict):
            raise SoloAIError(
                "Pending bootstrap target identity is unreadable; preserve it and run doctor"
            )
        return target, target_ref, target_head, identity

    # schema-1 只记录了默认分支和 bootstrap 提交。它的父提交是唯一可验证的
    # 原始基线；分支必须恰好附着在一个本地工作树上，不能回退到名称猜测。
    target_ref = str(pending.get("default_branch") or "")
    if not target_ref:
        raise SoloAIError(
            "Legacy pending bootstrap has no target branch; preserve it and run doctor"
        )
    matches = [
        item.path
        for item in repo.worktrees()
        if not item.bare and repo.branch(item.path) == target_ref
    ]
    if len(matches) != 1:
        raise SoloAIError(
            "Legacy pending bootstrap target is not uniquely checked out; preserve it and run doctor"
        )
    parent = repo.git(["rev-parse", "--verify", f"{branch}^"], check=False)
    if parent.returncode != 0:
        raise SoloAIError(
            "Legacy pending bootstrap has no verifiable target baseline; preserve it and run doctor"
        )
    return matches[0], target_ref, parent.stdout.strip(), None


def _integrate_pending_bootstrap(repo: GitRepo) -> dict[str, Any] | None:
    pending = _bootstrap(repo)
    if not pending:
        return None
    branch = str(pending["branch"])
    target, target_ref, target_head, target_identity = _pending_bootstrap_target(
        repo, pending
    )
    target = repo.require_checked_out_branch_target(
        target,
        target_ref,
        expected_head=target_head,
        expected_identity=target_identity,
    )
    if not _target_is_clean(repo, target):
        raise SoloAIError(
            "Recorded bootstrap target worktree must be clean before integration"
        )
    repo.git(["merge", "--ff-only", branch], cwd=target)
    result = {
        "bootstrap_branch": branch,
        "base_ref": target_ref,
        "base_head": repo.head(target),
        "base_worktree": str(target.resolve()),
        "base_worktree_resolved": str(target.resolve()),
        "base_worktree_identity": path_identity(target),
    }
    worktree = Path(str(pending["worktree"]))
    if worktree.exists():
        repo.git(["worktree", "remove", str(worktree)], cwd=target)
    repo.git(["branch", "-d", branch], cwd=target)
    (repo.local_dir / "bootstrap.json").unlink(missing_ok=True)
    return result


def create_root_task_anchor(
    repo: GitRepo,
    *,
    purpose: str,
    target: str,
    scope: str,
    acceptance: str,
    base: str | None = None,
    plan_input_path: Path | None = None,
    plan_source: str | None = None,
    request_id: str | None = None,
    acceptance_index_input_path: Path | None = None,
    host_origin: dict[str, str] | None = None,
    include_content: bool = True,
) -> dict[str, Any]:
    """创建主会话长期执行合同；它不领取工作树也不创建候选。"""

    _config_and_mode(repo)
    host_origin = normalize_host_reference(host_origin)
    if host_origin is not None and not request_id:
        raise SoloAIError(
            "A host-associated confirmed objective requires a stable request id"
        )
    with maintenance_lock(repo):
        base_ref = base or repo.branch(repo.root)
        if not base_ref:
            raise SoloAIError("Root anchor requires an attached local base branch")
        base_head = repo.ref_head(f"refs/heads/{base_ref}")
        if not base_head:
            raise SoloAIError("Root anchor base branch no longer exists")
        confirmed_plan = (
            read_root_plan_input(repo, plan_input_path)
            if plan_input_path is not None
            else None
        )
        root_id = (
            root_id_for_request(request_id)
            if request_id is not None
            else f"root-{time.strftime('%Y%m%d%H%M%S', time.gmtime())}-{uuid.uuid4().hex[:8]}"
        )
        store = StateStore(repo)
        existing_host_binding = store.host_root_binding(host_origin)
        request_fingerprint = sha256_text(request_id) if request_id else None
        acceptance_index = (
            read_root_acceptance_index_input(repo, acceptance_index_input_path)
            if acceptance_index_input_path is not None
            else None
        )
        if host_origin is not None and acceptance_index is None:
            raise SoloAIError(
                "A host-associated confirmed objective requires an acceptance index"
            )
        if existing_host_binding is not None and (
            existing_host_binding.get("root_anchor_id") != root_id
            or existing_host_binding.get("root_anchor_file") is not None
            or existing_host_binding.get("request_fingerprint") != request_fingerprint
        ):
            raise SoloAIError(
                "Host is already associated with a different active root anchor"
            )
        if host_origin is not None:
            assert request_fingerprint is not None
            store.begin_host_root_registration(
                host_origin,
                root_anchor_id=root_id,
                root_anchor_file=None,
                request_fingerprint=request_fingerprint,
            )
        result = create_root_anchor(
            repo,
            root_id=root_id,
            purpose=purpose,
            target=target,
            base_ref=base_ref,
            base_head=base_head,
            scope=scope,
            acceptance=acceptance,
            confirmed_plan=confirmed_plan,
            plan_source=plan_source,
            request_id=request_id,
            acceptance_index=acceptance_index,
            objective_protocol_version=1 if host_origin is not None else 0,
        )
        if host_origin is not None:
            store.complete_host_root_registration(
                host_origin,
                root_anchor_id=root_id,
                root_anchor_file=None,
                request_fingerprint=str(request_fingerprint),
            )
        return {
            **_anchor_view(result, include_content=include_content),
            "host_root_binding": (
                store.host_root_binding(host_origin)
                if host_origin is not None
                else None
            ),
        }


def bind_host_root_anchor(
    repo: GitRepo,
    *,
    root_id: str,
    host_origin: dict[str, str] | None,
    root_anchor_file: Path | None = None,
) -> dict[str, Any]:
    """把一个已核对的本地或外部根关联到精确宿主，供续作恢复使用。"""

    _config_and_mode(repo)
    normalized_host = normalize_host_reference(host_origin)
    if normalized_host is None:
        raise SoloAIError("Binding a root to a host requires an exact host reference")
    with maintenance_lock(repo):
        root = resolve_root_anchor(
            repo, root_id=root_id, external_path=root_anchor_file
        )
        external_root_file = (
            str(root["root_anchor_path"]) if root_anchor_file is not None else None
        )
        binding = StateStore(repo).bind_host_root(
            normalized_host,
            root_anchor_id=root_id,
            root_anchor_file=external_root_file,
            request_fingerprint=sha256_text(
                f"explicit-host-root:{root_id}:{external_root_file or 'local'}"
            ),
        )
        return {
            "root_id": root_id,
            "root_anchor_path": str(root["root_anchor_path"]),
            "host_root_binding": binding,
        }


def host_root_context(
    repo: GitRepo, *, host_origin: dict[str, str] | None
) -> dict[str, Any]:
    """查询一个精确宿主当前可用的目标关联；默认不返回完整方案。"""

    _config_and_mode(repo)
    normalized_host = normalize_host_reference(host_origin)
    if normalized_host is None:
        raise SoloAIError("Host root context requires an exact host reference")
    store = StateStore(repo)
    binding = store.host_root_binding(normalized_host)
    if binding is None:
        return {
            "host_origin": normalized_host,
            "associated": False,
            "next_action": {"kind": "create_or_bind_root"},
        }
    root_file = binding.get("root_anchor_file")
    try:
        root = resolve_root_anchor(
            repo,
            root_id=str(binding["root_anchor_id"]),
            external_path=Path(str(root_file)) if root_file else None,
        )
    except SoloAIError as exc:
        receipt = (
            read_external_root_close_receipt(
                Path(str(root_file)), root_id=str(binding["root_anchor_id"])
            )
            if root_file
            else read_root_close_receipt(repo, root_id=str(binding["root_anchor_id"]))
        )
        if receipt is not None:
            store.clear_host_root_binding(
                normalized_host,
                root_anchor_id=str(binding["root_anchor_id"]),
                root_anchor_file=str(root_file),
            )
            return {
                "host_origin": normalized_host,
                "associated": False,
                "closed_root_receipt": receipt,
                "next_action": {"kind": "create_or_bind_root"},
            }
        return {
            "host_origin": normalized_host,
            "associated": True,
            "binding": binding,
            "status": "unverifiable",
            "reason": str(exc),
            "next_action": {"kind": "restore_or_verify_root"},
        }
    return {
        "host_origin": normalized_host,
        "associated": True,
        "binding": binding,
        "status": str(binding["status"]),
        "root": _anchor_view(root, include_content=False),
        "next_action": (
            {"kind": "retry_root_create"}
            if binding["status"] == "registering"
            else {"kind": "start_or_refresh"}
        ),
    }


def show_root_task_anchor(
    repo: GitRepo,
    *,
    root_id: str,
    version: int | None = None,
    include_content: bool = True,
) -> dict[str, Any]:
    _config_and_mode(repo)
    result = (
        show_root_anchor_history(repo, root_id=root_id, version=version)
        if version is not None
        else show_root_anchor(repo, root_id=root_id)
    )
    return _anchor_view(result, include_content=include_content)


def update_root_task_anchor(
    repo: GitRepo,
    *,
    root_id: str,
    input_path: Path,
    expected_sha256: str,
    include_content: bool = True,
) -> dict[str, Any]:
    _config_and_mode(repo)
    with maintenance_lock(repo):
        result = update_root_anchor(
            repo,
            root_id=root_id,
            input_path=input_path,
            expected_sha256=expected_sha256,
        )
        return _anchor_view(result, include_content=include_content)


def amend_root_task_anchor(
    repo: GitRepo,
    *,
    root_id: str,
    plan_input_path: Path | None,
    change_input_path: Path | None,
    source: str,
    summary: str,
    expected_sha256: str,
    target: str | None = None,
    scope: str | None = None,
    acceptance: str | None = None,
    acceptance_index_input_path: Path | None = None,
    include_content: bool = True,
) -> dict[str, Any]:
    _config_and_mode(repo)
    with maintenance_lock(repo):
        result = amend_root_anchor(
            repo,
            root_id=root_id,
            confirmed_plan=(
                read_root_plan_input(repo, plan_input_path)
                if plan_input_path is not None
                else None
            ),
            change_text=(
                read_root_change_input(repo, change_input_path)
                if change_input_path is not None
                else None
            ),
            source=source,
            summary=summary,
            expected_sha256=expected_sha256,
            target=target,
            scope=scope,
            acceptance=acceptance,
            acceptance_index=(
                read_root_acceptance_index_input(repo, acceptance_index_input_path)
                if acceptance_index_input_path is not None
                else None
            ),
        )
        return _anchor_view(result, include_content=include_content)


def update_root_task_progress(
    repo: GitRepo,
    *,
    root_id: str,
    progress: str,
    expected_sha256: str,
    include_content: bool = True,
) -> dict[str, Any]:
    _config_and_mode(repo)
    with maintenance_lock(repo):
        result = update_root_progress(
            repo,
            root_id=root_id,
            progress=progress,
            expected_sha256=expected_sha256,
        )
        return _anchor_view(result, include_content=include_content)


def record_root_task_acceptance(
    repo: GitRepo,
    *,
    root_id: str,
    status: str,
    expected_sha256: str,
    evidence_input_path: Path | None = None,
    evidence_json: str | None = None,
    include_content: bool = True,
) -> dict[str, Any]:
    if (evidence_input_path is None) == (evidence_json is None):
        raise SoloAIError(
            "Provide exactly one acceptance evidence file or inline JSON value"
        )
    _config_and_mode(repo)
    with maintenance_lock(repo):
        evidence = (
            evidence_json
            if evidence_json is not None
            else read_root_acceptance_evidence_input(repo, evidence_input_path)
        )
        result = record_root_acceptance(
            repo,
            root_id=root_id,
            status=status,
            evidence=evidence,
            expected_sha256=expected_sha256,
            require_structured_evidence=evidence_json is not None,
        )
        return _anchor_view(result, include_content=include_content)


def reindex_root_task_acceptance(
    repo: GitRepo,
    *,
    root_id: str,
    index_input_path: Path,
    expected_sha256: str,
    include_content: bool = True,
) -> dict[str, Any]:
    _config_and_mode(repo)
    with maintenance_lock(repo):
        result = reindex_root_acceptance(
            repo,
            root_id=root_id,
            acceptance_index=read_root_acceptance_index_input(repo, index_input_path),
            expected_sha256=expected_sha256,
        )
        return _anchor_view(result, include_content=include_content)


def upgrade_root_task_to_objective_protocol(
    repo: GitRepo,
    *,
    root_id: str,
    index_input_path: Path,
    expected_sha256: str,
    include_content: bool = True,
) -> dict[str, Any]:
    _config_and_mode(repo)
    with maintenance_lock(repo):
        result = upgrade_root_to_objective_protocol(
            repo,
            root_id=root_id,
            acceptance_index=read_root_acceptance_index_input(repo, index_input_path),
            expected_sha256=expected_sha256,
        )
        return _anchor_view(result, include_content=include_content)


def close_root_task_anchor(
    repo: GitRepo, *, root_id: str, confirm: str
) -> dict[str, Any]:
    _config_and_mode(repo)
    if confirm != root_id:
        raise SoloAIError("Root anchor close confirmation must equal the root id")
    with maintenance_lock(repo):
        root_path = root_anchor_path(repo, root_id)
        with root_anchor_lock(root_path):
            shown_root = show_root_anchor(repo, root_id=root_id)
            store = StateStore(repo)
            root_tasks = [
                task
                for task in store.read()["tasks"].values()
                if task.get("root_anchor_id") == root_id
            ]
            active = [
                str(task["id"])
                for task in root_tasks
                if task.get("status") not in FINAL_TASK_STATES
            ]
            published = [
                task
                for task in root_tasks
                if task.get("status") == "candidate-published"
            ]
            if published:
                from .candidate_batches import CandidateBatchStore

                candidates = CandidateBatchStore(repo).read()["candidates"]
                for task in published:
                    require_candidate_delivery_terminal(
                        candidates,
                        task_id=str(task["id"]),
                        label=f"Root child {task['id']}",
                    )
            active.extend(
                nonterminal_external_root_children(
                    root_id=root_id,
                    root_anchor_file=root_path,
                    final_states=FINAL_TASK_STATES,
                )
            )
            if active:
                raise SoloAIError(
                    "Root anchor still has nonterminal child tasks:\n"
                    + "\n".join(f"- {task_id}" for task_id in sorted(set(active)))
                )
            if shown_root.get("plan_version") is not None and shown_root.get(
                "overall_acceptance_status"
            ) not in {"accepted", "cancelled"}:
                raise SoloAIError(
                    "Root anchor requires a recorded overall acceptance result before closing"
                )
            if shown_root.get("plan_version") is not None and shown_root.get(
                "overall_acceptance_plan_version"
            ) != shown_root.get("plan_version"):
                raise SoloAIError(
                    "Root anchor acceptance must be recorded for the current plan version before closing"
                )
            if shown_root.get("objective_protocol_version") == 1 and shown_root.get(
                "overall_acceptance_index_fingerprint"
            ) != shown_root.get("acceptance_index_fingerprint"):
                raise SoloAIError(
                    "Root anchor acceptance must cover the current acceptance index before closing"
                )
            write_root_close_receipt(
                repo,
                root_id=root_id,
                plan_version=shown_root.get("plan_version"),
                acceptance_status=shown_root.get("overall_acceptance_status"),
            )
            delete_root_anchor(repo, root_id=root_id, locked=True)
            store.clear_host_root_bindings(
                root_anchor_id=root_id, root_anchor_file=None
            )
            return {"root_id": root_id, "status": "closed"}


def list_root_task_anchors(repo: GitRepo) -> dict[str, Any]:
    _config_and_mode(repo)
    return {"root_anchors": list_root_anchors(repo)}


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
        with candidate_admission_lock(repo):
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
    _require_validation_approval(
        repo,
        cwd=worktree,
        base=_verification_base(task),
        verification=verification,
        level="ready",
        full_scope=None,
        scope="finish",
        include_secret_scanner=config.secret_scanner is not None,
        approval_target={"task": str(task["id"])},
    )
    _run_declared_secret_scanner(repo, cwd=worktree, scanner=config.secret_scanner)
    _assert_in_place_binding(repo, store, task, session_id=session_id)
    require_safe(
        repo,
        cwd=worktree,
        base=_verification_base(task),
        allowlist=config.sensitive_allowlist,
    )
    _assert_in_place_binding(repo, store, task, session_id=session_id)
    attempt_id = new_validation_attempt_id("ready")
    attempts = [
        str(item)
        for item in task.get("validation_attempts", [])
        if isinstance(item, str) and item
    ]
    attempts.append(attempt_id)
    task = store.update_task(
        task["id"],
        validation_attempt=attempt_id,
        validation_attempts=attempts,
    )
    proof = validate(
        repo,
        cwd=worktree,
        base=_verification_base(task),
        verification=verification,
        task_id=task["id"],
        force_task_scope=True,
        expected_candidate_head=str(task["candidate_head"]),
        validation_base_ref=str(task["base_ref"]),
        attempt_id=attempt_id,
        attempt_owner={"kind": "task", "id": str(task["id"])},
    )
    _assert_in_place_binding(repo, store, task, session_id=session_id)
    if not repo.is_clean(worktree):
        raise SoloAIError(
            "In-place validation left tracked or nonignored changes. They were preserved; commit exact paths and run Ready again before Finish."
        )
    from .runtime_adapter import release_task_runtime

    runtime_release = release_task_runtime(repo, task=task, reason="in-place-finish")
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
    with candidate_admission_lock(repo):
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
        "runtime_release": runtime_release,
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


def _prepare_candidate_publication(
    repo: GitRepo,
    *,
    store: StateStore,
    task: dict[str, Any],
    proof: dict[str, Any] | None,
    delivery_intent: dict[str, str] | None = None,
) -> dict[str, Any]:
    active = task.get("active_operation") or {}
    operation_id = str(active.get("id") or "")
    if not operation_id:
        raise SoloAIError("Finish operation identity is missing")
    candidate_id = f"candidate-{task['id'].removeprefix('task-')}"
    worktree = Path(str(task["worktree"])).absolute()
    managed_root = worktree.parent
    publication = {
        "schema_version": 3 if delivery_intent else 2,
        "phase": "prepared",
        "candidate_id": candidate_id,
        "ref": f"refs/dww/candidates/{candidate_id}",
        "task_id": task["id"],
        "name": task["name"],
        "slot_id": task["slot_id"],
        "worktree": task["worktree"],
        "worktree_resolved": str(worktree.resolve()),
        "managed_root": str(managed_root),
        "managed_root_resolved": str(managed_root.resolve()),
        "managed_root_identity": path_identity(managed_root),
        "worktree_identity": path_identity(worktree),
        "branch": task["branch"],
        "base_ref": task["base_ref"],
        "base_head": task["base_head"],
        "base_worktree": task["base_worktree"],
        "head": task["candidate_head"],
        "proof": proof["fingerprint"] if proof else None,
        "proof_kind": proof["kind"] if proof else "source-candidate",
        "candidate_validation": "ready" if proof else "batch",
        "supersedes": task.get("supersedes"),
        "integration_policy": task.get("integration_policy"),
        "host_origin": task.get("host_origin"),
        "anchor_path": str(require_anchor(repo, task).resolve()),
        "prepared_by_operation_id": operation_id,
        "prepared_at": utc_timestamp(),
    }
    if delivery_intent:
        publication["delivery_intent"] = {
            "schema_version": 1,
            **delivery_intent,
        }
    return store.prepare_candidate_publication(
        task["id"], operation_id=operation_id, publication=publication
    )


def _restore_orphaned_ready_proof(
    repo: GitRepo, *, store: StateStore, task: dict[str, Any]
) -> dict[str, Any] | None:
    """仅恢复旧版 Finish 在发布前错误遗失的、任务专属的 Ready 证明。"""
    if (
        task.get("status") != "active"
        or task.get("ready_proof")
        or task.get("candidate_publication")
        or task.get("integration")
    ):
        return None
    candidate = str(task.get("candidate_head") or "")
    base_head = str(task.get("base_head") or "")
    if not candidate or not base_head:
        return None
    worktree = Path(str(task["worktree"]))
    if (
        not worktree.is_dir()
        or not any(item.path == worktree.resolve() for item in repo.worktrees())
        or not repo.is_clean(worktree)
        or repo.head(worktree) != candidate
        or repo.branch(worktree) != task.get("branch")
    ):
        return None
    matches: list[dict[str, Any]] = []
    for path in (repo.local_dir / "proofs").glob("*.json"):
        fingerprint = path.stem
        proof = read_json(path, {})
        try:
            require_exact_passed_proof(
                proof,
                fingerprint=fingerprint,
                candidate_head=candidate,
                base_head=base_head,
            )
        except SoloAIError:
            continue
        profiles = proof.get("profile_proofs") or []
        if not isinstance(profiles, list):
            continue
        allowed_reuse_scopes = {f"task:{task['id']}", "cross-task"}
        if any(
            read_json(
                repo.local_dir / "profile-proofs" / f"{item.get('fingerprint')}.json",
                {},
            )
            .get("inputs", {})
            .get("reuse_scope")
            not in allowed_reuse_scopes
            for item in profiles
            if isinstance(item, dict) and item.get("fingerprint")
        ):
            continue
        if any(
            not isinstance(item, dict) or not item.get("fingerprint")
            for item in profiles
        ):
            continue
        matches.append(proof)
    if len(matches) > 1:
        raise SoloAIError(
            "More than one task-scoped Ready proof matches this candidate; run Ready again"
        )
    if not matches:
        return None
    return store.update_task(
        task["id"], status="ready", ready_proof=str(matches[0]["fingerprint"])
    )


def _resume_candidate_publication(
    repo: GitRepo,
    *,
    store: StateStore,
    task: dict[str, Any],
    coordinator: dict[str, str] | None = None,
) -> dict[str, Any]:
    from .candidate_batches import CandidateBatchStore

    publication = task.get("candidate_publication") or {}
    policy = publication.get("integration_policy") or {}
    candidate_validation = str(
        publication.get("candidate_validation")
        or policy.get("candidate_validation", "ready")
    )
    required = (
        "candidate_id",
        "ref",
        "task_id",
        "worktree",
        "branch",
        "base_ref",
        "base_head",
        "head",
        "worktree_identity",
        "managed_root_identity",
        "integration_policy",
    )
    if publication.get("schema_version") not in {1, 2, 3} or any(
        not publication.get(key) for key in required
    ):
        raise SoloAIError("Candidate publication identity is incomplete")
    delivery_intent = _publication_delivery_intent(publication)
    if candidate_validation not in {"ready", "batch"}:
        raise SoloAIError("Candidate publication has an unknown validation mode")
    if candidate_validation == "ready" and not publication.get("proof"):
        raise SoloAIError("Ready-gated candidate publication lacks its proof")
    exact = {
        "task_id": task["id"],
        "worktree": task["worktree"],
        "branch": task["branch"],
        "base_ref": task["base_ref"],
        "base_head": task["base_head"],
        "head": task["candidate_head"],
        "proof": task.get("ready_proof"),
    }
    for key, value in exact.items():
        if publication.get(key) != value:
            raise SoloAIError(f"Candidate publication identity changed: {key}")
    worktree = Path(str(publication["worktree"]))
    resolved = require_managed_directory_identity(
        worktree,
        managed_root=Path(str(publication["managed_root"])),
        expected_resolved=str(publication["worktree_resolved"]),
        expected_root_resolved=str(publication["managed_root_resolved"]),
        expected_identity=dict(publication["worktree_identity"]),
        expected_root_identity=dict(publication["managed_root_identity"]),
    )
    if not any(item.path == resolved for item in repo.worktrees()):
        raise SoloAIError("Candidate worktree is no longer registered")
    head = str(publication["head"])
    branch_ref = f"refs/heads/{publication['branch']}"
    current_branch = repo.branch(worktree)
    if (
        not repo.is_clean(worktree)
        or repo.head(worktree) != head
        or current_branch not in {None, publication["branch"]}
    ):
        raise SoloAIError("Candidate worktree changed during publication")
    if unknown := _unknown_ignored(repo, worktree):
        raise _unknown_content_error(
            task_id=str(task["id"]),
            phase="candidate publication",
            paths=unknown,
        )
    load_repo_config(repo, cwd=worktree)
    if policy.get("mode") != "batched":
        raise SoloAIError("Candidate publication requires a batched task policy")
    batch_store = CandidateBatchStore(repo)
    coordinator = normalize_host_reference(coordinator)
    publication_result = batch_store.publish(
        dict(publication),
        capacity=int(policy["candidate_capacity"]),
        batch_size=int(policy["batch_size"]),
        seal_policy=str(policy["seal_policy"]),
        activate=False,
        coordinator=coordinator,
    )
    published = publication_result["candidate"]
    from .runtime_adapter import (
        release_task_runtime,
        require_exact_passed_task_runtime_release,
    )

    stored_release = publication.get("runtime_release")
    if isinstance(stored_release, dict):
        require_exact_passed_task_runtime_release(
            repo, task=task, candidate=published, receipt=stored_release
        )
        runtime_release = stored_release
    else:
        runtime_release = release_task_runtime(
            repo,
            task=task,
            reason="candidate-published",
            candidate=published,
        )
        if runtime_release.get("configured"):
            task = store.record_candidate_runtime_release(
                task["id"],
                candidate_id=str(published["candidate_id"]),
                receipt=runtime_release,
            )
    if (
        not repo.is_clean(worktree)
        or repo.head(worktree) != head
        or repo.branch(worktree) not in {None, publication["branch"]}
        or _unknown_ignored(repo, worktree)
    ):
        raise SoloAIError(
            "Runtime Adapter changed or contaminated the candidate worktree; files were preserved"
        )
    branch_head = repo.ref_head(branch_ref)
    if current_branch is not None:
        if branch_head != head:
            raise SoloAIError("Task branch changed during candidate publication")
        repo.git(["switch", "--detach", head], cwd=worktree)
    if branch_head is not None:
        repo.delete_ref(branch_ref, expected=head)
    if (
        not repo.is_clean(worktree)
        or repo.head(worktree) != head
        or repo.branch(worktree) is not None
    ):
        raise SoloAIError("Candidate worktree changed before slot release")
    completed = store.complete_candidate_publication(
        task["id"], candidate_id=str(publication["candidate_id"])
    )
    try:
        require_managed_directory_identity(
            worktree,
            managed_root=Path(str(publication["managed_root"])),
            expected_resolved=str(publication["worktree_resolved"]),
            expected_root_resolved=str(publication["managed_root_resolved"]),
            expected_identity=dict(publication["worktree_identity"]),
            expected_root_identity=dict(publication["managed_root_identity"]),
        )
        if (
            not repo.is_clean(worktree)
            or repo.head(worktree) != head
            or repo.branch(worktree) is not None
            or _unknown_ignored(repo, worktree)
        ):
            raise SoloAIError("Candidate worktree changed after slot release")
    except Exception as exc:
        store.quarantine_released_slot(
            task["id"], f"Worktree changed at candidate release: {exc}"
        )
        raise
    publication_result = batch_store.activate(
        str(publication["candidate_id"]),
        batch_size=int(policy["batch_size"]),
        seal_policy=str(policy["seal_policy"]),
        coordinator=coordinator,
    )
    published = publication_result["candidate"]
    handoff = None
    try:
        from .host_handoffs import HostHandoffStore

        handoff = HostHandoffStore(repo).record_repair_candidate_published(
            task_id=str(task["id"]), candidate=published
        )
    except (OSError, SoloAIError) as handoff_error:
        # 候选已安全发布，不能因通知记录问题回滚不可变 Git 事实；将不确定性返回给宿主。
        handoff = {"recording_error": str(handoff_error)}
    return {
        "task_id": completed["id"],
        "status": "candidate-published",
        "outcome": "candidate_published",
        "candidate_id": published["candidate_id"],
        "candidate_head": published["head"],
        "candidate_ref": published["ref"],
        "base_ref": published["base_ref"],
        "anchor_path": published["anchor_path"],
        "proof": published["proof"],
        "runtime_release": runtime_release,
        "auto_batch_id": (
            publication_result["auto_batch"]["id"]
            if publication_result.get("auto_batch")
            else None
        ),
        "seal_policy": policy["seal_policy"],
        "tail_policy": policy["tail_policy"],
        "delivery_intent": delivery_intent,
        "repair_handoff": handoff,
    }


def _normalize_finish_delivery_intent(
    *, cause: str | None, reason: str | None
) -> dict[str, str] | None:
    """把 Finish 的可选交付意图规范化为可持久化的最小事实。"""

    if cause is None and reason is None:
        return None
    if cause is None or reason is None:
        raise ActionableSoloAIError(
            "Finish delivery intent requires both --cause and --reason",
            code="INVALID_DELIVERY_INTENT",
            next_action={"kind": "supply_cause_and_reason"},
        )
    from .candidate_batches import EXPLICIT_TAIL_CAUSES, _tail_request

    if cause not in EXPLICIT_TAIL_CAUSES:
        raise ActionableSoloAIError(
            "Finish delivery intent must use user, deploy, dependency, or round-complete",
            code="INVALID_DELIVERY_INTENT",
            context={"cause": cause},
            next_action={"kind": "choose_explicit_tail_cause"},
        )
    try:
        return _tail_request(cause=cause, reason=reason)
    except SoloAIError as exc:
        raise ActionableSoloAIError(
            str(exc),
            code="INVALID_DELIVERY_INTENT",
            next_action={"kind": "provide_one_line_reason"},
        ) from exc


def _publication_delivery_intent(publication: dict[str, Any]) -> dict[str, str] | None:
    stored = publication.get("delivery_intent")
    if stored is None:
        return None
    if publication.get("schema_version") != 3 or not isinstance(stored, dict):
        raise SoloAIError("Candidate publication delivery intent is invalid")
    if stored.get("schema_version") != 1:
        raise SoloAIError("Candidate publication delivery intent schema is unsupported")
    try:
        return _normalize_finish_delivery_intent(
            cause=stored.get("cause"), reason=stored.get("reason")
        )
    except ActionableSoloAIError as exc:
        raise SoloAIError(
            f"Candidate publication delivery intent is invalid: {exc}"
        ) from exc


def _record_finish_delivery_intent(
    store: StateStore,
    *,
    task: dict[str, Any],
    delivery_intent: dict[str, str] | None,
) -> dict[str, Any]:
    """给中断后的 prepared publication 追加一次且不可改写的意图。"""

    if delivery_intent is None:
        return task
    publication = task.get("candidate_publication") or {}
    existing = _publication_delivery_intent(publication)
    if existing is not None and existing != delivery_intent:
        raise ActionableSoloAIError(
            "Finish delivery intent conflicts with the one already recorded for this candidate",
            code="DELIVERY_INTENT_CONFLICT",
            context={"candidate_id": publication.get("candidate_id")},
            next_action={"kind": "recover_with_recorded_intent"},
        )
    if existing is not None:
        return task
    active = task.get("active_operation") or {}
    operation_id = str(active.get("id") or "")
    if not operation_id:
        raise SoloAIError("Finish operation identity is missing for delivery intent")
    return store.record_candidate_delivery_intent(
        str(task["id"]),
        operation_id=operation_id,
        delivery_intent={"schema_version": 1, **delivery_intent},
    )


def _reconcile_delivery_intent(
    repo: GitRepo,
    *,
    candidate_id: str,
    delivery_intent: dict[str, str] | None,
    coordinator: dict[str, str] | None,
) -> dict[str, Any] | None:
    if delivery_intent is None:
        return None
    from .candidate_batches import reconcile_batches

    return reconcile_batches(
        repo,
        force=True,
        cause=delivery_intent["cause"],
        reason=delivery_intent["reason"],
        require_tail_reason=True,
        coordinator=coordinator,
        candidate_id=candidate_id,
    )


def _apply_delivery_intent_result(
    repo: GitRepo,
    *,
    candidate_result: dict[str, Any],
    coordinator: dict[str, str] | None,
) -> dict[str, Any]:
    """只由已发布候选的持久化意图触发一次精确通道 reconcile。"""

    reconciliation = _reconcile_delivery_intent(
        repo,
        candidate_id=str(candidate_result["candidate_id"]),
        delivery_intent=candidate_result.get("delivery_intent"),
        coordinator=coordinator,
    )
    if reconciliation is None:
        return candidate_result
    batch = reconciliation.get("batch")
    delivered = bool(reconciliation.get("delivered"))
    candidate_result.update(
        {
            "reconciliation": reconciliation,
            "delivered": delivered,
            "delivery_status": "integrated" if delivered else "awaiting-integration",
        }
    )
    if isinstance(batch, dict):
        candidate_result.update(
            {
                "outcome": "batch_integrated" if delivered else "candidate_published",
                "batch_id": batch.get("id"),
                "batch_status": batch.get("status"),
                "batch_trigger": batch.get("trigger"),
                "integrated_head": batch.get("integrated_head"),
                "candidate_count": len(batch.get("candidate_ids") or []),
            }
        )
    return candidate_result


def finish(
    repo: GitRepo,
    *,
    task_id: str,
    lease: str,
    session_id: str | None = None,
    host_actor: dict[str, str] | None = None,
    cause: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    config, _, _ = _config_and_mode(repo)
    store = StateStore(repo)
    initial = store.task(task_id)
    host_actor = normalize_host_reference(host_actor)
    if not initial.get("integration_policy"):
        initial = store.ensure_task_integration_policy(task_id, config)
    policy = initial.get("integration_policy") or {}
    delivery_intent = _normalize_finish_delivery_intent(cause=cause, reason=reason)
    if delivery_intent and (_is_in_place(initial) or policy.get("mode") != "batched"):
        raise ActionableSoloAIError(
            "Finish delivery intent is available only for batched isolated tasks",
            code="DELIVERY_INTENT_UNSUPPORTED",
            context={"mode": policy.get("mode")},
            next_action={"kind": "finish_without_delivery_intent"},
        )
    candidate_result: dict[str, Any] | None = None
    with store.operation(task_id, lease, "finish") as active_task:
        if _is_in_place(active_task):
            result = _finish_in_place(
                repo,
                store=store,
                task=active_task,
                lease=lease,
                session_id=session_id,
            )
            delete_anchor(repo, task_id)
            return result
        candidate_requires_ready = _candidate_requires_ready(active_task)
        allowed_statuses = {"finishing", "publishing"}
        allowed_statuses.update(
            {"ready"} if candidate_requires_ready else {"active", "ready"}
        )
        if active_task.get("status") not in allowed_statuses:
            raise SoloAIError("Finish requires a successful Ready")
        turn = (
            integration_turn(repo, task_id)
            if policy.get("mode") != "batched"
            else nullcontext()
        )
        with maintenance_lock(repo), turn:
            pending = _bootstrap(repo)
            if pending:
                bootstrap_result = _integrate_pending_bootstrap(repo)
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
                        base_worktree_resolved=bootstrap_result[
                            "base_worktree_resolved"
                        ],
                        base_worktree_identity=bootstrap_result[
                            "base_worktree_identity"
                        ],
                        ready_proof=None,
                    )
            task = store.task(task_id)
            store.require_lease(task, lease)
            _assert_no_in_place_integration_conflict(store, task)
            if task.get("candidate_publication"):
                task = _record_finish_delivery_intent(
                    store, task=task, delivery_intent=delivery_intent
                )
                with candidate_admission_lock(repo):
                    candidate_result = _resume_candidate_publication(
                        repo, store=store, task=task, coordinator=host_actor
                    )
            elif task.get("integration"):
                result = resume_integration(
                    repo, store=store, task=task, allow_stale=False
                )
                delete_anchor(repo, task_id)
                return result
            else:
                if policy.get("mode") == "batched":
                    _require_current_structured_root_review(repo, task, store=store)
                candidate_requires_ready = _candidate_requires_ready(task)
                if candidate_requires_ready and task.get("status") != "ready":
                    raise SoloAIError("Finish requires a successful Ready")
                _recorded_base_worktree(repo, task)
                worktree = Path(str(task["worktree"]))
                if candidate_requires_ready:
                    _assert_exact_candidate(
                        repo, task, candidate_head=str(task["candidate_head"])
                    )
                    recorded_candidate_head = task["candidate_head"]
                    recorded_base_head = task["base_head"]
                    task = _sync_base(repo, task)
                    task = store.update_task(
                        task_id,
                        candidate_head=task["candidate_head"],
                        base_head=task["base_head"],
                        # 只有同步实际改变候选或基线时，既有 Ready 证明才不再对应
                        # 当前状态；无变化时，发布前清理失败必须保留该证明以便重试。
                        ready_proof=(
                            None
                            if task["candidate_head"] != recorded_candidate_head
                            or task["base_head"] != recorded_base_head
                            else task.get("ready_proof")
                        ),
                    )
                candidate_head = repo.head(worktree)
                _assert_exact_candidate(repo, task, candidate_head=candidate_head)
                # 候选固定前仍核验配置、机密与工作区安全；项目检查由新批次
                # 在组合后的源码上统一执行，旧策略保留 Ready 验收。
                candidate_config = load_repo_config(repo, cwd=worktree)
                store.require_slot_layout(candidate_config)
                verification = load_verification_config(repo, cwd=worktree)
                if candidate_requires_ready:
                    _require_validation_approval(
                        repo,
                        cwd=worktree,
                        base=str(task["base_ref"]),
                        verification=verification,
                        level="ready",
                        full_scope=None,
                        scope="finish",
                        include_secret_scanner=candidate_config.secret_scanner
                        is not None,
                        approval_target={"task": task_id},
                    )
                else:
                    require_approval(
                        repo,
                        verification,
                        cwd=worktree,
                        scope="finish",
                        include_secret_scanner=candidate_config.secret_scanner
                        is not None,
                        approval_target={"task": task_id},
                    )
                _run_declared_secret_scanner(
                    repo, cwd=worktree, scanner=candidate_config.secret_scanner
                )
                _assert_exact_candidate(repo, task, candidate_head=candidate_head)
                require_safe(
                    repo,
                    cwd=worktree,
                    base=str(task["base_ref"]),
                    allowlist=candidate_config.sensitive_allowlist,
                )
                _assert_exact_candidate(repo, task, candidate_head=candidate_head)
                proof: dict[str, Any] | None = None
                if candidate_requires_ready:
                    attempt_id = new_validation_attempt_id("ready")
                    attempts = [
                        str(item)
                        for item in task.get("validation_attempts", [])
                        if isinstance(item, str) and item
                    ]
                    attempts.append(attempt_id)
                    task = store.update_task(
                        task_id,
                        validation_attempt=attempt_id,
                        validation_attempts=attempts,
                    )
                    proof = validate(
                        repo,
                        cwd=worktree,
                        base=str(task["base_ref"]),
                        verification=verification,
                        task_id=task_id,
                        expected_base_head=str(task["base_head"]),
                        expected_candidate_head=candidate_head,
                        validation_base_ref=str(task["base_ref"]),
                        attempt_id=attempt_id,
                        attempt_owner={"kind": "task", "id": task_id},
                    )
                _assert_exact_candidate(repo, task, candidate_head=candidate_head)
                if unknown := _unknown_ignored(repo, worktree):
                    raise _unknown_content_error(
                        task_id=task_id,
                        phase="slot release",
                        paths=unknown,
                    )
                _stop_registered_processes(store, task)
                _assert_exact_candidate(repo, task, candidate_head=candidate_head)
                task = store.update_task(
                    task_id,
                    candidate_head=candidate_head,
                    base_head=task["base_head"],
                    ready_proof=proof["fingerprint"] if proof else None,
                )
                if policy.get("mode") == "batched":
                    prepared = _prepare_candidate_publication(
                        repo,
                        store=store,
                        task=task,
                        proof=proof,
                        delivery_intent=delivery_intent,
                    )
                    with candidate_admission_lock(repo):
                        candidate_result = _resume_candidate_publication(
                            repo, store=store, task=prepared, coordinator=host_actor
                        )
                else:
                    from .runtime_adapter import release_task_runtime

                    runtime_release = release_task_runtime(
                        repo,
                        task=task,
                        reason="direct-integration",
                    )
                    prepared = prepare_integration(
                        repo, store=store, task=task, proof=proof
                    )
                    result = resume_integration(
                        repo, store=store, task=prepared, allow_stale=False
                    )
                    delete_anchor(repo, task_id)
                    return {**result, "runtime_release": runtime_release}
    if candidate_result is None:
        raise SoloAIError("Candidate publication did not produce a durable result")
    auto_batch_id = candidate_result.get("auto_batch_id")
    if auto_batch_id:
        from .candidate_batches import run_batch

        batch = run_batch(repo, batch_id=str(auto_batch_id))
        candidate_result.update(
            {
                "outcome": "batch_integrated",
                "batch_id": batch["id"],
                "batch_status": batch["status"],
                "integrated_head": batch.get("integrated_head"),
                "candidate_count": len(batch["candidate_ids"]),
                "delivered": batch["status"] == "completed",
                "delivery_status": "integrated"
                if batch["status"] == "completed"
                else "awaiting-integration",
            }
        )
    else:
        if candidate_result.get("delivery_intent"):
            candidate_result = _apply_delivery_intent_result(
                repo, candidate_result=candidate_result, coordinator=host_actor
            )
        else:
            from .candidate_batches import reconcile_batches

            reconciliation = reconcile_batches(
                repo, cause="finish", coordinator=host_actor
            )
            candidate_result.update(
                {
                    "delivered": False,
                    "delivery_status": "awaiting-integration",
                    "reconciliation": reconciliation,
                }
            )
    return candidate_result


def _recover_runtime_adapter_repair(
    repo: GitRepo,
    *,
    store: StateStore,
    task: dict[str, Any],
    paths: list[str],
) -> dict[str, Any]:
    if (
        _is_in_place(task)
        or task.get("status") != "starting"
        or task.get("runtime_activation_pending") is not True
        or task.get("runtime_activation") is not None
        or task.get("candidate_publication")
        or task.get("integration")
        or task.get("abandonment")
    ):
        raise SoloAIError(
            "Runtime Adapter repair only applies to a failed pre-activation isolated task"
        )
    active = task.get("active_operation") or {}
    if active and process_matches(active.get("owner", {})):
        raise SoloAIError("Task still has a live operation; recovery is unsafe")
    _assert_starting_task_identity(repo, task)
    worktree = Path(str(task["worktree"]))
    from .runtime_adapter import require_failed_task_runtime_activation

    config = require_failed_task_runtime_activation(repo, task=task)
    if (
        not paths
        or len(paths) != len(set(paths))
        or any(not _path_is_safe(path) for path in paths)
    ):
        raise SoloAIError(
            "Runtime Adapter repair requires unique repository-relative exact --path values"
        )
    tracked = set(
        item
        for item in repo.git(["ls-files", "-z"], cwd=worktree).stdout.split("\0")
        if item
    )
    allowed_inputs = set(config.runtime_adapter.input_paths)
    invalid = sorted(
        path
        for path in paths
        if path not in tracked
        or (
            path != ".solo-ai/config.toml"
            and not any(
                fnmatch.fnmatchcase(path, pattern) for pattern in allowed_inputs
            )
        )
    )
    if invalid:
        raise SoloAIError(
            "Runtime Adapter repair paths must be tracked and covered by the approved input_paths:\n"
            + "\n".join(f"- {path}" for path in invalid)
        )
    allowed_paths = tuple(paths)
    repair = {
        "schema_version": 1,
        "allowed_paths": list(allowed_paths),
        "release_required": True,
        "recovered_at": utc_timestamp(),
    }
    activated = store.activate_started_task(
        str(task["id"]),
        runtime_activation={
            "configured": True,
            "operation": "activate",
            "skipped": True,
            "reason": "runtime-adapter-repair",
        },
        runtime_adapter_repair=repair,
    )
    anchor = require_anchor(repo, activated)
    return {**activated, "anchor_path": str(anchor.resolve())}


def _recover_published_runtime_adapter_release(
    repo: GitRepo,
    *,
    store: StateStore,
    task: dict[str, Any],
    paths: list[str],
    coordinator: dict[str, str] | None = None,
) -> dict[str, Any]:
    """从已交付 base 重试一个 held 候选的 Adapter release，不改候选内容。"""

    publication = task.get("candidate_publication") or {}
    active = task.get("active_operation") or {}
    if (
        _is_in_place(task)
        or task.get("status") != "publishing"
        or publication.get("phase") != "prepared"
        or publication.get("task_id") != task.get("id")
        or active
        and process_matches(active.get("owner", {}))
    ):
        raise SoloAIError(
            "Delivered-base Runtime Adapter recovery requires an idle prepared candidate publication"
        )
    if (
        not paths
        or len(paths) != len(set(paths))
        or any(not _path_is_safe(path) for path in paths)
    ):
        raise SoloAIError(
            "Delivered-base Runtime Adapter recovery requires unique repository-relative exact --path values"
        )
    worktree = Path(str(task["worktree"]))
    source_root = Path(str(task["base_worktree"]))
    if (
        not worktree.is_dir()
        or not repo.is_clean(worktree)
        or repo.head(worktree) != publication.get("head")
        or repo.branch(worktree) not in {None, task.get("branch")}
        or not source_root.is_dir()
        or not repo.is_clean(source_root)
        or repo.branch(source_root) != task.get("base_ref")
    ):
        raise SoloAIError(
            "Delivered-base Runtime Adapter recovery requires exact clean candidate and base worktrees"
        )
    delivered_head = repo.head(source_root)
    if (
        delivered_head == task.get("base_head")
        or repo.git(
            ["merge-base", "--is-ancestor", str(task["base_head"]), delivered_head],
            cwd=source_root,
            check=False,
        ).returncode
        != 0
    ):
        raise SoloAIError(
            "Delivered-base Runtime Adapter recovery requires a clean base advanced from the task baseline"
        )
    config = load_repo_config(repo, cwd=source_root)
    if config.runtime_adapter.release is None:
        raise SoloAIError(
            "Delivered base no longer configures a Runtime Adapter release"
        )
    candidate_tracked = {
        item
        for item in repo.git(["ls-files", "-z"], cwd=worktree).stdout.split("\0")
        if item
    }
    source_tracked = {
        item
        for item in repo.git(["ls-files", "-z"], cwd=source_root).stdout.split("\0")
        if item
    }
    allowed_inputs = tuple(config.runtime_adapter.input_paths)
    invalid = sorted(
        path
        for path in paths
        if path not in candidate_tracked
        or path not in source_tracked
        or (
            path != ".solo-ai/config.toml"
            and not any(
                fnmatch.fnmatchcase(path, pattern) for pattern in allowed_inputs
            )
        )
        or sha256_file(worktree / path) == sha256_file(source_root / path)
    )
    if invalid:
        raise SoloAIError(
            "Delivered-base Runtime Adapter recovery paths must be changed, tracked, and covered by input_paths:\n"
            + "\n".join(f"- {path}" for path in invalid)
        )
    from .candidate_batches import CandidateBatchStore
    from .runtime_adapter import release_task_runtime

    candidate = CandidateBatchStore(repo).candidate_for_task(str(task["id"]))
    if (
        candidate is None
        or candidate.get("status") != "held"
        or candidate.get("candidate_id") != publication.get("candidate_id")
        or candidate.get("head") != publication.get("head")
        or candidate.get("ref") != publication.get("ref")
    ):
        raise SoloAIError(
            "Delivered-base Runtime Adapter recovery requires the exact held candidate"
        )
    with store.recovery_operation(str(task["id"])):
        task = store.task(str(task["id"]))
        runtime_release = release_task_runtime(
            repo,
            task=task,
            reason="candidate-published",
            candidate=candidate,
            adapter_source=source_root,
            repaired_paths=tuple(paths),
        )
        if runtime_release.get("configured"):
            task = store.record_candidate_runtime_release(
                str(task["id"]),
                candidate_id=str(candidate["candidate_id"]),
                receipt=runtime_release,
            )
        with candidate_admission_lock(repo):
            resumed = _resume_candidate_publication(
                repo, store=store, task=task, coordinator=coordinator
            )
        return _apply_delivery_intent_result(
            repo, candidate_result=resumed, coordinator=coordinator
        )


def recover(
    repo: GitRepo,
    *,
    task_id: str,
    repair_runtime_adapter_paths: list[str] | None = None,
    host_actor: dict[str, str] | None = None,
) -> dict[str, Any]:
    """根据持久化事务和 Git 事实恢复；失败时不轮换租约或改变现场。"""
    _, _, _ = _config_and_mode(repo)
    store = StateStore(repo)
    host_actor = normalize_host_reference(host_actor)
    store.reconcile_operation_receipts()
    task = store.task(task_id)
    if task.get("status") in {
        "preactivation-releasing",
        "abandoned",
    } and task.get("preactivation_release"):
        with maintenance_lock(repo):
            return _resume_preactivation_release(repo, store=store, task=task)
    if repair_runtime_adapter_paths is not None:
        with maintenance_lock(repo):
            if task.get("status") == "publishing" and task.get("candidate_publication"):
                return _recover_published_runtime_adapter_release(
                    repo,
                    store=store,
                    task=task,
                    paths=repair_runtime_adapter_paths,
                    coordinator=host_actor,
                )
            return _recover_runtime_adapter_repair(
                repo,
                store=store,
                task=task,
                paths=repair_runtime_adapter_paths,
            )
    if task.get("status") == "candidate-published":
        from .candidate_batches import CandidateBatchStore, run_batch

        publication = task.get("candidate_publication") or {}
        batch_store = CandidateBatchStore(repo)
        candidate = batch_store.candidate_for_task(task_id) or {}
        repair_handoff = None
        if candidate:
            try:
                from .host_handoffs import HostHandoffStore

                repair_handoff = HostHandoffStore(
                    repo
                ).record_repair_candidate_published(
                    task_id=task_id, candidate=candidate
                )
            except (OSError, SoloAIError) as handoff_error:
                repair_handoff = {"recording_error": str(handoff_error)}
        if candidate.get("status") == "held":
            slot = store.read()["slots"].get(str(task.get("slot_id"))) or {}
            if slot.get("status") == "quarantined":
                raise SoloAIError(
                    "The released slot is quarantined; inspect it before activating the held candidate"
                )
            policy = publication.get("integration_policy") or {}
            with candidate_admission_lock(repo):
                activated = batch_store.activate(
                    str(candidate["candidate_id"]),
                    batch_size=int(policy["batch_size"]),
                    seal_policy=str(policy["seal_policy"]),
                    coordinator=host_actor,
                )
            candidate = activated["candidate"]
            auto_batch = activated.get("auto_batch")
            if auto_batch:
                batch = run_batch(repo, batch_id=str(auto_batch["id"]))
                return {
                    "id": task_id,
                    "status": "integrated",
                    "candidate_id": candidate.get("candidate_id"),
                    "batch_id": batch["id"],
                    "delivered": batch.get("status") == "completed",
                    "delivery_status": "integrated"
                    if batch.get("status") == "completed"
                    else "awaiting-integration",
                    "repair_handoff": repair_handoff,
                }
        if candidate.get("status") in {"integrated", "withdrawn", "superseded"}:
            delivered = candidate.get("status") == "integrated"
            return {
                "id": task_id,
                "status": candidate["status"],
                "candidate_id": candidate.get("candidate_id"),
                "batch_id": candidate.get("integrated_batch"),
                "delivered": delivered,
                "delivery_status": "integrated" if delivered else "not-delivered",
                "repair_handoff": repair_handoff,
            }
        require_anchor(repo, task)
        result = {
            "id": task_id,
            "status": "candidate-published",
            "candidate_id": publication.get("candidate_id"),
            "candidate_head": publication.get("head"),
            "anchor_path": publication.get("anchor_path"),
            "delivered": False,
            "delivery_status": "awaiting-integration",
            "delivery_intent": _publication_delivery_intent(publication),
            "repair_handoff": repair_handoff,
        }
        applied = _apply_delivery_intent_result(
            repo, candidate_result=result, coordinator=host_actor
        )
        return {
            **applied,
            "status": "integrated"
            if applied.get("delivered")
            else "candidate-published",
        }
    if _is_in_place(task):
        receipt = read_json(_in_place_receipt_path(repo, task_id), {})
        if task.get("status") == "finished" and receipt.get("stage") in {
            "completed",
            "released",
        }:
            _validate_in_place_receipt(repo, task=task, receipt=receipt)
            receipt["stage"] = "released"
            _write_in_place_receipt(repo, receipt)
            delete_anchor(repo, task_id)
            return {"id": task_id, "status": "completed", "mode": IN_PLACE_MODE}
        raise SoloAIError(
            "In-place tasks require resume-in-place; ordinary recovery cannot change their binding"
        )
    if task.get("status") == "starting" and task.get("runtime_activation_pending"):
        active = task.get("active_operation") or {}
        if active and process_matches(active.get("owner", {})):
            raise SoloAIError("Task still has a live operation; recovery is unsafe")
        with maintenance_lock(repo):
            create_anchor(repo, task)
            return _complete_runtime_activation(repo, store=store, task=task)
    if task.get("status") == "quarantined" and not _is_in_place(task):
        with store.recovery_operation(task_id) as recovery_task:
            operation_id = str(recovery_task["active_operation"]["id"])
            with maintenance_lock(repo):
                if _is_dirty_preactivation_failure(store.task(task_id)):
                    transaction = _new_preactivation_release(
                        repo,
                        store=store,
                        task=store.task(task_id),
                        operation_id=operation_id,
                    )
                    prepared = store.prepare_preactivation_release(
                        task_id,
                        operation_id=operation_id,
                        recovery=transaction,
                    )
                    return _resume_preactivation_release(
                        repo, store=store, task=prepared
                    )
                return _resume_quarantined_start(
                    repo,
                    store=store,
                    task=store.task(task_id),
                    operation_id=operation_id,
                    request_reused=False,
                )
    active = task.get("active_operation") or {}
    if active and process_matches(active.get("owner", {})):
        raise SoloAIError("Task still has a live operation; recovery is unsafe")
    transaction = task.get("integration") or {}
    if task.get("status") == "finished" and transaction.get("phase") == "completed":
        slot = store.read()["slots"][task["slot_id"]]
        if slot.get("task_id") == task_id and slot.get("status") == "release-checking":
            with maintenance_lock(repo), integration_turn(repo, task_id):
                result = resume_integration(
                    repo, store=store, task=store.task(task_id), allow_stale=False
                )
                delete_anchor(repo, task_id)
                return result
        receipt = write_completed_receipt(repo, task)
        delete_anchor(repo, task_id)
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
                result = resume_abandonment(repo, store=store, task=store.task(task_id))
                delete_anchor(repo, task_id)
                return result
        receipt = write_abandonment_receipt(repo, task)
        delete_anchor(repo, task_id)
        return {
            "id": task_id,
            "status": "abandoned",
            "transaction_id": receipt["transaction_id"],
        }
    if task.get("status") == "finished":
        with integration_turn(repo, task_id):
            migrated = migrate_legacy_receipt(
                repo, store=store, task=store.task(task_id)
            )
            if not migrated:
                raise SoloAIError(f"Task cannot be recovered: {task_id}")
            receipt = write_completed_receipt(repo, migrated)
            delete_anchor(repo, task_id)
            return {
                "id": task_id,
                "status": "completed",
                "transaction_id": receipt["transaction_id"],
                "candidate_head": receipt["candidate_head"],
            }
    if task.get("candidate_publication"):
        # 候选发布恢复仍需按原任务进入 FIFO，但持久化的尾批意图只能在
        # 此 turn 释放之后执行；否则同一进程会排在自己的任务票据之后。
        with store.recovery_operation(task_id) as recovery_task:
            recovery_operation_id = str(recovery_task["active_operation"]["id"])
            with maintenance_lock(repo), integration_turn(repo, task_id):
                task = store.task(task_id)
                active = task.get("active_operation") or {}
                if active.get("id") != recovery_operation_id:
                    raise SoloAIError(
                        "Task recovery operation identity changed while waiting"
                    )
                resumed = _resume_candidate_publication(
                    repo, store=store, task=task, coordinator=host_actor
                )
        return _apply_delivery_intent_result(
            repo, candidate_result=resumed, coordinator=host_actor
        )
    with store.recovery_operation(task_id) as recovery_task:
        recovery_operation_id = str(recovery_task["active_operation"]["id"])
        with maintenance_lock(repo), integration_turn(repo, task_id):
            task = store.task(task_id)
            active = task.get("active_operation") or {}
            if active.get("id") != recovery_operation_id:
                raise SoloAIError(
                    "Task recovery operation identity changed while waiting"
                )
            restored = _restore_orphaned_ready_proof(repo, store=store, task=task)
            if restored is not None:
                return {
                    "id": restored["id"],
                    "status": restored["status"],
                    "ready_proof": restored["ready_proof"],
                    "recovered_ready_proof": True,
                }
            if task.get("abandonment"):
                if task["abandonment"].get("retained_worktree") is True:
                    result = resume_retained_abandonment(repo, store=store, task=task)
                else:
                    _stop_registered_processes(store, task)
                    result = resume_abandonment(
                        repo, store=store, task=store.task(task_id)
                    )
                delete_anchor(repo, task_id)
                return result
            integration = task.get("integration")
            if not integration:
                migrated = migrate_legacy_receipt(repo, store=store, task=task)
                if migrated:
                    task = migrated
                    integration = task.get("integration")
            if (
                not integration
                and task.get("status") == "ready"
                and task.get("candidate_head")
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
                    return store.recover(task_id, operation_id=recovery_operation_id)
                delete_anchor(repo, task_id)
                return result
            if task.get("status") in FINAL_TASK_STATES:
                raise SoloAIError(f"Task cannot be recovered: {task_id}")
            worktree = Path(str(task["worktree"]))
            if not worktree.is_dir() or not any(
                item.path == worktree.resolve() for item in repo.worktrees()
            ):
                raise SoloAIError(
                    "Task worktree is missing or unregistered; preserve state"
                )
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
                repo.git(
                    ["switch", "-c", task["branch"], task["base_head"]], cwd=worktree
                )
            elif current_branch is None:
                if current_head != branch_head:
                    raise SoloAIError(
                        "Detached task HEAD differs from its branch; preserve it"
                    )
                repo.git(["switch", task["branch"]], cwd=worktree)
            elif current_head != branch_head:
                raise SoloAIError("Task branch and worktree HEAD differ; preserve them")
            return store.recover(task_id, operation_id=recovery_operation_id)


def _assert_handoff_identity(repo: GitRepo, task: dict[str, Any]) -> None:
    """交接只确认原现场，绝不清理、合并或改写其中的内容。"""

    worktree = Path(str(task["worktree"]))
    managed_root = worktree.absolute().parent
    identity_fields = (
        task.get("slot_worktree_identity"),
        task.get("slot_managed_root_identity"),
        task.get("slot_worktree_resolved"),
        task.get("slot_managed_root_resolved"),
    )
    if not all(identity_fields):
        raise SoloAIError("Task lacks complete managed-directory identity for handoff")
    require_managed_directory_identity(
        worktree,
        managed_root=managed_root,
        expected_resolved=str(task["slot_worktree_resolved"]),
        expected_root_resolved=str(task["slot_managed_root_resolved"]),
        expected_identity=dict(task["slot_worktree_identity"]),
        expected_root_identity=dict(task["slot_managed_root_identity"]),
    )
    if not worktree.is_dir() or not any(
        item.path == worktree.resolve() for item in repo.worktrees()
    ):
        raise SoloAIError("Task worktree is missing or unregistered; preserve it")
    expected_head = task.get("candidate_head")
    if not isinstance(expected_head, str) or not expected_head:
        raise SoloAIError("Task has no exact recorded HEAD for handoff")
    if repo.branch(worktree) != task.get("branch"):
        raise SoloAIError("Task worktree branch differs from its recorded identity")
    if repo.head(worktree) != expected_head:
        raise SoloAIError("Task worktree HEAD differs from its recorded identity")
    require_anchor(repo, task, require_verified_origin=True)


def _assert_handoff_validation_idle(repo: GitRepo, task_id: str) -> None:
    """不接管正在运行或尚未被常规恢复确认的验证。"""

    run_root = repo.local_dir / "validation-runs"
    for receipt_path in run_root.glob("**/*.json") if run_root.exists() else ():
        receipt = read_json(receipt_path, {})
        if receipt.get("metadata", {}).get("task_id") != task_id:
            continue
        if receipt.get("status") not in {"running", "terminating"}:
            continue
        if process_matches(receipt.get("process", {})):
            raise SoloAIError(
                "Task still has a live validation process; handoff is unsafe"
            )
        raise SoloAIError(
            "Task has an unsettled validation receipt; recover it before handoff"
        )


def handoff(
    repo: GitRepo,
    *,
    task_id: str,
    confirm: str,
    host_origin: dict[str, str] | None,
) -> dict[str, Any]:
    """显式转交中断会话遗留的隔离任务，不改变其 Git 或文件现场。"""

    _, _, _ = _config_and_mode(repo)
    host_origin = normalize_host_reference(host_origin)
    if host_origin is None:
        raise SoloAIError("Handoff requires an exact recipient host reference")
    store = StateStore(repo)
    store.reconcile_operation_receipts()
    task = store.task(task_id)
    confirmed_branch = task.get("branch")
    confirmed_head = task.get("candidate_head")
    expected = f"{task_id}:{confirmed_branch}:{confirmed_head}"
    if confirm != expected:
        raise SoloAIError(f"Handoff requires --confirm {expected!r}")
    if StateStore.mode(task) != ISOLATED_MODE:
        raise SoloAIError("Handoff only applies to isolated tasks")
    if task.get("status") not in {"active", "ready"}:
        raise SoloAIError("Only active or ready tasks can be handed off")
    if (
        task.get("candidate_publication")
        or task.get("integration")
        or task.get("abandonment")
    ):
        raise SoloAIError(
            "Published, finishing, or abandonment tasks cannot be handed off"
        )
    active = task.get("active_operation") or {}
    if active:
        if process_matches(active.get("owner", {})):
            raise SoloAIError("Task still has a live operation; handoff is unsafe")
        raise SoloAIError("Task has an unsettled operation; recover it before handoff")
    if task.get("processes"):
        if any(process_matches(item) for item in task["processes"]):
            raise SoloAIError(
                "Task still has a live registered process; handoff is unsafe"
            )
        raise SoloAIError("Task has an unsettled registered process; preserve it")
    _assert_handoff_validation_idle(repo, task_id)
    with store.recovery_operation(task_id) as recovery_task:
        operation_id = str(recovery_task["active_operation"]["id"])
        with maintenance_lock(repo):
            task = store.task(task_id)
            active = task.get("active_operation") or {}
            if active.get("id") != operation_id or active.get("kind") != "recover":
                raise SoloAIError(
                    "Task handoff operation identity changed while waiting"
                )
            if (
                task.get("branch") != confirmed_branch
                or task.get("candidate_head") != confirmed_head
            ):
                raise SoloAIError("Task identity changed after handoff confirmation")
            _assert_handoff_validation_idle(repo, task_id)
            _assert_handoff_identity(repo, task)
            return store.handoff_isolated_task(
                task_id,
                operation_id=operation_id,
                expected_branch=str(confirmed_branch),
                expected_head=str(confirmed_head),
                host_origin=host_origin,
            )


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
    reason: str | None = None,
    source: str = "api",
    session_id: str | None = None,
    retain_worktree: bool = False,
) -> dict[str, Any]:
    if confirm != task_id:
        raise SoloAIError("Abandon requires --confirm with the exact task id")
    if retain_worktree and not (reason or "").strip():
        raise SoloAIError("Retained abandon requires one non-empty audit reason")
    config, _, _ = _config_and_mode(repo)
    store = StateStore(repo)
    result: dict[str, Any] | None = None
    with store.operation(task_id, lease, "abandon") as task:
        if _is_in_place(task):
            if retain_worktree:
                raise SoloAIError(
                    "--retain-worktree applies only to isolated tasks; in-place tasks are already preserved"
                )
            _assert_in_place_binding(repo, store, task, session_id=session_id)
            worktree = Path(str(task["worktree"]))
            if not repo.is_clean(worktree):
                raise SoloAIError(
                    "In-place abandon never resets or cleans the current worktree. Commit exact paths and Finish, or preserve and handle the changes manually."
                )
            audit = in_place_audit(task, reason=reason, source=source)
            if not task.get("abandonment_audit"):
                store.update_task(task_id, abandonment_audit=audit)
            from .runtime_adapter import release_task_runtime

            runtime_release = release_task_runtime(repo, task=task, reason="abandon")
            if not audit.get("completed_at"):
                audit["completed_at"] = utc_timestamp()
                store.update_task(task_id, abandonment_audit=audit)
            with candidate_admission_lock(repo):
                store.release(task_id, final_status="abandoned")
            delete_anchor(repo, task_id)
            result = {
                "task_id": task_id,
                "status": "abandoned",
                "mode": IN_PLACE_MODE,
                "preserved": True,
                "abandonment_audit": audit,
                "runtime_release": runtime_release,
            }
        else:
            with maintenance_lock(repo), integration_turn(repo, task_id):
                task = store.task(task_id)
                store.require_lease(task, lease)
                if task.get("integration"):
                    raise SoloAIError(
                        "An integration transaction exists; Recover must resolve it before Abandon"
                    )
                if task.get("abandonment"):
                    with candidate_admission_lock(repo):
                        if task["abandonment"].get("retained_worktree") is True:
                            result = resume_retained_abandonment(
                                repo, store=store, task=task
                            )
                            runtime_release = None
                        else:
                            from .runtime_adapter import release_task_runtime

                            runtime_release = release_task_runtime(
                                repo, task=task, reason="abandon"
                            )
                            result = resume_abandonment(repo, store=store, task=task)
                else:
                    with candidate_admission_lock(repo):
                        assert_task_not_held_by_candidate_delivery(repo, task=task)
                    ensure_within(
                        Path(task["worktree"]),
                        store.managed_worktree_root(config),
                    )
                    if retain_worktree:
                        assert_retained_worktree_safe(task)
                        prepared = prepare_retained_abandonment(
                            repo,
                            store=store,
                            task=task,
                            reason=str(reason).strip(),
                            source=source,
                        )
                        runtime_release = None
                    else:
                        _stop_registered_processes(store, task)
                        task = store.task(task_id)
                        from .runtime_adapter import release_task_runtime

                        runtime_release = release_task_runtime(
                            repo, task=task, reason="abandon"
                        )
                        prepared = prepare_abandonment(
                            repo,
                            store=store,
                            task=task,
                            reason=reason,
                            source=source,
                        )
                    with candidate_admission_lock(repo):
                        result = (
                            resume_retained_abandonment(
                                repo, store=store, task=prepared
                            )
                            if retain_worktree
                            else resume_abandonment(repo, store=store, task=prepared)
                        )
                if runtime_release is not None:
                    result["runtime_release"] = runtime_release
                delete_anchor(repo, task_id)
    if result is None:
        raise SoloAIError("Abandonment did not produce a durable result")
    if config.integration.mode == "batched":
        from .candidate_batches import reconcile_batches

        result["reconciliation"] = reconcile_batches(repo, cause="abandon")
    return result


def reclaim_retained_worktree(
    repo: GitRepo, *, task_id: str, confirm: str | None = None
) -> dict[str, Any]:
    """按一次可审阅清单回收已保留的终态隔离工作树。"""
    _config_and_mode(repo)
    with maintenance_lock(repo), candidate_admission_lock(repo):
        store = StateStore(repo)
        task = store.task(task_id)
        existing = task.get("retained_reclaim")
        if isinstance(existing, dict):
            if existing.get("phase") == "completed":
                return resume_retained_reclaim(repo, store=store, task=task)
            if not confirm:
                return {
                    "task_id": task_id,
                    "slot_id": task["slot_id"],
                    "status": "needs-confirmation",
                    "confirmation": existing.get("confirmation"),
                    "delete": sorted((existing.get("ordinary_untracked") or {}).keys()),
                    "resuming": True,
                }
            if confirm != existing.get("confirmation"):
                raise SoloAIError("Retained reclaim confirmation changed")
            return resume_retained_reclaim(repo, store=store, task=task)
        plan = retained_reclaim_plan(
            repo, store=store, task=task, diagnostic=confirm is None
        )
        if confirm is None:
            return plan
        if confirm != plan["confirmation"]:
            raise SoloAIError(
                "Retained reclaim confirmation does not match the current file checklist"
            )
        reclaim = {
            **{
                key: value
                for key, value in plan.items()
                if key
                not in {"delete", "retained", "status", "scan_complete", "blockers"}
            },
            "transaction_id": uuid.uuid4().hex,
            "phase": "prepared",
            "prepared_at": utc_timestamp(),
        }
        prepared = store.prepare_retained_reclaim(task_id, reclaim=reclaim)
        return resume_retained_reclaim(repo, store=store, task=prepared)


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
    _, _, _ = _config_and_mode(repo)
    store = StateStore(repo)
    with store.operation(task_id, lease, "dev-start") as task:
        if _is_in_place(task):
            raise SoloAIError(
                "In-place tasks intentionally do not claim a managed dev-server slot; run the project's current-worktree command explicitly if needed"
            )
        if task.get("status") not in {"active", "ready"}:
            raise SoloAIError(
                "Development processes cannot start during task finalization or recovery"
            )
        if task.get("processes"):
            raise SoloAIError("Task already has a registered development process")
        worktree = Path(str(task["worktree"]))
        config = load_repo_config(repo, cwd=worktree)
        verification = load_verification_config(repo, cwd=worktree)
        if not config.dev_start or not config.readiness:
            raise SoloAIError(
                "No lifecycle.dev_start plus readiness configuration is declared"
            )
        require_approval(
            repo,
            verification,
            cwd=worktree,
            scope="development",
            include_dev_start=True,
            approval_target={"task": task_id},
        )
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
        config, verification, policy = _config_and_mode(repo)
        if slot_id not in {f"{number:02d}" for number in range(1, config.slots + 1)}:
            raise SoloAIError("WarmSlot requires an active configured slot id")
        if config.warm_commands:
            require_approval(
                repo,
                verification,
                cwd=policy,
                scope="warm",
                include_warm_commands=True,
                approval_target={"slot": slot_id},
            )
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
    primary, default = repo.checked_out_local_branch()
    if not repo.is_clean(primary):
        raise SoloAIError(
            "Current policy worktree must be clean before deinitialization"
        )
    primary_head = repo.head(primary)
    primary_identity = path_identity(primary)
    agents = repo.root / "AGENTS.md"
    existing = agents.read_text(encoding="utf-8") if agents.exists() else ""
    cleaned_agents = remove_managed_agents_block(existing)
    # 先检查全部槽位；任何一个不安全都不得创建或合入策略删除提交。
    slots_to_remove = _preflight_deinit_slots(repo, config=config, state=state)
    # 在临时分支准备策略删除，但只在槽位全部安全释放后才合入默认分支。
    cleanup = repo.local_dir / "deinit" / uuid.uuid4().hex / "worktree"
    branch = f"solo-ai/deinit-{uuid.uuid4().hex[:8]}"
    repo.require_checked_out_branch_target(
        primary,
        default,
        expected_head=primary_head,
        expected_identity=primary_identity,
    )
    repo.git(["worktree", "add", "-b", branch, str(cleanup), default], cwd=primary)
    policy_integrated = False
    removed_slot_paths: list[Path] = []
    try:
        (cleanup / ".solo-ai" / "config.toml").unlink()
        (cleanup / ".solo-ai" / "verification.toml").unlink()
        (cleanup / ".solo-ai" / STRESS_VERIFICATION_FILENAME).unlink(missing_ok=True)
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
        # 槽位释放成功后再次确认调用目标，随后才合入策略删除。
        primary = repo.require_checked_out_branch_target(
            primary,
            default,
            expected_head=primary_head,
            expected_identity=primary_identity,
        )
        if not repo.is_clean(primary):
            raise SoloAIError(
                "Current policy worktree changed before deinitialization merge"
            )
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
