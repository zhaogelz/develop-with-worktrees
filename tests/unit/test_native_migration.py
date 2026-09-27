from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from solo_ai import batch_workspace
from solo_ai.candidate_batches import CandidateBatchStore
from solo_ai.config import (
    load_repo_config,
    render_repo_config,
    render_verification_config,
)
from solo_ai.native_migration import enable_native_migration, preview_native_migration
from solo_ai.repo import GitRepo
from solo_ai.state import STATE_SCHEMA, StateStore
from solo_ai.util import (
    CommandResult,
    SoloAIError,
    atomic_write_json,
    path_identity,
    sha256_text,
    stable_json,
)


def _legacy_repo(root: Path) -> tuple[GitRepo, StateStore]:
    config = root / ".solo-ai"
    config.mkdir()
    (config / "config.toml").write_text(render_repo_config(), encoding="utf-8")
    (config / "verification.toml").write_text(
        render_verification_config([], static_only=True), encoding="utf-8"
    )
    repo = GitRepo(root)
    return repo, StateStore(repo)


def _idle_integration_scene(
    root: Path, *, source_ref: str = "main"
) -> tuple[GitRepo, StateStore, dict[str, object], Path]:
    """建立已完成并归还的旧集成工作区，保留真实 Git 注册和引用。"""

    repo, store = _legacy_repo(root)
    worktree = (
        store.managed_worktree_root(load_repo_config(repo)) / "solo-ai-integration"
    )
    worktree.parent.mkdir(parents=True)
    if source_ref != "main":
        repo.git(["branch", source_ref, "main"])
    source_base = repo.ref_head(f"refs/heads/{source_ref}")
    repo.git(["worktree", "add", "--detach", str(worktree), source_ref])
    if source_ref != "main":
        (worktree / "release-only.txt").write_text("release\n", encoding="utf-8")
        repo.git(["add", "release-only.txt"], cwd=worktree)
        repo.git(["commit", "-m", "test: release result"], cwd=worktree)
        repo.git(["update-ref", f"refs/heads/{source_ref}", repo.head(worktree)])
    head = repo.head(worktree)
    head_ref = "refs/dww/batch-heads/batch-legacy-idle"
    repo.git(["update-ref", head_ref, head])
    location = {
        "worktree": str(worktree),
        "worktree_resolved": str(worktree.resolve()),
        "worktree_identity": path_identity(worktree),
        "managed_root_resolved": str(worktree.parent.resolve()),
        "managed_root_identity": path_identity(worktree.parent),
    }
    record: dict[str, object] = {
        **location,
        "generation": 7,
        "owner": None,
        "registering": False,
        "head": head,
        "head_ref": head_ref,
    }
    pool_store = CandidateBatchStore(repo)
    pool = pool_store._empty()
    pool["integration_workspace"] = record
    pool["batches"]["batch-legacy-idle"] = {
        "id": "batch-legacy-idle",
        "status": "completed",
        "worktree_mode": "reusable",
        "worktree_released_at": "2026-09-01T00:00:00Z",
        "run_owner": None,
        "worktree_generation": 7,
        "base_ref": source_ref,
        "integration_ref": head_ref,
        "integration_head": head,
        "integrated_head": head,
        **location,
    }
    if source_ref != "main":
        proof_inputs = {
            "candidate_head": head,
            "base_head": source_base,
            "levels": ["ready", "full"],
        }
        fingerprint = sha256_text(stable_json(proof_inputs))
        log = repo.local_dir / "logs" / "content" / f"{fingerprint}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("passed\n", encoding="utf-8")
        atomic_write_json(
            repo.local_dir / "proofs" / f"{fingerprint}.json",
            {
                "schema_version": 3,
                "fingerprint": fingerprint,
                "result": "passed",
                "inputs": proof_inputs,
                "runs": [
                    {
                        "exit_code": 0,
                        "timed_out": False,
                        "log": str(log),
                        "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
                    }
                ],
            },
        )
        pool["batches"]["batch-legacy-idle"].update(
            base_before=source_base,
            validation_outcome="passed",
            proof=fingerprint,
            promoted_at="2026-09-01T00:00:00Z",
            completed_at="2026-09-01T00:00:00Z",
            runtime_release={"configured": False, "operation": "batch-release"},
        )
    atomic_write_json(pool_store.path, pool)
    atomic_write_json(store.path, store._empty())
    return repo, store, record, worktree


@pytest.mark.parametrize("source_ref", ["main", "release/preflight"])
def test_native_migration_carries_verified_idle_integration_workspace(
    git_repo: Path, source_ref: str
) -> None:
    repo, store, record, worktree = _idle_integration_scene(
        git_repo, source_ref=source_ref
    )
    base = repo.ref_head("refs/heads/main")
    assert base is not None
    if source_ref != "main":
        assert not repo.is_ancestor(str(record["head"]), base)

    preview = preview_native_migration(repo, base_ref="main")
    assert preview["status"] == "ready"
    assert preview["integration_workspace_status"] == "verified-idle"

    enabled = enable_native_migration(repo, base_ref="main", confirm=f"main:{base}")
    assert enabled["status"] == "enabled"
    assert store.read()["integration_workspace"] == record
    assert repo.head(worktree) == record["head"]
    assert repo.ref_head(str(record["head_ref"])) == record["head"]
    after = preview_native_migration(repo, base_ref="main")
    assert after["blockers"] == []
    assert after["integration_workspace_status"] == "managed-idle"

    state = store.read()
    next_batch = {
        "id": "batch-native-next",
        "status": "sealed",
        "base_before": base,
        "integration_head": base,
    }
    state["batches"][next_batch["id"]] = next_batch
    atomic_write_json(store.path, state)
    acquired = batch_workspace.acquire(repo, store, next_batch, worktree)
    assert acquired["worktree_generation"] == 8
    assert acquired["integration_ref"] == "refs/dww/batch-heads/batch-native-next"
    assert store.read()["integration_workspace"]["owner"] == next_batch["id"]
    assert repo.head(worktree) == base


def test_native_migration_blocks_existing_worktree_without_legacy_binding(
    git_repo: Path,
) -> None:
    repo, store, _record, worktree = _idle_integration_scene(git_repo)
    pool_store = CandidateBatchStore(repo)
    pool = pool_store.read()
    pool.pop("integration_workspace")
    atomic_write_json(pool_store.path, pool)

    preview = preview_native_migration(repo, base_ref="main")
    assert preview["status"] == "blocked"
    assert {"kind": "legacy-integration-workspace-unowned"} in preview["blockers"]
    result = enable_native_migration(
        repo, base_ref="main", confirm=f"main:{repo.ref_head('refs/heads/main')}"
    )
    assert result["status"] == "blocked"
    assert store.read()["schema_version"] != STATE_SCHEMA
    assert worktree.exists()


def test_native_preview_blocks_unreadable_unknown_workspace_directory(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _, _, _ = _idle_integration_scene(git_repo)
    original_git = repo.git

    def warned_git(args: list[str], **kwargs: object) -> CommandResult:
        result = original_git(args, **kwargs)
        if args[:2] == ["ls-files", "--others"]:
            return CommandResult(
                result.args,
                result.returncode,
                result.stdout,
                "warning: could not open directory 'unknown/': Permission denied\n",
            )
        return result

    monkeypatch.setattr(repo, "git", warned_git)
    preview = preview_native_migration(repo, base_ref="main")
    assert preview["status"] == "blocked"
    assert any(
        "Cannot inventory" in item.get("reason", "") for item in preview["blockers"]
    )


@pytest.mark.parametrize("drift", ["saved-ref", "directory-identity", "unknown-file"])
def test_native_migration_preserves_unverified_idle_workspace(
    git_repo: Path, drift: str
) -> None:
    repo, store, _record, worktree = _idle_integration_scene(git_repo)
    if drift == "saved-ref":
        repo.git(["update-ref", "-d", "refs/dww/batch-heads/batch-legacy-idle"])
    elif drift == "directory-identity":
        pool_store = CandidateBatchStore(repo)
        pool = pool_store.read()
        pool["integration_workspace"]["worktree_identity"] = {
            "device": -1,
            "inode": -1,
        }
        atomic_write_json(pool_store.path, pool)
    else:
        (worktree / "untracked.txt").write_text("keep me\n", encoding="utf-8")

    preview = preview_native_migration(repo, base_ref="main")
    assert preview["status"] == "blocked"
    assert any(
        item["kind"] == "legacy-integration-workspace-unverified"
        for item in preview["blockers"]
    )
    assert store.read()["schema_version"] != STATE_SCHEMA


def test_upgraded_preview_reports_missing_legacy_workspace_binding(
    git_repo: Path,
) -> None:
    repo, store, _record, _worktree = _idle_integration_scene(git_repo)
    state = store.read()
    state["schema_version"] = STATE_SCHEMA
    state["integration_workspace"] = None
    state["native_migration"] = {
        "base_ref": "main",
        "base_head": repo.ref_head("refs/heads/main"),
    }
    atomic_write_json(store.path, state)

    preview = preview_native_migration(repo, base_ref="main")
    assert preview["status"] == "enabled"
    assert preview["integration_workspace_status"] == "legacy-adoption-required"
    assert {"kind": "legacy-integration-workspace-unbound"} in preview["blockers"]


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


@pytest.mark.parametrize(
    "case",
    [
        "valid",
        "missing-withdrawal",
        "missing-ref",
        "moved-ref",
        "not-ancestor",
        "wrong-owner",
    ],
)
def test_native_migration_checks_withdrawn_ancestor_slot(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    repo, store = _legacy_repo(git_repo)
    slot_path = git_repo.parent / "withdrawn-ancestor-slot"
    old_branch = "legacy/withdrawn-source"
    repo.git(["worktree", "add", "-b", old_branch, str(slot_path), "main"])
    base_head = repo.head()
    (slot_path / "source.txt").write_text("source\n", encoding="utf-8")
    repo.git(["add", "source.txt"], cwd=slot_path)
    repo.git(["commit", "-m", "test: withdrawn source"], cwd=slot_path)
    old_head = repo.head(slot_path)
    old_ref = "refs/dww/candidates/candidate-old"
    if case != "missing-ref":
        repo.git(
            ["update-ref", old_ref, base_head if case == "moved-ref" else old_head]
        )
    if case != "not-ancestor":
        repo.git(["merge", "--ff-only", old_branch])
        (git_repo / "later.txt").write_text("later\n", encoding="utf-8")
        repo.git(["add", "later.txt"])
        repo.git(["commit", "-m", "test: later target commit"])
    assert repo.is_ancestor(old_head, repo.head()) is (case != "not-ancestor")

    state = store._empty()
    state["slots"]["01"] = {
        "id": "01",
        "path": str(slot_path),
        "status": "idle",
        "task_id": None,
        "released_candidate_task_id": (
            "task-newer" if case == "wrong-owner" else "task-old"
        ),
    }
    atomic_write_json(store.path, state)
    candidate = {
        "candidate_id": "candidate-old",
        "task_id": "task-old",
        "branch": old_branch,
        "head": old_head,
        "ref": old_ref,
        "base_ref": "legacy/other-target",
        "status": "withdrawn",
        "delivered": False,
        "withdrawal": {
            "reason": "source commit is in the target history",
            "source": "cli",
            "started_at": "2026-09-25T00:00:00Z",
            "ref_retention": "preserved",
        },
        "withdrawn_at": "2026-09-25T00:00:00Z",
    }
    if case == "missing-withdrawal":
        candidate.pop("withdrawal")
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
    if case == "valid":
        assert preview["status"] == "ready"
        enabled = enable_native_migration(
            repo, base_ref="main", confirm=f"main:{preview['base_head']}"
        )
        assert enabled["status"] == "enabled"
        assert store.read()["schema_version"] == STATE_SCHEMA
        assert repo.branch(slot_path) == preview["slots"][0]["fixed_branch"]
        assert repo.ref_head(old_ref) == old_head
        assert repo.ref_head(f"refs/heads/{old_branch}") == old_head
    else:
        assert preview["status"] == "blocked"
        assert {"kind": "attached-legacy-slot-unsettled", "slot_id": "01"} in preview[
            "blockers"
        ]
        assert store.read()["schema_version"] != STATE_SCHEMA
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
