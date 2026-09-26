from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import pytest

from solo_ai.candidate_batches import CandidateBatchStore
from solo_ai.cli import _parser
from solo_ai.config import (
    load_repo_config,
    render_repo_config,
    render_verification_config,
)
from solo_ai.legacy_workspace_adoption import (
    adopt_legacy_integration_workspace,
    verified_pre_full_maintenance_source,
)
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
    assert args.base is None
    alternate = _parser().parse_args(
        [
            "migration",
            "adopt-legacy-integration-workspace",
            "--base",
            "main",
            "--confirm",
            "main:base:head:7",
        ]
    )
    assert alternate.base == "main"


def _passed_proof(
    repo: GitRepo, *, fingerprint: str, candidate: str, base: str
) -> None:
    log = repo.local_dir / "logs" / "content" / f"{fingerprint}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("passed\n", encoding="utf-8")
    atomic_write_json(
        repo.local_dir / "proofs" / f"{fingerprint}.json",
        {
            "schema_version": 3,
            "fingerprint": fingerprint,
            "result": "passed",
            "inputs": {
                "candidate_head": candidate,
                "base_head": base,
                "levels": ["ready", "full"],
            },
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


def _cross_target_setup(
    git_repo: Path,
) -> tuple[GitRepo, StateStore, CandidateBatchStore, Path, str]:
    repo, store, pool_store, worktree, _ = _setup(git_repo)
    base = repo.head()
    repo.git(["checkout", "-b", "release/test"], cwd=worktree)
    (worktree / "delivered.txt").write_text("delivered", encoding="utf-8")
    repo.git(["add", "delivered.txt"], cwd=worktree)
    repo.git(["commit", "-m", "release delivery"], cwd=worktree)
    head = repo.head(worktree)
    repo.git(["checkout", "--detach", head], cwd=worktree)
    repo.git(["update-ref", "refs/dww/batch-heads/batch-legacy", head])
    pool = pool_store.read()
    pool["integration_workspace"]["head"] = head
    legacy = pool["batches"]["batch-legacy"]
    legacy.update(
        base_ref="release/test",
        base_before=base,
        integration_head=head,
        integrated_head=head,
        validation_outcome="passed",
        proof="a" * 64,
        promoted_at="2026-09-25T00:00:00Z",
        completed_at="2026-09-25T00:00:00Z",
        runtime_release={"configured": False, "operation": "batch-release"},
    )
    atomic_write_json(pool_store.path, pool)
    _passed_proof(repo, fingerprint="a" * 64, candidate=head, base=base)
    state = store.read()
    state["batches"] = {}
    atomic_write_json(store.path, state)
    return repo, store, pool_store, worktree, f"main:{base}:{head}:7"


def test_adopts_cross_target_idle_workspace_without_switching_head(
    git_repo: Path,
) -> None:
    repo, store, pool_store, worktree, confirm = _cross_target_setup(git_repo)
    original = repo.head(worktree)
    result = adopt_legacy_integration_workspace(repo, base_ref="main", confirm=confirm)
    assert result["status"] == "adopted"
    assert result["receipt"]["source_batch_id"] == "batch-legacy"
    assert result["receipt"]["base_ref"] == "main"
    assert (
        store.read()["integration_workspace"]
        == pool_store.read()["integration_workspace"]
    )
    assert repo.head(worktree) == original
    assert (
        adopt_legacy_integration_workspace(repo, base_ref="main", confirm=confirm)[
            "status"
        ]
        == "already-adopted"
    )


@pytest.mark.parametrize(
    "drift", ["confirm", "native-batch", "source-branch", "proof", "ordinary-content"]
)
def test_cross_target_adoption_rejects_changed_evidence(
    git_repo: Path, drift: str
) -> None:
    repo, store, pool_store, worktree, confirm = _cross_target_setup(git_repo)
    if drift == "confirm":
        confirm = "main:wrong:head:7"
    elif drift == "native-batch":
        state = store.read()
        state["batches"]["batch-native"] = {"status": "sealed"}
        atomic_write_json(store.path, state)
    elif drift == "source-branch":
        repo.git(["update-ref", "refs/heads/release/test", repo.head()])
    elif drift == "proof":
        (repo.local_dir / "proofs" / f"{'a' * 64}.json").unlink()
    else:
        (worktree / "unknown.txt").write_text("unknown", encoding="utf-8")
    with pytest.raises(SoloAIError):
        adopt_legacy_integration_workspace(repo, base_ref="main", confirm=confirm)
    assert store.read()["integration_workspace"] is None


def _pre_full_setup(
    git_repo: Path,
) -> tuple[GitRepo, StateStore, CandidateBatchStore, str]:
    repo, store, pool_store, _, _ = _setup(git_repo)
    base = repo.head()
    legacy = pool_store.read()
    legacy["batches"]["batch-legacy"].update(
        base_before=base,
        validation_outcome="passed",
        proof="b" * 64,
        runtime_release={"configured": False, "operation": "batch-release"},
    )
    atomic_write_json(pool_store.path, legacy)
    _passed_proof(repo, fingerprint="b" * 64, candidate=base, base=base)
    task_tree = git_repo.parent / f"{git_repo.name}-slot-01"
    repo.git(["worktree", "add", "-b", "codex/slot-01", str(task_tree), base])
    source_file = task_tree / "plugins" / "develop-with-worktrees" / "fix.txt"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("fix", encoding="utf-8")
    repo.git(["add", "plugins/develop-with-worktrees/fix.txt"], cwd=task_tree)
    repo.git(["commit", "-m", "maintenance fix"], cwd=task_tree)
    commit = repo.head(task_tree)
    state = store.read()
    state["tasks"]["task-maintenance"] = {
        "id": "task-maintenance",
        "status": "ready",
        "base_ref": "main",
        "base_head": base,
        "candidate_head": commit,
        "branch": "codex/slot-01",
        "worktree": str(task_tree),
        "slot_id": "01",
        "slot_generation": 1,
        "slot_worktree_identity": path_identity(task_tree),
        "native_delivery": {"batch_id": None, "ready_head": commit},
        "validation_attempts": ["full-attempt-maintenance"],
    }
    state["slots"]["01"] = {
        "task_id": "task-maintenance",
        "generation": 1,
        "status": "ready",
    }
    atomic_write_json(store.path, state)
    _passed_proof(repo, fingerprint="c" * 64, candidate=commit, base=base)
    atomic_write_json(
        repo.local_dir / "validation-attempts" / "full-attempt-maintenance.json",
        {
            "schema_version": 1,
            "id": "full-attempt-maintenance",
            "owner": {"kind": "task", "id": "task-maintenance"},
            "task_id": "task-maintenance",
            "level": "full",
            "state": "completed",
            "result": "passed",
            "candidate_head": commit,
            "base_head": base,
            "proof": "c" * 64,
        },
    )
    return repo, store, pool_store, commit


def test_pre_full_source_requires_exact_task_full_and_idle_batch(
    git_repo: Path,
) -> None:
    repo, _, _, commit = _pre_full_setup(git_repo)
    result = verified_pre_full_maintenance_source(repo, commit=commit)
    assert result["source_commit"] == commit
    assert result["purpose"] == "pre-full-maintenance-review"
    assert result["validation_attempt"] == "full-attempt-maintenance"


@pytest.mark.parametrize("drift", ["batch", "proof", "task", "branch", "extra-path"])
def test_pre_full_source_rejects_changed_evidence(git_repo: Path, drift: str) -> None:
    repo, store, _, commit = _pre_full_setup(git_repo)
    if drift == "batch":
        state = store.read()
        state["batches"]["batch-native"]["runtime_cycle"] = 1
        atomic_write_json(store.path, state)
    elif drift == "proof":
        (repo.local_dir / "proofs" / f"{'c' * 64}.json").unlink()
    elif drift == "task":
        state = store.read()
        state["tasks"]["task-maintenance"]["status"] = "active"
        atomic_write_json(store.path, state)
    elif drift == "branch":
        repo.git(["update-ref", "refs/heads/codex/slot-01", repo.head()])
    else:
        task_tree = Path(store.read()["tasks"]["task-maintenance"]["worktree"])
        repo.git(["checkout", "-b", "extra-path", commit], cwd=task_tree)
        (task_tree / "unrelated.txt").write_text("drift", encoding="utf-8")
        repo.git(["add", "unrelated.txt"], cwd=task_tree)
        repo.git(["commit", "-m", "unrelated"], cwd=task_tree)
        moved = repo.head(task_tree)
        state = store.read()
        state["tasks"]["task-maintenance"]["candidate_head"] = moved
        state["tasks"]["task-maintenance"]["branch"] = "extra-path"
        state["tasks"]["task-maintenance"]["native_delivery"]["ready_head"] = moved
        atomic_write_json(store.path, state)
        commit = moved
    with pytest.raises(SoloAIError):
        verified_pre_full_maintenance_source(repo, commit=commit)

