from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from .config import load_verification_config
from .lifecycle import (
    abandon,
    approve,
    commit_task,
    dev_start,
    dev_stop,
    finish,
    initialize,
    local_enabled,
    ready,
    set_local_enabled,
    start,
)
from .repo import GitRepo
from .state import StateStore
from .util import SoloAIError, directory_size, format_bytes


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dww",
        description="Local-first isolated worktree lifecycle for AI coding tasks",
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path.cwd(),
        help="path inside the target Git repository",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit machine-readable output"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="initialize the repository once")
    init.add_argument("--slots", type=int, default=3)
    init.add_argument("--verify", action="append", default=None, metavar="COMMAND")
    init.add_argument("--accept", action="store_true")
    init.add_argument("--accept-static-only", action="store_true")
    init.add_argument("--compatible", action="store_true")

    approval = sub.add_parser(
        "approve", help="approve the current validation command set"
    )
    approval.add_argument("--accept", action="store_true", required=True)

    sub.add_parser(
        "disable", help="opt out on this machine without changing tracked policy"
    )
    sub.add_parser("enable", help="re-enable managed tasks on this machine")

    start_parser = sub.add_parser("start", help="claim a slot and create a task branch")
    start_parser.add_argument("--name", required=True)

    for name in ("commit", "ready", "finish"):
        item = sub.add_parser(name)
        item.add_argument("--task", required=True)
        item.add_argument("--lease", required=True)
        if name == "commit":
            item.add_argument("--message", required=True)

    status = sub.add_parser("status", help="show slots and tasks")
    status.add_argument("--detailed", action="store_true")

    recover = sub.add_parser(
        "recover", help="rotate a stale task lease without discarding work"
    )
    recover.add_argument("--task", required=True)

    abandon_parser = sub.add_parser(
        "abandon", help="explicitly discard a task after exact confirmation"
    )
    abandon_parser.add_argument("--task", required=True)
    abandon_parser.add_argument("--lease", required=True)
    abandon_parser.add_argument("--confirm", required=True)

    prune = sub.add_parser(
        "prune", help="delete local logs/proofs and invalidate reuse"
    )
    prune.add_argument("--confirm", choices=["PRUNE"], required=True)

    dev = sub.add_parser("dev", help="manage a configured development process")
    dev_sub = dev.add_subparsers(dest="dev_command", required=True)
    for name in ("start", "stop"):
        item = dev_sub.add_parser(name)
        item.add_argument("--task", required=True)
        item.add_argument("--lease", required=True)
    return parser


def _status(repo: GitRepo, *, detailed: bool) -> dict[str, Any]:
    state = StateStore(repo).read()
    config_path = repo.root / ".solo-ai" / "config.toml"
    result: dict[str, Any] = {
        "repository": str(repo.root),
        "initialized": config_path.exists(),
        "default_branch": repo.default_branch(),
        "primary_clean": repo.is_clean(repo.primary_path),
        "local_enabled": local_enabled(repo),
        "slots": list(state.get("slots", {}).values()),
        "tasks": list(state.get("tasks", {}).values()),
    }
    if detailed:
        for slot in result["slots"]:
            size = directory_size(Path(slot["path"]))
            slot["disk_bytes"] = size
            slot["disk"] = format_bytes(size)
        result["local_state_bytes"] = directory_size(repo.local_dir)
    return result


def _prune(repo: GitRepo) -> dict[str, Any]:
    removed: list[str] = []
    for name in ("proofs", "logs"):
        path = repo.local_dir / name
        if path.exists():
            shutil.rmtree(path)
            removed.append(str(path))
    return {"removed": removed, "proof_reuse": "invalidated"}


def _dispatch(args: argparse.Namespace) -> dict[str, Any]:
    repo = GitRepo(args.repo)
    if args.command == "init":
        return initialize(
            repo,
            slots=args.slots,
            commands=args.verify,
            accept=args.accept,
            accept_static_only=args.accept_static_only,
            compatible=args.compatible,
        )
    if args.command == "approve":
        verification = load_verification_config(repo)
        return {
            "approval": approve(repo, verification),
            "commands": list(verification.commands),
        }
    if args.command == "disable":
        return set_local_enabled(repo, enabled=False)
    if args.command == "enable":
        return set_local_enabled(repo, enabled=True)
    if args.command == "start":
        return start(repo, name=args.name)
    if args.command == "commit":
        return commit_task(
            repo, task_id=args.task, lease=args.lease, message=args.message
        )
    if args.command == "ready":
        return ready(repo, task_id=args.task, lease=args.lease)
    if args.command == "finish":
        return finish(repo, task_id=args.task, lease=args.lease)
    if args.command == "status":
        return _status(repo, detailed=args.detailed)
    if args.command == "recover":
        return StateStore(repo).recover(args.task)
    if args.command == "abandon":
        return abandon(repo, task_id=args.task, lease=args.lease, confirm=args.confirm)
    if args.command == "prune":
        return _prune(repo)
    if args.command == "dev" and args.dev_command == "start":
        return dev_start(repo, task_id=args.task, lease=args.lease)
    if args.command == "dev" and args.dev_command == "stop":
        return dev_stop(repo, task_id=args.task, lease=args.lease)
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
            "static checks only"
            if result.get("proof_kind") == "static-only"
            else "validation commands passed"
        )
        return (
            f"Integrated {result['task_id']} at {result['integrated_head']} ({label})."
        )
    return json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)


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
                {"ok": True, "result": result}, ensure_ascii=False, sort_keys=True
            )
        )
    else:
        print(_human(args.command, result))
    return 0
