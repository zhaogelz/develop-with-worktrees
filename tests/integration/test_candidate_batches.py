from __future__ import annotations

from pathlib import Path

import pytest
from conftest import git
from solo_ai import candidate_batches as batch_module
from solo_ai.candidate_batches import (
    CandidateBatchStore,
    prepare_candidate_repair,
    seal_batch,
)
from solo_ai.config import CommandSpec, load_verification_config
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
from solo_ai.state import StateStore
from solo_ai.task_context import anchor_path
from solo_ai.util import SoloAIError

VERIFY = CommandSpec(("git", "diff", "--check", "main...HEAD"))


def initialized_batched(path: Path) -> GitRepo:
    repo = GitRepo(path)
    initialize(repo, slots=3, commands=[VERIFY], accept=True, accept_static_only=False)
    config = path / ".solo-ai" / "config.toml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            'integration = { mode = "direct", batch_size = 5, candidate_capacity = 10 }',
            'integration = { mode = "batched", batch_size = 5, candidate_capacity = 10 }',
        ),
        encoding="utf-8",
    )
    git(path, "add", ".solo-ai/config.toml")
    git(path, "commit", "-m", "test: enable candidate batches")
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
    repo = initialized_batched(git_repo)
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
    repo = initialized_batched(git_repo)
    task = start(repo, name="anchor required")
    anchor_path(repo, task["id"]).unlink()

    with pytest.raises(SoloAIError, match="anchor is missing"):
        ready(repo, task_id=task["id"], lease=task["lease"])


def test_finish_publishes_then_explicit_seal_integrates_exact_candidates(
    git_repo: Path,
) -> None:
    repo = initialized_batched(git_repo)
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


def test_failed_combined_validation_preserves_base_and_generation_is_not_rerun(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized_batched(git_repo)
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
