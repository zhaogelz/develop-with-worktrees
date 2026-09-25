from __future__ import annotations

import copy
from pathlib import Path

import pytest

from solo_ai.candidate_batches import CandidateBatchStore
from solo_ai.cli import _parser
from solo_ai.config import (
    load_repo_config,
    render_repo_config,
    render_verification_config,
)
from solo_ai.legacy_workspace_adoption import adopt_legacy_integration_workspace
from solo_ai.repo import GitRepo
from solo_ai.state import STATE_SCHEMA, StateStore
from solo_ai.util import CommandResult, SoloAIError, atomic_write_json, path_identity


pytestmark = pytest.mark.dww_fast


def _setup(
    git_repo: Path,
) -> tuple[GitRepo, StateStore, CandidateBatchStore, Path, str]:
    config = git_repo / ".solo-ai"
    config.mkdir()
    (config / "config.toml").write_text(render_repo_config(), encoding="utf-8")
    (config / "verification.toml").write_text(
        render_verification_config([], static_only=True), encoding="utf-8"
    )
    (git_repo / ".gitignore").write_text(".pytest_cache/\n", encoding="utf-8")
    repo = GitRepo(git_repo)
    repo.git(["add", ".gitignore"])
    repo.git(["commit", "-m", "test ignore"])
    store = StateStore(repo)
    pool_store = CandidateBatchStore(repo)
    worktree = (
        store.managed_worktree_root(load_repo_config(repo)) / "solo-ai-integration"
    )
    worktree.parent.mkdir(parents=True, exist_ok=True)
    head = repo.head()
    repo.git(["worktree", "add", "--detach", str(worktree), head])

    legacy_id = "batch-legacy"
    head_ref = f"refs/dww/batch-heads/{legacy_id}"
    repo.git(["update-ref", head_ref, head])
    record = {
        "generation": 7,
        "owner": None,
        "registering": False,
        "head": head,
        "head_ref": head_ref,
        "worktree": str(worktree),
        "worktree_resolved": str(worktree.resolve()),
        "worktree_identity": path_identity(worktree),
        "managed_root_resolved": str(worktree.parent.resolve()),
        "managed_root_identity": path_identity(worktree.parent),
    }
    pool = pool_store._empty()
    pool["integration_workspace"] = copy.deepcopy(record)
    pool["batches"][legacy_id] = {
        "id": legacy_id,
        "status": "completed",
        "worktree_mode": "reusable",
        "worktree_released_at": "2026-09-25T00:00:00Z",
        "run_owner": None,
        "base_ref": "main",
        "worktree_generation": 7,
        "integration_ref": head_ref,
        "integration_head": head,
        "integrated_head": head,
        **{
            key: copy.deepcopy(record[key])
            for key in (
                "worktree",
                "worktree_resolved",
                "worktree_identity",
                "managed_root_resolved",
                "managed_root_identity",
            )
        },
    }
    atomic_write_json(pool_store.path, pool)
    state = store._empty()
    state["schema_version"] = STATE_SCHEMA
    state["native_migration"] = {
        "base_ref": "main",
        "base_head": head,
        "legacy_candidate_count": 0,
        "legacy_batch_count": 1,
    }
    state["batches"]["batch-native"] = {
        "id": "batch-native",
        "status": "sealed",
        "base_ref": "main",
        "base_before": head,
        "integration_head": head,
        "worktree": str(worktree),
    }
    atomic_write_json(store.path, state)
    return repo, store, pool_store, worktree, f"batch-native:{head}:7"


def test_adopts_exact_idle_record_once_without_touching_worktree(
    git_repo: Path,
) -> None:
    repo, store, pool_store, worktree, confirm = _setup(git_repo)
    (worktree / ".pytest_cache").mkdir()
    (worktree / ".pytest_cache" / "cache.bin").write_bytes(b"retained")
    original = copy.deepcopy(pool_store.read()["integration_workspace"])

    result = adopt_legacy_integration_workspace(
        repo, batch_id="batch-native", confirm=confirm
    )

    assert result["status"] == "adopted"
    state = store.read()
    assert state["integration_workspace"] == original
    assert state["batches"]["batch-native"]["status"] == "sealed"
    assert state["batches"]["batch-native"].get("worktree_generation") is None
    assert (
        result["receipt"]
        == state["native_migration"]["legacy_integration_workspace_adoption"]
    )
    assert (worktree / ".pytest_cache" / "cache.bin").read_bytes() == b"retained"
    assert pool_store.read()["integration_workspace"] == original
    assert (
        adopt_legacy_integration_workspace(
            repo, batch_id="batch-native", confirm=confirm
        )["status"]
        == "already-adopted"
    )
    state["integration_workspace"] = None
    atomic_write_json(store.path, state)
    with pytest.raises(SoloAIError, match="already adopted"):
        adopt_legacy_integration_workspace(
            repo, batch_id="batch-native", confirm=confirm
        )


@pytest.mark.parametrize(
    "drift",
    [
        "confirm",
        "native-status",
        "legacy-owner",
        "legacy-identity",
        "missing-ref",
        "ordinary-content",
        "unverified-delivery",
    ],
)
def test_rejects_drift_without_creating_binding(git_repo: Path, drift: str) -> None:
    repo, store, pool_store, worktree, confirm = _setup(git_repo)
    if drift == "confirm":
        confirm = "batch-native:wrong:7"
    elif drift == "native-status":
        state = store.read()
        state["batches"]["batch-native"]["status"] = "composing"
        atomic_write_json(store.path, state)
    elif drift in {"legacy-owner", "legacy-identity"}:
        pool = pool_store.read()
        record = pool["integration_workspace"]
        if drift == "legacy-owner":
            record["owner"] = "batch-other"
        else:
            record["worktree_identity"] = {"device": -1, "inode": -1}
        atomic_write_json(pool_store.path, pool)
    elif drift == "missing-ref":
        repo.git(
            ["update-ref", "-d", pool_store.read()["integration_workspace"]["head_ref"]]
        )
    elif drift == "unverified-delivery":
        pool = pool_store.read()
        pool["candidates"]["candidate-unverified"] = {
            "candidate_id": "candidate-unverified",
            "status": "integrated",
            "base_ref": "main",
            "head": repo.head(),
        }
        atomic_write_json(pool_store.path, pool)
        state = store.read()
        state["native_migration"]["legacy_candidate_count"] = 1
        atomic_write_json(store.path, state)
    else:
        (worktree / "notes.txt").write_text("unknown", encoding="utf-8")

    with pytest.raises(SoloAIError):
        adopt_legacy_integration_workspace(
            repo, batch_id="batch-native", confirm=confirm
        )

    assert store.read()["integration_workspace"] is None
    assert (
        "legacy_integration_workspace_adoption" not in store.read()["native_migration"]
    )


def test_unreadable_directory_warning_requires_plain_opaque_root(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, store, _, worktree, confirm = _setup(git_repo)
    (worktree / ".pytest_cache").mkdir()
    original_git = repo.git

    def warned_git(args: list[str], **kwargs: object) -> CommandResult:
        result = original_git(args, **kwargs)
        if args[:2] == ["ls-files", "--others"]:
            return CommandResult(
                result.args,
                result.returncode,
                result.stdout,
                "warning: could not open directory '.pytest_cache/': Permission denied\n",
            )
        return result

    monkeypatch.setattr(repo, "git", warned_git)
    assert (
        adopt_legacy_integration_workspace(
            repo, batch_id="batch-native", confirm=confirm
        )["status"]
        == "adopted"
    )
    assert store.read()["integration_workspace"] is not None


def test_unreadable_unknown_directory_warning_blocks_adoption(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, store, _, _, confirm = _setup(git_repo)
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
    with pytest.raises(SoloAIError, match="Cannot inventory"):
        adopt_legacy_integration_workspace(
            repo, batch_id="batch-native", confirm=confirm
        )
    assert store.read()["integration_workspace"] is None


def test_cli_exposes_exact_one_time_command() -> None:
    args = _parser().parse_args(
        [
            "migration",
            "adopt-legacy-integration-workspace",
            "--batch",
            "batch-native",
            "--confirm",
            "batch-native:head:7",
        ]
    )
    assert args.migration_command == "adopt-legacy-integration-workspace"
    assert args.batch == "batch-native"
