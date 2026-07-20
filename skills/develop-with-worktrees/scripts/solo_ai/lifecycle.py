from __future__ import annotations

import os
import socket
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import psutil

from .config import (
    VerificationConfig,
    detect_existing_workflows,
    discover_validation_commands,
    load_repo_config,
    load_verification_config,
    render_agents,
    render_repo_config,
    render_verification_config,
)
from .proof import validate
from .repo import GitRepo
from .safety import require_safe
from .state import StateStore
from .util import (
    DirectoryLock,
    SoloAIError,
    atomic_write_json,
    ensure_within,
    process_matches,
    process_snapshot,
    read_json,
    safe_slug,
    utc_timestamp,
)


def _approvals(repo: GitRepo) -> dict[str, Any]:
    return read_json(
        repo.local_dir / "approvals.json", {"schema_version": 1, "accepted": {}}
    )


def set_local_enabled(repo: GitRepo, *, enabled: bool) -> dict[str, Any]:
    preferences = {
        "schema_version": 1,
        "enabled": enabled,
        "updated_at": utc_timestamp(),
    }
    atomic_write_json(repo.local_dir / "preferences.json", preferences)
    return preferences


def local_enabled(repo: GitRepo) -> bool:
    preferences = read_json(
        repo.local_dir / "preferences.json",
        {"schema_version": 1, "enabled": True},
    )
    return bool(preferences.get("enabled", True))


def approve(repo: GitRepo, verification: VerificationConfig) -> str:
    approvals = _approvals(repo)
    fingerprint = verification.command_fingerprint
    approvals["accepted"][fingerprint] = {
        "commands": list(verification.commands),
        "accepted_at": utc_timestamp(),
    }
    atomic_write_json(repo.local_dir / "approvals.json", approvals)
    return fingerprint


def require_approval(repo: GitRepo, verification: VerificationConfig) -> None:
    if verification.command_fingerprint not in _approvals(repo).get("accepted", {}):
        raise SoloAIError(
            "Validation commands changed and are not approved. Review .solo-ai/verification.toml, then run `approve --accept`."
        )


def initialize(
    repo: GitRepo,
    *,
    slots: int,
    commands: list[str] | None,
    accept: bool,
    accept_static_only: bool,
    compatible: bool,
) -> dict[str, Any]:
    if (repo.root / ".solo-ai" / "config.toml").exists():
        raise SoloAIError("Repository is already initialized")
    if not 1 <= slots <= 5:
        raise SoloAIError("--slots must be between 1 and 5")
    primary, default = repo.ensure_default_primary_clean()
    existing = detect_existing_workflows(repo.root)
    if existing and not compatible:
        raise SoloAIError(
            "Existing worktree/orchestration workflow detected: "
            + ", ".join(existing)
            + ". Re-run with --compatible to defer to it."
        )
    selected = (
        commands if commands is not None else discover_validation_commands(repo.root)
    )
    static_only = not selected
    if static_only and not accept_static_only:
        raise SoloAIError(
            "No validation command was discovered. Review the repository and re-run with --accept-static-only, or provide --verify COMMAND."
        )
    if selected and not accept:
        rendered = "\n".join(f"- {item}" for item in selected)
        raise SoloAIError(
            f"Review the discovered validation commands, then re-run with --accept:\n{rendered}"
        )

    mode = "compatible" if compatible else "managed"
    bootstrap_id = uuid.uuid4().hex[:8]
    branch = f"codex/solo-ai-bootstrap-{bootstrap_id}"
    bootstrap = repo.local_dir / "bootstrap" / bootstrap_id
    repo.git(["worktree", "add", "-b", branch, str(bootstrap), default], cwd=primary)
    try:
        config_dir = bootstrap / ".solo-ai"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.toml").write_text(
            render_repo_config(mode=mode, slots=slots), encoding="utf-8", newline="\n"
        )
        (config_dir / "verification.toml").write_text(
            render_verification_config(selected, static_only=static_only),
            encoding="utf-8",
            newline="\n",
        )
        agents = bootstrap / "AGENTS.md"
        previous = agents.read_text(encoding="utf-8") if agents.exists() else ""
        agents.write_text(
            render_agents(previous, compatible=compatible),
            encoding="utf-8",
            newline="\n",
        )
        repo.git(
            [
                "add",
                "--",
                ".solo-ai/config.toml",
                ".solo-ai/verification.toml",
                "AGENTS.md",
            ],
            cwd=bootstrap,
        )
        repo.git(
            ["commit", "-m", "chore: initialize isolated worktree workflow"],
            cwd=bootstrap,
        )
        repo.git(["merge", "--ff-only", branch], cwd=primary)
    except Exception as exc:
        raise SoloAIError(
            f"Initialization was preserved for inspection at {bootstrap}. Cause: {exc}"
        ) from exc
    else:
        repo.git(["worktree", "remove", str(bootstrap)], cwd=primary)
        repo.git(["branch", "-d", branch], cwd=primary)
    repo.add_local_exclude("/.worktrees/")
    verification = load_verification_config(repo)
    fingerprint = approve(repo, verification)
    StateStore(repo).ensure_slots(load_repo_config(repo))
    return {
        "mode": mode,
        "slots": slots,
        "static_only": static_only,
        "commands": selected,
        "approval": fingerprint,
    }


def start(repo: GitRepo, *, name: str) -> dict[str, Any]:
    config = load_repo_config(repo)
    if not local_enabled(repo):
        raise SoloAIError(
            "develop-with-worktrees is disabled on this machine; run `enable` to opt in again"
        )
    if config.mode == "disabled":
        raise SoloAIError("develop-with-worktrees is disabled for this repository")
    if config.mode == "compatible":
        raise SoloAIError(
            "Compatible mode is active: use the repository's existing worktree workflow instead of claiming a managed slot"
        )
    store = StateStore(repo)
    store.ensure_slots(config)
    default = repo.default_branch()
    base_head = repo.git(["rev-parse", default]).stdout.strip()
    branch = f"{config.branch_prefix}{safe_slug(name)}-{uuid.uuid4().hex[:6]}"
    task = store.allocate(config, name=name, branch=branch, base_head=base_head)
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
            repo.git(["worktree", "add", "--detach", str(worktree), default])
        else:
            if not repo.is_clean(worktree):
                raise SoloAIError(f"Idle slot is not clean: {worktree}")
            if repo.branch(worktree) is not None:
                raise SoloAIError(
                    f"Idle slot is unexpectedly attached to a branch: {worktree}"
                )
            repo.git(["reset", "--hard", default], cwd=worktree)
        repo.git(["switch", "-c", branch, default], cwd=worktree)
        return store.update_task(
            task["id"], status="active", candidate_head=repo.head(worktree)
        )
    except Exception as exc:
        store.quarantine(task["id"], str(exc))
        raise


def commit_task(
    repo: GitRepo, *, task_id: str, lease: str, message: str
) -> dict[str, Any]:
    config = load_repo_config(repo)
    store = StateStore(repo)
    task = store.task(task_id)
    store.require_lease(task, lease)
    worktree = Path(task["worktree"])
    if repo.branch(worktree) != task["branch"]:
        raise SoloAIError("Task branch identity no longer matches its slot")
    if repo.is_clean(worktree):
        if (
            repo.is_ancestor(task["base_head"], repo.head(worktree), cwd=worktree)
            and repo.head(worktree) != task["base_head"]
        ):
            return store.update_task(task_id, candidate_head=repo.head(worktree))
        raise SoloAIError("Task has no changes to commit")
    repo.git(["add", "-A"], cwd=worktree)
    require_safe(
        repo, cwd=worktree, base=None, staged=True, allowlist=config.sensitive_allowlist
    )
    repo.git(["commit", "-m", message], cwd=worktree)
    return store.update_task(
        task_id, candidate_head=repo.head(worktree), ready_proof=None
    )


def _sync_default(repo: GitRepo, task: dict[str, Any]) -> dict[str, Any]:
    worktree = Path(task["worktree"])
    default = repo.default_branch()
    default_head = repo.git(["rev-parse", default], cwd=worktree).stdout.strip()
    if not repo.is_ancestor(default_head, "HEAD", cwd=worktree):
        repo.git(["merge", "--no-edit", default], cwd=worktree)
    task["candidate_head"] = repo.head(worktree)
    return task


def ready(repo: GitRepo, *, task_id: str, lease: str) -> dict[str, Any]:
    config = load_repo_config(repo)
    store = StateStore(repo)
    task = store.task(task_id)
    store.require_lease(task, lease)
    worktree = Path(task["worktree"])
    verification = load_verification_config(repo, cwd=worktree)
    require_approval(repo, verification)
    if not repo.is_clean(worktree):
        raise SoloAIError("Commit all task changes before Ready")
    task = _sync_default(repo, task)
    if not repo.is_clean(worktree):
        raise SoloAIError("Default-branch synchronization left the task dirty")
    default = repo.default_branch()
    require_safe(repo, cwd=worktree, base=default, allowlist=config.sensitive_allowlist)
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
            position = tickets.index(ticket) + 1 if ticket in tickets else 0
            print(f"Waiting in integration queue at position {position}...", flush=True)
            last_report = time.monotonic()
        time.sleep(0.5)


def _known_ignored(path: str) -> bool:
    parts = Path(path).parts
    return (
        any(
            part in {".venv", "node_modules", ".tmp", ".cache", "__pycache__"}
            for part in parts
        )
        or Path(path).name == "uv.toml"
        or Path(path).name.startswith(".env")
    )


def _stop_registered_processes(store: StateStore, task: dict[str, Any]) -> None:
    for snapshot in task.get("processes", []):
        if not process_matches(snapshot):
            raise SoloAIError(
                f"Registered process identity changed or is unknown: PID {snapshot.get('pid')}"
            )
        process = psutil.Process(snapshot["pid"])
        process.terminate()
        try:
            process.wait(timeout=10)
        except psutil.TimeoutExpired as exc:
            raise SoloAIError(
                f"Registered process did not stop: PID {snapshot['pid']}"
            ) from exc
    if task.get("processes"):
        store.update_task(task["id"], processes=[])


def finish(repo: GitRepo, *, task_id: str, lease: str) -> dict[str, Any]:
    config = load_repo_config(repo)
    store = StateStore(repo)
    initial = store.task(task_id)
    store.require_lease(initial, lease)
    verification = load_verification_config(repo, cwd=Path(initial["worktree"]))
    require_approval(repo, verification)
    ticket = _queue_ticket(repo, task_id)
    try:
        _wait_turn(ticket)
        with DirectoryLock(repo.local_dir / "locks" / "integration.lock", wait=True):
            if sorted(ticket.parent.glob("*.json"))[0] != ticket:
                raise SoloAIError("Integration queue order changed unexpectedly")
            primary, default = repo.ensure_default_primary_clean()
            task = store.task(task_id)
            store.require_lease(task, lease)
            worktree = Path(task["worktree"])
            if not repo.is_clean(worktree) or repo.head(worktree) != task.get(
                "candidate_head"
            ):
                raise SoloAIError("Candidate changed after Ready; run Ready again")
            task = _sync_default(repo, task)
            require_safe(
                repo, cwd=worktree, base=default, allowlist=config.sensitive_allowlist
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
            ignored = repo.ignored_untracked(worktree)
            unknown = [item for item in ignored if not _known_ignored(item)]
            if unknown:
                raise SoloAIError(
                    "Unknown ignored files block slot release:\n"
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
            }
    finally:
        ticket.unlink(missing_ok=True)


def abandon(repo: GitRepo, *, task_id: str, lease: str, confirm: str) -> dict[str, Any]:
    if confirm != task_id:
        raise SoloAIError("Abandon requires --confirm with the exact task id")
    config = load_repo_config(repo)
    store = StateStore(repo)
    task = store.task(task_id)
    store.require_lease(task, lease)
    worktree = ensure_within(
        Path(task["worktree"]), repo.root / config.worktree_directory
    )
    _stop_registered_processes(store, task)
    unknown = [
        item for item in repo.ignored_untracked(worktree) if not _known_ignored(item)
    ]
    if unknown:
        store.quarantine(task_id, "Unknown ignored files require manual review")
        raise SoloAIError(
            "Unknown ignored files block abandon:\n"
            + "\n".join(f"- {item}" for item in unknown[:20])
        )
    default = repo.default_branch()
    repo.git(["reset", "--hard", default], cwd=worktree)
    repo.git(["clean", "-fd"], cwd=worktree)
    repo.git(["switch", "--detach", default], cwd=worktree)
    repo.git(["branch", "-D", task["branch"]], cwd=repo.primary_path, check=False)
    store.release(task_id, final_status="abandoned")
    return {"task_id": task_id, "status": "abandoned"}


def dev_start(repo: GitRepo, *, task_id: str, lease: str) -> dict[str, Any]:
    config = load_repo_config(repo)
    if not config.dev_start:
        raise SoloAIError("No lifecycle.dev_start command is configured")
    store = StateStore(repo)
    task = store.task(task_id)
    store.require_lease(task, lease)
    if task.get("processes"):
        raise SoloAIError("Task already has a registered development process")
    block = config.port_base + (int(task["slot_id"]) - 1) * 100
    port = next(
        (candidate for candidate in range(block, block + 100) if _port_free(candidate)),
        None,
    )
    if port is None:
        raise SoloAIError(f"No free port in slot block {block}-{block + 99}")
    command = config.dev_start.format(port=port, slot=task["slot_id"])
    kwargs: dict[str, Any] = {
        "cwd": task["worktree"],
        "shell": True,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(command, **kwargs)
    time.sleep(0.2)
    if process.poll() is not None:
        raise SoloAIError(
            f"Development command exited immediately with code {process.returncode}"
        )
    snapshot = process_snapshot(process.pid)
    store.update_task(task_id, processes=[snapshot], port=port)
    return {"task_id": task_id, "pid": process.pid, "port": port}


def dev_stop(repo: GitRepo, *, task_id: str, lease: str) -> dict[str, Any]:
    store = StateStore(repo)
    task = store.task(task_id)
    store.require_lease(task, lease)
    _stop_registered_processes(store, task)
    return {"task_id": task_id, "status": "stopped"}


def _port_free(port: int) -> bool:
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False
