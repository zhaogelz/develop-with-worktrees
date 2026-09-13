from __future__ import annotations

from pathlib import Path

from solo_ai.host_handoffs import HANDOFF_SCHEMA, HostHandoffStore
from solo_ai.repo import GitRepo
from solo_ai.util import atomic_write_json


SOURCE = {"kind": "codex", "thread_id": "developer"}
COORDINATOR = {"kind": "codex", "thread_id": "coordinator"}
SUCCESSOR = {"kind": "codex", "thread_id": "developer-replacement"}


def seed_handoff(repo: GitRepo, *, status: str) -> str:
    request_id = "repair-test-request"
    atomic_write_json(
        repo.local_dir / "candidate-batches.json",
        {
            "schema_version": 5,
            "candidates": {
                "candidate-1": {
                    "candidate_id": "candidate-1",
                    "task_id": "task-1",
                    "status": "retained",
                    "base_ref": "main",
                    "head": "a" * 40,
                    "host_origin": SOURCE,
                    "repair_attempt": 0,
                    "integration_policy": {"tail_policy": "explicit"},
                }
            },
            "batches": {
                "batch-1": {
                    "id": "batch-1",
                    "status": "failed",
                    "base_ref": "main",
                    "candidate_ids": ["candidate-1"],
                    "host_coordinator": COORDINATOR,
                    "host_coordinator_revision": 1,
                    "host_coordinator_transfers": [],
                }
            },
            "next_publication_sequence": 1,
        },
    )
    atomic_write_json(
        repo.local_dir / "host-handoffs.json",
        {
            "schema_version": HANDOFF_SCHEMA,
            "repair_requests": {
                request_id: {
                    "schema_version": 1,
                    "id": request_id,
                    "batch_id": "batch-1",
                    "candidate_id": "candidate-1",
                    "candidate_head": "a" * 40,
                    "origin": SOURCE,
                    "assignee": SOURCE,
                    "coordinator": COORDINATOR,
                    "coordinator_revision": 1,
                    "status": status,
                    "delivery_status": "dispatched",
                    "delivery_attempts": [],
                    "claim": {"actor": SOURCE, "at": "2026-09-13T00:00:00Z"}
                    if status == "claimed"
                    else None,
                    "repair_task_id": None,
                    "repair_result": None,
                    "takeovers": [],
                    "created_at": "2026-09-13T00:00:00Z",
                    "updated_at": "2026-09-13T00:00:00Z",
                }
            },
        },
    )
    return request_id


def test_claimed_handoff_projects_the_prepare_action(git_repo: Path) -> None:
    repo = GitRepo(git_repo)
    request_id = seed_handoff(repo, status="claimed")

    assert HostHandoffStore(repo).status()["actions"] == [
        {
            "kind": "prepare_repair",
            "request_id": request_id,
            "target": SOURCE,
        }
    ]


def test_same_repair_takeover_is_idempotent(git_repo: Path) -> None:
    repo = GitRepo(git_repo)
    request_id = seed_handoff(repo, status="pending")
    store = HostHandoffStore(repo)

    first = store.take_over(
        request_id=request_id, actor=SUCCESSOR, reason="source unavailable"
    )
    repeated = store.take_over(
        request_id=request_id, actor=SUCCESSOR, reason="source unavailable"
    )

    assert repeated == first
    assert len(repeated["takeovers"]) == 1
