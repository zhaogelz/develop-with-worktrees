from __future__ import annotations

from pathlib import Path

import pytest

from conftest import git
from solo_ai.config import CommandSpec, load_repo_config, load_verification_config
from solo_ai.lifecycle import (
    approve,
    commit_task,
    finish,
    initialize,
    prepare_merge_source,
    start,
)
from solo_ai.native_batches import run_native_batch, seal_native_batch
from solo_ai.repo import GitRepo
from solo_ai.state import STATE_SCHEMA, StateStore
from solo_ai.util import SoloAIError


def _divergent_repo(path: Path) -> tuple[GitRepo, StateStore, str, str, str]:
    repo = GitRepo(path)
    initialize(
        repo,
        slots=1,
        commands=[CommandSpec(("git", "diff", "--check", "main...HEAD"))],
        accept=True,
        accept_static_only=False,
    )
    store = StateStore(repo)
    store.mutate(lambda state: state.update(schema_version=STATE_SCHEMA))
    store.ensure_slots(load_repo_config(repo))

    git(path, "switch", "-c", "remote-side")
    (path / "remote.txt").write_text("remote\n", encoding="utf-8")
    git(path, "add", "remote.txt")
    git(path, "commit", "-m", "test: remote side")
    source = repo.head(path)
    git(path, "switch", "main")
    git(path, "switch", "-c", "other-side")
    (path / "other.txt").write_text("other\n", encoding="utf-8")
    git(path, "add", "other.txt")
    git(path, "commit", "-m", "test: other side")
    other_source = repo.head(path)
    git(path, "switch", "main")
    (path / "local.txt").write_text("local\n", encoding="utf-8")
    git(path, "add", "local.txt")
    git(path, "commit", "-m", "test: local side")
    return repo, store, repo.head(path), source, other_source


def test_exact_local_merge_source_keeps_both_histories(git_repo: Path) -> None:
    repo, _store, base, source, _other_source = _divergent_repo(git_repo)

    task = start(repo, name="preserve remote source")
    worktree = Path(task["worktree"])
    with pytest.raises(SoloAIError, match="full lowercase commit SHA"):
        prepare_merge_source(
            repo, task_id=task["id"], lease=task["lease"], source_head=source[:8]
        )
    prepared = prepare_merge_source(
        repo, task_id=task["id"], lease=task["lease"], source_head=source
    )
    assert prepared["merge_source_status"] == "prepared"
    assert prepared["merge_source_preparation"] == {
        "task_head": base,
        "base_head": base,
        "source_head": source,
    }
    repeated = prepare_merge_source(
        repo, task_id=task["id"], lease=task["lease"], source_head=source
    )
    assert repeated["merge_source_status"] == "prepared"
    assert repo.git(["rev-parse", "MERGE_HEAD"], cwd=worktree).stdout.strip() == source

    committed = commit_task(
        repo,
        task_id=task["id"],
        lease=task["lease"],
        message="test: merge exact remote source",
        paths=["remote.txt"],
    )
    merged = committed["candidate_head"]
    assert committed["merge_source_preparation"] is None
    assert repo.git(["rev-list", "--parents", "-n", "1", merged]).stdout.strip().split() == [
        merged,
        base,
        source,
    ]
    finish(repo, task_id=task["id"], lease=task["lease"])
    batch = seal_native_batch(
        repo, task_ids=[task["id"]], cause="user", reason="deliver merge"
    )
    approve(repo, load_verification_config(repo))
    delivered = run_native_batch(repo, batch_id=batch["id"])
    assert delivered["status"] == "completed"
    assert repo.is_ancestor(source, repo.head(git_repo))
    assert repo.is_ancestor(merged, repo.head(git_repo))


def test_merge_source_rejects_unowned_merge_dirty_tree_and_changed_source(
    git_repo: Path,
) -> None:
    repo, store, _base, source, other_source = _divergent_repo(git_repo)
    task = start(repo, name="reject unowned merge")
    worktree = Path(task["worktree"])
    dirty = worktree / "unknown.txt"
    dirty.write_text("keep\n", encoding="utf-8")
    with pytest.raises(SoloAIError, match="Unowned merge or dirty"):
        prepare_merge_source(
            repo, task_id=task["id"], lease=task["lease"], source_head=source
        )
    assert store.task(task["id"]).get("merge_source_preparation") is None
    dirty.unlink()

    git(worktree, "merge", "--no-ff", "--no-commit", source)
    with pytest.raises(SoloAIError, match="Unowned merge or dirty"):
        prepare_merge_source(
            repo, task_id=task["id"], lease=task["lease"], source_head=source
        )
    assert store.task(task["id"]).get("merge_source_preparation") is None
    git(worktree, "merge", "--abort")

    prepare_merge_source(
        repo, task_id=task["id"], lease=task["lease"], source_head=source
    )
    with pytest.raises(SoloAIError, match="identity changed"):
        prepare_merge_source(
            repo,
            task_id=task["id"],
            lease=task["lease"],
            source_head=other_source,
        )
    git(worktree, "merge", "--abort")
    git(worktree, "merge", "--no-ff", "--no-commit", other_source)
    with pytest.raises(SoloAIError, match="MERGE_HEAD changed"):
        prepare_merge_source(
            repo, task_id=task["id"], lease=task["lease"], source_head=source
        )


def test_merge_source_rejects_moved_target_and_slot_generation(git_repo: Path) -> None:
    repo, store, _base, source, _other_source = _divergent_repo(git_repo)
    task = start(repo, name="freeze merge target")
    (git_repo / "later.txt").write_text("later\n", encoding="utf-8")
    git(git_repo, "add", "later.txt")
    git(git_repo, "commit", "-m", "test: move target")
    with pytest.raises(SoloAIError, match="target base moved"):
        prepare_merge_source(
            repo, task_id=task["id"], lease=task["lease"], source_head=source
        )
    assert store.task(task["id"]).get("merge_source_preparation") is None

    store.mutate(
        lambda state: state["slots"][task["slot_id"]].update(
            generation=task["slot_generation"] + 1
        )
    )
    with pytest.raises(SoloAIError, match="exact slot generation"):
        prepare_merge_source(
            repo, task_id=task["id"], lease=task["lease"], source_head=source
        )


def test_merge_source_conflict_retry_keeps_exact_parents(git_repo: Path) -> None:
    repo, _store, _base, _source, _other_source = _divergent_repo(git_repo)
    git(git_repo, "switch", "remote-side")
    (git_repo / "conflict.txt").write_text("remote\n", encoding="utf-8")
    git(git_repo, "add", "conflict.txt")
    git(git_repo, "commit", "-m", "test: remote conflict")
    source = repo.head(git_repo)
    git(git_repo, "switch", "main")
    (git_repo / "conflict.txt").write_text("local\n", encoding="utf-8")
    git(git_repo, "add", "conflict.txt")
    git(git_repo, "commit", "-m", "test: local conflict")
    base = repo.head(git_repo)

    task = start(repo, name="resolve exact source conflict")
    worktree = Path(task["worktree"])
    first = prepare_merge_source(
        repo, task_id=task["id"], lease=task["lease"], source_head=source
    )
    assert first["merge_source_status"] == "conflicted"
    repeated = prepare_merge_source(
        repo, task_id=task["id"], lease=task["lease"], source_head=source
    )
    assert repeated["merge_source_status"] == "conflicted"
    (worktree / "conflict.txt").write_text("local\nremote\n", encoding="utf-8")
    committed = commit_task(
        repo,
        task_id=task["id"],
        lease=task["lease"],
        message="test: keep both conflicting values",
        paths=["remote.txt", "conflict.txt"],
    )
    merged = committed["candidate_head"]
    assert repo.git(["rev-list", "--parents", "-n", "1", merged]).stdout.strip().split() == [
        merged,
        base,
        source,
    ]
