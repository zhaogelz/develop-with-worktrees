from __future__ import annotations

from pathlib import Path

import pytest

from solo_ai.candidate_batches import CandidateBatchStore
from solo_ai.config import render_repo_config, render_verification_config
from solo_ai.native_migration import enable_native_migration, preview_native_migration
from solo_ai.repo import GitRepo
from solo_ai.state import STATE_SCHEMA, StateStore
from solo_ai.util import SoloAIError, atomic_write_json, path_identity


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


@pytest.mark.parametrize(
    "recorded_field",
    ["released_worktree_identity", "released_managed_root_identity"],
)
def test_native_migration_blocks_replaced_idle_slot_identity(
    git_repo: Path, recorded_field: str
) -> None:
    repo, store = _legacy_repo(git_repo)
    slot_path = git_repo.parent / "migration-identity-slot"
    repo.git(["worktree", "add", "--detach", str(slot_path), "main"])
    state = store._empty()
    state["slots"]["01"] = {
        "id": "01",
        "path": str(slot_path),
        "status": "idle",
        "task_id": None,
        "released_worktree_resolved": str(slot_path.resolve()),
        "released_worktree_identity": path_identity(slot_path),
        "released_managed_root_resolved": str(slot_path.parent.resolve()),
        "released_managed_root_identity": path_identity(slot_path.parent),
    }
    state["slots"]["01"][recorded_field] = {"device": -1, "inode": -1}
    atomic_write_json(store.path, state)

    preview = preview_native_migration(repo, base_ref="main")
    assert preview["status"] == "blocked"
    assert {"kind": "slot-identity-mismatch", "slot_id": "01"} in preview["blockers"]
    result = enable_native_migration(
        repo, base_ref="main", confirm=f"main:{preview['base_head']}"
    )
    assert result["status"] == "blocked"
    assert store.read()["schema_version"] != STATE_SCHEMA


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
    old_ref = "refs/dww/candidates/candidate-old"
    repo.git(["update-ref", old_ref, old_head])
    (git_repo / "legacy.txt").write_text("legacy\n", encoding="utf-8")
    repo.git(["add", "legacy.txt"])
    repo.git(["commit", "-m", "test: legacy composed result"])
    integrated_head = repo.head()
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
        "ref": old_ref,
        "base_ref": "main",
        "status": "integrated",
        "integrated_batch": "batch-old",
        "integrated_at": "2026-09-01T00:00:00Z",
    }
    batch = {
        "id": "batch-old",
        "status": "completed",
        "base_ref": "main",
        "candidate_ids": ["candidate-old"],
        "applied_candidate_ids": ["candidate-old"],
        "candidates": [candidate.copy()],
        "integration_head": integrated_head,
        "integrated_head": integrated_head,
        "proof": "legacy-proof",
        "promoted_at": "2026-09-01T00:00:00Z",
        "completed_at": "2026-09-01T00:00:01Z",
    }
    monkeypatch.setattr(
        CandidateBatchStore,
        "read",
        lambda _self: {
            "candidates": {"candidate-old": candidate},
            "batches": {"batch-old": batch},
        },
    )

    preview = preview_native_migration(repo, base_ref="main")
    assert preview["status"] == "ready"
    assert (
        CandidateBatchStore(repo).project_candidates([candidate], {"batch-old": batch})[
            0
        ]["delivered"]
        is True
    )
    repo.git(["update-ref", "-d", old_ref])
    assert preview_native_migration(repo, base_ref="main")["status"] == "ready"
    batch["candidates"][0]["head"] = integrated_head
    assert {
        "kind": "legacy-delivery-unverified",
        "candidate_id": "candidate-old",
    } in preview_native_migration(repo, base_ref="main")["blockers"]
    batch["candidates"][0]["head"] = old_head
    proof = batch.pop("proof")
    blocked = preview_native_migration(repo, base_ref="main")
    assert {
        "kind": "legacy-delivery-unverified",
        "candidate_id": "candidate-old",
    } in blocked["blockers"]
    batch["proof"] = proof
    enabled = enable_native_migration(
        repo, base_ref="main", confirm=f"main:{repo.head()}"
    )

    assert enabled["migration"]["legacy_non_ancestor_candidate_ids"] == [
        "candidate-old"
    ]
    assert repo.ref_head(f"refs/heads/{old_branch}") == old_head
    assert repo.head(slot_path) == repo.head(git_repo)
    assert repo.branch(slot_path) == preview["slots"][0]["fixed_branch"]


def test_native_migration_accepts_released_failed_batch_and_keeps_history(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, store = _legacy_repo(git_repo)
    slot_path = git_repo.parent / "mixed-legacy-slot"
    repo.git(["worktree", "add", "--detach", str(slot_path), "main"])
    state = store._empty()
    state["slots"]["01"] = {
        "id": "01",
        "path": str(slot_path),
        "status": "idle",
        "task_id": None,
        "released_candidate_task_id": "task-newer",
    }
    atomic_write_json(store.path, state)
    old_ref = "refs/dww/candidates/candidate-old"
    old_head = repo.head()
    repo.git(["update-ref", old_ref, old_head])
    old_candidate = {
        "candidate_id": "candidate-old",
        "task_id": "task-older",
        "branch": "legacy/older",
        "head": old_head,
        "ref": old_ref,
        "base_ref": "main",
        "status": "integrated",
        "delivered": True,
    }
    pool = {
        "candidates": {"candidate-old": old_candidate},
        "batches": {
            "batch-failed": {
                "id": "batch-failed",
                "status": "failed",
                "worktree_mode": "reusable",
                "worktree_released_at": "2026-09-01T00:00:00Z",
                "run_owner": None,
                "candidate_ids": ["candidate-old"],
            }
        },
    }
    monkeypatch.setattr(CandidateBatchStore, "read", lambda _self: pool)
    monkeypatch.setattr(
        CandidateBatchStore,
        "project_candidates",
        lambda _self, *_args, **_kwargs: [old_candidate],
    )

    ready = preview_native_migration(repo, base_ref="main")
    assert ready["status"] == "ready"
    assert store.read()["schema_version"] != STATE_SCHEMA

    enabled = enable_native_migration(
        repo, base_ref="main", confirm=f"main:{ready['base_head']}"
    )
    assert enabled["status"] == "enabled"
    assert enabled["migration"]["legacy_candidate_count"] == 1
    assert enabled["migration"]["legacy_batch_count"] == 1
    assert pool["batches"]["batch-failed"]["status"] == "failed"
    assert repo.ref_head(old_ref) == old_head
    assert repo.branch(slot_path) == ready["slots"][0]["fixed_branch"]


@pytest.mark.parametrize(
    ("released", "candidate_status", "run_owner"),
    [
        (False, "superseded", None),
        (True, "pending", None),
        (True, "superseded", {"pid": 123}),
    ],
)
def test_native_migration_keeps_unsettled_failed_batch_blocked(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    released: bool,
    candidate_status: str,
    run_owner: dict[str, int] | None,
) -> None:
    repo, store = _legacy_repo(git_repo)
    atomic_write_json(store.path, store._empty())
    candidate = {"candidate_id": "candidate-old", "status": candidate_status}
    batch = {
        "id": "batch-failed",
        "status": "failed",
        "worktree_mode": "reusable",
        "worktree_released_at": "2026-09-01T00:00:00Z" if released else None,
        "run_owner": run_owner,
        "candidate_ids": ["candidate-old"],
    }
    monkeypatch.setattr(
        CandidateBatchStore,
        "read",
        lambda _self: {
            "candidates": {"candidate-old": candidate},
            "batches": {"batch-failed": batch},
        },
    )
    monkeypatch.setattr(
        CandidateBatchStore,
        "project_candidates",
        lambda _self, *_args, **_kwargs: [candidate],
    )

    preview = preview_native_migration(repo, base_ref="main")
    assert {
        "kind": "unsettled-legacy-batch",
        "batch_id": "batch-failed",
        "status": "failed",
    } in preview["blockers"]


def test_native_migration_rejects_older_candidate_as_reused_slot_owner(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, store = _legacy_repo(git_repo)
    slot_path = git_repo.parent / "reused-attached-slot"
    old_branch = "legacy/older-owner"
    repo.git(["worktree", "add", "-b", old_branch, str(slot_path), "main"])
    old_head = repo.head(slot_path)
    state = store._empty()
    state["slots"]["01"] = {
        "id": "01",
        "path": str(slot_path),
        "status": "idle",
        "task_id": None,
        "released_candidate_task_id": "task-newer",
    }
    atomic_write_json(store.path, state)
    old_candidate = {
        "candidate_id": "candidate-old",
        "task_id": "task-older",
        "branch": old_branch,
        "head": old_head,
        "base_ref": "main",
        "status": "integrated",
        "delivered": True,
    }
    monkeypatch.setattr(
        CandidateBatchStore,
        "read",
        lambda _self: {"candidates": {"candidate-old": old_candidate}, "batches": {}},
    )
    monkeypatch.setattr(
        CandidateBatchStore,
        "project_candidates",
        lambda _self, *_args, **_kwargs: [old_candidate],
    )

    preview = preview_native_migration(repo, base_ref="main")
    assert preview["status"] == "blocked"
    assert {"kind": "attached-legacy-slot-unsettled", "slot_id": "01"} in preview[
        "blockers"
    ]
    assert repo.branch(slot_path) == old_branch
    assert store.read()["schema_version"] != STATE_SCHEMA


@pytest.mark.parametrize("linked", [True, False])
def test_native_migration_checks_attached_superseded_slot_lineage(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, linked: bool
) -> None:
    repo, store = _legacy_repo(git_repo)
    slot_path = git_repo.parent / "superseded-slot"
    old_branch = "legacy/superseded"
    repo.git(["worktree", "add", "-b", old_branch, str(slot_path), "main"])
    old_head = repo.head(slot_path)
    old_ref = "refs/dww/candidates/candidate-old"
    repo.git(["update-ref", old_ref, old_head])
    state = store._empty()
    state["slots"]["01"] = {
        "id": "01",
        "path": str(slot_path),
        "status": "idle",
        "task_id": None,
        "released_candidate_task_id": "task-old",
    }
    atomic_write_json(store.path, state)
    old = {
        "candidate_id": "candidate-old",
        "task_id": "task-old",
        "branch": old_branch,
        "head": old_head,
        "ref": old_ref,
        "base_ref": "main",
        "status": "superseded",
        "superseded_by": "candidate-new",
        "delivered": False,
    }
    successor = {
        "candidate_id": "candidate-new",
        "task_id": "task-new",
        "head": repo.head(),
        "base_ref": "main",
        "status": "integrated",
        "supersedes": "candidate-old" if linked else "another-candidate",
        "delivered": True,
    }
    candidates = [old, successor]
    monkeypatch.setattr(
        CandidateBatchStore,
        "read",
        lambda _self: {
            "candidates": {item["candidate_id"]: item for item in candidates},
            "batches": {},
        },
    )
    monkeypatch.setattr(
        CandidateBatchStore,
        "project_candidates",
        lambda _self, *_args, **_kwargs: candidates,
    )

    preview = preview_native_migration(repo, base_ref="main")
    assert preview["status"] == ("ready" if linked else "blocked")
    if linked:
        enabled = enable_native_migration(
            repo, base_ref="main", confirm=f"main:{preview['base_head']}"
        )
        assert enabled["status"] == "enabled"
        assert repo.branch(slot_path) == preview["slots"][0]["fixed_branch"]
        assert repo.ref_head(f"refs/heads/{old_branch}") == old_head
    else:
        assert {
            "kind": "attached-legacy-slot-unsettled",
            "slot_id": "01",
        } in preview["blockers"]
        assert repo.branch(slot_path) == old_branch


def test_native_migration_preserves_verified_retained_slot(git_repo: Path) -> None:
    repo, store = _legacy_repo(git_repo)
    slot_path = git_repo.parent / "retained-slot"
    branch = "legacy/retained"
    repo.git(["worktree", "add", "-b", branch, str(slot_path), "main"])
    (slot_path / "source.txt").write_text("retained\n", encoding="utf-8")
    repo.git(["add", "source.txt"], cwd=slot_path)
    repo.git(["commit", "-m", "test: retained source"], cwd=slot_path)
    tip = repo.head(slot_path)
    exclude = git_repo / ".git" / "info" / "exclude"
    exclude.write_text(
        exclude.read_text(encoding="utf-8") + "\n.audit/\n", encoding="utf-8"
    )
    audit = slot_path / ".audit" / "record.txt"
    audit.parent.mkdir()
    audit.write_text("preserve\n", encoding="utf-8")
    reason = "Retained worktree: 审计留存"
    worktree_identity = path_identity(slot_path)
    root_identity = path_identity(slot_path.parent)
    state = store._empty()
    state["slots"]["03"] = {
        "id": "03",
        "path": str(slot_path),
        "status": "quarantined",
        "task_id": "task-retained",
        "generation": 1,
        "quarantine_reason": reason,
        "released_worktree_resolved": str(slot_path.resolve()),
        "released_worktree_identity": worktree_identity,
        "released_managed_root_resolved": str(slot_path.parent.resolve()),
        "released_managed_root_identity": root_identity,
    }
    state["tasks"]["task-retained"] = {
        "id": "task-retained",
        "status": "abandoned",
        "slot_id": "03",
        "slot_generation": 1,
        "worktree": str(slot_path.resolve()),
        "quarantine_reason": reason,
        "active_operation": None,
        "processes": [],
        "slot_worktree_resolved": str(slot_path.resolve()),
        "slot_worktree_identity": worktree_identity,
        "slot_managed_root_resolved": str(slot_path.parent.resolve()),
        "slot_managed_root_identity": root_identity,
        "abandonment": {
            "phase": "completed",
            "retained_worktree": True,
            "task_id": "task-retained",
            "slot_id": "03",
            "branch": branch,
            "branch_tip": tip,
            "worktree": str(slot_path.resolve()),
            "worktree_resolved": str(slot_path.resolve()),
            "worktree_identity": worktree_identity,
            "managed_root_resolved": str(slot_path.parent.resolve()),
            "managed_root_identity": root_identity,
        },
    }
    atomic_write_json(store.path, state)

    preview = preview_native_migration(repo, base_ref="main")
    assert preview["status"] == "ready"
    assert preview["retained_slot_ids"] == ["03"]
    assert preview["slots"] == []
    state["tasks"]["task-retained"]["abandonment"]["branch_tip"] = repo.head()
    atomic_write_json(store.path, state)
    assert {"kind": "retained-slot-unverified", "slot_id": "03"} in (
        preview_native_migration(repo, base_ref="main")["blockers"]
    )
    state["tasks"]["task-retained"]["abandonment"]["branch_tip"] = tip
    atomic_write_json(store.path, state)

    enabled = enable_native_migration(
        repo, base_ref="main", confirm=f"main:{repo.head()}"
    )
    assert enabled["status"] == "enabled"
    assert enabled["migration"]["retained_slot_ids"] == ["03"]
    assert store.read()["slots"]["03"]["status"] == "quarantined"
    assert repo.branch(slot_path) == branch
    assert repo.head(slot_path) == tip
    assert audit.read_text(encoding="utf-8") == "preserve\n"
