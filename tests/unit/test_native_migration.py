from __future__ import annotations

from pathlib import Path

import pytest

from solo_ai.candidate_batches import CandidateBatchStore
from solo_ai.config import render_repo_config, render_verification_config
from solo_ai.native_migration import enable_native_migration, preview_native_migration
from solo_ai.repo import GitRepo
from solo_ai.state import STATE_SCHEMA, StateStore
from solo_ai.util import SoloAIError, atomic_write_json


def _legacy_repo(root: Path) -> tuple[GitRepo, StateStore]:
    config = root / ".solo-ai"
    config.mkdir()
    (config / "config.toml").write_text(render_repo_config(), encoding="utf-8")
    (config / "verification.toml").write_text(
        render_verification_config([], static_only=True), encoding="utf-8"
    )
    repo = GitRepo(root)
    return repo, StateStore(repo)


def test_native_migration_preview_reports_active_legacy_task(git_repo: Path) -> None:
    repo, store = _legacy_repo(git_repo)
    state = store._empty()
    state["tasks"]["active-one"] = {"id": "active-one", "status": "active"}
    atomic_write_json(store.path, state)

    preview = preview_native_migration(repo, base_ref="main")

    assert preview["status"] == "blocked"
    assert {item["kind"] for item in preview["blockers"]} == {"active-legacy-task"}
    assert store.read()["schema_version"] == state["schema_version"]


def test_native_migration_preview_allows_empty_legacy_state(git_repo: Path) -> None:
    repo, store = _legacy_repo(git_repo)
    atomic_write_json(store.path, store._empty())

    preview = preview_native_migration(repo, base_ref="main")

    assert preview["status"] == "ready"
    assert preview["base_head"] == repo.head()
    assert preview["blockers"] == []


def test_native_migration_enables_detached_idle_slot_and_retries(
    git_repo: Path,
) -> None:
    repo, store = _legacy_repo(git_repo)
    slot_path = git_repo.parent / "migration-slot"
    repo.git(["worktree", "add", "--detach", str(slot_path), "main"])
    state = store._empty()
    state["slots"]["01"] = {
        "id": "01",
        "path": str(slot_path),
        "status": "idle",
        "task_id": None,
        "generation": 4,
    }
    atomic_write_json(store.path, state)
    preview = preview_native_migration(repo, base_ref="main")
    assert preview["status"] == "ready"
    branch = preview["slots"][0]["fixed_branch"]

    with pytest.raises(SoloAIError, match="--confirm"):
        enable_native_migration(repo, base_ref="main", confirm="main:stale")
    assert store.read()["schema_version"] != STATE_SCHEMA
    enabled = enable_native_migration(
        repo, base_ref="main", confirm=f"main:{preview['base_head']}"
    )

    assert enabled["status"] == "enabled"
    assert repo.branch(slot_path) == branch
    assert repo.head(slot_path) == preview["base_head"]
    assert store.read()["slots"]["01"]["fixed_branch"] == branch
    assert store.read()["slots"]["01"]["generation"] == 4
    assert (
        enable_native_migration(
            repo, base_ref="main", confirm=f"main:{preview['base_head']}"
        )["migration"]
        == enabled["migration"]
    )


def test_native_migration_keeps_active_task_blocked(git_repo: Path) -> None:
    repo, store = _legacy_repo(git_repo)
    state = store._empty()
    state["tasks"]["active-one"] = {"id": "active-one", "status": "active"}
    atomic_write_json(store.path, state)
    head = repo.head()

    result = enable_native_migration(repo, base_ref="main", confirm=f"main:{head}")

    assert result["status"] == "blocked"
    assert {item["kind"] for item in result["blockers"]} == {"active-legacy-task"}
    assert store.read()["schema_version"] != STATE_SCHEMA


def test_native_migration_recognizes_interrupted_fixed_branch_setup(
    git_repo: Path,
) -> None:
    repo, store = _legacy_repo(git_repo)
    slot_path = git_repo.parent / "staged-slot"
    repo.git(["worktree", "add", "--detach", str(slot_path), "main"])
    state = store._empty()
    state["slots"]["01"] = {
        "id": "01",
        "path": str(slot_path),
        "status": "idle",
        "task_id": None,
    }
    atomic_write_json(store.path, state)
    branch = preview_native_migration(repo, base_ref="main")["slots"][0]["fixed_branch"]
    repo.git(["switch", "-c", branch, repo.head()], cwd=slot_path)

    preview = preview_native_migration(repo, base_ref="main")
    assert preview["status"] == "ready"
    assert preview["slots"][0]["staged"] is True
    assert (
        enable_native_migration(repo, base_ref="main", confirm=f"main:{repo.head()}")[
            "status"
        ]
        == "enabled"
    )


def test_native_migration_labels_legacy_non_ancestor_delivery(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, store = _legacy_repo(git_repo)
    slot_path = git_repo.parent / "legacy-delivered-slot"
    old_branch = "legacy/delivered-task"
    repo.git(["worktree", "add", "-b", old_branch, str(slot_path), "main"])
    (slot_path / "legacy.txt").write_text("legacy\n", encoding="utf-8")
    repo.git(["add", "legacy.txt"], cwd=slot_path)
    repo.git(["commit", "-m", "test: legacy source"], cwd=slot_path)
    old_head = repo.head(slot_path)
    state = store._empty()
    state["slots"]["01"] = {
        "id": "01",
        "path": str(slot_path),
        "status": "idle",
        "task_id": None,
        "released_candidate_task_id": "task-old",
    }
    atomic_write_json(store.path, state)
    candidate = {
        "candidate_id": "candidate-old",
        "task_id": "task-old",
        "branch": old_branch,
        "head": old_head,
        "base_ref": "main",
        "status": "integrated",
        "delivered": True,
    }
    monkeypatch.setattr(
        CandidateBatchStore,
        "read",
        lambda _self: {"candidates": {"candidate-old": candidate}, "batches": {}},
    )
    monkeypatch.setattr(
        CandidateBatchStore,
        "project_candidates",
        lambda _self, *_args, **_kwargs: [candidate],
    )

    preview = preview_native_migration(repo, base_ref="main")
    assert preview["status"] == "ready"
    enabled = enable_native_migration(
        repo, base_ref="main", confirm=f"main:{repo.head()}"
    )

    assert enabled["migration"]["legacy_non_ancestor_candidate_ids"] == [
        "candidate-old"
    ]
    assert repo.ref_head(f"refs/heads/{old_branch}") == old_head
    assert repo.head(slot_path) == repo.head(git_repo)
    assert repo.branch(slot_path) == preview["slots"][0]["fixed_branch"]
