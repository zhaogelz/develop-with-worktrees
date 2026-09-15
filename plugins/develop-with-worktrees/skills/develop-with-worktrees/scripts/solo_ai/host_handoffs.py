from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

from .host_context import normalize_host_reference
from .repo import GitRepo
from .util import (
    DirectoryLock,
    SoloAIError,
    atomic_write_json,
    new_id,
    read_json,
    redact_text,
    sha256_text,
    stable_json,
    utc_timestamp,
)

HANDOFF_SCHEMA = 2
REQUEST_SCHEMA = 1
_MAX_REASON_LENGTH = 512
_MAX_REQUEST_HISTORY = 20
_MAX_DELIVERY_ATTEMPTS = 3
_DELIVERY_OUTCOMES = {"sent", "uncertain", "failed"}


def _same_host(left: dict[str, str] | None, right: dict[str, str] | None) -> bool:
    return left == right


def _reason(value: str) -> str:
    if (
        not value
        or len(value) > _MAX_REASON_LENGTH
        or any(
            character.isspace() and character not in {" ", "\t"} for character in value
        )
    ):
        raise SoloAIError(
            "Handoff reason must be a nonblank single-line value up to 512 characters"
        )
    return value


def _failure_detail(value: object) -> str:
    """把批次错误压缩成可安全发送的单行摘要。"""

    detail = " ".join(redact_text(str(value)).split())
    return detail[:_MAX_REASON_LENGTH]


def _delivery_detail(value: str | None) -> str | None:
    if value is None:
        return None
    return _reason(value)


def _record_attempt_outcome(
    request: dict[str, Any],
    *,
    attempts_key: str,
    status_key: str,
    active_attempt_key: str,
    attempt_id: str | None,
    sender: dict[str, str],
    recipient: dict[str, str],
    coordinator_revision: int,
    outcome: str,
    detail: str | None,
    candidate_id: str | None = None,
) -> bool | None:
    """将回执绑定到一次准备；None 保留旧记录的兼容写法。"""

    attempts = request.setdefault(attempts_key, [])
    has_identified_attempt = any(isinstance(item.get("id"), str) for item in attempts)
    if attempt_id is None:
        if has_identified_attempt:
            raise SoloAIError(
                "Delivery attempt id is required; use the id returned by dispatch"
            )
        return None
    attempt = next((item for item in attempts if item.get("id") == attempt_id), None)
    if not isinstance(attempt, dict):
        raise SoloAIError("Unknown delivery attempt")
    if (
        not _same_host(normalize_host_reference(attempt.get("sender")), sender)
        or not _same_host(normalize_host_reference(attempt.get("recipient")), recipient)
        or int(attempt.get("coordinator_revision", -1)) != coordinator_revision
        or (
            candidate_id is not None
            and str(attempt.get("candidate_id") or "") != candidate_id
        )
    ):
        raise SoloAIError(
            "Delivery attempt does not match this sender, recipient, or revision"
        )
    existing = attempt.get("outcome")
    if existing in _DELIVERY_OUTCOMES:
        if existing == outcome and attempt.get("detail") == detail:
            return False
        raise SoloAIError("Delivery attempt already has a different recorded outcome")
    if existing != "prepared":
        raise SoloAIError("Delivery attempt is not ready to record")
    attempt.update(
        {"outcome": outcome, "detail": detail, "delivered_at": utc_timestamp()}
    )
    if request.get(active_attempt_key) == attempt_id:
        request[status_key] = outcome
    return True


def _identified_attempt_context(
    request: dict[str, Any],
    *,
    attempts_key: str,
    attempt_id: str | None,
    sender: dict[str, str],
) -> tuple[dict[str, str], dict[str, str], int] | None:
    """为带编号的历史回执取回原发送上下文，避免迟到回执串入当前分配。"""

    if attempt_id is None:
        return None
    attempts = request.get(attempts_key)
    if not isinstance(attempts, list) or not any(
        isinstance(item.get("id"), str) for item in attempts
    ):
        return None
    attempt = next((item for item in attempts if item.get("id") == attempt_id), None)
    if not isinstance(attempt, dict):
        raise SoloAIError("Unknown delivery attempt")
    attempt_sender = normalize_host_reference(attempt.get("sender"))
    recipient = normalize_host_reference(attempt.get("recipient"))
    if not _same_host(attempt_sender, sender) or recipient is None:
        raise SoloAIError("Delivery attempt does not match this sender or recipient")
    try:
        revision = int(attempt.get("coordinator_revision"))
    except (TypeError, ValueError) as error:
        raise SoloAIError(
            "Delivery attempt has an invalid coordinator revision"
        ) from error
    return attempt_sender, recipient, revision


class HostHandoffStore:
    """保存宿主协作回执；候选、批次与 Git 事实仍由 DWW 主状态管理。"""

    def __init__(self, repo: GitRepo):
        self.repo = repo
        self.path = repo.local_dir / "host-handoffs.json"
        self.lock_path = repo.local_dir / "locks" / "host-handoffs.lock"

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "schema_version": HANDOFF_SCHEMA,
            "repair_requests": {},
            "updated_at": utc_timestamp(),
        }

    @staticmethod
    def _upgrade_request(request: dict[str, Any]) -> None:
        """把旧的“已派发”记录降级为待确认，避免把待发送消息当回执。"""

        if request.get("delivery_status") == "dispatched":
            request["delivery_status"] = "uncertain"
        request.setdefault("delivery_status", "pending")
        request.setdefault("delivery_attempts", [])
        request.setdefault("delivery_attempt_id", None)
        request.setdefault("return_delivery_status", "pending")
        request.setdefault("return_delivery_attempts", [])
        request.setdefault("return_delivery_attempt_id", None)
        request.setdefault("repair_candidate", None)
        request.setdefault("attribution", {"kind": "legacy"})
        request.setdefault("takeovers", [])
        request.setdefault("claim", None)
        request.setdefault("repair_task_id", None)
        request.setdefault("repair_result", None)

    def read(self) -> dict[str, Any]:
        value = read_json(self.path, self._empty())
        version = value.get("schema_version")
        if version == 1:
            value["schema_version"] = HANDOFF_SCHEMA
        elif version != HANDOFF_SCHEMA:
            raise SoloAIError("Unsupported host handoff state schema")
        requests = value.setdefault("repair_requests", {})
        if not isinstance(requests, dict):
            raise SoloAIError("Host handoff repair requests are invalid")
        for request in requests.values():
            if not isinstance(request, dict):
                raise SoloAIError("Host handoff repair request is invalid")
            self._upgrade_request(request)
        return value

    def mutate(self, callback: Callable[[dict[str, Any]], Any]) -> Any:
        with DirectoryLock(self.lock_path, wait=True):
            value = self.read()
            result = callback(value)
            value["updated_at"] = utc_timestamp()
            atomic_write_json(self.path, value)
            return result

    @staticmethod
    def repair_request_id(batch: dict[str, Any], candidate: dict[str, Any]) -> str:
        payload = {
            "schema_version": REQUEST_SCHEMA,
            "batch_id": batch.get("id"),
            "candidate_id": candidate.get("candidate_id"),
            "repair_attempt": int(candidate.get("repair_attempt", 0)) + 1,
        }
        return "repair-" + sha256_text(stable_json(payload))[:24]

    def ensure_composition_conflict(
        self, *, batch: dict[str, Any], candidate: dict[str, Any]
    ) -> dict[str, Any]:
        if (
            batch.get("status") != "failed"
            or batch.get("failure_kind") != "composition_conflict"
            or batch.get("failed_candidate_id") != candidate.get("candidate_id")
            or not candidate.get("repair_eligible")
        ):
            raise SoloAIError(
                "Only an attributed composition conflict can create a repair handoff"
            )
        return self._ensure_request(
            batch=batch,
            candidate=candidate,
            attribution={"kind": "automatic_composition_conflict"},
        )

    def attribute_validation_failure(
        self,
        *,
        batch_id: str,
        candidate_id: str,
        coordinator: dict[str, str],
        evidence: str,
    ) -> dict[str, Any]:
        """由当前批次负责人基于明确证据归因，绝不从失败自动推断业务返修。"""

        coordinator = normalize_host_reference(coordinator)
        if coordinator is None:
            raise SoloAIError("Validation attribution requires a verified coordinator")
        evidence = _reason(evidence)
        from .candidate_batches import CandidateBatchStore

        pool = CandidateBatchStore(self.repo)
        batch = pool.batch(batch_id)
        if (
            batch.get("status") != "failed"
            or batch.get("failure_kind") != "validation_failed"
        ):
            raise SoloAIError("Only a failed validation batch can be attributed")
        current = normalize_host_reference(batch.get("host_coordinator"))
        if not _same_host(current, coordinator):
            raise SoloAIError(
                "Only the current batch coordinator can attribute validation"
            )
        if candidate_id not in [str(item) for item in batch.get("candidate_ids", [])]:
            raise SoloAIError("Attributed candidate is not in the failed batch")
        pool.mark_candidate_repair_eligible(
            batch_id=batch_id,
            candidate_id=candidate_id,
            failure_kind="validation_failed",
        )
        return self._ensure_request(
            batch=pool.batch(batch_id),
            candidate=pool.candidate(candidate_id),
            attribution={
                "kind": "coordinator_validation_failure",
                "coordinator": coordinator,
                "evidence": evidence,
            },
        )

    def _ensure_request(
        self,
        *,
        batch: dict[str, Any],
        candidate: dict[str, Any],
        attribution: dict[str, Any],
    ) -> dict[str, Any]:
        origin = normalize_host_reference(candidate.get("host_origin"))
        coordinator = normalize_host_reference(batch.get("host_coordinator"))
        request_id = self.repair_request_id(batch, candidate)

        def update(value: dict[str, Any]) -> dict[str, Any]:
            existing = value["repair_requests"].get(request_id)
            if existing:
                immutable = {
                    "batch_id": batch.get("id"),
                    "candidate_id": candidate.get("candidate_id"),
                    "candidate_head": candidate.get("head"),
                    "origin": origin,
                }
                if any(existing.get(key) != item for key, item in immutable.items()):
                    raise SoloAIError("Repair handoff identity changed")
                return copy.deepcopy(existing)
            request = {
                "schema_version": REQUEST_SCHEMA,
                "id": request_id,
                "batch_id": batch["id"],
                "candidate_id": candidate["candidate_id"],
                "candidate_head": candidate["head"],
                "repair_attempt": int(candidate.get("repair_attempt", 0)) + 1,
                "origin": origin,
                "assignee": copy.deepcopy(origin),
                "coordinator": coordinator,
                "coordinator_revision": int(batch.get("host_coordinator_revision", 0)),
                "failure_kind": batch["failure_kind"],
                "failure_detail": _failure_detail(batch.get("error") or ""),
                "attribution": copy.deepcopy(attribution),
                "status": "pending",
                "delivery_status": "pending",
                "delivery_attempts": [],
                "delivery_attempt_id": None,
                "claim": None,
                "repair_task_id": None,
                "repair_result": None,
                "repair_candidate": None,
                "return_delivery_status": "pending",
                "return_delivery_attempts": [],
                "return_delivery_attempt_id": None,
                "takeovers": [],
                "created_at": utc_timestamp(),
                "updated_at": utc_timestamp(),
            }
            value["repair_requests"][request_id] = request
            return copy.deepcopy(request)

        return self.mutate(update)

    def request(self, request_id: str) -> dict[str, Any]:
        request = self.read()["repair_requests"].get(request_id)
        if not isinstance(request, dict):
            raise SoloAIError(f"Unknown repair handoff: {request_id}")
        return copy.deepcopy(request)

    def dispatch(
        self, *, request_id: str, sender: dict[str, str], retry: bool = False
    ) -> dict[str, Any]:
        """准备原负责人向返修负责人的消息；调用方发送后必须单独记录结果。"""

        sender, coordinator, revision = self._require_current_coordinator(
            request_id=request_id, sender=sender, action="dispatch this repair"
        )

        def update(value: dict[str, Any]) -> dict[str, Any]:
            request = self._request_in(value, request_id)
            if request.get("repair_task_id"):
                return copy.deepcopy(request)
            assignee = normalize_host_reference(request.get("assignee"))
            if assignee is None:
                raise SoloAIError(
                    "Repair handoff has no assignee; take it over before dispatch"
                )
            attempts = request.setdefault("delivery_attempts", [])
            current = request.get("delivery_status")
            same_revision = (
                _same_host(
                    normalize_host_reference(request.get("coordinator")), coordinator
                )
                and int(request.get("coordinator_revision", 0)) == revision
            )
            if current in {"prepared", "sent"} and same_revision and not retry:
                return copy.deepcopy(request)
            if current in {"prepared", "sent"} and same_revision:
                raise SoloAIError(
                    "A delivered or prepared repair message cannot be retried"
                )
            if current in {"failed", "uncertain"} and not retry:
                raise SoloAIError(
                    "Repair delivery needs --retry after a failure or uncertainty"
                )
            attempt_count = sum(
                1 for item in attempts if isinstance(item.get("id"), str)
            )
            if attempt_count >= _MAX_DELIVERY_ATTEMPTS:
                raise SoloAIError(
                    "Repair delivery retry limit reached; take over or investigate"
                )
            attempt_id = new_id("repair-delivery")
            attempts.append(
                {
                    "id": attempt_id,
                    "sender": sender,
                    "recipient": assignee,
                    "coordinator_revision": revision,
                    "at": utc_timestamp(),
                    "retry": retry,
                    "outcome": "prepared",
                }
            )
            del attempts[:-_MAX_REQUEST_HISTORY]
            request.update(
                {
                    "coordinator": coordinator,
                    "coordinator_revision": revision,
                    "delivery_status": "prepared",
                    "delivery_attempt_id": attempt_id,
                    "updated_at": utc_timestamp(),
                }
            )
            return copy.deepcopy(request)

        request = self.mutate(update)
        return {**request, "message": self._message(request)}

    def record_delivery(
        self,
        *,
        request_id: str,
        sender: dict[str, str],
        outcome: str,
        detail: str | None = None,
        attempt_id: str | None = None,
    ) -> dict[str, Any]:
        """记录宿主原生消息的实际发送结果，不由 DWW 猜测成功。"""

        if outcome not in _DELIVERY_OUTCOMES:
            raise SoloAIError(
                "Repair delivery outcome must be sent, uncertain, or failed"
            )
        detail = _delivery_detail(detail)
        sender = normalize_host_reference(sender)
        if sender is None:
            raise SoloAIError("Repair delivery requires a verified host sender")
        existing = self.request(request_id)
        attempt_context = _identified_attempt_context(
            existing,
            attempts_key="delivery_attempts",
            attempt_id=attempt_id,
            sender=sender,
        )
        if attempt_context is None:
            sender, coordinator, revision = self._require_current_coordinator(
                request_id=request_id,
                sender=sender,
                action="record this repair delivery",
            )
            recipient = None
        else:
            sender, recipient, revision = attempt_context
            coordinator = sender

        def update(value: dict[str, Any]) -> dict[str, Any]:
            request = self._request_in(value, request_id)
            attempts = request.setdefault("delivery_attempts", [])
            if not attempts or (
                attempt_context is None
                and request.get("delivery_status")
                not in {"prepared", "sent", "uncertain", "failed"}
            ):
                raise SoloAIError(
                    "Prepare the repair message before recording delivery"
                )
            assignee = recipient or normalize_host_reference(request.get("assignee"))
            if assignee is None:
                raise SoloAIError("Repair handoff has no assignee")
            recorded = _record_attempt_outcome(
                request,
                attempts_key="delivery_attempts",
                status_key="delivery_status",
                active_attempt_key="delivery_attempt_id",
                attempt_id=attempt_id,
                sender=sender,
                recipient=assignee,
                coordinator_revision=revision,
                outcome=outcome,
                detail=detail,
            )
            if recorded is not None:
                if not recorded:
                    return copy.deepcopy(request)
                if request.get("delivery_attempt_id") == attempt_id:
                    request.update(
                        {
                            "coordinator": coordinator,
                            "coordinator_revision": revision,
                            "updated_at": utc_timestamp(),
                        }
                    )
                return copy.deepcopy(request)
            attempts.append(
                {
                    "sender": sender,
                    "coordinator_revision": revision,
                    "at": utc_timestamp(),
                    "outcome": outcome,
                    "detail": detail,
                }
            )
            del attempts[:-_MAX_REQUEST_HISTORY]
            request.update(
                {
                    "coordinator": coordinator,
                    "coordinator_revision": revision,
                    "delivery_status": outcome,
                    "updated_at": utc_timestamp(),
                }
            )
            return copy.deepcopy(request)

        return self.mutate(update)

    def claim(self, *, request_id: str, actor: dict[str, str]) -> dict[str, Any]:
        actor = normalize_host_reference(actor)
        if actor is None:
            raise SoloAIError("Repair claim requires a verified host actor")

        def update(value: dict[str, Any]) -> dict[str, Any]:
            request = self._request_in(value, request_id)
            if request.get("repair_task_id"):
                claim = normalize_host_reference(
                    (request.get("claim") or {}).get("actor")
                )
                if _same_host(claim, actor):
                    return copy.deepcopy(request)
                raise SoloAIError("Repair handoff is already prepared by another host")
            assignee = normalize_host_reference(request.get("assignee"))
            if not _same_host(assignee, actor):
                raise SoloAIError(
                    "Only the recorded repair assignee can claim this handoff"
                )
            if request.get("delivery_status") not in {"sent", "uncertain"}:
                raise SoloAIError(
                    "Record a sent or uncertain repair delivery before claiming it"
                )
            claim = request.get("claim") or {}
            previous = normalize_host_reference(claim.get("actor"))
            if previous is not None and not _same_host(previous, actor):
                raise SoloAIError("Repair handoff is already claimed by another host")
            request.update(
                {
                    "status": "claimed",
                    "delivery_status": "sent",
                    "claim": {"actor": actor, "at": claim.get("at") or utc_timestamp()},
                    "updated_at": utc_timestamp(),
                }
            )
            return copy.deepcopy(request)

        return self.mutate(update)

    def take_over(
        self, *, request_id: str, actor: dict[str, str], reason: str
    ) -> dict[str, Any]:
        actor = normalize_host_reference(actor)
        if actor is None:
            raise SoloAIError("Repair takeover requires a verified host actor")
        reason = _reason(reason)

        def update(value: dict[str, Any]) -> dict[str, Any]:
            request = self._request_in(value, request_id)
            if request.get("repair_task_id"):
                raise SoloAIError("A prepared repair handoff cannot be taken over")
            previous = normalize_host_reference(request.get("assignee"))
            takeovers = request.setdefault("takeovers", [])
            if (
                previous == actor
                and takeovers
                and takeovers[-1].get("to") == actor
                and takeovers[-1].get("reason") == reason
            ):
                return copy.deepcopy(request)
            takeovers.append(
                {
                    "from": previous,
                    "to": actor,
                    "reason": reason,
                    "at": utc_timestamp(),
                }
            )
            del takeovers[:-_MAX_REQUEST_HISTORY]
            request.update(
                {
                    "assignee": actor,
                    "status": "pending",
                    "delivery_status": "pending",
                    "delivery_attempt_id": None,
                    "claim": None,
                    "updated_at": utc_timestamp(),
                }
            )
            return copy.deepcopy(request)

        return self.mutate(update)

    def prepare(self, *, request_id: str, actor: dict[str, str]) -> dict[str, Any]:
        claimed = self.claim(request_id=request_id, actor=actor)
        if claimed.get("repair_task_id"):
            return {"request": claimed, "repair": claimed.get("repair_result")}
        from .candidate_batches import prepare_candidate_repair

        repair = prepare_candidate_repair(
            self.repo,
            candidate_id=str(claimed["candidate_id"]),
            host_origin=actor,
        )

        def update(value: dict[str, Any]) -> dict[str, Any]:
            request = self._request_in(value, request_id)
            if request.get("repair_task_id"):
                return copy.deepcopy(request)
            if repair.get("outcome") == "already_in_base":
                request.update(
                    {
                        "status": "resolved",
                        "repair_result": copy.deepcopy(repair),
                        "updated_at": utc_timestamp(),
                    }
                )
                return copy.deepcopy(request)
            repair_task_id = repair.get("id")
            if not isinstance(repair_task_id, str):
                raise SoloAIError("Candidate repair did not return a durable task id")
            request.update(
                {
                    "status": "prepared",
                    "repair_task_id": repair_task_id,
                    "repair_result": copy.deepcopy(repair),
                    "updated_at": utc_timestamp(),
                }
            )
            return copy.deepcopy(request)

        request = self.mutate(update)
        return {"request": request, "repair": request.get("repair_result")}

    def record_repair_candidate_published(
        self, *, task_id: str, candidate: dict[str, Any]
    ) -> dict[str, Any] | None:
        """返修候选发布后记录待回告事实；发布本身不等同于交付。"""

        candidate_id = str(candidate.get("candidate_id") or "")
        if not candidate_id or str(candidate.get("task_id") or "") != task_id:
            raise SoloAIError("Repair candidate publication identity is incomplete")
        if not any(
            request.get("repair_task_id") == task_id
            for request in self.read()["repair_requests"].values()
        ):
            return None

        def update(value: dict[str, Any]) -> dict[str, Any] | None:
            matches = [
                request
                for request in value["repair_requests"].values()
                if request.get("repair_task_id") == task_id
            ]
            if not matches:
                return None
            if len(matches) != 1:
                raise SoloAIError("Repair task is linked to more than one handoff")
            request = matches[0]
            recorded = request.get("repair_candidate")
            identity = {
                "candidate_id": candidate_id,
                "head": candidate.get("head"),
                "ref": candidate.get("ref"),
            }
            if recorded:
                if any(recorded.get(key) != item for key, item in identity.items()):
                    raise SoloAIError("Repair candidate identity changed")
                return copy.deepcopy(request)
            request.update(
                {
                    "status": "repair-candidate-published",
                    "repair_candidate": {**identity, "published_at": utc_timestamp()},
                    "return_delivery_status": "pending",
                    "updated_at": utc_timestamp(),
                }
            )
            return copy.deepcopy(request)

        return self.mutate(update)

    def dispatch_repair_result(
        self, *, request_id: str, sender: dict[str, str], retry: bool = False
    ) -> dict[str, Any]:
        """准备返修负责人回告候选的消息；发送结果由单独回执记录。"""

        sender, coordinator, revision, repair_candidate = self._require_repair_sender(
            request_id=request_id, sender=sender
        )

        def update(value: dict[str, Any]) -> dict[str, Any]:
            request = self._request_in(value, request_id)
            attempts = request.setdefault("return_delivery_attempts", [])
            current = request.get("return_delivery_status")
            same_revision = (
                _same_host(
                    normalize_host_reference(request.get("coordinator")), coordinator
                )
                and int(request.get("coordinator_revision", 0)) == revision
            )
            if current in {"prepared", "sent"} and same_revision and not retry:
                return copy.deepcopy(request)
            if current in {"prepared", "sent"} and same_revision:
                raise SoloAIError(
                    "A delivered or prepared repair result cannot be retried"
                )
            if current in {"failed", "uncertain"} and not retry:
                raise SoloAIError(
                    "Repair result delivery needs --retry after a failure or uncertainty"
                )
            attempt_count = sum(
                1 for item in attempts if isinstance(item.get("id"), str)
            )
            if attempt_count >= _MAX_DELIVERY_ATTEMPTS:
                raise SoloAIError(
                    "Repair result delivery retry limit reached; investigate"
                )
            attempt_id = new_id("repair-result-delivery")
            attempts.append(
                {
                    "id": attempt_id,
                    "sender": sender,
                    "recipient": coordinator,
                    "coordinator_revision": revision,
                    "candidate_id": repair_candidate["candidate_id"],
                    "at": utc_timestamp(),
                    "retry": retry,
                    "outcome": "prepared",
                }
            )
            del attempts[:-_MAX_REQUEST_HISTORY]
            request.update(
                {
                    "coordinator": coordinator,
                    "coordinator_revision": revision,
                    "return_delivery_status": "prepared",
                    "return_delivery_attempt_id": attempt_id,
                    "updated_at": utc_timestamp(),
                }
            )
            return copy.deepcopy(request)

        request = self.mutate(update)
        return {**request, "message": self._result_message(request)}

    def record_repair_result_delivery(
        self,
        *,
        request_id: str,
        sender: dict[str, str],
        outcome: str,
        detail: str | None = None,
        attempt_id: str | None = None,
    ) -> dict[str, Any]:
        if outcome not in _DELIVERY_OUTCOMES:
            raise SoloAIError(
                "Repair result outcome must be sent, uncertain, or failed"
            )
        detail = _delivery_detail(detail)
        sender = normalize_host_reference(sender)
        if sender is None:
            raise SoloAIError("Repair result delivery requires a verified host sender")
        existing = self.request(request_id)
        attempt_context = _identified_attempt_context(
            existing,
            attempts_key="return_delivery_attempts",
            attempt_id=attempt_id,
            sender=sender,
        )
        current_sender, current_coordinator, current_revision, repair_candidate = (
            self._require_repair_sender(request_id=request_id, sender=sender)
        )
        if attempt_context is None:
            sender, coordinator, revision = (
                current_sender,
                current_coordinator,
                current_revision,
            )
            recipient = None
        else:
            sender, recipient, revision = attempt_context
            coordinator = recipient

        def update(value: dict[str, Any]) -> dict[str, Any]:
            request = self._request_in(value, request_id)
            attempts = request.setdefault("return_delivery_attempts", [])
            if not attempts or (
                attempt_context is None
                and request.get("return_delivery_status")
                not in {"prepared", "sent", "uncertain", "failed"}
            ):
                raise SoloAIError(
                    "Prepare the repair result message before recording delivery"
                )
            recorded = _record_attempt_outcome(
                request,
                attempts_key="return_delivery_attempts",
                status_key="return_delivery_status",
                active_attempt_key="return_delivery_attempt_id",
                attempt_id=attempt_id,
                sender=sender,
                recipient=recipient or coordinator,
                coordinator_revision=revision,
                outcome=outcome,
                detail=detail,
                candidate_id=str(repair_candidate["candidate_id"]),
            )
            if recorded is not None:
                if not recorded:
                    return copy.deepcopy(request)
                if request.get("return_delivery_attempt_id") == attempt_id:
                    request.update(
                        {
                            "coordinator": coordinator,
                            "coordinator_revision": revision,
                            "updated_at": utc_timestamp(),
                        }
                    )
                return copy.deepcopy(request)
            attempts.append(
                {
                    "sender": sender,
                    "coordinator_revision": revision,
                    "candidate_id": repair_candidate["candidate_id"],
                    "at": utc_timestamp(),
                    "outcome": outcome,
                    "detail": detail,
                }
            )
            del attempts[:-_MAX_REQUEST_HISTORY]
            request.update(
                {
                    "coordinator": coordinator,
                    "coordinator_revision": revision,
                    "return_delivery_status": outcome,
                    "updated_at": utc_timestamp(),
                }
            )
            return copy.deepcopy(request)

        return self.mutate(update)

    def status(self, *, pool: dict[str, Any] | None = None) -> dict[str, Any]:
        from .candidate_batches import CandidateBatchStore

        pool = pool or CandidateBatchStore(self.repo).summary()
        candidates = {
            str(candidate["candidate_id"]): candidate
            for candidate in pool["candidates"]
        }
        batches = {str(batch["id"]): batch for batch in pool["batches"]}
        requests = [
            self._project_request(request, candidates=candidates, batches=batches)
            for request in self.read()["repair_requests"].values()
        ]
        requests.sort(
            key=lambda request: (str(request.get("created_at")), str(request["id"]))
        )
        actions: list[dict[str, Any]] = []
        for request in requests:
            lifecycle = request["lifecycle_status"]
            if lifecycle == "actionable":
                actions.extend(self._request_actions(request))
            elif lifecycle == "repair-pending-integration":
                actions.extend(self._repair_result_actions(request))
        return {"repair_requests": requests, "actions": actions}

    @staticmethod
    def _request_actions(request: dict[str, Any]) -> list[dict[str, Any]]:
        if request.get("assignee") is None:
            return [
                {
                    "kind": "assign_repair_owner",
                    "request_id": request["id"],
                    "candidate_id": request["candidate_id"],
                }
            ]
        if request.get("coordinator") is None:
            return [
                {
                    "kind": "assign_batch_coordinator",
                    "batch_id": request["batch_id"],
                    "request_id": request["id"],
                }
            ]
        if request.get("status") == "claimed":
            claim = request.get("claim") or {}
            return [
                {
                    "kind": "prepare_repair",
                    "request_id": request["id"],
                    "target": claim.get("actor"),
                }
            ]
        delivery = request.get("delivery_status")
        if delivery in {"pending", "failed"} or request.get("coordinator_stale"):
            return [
                {
                    "kind": "dispatch_repair_request",
                    "request_id": request["id"],
                    "target": request["assignee"],
                    "coordinator": request["current_coordinator"],
                }
            ]
        if delivery == "prepared":
            return [
                {
                    "kind": "send_repair_request",
                    "request_id": request["id"],
                    "target": request["assignee"],
                }
            ]
        if delivery == "uncertain":
            return [
                {
                    "kind": "confirm_or_retry_repair_delivery",
                    "request_id": request["id"],
                    "target": request["assignee"],
                }
            ]
        return [
            {
                "kind": "await_repair_claim",
                "request_id": request["id"],
                "target": request["assignee"],
            }
        ]

    @staticmethod
    def _repair_result_actions(request: dict[str, Any]) -> list[dict[str, Any]]:
        if request.get("current_coordinator") is None:
            return [
                {
                    "kind": "assign_batch_coordinator",
                    "batch_id": request["batch_id"],
                    "request_id": request["id"],
                }
            ]
        delivery = request.get("return_delivery_status")
        target = request["current_coordinator"]
        if delivery in {"pending", "failed"} or request.get("return_coordinator_stale"):
            return [
                {
                    "kind": "dispatch_repair_candidate",
                    "request_id": request["id"],
                    "target": target,
                    "candidate_id": request["repair_candidate"]["candidate_id"],
                }
            ]
        if delivery == "prepared":
            return [
                {
                    "kind": "send_repair_candidate",
                    "request_id": request["id"],
                    "target": target,
                    "candidate_id": request["repair_candidate"]["candidate_id"],
                }
            ]
        if delivery == "uncertain":
            return [
                {
                    "kind": "confirm_or_retry_repair_result_delivery",
                    "request_id": request["id"],
                    "target": target,
                }
            ]
        return [
            {
                "kind": "await_coordinator_integration",
                "request_id": request["id"],
                "target": target,
                "candidate_id": request["repair_candidate"]["candidate_id"],
            }
        ]

    def _batch(self, request_id: str) -> dict[str, Any]:
        from .candidate_batches import CandidateBatchStore

        request = self.request(request_id)
        return CandidateBatchStore(self.repo).batch(str(request["batch_id"]))

    def _require_current_coordinator(
        self, *, request_id: str, sender: dict[str, str], action: str
    ) -> tuple[dict[str, str], dict[str, str], int]:
        sender = normalize_host_reference(sender)
        if sender is None:
            raise SoloAIError("Repair delivery requires a verified host sender")
        batch = self._batch(request_id)
        coordinator = normalize_host_reference(batch.get("host_coordinator"))
        revision = int(batch.get("host_coordinator_revision", 0))
        if coordinator is None:
            raise SoloAIError("Repair delivery requires a recorded batch coordinator")
        if not _same_host(sender, coordinator):
            raise SoloAIError(f"Only the recorded batch coordinator can {action}")
        return sender, coordinator, revision

    def _require_repair_sender(
        self, *, request_id: str, sender: dict[str, str]
    ) -> tuple[dict[str, str], dict[str, str], int, dict[str, Any]]:
        sender = normalize_host_reference(sender)
        if sender is None:
            raise SoloAIError("Repair result delivery requires a verified host sender")
        request = self.request(request_id)
        repair_candidate = request.get("repair_candidate")
        if not isinstance(repair_candidate, dict):
            raise SoloAIError("Repair candidate has not been published")
        from .candidate_batches import CandidateBatchStore

        candidate = CandidateBatchStore(self.repo).candidate(
            str(repair_candidate.get("candidate_id") or "")
        )
        if not _same_host(
            sender, normalize_host_reference(candidate.get("host_origin"))
        ):
            raise SoloAIError("Only the recorded repair candidate host can return it")
        batch = self._batch(request_id)
        coordinator = normalize_host_reference(batch.get("host_coordinator"))
        if coordinator is None:
            raise SoloAIError(
                "Repair result delivery requires a recorded batch coordinator"
            )
        return (
            sender,
            coordinator,
            int(batch.get("host_coordinator_revision", 0)),
            repair_candidate,
        )

    @staticmethod
    def _request_in(value: dict[str, Any], request_id: str) -> dict[str, Any]:
        request = value["repair_requests"].get(request_id)
        if not isinstance(request, dict):
            raise SoloAIError(f"Unknown repair handoff: {request_id}")
        return request

    @staticmethod
    def _message(request: dict[str, Any]) -> str:
        assignee = request.get("assignee") or {}
        return "\n".join(
            (
                f"返修请求 ID：{request['id']}",
                f"失败批次：{request['batch_id']}",
                f"源候选：{request['candidate_id']} at {request['candidate_head']}",
                f"失败类型：{request['failure_kind']}",
                f"失败详情：{request['failure_detail']}",
                f"目标宿主：{assignee.get('kind')} / {assignee.get('thread_id')}",
                f"发送尝试 ID：{request.get('delivery_attempt_id')}",
                "发送后用 host-handoff repair delivery --attempt <发送尝试 ID> 记录实际结果；"
                "接收方再 claim 并 prepare 建立受管返修任务。",
            )
        )

    @staticmethod
    def _result_message(request: dict[str, Any]) -> str:
        candidate = request.get("repair_candidate") or {}
        coordinator = (
            request.get("current_coordinator") or request.get("coordinator") or {}
        )
        return "\n".join(
            (
                f"返修候选回告 ID：{request['id']}",
                f"返修候选：{candidate.get('candidate_id')} at {candidate.get('head')}",
                f"目标负责人：{coordinator.get('kind')} / {coordinator.get('thread_id')}",
                f"发送尝试 ID：{request.get('return_delivery_attempt_id')}",
                "候选已发布，尚未交付；请由当前批次负责人继续集成并以 main 的实际提交为准。",
            )
        )

    @staticmethod
    def _terminal_candidate(
        candidates: dict[str, dict[str, Any]], candidate: dict[str, Any] | None
    ) -> tuple[dict[str, Any] | None, str | None]:
        """沿已记录的替代链读取末端，不把缺失或循环误当成交付。"""

        current = candidate
        seen: set[str] = set()
        while current is not None:
            candidate_id = str(current.get("candidate_id") or "")
            if not candidate_id or candidate_id in seen:
                return None, "candidate successor lineage is missing or cyclic"
            seen.add(candidate_id)
            if current.get("status") != "superseded":
                return current, None
            successor_id = str(current.get("superseded_by") or "")
            successor = candidates.get(successor_id)
            if (
                successor is None
                or str(successor.get("supersedes") or "") != candidate_id
            ):
                return None, "candidate successor lineage is incomplete"
            current = successor
        return None, "candidate successor lineage is missing"

    @staticmethod
    def _project_request(
        request: dict[str, Any],
        *,
        candidates: dict[str, dict[str, Any]],
        batches: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        projected = copy.deepcopy(request)
        candidate = candidates.get(str(request["candidate_id"]))
        batch = batches.get(str(request["batch_id"]))
        repair_identity = request.get("repair_candidate")
        repair_candidate = (
            candidates.get(str(repair_identity.get("candidate_id")))
            if isinstance(repair_identity, dict)
            else None
        )
        source_terminal, source_lineage_error = HostHandoffStore._terminal_candidate(
            candidates, candidate
        )
        repair_terminal, repair_lineage_error = HostHandoffStore._terminal_candidate(
            candidates, repair_candidate
        )
        projected["source_candidate_lineage_error"] = source_lineage_error
        if repair_identity:
            projected["repair_candidate_lineage_error"] = repair_lineage_error
            projected["repair_terminal_candidate_id"] = (
                repair_terminal.get("candidate_id") if repair_terminal else None
            )
        if request.get("status") == "resolved":
            projected["lifecycle_status"] = "resolved"
        elif repair_identity:
            if repair_terminal and repair_terminal.get("delivered"):
                projected["lifecycle_status"] = "resolved"
            elif repair_terminal and repair_terminal.get("status") == "withdrawn":
                projected["lifecycle_status"] = "closed-without-delivery"
            elif repair_terminal:
                projected["lifecycle_status"] = "repair-pending-integration"
            else:
                projected["lifecycle_status"] = "repair-return-blocked"
        elif source_terminal and source_terminal.get("delivered"):
            projected["lifecycle_status"] = "resolved"
        elif source_terminal and source_terminal.get("status") == "withdrawn":
            projected["lifecycle_status"] = "closed-without-delivery"
        elif not source_terminal or not batch:
            projected["lifecycle_status"] = "unknown"
        elif request.get("repair_task_id"):
            projected["lifecycle_status"] = "repair-prepared"
        else:
            projected["lifecycle_status"] = "actionable"
        coordinator = normalize_host_reference((batch or {}).get("host_coordinator"))
        revision = int((batch or {}).get("host_coordinator_revision", 0))
        projected["current_coordinator"] = coordinator
        projected["current_coordinator_revision"] = revision
        projected["coordinator_stale"] = coordinator is not None and (
            not _same_host(
                coordinator, normalize_host_reference(request.get("coordinator"))
            )
            or revision != int(request.get("coordinator_revision", 0))
        )
        projected["return_coordinator_stale"] = projected["coordinator_stale"]
        return projected
