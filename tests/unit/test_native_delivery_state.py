from __future__ import annotations

from pathlib import Path

import pytest

from solo_ai.repo import GitRepo
from solo_ai.state import LEGACY_STATE_SCHEMA, STATE_SCHEMA, StateStore
from solo_ai.util import SoloAIError, atomic_write_json


def _native_store(root: Path) -> StateStore:
    store = StateStore(GitRepo(root))
    state = store._empty()
    state["schema_version"] = STATE_SCHEMA
    state["slots"]["01"] = {
        "id": "01",
        "status": "active",
        "task_id": "task-one",
        "generation": 1,
    }
    state["tasks"]["task-one"] = {
        "id": "task-one",
        "status": "active",
        "slot_id": "01",
        "slot_generation": 1,
        "branch": "codex/slot-01",
        "base_ref": "main",
        "candidate_head": "source-head",
        "lease": "old-lease",
        "native_delivery": {
            "schema_version": 1,
            "ready_head": None,
            "batch_id": None,
            "attempts": [],
            "delivery": None,
        },
    }
    atomic_write_json(store.path, state)
    return store


def test_legacy_state_read_does_not_enable_native_delivery(git_repo: Path) -> None:
    store = StateStore(GitRepo(git_repo))
    legacy = store._empty()
    assert legacy["schema_version"] == LEGACY_STATE_SCHEMA
    atomic_write_json(store.path, legacy)

    store.mutate(lambda state: state.setdefault("legacy_read", True))

    assert store.read()["schema_version"] == LEGACY_STATE_SCHEMA
    with pytest.raises(SoloAIError, match="has not been enabled"):
        store.mark_native_ready("missing", head="source-head")


def test_native_ready_and_finish_keep_slot_until_exact_delivery(
    git_repo: Path,
) -> None:
    store = _native_store(git_repo)
    ready = store.mark_native_ready("task-one", head="source-head")
    assert ready["native_delivery"]["ready_head"] == "source-head"
    assert store.read()["slots"]["01"]["task_id"] == "task-one"

    with pytest.raises(SoloAIError, match="without a managed withdrawal"):
        store.mark_native_ready("task-one", head="moved-head")
    waiting = store.mark_native_waiting("task-one", head="source-head")
    assert waiting["status"] == "waiting-integration"
    assert store.read()["slots"]["01"]["task_id"] == "task-one"

    batch = {
        "id": "batch-one",
        "base_ref": "main",
        "base_before": "base-head",
        "status": "sealed",
        "integration_head": "merge-head",
        "tasks": [
            {
                "task_id": "task-one",
                "slot_generation": 1,
                "branch": "codex/slot-01",
                "ready_head": "source-head",
            }
        ],
    }
    store.seal_native_batch(batch)
    assert store.read()["slots"]["01"]["status"] == "integrating"
    with pytest.raises(SoloAIError, match="changed status"):
        store.withdraw_native_ready("task-one", reason="repair")

    store.update_batch("batch-one", status="validated")
    store.mark_native_promoted("batch-one", integration_head="merge-head")
    assert store.read()["slots"]["01"]["status"] == "delivered-pending-release"
    receipt = {"result": "passed", "head": "merge-head"}
    delivered = store.complete_native_delivery(
        "task-one",
        batch_id="batch-one",
        integration_head="merge-head",
        release_receipt=receipt,
    )
    assert delivered["status"] == "finished"
    assert store.read()["slots"]["01"]["task_id"] is None
    assert store.read()["slots"]["01"]["released_task_id"] == "task-one"
    assert store.native_batch("batch-one")["status"] == "completed"
    assert (
        store.complete_native_delivery(
            "task-one",
            batch_id="batch-one",
            integration_head="merge-head",
            release_receipt=receipt,
        )
        == delivered
    )
    with pytest.raises(SoloAIError, match="changed status"):
        store.mark_native_ready("task-one", head="source-head")


def test_native_withdraw_keeps_old_head_and_rejects_sealed_repair(
    git_repo: Path,
) -> None:
    store = _native_store(git_repo)
    store.mark_native_ready("task-one", head="source-head")
    store.mark_native_waiting("task-one", head="source-head")
    withdrawn = store.withdraw_native_ready("task-one", reason="fix failing check")
    assert withdrawn["status"] == "active"
    assert withdrawn["native_delivery"]["attempts"][0]["ready_head"] == "source-head"
    assert store.read()["slots"]["01"]["task_id"] == "task-one"
    with pytest.raises(SoloAIError, match="Task head changed"):
        store.mark_native_ready("task-one", head="new-head")


def test_native_batch_retry_after_partial_release_preserves_receipts(
    git_repo: Path,
) -> None:
    store = _native_store(git_repo)
    store.mark_native_ready("task-one", head="source-head")
    store.mark_native_waiting("task-one", head="source-head")
    batch = {
        "id": "batch-one",
        "base_ref": "main",
        "base_head": "base-head",
        "status": "sealed",
        "integration_head": "merge-head",
        "tasks": [
            {
                "task_id": "task-one",
                "slot_generation": 1,
                "branch": "codex/slot-01",
                "ready_head": "source-head",
            }
        ],
    }
    store.seal_native_batch(batch)
    store.update_batch("batch-one", status="validated")
    assert store.seal_native_batch(batch)["status"] == "validated"
    store.mark_native_promoted("batch-one", integration_head="merge-head")
    store.complete_native_delivery(
        "task-one",
        batch_id="batch-one",
        integration_head="merge-head",
        release_receipt={"result": "passed"},
    )
    assert (
        store.mark_native_promoted("batch-one", integration_head="merge-head")["status"]
        == "completed"
    )
    assert store.read()["slots"]["01"]["task_id"] is None
