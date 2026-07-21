from __future__ import annotations

import errno
import os
import json
import platform
import signal
import shutil
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
    detect_existing_workflows,
    discover_validation_commands,
    load_repo_config,
    load_verification_config,
    managed_block,
    remove_managed_agents_block,
    render_agents,
    render_repo_config,
    render_verification_config,
)
from .proof import approval_plan, validate
from .repo import GitRepo
from .safety import require_safe
from .state import FINAL_TASK_STATES, StateStore
from .util import (
    DirectoryLock,
    SoloAIError,
    atomic_write_json,
    ensure_within,
    process_matches,
    process_snapshot,
    redact_text,
    read_json,
    run_logged,
    safe_slug,
    sha256_text,
    stable_json,
    utc_timestamp,
)


BOOTSTRAP_SCHEMA = 1
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


def _bootstrap(repo: GitRepo) -> dict[str, Any]:
    return read_json(repo.local_dir / "bootstrap.json", {})


def _effective_mode(repo: GitRepo) -> str:
    if not local_enabled(repo):
        return "disabled"
    existing = detect_existing_workflows(repo.root)
    if existing:
        return "defer"
    if (repo.policy_path() / ".solo-ai" / "config.toml").exists():
        return "managed"
    return "uninitialized"


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
            "local_preference": set_local_enabled(repo, enabled=False),
        }
    if not local_enabled(repo):
        raise SoloAIError(
            "This repository is locally disabled; run enable before adopting it"
        )
    if not 1 <= slots <= 5:
        raise SoloAIError("--slots must be between 1 and 5")
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
            "Repository is not adopted. Review the one-time plan with `init`, then pass --accept or --decline"
        )
    policy = repo.policy_path()
    config = load_repo_config(repo, cwd=policy)
    verification = load_verification_config(repo, cwd=policy)
    require_approval(repo, verification, cwd=policy)
    return config, verification, policy


def _base_ref(repo: GitRepo) -> str:
    bootstrap = _bootstrap(repo)
    return str(bootstrap.get("branch") or repo.default_branch())


def start(repo: GitRepo, *, name: str) -> dict[str, Any]:
    with maintenance_lock(repo):
        config, _, _ = _config_and_mode(repo)
        store = StateStore(repo)
        store.ensure_slots(config)
        base_ref = _base_ref(repo)
        base_head = repo.git(["rev-parse", base_ref]).stdout.strip()
        branch = f"{config.branch_prefix}{safe_slug(name)}-{uuid.uuid4().hex[:6]}"
        task = store.allocate(
            config, name=name, branch=branch, base_head=base_head, base_ref=base_ref
        )
        worktree = ensure_within(
            Path(task["worktree"]), repo.root / config.worktree_directory
        )
        try:
            registered = next(
                (item for item in repo.worktrees() if item.path == worktree), None
            )
            if registered is None:
                if worktree.exists() and any(worktree.iterdir()):
                    raise SoloAIError(f"Unregistered non-empty slot path: {worktree}")
                repo.git(["worktree", "add", "--detach", str(worktree), base_ref])
            else:
                if not repo.is_clean(worktree):
                    raise SoloAIError(f"Idle slot is not clean: {worktree}")
                if repo.branch(worktree) is not None:
                    raise SoloAIError(
                        f"Idle slot is unexpectedly attached to a branch: {worktree}"
                    )
                repo.git(["reset", "--hard", base_ref], cwd=worktree)
            repo.git(["switch", "-c", branch, base_ref], cwd=worktree)
            return store.update_task(
                task["id"],
                status="active",
                candidate_head=repo.head(worktree),
                baseline_paths=repo.changed_paths(worktree),
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


def _run_declared_secret_scanner(
    repo: GitRepo, *, cwd: Path, scanner: CommandSpec | None
) -> None:
    if scanner is None:
        return
    pending = (
        repo.local_dir / "logs" / "pending" / f"secret-scan-{uuid.uuid4().hex}.log"
    )
    code, _ = run_logged(scanner.argv, cwd=cwd, log_path=pending)
    if code:
        raise SoloAIError(
            f"Repository-declared secret scanner failed. Review its local redacted log: {pending}"
        )


def commit_task(
    repo: GitRepo, *, task_id: str, lease: str, message: str, paths: list[str]
) -> dict[str, Any]:
    config, _, _ = _config_and_mode(repo)
    store = StateStore(repo)
    with store.operation(task_id, lease, "commit") as task:
        worktree = Path(task["worktree"])
        if repo.branch(worktree) != task["branch"]:
            raise SoloAIError("Task branch identity no longer matches its slot")
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
        return store.update_task(
            task_id,
            candidate_head=repo.head(worktree),
            ready_proof=None,
            status="active",
        )


def _sync_default(repo: GitRepo, task: dict[str, Any]) -> dict[str, Any]:
    worktree = Path(task["worktree"])
    default = repo.default_branch()
    default_head = repo.git(["rev-parse", default], cwd=worktree).stdout.strip()
    if not repo.is_ancestor(default_head, "HEAD", cwd=worktree):
        prediction = repo.git(
            ["merge-tree", "--write-tree", default, "HEAD"], cwd=worktree, check=False
        )
        if prediction.returncode != 0:
            raise SoloAIError(
                "Read-only merge prediction found a conflict. Resolve it in this task worktree; no automatic semantic merge was attempted."
            )
        repo.git(["merge", "--no-edit", default], cwd=worktree)
    task["candidate_head"] = repo.head(worktree)
    return task


def ready(repo: GitRepo, *, task_id: str, lease: str) -> dict[str, Any]:
    _, _, _ = _config_and_mode(repo)
    store = StateStore(repo)
    with store.operation(task_id, lease, "ready") as task:
        worktree = Path(task["worktree"])
        if task.get("status") not in {"active", "ready"}:
            raise SoloAIError(f"Task cannot enter Ready from {task.get('status')}")
        if not repo.is_clean(worktree):
            raise SoloAIError("Commit all task changes before Ready")
        task = _sync_default(repo, task)
        if not repo.is_clean(worktree):
            raise SoloAIError("Default-branch synchronization left the task dirty")
        # 同步可能带入新的受管策略；必须按同步后的策略重新确认和验证。
        config = load_repo_config(repo, cwd=worktree)
        store.require_slot_layout(config)
        verification = load_verification_config(repo, cwd=worktree)
        require_approval(repo, verification, cwd=worktree)
        default = repo.default_branch()
        _run_declared_secret_scanner(repo, cwd=worktree, scanner=config.secret_scanner)
        require_safe(
            repo, cwd=worktree, base=default, allowlist=config.sensitive_allowlist
        )
        proof = validate(repo, cwd=worktree, base=default, verification=verification)
        return store.update_task(
            task_id,
            status="ready",
            candidate_head=repo.head(worktree),
            ready_proof=proof["fingerprint"],
        )


def _queue_ticket(repo: GitRepo, task_id: str) -> Path:
    queue = repo.local_dir / "queue"
    queue.mkdir(parents=True, exist_ok=True)
    ticket = queue / f"{time.time_ns():020d}-{uuid.uuid4().hex}-{task_id}.json"
    atomic_write_json(
        ticket,
        {
            "task_id": task_id,
            "owner": process_snapshot(),
            "created_at": utc_timestamp(),
        },
    )
    return ticket


def _wait_turn(ticket: Path) -> None:
    last_report = time.monotonic()
    while True:
        tickets = sorted(ticket.parent.glob("*.json"))
        if tickets and tickets[0] != ticket:
            owner = read_json(tickets[0], {}).get("owner", {})
            if owner and not process_matches(owner):
                tickets[0].unlink(missing_ok=True)
                continue
        if tickets and tickets[0] == ticket:
            return
        if time.monotonic() - last_report >= 30:
            print(
                f"Waiting in integration queue at position {tickets.index(ticket) + 1 if ticket in tickets else 0}...",
                flush=True,
            )
            last_report = time.monotonic()
        time.sleep(0.5)


def _unknown_ignored(repo: GitRepo, worktree: Path) -> list[str]:
    known_roots = {".venv", "node_modules", ".tmp", ".cache", "__pycache__"}
    unknown: list[str] = []
    for item in repo.ignored_untracked(worktree):
        parts = Path(item).parts
        if Path(item).name.startswith(".env"):
            unknown.append(item)
        elif (
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
        path = ensure_within(Path(slot["path"]), repo.root / config.worktree_directory)
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


def _integrate_pending_bootstrap(repo: GitRepo, primary: Path) -> None:
    pending = _bootstrap(repo)
    if not pending:
        return
    branch = str(pending["branch"])
    repo.git(["merge", "--ff-only", branch], cwd=primary)
    worktree = Path(str(pending["worktree"]))
    if worktree.exists():
        repo.git(["worktree", "remove", str(worktree)], cwd=primary)
    repo.git(["branch", "-d", branch], cwd=primary)
    (repo.local_dir / "bootstrap.json").unlink(missing_ok=True)


def finish(repo: GitRepo, *, task_id: str, lease: str) -> dict[str, Any]:
    _, _, _ = _config_and_mode(repo)
    store = StateStore(repo)
    with store.operation(task_id, lease, "finish"):
        initial = store.task(task_id)
        if initial.get("status") != "ready":
            raise SoloAIError("Finish requires a successful Ready")
        ticket = _queue_ticket(repo, task_id)
        try:
            _wait_turn(ticket)
            with DirectoryLock(
                repo.local_dir / "locks" / "integration.lock", wait=True
            ):
                if sorted(ticket.parent.glob("*.json"))[0] != ticket:
                    raise SoloAIError("Integration queue order changed unexpectedly")
                primary, default = repo.ensure_default_primary_clean()
                _integrate_pending_bootstrap(repo, primary)
                task = store.task(task_id)
                store.require_lease(task, lease)
                worktree = Path(task["worktree"])
                if not repo.is_clean(worktree) or repo.head(worktree) != task.get(
                    "candidate_head"
                ):
                    raise SoloAIError("Candidate changed after Ready; run Ready again")
                task = _sync_default(repo, task)
                # 不能用同步前的策略证明同步后的候选；策略变化必须重新绑定。
                config = load_repo_config(repo, cwd=worktree)
                store.require_slot_layout(config)
                verification = load_verification_config(repo, cwd=worktree)
                require_approval(repo, verification, cwd=worktree)
                _run_declared_secret_scanner(
                    repo, cwd=worktree, scanner=config.secret_scanner
                )
                require_safe(
                    repo,
                    cwd=worktree,
                    base=default,
                    allowlist=config.sensitive_allowlist,
                )
                proof = validate(
                    repo, cwd=worktree, base=default, verification=verification
                )
                store.update_task(
                    task_id,
                    candidate_head=repo.head(worktree),
                    ready_proof=proof["fingerprint"],
                )
                if not repo.is_clean(primary) or not repo.is_clean(worktree):
                    raise SoloAIError("A worktree changed during integration")
                unknown = _unknown_ignored(repo, worktree)
                if unknown:
                    raise SoloAIError(
                        "Unknown or protected ignored files block slot release:\n"
                        + "\n".join(f"- {item}" for item in unknown[:20])
                    )
                _stop_registered_processes(store, task)
                repo.git(["merge", "--ff-only", task["branch"]], cwd=primary)
                integrated_head = repo.head(primary)
                repo.git(["switch", "--detach", integrated_head], cwd=worktree)
                repo.git(["branch", "-d", task["branch"]], cwd=primary)
                store.release(task_id, final_status="finished")
                return {
                    "task_id": task_id,
                    "integrated_head": integrated_head,
                    "proof": proof["fingerprint"],
                    "proof_kind": proof["kind"],
                    "proof_reused": proof.get("reused", False),
                }
        finally:
            ticket.unlink(missing_ok=True)


def abandon(repo: GitRepo, *, task_id: str, lease: str, confirm: str) -> dict[str, Any]:
    if confirm != task_id:
        raise SoloAIError("Abandon requires --confirm with the exact task id")
    config, _, _ = _config_and_mode(repo)
    store = StateStore(repo)
    with store.operation(task_id, lease, "abandon") as task:
        worktree = ensure_within(
            Path(task["worktree"]), repo.root / config.worktree_directory
        )
        _stop_registered_processes(store, task)
        unknown = _unknown_ignored(repo, worktree)
        if unknown:
            store.quarantine(
                task_id, "Unknown or protected ignored files require manual review"
            )
            raise SoloAIError(
                "Unknown or protected ignored files block abandon:\n"
                + "\n".join(f"- {item}" for item in unknown[:20])
            )
        repo.git(["reset", "--hard", task["base_ref"]], cwd=worktree)
        repo.git(["clean", "-fd"], cwd=worktree)
        repo.git(["switch", "--detach", task["base_ref"]], cwd=worktree)
        repo.git(["branch", "-D", task["branch"]], cwd=repo.primary_path, check=False)
        store.release(task_id, final_status="abandoned")
        return {"task_id": task_id, "status": "abandoned"}


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
            code, duration = run_logged(command.argv, cwd=worktree, log_path=pending)
            results.append(
                {
                    "command": command.redacted(),
                    "exit_code": code,
                    "duration_seconds": round(duration, 3),
                    "log": str(pending),
                }
            )
            if code:
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
