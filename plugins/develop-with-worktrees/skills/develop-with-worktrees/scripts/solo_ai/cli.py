from __future__ import annotations

import argparse
import fnmatch
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from .config import (
    CommandSpec,
    detect_existing_workflows,
    load_repo_config,
    load_verification_config,
)
from .lifecycle import (
    abandon,
    approve,
    commit_task,
    deinit,
    disable,
    dev_start,
    dev_stop,
    finish,
    initialize,
    local_enabled,
    maintenance_lock,
    ready,
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
    new_id,
    read_json,
    sha256_file,
    sha256_text,
    stable_json,
    utc_timestamp,
)


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
        help="disable this repository locally without editing tracked files",
    )

    approval = sub.add_parser(
        "approve", help="locally approve the current full normalized validation plan"
    )
    approval.add_argument("--accept", action="store_true", required=True)

    sub.add_parser(
        "disable", help="opt out on this machine without changing tracked policy"
    )
    sub.add_parser("enable", help="re-enable managed tasks on this machine")
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

    commit = sub.add_parser(
        "commit", help="stage only an exact reviewed task path list and commit it"
    )
    commit.add_argument("--task", required=True)
    commit.add_argument("--lease", required=True)
    commit.add_argument("--message", required=True)
    commit.add_argument("--path", action="append", default=[], required=True)

    for name in ("ready", "finish"):
        item = sub.add_parser(name)
        item.add_argument("--task", required=True)
        item.add_argument("--lease", required=True)

    retarget_parser = sub.add_parser(
        "retarget", help="explicitly rebind a task after its base branch changed"
    )
    retarget_parser.add_argument("--task", required=True)
    retarget_parser.add_argument("--lease", required=True)
    retarget_parser.add_argument("--base", required=True)
    retarget_parser.add_argument(
        "--confirm", required=True, help="exactly TASK_ID:BASE_BRANCH"
    )

    plan = sub.add_parser("plan", help="read the registered verification plan for one task")
    plan.add_argument("--task", required=True)

    verify = sub.add_parser(
        "verify", help="run only registered development, ready, or explicit full profiles"
    )
    verify.add_argument("--task", required=True)
    verify.add_argument("--lease", required=True)
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
    state = StateStore(repo).read()
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
        "local_enabled": local_enabled(repo),
        "slots": list(state.get("slots", {}).values()),
        "tasks": [
            StateStore.public_task(task) for task in state.get("tasks", {}).values()
        ],
    }
    if detailed:
        for slot in result["slots"]:
            size = directory_size(Path(slot["path"]))
            slot["disk_bytes"] = size
            slot["disk"] = format_bytes(size)
        result["local_state_bytes"] = directory_size(repo.local_dir)
    return result


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
    """为两阶段清理构建内容摘要；遇到链接即停止，不沿链接走出槽位。"""
    path = ensure_within(path, root)
    if path.is_symlink():
        raise SoloAIError(f"Refusing symlink cleanup target: {path}")
    entries: list[dict[str, Any]] = []
    if path.is_file():
        entries.append({"path": path.name, "kind": "file", "sha256": sha256_file(path)})
    else:
        for current, directories, files in os.walk(path):
            current_path = Path(current)
            for name in sorted([*directories, *files]):
                candidate = current_path / name
                if candidate.is_symlink():
                    raise SoloAIError(f"Refusing cleanup target containing symlink: {candidate}")
                relative = str(candidate.relative_to(root)).replace("\\", "/")
                if candidate.is_file():
                    entries.append(
                        {"path": relative, "kind": "file", "sha256": sha256_file(candidate)}
                    )
                else:
                    entries.append({"path": relative, "kind": "directory"})
    return {
        "path": str(path.relative_to(root)).replace("\\", "/"),
        "kind": "file" if path.is_file() else "directory",
        "entries": entries,
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
        return {"slot": slot, "worktree_retained": False, "targets": [], "protected": []}
    store.require_slot_ownership(slot, root)
    protected = [
        child.name
        for child in root.iterdir()
        if any(fnmatch.fnmatchcase(child.name, pattern) for pattern in config.cleanup_protected_paths)
    ]
    targets: list[dict[str, Any]] = []
    for relative in config.cleanup_owned_paths:
        if any(fnmatch.fnmatchcase(relative, pattern) for pattern in config.cleanup_protected_paths):
            continue
        candidate = ensure_within(root / relative, root)
        if candidate.exists() or candidate.is_symlink():
            targets.append(_cleanup_target(candidate, root))
    return {
        "slot": slot,
        "worktree": str(root),
        "worktree_retained": True,
        "targets": targets,
        "protected": sorted(protected),
        "owned_paths": list(config.cleanup_owned_paths),
        "protected_paths": list(config.cleanup_protected_paths),
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
            "protected_retained": [],
            "worktree_retained": False,
        }
    root = Path(str(current["worktree"]))
    removed: list[str] = []
    for target in current["targets"]:
        path = ensure_within(root / str(target["path"]), root)
        # payload 已重新摘要；再次拒绝符号链接，防止检查和删除之间路径替换。
        if path.is_symlink():
            raise SoloAIError(f"Cleanup target became a symlink: {path}")
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
        "protected_retained": current["protected"],
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
        return _execute_slot_prune(
            repo, slot=slot, plan_id=plan_id, confirm=confirm
        )


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
    if args.command == "approve":
        verification = load_verification_config(repo)
        return approve(repo, verification)
    if args.command == "disable":
        return disable(repo)
    if args.command == "enable":
        return set_local_enabled(repo, enabled=True)
    if args.command == "doctor":
        return _doctor(repo)
    if args.command == "start":
        return start(repo, name=args.name, base=args.base)
    if args.command == "commit":
        return commit_task(
            repo,
            task_id=args.task,
            lease=args.lease,
            message=args.message,
            paths=args.path,
        )
    if args.command == "ready":
        return ready(repo, task_id=args.task, lease=args.lease)
    if args.command == "finish":
        return finish(repo, task_id=args.task, lease=args.lease)
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
        inputs, records = proof_inputs(
            repo,
            cwd=worktree,
            base=str(task["base_ref"]),
            verification=verification,
            task_id=task["id"],
        )
        return {
            "task_id": task["id"],
            "base_ref": task["base_ref"],
            "changed_files": inputs["files"],
            "unmapped_files": inputs["unmapped_files"],
            "profiles": [
                {
                    "id": profile.profile_id,
                    "level": profile.level,
                    "resource_class": profile.resource_class,
                    "timeout_seconds": profile.timeout_seconds,
                    "commands": [command.redacted() for command in profile.commands],
                    "fingerprint": fingerprint,
                }
                for profile, _, fingerprint in records
            ],
        }
    if args.command == "verify":
        store = StateStore(repo)
        with store.operation(args.task, args.lease, "verify") as task:
            worktree = Path(str(task["worktree"]))
            if not repo.is_clean(worktree):
                raise SoloAIError("Commit task changes before producing reusable verification evidence")
            verification = load_verification_config(repo, cwd=worktree)
            proof = validate(
                repo,
                cwd=worktree,
                base=str(task["base_ref"]),
                verification=verification,
                task_id=task["id"],
                level=args.level,
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
    if args.command == "abandon":
        return abandon(repo, task_id=args.task, lease=args.lease, confirm=args.confirm)
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
    if command == "start":
        return "\n".join(
            (
                f"Task: {result['id']}",
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
        return (
            f"Integrated {result['task_id']} at {result['integrated_head']} ({label})."
        )
    if command == "recover":
        return "\n".join((f"Task: {result['id']}", f"Lease: {result['lease']}"))
    return json.dumps(
        _redact_leases(result), ensure_ascii=False, indent=2, sort_keys=True
    )


def _redact_leases(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _redact_leases(item)
            for key, item in value.items()
            if key not in {"lease", "lease_owner"}
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
