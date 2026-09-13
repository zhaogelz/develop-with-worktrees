from __future__ import annotations

from pathlib import Path

import pytest
from conftest import git
from solo_ai import candidate_batches as batch_module
from solo_ai.candidate_batches import CandidateBatchStore, seal_batch
from solo_ai.config import CommandSpec, load_verification_config
from solo_ai.host_handoffs import HostHandoffStore
from solo_ai.lifecycle import approve, commit_task, finish, initialize, ready, start
from solo_ai.repo import GitRepo
from solo_ai.util import SoloAIError

VERIFY = CommandSpec(("git", "diff", "--check", "main...HEAD"))


def initialized_batched(path: Path) -> GitRepo:
    repo = GitRepo(path)
    initialize(repo, slots=3, commands=[VERIFY], accept=True, accept_static_only=False)
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
    git_repo: Path,
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
    assert successor_dispatch["message"] == first_dispatch["message"]

    taken_repair = handoffs.take_over(
        request_id=request["id"], actor=successor, reason="source task is unavailable"
    )
    assert taken_repair["assignee"] == successor
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

    prepared = handoffs.prepare(request_id=request["id"], actor=successor)
    replayed = handoffs.prepare(request_id=request["id"], actor=successor)
    assert (
        prepared["request"]["repair_task_id"] == replayed["request"]["repair_task_id"]
    )
    repair_task = prepared["request"]["repair_task_id"]
    assert repair_task
    assert (
        CandidateBatchStore(repo).candidate(candidate["candidate_id"])[
            "repair_eligible"
        ]
        is True
    )
    from solo_ai.state import StateStore

    assert StateStore(repo).task(repair_task)["host_origin"] == successor


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
