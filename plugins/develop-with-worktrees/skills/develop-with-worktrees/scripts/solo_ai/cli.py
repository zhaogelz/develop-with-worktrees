from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from . import VERSION
from .config import (
    CommandSpec,
    detect_existing_workflows,
    load_repo_config,
    load_verification_config,
)
from .lifecycle import (
    abandon,
    approve,
    choose,
    commit_task,
    deinit,
    dev_start,
    dev_stop,
    disable,
    finish,
    initialize,
    local_enabled,
    maintenance_lock,
    ready,
    resume_in_place,
    retarget,
    set_local_enabled,
    start,
    warm_slot,
)
from .proof import approval_plan, proof_inputs, validate
from .repo import GitRepo
from .state import FINAL_TASK_STATES, StateStore
from .util import (
    SoloAIError,
    atomic_write_json,
    directory_size,
    ensure_within,
    format_bytes,
    is_link_or_junction,
    new_id,
    read_json,
    sha256_file,
    sha256_text,
    stable_json,
    utc_timestamp,
)
from .validation_queue import estimate_validation, queue_status, set_capacity


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dww",
        description="Local-first isolated worktree lifecycle for Codex coding tasks",
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path.cwd(),
        help="path inside the target Git repository",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable output; task leases are still redacted",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "version", help="show installed workflow version and runtime contract"
    )

    init = sub.add_parser(
        "init", help="show or accept the one-time repository adoption plan"
    )
    init.add_argument("--slots", type=int, default=3)
    init.add_argument(
        "--verify",
        action="append",
        default=None,
        metavar="JSON_ARGV",
        help='explicit command argv, e.g. --verify \'["uv","run","pytest"]\'',
    )
    init.add_argument("--accept", action="store_true")
    init.add_argument("--accept-static-only", action="store_true")
    init.add_argument(
        "--decline",
        action="store_true",
        help="advanced compatibility alias for choosing this repository's local direct mode",
    )

    choose_parser = sub.add_parser(
        "choose",
        help="record one repository modification choice from the first-write prompt",
    )
    choose_parser.add_argument(
        "--mode",
        required=True,
        choices=["isolated", "current-task", "current-repository"],
    )
    choose_parser.add_argument("--slots", type=int, default=3)
    choose_parser.add_argument(
        "--verify",
        action="append",
        default=None,
        metavar="JSON_ARGV",
        help="advanced explicit command argv for isolated setup",
    )
    choose_parser.add_argument(
        "--session",
        help="Codex session identifier from the trusted hook; only for current-task",
    )
    choose_parser.add_argument(
        "--delegate",
        help="one-time parent-task delegation code; only for a child current-task session",
    )

    approval = sub.add_parser(
        "approve", help="locally approve the current full normalized validation plan"
    )
    approval.add_argument("--accept", action="store_true", required=True)
    approval.add_argument(
        "--task",
        help="approve the exact committed candidate policy of one active task",
    )

    sub.add_parser(
        "disable", help="opt out on this machine without changing tracked policy"
    )
    sub.add_parser("enable", help="re-enable managed tasks on this machine")
    settings = sub.add_parser(
        "settings", help="show or adjust machine-local validation capacity"
    )
    settings.add_argument(
        "--validation-capacity",
        metavar="AUTO_OR_1_TO_4",
        help="auto or 1..4; this local setting never changes tracked repository policy",
    )
    sub.add_parser(
        "doctor",
        help="read-only mode, policy, approval, task, and uninstall readiness report",
    )

    start_parser = sub.add_parser("start", help="claim a slot and create a task branch")
    start_parser.add_argument("--name", required=True)
    start_parser.add_argument(
        "--base",
        help="local branch to use as the task base; defaults to the invocation worktree's current branch",
    )
    start_parser.add_argument(
        "--in-place",
        action="store_true",
        help="use the current clean worktree for this one Codex session; no slot or branch is created",
    )
    start_parser.add_argument(
        "--session",
        help="Codex session identifier supplied by the trusted hook; required for in-place tasks",
    )

    commit = sub.add_parser(
        "commit", help="stage only an exact reviewed task path list and commit it"
    )
    commit.add_argument("--task", required=True)
    commit.add_argument("--lease", required=True)
    commit.add_argument("--message", required=True)
    commit.add_argument("--path", action="append", default=[], required=True)
    commit.add_argument("--session")

    for name in ("ready", "finish"):
        item = sub.add_parser(name)
        item.add_argument("--task", required=True)
        item.add_argument("--lease", required=True)
        item.add_argument("--session")

    retarget_parser = sub.add_parser(
        "retarget", help="explicitly rebind a task after its base branch changed"
    )
    retarget_parser.add_argument("--task", required=True)
    retarget_parser.add_argument("--lease", required=True)
    retarget_parser.add_argument("--base", required=True)
    retarget_parser.add_argument(
        "--confirm", required=True, help="exactly TASK_ID:BASE_BRANCH"
    )

    plan = sub.add_parser(
        "plan", help="read the registered verification plan for one task"
    )
    plan.add_argument("--task", required=True)

    verify = sub.add_parser(
        "verify",
        help="run only registered development, ready, or explicit full profiles",
    )
    verify.add_argument("--task", required=True)
    verify.add_argument("--lease", required=True)
    verify.add_argument("--session")
    verify.add_argument(
        "--level", choices=["development", "ready", "full"], default="development"
    )

    status = sub.add_parser("status", help="show masked slots and tasks")
    status.add_argument("--detailed", action="store_true")

    recover = sub.add_parser(
        "recover", help="rotate a stale task lease without discarding work"
    )
    recover.add_argument("--task", required=True)

    abandoned = sub.add_parser(
        "abandon", help="explicitly discard one task after exact confirmation"
    )
    abandoned.add_argument("--task", required=True)
    abandoned.add_argument("--lease", required=True)
    abandoned.add_argument("--confirm", required=True)
    abandoned.add_argument("--session")

    resume = sub.add_parser(
        "resume-in-place",
        help="resume a quarantined in-place task after manually restoring its recorded branch and HEAD",
    )
    resume.add_argument("--task", required=True)
    resume.add_argument("--session", required=True)
    resume.add_argument("--confirm", required=True)

    warm = sub.add_parser(
        "warm-slot", help="serially prepare declared dependencies in one idle slot"
    )
    warm.add_argument("--slot", required=True)

    dev = sub.add_parser("dev", help="manage one configured owned development process")
    dev_sub = dev.add_subparsers(dest="dev_command", required=True)
    for name in ("start", "stop"):
        item = dev_sub.add_parser(name)
        item.add_argument("--task", required=True)
        item.add_argument("--lease", required=True)

    for name, target in (("prune-proofs", "proofs"), ("prune-logs", "logs")):
        prune = sub.add_parser(
            name,
            help=f"explicitly delete local {target} and invalidate dependent reuse",
        )
        prune.add_argument("--confirm", choices=["PRUNE"], required=True)
    prune_slot = sub.add_parser(
        "prune-slot",
        help="plan or execute cleanup of declared paths in one empty managed slot",
    )
    prune_slot.add_argument("--slot", required=True)
    prune_slot.add_argument("--plan", help="plan id returned by a previous prune-slot")
    prune_slot.add_argument(
        "--confirm", help="exact digest returned by a previous prune-slot plan"
    )

    deinitialize = sub.add_parser(
        "deinit", help="safely remove adopted policy and exact managed slots"
    )
    deinitialize.add_argument("--confirm", choices=["DEINIT"], required=True)
    deinitialize.add_argument(
        "--message",
        required=True,
        help="repository-conventional cleanup commit message",
    )
    return parser


def _parse_commands(values: list[str] | None) -> list[CommandSpec] | None:
    if values is None:
        return None
    commands: list[CommandSpec] = []
    for value in values:
        try:
            raw = json.loads(value)
        except json.JSONDecodeError as exc:
            raise SoloAIError(f"--verify must be a JSON argv array: {exc}") from exc
        if (
            not isinstance(raw, list)
            or not raw
            or not all(isinstance(item, str) and item for item in raw)
        ):
            raise SoloAIError("--verify must be a non-empty JSON argv array of strings")
        commands.append(CommandSpec(tuple(raw)))
    return commands


def _status(repo: GitRepo, *, detailed: bool) -> dict[str, Any]:
    store = StateStore(repo)
    state = store.read()
    result: dict[str, Any] = {
        "repository": str(repo.root),
        "mode": "disabled"
        if not local_enabled(repo)
        else "defer"
        if detect_existing_workflows(repo.root)
        else "managed"
        if (repo.policy_path() / ".solo-ai" / "config.toml").exists()
        else "uninitialized",
        "existing_workflows": detect_existing_workflows(repo.root),
        "default_branch": repo.default_branch(),
        "primary_clean": repo.is_clean(repo.primary_path),
        "invocation_worktree": str(repo.root),
        "invocation_worktree_clean": repo.is_clean(repo.root),
        "local_enabled": local_enabled(repo),
        "validation_queue": queue_status(),
        "slots": list(state.get("slots", {}).values()),
        "tasks": [
            StateStore.public_task(task) for task in state.get("tasks", {}).values()
        ],
        "guard_alerts": store.guard_alerts(),
    }
    if detailed:
        for slot in result["slots"]:
            size = directory_size(Path(slot["path"]))
            slot["disk_bytes"] = size
            slot["disk"] = format_bytes(size)
        result["local_state_bytes"] = directory_size(repo.local_dir)
    return result


def _version() -> dict[str, Any]:
    plugin_root = Path(__file__).resolve().parents[4]
    manifest_path = plugin_root / ".codex-plugin" / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {
        "version": VERSION,
        "plugin_version": manifest.get("version"),
        "verification_schema": 3,
        "state_schema": 3,
        "codex_guard": "PreToolUse deny on supported local tool paths after user trusts this plugin hook",
        "script": str(Path(sys.argv[0]).resolve()),
        "validation_queue": queue_status(),
    }


def _doctor(repo: GitRepo) -> dict[str, Any]:
    report = _status(repo, detailed=False)
    policy = repo.policy_path()
    if report["mode"] == "managed":
        verification = load_verification_config(repo, cwd=policy)
        plan = approval_plan(repo, cwd=policy, verification=verification)
        from .lifecycle import _approval_fingerprint

        fingerprint, _ = _approval_fingerprint(repo, verification, cwd=policy)
        from .util import read_json

        report["approval_current"] = fingerprint in read_json(
            repo.local_dir / "approvals.json", {"accepted": {}}
        ).get("accepted", {})
        report["validation_plan"] = plan
    report["deinit_ready"] = (
        report["mode"] == "managed"
        and report["primary_clean"]
        and not any(
            task["status"] not in FINAL_TASK_STATES
            for task in StateStore(repo).read()["tasks"].values()
        )
    )
    report["uninstall_rule"] = (
        "Run deinit successfully before removing the Codex plugin. The plugin registry never scans disks."
    )
    report["hook_trust"] = (
        "Codex plugin hooks require a user trust decision after install or hook changes. Run /hooks and start a new task before relying on in-place protection."
    )
    return report


def _require_idle(repo: GitRepo) -> None:
    state = StateStore(repo).read()
    if any(
        task.get("status") not in FINAL_TASK_STATES for task in state["tasks"].values()
    ):
        raise SoloAIError("Active or quarantined tasks block pruning")
    if any((repo.local_dir / "queue").glob("*.json")):
        raise SoloAIError("Integration queue tickets block pruning")
    locks = repo.local_dir / "locks"
    # A stale lock is not removed by pruning; doctor/recover must assess it first.
    if locks.exists() and any(path.name != "state.lock" for path in locks.iterdir()):
        raise SoloAIError("A lifecycle lock exists; pruning is unsafe")


def _cleanup_target(path: Path, root: Path) -> dict[str, Any]:
    """为声明目标生成可复核摘要；任何保护项或链接都会停止整次清理。"""
    if is_link_or_junction(path):
        raise SoloAIError(f"Cleanup target contains a link or junction: {path}")
    path = ensure_within(path, root)
    entries: list[dict[str, Any]] = []
    total_bytes = 0

    def add(candidate: Path, *, directory: bool) -> None:
        nonlocal total_bytes
        if is_link_or_junction(candidate):
            raise SoloAIError(
                f"Cleanup target contains a link or junction: {candidate}"
            )
        if candidate.name.startswith(".env"):
            raise SoloAIError(
                f"Cleanup target contains protected .env content: {candidate}"
            )
        relative = str(candidate.relative_to(root)).replace("\\", "/")
        if directory:
            entries.append({"path": relative, "kind": "directory"})
            return
        size = candidate.stat().st_size
        total_bytes += size
        entries.append(
            {
                "path": relative,
                "kind": "file",
                "bytes": size,
                "sha256": sha256_file(candidate),
            }
        )

    if path.is_file():
        add(path, directory=False)
    else:
        for current, directories, files in os.walk(path, followlinks=False):
            current_path = Path(current)
            for name in sorted(directories):
                add(current_path / name, directory=True)
            for name in sorted(files):
                add(current_path / name, directory=False)
    return {
        "path": str(path.relative_to(root)).replace("\\", "/"),
        "kind": "file" if path.is_file() else "directory",
        "bytes": total_bytes,
        "delete_reason": "declared cleanup.owned_paths entry",
        "contents_digest": sha256_text(stable_json(entries)),
    }


def _slot_prune_payload(repo: GitRepo, *, slot: str) -> dict[str, Any]:
    policy = repo.policy_path()
    config = load_repo_config(repo, cwd=policy)
    store = StateStore(repo)
    state = store.require_slot_layout(config)
    details = state["slots"].get(slot)
    if not details or details.get("status") not in {"idle", "inactive"}:
        raise SoloAIError("Only an empty idle or inactive slot can be pruned")
    root = ensure_within(
        Path(details["path"]), repo.primary_path / config.worktree_directory
    )
    if not any(item.path == root for item in repo.worktrees()):
        if root.exists():
            raise SoloAIError(
                "Slot path is not registered with Git and is retained; recover or inspect it manually"
            )
        return {"slot": slot, "worktree_retained": False, "targets": []}
    store.require_slot_ownership(slot, root)
    targets: list[dict[str, Any]] = []
    for relative in config.cleanup_owned_paths:
        candidate = root / relative
        if is_link_or_junction(candidate):
            raise SoloAIError(
                f"Cleanup target contains a link or junction: {candidate}"
            )
        candidate = ensure_within(candidate, root)
        if candidate.exists() or candidate.is_symlink():
            targets.append(_cleanup_target(candidate, root))
    return {
        "slot": slot,
        "worktree": str(root),
        "worktree_retained": True,
        "targets": targets,
        "owned_paths": list(config.cleanup_owned_paths),
    }


def _plan_slot_prune(repo: GitRepo, *, slot: str) -> dict[str, Any]:
    payload = _slot_prune_payload(repo, slot=slot)
    digest = sha256_text(stable_json(payload))
    plan_id = new_id(f"cleanup-slot-{slot}")
    plan = {
        "schema_version": 1,
        "id": plan_id,
        "digest": digest,
        "created_at": utc_timestamp(),
        "payload": payload,
    }
    atomic_write_json(repo.local_dir / "cleanup-plans" / f"{plan_id}.json", plan)
    return {
        "status": "planned",
        "plan_id": plan_id,
        "digest": digest,
        **payload,
        "next": f"prune-slot --slot {slot} --plan {plan_id} --confirm {digest}",
    }


def _execute_slot_prune(
    repo: GitRepo, *, slot: str, plan_id: str, confirm: str
) -> dict[str, Any]:
    plan = read_json(repo.local_dir / "cleanup-plans" / f"{plan_id}.json", {})
    if not plan or plan.get("schema_version") != 1:
        raise SoloAIError("Unknown cleanup plan; generate a new plan before pruning")
    if plan.get("payload", {}).get("slot") != slot:
        raise SoloAIError("Cleanup plan belongs to a different slot")
    if confirm != plan.get("digest"):
        raise SoloAIError("Cleanup confirmation must exactly match the planned digest")
    current = _slot_prune_payload(repo, slot=slot)
    if sha256_text(stable_json(current)) != plan["digest"]:
        raise SoloAIError(
            "Cleanup plan changed after review; nothing was deleted. Generate and review a new plan."
        )
    if not current["worktree_retained"]:
        return {
            "status": "pruned",
            "slot": slot,
            "plan_id": plan_id,
            "removed": [],
            "worktree_retained": False,
        }
    root = Path(str(current["worktree"]))
    removed: list[str] = []
    for target in current["targets"]:
        path = root / str(target["path"])
        # 计划已重新摘要；再次拒绝链接或 junction，防止检查和删除之间路径替换。
        if is_link_or_junction(path):
            raise SoloAIError(f"Cleanup target became a link or junction: {path}")
        path = ensure_within(path, root)
        if path.is_dir():
            shutil.rmtree(path)
        elif path.is_file():
            path.unlink()
        else:
            raise SoloAIError(f"Cleanup target changed type: {path}")
        removed.append(str(path))
    return {
        "status": "pruned",
        "slot": slot,
        "plan_id": plan_id,
        "removed": removed,
        "worktree_retained": current["worktree_retained"],
    }


def _prune(
    repo: GitRepo,
    *,
    kind: str,
    slot: str | None = None,
    plan_id: str | None = None,
    confirm: str | None = None,
) -> dict[str, Any]:
    with maintenance_lock(repo):
        _require_idle(repo)
        if kind in {"proofs", "logs"}:
            targets = [repo.local_dir / kind]
            if kind == "proofs":
                targets.append(repo.local_dir / "profile-proofs")
            else:
                targets.append(repo.local_dir / "validation-runs")
            removed: list[str] = []
            for target in targets:
                if target.exists():
                    shutil.rmtree(target)
                    removed.append(str(target))
            return {
                "removed": removed,
                "proof_reuse": "invalidated"
                if kind in {"proofs", "logs"}
                else "unchanged",
            }
        if not slot:
            raise SoloAIError("Slot is required")
        if plan_id is None and confirm is None:
            return _plan_slot_prune(repo, slot=slot)
        if not plan_id or not confirm:
            raise SoloAIError("PruneSlot execution requires both --plan and --confirm")
        return _execute_slot_prune(repo, slot=slot, plan_id=plan_id, confirm=confirm)


def _dispatch(args: argparse.Namespace) -> dict[str, Any]:
    repo = GitRepo(args.repo)
    if args.command == "init":
        return initialize(
            repo,
            slots=args.slots,
            commands=_parse_commands(args.verify),
            accept=args.accept,
            accept_static_only=args.accept_static_only,
            decline=args.decline,
        )
    if args.command == "choose":
        return choose(
            repo,
            mode=args.mode,
            slots=args.slots,
            commands=_parse_commands(args.verify),
            session_id=args.session,
            delegation_code=args.delegate,
        )
    if args.command == "version":
        return _version()
    if args.command == "approve":
        if args.task:
            task = StateStore(repo).task(args.task)
            worktree = Path(str(task["worktree"]))
            verification = load_verification_config(repo, cwd=worktree)
            return approve(repo, verification, cwd=worktree)
        verification = load_verification_config(repo)
        return approve(repo, verification)
    if args.command == "disable":
        return disable(repo)
    if args.command == "enable":
        return set_local_enabled(repo, enabled=True)
    if args.command == "settings":
        return (
            set_capacity(args.validation_capacity)
            if args.validation_capacity is not None
            else queue_status()
        )
    if args.command == "doctor":
        return _doctor(repo)
    if args.command == "start":
        return start(
            repo,
            name=args.name,
            base=args.base,
            in_place=args.in_place,
            session_id=args.session,
        )
    if args.command == "commit":
        return commit_task(
            repo,
            task_id=args.task,
            lease=args.lease,
            message=args.message,
            paths=args.path,
            session_id=args.session,
        )
    if args.command == "ready":
        return ready(repo, task_id=args.task, lease=args.lease, session_id=args.session)
    if args.command == "finish":
        return finish(
            repo, task_id=args.task, lease=args.lease, session_id=args.session
        )
    if args.command == "retarget":
        return retarget(
            repo,
            task_id=args.task,
            lease=args.lease,
            base=args.base,
            confirm=args.confirm,
        )
    if args.command == "plan":
        task = StateStore(repo).task(args.task)
        worktree = Path(str(task["worktree"]))
        verification = load_verification_config(repo, cwd=worktree)
        verification_base = str(task.get("start_head") or task["base_ref"])
        force_task_scope = task.get("mode") == "in-place"
        inputs, _ = proof_inputs(
            repo,
            cwd=worktree,
            base=verification_base,
            verification=verification,
            task_id=task["id"],
            levels=("ready",),
            force_task_scope=force_task_scope,
        )
        _, records = proof_inputs(
            repo,
            cwd=worktree,
            base=verification_base,
            verification=verification,
            task_id=task["id"],
            levels=("development", "ready", "full"),
            force_task_scope=force_task_scope,
        )
        estimate = estimate_validation(
            [
                (
                    profile.profile_id,
                    [command.fingerprint for command in profile.commands],
                )
                for profile, _, _ in records
            ]
        )
        profiles = [
            {
                "id": profile.profile_id,
                "level": profile.level,
                "resource_class": profile.resource_class,
                "timeout_seconds": profile.timeout_seconds,
                "commands": [command.redacted() for command in profile.commands],
                "fingerprint": fingerprint,
                "estimated_seconds": estimate["profile_seconds"][index],
            }
            for index, (profile, _, fingerprint) in enumerate(records)
        ]
        return {
            "task_id": task["id"],
            "base_ref": verification_base,
            "changed_files": inputs["files"],
            "unmapped_files": inputs["unmapped_files"],
            "profiles": profiles,
            "estimated_seconds": estimate["estimated_seconds"],
            "advisory": estimate["advisory"],
        }
    if args.command == "verify":
        store = StateStore(repo)
        with store.operation(args.task, args.lease, "verify") as task:
            worktree = Path(str(task["worktree"]))
            from .lifecycle import _assert_in_place_binding, _is_in_place

            if _is_in_place(task):
                _assert_in_place_binding(repo, store, task, session_id=args.session)
            if not repo.is_clean(worktree):
                raise SoloAIError(
                    "Commit task changes before producing reusable verification evidence"
                )
            verification = load_verification_config(repo, cwd=worktree)
            proof = validate(
                repo,
                cwd=worktree,
                base=str(task.get("start_head") or task["base_ref"]),
                verification=verification,
                task_id=task["id"],
                level=args.level,
                force_task_scope=_is_in_place(task),
            )
            return {
                "task_id": task["id"],
                "level": args.level,
                "proof": proof["fingerprint"],
                "reused": proof.get("reused", False),
                "kind": proof["kind"],
            }
    if args.command == "status":
        return _status(repo, detailed=args.detailed)
    if args.command == "recover":
        return StateStore(repo).recover(args.task)
    if args.command == "resume-in-place":
        return resume_in_place(
            repo,
            task_id=args.task,
            session_id=args.session,
            confirm=args.confirm,
        )
    if args.command == "abandon":
        return abandon(
            repo,
            task_id=args.task,
            lease=args.lease,
            confirm=args.confirm,
            session_id=args.session,
        )
    if args.command == "warm-slot":
        return warm_slot(repo, slot_id=args.slot)
    if args.command == "dev" and args.dev_command == "start":
        return dev_start(repo, task_id=args.task, lease=args.lease)
    if args.command == "dev" and args.dev_command == "stop":
        return dev_stop(repo, task_id=args.task, lease=args.lease)
    if args.command == "prune-proofs":
        return _prune(repo, kind="proofs")
    if args.command == "prune-logs":
        return _prune(repo, kind="logs")
    if args.command == "prune-slot":
        return _prune(
            repo,
            kind="slot",
            slot=args.slot,
            plan_id=args.plan,
            confirm=args.confirm,
        )
    if args.command == "deinit":
        return deinit(repo, confirm=args.confirm, message=args.message)
    raise SoloAIError("Unsupported command")


def _human(command: str, result: dict[str, Any]) -> str:
    if command == "choose":
        choice = result.get("choice")
        if choice == "isolated":
            return "已选择独立目录开发；之后的普通修改会自动隔离。"
        if choice == "current-repository":
            return "已记住：此仓库在本机以后直接在当前目录修改。"
        if result.get("delegated"):
            return "已加入本次当前目录修改授权；不要启动 DWW 生命周期。"
        return "\n".join(
            (
                "本次已切换为当前目录直接修改；不要启动 DWW 生命周期。",
                "仅在委托子智能体修改时传递此一次性委托码：",
                str(result["delegation_code"]),
            )
        )
    if command == "start":
        mode = result.get("mode", "isolated")
        return "\n".join(
            (
                f"Task: {result['id']}",
                f"Mode: {mode}",
                f"Worktree: {result['worktree']}",
                f"Branch: {result['branch']}",
                f"Lease: {result['lease']}",
            )
        )
    if command == "finish":
        label = (
            "static checks only; no test command ran"
            if result.get("proof_kind") == "static-only"
            else "validation commands passed"
        )
        verb = (
            "Completed in place" if result.get("mode") == "in-place" else "Integrated"
        )
        return f"{verb} {result['task_id']} at {result['integrated_head']} ({label})."
    if command in {"recover", "resume-in-place"}:
        return "\n".join((f"Task: {result['id']}", f"Lease: {result['lease']}"))
    return json.dumps(
        _redact_leases(result), ensure_ascii=False, indent=2, sort_keys=True
    )


def _redact_leases(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _redact_leases(item)
            for key, item in value.items()
            if key
            not in {"lease", "lease_owner", "session_fingerprint", "delegation_code"}
        }
    if isinstance(value, list):
        return [_redact_leases(item) for item in value]
    return value


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        result = _dispatch(args)
    except SoloAIError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(
            json.dumps(
                {"ok": True, "result": _redact_leases(result)},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        print(_human(args.command, result))
    return 0
