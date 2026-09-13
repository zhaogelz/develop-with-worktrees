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
    read_json,
    redact_text,
    sha256_text,
    stable_json,
    utc_timestamp,
)

HANDOFF_SCHEMA = 1
REQUEST_SCHEMA = 1
_MAX_REASON_LENGTH = 512
_MAX_REQUEST_HISTORY = 20


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

    def read(self) -> dict[str, Any]:
        value = read_json(self.path, self._empty())
        if value.get("schema_version") != HANDOFF_SCHEMA:
            raise SoloAIError("Unsupported host handoff state schema")
        requests = value.setdefault("repair_requests", {})
        if not isinstance(requests, dict):
            raise SoloAIError("Host handoff repair requests are invalid")
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
                "status": "pending",
                "delivery_status": "pending",
                "delivery_attempts": [],
                "claim": None,
                "repair_task_id": None,
                "repair_result": None,
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
        sender = normalize_host_reference(sender)
        if sender is None:
            raise SoloAIError("Repair dispatch requires a verified host sender")
        batch = self._batch(request_id)
        coordinator = normalize_host_reference(batch.get("host_coordinator"))
        revision = int(batch.get("host_coordinator_revision", 0))
        if coordinator is None:
            raise SoloAIError("Repair dispatch requires a recorded batch coordinator")
        if not _same_host(sender, coordinator):
            raise SoloAIError(
                "Only the recorded batch coordinator can dispatch this repair"
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
            already_dispatched = (
                request.get("delivery_status") == "dispatched"
                and _same_host(
                    normalize_host_reference(request.get("coordinator")), coordinator
                )
                and int(request.get("coordinator_revision", 0)) == revision
            )
            if already_dispatched and not retry:
                return copy.deepcopy(request)
            attempts.append(
                {
                    "sender": sender,
                    "coordinator_revision": revision,
                    "at": utc_timestamp(),
                    "retry": retry,
                }
            )
            del attempts[:-_MAX_REQUEST_HISTORY]
            request.update(
                {
                    "coordinator": coordinator,
                    "coordinator_revision": revision,
                    "delivery_status": "dispatched",
                    "updated_at": utc_timestamp(),
                }
            )
            return copy.deepcopy(request)

        request = self.mutate(update)
        return {**request, "message": self._message(request)}

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
            claim = request.get("claim") or {}
            previous = normalize_host_reference(claim.get("actor"))
            if previous is not None and not _same_host(previous, actor):
                raise SoloAIError("Repair handoff is already claimed by another host")
            request.update(
                {
                    "status": "claimed",
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

    def status(self) -> dict[str, Any]:
        from .candidate_batches import CandidateBatchStore

        pool = CandidateBatchStore(self.repo).summary()
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
            if request["lifecycle_status"] != "actionable":
                continue
            if request.get("assignee") is None:
                actions.append(
                    {
                        "kind": "assign_repair_owner",
                        "request_id": request["id"],
                        "candidate_id": request["candidate_id"],
                    }
                )
                continue
            if request.get("coordinator") is None:
                actions.append(
                    {
                        "kind": "assign_batch_coordinator",
                        "batch_id": request["batch_id"],
                        "request_id": request["id"],
                    }
                )
                continue
            if request.get("status") == "claimed":
                claim = request.get("claim") or {}
                actions.append(
                    {
                        "kind": "prepare_repair",
                        "request_id": request["id"],
                        "target": claim.get("actor"),
                    }
                )
            elif request.get("delivery_status") == "pending" or request.get(
                "coordinator_stale"
            ):
                actions.append(
                    {
                        "kind": "dispatch_repair_request",
                        "request_id": request["id"],
                        "target": request["assignee"],
                        "coordinator": request["coordinator"],
                    }
                )
            elif (
                request.get("delivery_status") == "dispatched"
                and request.get("status") == "pending"
            ):
                actions.append(
                    {
                        "kind": "confirm_or_retry_repair_delivery",
                        "request_id": request["id"],
                        "target": request["assignee"],
                    }
                )
        return {"repair_requests": requests, "actions": actions}

    def _batch(self, request_id: str) -> dict[str, Any]:
        from .candidate_batches import CandidateBatchStore

        request = self.request(request_id)
        return CandidateBatchStore(self.repo).batch(str(request["batch_id"]))

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
                "请先使用 host-handoff repair claim 确认接收，再使用 host-handoff repair prepare 建立受管返修任务。",
            )
        )

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
        candidate_status = str((candidate or {}).get("status") or "missing")
        if candidate_status in {"superseded", "integrated", "withdrawn"}:
            projected["lifecycle_status"] = "resolved"
        elif not candidate or not batch:
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
        return projected
