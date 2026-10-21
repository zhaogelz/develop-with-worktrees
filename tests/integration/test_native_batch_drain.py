from __future__ import annotations

from pathlib import Path

import pytest
from solo_ai import native_batches
from solo_ai.config import load_verification_config
from solo_ai.lifecycle import approve, commit_task, finish, start
from solo_ai.util import SoloAIError
from test_native_lifecycle import _native_repo


def _prepared_tasks(git_repo: Path, count: int, *, spare: int = 0):
    repo, store = _native_repo(git_repo, slots=count + spare)
    approve(repo, load_verification_config(repo))
    tasks = [start(repo, name=f"parallel-{index}") for index in range(count)]
    for index, task in enumerate(tasks):
        name = f"branch-{index}.txt"
        (Path(task["worktree"]) / name).write_text(str(index), encoding="utf-8")
        commit_task(
            repo,
            task_id=task["id"],
            lease=task["lease"],
            message=f"test: independent branch {index}",
            paths=[name],
        )
    return repo, store, tasks


@pytest.mark.parametrize("tail", [True, False])
def test_finish_drains_existing_ready_branches_without_inventing_tail(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, tail: bool
) -> None:
    """同一调用处理三加二分支；没有尾批意图时只交付满批。"""
    repo, store, tasks = _prepared_tasks(git_repo, 5)
    with monkeypatch.context() as staged:
        staged.setattr(native_batches, "reconcile_native_batches", lambda *a, **k: None)
        for task in tasks[:-1]:
            finish(repo, task_id=task["id"], lease=task["lease"])
    validations = []
    original = native_batches.validate

    def observed(*args, **kwargs):
        validations.append(kwargs["task_id"])
        return original(*args, **kwargs)

    monkeypatch.setattr(native_batches, "validate", observed)
    result = finish(
        repo,
        task_id=tasks[-1]["id"],
        lease=tasks[-1]["lease"],
        **(
            {"cause": "round-complete", "reason": "all five producers finished"}
            if tail
            else {}
        ),
    )
    batches = [store.native_batch(batch_id) for batch_id in validations]
    assert len(validations) == (2 if tail else 1)
    assert [len(batch["tasks"]) for batch in batches] == ([3, 2] if tail else [3])
    assert all(batch["status"] == "completed" for batch in batches)
    assert result["delivered"] is tail
    assert sum(store.task(task["id"])["status"] == "finished" for task in tasks) == (
        5 if tail else 3
    )
    if tail:
        assert (
            result["batch_id"]
            == store.task(tasks[-1]["id"])["native_delivery"]["batch_id"]
        )
        assert batches[1]["base_before"] == batches[0]["integration_head"]
        for index in range(5):
            assert (git_repo / f"branch-{index}.txt").read_text(
                encoding="utf-8"
            ) == str(index)
        again = finish(repo, task_id=tasks[-1]["id"], lease=tasks[-1]["lease"])
        assert again["delivered"] is True
        assert len(validations) == 2
    else:
        assert result["waiting"]["reason"] == "tail_intent_required"


def test_finish_draining_stops_at_failed_batch_and_preserves_later_sources(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, store, tasks = _prepared_tasks(git_repo, 5)
    with monkeypatch.context() as staged:
        staged.setattr(native_batches, "reconcile_native_batches", lambda *a, **k: None)
        for task in tasks[:-1]:
            finish(repo, task_id=task["id"], lease=task["lease"])
    base = repo.head(git_repo)

    def fail(*args, **kwargs):
        raise SoloAIError("required check failed")

    monkeypatch.setattr(native_batches, "validate", fail)
    with pytest.raises(SoloAIError, match="required check failed"):
        finish(
            repo,
            task_id=tasks[-1]["id"],
            lease=tasks[-1]["lease"],
            cause="round-complete",
            reason="round finished",
        )
    assert repo.head(git_repo) == base
    assert len(store.read()["batches"]) == 1
    assert all(
        store.task(task["id"])["status"] == "waiting-integration" for task in tasks[3:]
    )


def test_finish_waiting_explains_active_producers(git_repo: Path) -> None:
    repo, store, tasks = _prepared_tasks(git_repo, 2)
    result = finish(
        repo,
        task_id=tasks[0]["id"],
        lease=tasks[0]["lease"],
        cause="round-complete",
        reason="finish when producers complete",
    )
    assert result["delivered"] is False
    assert result["waiting"]["reason"] == "active_producers"
    assert result["waiting"]["task_ids"] == [tasks[1]["id"]]
    assert store.read()["batches"] == {}


def test_finish_includes_existing_producers_completed_during_first_batch(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首批验证时其他已有分支完成，当前调用继续交付尾批并返回自身回执。"""
    repo, store, tasks = _prepared_tasks(git_repo, 5)
    for task in tasks[:2]:
        finish(repo, task_id=task["id"], lease=task["lease"])
    original = native_batches.validate
    validations = []

    def finish_followers(*args, **kwargs):
        validations.append(kwargs["task_id"])
        if len(validations) == 1:
            for index, task in enumerate(tasks[3:]):
                waiting = finish(
                    repo,
                    task_id=task["id"],
                    lease=task["lease"],
                    **(
                        {"cause": "round-complete", "reason": "all producers finished"}
                        if index
                        else {}
                    ),
                )
                assert waiting["waiting"]["reason"] == "prior_batch_pending"
        return original(*args, **kwargs)

    monkeypatch.setattr(native_batches, "validate", finish_followers)
    result = finish(repo, task_id=tasks[2]["id"], lease=tasks[2]["lease"])
    assert result["delivered"] is True
    assert result["batch_id"] == validations[0]
    assert len(validations) == 2
    assert all(store.task(task["id"])["status"] == "finished" for task in tasks)


def test_reconcile_keeps_explicit_round_cause_across_captured_batches(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, store, tasks = _prepared_tasks(git_repo, 5)
    with monkeypatch.context() as staged:
        staged.setattr(native_batches, "reconcile_native_batches", lambda *a, **k: None)
        for task in tasks:
            finish(repo, task_id=task["id"], lease=task["lease"])
    result = native_batches.reconcile_native_batches(
        repo, base_ref="main", cause="round-complete", reason="captured round completed"
    )
    assert len(result["completed_batches"]) == 2
    assert all(store.task(task["id"])["status"] == "finished" for task in tasks)


@pytest.mark.parametrize("resume", [False, True])
def test_second_batch_failure_reports_completed_batch_without_losing_sources(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    resume: bool,
) -> None:
    from solo_ai.util import ActionableSoloAIError

    repo, store, tasks = _prepared_tasks(git_repo, 5)
    with monkeypatch.context() as staged:
        staged.setattr(native_batches, "reconcile_native_batches", lambda *a, **k: None)
        for task in tasks:
            finish(
                repo,
                task_id=task["id"],
                lease=task["lease"],
                cause="round-complete",
                reason="round finished",
            )
    original = native_batches.validate
    calls = []

    def fail_second(*args, **kwargs):
        calls.append(kwargs["task_id"])
        if len(calls) == 2:
            raise (SoloAIError if resume else OSError)("second batch check failed")
        return original(*args, **kwargs)

    monkeypatch.setattr(native_batches, "validate", fail_second)
    with pytest.raises(
        ActionableSoloAIError, match="second batch check failed"
    ) as raised:
        if resume:
            native_batches.seal_native_batch(
                repo, task_ids=[task["id"] for task in tasks[:3]]
            )
            finish(repo, task_id=tasks[0]["id"], lease=tasks[0]["lease"])
        else:
            native_batches.reconcile_native_batches(
                repo, base_ref="main", cause="round-complete", reason="round completed"
            )
    assert raised.value.context["completed_batches"] == calls[:1]
    assert raised.value.context["pending_batch_id"] == calls[1]
    assert sum(store.task(task["id"])["status"] == "finished" for task in tasks) == 3
    assert store.native_batch(calls[0])["integration_head"] == repo.head(git_repo)


def test_concurrent_reconcile_selects_one_batch_without_false_failure(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    import threading

    repo, store, tasks = _prepared_tasks(git_repo, 3)
    with monkeypatch.context() as staged:
        staged.setattr(native_batches, "reconcile_native_batches", lambda *a, **k: None)
        for task in tasks:
            finish(repo, task_id=task["id"], lease=task["lease"])
    rendezvous = threading.Barrier(2)
    other_returned = threading.Event()
    validations = []
    original = native_batches.validate

    def observe(*args, **kwargs):
        validations.append(kwargs["task_id"])
        assert other_returned.wait(timeout=20)
        return original(*args, **kwargs)

    def deliver():
        rendezvous.wait(timeout=20)
        result = native_batches.reconcile_native_batches(repo, base_ref="main")
        if result is None:
            other_returned.set()
        return result

    monkeypatch.setattr(native_batches, "validate", observe)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: deliver(), range(2)))
    assert sum(result is None for result in results) == 1
    assert len(validations) == 1
    assert all(store.task(task["id"])["status"] == "finished" for task in tasks)


def test_drain_excludes_tasks_created_after_delivery_started(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, store, tasks = _prepared_tasks(git_repo, 3, spare=1)
    for task in tasks[:2]:
        finish(repo, task_id=task["id"], lease=task["lease"])
    original = native_batches.validate
    arrivals = []

    def new_arrival(*args, **kwargs):
        task = start(repo, name="later-round")
        (Path(task["worktree"]) / "later.txt").write_text("later", encoding="utf-8")
        commit_task(
            repo,
            task_id=task["id"],
            lease=task["lease"],
            message="test: later round",
            paths=["later.txt"],
        )
        waiting = finish(
            repo,
            task_id=task["id"],
            lease=task["lease"],
            cause="user",
            reason="deliver this later source",
        )
        assert waiting["waiting"]["reason"] == "prior_batch_pending"
        arrivals.append(task)
        return original(*args, **kwargs)

    monkeypatch.setattr(native_batches, "validate", new_arrival)
    result = finish(repo, task_id=tasks[2]["id"], lease=tasks[2]["lease"])
    assert result["delivered"] is True
    assert len(arrivals) == 1
    assert store.task(arrivals[0]["id"])["status"] == "waiting-integration"
    assert not (git_repo / "later.txt").exists()
