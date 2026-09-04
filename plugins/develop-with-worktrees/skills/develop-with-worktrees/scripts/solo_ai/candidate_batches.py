from __future__ import annotations

import copy
import subprocess
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .cleanup import (
    inspect_untracked,
    remove_recreatable_ignored,
    require_managed_directory_identity,
)
from .config import CommandSpec, load_repo_config, load_verification_config
from .integration import integration_turn
from .proof import require_approved_plan, validate
from .repo import GitRepo
from .runtime_adapter import activate_batch_runtime, release_batch_runtime
from .safety import require_safe
from .state import StateStore, candidate_admission_lock
from .task_context import delete_anchor, require_anchor
from .util import (
    DirectoryLock,
    SoloAIError,
    atomic_write_json,
    atomic_write_text,
    path_identity,
    read_json,
    run_logged,
    sha256_text,
    stable_json,
    utc_timestamp,
)

POOL_SCHEMA = 3
ACTIVE_BATCH_STATES = {
    "sealed",
    "composing",
    "composed",
    "runtime_activating",
    "runtime_activation_pending",
    "runtime_active",
    "runtime_releasing",
    "runtime_release_pending",
    "validated",
    "promoted",
}
LEGACY_EXPLICIT_POLICY = {
    "schema_version": 1,
    "mode": "batched",
    "batch_size": 5,
    "candidate_capacity": 10,
    "seal_policy": "explicit",
    "tail_policy": "explicit",
    "tail_quiet_seconds": 90,
    "activation_epoch": "legacy-explicit",
}
AUTOMATIC_REPAIR_LIMIT = 2


class CandidateCompositionConflict(SoloAIError):
    def __init__(self, candidate_id: str, detail: str):
        super().__init__(detail)
        self.candidate_id = candidate_id


class BatchRuntimePending(SoloAIError):
    """批次运行时结果不确定；保留批次所有权并等待显式恢复。"""


class BatchCleanupPending(SoloAIError):
    """批次清理事实不安全或不确定；保持当前阶段等待精确恢复。"""


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
                if (
                    candidate.get("sealed_batch")
                    and candidate.get("status") == "pending"
                ):
                    candidate["status"] = "sealed"
                sequence = max(
                    sequence + 1,
                    int(candidate.get("publication_sequence", sequence)) + 1,
                )
            value["next_publication_sequence"] = sequence
            value["schema_version"] = POOL_SCHEMA
        elif value.get("schema_version") == 2:
            value["schema_version"] = POOL_SCHEMA
        elif value.get("schema_version") != POOL_SCHEMA:
            raise SoloAIError("Unsupported candidate-pool state schema")
        value.setdefault("next_publication_sequence", 1)
        for candidate in value.get("candidates", {}).values():
            candidate.setdefault("repair_attempt", 0)
            policy = candidate.setdefault(
                "integration_policy", copy.deepcopy(LEGACY_EXPLICIT_POLICY)
            )
            policy.setdefault("tail_policy", "explicit")
            policy.setdefault("tail_quiet_seconds", 90)
        for batch in value.get("batches", {}).values():
            batch.setdefault("runtime_cycle", 0)
            if batch.get("seal_intent_id"):
                continue
            policy = batch.get("integration_policy") or LEGACY_EXPLICIT_POLICY
            batch["seal_intent_id"] = self._seal_intent(
                base_ref=str(batch.get("base_ref")),
                activation_epoch=str(
                    policy.get("activation_epoch") or "legacy-explicit"
                ),
                candidate_ids=[str(item) for item in batch.get("candidate_ids", [])],
            )
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
            "candidates": [
                self._candidate_projection(item)
                for item in value["candidates"].values()
            ],
            "batches": list(value["batches"].values()),
        }

    @staticmethod
    def _candidate_projection(candidate: dict[str, Any]) -> dict[str, Any]:
        projected = copy.deepcopy(candidate)
        status = str(projected.get("status"))
        projected["delivered"] = status == "integrated"
        projected["delivery_status"] = (
            "integrated"
            if status == "integrated"
            else "not-delivered"
            if status in {"withdrawn", "superseded"}
            else "awaiting-integration"
        )
        return projected

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

    @staticmethod
    def _seal_intent(
        *,
        base_ref: str,
        activation_epoch: str,
        candidate_ids: list[str],
    ) -> str:
        return sha256_text(
            stable_json(
                {
                    "schema_version": 1,
                    "base_ref": base_ref,
                    "activation_epoch": activation_epoch,
                    "candidate_ids": candidate_ids,
                }
            )
        )

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
        activation_epochs: set[str] = set()
        for candidate_id in candidate_ids:
            candidate = value["candidates"].get(candidate_id)
            if not candidate:
                raise SoloAIError(f"Unknown candidate: {candidate_id}")
            policy = candidate.get("integration_policy") or LEGACY_EXPLICIT_POLICY
            candidates.append(copy.deepcopy(candidate))
            base_refs.add(str(candidate["base_ref"]))
            activation_epochs.add(
                str(policy.get("activation_epoch") or "legacy-explicit")
            )
        if len(base_refs) != 1:
            raise SoloAIError("One batch can target only one local base branch")
        if len(activation_epochs) != 1:
            raise SoloAIError("One batch can contain only one integration policy lane")
        base_ref = next(iter(base_refs))
        activation_epoch = next(iter(activation_epochs))
        seal_intent_id = self._seal_intent(
            base_ref=base_ref,
            activation_epoch=activation_epoch,
            candidate_ids=candidate_ids,
        )
        for existing in value["batches"].values():
            if existing.get("seal_intent_id") == seal_intent_id:
                return copy.deepcopy(existing)
        if any(
            existing.get("status") in ACTIVE_BATCH_STATES
            and existing.get("base_ref") == base_ref
            for existing in value["batches"].values()
        ):
            raise SoloAIError(
                "An active integration batch already owns this base; reconcile or recover it before sealing another batch"
            )
        for candidate in candidates:
            candidate_id = str(candidate["candidate_id"])
            if candidate.get("status") not in {"pending", "retained"}:
                raise SoloAIError(
                    f"Candidate is not available for sealing: {candidate_id}"
                )
            if candidate.get("sealed_batch"):
                raise SoloAIError(
                    f"Candidate is already in an active batch: {candidate_id}"
                )
            if self.repo.ref_head(str(candidate["ref"])) != candidate.get("head"):
                raise SoloAIError(f"Candidate ref changed: {candidate_id}")
        base_before = self.repo.ref_head(f"refs/heads/{base_ref}")
        if base_before is None:
            raise SoloAIError("Batch base branch no longer exists")
        batch_id = f"batch-{seal_intent_id[:24]}"
        batch = {
            "id": batch_id,
            "seal_intent_id": seal_intent_id,
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
            "runtime_cycle": 0,
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
        activate: bool = True,
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
                    and value["batches"].get(batch_id, {}).get("trigger") == "auto_full"
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
                if item.get("status") in {"held", "pending", "sealed"}
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
            repair_attempt = int(source.get("repair_attempt", 0)) + 1 if source else 0
            record = {
                **copy.deepcopy(candidate),
                "status": "pending" if activate else "held",
                "publication_sequence": sequence,
                "repair_attempt": repair_attempt,
                "published_at": utc_timestamp(),
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
            active_lane_batch = any(
                batch.get("status") in ACTIVE_BATCH_STATES
                and batch.get("base_ref") == record.get("base_ref")
                and (batch.get("integration_policy") or {}).get("activation_epoch")
                == policy.get("activation_epoch")
                for batch in value["batches"].values()
            )
            if activate and seal_policy == "auto_full" and not active_lane_batch:
                eligible = sorted(
                    (
                        item
                        for item in value["candidates"].values()
                        if item.get("status") == "pending"
                        and not item.get("sealed_batch")
                        and item.get("base_ref") == record.get("base_ref")
                        and (item.get("integration_policy") or {}).get("seal_policy")
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

    def activate(
        self,
        candidate_id: str,
        *,
        batch_size: int,
        seal_policy: str,
    ) -> dict[str, Any]:
        def update(value: dict[str, Any]) -> dict[str, Any]:
            candidate = value["candidates"].get(candidate_id)
            if not candidate:
                raise SoloAIError(f"Unknown candidate: {candidate_id}")
            batch_id = candidate.get("sealed_batch")
            if candidate.get("status") != "held":
                return {
                    "candidate": copy.deepcopy(candidate),
                    "auto_batch": copy.deepcopy(value["batches"].get(batch_id))
                    if batch_id
                    and value["batches"].get(batch_id, {}).get("trigger") == "auto_full"
                    else None,
                }
            if self.repo.ref_head(str(candidate["ref"])) != candidate.get("head"):
                raise SoloAIError("Held candidate ref changed before activation")
            candidate["status"] = "pending"
            candidate["activated_at"] = utc_timestamp()
            policy = candidate.get("integration_policy") or LEGACY_EXPLICIT_POLICY
            active_lane_batch = any(
                batch.get("status") in ACTIVE_BATCH_STATES
                and batch.get("base_ref") == candidate.get("base_ref")
                and (batch.get("integration_policy") or {}).get("activation_epoch")
                == policy.get("activation_epoch")
                for batch in value["batches"].values()
            )
            auto_batch = None
            if seal_policy == "auto_full" and not active_lane_batch:
                eligible = sorted(
                    (
                        item
                        for item in value["candidates"].values()
                        if item.get("status") == "pending"
                        and not item.get("sealed_batch")
                        and item.get("base_ref") == candidate.get("base_ref")
                        and (item.get("integration_policy") or {}).get("seal_policy")
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
                "candidate": copy.deepcopy(candidate),
                "auto_batch": auto_batch,
            }

        return self.mutate(update)

    def seal(self, candidate_ids: list[str], *, batch_size: int) -> dict[str, Any]:
        return self.mutate(
            lambda value: self._seal_in_value(
                value,
                candidate_ids,
                batch_size=batch_size,
                trigger="explicit_tail",
            )
        )

    def pending_lanes(self) -> list[dict[str, Any]]:
        candidates = sorted(
            (
                item
                for item in self.read()["candidates"].values()
                if item.get("status") == "pending" and not item.get("sealed_batch")
            ),
            key=lambda item: (
                int(item.get("publication_sequence", 0)),
                str(item.get("candidate_id")),
            ),
        )
        lanes: dict[tuple[str, str], dict[str, Any]] = {}
        for candidate in candidates:
            policy = candidate.get("integration_policy") or LEGACY_EXPLICIT_POLICY
            key = (
                str(candidate["base_ref"]),
                str(policy.get("activation_epoch") or "legacy-explicit"),
            )
            lane = lanes.setdefault(
                key,
                {
                    "base_ref": key[0],
                    "activation_epoch": key[1],
                    "integration_policy": copy.deepcopy(policy),
                    "candidate_ids": [],
                    "first_publication_sequence": int(
                        candidate.get("publication_sequence", 0)
                    ),
                },
            )
            lane["candidate_ids"].append(str(candidate["candidate_id"]))
        return sorted(
            lanes.values(), key=lambda lane: int(lane["first_publication_sequence"])
        )

    def reconcile(
        self,
        *,
        producer_snapshots: dict[tuple[str, str], dict[str, Any]],
        force: bool = False,
        cause: str = "heartbeat",
        now_epoch: float | None = None,
    ) -> dict[str, Any]:
        observed_now = time.time() if now_epoch is None else now_epoch

        def timestamp_epoch(value: str | None) -> float | None:
            if not value:
                return None
            try:
                return (
                    datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
                    .replace(tzinfo=timezone.utc)
                    .timestamp()
                )
            except ValueError:
                return None

        def update(value: dict[str, Any]) -> dict[str, Any]:
            waiting: list[dict[str, Any]] = []
            active_batches = sorted(
                (
                    batch
                    for batch in value["batches"].values()
                    if batch.get("status") in ACTIVE_BATCH_STATES
                ),
                key=lambda batch: (
                    str(batch.get("created_at") or ""),
                    str(batch.get("id")),
                ),
            )
            if active_batches:
                return {
                    "status": "active-batch",
                    "batch": copy.deepcopy(active_batches[0]),
                    "cause": cause,
                    "idempotent": True,
                }
            candidates = sorted(
                (
                    item
                    for item in value["candidates"].values()
                    if item.get("status") == "pending" and not item.get("sealed_batch")
                ),
                key=lambda item: (
                    int(item.get("publication_sequence", 0)),
                    str(item.get("candidate_id")),
                ),
            )
            lane_candidates: dict[tuple[str, str], list[dict[str, Any]]] = {}
            for candidate in candidates:
                policy = candidate.get("integration_policy") or LEGACY_EXPLICIT_POLICY
                key = (
                    str(candidate["base_ref"]),
                    str(policy.get("activation_epoch") or "legacy-explicit"),
                )
                lane_candidates.setdefault(key, []).append(candidate)
            for key, eligible in lane_candidates.items():
                policy = eligible[0].get("integration_policy") or LEGACY_EXPLICIT_POLICY
                batch_size = int(policy.get("batch_size", 5))
                candidate_ids = [str(item["candidate_id"]) for item in eligible]
                if (
                    policy.get("seal_policy") == "auto_full"
                    and len(candidate_ids) >= batch_size
                ):
                    batch = self._seal_in_value(
                        value,
                        candidate_ids[:batch_size],
                        batch_size=batch_size,
                        trigger="auto_full",
                    )
                    return {"status": "sealed", "batch": batch, "cause": cause}
                snapshot = producer_snapshots.get(
                    key,
                    {
                        "active_count": 0,
                        "active_task_ids": [],
                        "quiet_since": None,
                    },
                )
                active_count = int(snapshot.get("active_count", 0))
                quiet_seconds = float(policy.get("tail_quiet_seconds", 90))
                quiet_since = timestamp_epoch(snapshot.get("quiet_since"))
                quiet_elapsed = (
                    observed_now - quiet_since if quiet_since is not None else None
                )
                quiet_eligible = (
                    policy.get("tail_policy") == "quiet_or_explicit"
                    and active_count == 0
                    and quiet_elapsed is not None
                    and quiet_elapsed >= quiet_seconds
                )
                if force or quiet_eligible:
                    batch = self._seal_in_value(
                        value,
                        candidate_ids,
                        batch_size=batch_size,
                        trigger="explicit_tail" if force else "quiet_tail",
                    )
                    return {"status": "sealed", "batch": batch, "cause": cause}
                next_reconcile_at = None
                if (
                    policy.get("tail_policy") == "quiet_or_explicit"
                    and active_count == 0
                    and quiet_since is not None
                ):
                    next_reconcile_at = time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ",
                        time.gmtime(quiet_since + quiet_seconds),
                    )
                waiting.append(
                    {
                        "base_ref": key[0],
                        "activation_epoch": key[1],
                        "candidate_ids": candidate_ids,
                        "active_candidate_producers": active_count,
                        "active_task_ids": list(snapshot.get("active_task_ids", [])),
                        "quiet_since": snapshot.get("quiet_since"),
                        "next_reconcile_at": next_reconcile_at,
                    }
                )
            if not candidates:
                held = sorted(
                    str(item["candidate_id"])
                    for item in value["candidates"].values()
                    if item.get("status") == "held"
                )
                if held:
                    return {
                        "status": "publication-held",
                        "cause": cause,
                        "held_candidate_ids": held,
                        "waiting": [],
                    }
                return {"status": "idle", "cause": cause, "waiting": []}
            return {"status": "waiting", "cause": cause, "waiting": waiting}

        return self.mutate(update)

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
    require_approved_plan(
        repo,
        cwd=cwd,
        verification=verification,
        message="This machine has not approved the combined validation plan.",
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
            + (f": {detail}" if detail else ""),
        )


def _integration_worktree(repo: GitRepo, batch: dict[str, Any]) -> Path:
    config = load_repo_config(repo, cwd=repo.policy_path())
    return (
        repo.primary_path / config.worktree_directory / f"solo-ai-batch-{batch['id']}"
    ).resolve()


def _batch_worktree_identity(repo: GitRepo, batch: dict[str, Any]) -> tuple[Path, Path]:
    """复核批次目录仍是 DWW 登记的原目录对象。"""

    expected = _integration_worktree(repo, batch)
    recorded = Path(str(batch.get("worktree") or "")).absolute()
    if recorded != expected.absolute():
        raise BatchCleanupPending("Recorded batch worktree identity changed")
    managed_root = expected.parent
    resolved = require_managed_directory_identity(
        expected,
        managed_root=managed_root,
        expected_resolved=batch.get("worktree_resolved"),
        expected_root_resolved=batch.get("managed_root_resolved"),
        expected_identity=batch.get("worktree_identity"),
        expected_root_identity=batch.get("managed_root_identity"),
    )
    if not any(item.path == resolved for item in repo.worktrees()):
        raise BatchCleanupPending("Batch integration worktree is not registered")
    return expected, managed_root


def _assert_batch_cleanup_safe(repo: GitRepo, batch: dict[str, Any]) -> Path:
    """只允许批次树携带可再生的已知忽略产物进入终态清理。"""

    worktree, _managed_root = _batch_worktree_identity(repo, batch)
    if (
        not repo.is_clean(worktree)
        or repo.head(worktree) != batch["integration_head"]
        or repo.branch(worktree) is not None
    ):
        raise BatchCleanupPending("Batch worktree changed before cleanup")
    inventory = inspect_untracked(repo, cwd=worktree)
    blocked = sorted(
        {
            *inventory["keep"],
            *inventory["protected"],
            *inventory["ordinary"],
            *inventory["unknown_ignored"],
        }
    )
    if blocked:
        raise BatchCleanupPending(
            "Protected or unknown content blocks batch worktree cleanup:\n"
            + "\n".join(f"- {item}" for item in blocked[:20])
        )
    return worktree


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
    managed_root = worktree.parent
    resolved = require_managed_directory_identity(
        worktree,
        managed_root=managed_root,
        expected_resolved=batch.get("worktree_resolved"),
        expected_root_resolved=batch.get("managed_root_resolved"),
        expected_identity=batch.get("worktree_identity"),
        expected_root_identity=batch.get("managed_root_identity"),
    )
    batch = store.update_batch(
        batch["id"],
        status="composing",
        worktree=str(worktree),
        worktree_resolved=str(resolved),
        managed_root_resolved=str(managed_root.resolve()),
        worktree_identity=path_identity(worktree),
        managed_root_identity=path_identity(managed_root),
    )
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
    proof_fingerprint: str | None = None
    try:
        if (
            not any(item.path == worktree for item in repo.worktrees())
            or not repo.is_clean(worktree)
            or repo.head(worktree) != batch["integration_head"]
        ):
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
        proof_fingerprint = str(proof["fingerprint"])
        if repo.ref_head(f"refs/heads/{batch['base_ref']}") != batch["base_before"]:
            raise SoloAIError(
                "Batch base advanced during final validation; seal a fresh batch"
            )
    except (KeyboardInterrupt, SystemExit):
        releasing = store.update_batch(
            batch["id"],
            status="runtime_releasing",
            validation_outcome="interrupted",
            validation_error="Combined Full validation was interrupted",
            proof=proof_fingerprint,
        )
        _release_batch_runtime(repo, store, releasing)
        raise
    except Exception as exc:
        releasing = store.update_batch(
            batch["id"],
            status="runtime_releasing",
            validation_outcome="failed",
            validation_error=str(exc),
            proof=proof_fingerprint,
        )
        _release_batch_runtime(repo, store, releasing)
        raise
    releasing = store.update_batch(
        batch["id"],
        status="runtime_releasing",
        validation_outcome="passed",
        validation_error=None,
        proof=proof_fingerprint,
    )
    return _release_batch_runtime(repo, store, releasing)


def _activate_batch_runtime(
    repo: GitRepo, store: CandidateBatchStore, batch: dict[str, Any]
) -> dict[str, Any]:
    worktree = Path(str(batch["worktree"]))
    if (
        not any(item.path == worktree for item in repo.worktrees())
        or not repo.is_clean(worktree)
        or repo.head(worktree) != batch["integration_head"]
    ):
        raise SoloAIError("Composed batch changed before runtime activation")
    if batch["status"] == "composed":
        runtime_cycle = int(batch.get("runtime_cycle", 0)) + 1
        activating = store.update_batch(
            batch["id"],
            status="runtime_activating",
            runtime_cycle=runtime_cycle,
            runtime_activation=None,
            runtime_activation_error=None,
            runtime_release=None,
            runtime_release_error=None,
            validation_outcome=None,
            validation_error=None,
            proof=None,
        )
    else:
        runtime_cycle = int(batch.get("runtime_cycle", 0))
        if runtime_cycle < 1:
            raise SoloAIError("Pending batch runtime activation has no valid cycle")
        activating = store.update_batch(batch["id"], status="runtime_activating")
    try:
        receipt = activate_batch_runtime(repo, batch=activating)
    except (KeyboardInterrupt, SystemExit):
        store.update_batch(
            batch["id"],
            status="runtime_activation_pending",
            runtime_activation_error="Runtime activation was interrupted",
        )
        raise
    except Exception as exc:
        store.update_batch(
            batch["id"],
            status="runtime_activation_pending",
            runtime_activation_error=str(exc),
        )
        raise BatchRuntimePending(
            "Batch runtime activation is pending; fix the Adapter and run batch recover"
        ) from exc
    return store.update_batch(
        batch["id"],
        status="runtime_active",
        runtime_activation=receipt,
        runtime_activation_error=None,
    )


def _release_batch_runtime(
    repo: GitRepo, store: CandidateBatchStore, batch: dict[str, Any]
) -> dict[str, Any]:
    try:
        receipt = release_batch_runtime(
            repo,
            batch=batch,
            validation_outcome=str(batch["validation_outcome"]),
            validation_error=batch.get("validation_error"),
        )
    except (KeyboardInterrupt, SystemExit):
        store.update_batch(
            batch["id"],
            status="runtime_release_pending",
            runtime_release_error="Runtime release was interrupted",
        )
        raise
    except Exception as exc:
        store.update_batch(
            batch["id"],
            status="runtime_release_pending",
            runtime_release_error=str(exc),
        )
        raise BatchRuntimePending(
            "Batch runtime release is pending; main was preserved and batch recover must retry release"
        ) from exc
    outcome = str(batch["validation_outcome"])
    worktree = Path(str(batch["worktree"]))
    exact = (
        any(item.path == worktree for item in repo.worktrees())
        and repo.is_clean(worktree)
        and repo.head(worktree) == batch["integration_head"]
    )
    if outcome == "passed" and exact:
        return store.update_batch(
            batch["id"],
            status="validated",
            runtime_release=receipt,
            runtime_release_error=None,
        )
    resumed = store.update_batch(
        batch["id"],
        status="composed",
        runtime_release=receipt,
        runtime_release_error=None,
    )
    if outcome == "failed":
        raise SoloAIError(
            str(batch.get("validation_error") or "Combined validation failed")
        )
    if outcome == "passed":
        raise SoloAIError("Batch runtime or validation changed the composed worktree")
    return resumed


def _promote(
    repo: GitRepo, store: CandidateBatchStore, batch: dict[str, Any]
) -> dict[str, Any]:
    _assert_batch_cleanup_safe(repo, batch)
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
    registered = any(item.path == worktree.resolve() for item in repo.worktrees())
    if worktree.exists():
        worktree = _assert_batch_cleanup_safe(repo, batch)
        try:
            remove_recreatable_ignored(repo, cwd=worktree)
            repo.git(["worktree", "remove", str(worktree)])
        except Exception as exc:
            raise BatchCleanupPending(
                "Batch worktree removal is pending exact recovery"
            ) from exc
    elif registered:
        raise BatchCleanupPending("Missing batch worktree remains registered")
    if worktree.exists() or any(
        item.path == worktree.resolve() for item in repo.worktrees()
    ):
        raise BatchCleanupPending(
            "Batch worktree removal did not reach a terminal state"
        )
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
    if status in {"sealed", "composing"}:
        batch = _compose(repo, store, batch)
    if batch["status"] in {
        "composed",
        "runtime_activating",
        "runtime_activation_pending",
    }:
        batch = _activate_batch_runtime(repo, store, batch)
    if batch["status"] == "runtime_active":
        batch = _validate_batch(repo, store, batch)
    if batch["status"] in {"runtime_releasing", "runtime_release_pending"}:
        batch = _release_batch_runtime(repo, store, batch)
        if batch["status"] == "composed":
            batch = _activate_batch_runtime(repo, store, batch)
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
    first = store.candidate(candidate_ids[0]) if candidate_ids else None
    frozen_policy = (first or {}).get("integration_policy") or LEGACY_EXPLICIT_POLICY
    with candidate_admission_lock(repo):
        batch = store.seal(
            candidate_ids,
            batch_size=int(
                frozen_policy.get("batch_size", config.integration.batch_size)
            ),
        )
    return run_batch(repo, batch_id=str(batch["id"]))


def reconcile_batches(
    repo: GitRepo,
    *,
    force: bool = False,
    cause: str = "heartbeat",
    now_epoch: float | None = None,
) -> dict[str, Any]:
    """仅用持久化候选与任务事实冻结一个可证明的批次。"""

    from .lifecycle import _config_and_mode

    config, _, _ = _config_and_mode(repo)
    if config.integration.mode != "batched":
        raise SoloAIError(
            "This repository uses direct integration, not candidate batches"
        )
    if force and cause not in {"user", "deploy", "dependency"}:
        raise SoloAIError(
            "Forced tail reconciliation requires cause user, deploy, or dependency"
        )
    batch_store = CandidateBatchStore(repo)
    state_store = StateStore(repo)
    with candidate_admission_lock(repo):
        snapshots: dict[tuple[str, str], dict[str, Any]] = {}
        for lane in batch_store.pending_lanes():
            key = (str(lane["base_ref"]), str(lane["activation_epoch"]))
            snapshots[key] = state_store.candidate_producer_snapshot(
                base_ref=key[0], activation_epoch=key[1]
            )
        result = batch_store.reconcile(
            producer_snapshots=snapshots,
            force=force,
            cause=cause,
            now_epoch=now_epoch,
        )
    batch = result.get("batch")
    if not batch:
        return result
    if result.get("status") == "active-batch" and cause in {"finish", "abandon"}:
        return result
    completed = run_batch(repo, batch_id=str(batch["id"]))
    return {
        **result,
        "status": completed["status"],
        "batch": completed,
        "delivered": completed["status"] == "completed",
    }


def run_batch(repo: GitRepo, *, batch_id: str) -> dict[str, Any]:
    """Run one already frozen generation without blocking candidate publication."""

    store = CandidateBatchStore(repo)
    try:
        run_lock = repo.local_dir / "locks" / f"batch-{sha256_text(batch_id)[:24]}.lock"
        with DirectoryLock(run_lock, wait=True):
            with integration_turn(repo, batch_id):
                batch = store.batch(batch_id)
                return _resume(repo, store, batch)
    except (KeyboardInterrupt, SystemExit, BatchRuntimePending, BatchCleanupPending):
        raise
    except Exception as exc:
        current = store.batch(batch_id)
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
            batch_id,
            str(exc),
            failure_kind=failure_kind,
            failed_candidate_id=failed_candidate_id,
        )
        raise


def prepare_candidate_repair(repo: GitRepo, *, candidate_id: str) -> dict[str, Any]:
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

- Task ID: `{task["id"]}`
- Original purpose: automatically repair candidate `{candidate_id}` after a deterministic composition conflict
- Implementation target: replay candidate `{source["head"]}` onto `{base_ref}` at `{base_head}` and preserve its verified intent
- Reference baseline: source `{source["base_head"]}` → `{source["head"]}`; repair base `{base_ref}` at `{base_head}`
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
