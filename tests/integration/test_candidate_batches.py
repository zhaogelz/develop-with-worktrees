from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from conftest import git
from solo_ai import candidate_batches as batch_module
from solo_ai.candidate_batches import (
    CandidateBatchStore,
    prepare_candidate_repair,
    seal_batch,
)
from solo_ai.config import CommandSpec, load_repo_config, load_verification_config
from solo_ai.lifecycle import (
    adopt_task_anchor,
    approve,
    commit_task,
    finish,
    initialize,
    ready,
    recover,
    start,
)
from solo_ai.repo import GitRepo
from solo_ai.state import StateStore
from solo_ai.task_context import anchor_path
from solo_ai.util import SoloAIError
from solo_ai.util import atomic_write_json, read_json

VERIFY = CommandSpec(("git", "diff", "--check", "main...HEAD"))


def initialized_batched(path: Path, *, auto_full: bool = True) -> GitRepo:
    repo = GitRepo(path)
    initialize(repo, slots=3, commands=[VERIFY], accept=True, accept_static_only=False)
    if not auto_full:
        config = path / ".solo-ai" / "config.toml"
        config.write_text(
            config.read_text(encoding="utf-8").replace(
                'seal_policy = "auto_full"', 'seal_policy = "explicit"'
            ),
            encoding="utf-8",
        )
        git(path, "add", ".solo-ai/config.toml")
        git(path, "commit", "-m", "test: require explicit candidate batches")
    approve(repo, load_verification_config(repo))
    return repo


def publish(repo: GitRepo, *, name: str, relative: str) -> dict[str, str]:
    task = start(repo, name=name)
    worktree = Path(task["worktree"])
    (worktree / relative).write_text(f"{name}\n", encoding="utf-8")
    commit_task(
        repo,
        task_id=task["id"],
        lease=task["lease"],
        message=f"test: {name}",
        paths=[relative],
    )
    ready(repo, task_id=task["id"], lease=task["lease"])
    return finish(repo, task_id=task["id"], lease=task["lease"])


def test_start_request_is_idempotent_and_anchor_is_first_class(git_repo: Path) -> None:
    repo = initialized_batched(git_repo, auto_full=False)
    first = start(repo, name="same work", request_id="request-123")
    second = start(repo, name="same work", request_id="request-123")

    assert second["id"] == first["id"]
    assert second["worktree"] == first["worktree"]
    assert second["request_reused"] is True
    assert Path(first["anchor_path"]) == anchor_path(repo, first["id"])
    assert Path(first["anchor_path"]).is_file()
    active = [
        item
        for item in StateStore(repo).read()["tasks"].values()
        if item["status"] not in {"finished", "abandoned", "candidate-published"}
    ]
    assert len(active) == 1


def test_ready_requires_the_managed_task_anchor(git_repo: Path) -> None:
    repo = initialized_batched(git_repo, auto_full=False)
    task = start(repo, name="anchor required")
    anchor_path(repo, task["id"]).unlink()

    with pytest.raises(SoloAIError, match="anchor is missing"):
        ready(repo, task_id=task["id"], lease=task["lease"])

    adopted = adopt_task_anchor(
        repo,
        task_id=task["id"],
        objective="恢复旧任务目标",
        target="旧任务候选",
        scope="只恢复已确认范围",
        acceptance="Ready 验证通过",
        confirm=task["id"],
    )
    assert Path(adopted["anchor_path"]).is_file()


def test_preupgrade_task_gets_explicit_policy_snapshot_before_finish(
    git_repo: Path,
) -> None:
    repo = initialized_batched(git_repo)
    task = start(repo, name="legacy active task")
    worktree = Path(task["worktree"])
    (worktree / "legacy-active.txt").write_text("legacy\n", encoding="utf-8")
    commit_task(
        repo,
        task_id=task["id"],
        lease=task["lease"],
        message="test: legacy active task",
        paths=["legacy-active.txt"],
    )
    state_path = repo.local_dir / "state.json"
    legacy = read_json(state_path, {})
    legacy["schema_version"] = 5
    legacy["tasks"][task["id"]].pop("integration_policy", None)
    atomic_write_json(state_path, legacy)

    ready(repo, task_id=task["id"], lease=task["lease"])
    result = finish(repo, task_id=task["id"], lease=task["lease"])

    assert result["outcome"] == "candidate_published"
    candidate = CandidateBatchStore(repo).candidate_for_task(task["id"])
    assert candidate is not None
    assert candidate["integration_policy"]["seal_policy"] == "explicit"
    assert CandidateBatchStore(repo).summary()["batches"] == []


def test_finish_publishes_then_explicit_seal_integrates_exact_candidates(
    git_repo: Path,
) -> None:
    repo = initialized_batched(git_repo, auto_full=False)
    base_before = repo.head(git_repo)
    first = publish(repo, name="candidate a", relative="a.txt")
    second = publish(repo, name="candidate b", relative="b.txt")

    assert first["outcome"] == "candidate_published"
    assert second["outcome"] == "candidate_published"
    assert repo.head(git_repo) == base_before
    assert not (git_repo / "a.txt").exists()
    assert Path(first["anchor_path"]).is_file()

    result = seal_batch(
        repo, candidate_ids=[first["candidate_id"], second["candidate_id"]]
    )

    assert result["status"] == "completed"
    assert (git_repo / "a.txt").read_text(encoding="utf-8") == "candidate a\n"
    assert (git_repo / "b.txt").read_text(encoding="utf-8") == "candidate b\n"
    assert not Path(first["anchor_path"]).exists()
    assert not Path(second["anchor_path"]).exists()
    recovered = recover(repo, task_id=first["task_id"])
    assert recovered["status"] == "integrated"
    assert recovered["batch_id"] == result["id"]
    pool = CandidateBatchStore(repo).summary()
    assert {item["status"] for item in pool["candidates"]} == {"integrated"}


def test_four_candidates_wait_and_fifth_finish_auto_integrates_oldest_five(
    git_repo: Path,
) -> None:
    repo = initialized_batched(git_repo)
    base_before = repo.head(git_repo)
    published = [
        publish(repo, name=f"candidate {index}", relative=f"{index}.txt")
        for index in range(1, 5)
    ]

    assert all(item["outcome"] == "candidate_published" for item in published)
    assert repo.head(git_repo) == base_before
    assert CandidateBatchStore(repo).summary()["batches"] == []

    fifth = publish(repo, name="candidate 5", relative="5.txt")

    assert fifth["outcome"] == "batch_integrated"
    assert fifth["candidate_count"] == 5
    assert repo.head(git_repo) == fifth["integrated_head"]
    assert all((git_repo / f"{index}.txt").is_file() for index in range(1, 6))
    pool = CandidateBatchStore(repo).summary()
    assert [item["trigger"] for item in pool["batches"]] == ["auto_full"]
    assert {item["status"] for item in pool["candidates"]} == {"integrated"}


def test_enabling_auto_full_does_not_capture_legacy_explicit_candidates(
    git_repo: Path,
) -> None:
    repo = initialized_batched(git_repo, auto_full=False)
    legacy = [
        publish(repo, name=f"legacy {index}", relative=f"legacy-{index}.txt")
        for index in range(1, 5)
    ]
    config = git_repo / ".solo-ai" / "config.toml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            'seal_policy = "explicit"', 'seal_policy = "auto_full"'
        ),
        encoding="utf-8",
    )
    git(git_repo, "add", ".solo-ai/config.toml")
    git(git_repo, "commit", "-m", "test: activate automatic full batches")
    approve(repo, load_verification_config(repo))

    first_new = publish(repo, name="new 1", relative="new-1.txt")

    assert first_new["outcome"] == "candidate_published"
    assert CandidateBatchStore(repo).summary()["batches"] == []

    remaining_new = [
        publish(repo, name=f"new {index}", relative=f"new-{index}.txt")
        for index in range(2, 6)
    ]

    assert remaining_new[-1]["outcome"] == "batch_integrated"
    pool = CandidateBatchStore(repo).summary()
    integrated = {
        item["candidate_id"]
        for item in pool["candidates"]
        if item["status"] == "integrated"
    }
    assert integrated.isdisjoint({item["candidate_id"] for item in legacy})
    assert all(
        item["status"] == "pending"
        for item in pool["candidates"]
        if item["candidate_id"] in {legacy_item["candidate_id"] for legacy_item in legacy}
    )


def test_concurrent_fifth_and_sixth_publications_create_only_one_full_batch(
    git_repo: Path,
) -> None:
    repo = initialized_batched(git_repo)
    store = CandidateBatchStore(repo)
    head = repo.head(git_repo)
    policy = StateStore.integration_policy(load_repo_config(repo))

    def publish_raw(index: int) -> dict[str, object]:
        candidate_id = f"candidate-concurrent-{index}"
        return store.publish(
            {
                "candidate_id": candidate_id,
                "task_id": f"task-concurrent-{index}",
                "name": candidate_id,
                "ref": f"refs/dww/candidates/{candidate_id}",
                "head": head,
                "base_head": head,
                "base_ref": "main",
                "proof": f"proof-{index}",
                "integration_policy": policy,
            },
            capacity=10,
            batch_size=5,
            seal_policy="auto_full",
        )

    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(publish_raw, range(1, 7)))

    auto_batches = [item["auto_batch"] for item in results if item["auto_batch"]]
    summary = store.summary()
    assert len({item["id"] for item in auto_batches}) == 1
    assert len(summary["batches"]) == 1
    assert len(summary["batches"][0]["candidate_ids"]) == 5
    assert sum(item["status"] == "pending" for item in summary["candidates"]) == 1

    for index in range(7, 11):
        publish_raw(index)
    with pytest.raises(SoloAIError, match="pool is full"):
        publish_raw(11)
    full = store.summary()
    assert len(full["batches"]) == 2
    assert len(full["candidates"]) == 10


def test_failed_combined_validation_preserves_base_and_generation_is_not_rerun(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized_batched(git_repo, auto_full=False)
    candidate = publish(repo, name="candidate failure", relative="failure.txt")
    base_before = repo.head(git_repo)

    def fail_validation(*args: object, **kwargs: object) -> dict[str, object]:
        raise SoloAIError("combined validation failed")

    monkeypatch.setattr(batch_module, "validate", fail_validation)
    with pytest.raises(SoloAIError, match="combined validation failed"):
        seal_batch(repo, candidate_ids=[candidate["candidate_id"]])

    assert repo.head(git_repo) == base_before
    assert not (git_repo / "failure.txt").exists()
    failed = CandidateBatchStore(repo).summary()["batches"][0]
    assert failed["status"] == "failed"
    retained = CandidateBatchStore(repo).candidate_for_task(candidate["task_id"])
    assert retained is not None
    assert retained["status"] == "retained"
    assert failed["failure_kind"] == "validation_failed"
    with pytest.raises(SoloAIError, match="Automatic repair is limited"):
        prepare_candidate_repair(repo, candidate_id=candidate["candidate_id"])
    with pytest.raises(SoloAIError, match="will not be rerun automatically"):
        batch_module.recover_batch(repo, batch_id=failed["id"])


def test_composition_conflict_prepares_bounded_repair_and_replacement_candidate(
    git_repo: Path,
) -> None:
    repo = initialized_batched(git_repo)
    candidate = publish(repo, name="candidate conflict", relative="shared.txt")
    (git_repo / "shared.txt").write_text("main change\n", encoding="utf-8")
    git(git_repo, "add", "shared.txt")
    git(git_repo, "commit", "-m", "test: advance conflicting base")
    base_before = repo.head(git_repo)

    with pytest.raises(SoloAIError, match="conflicts with the sealed batch"):
        seal_batch(repo, candidate_ids=[candidate["candidate_id"]])

    source = CandidateBatchStore(repo).candidate(candidate["candidate_id"])
    assert source["status"] == "retained"
    assert source["last_failure_kind"] == "composition_conflict"
    assert source["repair_eligible"] is True

    repair = prepare_candidate_repair(
        repo, candidate_id=candidate["candidate_id"]
    )
    repair_worktree = Path(repair["worktree"])
    assert repair["outcome"] == "conflicted"
    assert repair["repair_attempt"] == 1
    assert repair["manual_notification_required"] is False
    assert repair["conflict_paths"] == ["shared.txt"]
    assert repo.head(git_repo) == base_before
    reused = prepare_candidate_repair(
        repo, candidate_id=candidate["candidate_id"]
    )
    assert reused["id"] == repair["id"]
    assert reused["request_reused"] is True
    assert reused["outcome"] == "conflicted"

    (repair_worktree / "shared.txt").write_text(
        "main change\ncandidate conflict\n", encoding="utf-8"
    )
    committed = commit_task(
        repo,
        task_id=repair["id"],
        lease=repair["lease"],
        message="test: resolve candidate conflict",
        paths=["shared.txt"],
    )
    assert committed["supersedes"] == candidate["candidate_id"]
    ready(repo, task_id=repair["id"], lease=repair["lease"])
    replacement = finish(repo, task_id=repair["id"], lease=repair["lease"])

    assert replacement["outcome"] == "candidate_published"
    pool = CandidateBatchStore(repo)
    assert pool.candidate(candidate["candidate_id"])["status"] == "superseded"
    assert pool.candidate(replacement["candidate_id"])["repair_attempt"] == 1

    integrated = seal_batch(repo, candidate_ids=[replacement["candidate_id"]])
    assert integrated["status"] == "completed"
    assert (git_repo / "shared.txt").read_text(encoding="utf-8") == (
        "main change\ncandidate conflict\n"
    )


def test_candidate_repair_stops_after_bounded_attempts(git_repo: Path) -> None:
    repo = initialized_batched(git_repo)
    candidate = publish(repo, name="bounded conflict", relative="bounded.txt")
    (git_repo / "bounded.txt").write_text("main\n", encoding="utf-8")
    git(git_repo, "add", "bounded.txt")
    git(git_repo, "commit", "-m", "test: create bounded conflict")
    with pytest.raises(SoloAIError, match="conflicts with the sealed batch"):
        seal_batch(repo, candidate_ids=[candidate["candidate_id"]])

    store = CandidateBatchStore(repo)

    def exhaust_attempts(value: dict[str, object]) -> None:
        candidates = value["candidates"]
        assert isinstance(candidates, dict)
        source = candidates[candidate["candidate_id"]]
        assert isinstance(source, dict)
        source["repair_attempt"] = batch_module.AUTOMATIC_REPAIR_LIMIT

    store.mutate(exhaust_attempts)
    with pytest.raises(SoloAIError, match="manual product or implementation review"):
        prepare_candidate_repair(repo, candidate_id=candidate["candidate_id"])
