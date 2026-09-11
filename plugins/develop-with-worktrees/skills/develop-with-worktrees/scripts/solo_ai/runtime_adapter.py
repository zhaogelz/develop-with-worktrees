from __future__ import annotations

import copy
import fnmatch
import uuid
from pathlib import Path
from typing import Any

from .config import CommandSpec, RepoConfig, load_repo_config, load_verification_config
from .proof import require_approved_plan
from .repo import GitRepo
from .util import (
    SoloAIError,
    atomic_write_json,
    read_json,
    run_logged,
    sha256_file,
    sha256_text,
    stable_json,
    utc_timestamp,
)

ADAPTER_CONTEXT_SCHEMA = 1
ADAPTER_RECEIPT_SCHEMA = 1
BATCH_PORT_BLOCK_OFFSET = 3200


def _context_record(operation: str, context: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": ADAPTER_CONTEXT_SCHEMA,
        "contract": "dww-runtime-adapter-v1",
        "operation": operation,
        **copy.deepcopy(context),
    }


def _adapter_input_hashes(
    repo: GitRepo, *, cwd: Path, patterns: tuple[str, ...]
) -> dict[str, str]:
    tracked = [
        item
        for item in repo.git(["ls-files", "-z"], cwd=cwd).stdout.split("\0")
        if item
    ]
    hashes = {
        relative: sha256_file(cwd / relative)
        for relative in tracked
        if (cwd / relative).is_file()
        and any(fnmatch.fnmatchcase(relative, pattern) for pattern in patterns)
    }
    if not hashes:
        raise SoloAIError("Runtime Adapter input_paths did not match any tracked file")
    return hashes


def _require_approval(repo: GitRepo, *, cwd: Path) -> None:
    verification = load_verification_config(repo, cwd=cwd)
    require_approved_plan(
        repo,
        cwd=cwd,
        verification=verification,
        message="This machine has not approved the runtime Adapter plan.",
    )


def _logs_exist(receipt: dict[str, Any]) -> bool:
    log = receipt.get("log")
    return bool(
        log
        and Path(str(log)).is_file()
        and sha256_file(Path(str(log))) == receipt.get("log_sha256")
    )


def _content_address_log(repo: GitRepo, temporary: Path) -> tuple[Path, str]:
    digest = sha256_file(temporary)
    target = repo.local_dir / "logs" / "content" / f"{digest}.log"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        temporary.unlink(missing_ok=True)
    else:
        temporary.replace(target)
    return target, digest


def _invoke(
    repo: GitRepo,
    *,
    cwd: Path,
    operation: str,
    command: CommandSpec,
    timeout_seconds: float,
    context: dict[str, Any],
    reusable_success: bool,
) -> dict[str, Any]:
    context_record = _context_record(operation, context)
    context_digest = sha256_text(stable_json(context_record))
    invocation_id = (
        f"adapter-{context_digest[:32]}"
        if reusable_success
        else f"adapter-{uuid.uuid4().hex}"
    )
    context_path = (
        repo.local_dir / "runtime-adapter" / "contexts" / f"{invocation_id}.json"
    )
    receipt_path = (
        repo.local_dir / "runtime-adapter" / "receipts" / f"{invocation_id}.json"
    )
    atomic_write_json(context_path, context_record)
    existing = read_json(receipt_path, {})
    if reusable_success and existing:
        if (
            existing.get("schema_version") != ADAPTER_RECEIPT_SCHEMA
            or existing.get("context_digest") != context_digest
            or existing.get("command_digest") != command.fingerprint
        ):
            raise SoloAIError(
                "Stored runtime Adapter receipt identity changed; inspect it before retrying"
            )
        if existing.get("result") == "passed" and _logs_exist(existing):
            return {**existing, "reused": True}
    pending = repo.local_dir / "logs" / "pending" / f"{invocation_id}.log"
    result = run_logged(
        (*command.argv, str(context_path)),
        cwd=cwd,
        log_path=pending,
        timeout_seconds=timeout_seconds,
        receipt_path=repo.local_dir
        / "runtime-adapter"
        / "runs"
        / f"{invocation_id}.json",
        receipt_metadata={
            "operation": operation,
            "context_digest": context_digest,
        },
    )
    log_path, log_digest = _content_address_log(repo, pending)
    receipt = {
        "schema_version": ADAPTER_RECEIPT_SCHEMA,
        "invocation_id": invocation_id,
        "operation": operation,
        "context": str(context_path),
        "context_digest": context_digest,
        "command": command.redacted(),
        "command_digest": command.fingerprint,
        "result": "passed" if result.returncode == 0 else "failed",
        "exit_code": result.returncode,
        "timed_out": result.timed_out,
        "duration_seconds": round(result.duration_seconds, 3),
        "log": str(log_path),
        "log_sha256": log_digest,
        "created_at": utc_timestamp(),
    }
    atomic_write_json(receipt_path, receipt)
    if result.returncode != 0:
        raise SoloAIError(
            f"Runtime Adapter {operation} {'timed out' if result.timed_out else 'failed'}. Local redacted log: {log_path}"
        )
    return {**receipt, "reused": False}


def _task_activation_request(
    repo: GitRepo,
    *,
    task: dict[str, Any],
) -> tuple[RepoConfig, CommandSpec | None, dict[str, Any]]:
    worktree = Path(str(task["worktree"]))
    config = load_repo_config(repo, cwd=worktree)
    command = config.runtime_adapter.activate
    if command is None:
        return config, None, {}
    if task.get("slot_id") is None:
        raise SoloAIError(
            "runtime_adapter.activate requires an isolated task with a managed slot"
        )
    _require_approval(repo, cwd=worktree)
    adapter_inputs = _adapter_input_hashes(
        repo, cwd=worktree, patterns=config.runtime_adapter.input_paths
    )
    slot_number = int(str(task["slot_id"]))
    slot_port_base = config.port_base + (slot_number - 1) * 100
    return (
        config,
        command,
        {
            "reason": "task-started",
            "task_id": task["id"],
            "task_mode": task.get("mode"),
            "slot_id": task["slot_id"],
            "worktree": str(worktree.resolve()),
            "base_ref": task.get("base_ref"),
            "base_head": task.get("base_head"),
            "port_block_start": slot_port_base,
            "port_block_end": slot_port_base + 99,
            "adapter_inputs": adapter_inputs,
        },
    )


def require_failed_task_runtime_activation(
    repo: GitRepo, *, task: dict[str, Any]
) -> RepoConfig:
    config, command, context = _task_activation_request(repo, task=task)
    if command is None or config.runtime_adapter.release is None:
        raise SoloAIError(
            "Runtime Adapter repair requires both activate and release commands"
        )
    context_digest = sha256_text(stable_json(_context_record("activate", context)))
    invocation_id = f"adapter-{context_digest[:32]}"
    receipt = read_json(
        repo.local_dir / "runtime-adapter" / "receipts" / f"{invocation_id}.json",
        {},
    )
    if (
        receipt.get("schema_version") != ADAPTER_RECEIPT_SCHEMA
        or receipt.get("context_digest") != context_digest
        or receipt.get("command_digest") != command.fingerprint
        or receipt.get("result") != "failed"
        or not _logs_exist(receipt)
    ):
        raise SoloAIError(
            "Runtime Adapter repair requires the exact persisted failed activation receipt"
        )
    return config


def activate_task_runtime(
    repo: GitRepo,
    *,
    task: dict[str, Any],
) -> dict[str, Any]:
    worktree = Path(str(task["worktree"]))
    config, command, context = _task_activation_request(repo, task=task)
    if command is None:
        return {"configured": False, "operation": "activate"}
    receipt = _invoke(
        repo,
        cwd=worktree,
        operation="activate",
        command=command,
        timeout_seconds=config.runtime_adapter.timeout_seconds,
        context=context,
        reusable_success=True,
    )
    return {"configured": True, **receipt}


def release_task_runtime(
    repo: GitRepo,
    *,
    task: dict[str, Any],
    reason: str,
    candidate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    worktree = Path(str(task["worktree"]))
    config = load_repo_config(repo, cwd=worktree)
    command = config.runtime_adapter.release
    repair = task.get("runtime_adapter_repair") or {}
    if repair.get("release_required") and command is None:
        raise SoloAIError("Runtime Adapter repair removed its required release command")
    if command is None:
        return {"configured": False, "operation": "release"}
    _require_approval(repo, cwd=worktree)
    selected_candidate = candidate or {}
    adapter_inputs = _adapter_input_hashes(
        repo, cwd=worktree, patterns=config.runtime_adapter.input_paths
    )
    context = {
        "reason": "runtime-adapter-repair" if repair else reason,
        "task_id": task["id"],
        "task_mode": task.get("mode"),
        "worktree": str(worktree.resolve()),
        "base_ref": task.get("base_ref"),
        "base_head": task.get("base_head"),
        "candidate_id": selected_candidate.get("candidate_id"),
        "candidate_head": selected_candidate.get("head") or task.get("candidate_head"),
        "registered_processes": copy.deepcopy(task.get("processes", [])),
        "adapter_inputs": adapter_inputs,
    }
    if repair:
        context["repair_mode"] = True
    receipt = _invoke(
        repo,
        cwd=worktree,
        operation="release",
        command=command,
        timeout_seconds=config.runtime_adapter.timeout_seconds,
        context=context,
        reusable_success=True,
    )
    return {"configured": True, **receipt}


def _batch_context(
    repo: GitRepo,
    *,
    batch: dict[str, Any],
    worktree: Path,
) -> tuple[Any, dict[str, Any]]:
    config = load_repo_config(repo, cwd=worktree)
    adapter_inputs = _adapter_input_hashes(
        repo, cwd=worktree, patterns=config.runtime_adapter.input_paths
    )
    runtime_cycle = batch.get("runtime_cycle")
    if (
        isinstance(runtime_cycle, bool)
        or not isinstance(runtime_cycle, int)
        or runtime_cycle < 1
    ):
        raise SoloAIError("Batch Runtime Adapter requires a positive runtime_cycle")
    port_block_start = config.port_base + BATCH_PORT_BLOCK_OFFSET
    context = {
        "batch_id": batch["id"],
        "runtime_cycle": runtime_cycle,
        "candidate_ids": list(batch["candidate_ids"]),
        "worktree": str(worktree.resolve()),
        "base_ref": batch["base_ref"],
        "base_head": batch["base_before"],
        "integration_head": batch["integration_head"],
        "port_block_start": port_block_start,
        "port_block_end": port_block_start + 99,
        "adapter_inputs": adapter_inputs,
    }
    if batch.get("worktree_mode") == "reusable":
        from .batch_workspace import context_binding
        from .candidate_batches import CandidateBatchStore

        context["worktree_binding"] = context_binding(
            repo, CandidateBatchStore(repo), batch
        )
    return config, context


def activate_batch_runtime(
    repo: GitRepo,
    *,
    batch: dict[str, Any],
) -> dict[str, Any]:
    worktree = Path(str(batch["worktree"]))
    config = load_repo_config(repo, cwd=worktree)
    command = config.runtime_adapter.batch_activate
    if command is None:
        return {"configured": False, "operation": "batch-activate"}
    _require_approval(repo, cwd=worktree)
    config, context = _batch_context(repo, batch=batch, worktree=worktree)
    receipt = _invoke(
        repo,
        cwd=worktree,
        operation="batch-activate",
        command=command,
        timeout_seconds=config.runtime_adapter.timeout_seconds,
        context={"reason": "combined-full-validation", **context},
        reusable_success=True,
    )
    return {"configured": True, **receipt}


def release_batch_runtime(
    repo: GitRepo,
    *,
    batch: dict[str, Any],
    validation_outcome: str,
    validation_error: str | None = None,
) -> dict[str, Any]:
    worktree = Path(str(batch["worktree"]))
    config = load_repo_config(repo, cwd=worktree)
    command = config.runtime_adapter.batch_release
    if command is None:
        return {"configured": False, "operation": "batch-release"}
    _require_approval(repo, cwd=worktree)
    config, context = _batch_context(repo, batch=batch, worktree=worktree)
    receipt = _invoke(
        repo,
        cwd=worktree,
        operation="batch-release",
        command=command,
        timeout_seconds=config.runtime_adapter.timeout_seconds,
        context={
            "reason": "combined-full-validation-finished",
            **context,
            "validation_outcome": validation_outcome,
            "validation_error": validation_error,
        },
        reusable_success=True,
    )
    return {"configured": True, **receipt}


def require_exact_passed_batch_release(
    repo: GitRepo, *, receipt: dict[str, Any]
) -> None:
    """恢复已通过 Full 的推进前，复核批次运行时已按原事实释放。"""

    if receipt.get("configured") is False:
        if receipt != {"configured": False, "operation": "batch-release"}:
            raise SoloAIError("Stored batch runtime release identity is invalid")
        return
    if receipt.get("configured") is not True:
        raise SoloAIError("Batch runtime release has no exact persisted outcome")

    stored_receipt = {
        key: value
        for key, value in receipt.items()
        if key not in {"configured", "reused"}
    }
    invocation_id = stored_receipt.get("invocation_id")
    if not isinstance(invocation_id, str) or not invocation_id:
        raise SoloAIError("Stored batch runtime release invocation is invalid")
    persisted = read_json(
        repo.local_dir / "runtime-adapter" / "receipts" / f"{invocation_id}.json",
        {},
    )
    if persisted != stored_receipt:
        raise SoloAIError("Stored batch runtime release receipt changed")
    if (
        persisted.get("schema_version") != ADAPTER_RECEIPT_SCHEMA
        or persisted.get("operation") != "batch-release"
        or persisted.get("result") != "passed"
        or persisted.get("exit_code") != 0
        or persisted.get("timed_out") is not False
        or not _logs_exist(persisted)
    ):
        raise SoloAIError("Stored batch runtime release receipt is not a passed result")
    context_path = Path(str(persisted.get("context") or ""))
    context_root = repo.local_dir / "runtime-adapter" / "contexts"
    try:
        context_path.resolve().relative_to(context_root.resolve())
    except (OSError, ValueError) as exc:
        raise SoloAIError(
            "Stored batch runtime release context escaped DWW storage"
        ) from exc
    context = read_json(context_path, {})
    if sha256_text(stable_json(context)) != persisted.get("context_digest"):
        raise SoloAIError("Stored batch runtime release context changed")


def verify_runtime_effective(repo: GitRepo, *, candidate_id: str) -> dict[str, Any]:
    from .candidate_batches import CandidateBatchStore

    store = CandidateBatchStore(repo)
    candidate = store.candidate(candidate_id)
    if candidate.get("status") != "integrated" or not candidate.get("integrated_batch"):
        raise SoloAIError(
            "Runtime effectiveness can be verified only after the candidate is integrated"
        )
    batch = store.batch(str(candidate["integrated_batch"]))
    base_ref = str(batch["base_ref"])
    current_head = repo.ref_head(f"refs/heads/{base_ref}")
    integrated_head = str(batch.get("integrated_head") or "")
    if (
        not current_head
        or not integrated_head
        or not repo.is_ancestor(integrated_head, current_head)
    ):
        raise SoloAIError(
            "The delivered batch is no longer provably contained in the current base"
        )
    matching = [
        item.path
        for item in repo.worktrees()
        if not item.bare and repo.branch(item.path) == base_ref
    ]
    if len(matching) != 1:
        raise SoloAIError(
            "Runtime verification requires one stable worktree for the delivered base"
        )
    base_worktree = matching[0]
    if not repo.is_clean(base_worktree) or repo.head(base_worktree) != current_head:
        raise SoloAIError(
            "Runtime verification requires the delivered base worktree to remain clean and exact"
        )
    config = load_repo_config(repo, cwd=base_worktree)
    command = config.runtime_adapter.verify_effective
    if command is None:
        raise SoloAIError("No runtime_adapter.verify_effective command is configured")
    _require_approval(repo, cwd=base_worktree)
    adapter_inputs = _adapter_input_hashes(
        repo, cwd=base_worktree, patterns=config.runtime_adapter.input_paths
    )
    receipt = _invoke(
        repo,
        cwd=base_worktree,
        operation="verify-effective",
        command=command,
        timeout_seconds=config.runtime_adapter.timeout_seconds,
        context={
            "candidate_id": candidate_id,
            "candidate_head": candidate.get("head"),
            "batch_id": batch["id"],
            "delivered_head": integrated_head,
            "base_ref": base_ref,
            "current_base_head": current_head,
            "base_worktree": str(base_worktree.resolve()),
            "adapter_inputs": adapter_inputs,
        },
        reusable_success=False,
    )
    if not repo.is_clean(base_worktree) or repo.head(base_worktree) != current_head:
        raise SoloAIError(
            "Runtime Adapter changed the delivered base worktree; files were preserved"
        )
    return {
        "candidate_id": candidate_id,
        "batch_id": batch["id"],
        "delivered": True,
        "runtime_effective": True,
        "current_base_head": current_head,
        "adapter_receipt": receipt,
    }
