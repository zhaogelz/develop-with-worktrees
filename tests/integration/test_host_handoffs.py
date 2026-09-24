from __future__ import annotations

from pathlib import Path

import pytest
from conftest import git
from solo_ai import candidate_batches as batch_module
from solo_ai.candidate_batches import CandidateBatchStore, seal_batch
from solo_ai.cli import _status, main as cli_main
from solo_ai.config import CommandSpec, load_verification_config
from solo_ai.host_handoffs import HostHandoffStore
from solo_ai.lifecycle import (
    approve,
    commit_task,
    finish,
    initialize,
    ready,
    recover,
    start,
)
from solo_ai.repo import GitRepo
from solo_ai.state import LEGACY_STATE_SCHEMA, StateStore
from solo_ai.util import SoloAIError

VERIFY = CommandSpec(("git", "diff", "--check", "main...HEAD"))


def initialized_batched(path: Path) -> GitRepo:
    repo = GitRepo(path)
    initialize(repo, slots=3, commands=[VERIFY], accept=True, accept_static_only=False)
    StateStore(repo).mutate(
        lambda state: state.update(schema_version=LEGACY_STATE_SCHEMA)
    )
    config = path / ".solo-ai" / "config.toml"
    config.write_text(
        config.read_text(encoding="utf-8")
        .replace('seal_policy = "auto_full"', 'seal_policy = "explicit"')
        .replace('worktree_mode = "reusable"', 'worktree_mode = "dedicated"'),
        encoding="utf-8",
    )
    git(path, "add", ".solo-ai/config.toml")
    git(path, "commit", "-m", "test: use explicit dedicated candidate batches")
    approve(repo, load_verification_config(repo))
    return repo


def publish(
    repo: GitRepo, *, host_origin: dict[str, str], relative: str = "conflict.txt"
) -> dict[str, str]:
    task = start(repo, name="handoff source", host_origin=host_origin)
    worktree = Path(task["worktree"])
    (worktree / relative).write_text("candidate change\n", encoding="utf-8")
    commit_task(
        repo,
        task_id=task["id"],
        lease=task["lease"],
        message="test: publish handoff source",
        paths=[relative],
    )
    ready(repo, task_id=task["id"], lease=task["lease"])
    return finish(repo, task_id=task["id"], lease=task["lease"])


def conflict_base(path: Path, *, relative: str = "conflict.txt") -> None:
    (path / relative).write_text("base change\n", encoding="utf-8")
    git(path, "add", relative)
    git(path, "commit", "-m", "test: create a candidate composition conflict")


def test_composition_conflict_has_one_durable_handoff_and_safe_takeovers(
    git_repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized_batched(git_repo)
    source = {"kind": "codex", "thread_id": "developer"}
    coordinator = {"kind": "codex", "thread_id": "coordinator"}
    next_coordinator = {"kind": "codex", "thread_id": "coordinator-replacement"}
    successor = {"kind": "codex", "thread_id": "developer-replacement"}
    candidate = publish(repo, host_origin=source)
    conflict_base(git_repo)

    with pytest.raises(SoloAIError, match="conflicts with the sealed batch"):
        seal_batch(
            repo,
            candidate_ids=[candidate["candidate_id"]],
            coordinator=coordinator,
        )

    handoffs = HostHandoffStore(repo)
    first_status = handoffs.status()
    assert len(first_status["repair_requests"]) == 1
    request = first_status["repair_requests"][0]
    assert request["origin"] == source
    assert request["assignee"] == source
    assert request["coordinator"] == coordinator
    assert first_status["actions"] == [
        {
            "kind": "dispatch_repair_request",
            "request_id": request["id"],
            "target": source,
            "coordinator": coordinator,
        }
    ]

    first_dispatch = handoffs.dispatch(request_id=request["id"], sender=coordinator)
    repeated_dispatch = handoffs.dispatch(request_id=request["id"], sender=coordinator)
    assert repeated_dispatch["message"] == first_dispatch["message"]
    assert len(repeated_dispatch["delivery_attempts"]) == 1
    first_attempt = first_dispatch["delivery_attempt_id"]

    batch = CandidateBatchStore(repo).batch(request["batch_id"])
    taken_batch = CandidateBatchStore(repo).assign_host_coordinator(
        request["batch_id"],
        coordinator=next_coordinator,
        expected_revision=batch["host_coordinator_revision"],
        reason="original coordinator is unavailable",
    )
    assert taken_batch["host_coordinator_revision"] == 2
    stale_status = handoffs.status()["repair_requests"][0]
    assert stale_status["coordinator_stale"] is True
    with pytest.raises(SoloAIError, match="recorded batch coordinator"):
        handoffs.dispatch(request_id=request["id"], sender=coordinator)
    successor_dispatch = handoffs.dispatch(
        request_id=request["id"], sender=next_coordinator
    )
    assert request["id"] in successor_dispatch["message"]
    assert successor_dispatch["delivery_attempt_id"] != first_attempt

    taken_repair = handoffs.take_over(
        request_id=request["id"], actor=successor, reason="source task is unavailable"
    )
    assert taken_repair["assignee"] == successor
    late_delivery = handoffs.record_delivery(
        request_id=request["id"],
        sender=coordinator,
        outcome="sent",
        attempt_id=first_attempt,
    )
    assert late_delivery["delivery_status"] == "pending"
    assert late_delivery["delivery_attempts"][0]["outcome"] == "sent"
    reissued = handoffs.dispatch(request_id=request["id"], sender=next_coordinator)
    assert reissued["delivery_status"] == "prepared"
    with pytest.raises(SoloAIError, match="does not match"):
        handoffs.record_delivery(
            request_id=request["id"],
            sender=next_coordinator,
            outcome="sent",
            attempt_id=first_attempt,
        )
    with pytest.raises(SoloAIError, match="attempt id is required"):
        handoffs.record_delivery(
            request_id=request["id"], sender=next_coordinator, outcome="sent"
        )
    delivered = handoffs.record_delivery(
        request_id=request["id"],
        sender=next_coordinator,
        outcome="sent",
        attempt_id=reissued["delivery_attempt_id"],
    )
    repeated_delivery = handoffs.record_delivery(
        request_id=request["id"],
        sender=next_coordinator,
        outcome="sent",
        attempt_id=reissued["delivery_attempt_id"],
    )
    assert repeated_delivery == delivered
    with pytest.raises(SoloAIError, match="recorded repair assignee"):
        handoffs.claim(request_id=request["id"], actor=source)

    claimed = handoffs.claim(request_id=request["id"], actor=successor)
    assert handoffs.status()["actions"] == [
        {
            "kind": "prepare_repair",
            "request_id": request["id"],
            "target": successor,
        }
    ]
    assert claimed["status"] == "claimed"

    arguments = [
        "--repo",
        str(git_repo),
        "host-handoff",
        "repair",
        "prepare",
        "--request",
        request["id"],
        "--host-kind",
        "codex",
        "--host-thread",
        successor["thread_id"],
    ]
    assert cli_main(arguments) == 0
    first_prepare = dict(
        line.split(": ", 1) for line in capsys.readouterr().out.splitlines()
    )
    assert cli_main(arguments) == 0
    replayed_prepare = dict(
        line.split(": ", 1) for line in capsys.readouterr().out.splitlines()
    )
    assert first_prepare["Task"] == replayed_prepare["Task"]
    assert first_prepare["Lease"] == replayed_prepare["Lease"]
    repair_task = first_prepare["Task"]
    assert repair_task
    assert (
        CandidateBatchStore(repo).candidate(candidate["candidate_id"])[
            "repair_eligible"
        ]
        is True
    )
    from solo_ai.state import StateStore

    assert StateStore(repo).task(repair_task)["host_origin"] == successor

    repair_worktree = Path(first_prepare["Worktree"])
    (repair_worktree / "conflict.txt").write_text("resolved\n", encoding="utf-8")
    commit_task(
        repo,
        task_id=repair_task,
        lease=first_prepare["Lease"],
        message="test: resolve the returned repair candidate",
        paths=["conflict.txt"],
    )
    ready(repo, task_id=repair_task, lease=first_prepare["Lease"])
    original_record = HostHandoffStore.record_repair_candidate_published
    calls = 0

    def fail_first_record(
        self: HostHandoffStore, *, task_id: str, candidate: dict[str, object]
    ) -> dict[str, object] | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise SoloAIError("test handoff receipt interruption")
        return original_record(self, task_id=task_id, candidate=candidate)

    monkeypatch.setattr(
        HostHandoffStore, "record_repair_candidate_published", fail_first_record
    )
    returned = finish(repo, task_id=repair_task, lease=first_prepare["Lease"])
    assert "recording_error" in returned["repair_handoff"]
    recovered = recover(repo, task_id=repair_task)
    assert recovered["repair_handoff"]["id"] == request["id"]

    returned_status = handoffs.status()
    assert returned_status["repair_requests"][0]["lifecycle_status"] == (
        "repair-pending-integration"
    )
    assert returned_status["actions"] == [
        {
            "kind": "dispatch_repair_candidate",
            "request_id": request["id"],
            "target": next_coordinator,
            "candidate_id": returned["candidate_id"],
        }
    ]
    result_dispatch = handoffs.dispatch_repair_result(
        request_id=request["id"], sender=successor
    )
    assert "尚未交付" in result_dispatch["message"]
    final_coordinator = {"kind": "codex", "thread_id": "final-coordinator"}
    current_batch = CandidateBatchStore(repo).batch(request["batch_id"])
    CandidateBatchStore(repo).assign_host_coordinator(
        request["batch_id"],
        coordinator=final_coordinator,
        expected_revision=current_batch["host_coordinator_revision"],
        reason="handoff result coordinator is unavailable",
    )
    reissued_result = handoffs.dispatch_repair_result(
        request_id=request["id"], sender=successor
    )
    late_result = handoffs.record_repair_result_delivery(
        request_id=request["id"],
        sender=successor,
        outcome="sent",
        attempt_id=result_dispatch["return_delivery_attempt_id"],
    )
    assert late_result["return_delivery_status"] == "prepared"
    assert late_result["return_delivery_attempts"][0]["outcome"] == "sent"
    returned_delivery = handoffs.record_repair_result_delivery(
        request_id=request["id"],
        sender=successor,
        outcome="sent",
        attempt_id=reissued_result["return_delivery_attempt_id"],
    )
    repeated_result_delivery = handoffs.record_repair_result_delivery(
        request_id=request["id"],
        sender=successor,
        outcome="sent",
        attempt_id=reissued_result["return_delivery_attempt_id"],
    )
    assert repeated_result_delivery == returned_delivery

    completed = seal_batch(
        repo,
        candidate_ids=[returned["candidate_id"]],
        coordinator=final_coordinator,
    )
    assert completed["status"] == "completed"
    assert handoffs.status()["repair_requests"][0]["lifecycle_status"] == "resolved"


def test_validation_failure_does_not_create_a_repair_handoff(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized_batched(git_repo)
    candidate = publish(repo, host_origin={"kind": "codex", "thread_id": "developer"})

    def fail_after_composition(
        _repo: GitRepo, store: CandidateBatchStore, batch: dict[str, object]
    ) -> dict[str, object]:
        store.update_batch(str(batch["id"]), status="composed")
        raise SoloAIError("test validation failure")

    monkeypatch.setattr(batch_module, "_resume", fail_after_composition)
    with pytest.raises(SoloAIError, match="test validation failure"):
        seal_batch(
            repo,
            candidate_ids=[candidate["candidate_id"]],
            coordinator={"kind": "codex", "thread_id": "coordinator"},
        )

    assert HostHandoffStore(repo).status()["repair_requests"] == []


def test_status_reuses_the_candidate_snapshot_for_handoff_projection(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized_batched(git_repo)
    original = CandidateBatchStore.summary
    calls = 0

    def counted_summary(self: CandidateBatchStore) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return original(self)

    monkeypatch.setattr(CandidateBatchStore, "summary", counted_summary)
    status = _status(repo, detailed=False)

    assert calls == 1
    assert status["host_handoffs"] == {"repair_requests": [], "actions": []}


def test_handoff_terminal_candidate_requires_a_complete_successor_lineage() -> None:
    first = {
        "candidate_id": "candidate-one",
        "status": "superseded",
        "superseded_by": "candidate-two",
    }
    second = {
        "candidate_id": "candidate-two",
        "status": "superseded",
        "supersedes": "candidate-one",
        "superseded_by": "candidate-three",
    }
    terminal = {
        "candidate_id": "candidate-three",
        "status": "integrated",
        "supersedes": "candidate-two",
        "delivered": True,
    }

    resolved, error = HostHandoffStore._terminal_candidate(
        {
            first["candidate_id"]: first,
            second["candidate_id"]: second,
            terminal["candidate_id"]: terminal,
        },
        first,
    )
    assert error is None
    assert resolved == terminal

    second["superseded_by"] = "candidate-one"
    blocked, error = HostHandoffStore._terminal_candidate(
        {first["candidate_id"]: first, second["candidate_id"]: second}, first
    )
    assert blocked is None
    assert error is not None


def test_coordinator_can_attribute_one_validation_failure_with_evidence(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized_batched(git_repo)
    source = {"kind": "codex", "thread_id": "developer"}
    coordinator = {"kind": "codex", "thread_id": "coordinator"}
    candidate = publish(repo, host_origin=source)

    def fail_after_composition(
        _repo: GitRepo, store: CandidateBatchStore, batch: dict[str, object]
    ) -> dict[str, object]:
        store.update_batch(str(batch["id"]), status="composed")
        raise SoloAIError("test validation failure")

    monkeypatch.setattr(batch_module, "_resume", fail_after_composition)
    with pytest.raises(SoloAIError, match="test validation failure"):
        seal_batch(
            repo,
            candidate_ids=[candidate["candidate_id"]],
            coordinator=coordinator,
        )

    batch = CandidateBatchStore(repo).summary()["batches"][0]
    handoff = HostHandoffStore(repo).attribute_validation_failure(
        batch_id=batch["id"],
        candidate_id=candidate["candidate_id"],
        coordinator=coordinator,
        evidence="full log: exact candidate validation failed",
    )

    assert handoff["failure_kind"] == "validation_failed"
    assert handoff["attribution"]["kind"] == "coordinator_validation_failure"
    assert (
        CandidateBatchStore(repo).candidate(candidate["candidate_id"])[
            "repair_eligible"
        ]
        is True
    )
