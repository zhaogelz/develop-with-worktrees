from __future__ import annotations

import copy
import math
import os
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

from . import batch_workspace, worktree_retirement
from .cleanup import (
    inspect_untracked,
    require_managed_directory_identity,
)
from .config import CommandSpec, load_repo_config, load_verification_config
from .integration import integration_turn
from .proof import require_approved_plan, require_exact_passed_proof, validate
from .repo import GitRepo
from .runtime_adapter import (
    activate_batch_runtime,
    release_batch_runtime,
    require_exact_passed_batch_release,
)
from .safety import require_safe
from .state import StateStore, candidate_admission_lock
from .task_context import delete_anchor, require_anchor
from .util import (
    DirectoryLock,
    SoloAIError,
    atomic_write_json,
    atomic_write_text,
    path_identity,
    process_matches,
    process_snapshot,
    read_json,
    run_logged,
    sha256_text,
    stable_json,
    utc_timestamp,
)

POOL_SCHEMA = 4
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
    "promotion_blocked",
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


def _candidate_lane(candidate: dict[str, Any]) -> tuple[str, str, str]:
    """自动封批只使用同一冻结基线和同一策略的候选。"""

    policy = candidate.get("integration_policy") or LEGACY_EXPLICIT_POLICY
    return (
        str(candidate["base_ref"]),
        str(candidate.get("base_head") or ""),
        str(policy.get("activation_epoch") or "legacy-explicit"),
    )


class CandidateCompositionConflict(SoloAIError):
    def __init__(self, candidate_id: str, detail: str):
        super().__init__(detail)
        self.candidate_id = candidate_id


class BatchRuntimePending(SoloAIError):
    """批次运行时结果不确定；保留批次所有权并等待显式恢复。"""


class BatchCleanupPending(SoloAIError):
    """批次清理事实不安全或不确定；保持当前阶段等待精确恢复。"""


class BatchPromotionPending(SoloAIError):
    """Full 已通过，但推进前的外部工作区事实尚未稳定。"""


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
        elif value.get("schema_version") in {2, 3}:
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
                after_failed_batch_id=(
                    str(batch["after_failed_batch"])
                    if batch.get("after_failed_batch")
                    else None
                ),
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
                self._candidate_projection(item, value["batches"])
                for item in value["candidates"].values()
            ],
            "batches": list(value["batches"].values()),
        }

    def _candidate_projection(
        self, candidate: dict[str, Any], batches: dict[str, Any]
    ) -> dict[str, Any]:
        projected = copy.deepcopy(candidate)
        status = str(projected.get("status"))
        batch_id = candidate.get("integrated_batch") or candidate.get("sealed_batch")
        batch = batches.get(str(batch_id), {})
        release = batch.get("runtime_release") or {}
        released = release.get("configured") is False or (
            release.get("result") == "passed" and release.get("exit_code") == 0
        )
        delivered = False
        if (
            batch.get("status") in {"validated", "promoted", "completed"}
            and batch.get("proof")
            and batch.get("validation_outcome") == "passed"
            and released
        ):
            current_base = self.repo.ref_head(f"refs/heads/{batch['base_ref']}")
            delivered = bool(
                current_base
                and self.repo.is_ancestor(str(batch["integration_head"]), current_base)
            )
        projected["delivered"] = delivered
        projected["finalization_pending"] = (
            delivered and batch.get("status") != "completed"
        )
        projected["delivery_status"] = (
            "integrated"
            if delivered
            else "not-delivered"
            if status in {"withdrawn", "superseded"}
            else "awaiting-integration"
        )
        projected["batch_ownership"] = self._batch_ownership(candidate, batches)
        return projected

    @staticmethod
    def _batch_ownership(
        candidate: dict[str, Any], batches: dict[str, Any]
    ) -> dict[str, Any] | None:
        """投影候选被队列或活动批次持有的事实，供任务状态和安全检查共用。"""
        candidate_status = str(candidate.get("status"))
        if candidate_status in {"integrated", "withdrawn", "superseded"}:
            return None
        batch_id = candidate.get("sealed_batch") or candidate.get("integrated_batch")
        batch = batches.get(str(batch_id), {}) if batch_id else {}
        batch_status = str(batch.get("status") or "")
        active_batch = batch_status in ACTIVE_BATCH_STATES
        policy = (
            batch.get("integration_policy")
            or candidate.get("integration_policy")
            or LEGACY_EXPLICIT_POLICY
        )
        batch_size = int(policy.get("batch_size", 5))
        candidate_count = len(batch.get("candidate_ids", []))
        process = copy.deepcopy(batch.get("run_owner")) if active_batch else None
        if process:
            process["live"] = process_matches(process)
        return {
            "state": "active_full_batch"
            if active_batch and candidate_count >= batch_size
            else "active_tail_batch"
            if active_batch
            else "candidate_queued",
            "candidate": {
                "id": candidate.get("candidate_id"),
                "status": candidate_status,
            },
            "batch": {
                "id": batch.get("id"),
                "kind": "full" if candidate_count >= batch_size else "tail",
                "phase": batch_status or None,
                "process": process,
            }
            if batch
            else None,
        }

    def task_batch_ownership(self, task_id: str) -> dict[str, Any] | None:
        """返回任务候选的未完成交付所有权；终态候选不再阻止任务收尾。"""
        value = self.read()
        candidate = next(
            (
                item
                for item in value["candidates"].values()
                if item.get("task_id") == task_id
            ),
            None,
        )
        if not candidate:
            return None
        return self._batch_ownership(candidate, value["batches"])

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
        after_failed_batch_id: str | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "schema_version": 1,
            "base_ref": base_ref,
            "activation_epoch": activation_epoch,
            "candidate_ids": candidate_ids,
        }
        if after_failed_batch_id:
            payload.update(
                {
                    "schema_version": 2,
                    "after_failed_batch": after_failed_batch_id,
                }
            )
        return sha256_text(stable_json(payload))

    def _seal_in_value(
        self,
        value: dict[str, Any],
        candidate_ids: list[str],
        *,
        batch_size: int,
        trigger: str,
        after_failed_batch_id: str | None = None,
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
        if after_failed_batch_id:
            previous = value["batches"].get(after_failed_batch_id)
            if not previous:
                raise SoloAIError(
                    f"Unknown previous failed batch: {after_failed_batch_id}"
                )
            if previous.get("status") != "failed":
                raise SoloAIError(
                    "A reviewed reseal must name a durably failed previous batch"
                )
            if [
                str(item) for item in previous.get("candidate_ids", [])
            ] != candidate_ids:
                raise SoloAIError(
                    "A reviewed reseal must use the previous failed batch's exact ordered candidates"
                )
        base_ref = next(iter(base_refs))
        activation_epoch = next(iter(activation_epochs))
        seal_intent_id = self._seal_intent(
            base_ref=base_ref,
            activation_epoch=activation_epoch,
            candidate_ids=candidate_ids,
            after_failed_batch_id=after_failed_batch_id,
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
            "after_failed_batch": after_failed_batch_id,
            "candidates": candidates,
            "integration_policy": copy.deepcopy(
                candidates[0].get("integration_policy") or LEGACY_EXPLICIT_POLICY
            ),
            "applied_candidate_ids": [],
            "integration_head": base_before,
            "proof": None,
            "runtime_cycle": 0,
            "worktree": None,
            "worktree_mode": candidates[0]
            .get("integration_policy", {})
            .get("worktree_mode", "dedicated"),
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
                        and item.get("base_head") == record.get("base_head")
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
                        and item.get("base_head") == candidate.get("base_head")
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

    def seal(
        self,
        candidate_ids: list[str],
        *,
        batch_size: int,
        after_failed_batch_id: str | None = None,
    ) -> dict[str, Any]:
        return self.mutate(
            lambda value: self._seal_in_value(
                value,
                candidate_ids,
                batch_size=batch_size,
                trigger="explicit_tail",
                after_failed_batch_id=after_failed_batch_id,
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
        lanes: dict[tuple[str, str, str], dict[str, Any]] = {}
        for candidate in candidates:
            policy = candidate.get("integration_policy") or LEGACY_EXPLICIT_POLICY
            key = _candidate_lane(candidate)
            lane = lanes.setdefault(
                key,
                {
                    "base_ref": key[0],
                    "base_head": key[1],
                    "activation_epoch": key[2],
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
        producer_snapshots: dict[tuple[str, str, str], dict[str, Any]],
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
            lane_candidates: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
            for candidate in candidates:
                key = _candidate_lane(candidate)
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
                        "base_head": key[1],
                        "activation_epoch": key[2],
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

    def metrics(self) -> dict[str, Any]:
        """只从现有候选、批次与证明事实派生调优指标。"""

        value = self.read()
        terminal = [
            batch
            for batch in value["batches"].values()
            if batch.get("status") in {"completed", "failed"}
        ]
        full = [
            batch
            for batch in terminal
            if len(batch.get("candidate_ids", []))
            >= int(
                (batch.get("integration_policy") or LEGACY_EXPLICIT_POLICY).get(
                    "batch_size", 5
                )
            )
        ]
        tail = [batch for batch in terminal if batch not in full]
        waits: list[float] = []
        for candidate in value["candidates"].values():
            published = _parse_timestamp(candidate.get("published_at"))
            integrated = _parse_timestamp(candidate.get("integrated_at"))
            if published is not None and integrated is not None:
                waits.append((integrated - published).total_seconds())

        full_costs: list[float] = []
        reused_full_profiles = 0
        missing_full_proofs = 0
        for batch in terminal:
            if batch.get("status") != "completed" or not batch.get("proof"):
                continue
            proof = read_json(
                self.repo.local_dir / "proofs" / f"{batch['proof']}.json", {}
            )
            matched = False
            for item in proof.get("profile_proofs", []):
                profile = read_json(
                    self.repo.local_dir
                    / "profile-proofs"
                    / f"{item.get('fingerprint')}.json",
                    {},
                )
                if (profile.get("inputs") or {}).get("level") != "full":
                    continue
                matched = True
                if item.get("reused"):
                    reused_full_profiles += 1
                    continue
                full_costs.append(
                    sum(
                        float(run.get("duration_seconds", 0))
                        for run in profile.get("runs", [])
                    )
                )
            if not matched:
                missing_full_proofs += 1

        count = len(terminal)
        return {
            "schema_version": 1,
            "terminal_batches": count,
            "completed_batches": sum(
                batch.get("status") == "completed" for batch in terminal
            ),
            "failed_batches": sum(
                batch.get("status") == "failed" for batch in terminal
            ),
            "full_batches": len(full),
            "tail_batches": len(tail),
            "full_batch_rate": round(len(full) / count, 4) if count else None,
            "tail_batch_rate": round(len(tail) / count, 4) if count else None,
            "candidate_wait_seconds": _numeric_summary(waits),
            "executed_full_validation_seconds": _numeric_summary(full_costs),
            "reused_full_profiles": reused_full_profiles,
            "missing_full_proofs": missing_full_proofs,
        }

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


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _numeric_summary(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {
            "count": 0,
            "minimum": None,
            "median": None,
            "p95": None,
            "maximum": None,
            "mean": None,
        }
    ordered = sorted(values)
    p95_index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    return {
        "count": len(ordered),
        "minimum": round(ordered[0], 3),
        "median": round(float(median(ordered)), 3),
        "p95": round(ordered[p95_index], 3),
        "maximum": round(ordered[-1], 3),
        "mean": round(sum(ordered) / len(ordered), 3),
    }


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
    keep_conflicts_out_of_worktree: bool = False,
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
    if keep_conflicts_out_of_worktree:
        # --check --3way 在真实冲突时也可能退出0，不能据此保护共享工作区。
        # 用临时索引执行真实三方应用；冲突只留在索引中，不污染已组合的干净结果。
        with tempfile.TemporaryDirectory(
            prefix="candidate-index-", dir=repo.local_dir
        ) as temporary:
            temporary_root = Path(temporary)
            temporary_worktree = temporary_root / "worktree"
            temporary_worktree.mkdir()
            environment = {
                **os.environ,
                "GIT_INDEX_FILE": str(temporary_root / "index"),
                # git apply --cached --3way can materialize conflict markers in
                # GIT_WORK_TREE for add/add conflicts. The probe must never use
                # the reusable integration worktree as that target.
                "GIT_WORK_TREE": str(temporary_worktree),
            }
            command = ["git", "-C", str(worktree)]
            initialized = subprocess.run(
                [*command, "read-tree", "HEAD"],
                env=environment,
                capture_output=True,
                check=False,
            )
            if initialized.returncode:
                raise SoloAIError("Could not prepare the isolated candidate index")
            checked = subprocess.run(
                [*command, "apply", "--cached", "--3way", "-"],
                input=diff.stdout,
                env=environment,
                capture_output=True,
                check=False,
            )
            conflicts = subprocess.run(
                [*command, "ls-files", "--unmerged", "-z"],
                env=environment,
                capture_output=True,
                check=False,
            )
            if conflicts.returncode:
                raise SoloAIError("Could not inspect the isolated candidate index")
            if checked.returncode or conflicts.stdout:
                detail = checked.stderr.decode("utf-8", errors="replace").strip()
                raise CandidateCompositionConflict(
                    candidate_id,
                    "Candidate conflicts with the sealed batch; base was preserved"
                    + (f": {detail}" if detail else ""),
                )
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
    mode = batch.get("worktree_mode", "dedicated")
    if mode not in {"dedicated", "reusable"}:
        raise SoloAIError("Unsupported batch worktree mode")
    name = (
        "solo-ai-integration" if mode == "reusable" else f"solo-ai-batch-{batch['id']}"
    )
    return (repo.primary_path / config.worktree_directory / name).absolute()


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


def _assert_batch_worktree_unchanged(repo: GitRepo, batch: dict[str, Any]) -> Path:
    """核对批次目录及 Git 身份；内容清点由后续实际操作负责。"""
    if batch.get("worktree_mode") == "reusable":
        batch_workspace.require_owner(repo, CandidateBatchStore(repo), batch)
    worktree, _managed_root = _batch_worktree_identity(repo, batch)
    if (
        not repo.is_clean(worktree)
        or repo.head(worktree) != batch["integration_head"]
        or repo.branch(worktree) is not None
    ):
        raise BatchCleanupPending("Batch worktree changed before cleanup")
    return worktree


def _assert_batch_cleanup_safe(repo: GitRepo, batch: dict[str, Any]) -> Path:
    """只允许批次树携带可再生的已知忽略产物进入终态清理。"""
    worktree = _assert_batch_worktree_unchanged(repo, batch)
    inventory = inspect_untracked(repo, cwd=worktree, expand_dependencies=True)
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
    if batch.get("worktree_mode") == "reusable":
        batch = batch_workspace.acquire(repo, store, batch, worktree)
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
            keep_conflicts_out_of_worktree=batch.get("worktree_mode") == "reusable",
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
        composed_head = repo.head(worktree)
        saved = {}
        if batch.get("worktree_mode") == "reusable":
            saved["integration_ref"] = batch_workspace.remember_head(
                repo, batch, composed_head
            )
        batch = store.update_batch(
            batch["id"],
            applied_candidate_ids=applied_ids,
            integration_head=composed_head,
            **saved,
        )
    return store.update_batch(batch["id"], status="composed")


def _validate_batch(
    repo: GitRepo, store: CandidateBatchStore, batch: dict[str, Any]
) -> dict[str, Any]:
    if batch.get("worktree_mode") == "reusable":
        batch_workspace.require_owner(repo, store, batch)
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
        if batch.get("worktree_mode") == "reusable":
            batch_workspace.require_owner(repo, store, batch)
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
    if batch.get("worktree_mode") == "reusable":
        batch_workspace.require_owner(repo, store, batch)
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
    if batch.get("worktree_mode") == "reusable":
        batch_workspace.require_owner(repo, store, batch)
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


def _require_exact_passed_promotion_recovery(
    repo: GitRepo, store: CandidateBatchStore, batch: dict[str, Any]
) -> None:
    """阻断后的恢复只能推进同一份已验证、已释放的批次事务。"""

    if batch.get("status") != "promotion_blocked":
        raise SoloAIError("Only a promotion-blocked batch can reuse a passed Full")
    integration_head = str(batch.get("integration_head") or "")
    base_before = str(batch.get("base_before") or "")
    proof_fingerprint = str(batch.get("proof") or "")
    if (
        batch.get("validation_outcome") != "passed"
        or not integration_head
        or not base_before
        or not proof_fingerprint
    ):
        raise SoloAIError("Promotion recovery has no exact passed Full facts")
    proof = read_json(repo.local_dir / "proofs" / f"{proof_fingerprint}.json", {})
    require_exact_passed_proof(
        proof,
        fingerprint=proof_fingerprint,
        candidate_head=integration_head,
        base_head=base_before,
    )
    if "full" not in (proof.get("inputs") or {}).get("levels", []):
        raise SoloAIError("Promotion recovery proof is not a Full validation")
    require_exact_passed_batch_release(
        repo, receipt=copy.deepcopy(batch.get("runtime_release") or {})
    )

    frozen = list(batch.get("candidates") or [])
    candidate_ids = [str(item) for item in batch.get("candidate_ids") or []]
    if (
        not frozen
        or len(frozen) != len(candidate_ids)
        or candidate_ids != [str(item.get("candidate_id") or "") for item in frozen]
        or len(set(candidate_ids)) != len(candidate_ids)
    ):
        raise SoloAIError("Promotion recovery candidate snapshot changed")
    pool = store.read().get("candidates") or {}
    for candidate_id, sealed in zip(candidate_ids, frozen, strict=True):
        current = pool.get(candidate_id)
        if not current or current.get("status") != "sealed":
            raise SoloAIError("Promotion recovery candidate ownership changed")
        if current.get("sealed_batch") != batch["id"]:
            raise SoloAIError("Promotion recovery candidate batch ownership changed")
        for field in (
            "candidate_id",
            "task_id",
            "ref",
            "head",
            "base_ref",
            "base_head",
            "integration_policy",
        ):
            if current.get(field) != sealed.get(field):
                raise SoloAIError(
                    f"Promotion recovery candidate identity changed: {candidate_id}"
                )
        if (
            sealed.get("base_ref") != batch.get("base_ref")
            or sealed.get("base_head") != base_before
            or repo.ref_head(str(sealed.get("ref") or "")) != sealed.get("head")
            or not repo.is_ancestor(base_before, str(sealed.get("head") or ""))
        ):
            raise SoloAIError(
                f"Promotion recovery candidate Git facts changed: {candidate_id}"
            )
    integration_ref = batch.get("integration_ref")
    if integration_ref and repo.ref_head(str(integration_ref)) != integration_head:
        raise SoloAIError("Promotion recovery integration ref changed")
    if not repo.is_ancestor(base_before, integration_head):
        raise SoloAIError("Promotion recovery integration head changed")


def _block_promotion(
    store: CandidateBatchStore, batch: dict[str, Any], error: Exception
) -> None:
    store.update_batch(
        batch["id"],
        status="promotion_blocked",
        promotion_blocked_at=utc_timestamp(),
        promotion_blocked_error=str(error),
    )
    raise BatchPromotionPending(
        "Combined Full passed; promotion is pending exact recovery: "
        f"{error}. "
        "Restore the recorded worktree facts, then run batch recover."
    ) from error


def _promote(
    repo: GitRepo, store: CandidateBatchStore, batch: dict[str, Any]
) -> dict[str, Any]:
    if batch.get("worktree_mode") == "reusable":
        worktree = _assert_batch_worktree_unchanged(repo, batch)
        batch_workspace.require_retained_contents(repo, worktree)
    else:
        _assert_batch_cleanup_safe(repo, batch)
    base_ref = str(batch["base_ref"])
    base_ref_head = repo.ref_head(f"refs/heads/{base_ref}")
    integration_head = str(batch["integration_head"])
    if not base_ref_head:
        raise SoloAIError("Batch base branch no longer exists; Full reuse is unsafe")
    if base_ref_head != batch["base_before"] and not repo.is_ancestor(
        integration_head, base_ref_head
    ):
        raise SoloAIError("Batch base advanced; no promotion was attempted")
    matching = [
        item.path
        for item in repo.worktrees()
        if not item.bare and repo.branch(item.path) == base_ref
    ]
    if len(matching) != 1:
        raise BatchPromotionPending(
            "Batch base branch is not checked out in one stable worktree"
        )
    base_worktree = matching[0]
    observed = repo.head(base_worktree)
    # Git已推进而记录中断，或此后主线再次前进：不重复Full或要求回退主线。
    if (
        repo.is_clean(base_worktree)
        and base_ref_head == observed
        and repo.is_ancestor(integration_head, observed)
    ):
        return store.update_batch(
            batch["id"], status="promoted", promoted_at=utc_timestamp()
        )
    if base_ref_head != batch["base_before"]:
        raise SoloAIError("Batch base advanced; no promotion was attempted")
    if not repo.is_clean(base_worktree) or observed != batch["base_before"]:
        raise BatchPromotionPending(
            "Batch base worktree is not clean and exact; no promotion was attempted"
        )
    try:
        repo.git(["merge", "--ff-only", integration_head], cwd=base_worktree)
    except SoloAIError as exc:
        raise BatchPromotionPending(
            "Batch promotion could not complete with otherwise exact facts"
        ) from exc
    observed = repo.head(base_worktree)
    if observed != integration_head:
        raise SoloAIError(
            "Batch promotion did not reach the validated integration head"
        )
    return store.update_batch(
        batch["id"], status="promoted", promoted_at=utc_timestamp()
    )


def _cleanup(
    repo: GitRepo, store: CandidateBatchStore, batch: dict[str, Any]
) -> dict[str, Any]:
    current_base = repo.ref_head(f"refs/heads/{batch['base_ref']}")
    if not current_base or not repo.is_ancestor(
        str(batch["integration_head"]), current_base
    ):
        raise SoloAIError("Promoted batch is no longer contained in its base")
    if batch.get("worktree_mode") == "reusable":
        batch = batch_workspace.return_workspace(repo, store, batch)
        return _complete_batch(repo, store, batch)
    worktree = Path(str(batch["worktree"]))
    registered = any(item.path == worktree.resolve() for item in repo.worktrees())
    if worktree.exists() or batch.get("worktree_removal_manifest_sha256"):
        try:
            batch = worktree_retirement.retire(repo, store, batch)
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
    return _complete_batch(repo, store, batch)


def _complete_batch(
    repo: GitRepo, store: CandidateBatchStore, batch: dict[str, Any]
) -> dict[str, Any]:
    for candidate in batch["candidates"]:
        ref = str(candidate["ref"])
        head = str(candidate["head"])
        if batch.get("worktree_mode") != "reusable" and repo.ref_head(ref) == head:
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
    if batch["status"] == "promotion_blocked":
        _require_exact_passed_promotion_recovery(repo, store, batch)
    if batch["status"] in {"validated", "promotion_blocked"}:
        try:
            batch = _promote(repo, store, batch)
        except (BatchCleanupPending, BatchPromotionPending) as exc:
            _block_promotion(store, batch, exc)
    if batch["status"] == "promoted":
        batch = _cleanup(repo, store, batch)
    return batch


def seal_batch(
    repo: GitRepo,
    *,
    candidate_ids: list[str],
    after_failed_batch_id: str | None = None,
) -> dict[str, Any]:
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
            after_failed_batch_id=after_failed_batch_id,
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
        snapshots: dict[tuple[str, str, str], dict[str, Any]] = {}
        for lane in batch_store.pending_lanes():
            key = (
                str(lane["base_ref"]),
                str(lane["base_head"]),
                str(lane["activation_epoch"]),
            )
            snapshots[key] = state_store.candidate_producer_snapshot(
                base_ref=key[0], base_head=key[1], activation_epoch=key[2]
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
    run_lock = repo.local_dir / "locks" / f"batch-{sha256_text(batch_id)[:24]}.lock"
    with DirectoryLock(run_lock, wait=True):
        with integration_turn(repo, batch_id):
            batch = store.batch(batch_id)
            owner = process_snapshot()
            if batch.get("status") in ACTIVE_BATCH_STATES:
                store.update_batch(batch_id, run_owner=owner)
            try:
                return _run_owned_batch(repo, store, batch_id)
            finally:
                current = store.batch(batch_id)
                if current.get("run_owner") == owner:
                    store.update_batch(batch_id, run_owner=None)


def _run_owned_batch(
    repo: GitRepo, store: CandidateBatchStore, batch_id: str
) -> dict[str, Any]:
    # 失败记录及归还仍在原集成锁内，不能先释放锁再碰共享位置。
    try:
        batch = store.batch(batch_id)
        return _resume(repo, store, batch)
    except (
        KeyboardInterrupt,
        SystemExit,
        BatchRuntimePending,
        BatchCleanupPending,
        BatchPromotionPending,
        batch_workspace.BatchWorkspacePending,
    ):
        raise
    except Exception as exc:
        current = store.batch(batch_id)
        if isinstance(exc, CandidateCompositionConflict):
            failure_kind = "composition_conflict"
            failed_candidate_id = exc.candidate_id
        elif current.get("status") == "composed":
            failure_kind = "validation_failed"
            failed_candidate_id = None
        elif current.get("status") in {"validated", "promotion_blocked"}:
            failure_kind = "promotion_blocked"
            failed_candidate_id = None
        else:
            failure_kind = "composition_failed"
            failed_candidate_id = None
        failed = store.fail(
            batch_id,
            str(exc),
            failure_kind=failure_kind,
            failed_candidate_id=failed_candidate_id,
        )
        if (
            failed.get("status") == "failed"
            and failed.get("worktree_mode") == "reusable"
            and failed.get("worktree_generation")
        ):
            try:
                batch_workspace.return_workspace(repo, store, failed)
            except SoloAIError as return_error:
                store.update_batch(batch_id, worktree_return_error=str(return_error))
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
- Original purpose: {task["anchor_origin"]["original_purpose"]}
- Implementation target: replay candidate `{source["head"]}` onto `{base_ref}` at `{base_head}` and preserve its verified intent
- Reference baseline: {task["anchor_origin"]["reference_baseline"]}
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
        ["diff", "--name-only", "--diff-filter=U", "-z"],
        cwd=worktree,
        check=False,
    ).stdout.split("\0")
    unmerged = [path for path in unmerged if path]
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
            ["diff", "--name-only", "--diff-filter=U", "-z"],
            cwd=worktree,
            check=False,
        ).stdout.split("\0")
        unmerged = [path for path in unmerged if path]
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


def retire_failed_batch(
    repo: GitRepo, *, batch_id: str, fast: bool = False
) -> dict[str, Any]:
    """幂等退休一个已失败批次的隔离工作树，保留候选与审计事实。"""

    from .lifecycle import _config_and_mode

    _config_and_mode(repo)
    store = CandidateBatchStore(repo)
    run_lock = repo.local_dir / "locks" / f"batch-{sha256_text(batch_id)[:24]}.lock"
    with DirectoryLock(run_lock, wait=True):
        with integration_turn(repo, batch_id):
            batch = store.batch(batch_id)
            if batch.get("status") != "failed":
                raise SoloAIError("Only a failed integration batch can be retired")
            if fast:
                candidates = [
                    store.candidate(str(item))
                    for item in batch.get("candidate_ids", [])
                ]
                if not candidates or any(
                    candidate.get("status") != "superseded" for candidate in candidates
                ):
                    raise SoloAIError(
                        "Fast retirement requires every batch candidate to be superseded"
                    )
                return worktree_retirement.retire_fast(repo, store, batch)
            if batch.get("worktree_mode") == "reusable":
                # 退役旧批次只终结其持有权；不得因路径相同删掉下个持有者的目录。
                return batch_workspace.return_workspace(repo, store, batch)
            expected = _integration_worktree(repo, batch)
            registered = any(item.path == expected for item in repo.worktrees())
            exists = expected.exists()
            if batch.get("worktree_retired_at"):
                if exists or registered:
                    raise SoloAIError(
                        "Retired batch worktree unexpectedly reappeared; no deletion was attempted"
                    )
                return batch

            started_at = batch.get("worktree_retirement_started_at")
            if batch.get("worktree_removal_manifest_sha256"):
                worktree_retirement.retire(repo, store, batch)
                return store.update_batch(batch_id, worktree_retired_at=utc_timestamp())
            if not exists:
                if registered:
                    raise SoloAIError(
                        "Missing failed batch worktree remains registered"
                    )
                if batch.get("worktree") and not started_at:
                    raise SoloAIError(
                        "Failed batch worktree disappeared before a retirement intent was recorded"
                    )
                return store.update_batch(
                    batch_id,
                    worktree_retirement_started_at=started_at or utc_timestamp(),
                    worktree_retired_at=utc_timestamp(),
                )

            # 删除器本身会完整清点和拒绝受保护内容；这里不重复预扫依赖。
            _assert_batch_worktree_unchanged(repo, batch)
            if not started_at:
                store.update_batch(
                    batch_id, worktree_retirement_started_at=utc_timestamp()
                )
            worktree_retirement.retire(repo, store, batch)
            return store.update_batch(batch_id, worktree_retired_at=utc_timestamp())


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
