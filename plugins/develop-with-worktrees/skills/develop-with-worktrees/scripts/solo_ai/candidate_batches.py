from __future__ import annotations

import copy
import subprocess
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import CommandSpec, load_repo_config, load_verification_config
from .integration import integration_turn
from .proof import approval_plan, validate
from .repo import GitRepo
from .safety import require_safe
from .state import StateStore
from .task_context import delete_anchor, require_anchor
from .util import (
    DirectoryLock,
    SoloAIError,
    atomic_write_json,
    atomic_write_text,
    read_json,
    run_logged,
    sha256_text,
    stable_json,
    utc_timestamp,
)

POOL_SCHEMA = 2
ACTIVE_BATCH_STATES = {"sealed", "composing", "composed", "validated", "promoted"}
LEGACY_EXPLICIT_POLICY = {
    "schema_version": 1,
    "mode": "batched",
    "batch_size": 5,
    "candidate_capacity": 10,
    "seal_policy": "explicit",
    "activation_epoch": "legacy-explicit",
}
AUTOMATIC_REPAIR_LIMIT = 2


class CandidateCompositionConflict(SoloAIError):
    def __init__(self, candidate_id: str, detail: str):
        super().__init__(detail)
        self.candidate_id = candidate_id


class CandidateBatchStore:
    def __init__(self, repo: GitRepo):
        self.repo = repo
        self.path = repo.local_dir / "candidate-batches.json"
        self.lock_path = repo.local_dir / "locks" / "candidate-batches.lock"

    def _empty(self) -> dict[str, Any]:
        return {
            "schema_version": POOL_SCHEMA,
            "candidates": {},
            "batches": {},
            "next_publication_sequence": 1,
            "updated_at": utc_timestamp(),
        }

    def read(self) -> dict[str, Any]:
        value = read_json(self.path, self._empty())
        if value.get("schema_version") == 1:
            sequence = 1
            for candidate in value.get("candidates", {}).values():
                candidate.setdefault("publication_sequence", sequence)
                candidate.setdefault(
                    "integration_policy", copy.deepcopy(LEGACY_EXPLICIT_POLICY)
                )
                if candidate.get("sealed_batch") and candidate.get("status") == "pending":
                    candidate["status"] = "sealed"
                sequence = max(
                    sequence + 1,
                    int(candidate.get("publication_sequence", sequence)) + 1,
                )
            value["next_publication_sequence"] = sequence
            value["schema_version"] = POOL_SCHEMA
        elif value.get("schema_version") != POOL_SCHEMA:
            raise SoloAIError("Unsupported candidate-pool state schema")
        value.setdefault("next_publication_sequence", 1)
        for candidate in value.get("candidates", {}).values():
            candidate.setdefault("repair_attempt", 0)
        return value

    def mutate(self, callback: Callable[[dict[str, Any]], Any]) -> Any:
        with DirectoryLock(self.lock_path, wait=True):
            value = self.read()
            result = callback(value)
            value["updated_at"] = utc_timestamp()
            atomic_write_json(self.path, value)
            return result

    def summary(self) -> dict[str, Any]:
        value = self.read()
        return {
            "candidates": list(value["candidates"].values()),
            "batches": list(value["batches"].values()),
        }

    def active(self) -> bool:
        value = self.read()
        return any(
            item.get("status") not in {"integrated", "withdrawn", "superseded"}
            for item in value["candidates"].values()
        ) or any(
            item.get("status") in ACTIVE_BATCH_STATES
            for item in value["batches"].values()
        )

    def candidate_for_task(self, task_id: str) -> dict[str, Any] | None:
        for candidate in self.read()["candidates"].values():
            if candidate.get("task_id") == task_id:
                return copy.deepcopy(candidate)
        return None

    def candidate(self, candidate_id: str) -> dict[str, Any]:
        candidate = self.read()["candidates"].get(candidate_id)
        if not candidate:
            raise SoloAIError(f"Unknown candidate: {candidate_id}")
        return copy.deepcopy(candidate)

    def _seal_in_value(
        self,
        value: dict[str, Any],
        candidate_ids: list[str],
        *,
        batch_size: int,
        trigger: str,
    ) -> dict[str, Any]:
        if not candidate_ids or len(candidate_ids) > batch_size:
            raise SoloAIError(
                f"Seal requires between 1 and {batch_size} explicit candidates"
            )
        if trigger == "auto_full" and len(candidate_ids) != batch_size:
            raise SoloAIError("Automatic sealing requires one complete batch")
        if len(candidate_ids) != len(set(candidate_ids)):
            raise SoloAIError("Seal candidates must be unique")
        candidates: list[dict[str, Any]] = []
        base_refs: set[str] = set()
        for candidate_id in candidate_ids:
            candidate = value["candidates"].get(candidate_id)
            if not candidate or candidate.get("status") not in {"pending", "retained"}:
                raise SoloAIError(
                    f"Candidate is not available for sealing: {candidate_id}"
                )
            if candidate.get("sealed_batch"):
                raise SoloAIError(
                    f"Candidate is already in an active batch: {candidate_id}"
                )
            if self.repo.ref_head(str(candidate["ref"])) != candidate.get("head"):
                raise SoloAIError(f"Candidate ref changed: {candidate_id}")
            candidates.append(copy.deepcopy(candidate))
            base_refs.add(str(candidate["base_ref"]))
        if len(base_refs) != 1:
            raise SoloAIError("One batch can target only one local base branch")
        base_ref = next(iter(base_refs))
        base_before = self.repo.ref_head(f"refs/heads/{base_ref}")
        if base_before is None:
            raise SoloAIError("Batch base branch no longer exists")
        batch_id = (
            f"batch-{time.strftime('%Y%m%d%H%M%S', time.gmtime())}-"
            f"{uuid.uuid4().hex[:8]}"
        )
        batch = {
            "id": batch_id,
            "status": "sealed",
            "trigger": trigger,
            "base_ref": base_ref,
            "base_before": base_before,
            "candidate_ids": list(candidate_ids),
            "candidates": candidates,
            "integration_policy": copy.deepcopy(
                candidates[0].get("integration_policy") or LEGACY_EXPLICIT_POLICY
            ),
            "applied_candidate_ids": [],
            "integration_head": base_before,
            "proof": None,
            "worktree": None,
            "created_at": utc_timestamp(),
            "updated_at": utc_timestamp(),
        }
        value["batches"][batch_id] = batch
        for candidate_id in candidate_ids:
            value["candidates"][candidate_id].update(
                {"status": "sealed", "sealed_batch": batch_id}
            )
        return copy.deepcopy(batch)

    def publish(
        self,
        candidate: dict[str, Any],
        *,
        capacity: int,
        batch_size: int,
        seal_policy: str,
    ) -> dict[str, Any]:
        created_ref = False

        def update(value: dict[str, Any]) -> dict[str, Any]:
            nonlocal created_ref
            existing = next(
                (
                    item
                    for item in value["candidates"].values()
                    if item.get("task_id") == candidate["task_id"]
                ),
                None,
            )
            if existing:
                immutable = ("candidate_id", "head", "ref", "task_id", "proof")
                if any(existing.get(key) != candidate.get(key) for key in immutable):
                    raise SoloAIError("Task already published a different candidate")
                batch_id = existing.get("sealed_batch")
                return {
                    "candidate": copy.deepcopy(existing),
                    "auto_batch": copy.deepcopy(value["batches"].get(batch_id))
                    if batch_id
                    and value["batches"].get(batch_id, {}).get("trigger")
                    == "auto_full"
                    else None,
                }
            supersedes = candidate.get("supersedes")
            source = value["candidates"].get(str(supersedes)) if supersedes else None
            if supersedes and not source:
                raise SoloAIError(f"Unknown superseded candidate: {supersedes}")
            if source and source.get("sealed_batch"):
                raise SoloAIError(
                    "A candidate in an active sealed batch cannot be superseded"
                )
            active = [
                item
                for item in value["candidates"].values()
                if item.get("status") in {"pending", "sealed"}
                and item.get("candidate_id") != supersedes
            ]
            if len(active) >= capacity:
                raise SoloAIError(
                    f"Candidate pool is full ({capacity}); seal or withdraw candidates first"
                )
            ref = str(candidate["ref"])
            head = str(candidate["head"])
            if self.repo.ref_head(ref) not in {None, head}:
                raise SoloAIError("Candidate ref already points to another commit")
            if self.repo.ref_head(ref) is None:
                created = self.repo.git(
                    ["update-ref", ref, head, "0" * len(head)], check=False
                )
                if created.returncode:
                    raise SoloAIError("Could not create the immutable candidate ref")
                created_ref = True
            sequence = int(value.get("next_publication_sequence", 1))
            value["next_publication_sequence"] = sequence + 1
            repair_attempt = (
                int(source.get("repair_attempt", 0)) + 1 if source else 0
            )
            record = {
                **copy.deepcopy(candidate),
                "status": "pending",
                "publication_sequence": sequence,
                "repair_attempt": repair_attempt,
            }
            value["candidates"][str(candidate["candidate_id"])] = record
            if source and source.get("status") in {"pending", "retained"}:
                source.update(
                    {
                        "status": "superseded",
                        "superseded_by": candidate["candidate_id"],
                        "repair_eligible": False,
                        "updated_at": utc_timestamp(),
                    }
                )
            auto_batch = None
            policy = record.get("integration_policy") or {}
            if seal_policy == "auto_full":
                eligible = sorted(
                    (
                        item
                        for item in value["candidates"].values()
                        if item.get("status") == "pending"
                        and not item.get("sealed_batch")
                        and item.get("base_ref") == record.get("base_ref")
                        and (item.get("integration_policy") or {}).get(
                            "seal_policy"
                        )
                        == "auto_full"
                        and (item.get("integration_policy") or {}).get(
                            "activation_epoch"
                        )
                        == policy.get("activation_epoch")
                    ),
                    key=lambda item: (
                        int(item.get("publication_sequence", 0)),
                        str(item.get("candidate_id")),
                    ),
                )
                if len(eligible) >= batch_size:
                    auto_batch = self._seal_in_value(
                        value,
                        [str(item["candidate_id"]) for item in eligible[:batch_size]],
                        batch_size=batch_size,
                        trigger="auto_full",
                    )
            return {
                "candidate": copy.deepcopy(
                    value["candidates"][str(candidate["candidate_id"])]
                ),
                "auto_batch": auto_batch,
            }

        try:
            return self.mutate(update)
        except Exception:
            ref = str(candidate["ref"])
            head = str(candidate["head"])
            if created_ref and self.repo.ref_head(ref) == head:
                self.repo.delete_ref(ref, expected=head)
            raise

    def seal(self, candidate_ids: list[str], *, batch_size: int) -> dict[str, Any]:
        return self.mutate(
            lambda value: self._seal_in_value(
                value,
                candidate_ids,
                batch_size=batch_size,
                trigger="explicit_tail",
            )
        )

    def batch(self, batch_id: str) -> dict[str, Any]:
        batch = self.read()["batches"].get(batch_id)
        if not batch:
            raise SoloAIError(f"Unknown integration batch: {batch_id}")
        return copy.deepcopy(batch)

    def update_batch(self, batch_id: str, **changes: Any) -> dict[str, Any]:
        def update(value: dict[str, Any]) -> dict[str, Any]:
            batch = value["batches"].get(batch_id)
            if not batch:
                raise SoloAIError(f"Unknown integration batch: {batch_id}")
            batch.update(changes)
            batch["updated_at"] = utc_timestamp()
            return copy.deepcopy(batch)

        return self.mutate(update)

    def fail(
        self,
        batch_id: str,
        error: str,
        *,
        failure_kind: str,
        failed_candidate_id: str | None = None,
    ) -> dict[str, Any]:
        def update(value: dict[str, Any]) -> dict[str, Any]:
            batch = value["batches"][batch_id]
            if batch.get("status") == "promoted":
                return copy.deepcopy(batch)
            batch.update(
                {
                    "status": "failed",
                    "error": error,
                    "failure_kind": failure_kind,
                    "failed_candidate_id": failed_candidate_id,
                    "failed_at": utc_timestamp(),
                }
            )
            for candidate_id in batch["candidate_ids"]:
                candidate = value["candidates"].get(candidate_id)
                if candidate and candidate.get("sealed_batch") == batch_id:
                    candidate.update({"sealed_batch": None, "status": "retained"})
                    candidate.setdefault("failed_batches", []).append(batch_id)
                    candidate["last_failed_batch"] = batch_id
                    candidate["last_failure_kind"] = failure_kind
                    candidate["repair_eligible"] = bool(
                        failure_kind == "composition_conflict"
                        and candidate_id == failed_candidate_id
                    )
            return copy.deepcopy(batch)

        return self.mutate(update)

    def repair_source(self, candidate_id: str) -> dict[str, Any]:
        candidate = self.candidate(candidate_id)
        if candidate.get("status") not in {"pending", "retained"} or candidate.get(
            "sealed_batch"
        ):
            raise SoloAIError(
                "Only an unsealed pending or retained candidate can start a repair task"
            )
        if not candidate.get("repair_eligible"):
            kind = candidate.get("last_failure_kind") or "unknown"
            raise SoloAIError(
                "Automatic repair is limited to a candidate that caused a composition conflict; "
                f"the latest failure kind is {kind}. Review the failure before choosing a new result."
            )
        attempt = int(candidate.get("repair_attempt", 0))
        if attempt >= AUTOMATIC_REPAIR_LIMIT:
            raise SoloAIError(
                f"Automatic repair stopped after {attempt} attempts; manual product or implementation review is required"
            )
        if self.repo.ref_head(str(candidate["ref"])) != candidate.get("head"):
            raise SoloAIError("Candidate ref changed before repair preparation")
        return candidate

    def complete(self, batch_id: str, *, integrated_head: str) -> dict[str, Any]:
        def update(value: dict[str, Any]) -> dict[str, Any]:
            batch = value["batches"][batch_id]
            batch.update(
                {
                    "status": "completed",
                    "integrated_head": integrated_head,
                    "completed_at": utc_timestamp(),
                }
            )
            for candidate_id in batch["candidate_ids"]:
                candidate = value["candidates"][candidate_id]
                candidate.update(
                    {
                        "status": "integrated",
                        "sealed_batch": None,
                        "integrated_batch": batch_id,
                        "integrated_at": utc_timestamp(),
                    }
                )
            return copy.deepcopy(batch)

        return self.mutate(update)

    def begin_withdraw(self, candidate_id: str) -> dict[str, Any]:
        def update(value: dict[str, Any]) -> dict[str, Any]:
            candidate = value["candidates"].get(candidate_id)
            if not candidate:
                raise SoloAIError(f"Unknown candidate: {candidate_id}")
            if candidate.get("status") == "withdrawn":
                return copy.deepcopy(candidate)
            if candidate.get("status") not in {
                "pending",
                "retained",
                "withdrawing",
            }:
                raise SoloAIError(
                    "Only an unsealed pending or retained candidate can be withdrawn"
                )
            if candidate.get("sealed_batch"):
                raise SoloAIError("A candidate in an active batch cannot be withdrawn")
            candidate["status"] = "withdrawing"
            candidate["updated_at"] = utc_timestamp()
            return copy.deepcopy(candidate)

        return self.mutate(update)

    def complete_withdraw(self, candidate_id: str) -> dict[str, Any]:
        def update(value: dict[str, Any]) -> dict[str, Any]:
            candidate = value["candidates"][candidate_id]
            if candidate.get("status") not in {"withdrawing", "withdrawn"}:
                raise SoloAIError("Candidate withdrawal state changed")
            candidate.update({"status": "withdrawn", "withdrawn_at": utc_timestamp()})
            return copy.deepcopy(candidate)

        return self.mutate(update)


def _run_secret_scanner(
    repo: GitRepo, *, cwd: Path, scanner: CommandSpec | None
) -> None:
    if scanner is None:
        return
    log = repo.local_dir / "logs" / "pending" / f"batch-secret-{uuid.uuid4().hex}.log"
    result = run_logged(scanner.argv, cwd=cwd, log_path=log)
    if result.returncode:
        raise SoloAIError(
            f"Repository-declared secret scanner failed. Review its local redacted log: {log}"
        )


def _require_approval(repo: GitRepo, *, cwd: Path) -> None:
    verification = load_verification_config(repo, cwd=cwd)
    fingerprint = sha256_text(
        stable_json(approval_plan(repo, cwd=cwd, verification=verification))
    )
    approvals = read_json(repo.local_dir / "approvals.json", {"accepted": {}})
    if fingerprint not in approvals.get("accepted", {}):
        raise SoloAIError(
            "This machine has not approved the combined validation plan. Review `doctor` then run `approve --accept`."
        )


def _apply_candidate_diff(
    repo: GitRepo,
    *,
    worktree: Path,
    base_head: str,
    candidate_head: str,
    candidate_id: str,
) -> None:
    diff = subprocess.run(
        [
            "git",
            "-C",
            str(repo.root),
            "diff",
            "--binary",
            "--full-index",
            base_head,
            candidate_head,
            "--",
        ],
        capture_output=True,
        check=False,
    )
    if diff.returncode:
        raise SoloAIError("Could not freeze the candidate tree difference")
    if not diff.stdout:
        return
    applied = subprocess.run(
        ["git", "-C", str(worktree), "apply", "--index", "--3way", "-"],
        input=diff.stdout,
        capture_output=True,
        check=False,
    )
    if applied.returncode:
        detail = applied.stderr.decode("utf-8", errors="replace").strip()
        raise CandidateCompositionConflict(
            candidate_id,
            "Candidate conflicts with the sealed batch; base was preserved"
            + (f": {detail}" if detail else "")
        )


def _integration_worktree(repo: GitRepo, batch: dict[str, Any]) -> Path:
    config = load_repo_config(repo, cwd=repo.policy_path())
    return (
        repo.primary_path / config.worktree_directory / f"solo-ai-batch-{batch['id']}"
    ).resolve()


def _compose(
    repo: GitRepo, store: CandidateBatchStore, batch: dict[str, Any]
) -> dict[str, Any]:
    worktree = _integration_worktree(repo, batch)
    if batch.get("worktree") and Path(str(batch["worktree"])).resolve() != worktree:
        raise SoloAIError("Recorded batch worktree identity changed")
    if not worktree.exists():
        repo.git(["worktree", "add", "--detach", str(worktree), batch["base_before"]])
    if not any(item.path == worktree for item in repo.worktrees()):
        raise SoloAIError("Batch integration worktree is not registered")
    batch = store.update_batch(batch["id"], status="composing", worktree=str(worktree))
    applied_ids = list(batch.get("applied_candidate_ids", []))
    if not repo.is_clean(worktree) or repo.head(worktree) != batch["integration_head"]:
        raise SoloAIError(
            "Interrupted batch worktree is not at its recorded clean head"
        )
    for candidate in batch["candidates"]:
        candidate_id = str(candidate["candidate_id"])
        if candidate_id in applied_ids:
            continue
        if repo.ref_head(str(candidate["ref"])) != candidate["head"]:
            raise SoloAIError(f"Sealed candidate ref changed: {candidate_id}")
        if not repo.is_ancestor(str(candidate["base_head"]), str(candidate["head"])):
            raise SoloAIError(f"Candidate base is not an ancestor: {candidate_id}")
        _apply_candidate_diff(
            repo,
            worktree=worktree,
            base_head=str(candidate["base_head"]),
            candidate_head=str(candidate["head"]),
            candidate_id=candidate_id,
        )
        staged = repo.git(["diff", "--cached", "--quiet"], cwd=worktree, check=False)
        if staged.returncode not in {0, 1}:
            raise SoloAIError("Could not inspect the composed candidate")
        if staged.returncode == 1:
            repo.git(
                ["commit", "-m", f"DWW 批次 {batch['id']}：{candidate_id}"],
                cwd=worktree,
            )
        applied_ids.append(candidate_id)
        batch = store.update_batch(
            batch["id"],
            applied_candidate_ids=applied_ids,
            integration_head=repo.head(worktree),
        )
    return store.update_batch(batch["id"], status="composed")


def _validate_batch(
    repo: GitRepo, store: CandidateBatchStore, batch: dict[str, Any]
) -> dict[str, Any]:
    worktree = Path(str(batch["worktree"]))
    if not repo.is_clean(worktree) or repo.head(worktree) != batch["integration_head"]:
        raise SoloAIError("Composed batch changed before final validation")
    config = load_repo_config(repo, cwd=worktree)
    policy = batch.get("integration_policy") or {}
    if policy.get("mode") != "batched":
        raise SoloAIError("The sealed generation has no batched integration policy")
    verification = load_verification_config(repo, cwd=worktree)
    _require_approval(repo, cwd=worktree)
    _run_secret_scanner(repo, cwd=worktree, scanner=config.secret_scanner)
    require_safe(
        repo,
        cwd=worktree,
        base=str(batch["base_ref"]),
        allowlist=config.sensitive_allowlist,
    )
    proof = validate(
        repo,
        cwd=worktree,
        base=str(batch["base_ref"]),
        verification=verification,
        task_id=str(batch["id"]),
        level="full",
        expected_base_head=str(batch["base_before"]),
        expected_candidate_head=str(batch["integration_head"]),
    )
    if repo.ref_head(f"refs/heads/{batch['base_ref']}") != batch["base_before"]:
        raise SoloAIError(
            "Batch base advanced during final validation; seal a fresh batch"
        )
    return store.update_batch(
        batch["id"], status="validated", proof=proof["fingerprint"]
    )


def _promote(
    repo: GitRepo, store: CandidateBatchStore, batch: dict[str, Any]
) -> dict[str, Any]:
    base_ref = str(batch["base_ref"])
    matching = [
        item.path
        for item in repo.worktrees()
        if not item.bare and repo.branch(item.path) == base_ref
    ]
    if len(matching) != 1:
        raise SoloAIError(
            "Batch base branch must be checked out in one stable worktree"
        )
    base_worktree = matching[0]
    if (
        not repo.is_clean(base_worktree)
        or repo.head(base_worktree) != batch["base_before"]
        or repo.ref_head(f"refs/heads/{base_ref}") != batch["base_before"]
    ):
        raise SoloAIError("Batch base changed; no promotion was attempted")
    repo.git(["merge", "--ff-only", str(batch["integration_head"])], cwd=base_worktree)
    observed = repo.head(base_worktree)
    if observed != batch["integration_head"]:
        raise SoloAIError(
            "Batch promotion did not reach the validated integration head"
        )
    return store.update_batch(
        batch["id"], status="promoted", promoted_at=utc_timestamp()
    )


def _cleanup(
    repo: GitRepo, store: CandidateBatchStore, batch: dict[str, Any]
) -> dict[str, Any]:
    if repo.ref_head(f"refs/heads/{batch['base_ref']}") != batch["integration_head"]:
        raise SoloAIError("Promoted batch is no longer the exact base head")
    worktree = Path(str(batch["worktree"]))
    if worktree.exists():
        if (
            not repo.is_clean(worktree)
            or repo.head(worktree) != batch["integration_head"]
        ):
            raise SoloAIError("Batch worktree changed after promotion")
        repo.git(["worktree", "remove", str(worktree)])
    for candidate in batch["candidates"]:
        ref = str(candidate["ref"])
        head = str(candidate["head"])
        if repo.ref_head(ref) == head:
            repo.delete_ref(ref, expected=head)
    completed = store.complete(
        batch["id"], integrated_head=str(batch["integration_head"])
    )
    for candidate in batch["candidates"]:
        delete_anchor(repo, str(candidate["task_id"]))
    return completed


def _resume(
    repo: GitRepo, store: CandidateBatchStore, batch: dict[str, Any]
) -> dict[str, Any]:
    status = str(batch["status"])
    if status == "completed":
        return batch
    if status == "failed":
        raise SoloAIError(
            "This generation failed and will not be rerun automatically; publish a repair candidate and seal a new explicit batch"
        )
    if status in {"sealed", "composing", "composed"}:
        batch = _compose(repo, store, batch)
    if batch["status"] == "composed":
        batch = _validate_batch(repo, store, batch)
    if batch["status"] == "validated":
        batch = _promote(repo, store, batch)
    if batch["status"] == "promoted":
        batch = _cleanup(repo, store, batch)
    return batch


def seal_batch(repo: GitRepo, *, candidate_ids: list[str]) -> dict[str, Any]:
    from .lifecycle import _config_and_mode

    config, _, _ = _config_and_mode(repo)
    if config.integration.mode != "batched":
        raise SoloAIError(
            "This repository uses direct integration, not candidate batches"
        )
    store = CandidateBatchStore(repo)
    batch = store.seal(candidate_ids, batch_size=config.integration.batch_size)
    return run_batch(repo, batch_id=str(batch["id"]))


def run_batch(repo: GitRepo, *, batch_id: str) -> dict[str, Any]:
    """Run one already frozen generation without blocking candidate publication."""

    store = CandidateBatchStore(repo)
    batch = store.batch(batch_id)
    try:
        with integration_turn(repo, str(batch["id"])):
            return _resume(repo, store, batch)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        current = store.batch(str(batch["id"]))
        if isinstance(exc, CandidateCompositionConflict):
            failure_kind = "composition_conflict"
            failed_candidate_id = exc.candidate_id
        elif current.get("status") == "composed":
            failure_kind = "validation_failed"
            failed_candidate_id = None
        elif current.get("status") == "validated":
            failure_kind = "promotion_blocked"
            failed_candidate_id = None
        else:
            failure_kind = "composition_failed"
            failed_candidate_id = None
        store.fail(
            str(batch["id"]),
            str(exc),
            failure_kind=failure_kind,
            failed_candidate_id=failed_candidate_id,
        )
        raise


def prepare_candidate_repair(
    repo: GitRepo, *, candidate_id: str
) -> dict[str, Any]:
    """在最新基线上准备一次受管候选修复，保留可由代理解决的冲突现场。"""

    from .lifecycle import _config_and_mode, start

    config, _, _ = _config_and_mode(repo)
    if config.integration.mode != "batched":
        raise SoloAIError("Candidate repair requires integration.mode = batched")
    store = CandidateBatchStore(repo)
    source = store.repair_source(candidate_id)
    base_ref = str(source["base_ref"])
    base_head = repo.ref_head(f"refs/heads/{base_ref}")
    if base_head is None:
        raise SoloAIError("Candidate repair base branch no longer exists")
    if repo.is_ancestor(str(source["head"]), base_head):
        return {
            "outcome": "already_in_base",
            "candidate_id": candidate_id,
            "candidate_head": source["head"],
            "base_ref": base_ref,
            "base_head": base_head,
        }

    attempt = int(source.get("repair_attempt", 0)) + 1
    request_id = f"candidate-repair:{candidate_id}:{base_head}"
    task = start(
        repo,
        name=f"repair {candidate_id}",
        base=base_ref,
        request_id=request_id,
        supersedes=candidate_id,
    )
    worktree = Path(str(task["worktree"]))
    existing = task.get("repair_preparation")
    if task.get("request_reused") and existing:
        return {**task, **copy.deepcopy(existing), "request_reused": True}

    anchor = require_anchor(repo, task)
    atomic_write_text(
        anchor,
        f"""# Task anchor: repair {candidate_id}

- Task ID: `{task['id']}`
- Original purpose: automatically repair candidate `{candidate_id}` after a deterministic composition conflict
- Implementation target: replay candidate `{source['head']}` onto `{base_ref}` at `{base_head}` and preserve its verified intent
- Reference baseline: source `{source['base_head']}` → `{source['head']}`; repair base `{base_ref}` at `{base_head}`
- Scope boundary: change only the source candidate's intent and the minimum conflict resolution; do not choose between competing product, permission, migration, deletion, or security rules
- Acceptance criteria: resolve every recorded conflict, review the exact path manifest, run Commit/Ready/Finish, then explicitly seal the replacement candidate and prove it is in the base
- Current progress: repair attempt {attempt} prepared at {utc_timestamp()}

This local file is not committed. Keep it current, and reread it after context loss or continuation.
""",
    )

    merge_head = repo.git(
        ["rev-parse", "--verify", "-q", "MERGE_HEAD"],
        cwd=worktree,
        check=False,
    )
    unmerged = repo.git(
        ["diff", "--name-only", "--diff-filter=U"],
        cwd=worktree,
        check=False,
    ).stdout.splitlines()
    if merge_head.returncode == 0:
        if merge_head.stdout.strip() != source["head"]:
            reason = "Repair worktree already contains a different merge identity"
            StateStore(repo).quarantine(str(task["id"]), reason)
            raise SoloAIError(f"{reason}; the repair worktree was preserved")
        outcome = "conflicted" if unmerged else "prepared"
    else:
        if repo.changed_paths(worktree):
            reason = "Repair worktree changed before candidate preparation"
            StateStore(repo).quarantine(str(task["id"]), reason)
            raise SoloAIError(f"{reason}; the repair worktree was preserved")
        merged = repo.git(
            ["merge", "--no-commit", "--no-ff", str(source["ref"])],
            cwd=worktree,
            check=False,
        )
        unmerged = repo.git(
            ["diff", "--name-only", "--diff-filter=U"],
            cwd=worktree,
            check=False,
        ).stdout.splitlines()
        if merged.returncode == 0:
            outcome = "prepared"
        elif unmerged:
            outcome = "conflicted"
        else:
            reason = "Candidate repair merge failed without a reviewable conflict set"
            StateStore(repo).quarantine(str(task["id"]), reason)
            raise SoloAIError(f"{reason}; the repair worktree was preserved")

    preparation = {
        "outcome": outcome,
        "candidate_id": candidate_id,
        "source_head": source["head"],
        "source_ref": source["ref"],
        "base_ref": base_ref,
        "base_head": base_head,
        "repair_attempt": attempt,
        "changed_paths": repo.changed_paths(worktree),
        "conflict_paths": unmerged,
        "manual_notification_required": False,
    }
    updated = StateStore(repo).update_task(
        str(task["id"]), repair_preparation=preparation
    )
    return {**updated, **preparation, "anchor_path": str(anchor.resolve())}


def recover_batch(repo: GitRepo, *, batch_id: str) -> dict[str, Any]:
    from .lifecycle import _config_and_mode

    _config_and_mode(repo)
    return run_batch(repo, batch_id=batch_id)


def withdraw_candidate(repo: GitRepo, *, candidate_id: str) -> dict[str, Any]:
    from .lifecycle import _config_and_mode

    _config_and_mode(repo)
    store = CandidateBatchStore(repo)
    candidate = store.begin_withdraw(candidate_id)
    ref = str(candidate["ref"])
    head = str(candidate["head"])
    if repo.ref_head(ref) == head:
        repo.delete_ref(ref, expected=head)
    elif repo.ref_head(ref) is not None:
        raise SoloAIError("Candidate ref changed and was preserved")
    result = store.complete_withdraw(candidate_id)
    delete_anchor(repo, str(candidate["task_id"]))
    return result
