from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import pytest

from solo_ai.candidate_batches import CandidateBatchStore
from solo_ai.cli import _parser
from solo_ai.config import (
    CommandSpec,
    load_repo_config,
    load_verification_config,
    render_repo_config,
    render_verification_config,
    verification_config_from_text,
)
from solo_ai.legacy_workspace_adoption import (
    adopt_legacy_integration_workspace,
    verified_pre_full_maintenance_source,
)
from solo_ai.proof import read_validation_attempt, validate
from solo_ai import proof as proof_module
from solo_ai import legacy_workspace_adoption as adoption_module
from solo_ai.repo import GitRepo
from solo_ai.state import STATE_SCHEMA, StateStore
from solo_ai.task_context import anchor_origin, create_anchor
from solo_ai.util import (
    CommandResult,
    SoloAIError,
    atomic_write_json,
    path_identity,
)


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


def test_adopts_cross_target_before_sealed_native_batch(
    git_repo: Path,
) -> None:
    repo, store, _, worktree, _ = _cross_target_setup(git_repo)
    base = repo.ref_head("refs/heads/main")
    old_head = repo.head(worktree)
    state = store.read()
    state["batches"]["batch-native"] = {
        "id": "batch-native",
        "status": "sealed",
        "base_ref": "main",
        "base_before": base,
        "integration_head": base,
        "worktree": str(worktree),
    }
    state["batches"]["batch-queued"] = {
        "id": "batch-queued",
        "status": "sealed",
        "base_ref": "main",
        "base_before": base,
        "integration_head": base,
        "worktree": str(worktree),
    }
    atomic_write_json(store.path, state)
    assert (
        adopt_legacy_integration_workspace(
            repo,
            batch_id="batch-native",
            confirm=f"batch-native:{old_head}:7",
        )["status"]
        == "adopted"
    )
    assert repo.head(worktree) == old_head
    assert store.read()["batches"]["batch-native"]["status"] == "sealed"


def test_cross_target_sealed_batch_rejects_claimed_workspace(
    git_repo: Path,
) -> None:
    repo, store, _, worktree, _ = _cross_target_setup(git_repo)
    base = repo.ref_head("refs/heads/main")
    old_head = repo.head(worktree)
    state = store.read()
    state["batches"]["batch-native"] = {
        "id": "batch-native",
        "status": "sealed",
        "base_ref": "main",
        "base_before": base,
        "integration_head": base,
        "worktree": str(worktree),
    }
    state["batches"]["batch-claimed"] = {
        "id": "batch-claimed",
        "status": "composing",
        "base_ref": "main",
        "base_before": base,
        "integration_head": base,
        "worktree": str(worktree),
        "worktree_generation": 8,
    }
    atomic_write_json(store.path, state)
    with pytest.raises(SoloAIError, match="Frozen native batch"):
        adopt_legacy_integration_workspace(
            repo,
            batch_id="batch-native",
            confirm=f"batch-native:{old_head}:7",
        )
    assert store.read()["integration_workspace"] is None


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


def test_cross_target_adoption_accepts_source_branch_advance(
    git_repo: Path,
) -> None:
    repo, store, _, worktree, confirm = _cross_target_setup(git_repo)
    saved = repo.head(worktree)
    tree = repo.tree(saved)
    advanced = repo.git(
        ["commit-tree", tree, "-p", saved, "-m", "later delivery"]
    ).stdout.strip()
    repo.git(["update-ref", "refs/heads/release/test", advanced, saved])
    assert (
        adopt_legacy_integration_workspace(repo, base_ref="main", confirm=confirm)[
            "status"
        ]
        == "adopted"
    )
    assert repo.head(worktree) == saved
    assert store.read()["integration_workspace"]["head"] == saved


def test_adopts_same_target_without_native_batch(git_repo: Path) -> None:
    repo, store, pool_store, worktree, _ = _setup(git_repo)
    head = repo.head()
    pool = pool_store.read()
    pool["batches"]["batch-legacy"].update(
        base_before=head,
        validation_outcome="passed",
        proof="d" * 64,
        promoted_at="2026-09-25T00:00:00Z",
        completed_at="2026-09-25T00:00:00Z",
        runtime_release={"configured": False, "operation": "batch-release"},
    )
    atomic_write_json(pool_store.path, pool)
    _passed_proof(repo, fingerprint="d" * 64, candidate=head, base=head)
    state = store.read()
    state["batches"] = {}
    atomic_write_json(store.path, state)
    result = adopt_legacy_integration_workspace(
        repo, base_ref="main", confirm=f"main:{head}:{head}:7"
    )
    assert result["status"] == "adopted"
    assert repo.head(worktree) == head


def _pre_full_setup(
    git_repo: Path,
) -> tuple[GitRepo, StateStore, CandidateBatchStore, str]:
    repo, store, pool_store, _, _ = _setup(git_repo)
    verification_file = git_repo / ".solo-ai" / "verification.toml"
    verification_file.write_text(
        render_verification_config(
            [CommandSpec(("git", "status", "--short"))],
            static_only=False,
            discovery_fallback=True,
        ).replace("environment = []", 'environment = ["DWW_TEST_ENV"]'),
        encoding="utf-8",
    )
    repo.git(["add", ".solo-ai/config.toml", ".solo-ai/verification.toml"])
    repo.git(["commit", "-m", "track verification policy"])
    base = repo.head()
    task_tree = git_repo.parent / f"{git_repo.name}-slot-01"
    repo.git(["worktree", "add", "-b", "codex/slot-01", str(task_tree), base])
    source_file = task_tree / "plugins" / "develop-with-worktrees" / "fix.txt"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("fix", encoding="utf-8")
    repo.git(["add", "plugins/develop-with-worktrees/fix.txt"], cwd=task_tree)
    repo.git(["commit", "-m", "maintenance fix"], cwd=task_tree)
    commit = repo.head(task_tree)
    state = store.read()
    state["batches"] = {}
    state["tasks"]["task-maintenance"] = {
        "id": "task-maintenance",
        "name": "DWW maintenance recovery",
        "anchor_contract": {
            "implementation_target": "DWW plugin maintenance",
            "scope_boundary": "verified plugin and test changes",
            "acceptance_criteria": "exact Full and Ready",
        },
        "status": "ready",
        "base_ref": "main",
        "base_head": base,
        "candidate_head": commit,
        "branch": "codex/slot-01",
        "worktree": str(task_tree),
        "slot_id": "01",
        "slot_generation": 1,
        "slot_worktree_identity": path_identity(task_tree),
        "runtime_activation": {"configured": False},
        "runtime_activation_pending": False,
        "processes": [],
        "native_delivery": {"batch_id": None, "ready_head": commit},
        "validation_attempts": ["full-attempt-maintenance"],
    }
    state["slots"]["01"] = {
        "task_id": "task-maintenance",
        "generation": 1,
        "status": "ready",
    }
    task = state["tasks"]["task-maintenance"]
    task["anchor_origin"] = anchor_origin(task)
    atomic_write_json(store.path, state)
    create_anchor(repo, task)
    _run_pre_full(repo, task_tree, base, commit, "full-attempt-maintenance")
    return repo, store, pool_store, commit


def _run_pre_full(
    repo: GitRepo, worktree: Path, base: str, commit: str, attempt_id: str
) -> None:
    validate(
        repo,
        cwd=worktree,
        base=base,
        verification=load_verification_config(repo, cwd=worktree),
        task_id="task-maintenance",
        level="full",
        expected_base_head=base,
        expected_candidate_head=commit,
        validation_base_ref="main",
        attempt_id=attempt_id,
        attempt_owner={"kind": "task", "id": "task-maintenance"},
    )


def test_pre_full_source_requires_exact_task_full_without_native_batch(
    git_repo: Path,
) -> None:
    repo, _, _, commit = _pre_full_setup(git_repo)
    result = verified_pre_full_maintenance_source(repo, commit=commit)
    assert result["source_commit"] == commit
    assert result["purpose"] == "pre-full-maintenance-review"
    assert result["validation_attempt"] == "full-attempt-maintenance"


def test_pre_full_source_accepts_latest_matching_full_after_environment_change(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, store, _, commit = _pre_full_setup(git_repo)
    monkeypatch.setenv("DWW_TEST_ENV", "second-full")
    task_tree = Path(store.read()["tasks"]["task-maintenance"]["worktree"])
    _run_pre_full(repo, task_tree, repo.head(), commit, "full-attempt-second")
    state = store.read()
    state["tasks"]["task-maintenance"]["validation_attempts"].append(
        "full-attempt-second"
    )
    atomic_write_json(store.path, state)
    result = verified_pre_full_maintenance_source(repo, commit=commit)
    assert result["validation_attempt"] == "full-attempt-second"


def test_pre_full_source_rejects_current_environment_drift(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _, _, commit = _pre_full_setup(git_repo)
    monkeypatch.setenv("DWW_TEST_ENV", "changed-after-full")
    with pytest.raises(SoloAIError, match="no current exact task Full"):
        verified_pre_full_maintenance_source(repo, commit=commit)


def test_pre_full_source_rejects_current_tool_drift(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _, _, commit = _pre_full_setup(git_repo)
    original_tool = proof_module._tool

    def changed_tool(command: CommandSpec, cwd: Path) -> dict[str, str | None]:
        facts = original_tool(command, cwd)
        if command.argv[0] == "git":
            return {**facts, "version": "changed-after-full"}
        return facts

    monkeypatch.setattr(proof_module, "_tool", changed_tool)
    with pytest.raises(SoloAIError, match="no current exact task Full"):
        verified_pre_full_maintenance_source(repo, commit=commit)


def test_pre_full_source_rejects_current_verification_policy_drift(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, store, _, commit = _pre_full_setup(git_repo)
    worktree = Path(store.read()["tasks"]["task-maintenance"]["worktree"])
    current = (worktree / ".solo-ai" / "verification.toml").read_text(encoding="utf-8")
    changed = verification_config_from_text(
        current.replace('environment = ["DWW_TEST_ENV"]', "environment = []"),
        source=worktree / ".solo-ai" / "verification.toml",
    )
    monkeypatch.setattr(
        adoption_module, "load_verification_config", lambda *_args, **_kwargs: changed
    )
    with pytest.raises(SoloAIError, match="no current exact task Full"):
        verified_pre_full_maintenance_source(repo, commit=commit)


@pytest.mark.parametrize(
    "drift", ["base", "proof", "task", "branch", "anchor", "extra-path"]
)
def test_pre_full_source_rejects_changed_evidence(git_repo: Path, drift: str) -> None:
    repo, store, _, commit = _pre_full_setup(git_repo)
    if drift == "base":
        repo.git(["commit", "--allow-empty", "-m", "move main"])
    elif drift == "proof":
        proof_id = read_validation_attempt(repo, "full-attempt-maintenance")["proof"]
        (repo.local_dir / "proofs" / f"{proof_id}.json").unlink()
    elif drift == "task":
        state = store.read()
        state["tasks"]["task-maintenance"]["status"] = "active"
        atomic_write_json(store.path, state)
    elif drift == "branch":
        repo.git(["update-ref", "refs/heads/codex/slot-01", repo.head()])
    elif drift == "anchor":
        (repo.local_dir / "task-anchors" / "task-maintenance.md").unlink()
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
