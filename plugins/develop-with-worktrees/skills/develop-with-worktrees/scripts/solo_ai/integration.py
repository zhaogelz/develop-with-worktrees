from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .cleanup import inspect_untracked, require_managed_directory_identity
from .proof import require_exact_passed_proof
from .repo import GitRepo
from .state import StateStore
from .util import (
    DirectoryLock,
    SoloAIError,
    atomic_write_json,
    process_matches,
    process_snapshot,
    read_json,
    path_identity,
    sha256_text,
    stable_json,
    utc_timestamp,
)

TRANSACTION_SCHEMA = 1
RECEIPT_SCHEMA = 2
KNOWN_IGNORED_ROOTS = {
    ".venv",
    "node_modules",
    ".tmp",
    ".cache",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
}


def receipt_path(repo: GitRepo, task_id: str) -> Path:
    return repo.local_dir / "integration-receipts" / f"{task_id}.json"


def queue_ticket(repo: GitRepo, task_id: str) -> Path:
    queue = repo.local_dir / "queue"
    queue.mkdir(parents=True, exist_ok=True)
    ticket = queue / f"{time.time_ns():020d}-{uuid.uuid4().hex}-{task_id}.json"
    atomic_write_json(
        ticket,
        {"task_id": task_id, "owner": process_snapshot(), "created_at": utc_timestamp()},
    )
    return ticket


def wait_turn(ticket: Path) -> None:
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


@contextmanager
def integration_turn(repo: GitRepo, task_id: str) -> Iterator[None]:
    ticket = queue_ticket(repo, task_id)
    try:
        wait_turn(ticket)
        with DirectoryLock(repo.local_dir / "locks" / "integration.lock", wait=True):
            if min(ticket.parent.glob("*.json")) != ticket:
                raise SoloAIError("Integration queue order changed unexpectedly")
            yield
    finally:
        ticket.unlink(missing_ok=True)


def new_transaction(
    task: dict[str, Any], *, proof: dict[str, Any], operation_id: str
) -> dict[str, Any]:
    candidate = str(task["candidate_head"])
    worktree = Path(str(task["worktree"])).absolute()
    managed_root = worktree.parent
    return {
        "schema_version": TRANSACTION_SCHEMA,
        "transaction_id": uuid.uuid4().hex,
        "phase": "prepared",
        "prepared_by_operation_id": operation_id,
        "task_id": task["id"],
        "slot_id": task["slot_id"],
        "worktree": task["worktree"],
        "worktree_resolved": str(worktree.resolve()),
        "managed_root": str(managed_root),
        "managed_root_resolved": str(managed_root.resolve()),
        "managed_root_identity": path_identity(managed_root),
        "worktree_identity": path_identity(worktree),
        "branch": task["branch"],
        "base_ref": task["base_ref"],
        "base_worktree": task["base_worktree"],
        "base_before": task["base_head"],
        "candidate_head": candidate,
        "proof": proof["fingerprint"],
        "proof_kind": proof["kind"],
        "proof_reused": bool(proof.get("reused", False)),
        "prepared_at": utc_timestamp(),
    }


def _assert_identity(
    repo: GitRepo, task: dict[str, Any], transaction: dict[str, Any]
) -> None:
    expected = {
        "task_id": task["id"],
        "slot_id": task["slot_id"],
        "worktree": task["worktree"],
        "branch": task["branch"],
        "base_ref": task["base_ref"],
        "base_worktree": task["base_worktree"],
        "candidate_head": task["candidate_head"],
    }
    if transaction.get("schema_version") != TRANSACTION_SCHEMA:
        raise SoloAIError("Unsupported integration transaction schema")
    phase = transaction.get("phase")
    if phase not in {"prepared", "promoted", "completed"}:
        raise SoloAIError("Unsupported integration transaction phase")
    required = (
        "transaction_id",
        "prepared_by_operation_id",
        "base_before",
        "proof",
        "proof_kind",
        "prepared_at",
        "worktree_resolved",
        "managed_root",
        "managed_root_resolved",
        "managed_root_identity",
        "worktree_identity",
    )
    if any(not transaction.get(key) for key in required):
        raise SoloAIError("Integration transaction is missing required identity")
    for key, value in expected.items():
        if transaction.get(key) != value:
            raise SoloAIError(f"Integration transaction identity changed: {key}")
    if transaction.get("base_before") != task.get("base_head"):
        raise SoloAIError("Integration transaction base snapshot changed")
    if transaction.get("proof") != task.get("ready_proof"):
        raise SoloAIError("Integration transaction proof changed")
    expected_status = "finished" if phase == "completed" else "finishing"
    if task.get("status") != expected_status:
        raise SoloAIError("Integration transaction and task status disagree")
    proof = read_json(
        repo.local_dir / "proofs" / f"{transaction['proof']}.json", {}
    )
    require_exact_passed_proof(
        proof,
        fingerprint=str(transaction["proof"]),
        candidate_head=str(transaction["candidate_head"]),
        base_head=str(transaction["base_before"]),
    )
    if proof.get("kind") != transaction.get("proof_kind"):
        raise SoloAIError("Integration transaction proof kind changed")


def _base_head(repo: GitRepo, transaction: dict[str, Any]) -> str:
    head = repo.ref_head(f"refs/heads/{transaction['base_ref']}")
    if head is None:
        raise SoloAIError("Recorded integration base branch no longer exists")
    return head


def _classify(
    repo: GitRepo, transaction: dict[str, Any]
) -> tuple[str, str]:
    candidate = str(transaction["candidate_head"])
    base_before = str(transaction["base_before"])
    base_head = _base_head(repo, transaction)
    if repo.is_ancestor(candidate, base_head):
        return "promoted", base_head
    if transaction.get("phase") == "promoted":
        raise SoloAIError("Promoted candidate is no longer contained in its base branch")
    if base_head == base_before:
        return "prepared", base_head
    if repo.is_ancestor(base_before, base_head):
        return "stale", base_head
    raise SoloAIError("Integration base was rewritten; preserve the task for inspection")


def _unknown_or_protected_ignored(repo: GitRepo, worktree: Path) -> list[str]:
    inventory = inspect_untracked(repo, cwd=worktree)
    return sorted(
        set(
            [
                *inventory["keep"],
                *inventory["protected"],
                *inventory["unknown_ignored"],
            ]
        )
    )


def _assert_releasable_worktree(
    repo: GitRepo, *, worktree: Path, transaction: dict[str, Any]
) -> None:
    candidate = str(transaction["candidate_head"])
    resolved_worktree = require_managed_directory_identity(
        worktree,
        managed_root=Path(str(transaction["managed_root"])),
        expected_resolved=str(transaction["worktree_resolved"]),
        expected_root_resolved=str(transaction["managed_root_resolved"]),
        expected_identity=dict(transaction["worktree_identity"]),
        expected_root_identity=dict(transaction["managed_root_identity"]),
    )
    if not worktree.is_dir() or not any(
        item.path == resolved_worktree for item in repo.worktrees()
    ):
        raise SoloAIError("Integration worktree is missing or no longer registered")
    if (
        not repo.is_clean(worktree)
        or repo.head(worktree) != candidate
        or repo.branch(worktree) is not None
    ):
        raise SoloAIError("Integration worktree changed before slot release")
    if blocked := _unknown_or_protected_ignored(repo, worktree):
        raise SoloAIError(
            "Worktree content changed before integration release:\n"
            + "\n".join(f"- {item}" for item in blocked[:20])
        )


def _assert_prepared_worktree_identity(
    repo: GitRepo, transaction: dict[str, Any]
) -> None:
    worktree = Path(str(transaction["worktree"]))
    resolved_worktree = require_managed_directory_identity(
        worktree,
        managed_root=Path(str(transaction["managed_root"])),
        expected_resolved=str(transaction["worktree_resolved"]),
        expected_root_resolved=str(transaction["managed_root_resolved"]),
        expected_identity=dict(transaction["worktree_identity"]),
        expected_root_identity=dict(transaction["managed_root_identity"]),
    )
    if not worktree.is_dir() or not any(
        item.path == resolved_worktree for item in repo.worktrees()
    ):
        raise SoloAIError("Prepared worktree is missing or unregistered")
    if (
        not repo.is_clean(worktree)
        or repo.head(worktree) != transaction["candidate_head"]
        or repo.branch(worktree) != transaction["branch"]
        or repo.ref_head(f"refs/heads/{transaction['branch']}")
        != transaction["candidate_head"]
    ):
        raise SoloAIError("Prepared candidate identity changed; preserve the transaction")
    if blocked := _unknown_or_protected_ignored(repo, worktree):
        raise SoloAIError(
            "Prepared worktree content changed; preserve the transaction:\n"
            + "\n".join(f"- {item}" for item in blocked[:20])
        )


def _cleanup(
    repo: GitRepo, *, store: StateStore, task: dict[str, Any], transaction: dict[str, Any]
) -> dict[str, Any]:
    _assert_identity(repo, task, transaction)
    candidate = str(transaction["candidate_head"])
    base_head = _base_head(repo, transaction)
    if not repo.is_ancestor(candidate, base_head):
        raise SoloAIError("Candidate is not integrated; cleanup is unsafe")
    worktree = Path(str(transaction["worktree"]))
    resolved_worktree = require_managed_directory_identity(
        worktree,
        managed_root=Path(str(transaction["managed_root"])),
        expected_resolved=str(transaction["worktree_resolved"]),
        expected_root_resolved=str(transaction["managed_root_resolved"]),
        expected_identity=dict(transaction["worktree_identity"]),
        expected_root_identity=dict(transaction["managed_root_identity"]),
    )
    if not worktree.is_dir() or not any(item.path == resolved_worktree for item in repo.worktrees()):
        raise SoloAIError("Integration worktree is missing or no longer registered")
    if not repo.is_clean(worktree):
        raise SoloAIError("Integration worktree changed; preserve it for inspection")
    if blocked := _unknown_or_protected_ignored(repo, worktree):
        raise SoloAIError(
            "Unknown or protected ignored files block integration cleanup:\n"
            + "\n".join(f"- {item}" for item in blocked[:20])
        )
    current_head = repo.head(worktree)
    current_branch = repo.branch(worktree)
    if current_head != candidate:
        raise SoloAIError("Integration worktree HEAD changed; preserve it")
    if current_branch not in {None, transaction["branch"]}:
        raise SoloAIError("Integration worktree is on another branch; preserve it")
    branch_ref = f"refs/heads/{transaction['branch']}"
    branch_head = repo.ref_head(branch_ref)
    if branch_head not in {None, candidate}:
        raise SoloAIError("Task branch advanced after integration; preserve it")
    if current_branch is not None:
        repo.git(["switch", "--detach", candidate], cwd=worktree)
    if not repo.is_clean(worktree) or repo.head(worktree) != candidate or repo.branch(worktree) is not None:
        raise SoloAIError("Integration worktree changed during cleanup; preserve it")
    if blocked := _unknown_or_protected_ignored(repo, worktree):
        raise SoloAIError(
            "Ignored content changed during integration cleanup:\n"
            + "\n".join(f"- {item}" for item in blocked[:20])
        )
    if branch_head is not None:
        repo.delete_ref(branch_ref, expected=candidate)
        if repo.ref_head(branch_ref) is not None:
            raise SoloAIError("Task branch still exists after atomic deletion")
    _assert_releasable_worktree(repo, worktree=worktree, transaction=transaction)
    store.complete_integration(
        task["id"], transaction_id=str(transaction["transaction_id"])
    )
    try:
        _assert_releasable_worktree(repo, worktree=worktree, transaction=transaction)
        completed = store.publish_integration_release(
            task["id"], transaction_id=str(transaction["transaction_id"])
        )
        _assert_releasable_worktree(repo, worktree=worktree, transaction=transaction)
    except Exception as exc:
        store.quarantine_released_slot(
            task["id"], f"Worktree changed at integration release: {exc}"
        )
        raise
    return completed


def _completed_receipt(task: dict[str, Any]) -> dict[str, Any]:
    transaction = dict(task["integration"])
    return {
        **transaction,
        "schema_version": RECEIPT_SCHEMA,
        "status": "completed",
        "integrated_head": transaction["candidate_head"],
        "base_head_observed_at_completion": transaction.get("base_head_observed"),
        "updated_at": utc_timestamp(),
    }


def write_completed_receipt(repo: GitRepo, task: dict[str, Any]) -> dict[str, Any]:
    transaction = task.get("integration") or {}
    _assert_identity(repo, task, transaction)
    cleanup = transaction.get("cleanup") or {}
    if any(
        cleanup.get(key) is not True
        for key in ("worktree_detached", "branch_deleted", "slot_released")
    ):
        raise SoloAIError("Completed integration cleanup facts are incomplete")
    observed_base = str(transaction.get("base_head_observed") or "")
    if not observed_base or not repo.is_ancestor(
        str(transaction["candidate_head"]), observed_base
    ):
        raise SoloAIError("Completed integration base fact does not contain its candidate")
    expected = _completed_receipt(task)
    path = receipt_path(repo, task["id"])
    existing = read_json(path, {})
    if existing and existing.get("schema_version") == 1:
        if existing.get("stage") not in {
            "integrated",
            "detached",
            "branch-deleted",
            "released",
        }:
            raise SoloAIError("Unsupported legacy integration receipt stage")
        legacy_expected = {
            "task_id": expected["task_id"],
            "branch": expected["branch"],
            "base_ref": expected["base_ref"],
            "candidate_head": expected["candidate_head"],
            "integrated_head": expected["candidate_head"],
            "proof": expected["proof"],
        }
        for key, value in legacy_expected.items():
            if existing.get(key) != value:
                raise SoloAIError(f"Legacy integration receipt conflicts with state: {key}")
    elif existing:
        immutable = (
            "transaction_id",
            "task_id",
            "slot_id",
            "worktree",
            "branch",
            "base_ref",
            "base_before",
            "candidate_head",
            "integrated_head",
        )
        for key in immutable:
            if existing.get(key) != expected.get(key):
                raise SoloAIError(f"Integration receipt conflicts with state: {key}")
    atomic_write_json(path, expected)
    return expected


def legacy_transaction(
    task: dict[str, Any], *, proof: dict[str, Any], receipt: dict[str, Any] | None = None
) -> dict[str, Any]:
    """为旧任务构造稳定事务身份，使重复恢复不会生成第二个事务。"""
    transaction = new_transaction(task, proof=proof, operation_id="legacy-recovery")
    identity = {
        "task_id": task["id"],
        "branch": task["branch"],
        "base_ref": task["base_ref"],
        "base_before": task["base_head"],
        "candidate_head": task["candidate_head"],
        "proof": proof["fingerprint"],
    }
    transaction["transaction_id"] = "legacy-" + sha256_text(stable_json(identity))[:32]
    transaction["phase"] = "promoted"
    transaction["promoted_at"] = (receipt or {}).get("updated_at") or utc_timestamp()
    transaction["base_head_observed"] = (receipt or {}).get("integrated_head") or task[
        "candidate_head"
    ]
    return transaction


def migrate_legacy_receipt(
    repo: GitRepo, *, store: StateStore, task: dict[str, Any]
) -> dict[str, Any] | None:
    """严格导入 schema 1 回执；任何不一致都在 Git 副作用前停止。"""
    existing = read_json(receipt_path(repo, task["id"]), {})
    if not existing or existing.get("schema_version") != 1:
        return None
    if existing.get("stage") not in {
        "integrated",
        "detached",
        "branch-deleted",
        "released",
    }:
        raise SoloAIError("Unsupported legacy integration receipt stage")
    candidate = str(task.get("candidate_head") or existing.get("candidate_head") or "")
    proof_id = str(task.get("ready_proof") or existing.get("proof") or "")
    exact = {
        "task_id": task["id"],
        "branch": task["branch"],
        "base_ref": task["base_ref"],
        "candidate_head": candidate,
        "integrated_head": candidate,
        "proof": proof_id,
    }
    for key, value in exact.items():
        if not value or existing.get(key) != value:
            raise SoloAIError(f"Legacy integration receipt identity changed: {key}")
    proof = read_json(repo.local_dir / "proofs" / f"{proof_id}.json", {})
    require_exact_passed_proof(
        proof,
        fingerprint=proof_id,
        candidate_head=candidate,
        base_head=str(task["base_head"]),
    )
    base_head = _base_head(repo, {"base_ref": task["base_ref"]})
    if not repo.is_ancestor(candidate, base_head):
        raise SoloAIError("Legacy receipt candidate is not contained in its base branch")
    transaction = legacy_transaction(task, proof=proof, receipt=existing)
    completed = task.get("status") == "finished"
    if completed:
        transaction.update(
            {
                "phase": "completed",
                "completed_at": existing.get("updated_at") or utc_timestamp(),
                "cleanup": {
                    "worktree_detached": True,
                    "branch_deleted": True,
                    "slot_released": True,
                },
            }
        )
    return store.migrate_legacy_integration(
        task["id"], integration=transaction, completed=completed
    )


def prepare(
    repo: GitRepo, *, store: StateStore, task: dict[str, Any], proof: dict[str, Any]
) -> dict[str, Any]:
    active = task.get("active_operation") or {}
    operation_id = str(active.get("id") or "")
    if not operation_id:
        raise SoloAIError("Finish operation identity is missing")
    transaction = new_transaction(task, proof=proof, operation_id=operation_id)
    return store.prepare_integration(
        task["id"], operation_id=operation_id, integration=transaction
    )


def resume_prepared(
    repo: GitRepo, *, store: StateStore, task: dict[str, Any], allow_stale: bool
) -> dict[str, Any]:
    transaction = task.get("integration")
    if not transaction:
        raise SoloAIError("Task has no prepared integration transaction")
    _assert_identity(repo, task, transaction)
    if transaction.get("phase") == "prepared":
        _assert_prepared_worktree_identity(repo, transaction)
    classification, base_head = _classify(repo, transaction)
    if classification == "stale":
        if not allow_stale:
            raise SoloAIError("Integration base advanced; run Recover and Ready again")
        return store.cancel_prepared_integration(
            task["id"], transaction_id=str(transaction["transaction_id"])
        )
    if classification == "prepared":
        base_worktree = Path(str(transaction["base_worktree"]))
        if repo.branch(base_worktree) != transaction["base_ref"] or not repo.is_clean(
            base_worktree
        ):
            raise SoloAIError("Recorded base worktree is not ready for exact integration")
        repo.git(["merge", "--ff-only", str(transaction["candidate_head"])], cwd=base_worktree)
        base_head = _base_head(repo, transaction)
        if not repo.is_ancestor(str(transaction["candidate_head"]), base_head):
            raise SoloAIError("Exact candidate was not integrated")
    if transaction.get("phase") != "completed":
        task = store.mark_integration_promoted(
            task["id"],
            transaction_id=str(transaction["transaction_id"]),
            observed_base_head=base_head,
        )
    completed = _cleanup(
        repo, store=store, task=task, transaction=dict(task["integration"])
    )
    receipt = write_completed_receipt(repo, completed)
    return {
        "task_id": completed["id"],
        "transaction_id": receipt["transaction_id"],
        "integrated_head": receipt["integrated_head"],
        "proof": receipt["proof"],
        "proof_kind": receipt["proof_kind"],
        "proof_reused": bool(receipt.get("proof_reused", False)),
        "status": "completed",
    }
