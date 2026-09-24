from __future__ import annotations

from pathlib import Path

import pytest
from conftest import git
from solo_ai import native_batches
from solo_ai.candidate_batches import CandidateBatchStore
from solo_ai.config import CommandSpec, load_repo_config, load_verification_config
from solo_ai.lifecycle import approve, commit_task, finish, initialize, ready, start
from solo_ai.native_batches import run_native_batch, seal_native_batch
from solo_ai.repo import GitRepo
from solo_ai.state import STATE_SCHEMA, StateStore
from solo_ai.util import ActionableSoloAIError, SoloAIError


def _native_repo(path: Path, *, slots: int = 1) -> tuple[GitRepo, StateStore]:
    repo = GitRepo(path)
    initialize(
        repo,
        slots=slots,
        commands=[CommandSpec(("git", "diff", "--check", "main...HEAD"))],
        accept=True,
        accept_static_only=False,
    )
    store = StateStore(repo)
    store.mutate(lambda state: state.update(schema_version=STATE_SCHEMA))
    store.ensure_slots(load_repo_config(repo))
    return repo, store


def test_native_fixed_slot_waits_for_real_delivery_then_reuses_branch(
    git_repo: Path,
) -> None:
    repo, store = _native_repo(git_repo)
    first = start(repo, name="first native task")
    worktree = Path(first["worktree"])
    branch = first["branch"]
    assert branch == "codex/slot-01"
    assert repo.branch(worktree) == branch
    assert repo.head(worktree) == first["base_head"]

    (worktree / "feature.txt").write_text("first\n", encoding="utf-8")
    committed = commit_task(
        repo,
        task_id=first["id"],
        lease=first["lease"],
        message="test: native source commit",
        paths=["feature.txt"],
    )
    source_head = committed["candidate_head"]
    assert ready(repo, task_id=first["id"], lease=first["lease"])["status"] == "ready"
    waiting = finish(repo, task_id=first["id"], lease=first["lease"])
    assert waiting["status"] == "waiting-integration"
    assert waiting["delivered"] is False
    assert store.read()["slots"]["01"]["task_id"] == first["id"]
    assert CandidateBatchStore(repo).read()["candidates"] == {}
    with pytest.raises(SoloAIError, match="All managed worktree slots are busy"):
        start(repo, name="blocked successor")

    base_before = repo.head(git_repo)
    git(git_repo, "merge", "--no-ff", source_head, "-m", "test: deliver source")
    delivered_head = repo.head(git_repo)
    batch = {
        "id": "native-test-batch",
        "base_ref": "main",
        "base_head": base_before,
        "status": "sealed",
        "integration_head": delivered_head,
        "tasks": [
            {
                "task_id": first["id"],
                "slot_generation": first["slot_generation"],
                "branch": branch,
                "ready_head": source_head,
            }
        ],
    }
    store.seal_native_batch(batch)
    store.update_batch(batch["id"], status="validated")
    store.mark_native_promoted(batch["id"], integration_head=delivered_head)
    store.complete_native_delivery(
        first["id"],
        batch_id=batch["id"],
        integration_head=delivered_head,
        release_receipt={"result": "passed", "head": delivered_head},
    )

    second = start(repo, name="second native task")
    assert second["id"] != first["id"]
    assert second["slot_generation"] == first["slot_generation"] + 1
    assert second["worktree"] == first["worktree"]
    assert second["branch"] == branch
    assert repo.head(worktree) == delivered_head
    assert CandidateBatchStore(repo).read()["candidates"] == {}


def test_native_batch_keeps_source_in_real_merge_history(git_repo: Path) -> None:
    repo, store = _native_repo(git_repo)
    task = start(repo, name="merge original task head")
    worktree = Path(task["worktree"])
    (worktree / "feature.txt").write_text("delivered\n", encoding="utf-8")
    source = commit_task(
        repo,
        task_id=task["id"],
        lease=task["lease"],
        message="test: preserve native source",
        paths=["feature.txt"],
    )["candidate_head"]
    finish(repo, task_id=task["id"], lease=task["lease"])
    batch = seal_native_batch(
        repo,
        task_ids=[task["id"]],
        cause="round-complete",
        reason="the single task round is complete",
    )
    approve(repo, load_verification_config(repo))

    completed = run_native_batch(repo, batch_id=batch["id"])

    assert completed["status"] == "completed"
    assert completed["validation_outcome"] == "passed"
    assert repo.head(git_repo) == completed["integration_head"]
    assert repo.is_ancestor(source, repo.head(git_repo))
    merge = completed["merge_records"][0]
    assert merge["source_head"] == source
    assert merge["previous_head"] == batch["base_before"]
    assert repo.git(
        ["rev-list", "--parents", "-n", "1", merge["merge_head"]]
    ).stdout.strip().split() == [
        merge["merge_head"],
        batch["base_before"],
        source,
    ]
    assert store.read()["slots"]["01"]["status"] == "idle"
    assert CandidateBatchStore(repo).read()["candidates"] == {}


def test_native_finish_explicit_tail_delivers_without_candidate(git_repo: Path) -> None:
    repo, store = _native_repo(git_repo)
    task = start(repo, name="finish a native tail")
    worktree = Path(task["worktree"])
    (worktree / "tail.txt").write_text("ready\n", encoding="utf-8")
    source = commit_task(
        repo,
        task_id=task["id"],
        lease=task["lease"],
        message="test: native tail",
        paths=["tail.txt"],
    )["candidate_head"]
    approve(repo, load_verification_config(repo))

    result = finish(
        repo,
        task_id=task["id"],
        lease=task["lease"],
        cause="round-complete",
        reason="the development round has ended",
    )

    assert result["outcome"] == "delivered"
    assert result["delivered"] is True
    assert repo.is_ancestor(source, repo.head(git_repo))
    assert store.read()["slots"]["01"]["status"] == "idle"
    assert CandidateBatchStore(repo).read()["candidates"] == {}


def test_native_serial_batches_freeze_the_promoted_main_as_next_base(
    git_repo: Path,
) -> None:
    repo, store = _native_repo(git_repo, slots=2)
    approve(repo, load_verification_config(repo))
    first = start(repo, name="first sibling")
    second = start(repo, name="second sibling")
    sources = []
    for task, filename in ((first, "one.txt"), (second, "two.txt")):
        (Path(task["worktree"]) / filename).write_text(filename, encoding="utf-8")
        sources.append(
            commit_task(
                repo,
                task_id=task["id"],
                lease=task["lease"],
                message=f"test: {filename}",
                paths=[filename],
            )["candidate_head"]
        )
    finish(repo, task_id=first["id"], lease=first["lease"])
    second_result = finish(
        repo,
        task_id=second["id"],
        lease=second["lease"],
        cause="round-complete",
        reason="both development producers have finished",
    )
    assert second_result["delivered"] is True
    first_batch = store.native_batch(second_result["batch_id"])
    assert first_batch["status"] == "completed"
    assert len(first_batch["tasks"]) == 2
    assert all(repo.is_ancestor(source, repo.head(git_repo)) for source in sources)

    next_task = start(repo, name="next batch producer")
    (Path(next_task["worktree"]) / "next.txt").write_text("next", encoding="utf-8")
    next_source = commit_task(
        repo,
        task_id=next_task["id"],
        lease=next_task["lease"],
        message="test: next batch source",
        paths=["next.txt"],
    )["candidate_head"]
    next_result = finish(
        repo,
        task_id=next_task["id"],
        lease=next_task["lease"],
        cause="round-complete",
        reason="the next development round has finished",
    )
    second_batch = store.native_batch(next_result["batch_id"])
    assert second_batch["base_before"] == first_batch["integration_head"]
    assert second_batch["status"] == "completed"
    assert repo.is_ancestor(next_source, repo.head(git_repo))


def test_native_target_has_only_one_active_batch(git_repo: Path) -> None:
    repo, _store = _native_repo(git_repo, slots=2)
    tasks = [start(repo, name=f"sibling {index}") for index in (1, 2)]
    for index, task in enumerate(tasks, start=1):
        filename = f"sibling-{index}.txt"
        (Path(task["worktree"]) / filename).write_text(filename, encoding="utf-8")
        commit_task(
            repo,
            task_id=task["id"],
            lease=task["lease"],
            message=f"test: {filename}",
            paths=[filename],
        )
        finish(repo, task_id=task["id"], lease=task["lease"])

    first = seal_native_batch(
        repo,
        task_ids=[tasks[0]["id"]],
        cause="user",
        reason="integrate the first source now",
    )
    assert first["status"] == "sealed"
    with pytest.raises(SoloAIError, match="Another batch owns this target"):
        seal_native_batch(
            repo,
            task_ids=[tasks[1]["id"]],
            cause="user",
            reason="integrate another source now",
        )


def test_native_promoted_release_failure_retries_only_cleanup(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, store = _native_repo(git_repo)
    task = start(repo, name="release after promotion")
    worktree = Path(task["worktree"])
    (worktree / "delivery.txt").write_text("delivered\n", encoding="utf-8")
    commit_task(
        repo,
        task_id=task["id"],
        lease=task["lease"],
        message="test: retain promoted result",
        paths=["delivery.txt"],
    )
    finish(repo, task_id=task["id"], lease=task["lease"])
    batch = seal_native_batch(
        repo,
        task_ids=[task["id"]],
        cause="round-complete",
        reason="the delivery round has ended",
    )
    approve(repo, load_verification_config(repo))
    original_release = native_batches._release
    unknown = worktree / "unknown.txt"

    def block_first_release(*args: object, **kwargs: object) -> dict[str, object]:
        unknown.write_text("preserve me", encoding="utf-8")
        return original_release(*args, **kwargs)

    monkeypatch.setattr(native_batches, "_release", block_first_release)
    with pytest.raises(ActionableSoloAIError, match="preserve it"):
        run_native_batch(repo, batch_id=batch["id"])
    promoted = store.native_batch(batch["id"])
    assert promoted["status"] == "promoted"
    assert store.task(task["id"])["status"] == "delivered-pending-release"
    promoted_head = repo.head(git_repo)
    attempt = promoted["validation_attempt"]
    proof = promoted["proof"]

    monkeypatch.setattr(native_batches, "_release", original_release)
    unknown.unlink()
    completed = run_native_batch(repo, batch_id=batch["id"])
    assert completed["status"] == "completed"
    assert completed["validation_attempt"] == attempt
    assert completed["proof"] == proof
    assert repo.head(git_repo) == promoted_head
