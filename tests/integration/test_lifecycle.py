from pathlib import Path
import json

import pytest

from solo_ai.config import load_verification_config
from solo_ai.lifecycle import (
    abandon,
    approve,
    commit_task,
    finish,
    initialize,
    set_local_enabled,
    ready,
    start,
)
from solo_ai.repo import GitRepo
from solo_ai.state import StateStore
from solo_ai.util import SoloAIError

from conftest import git


VERIFY = "git diff --check main...HEAD"


def initialized(path: Path) -> GitRepo:
    repo = GitRepo(path)
    result = initialize(
        repo,
        slots=3,
        commands=[VERIFY],
        accept=True,
        accept_static_only=False,
        compatible=False,
    )
    assert result["commands"] == [VERIFY]
    return repo


def test_full_managed_lifecycle(git_repo: Path) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="add greeting")
    worktree = Path(task["worktree"])
    (worktree / "hello.txt").write_text("hello\n", encoding="utf-8")

    committed = commit_task(
        repo, task_id=task["id"], lease=task["lease"], message="feat: add greeting"
    )
    prepared = ready(repo, task_id=task["id"], lease=task["lease"])
    result = finish(repo, task_id=task["id"], lease=task["lease"])

    assert committed["candidate_head"] == prepared["candidate_head"]
    assert result["proof"] == prepared["ready_proof"]
    assert (git_repo / "hello.txt").read_text(encoding="utf-8") == "hello\n"
    assert git(git_repo, "branch", "--show-current") == "main"
    assert StateStore(repo).task(task["id"])["status"] == "finished"


def test_start_ignores_dirty_primary_and_abandon_is_exact(git_repo: Path) -> None:
    repo = initialized(git_repo)
    (git_repo / "README.md").write_text("dirty\n", encoding="utf-8")
    task = start(repo, name="isolated task")
    with pytest.raises(SoloAIError, match="exact task id"):
        abandon(repo, task_id=task["id"], lease=task["lease"], confirm="wrong")
    result = abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])
    assert result["status"] == "abandoned"
    assert (git_repo / "README.md").read_text(encoding="utf-8") == "dirty\n"


def test_sensitive_candidate_is_blocked_and_preserved(git_repo: Path) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="unsafe task")
    worktree = Path(task["worktree"])
    (worktree / ".env.production").write_text(
        "PASSWORD=not-for-commit\n", encoding="utf-8"
    )
    with pytest.raises(SoloAIError, match="Sensitive-content gate"):
        commit_task(repo, task_id=task["id"], lease=task["lease"], message="bad")
    assert (worktree / ".env.production").exists()
    assert StateStore(repo).task(task["id"])["status"] == "active"


def test_static_only_requires_explicit_acceptance(git_repo: Path) -> None:
    repo = GitRepo(git_repo)
    with pytest.raises(SoloAIError, match="accept-static-only"):
        initialize(
            repo,
            slots=1,
            commands=[],
            accept=False,
            accept_static_only=False,
            compatible=False,
        )


def test_changed_validation_commands_require_new_approval(git_repo: Path) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="change validation")
    worktree = Path(task["worktree"])
    verification = worktree / ".solo-ai" / "verification.toml"
    verification.write_text(
        'schema_version = 1\nstatic_only = false\n\n[[profiles]]\nid = "default"\npaths = ["**"]\ncommands = ["git diff --check"]\n',
        encoding="utf-8",
    )
    commit_task(
        repo,
        task_id=task["id"],
        lease=task["lease"],
        message="chore: change validation",
    )
    with pytest.raises(SoloAIError, match="not approved"):
        ready(repo, task_id=task["id"], lease=task["lease"])
    approve(repo, load_verification_config(repo, cwd=worktree))
    assert ready(repo, task_id=task["id"], lease=task["lease"])["status"] == "ready"


def test_compatible_mode_defers_to_existing_workflow(git_repo: Path) -> None:
    marker = git_repo / ".config" / "wt.toml"
    marker.parent.mkdir()
    marker.write_text("# existing\n", encoding="utf-8")
    git(git_repo, "add", ".config/wt.toml")
    git(git_repo, "commit", "-m", "add existing workflow")
    repo = GitRepo(git_repo)
    initialize(
        repo,
        slots=3,
        commands=[VERIFY],
        accept=True,
        accept_static_only=False,
        compatible=True,
    )
    assert "must not claim its own managed slot" in (git_repo / "AGENTS.md").read_text(
        encoding="utf-8"
    )
    with pytest.raises(SoloAIError, match="Compatible mode"):
        start(repo, name="defer")


def test_personal_disable_is_local_and_blocks_start(git_repo: Path) -> None:
    repo = initialized(git_repo)
    set_local_enabled(repo, enabled=False)
    with pytest.raises(SoloAIError, match="disabled on this machine"):
        start(repo, name="paused")
    assert not (git_repo / ".solo-ai" / "preferences.json").exists()
    set_local_enabled(repo, enabled=True)
    task = start(repo, name="resumed")
    abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])


def test_profile_proof_reuses_identical_inputs_across_tasks(git_repo: Path) -> None:
    repo = initialized(git_repo)
    first = start(repo, name="first identical change")
    first_tree = Path(first["worktree"])
    (first_tree / "same.txt").write_text("same\n", encoding="utf-8")
    commit_task(
        repo,
        task_id=first["id"],
        lease=first["lease"],
        message="test: first candidate",
    )
    first_ready = ready(repo, task_id=first["id"], lease=first["lease"])

    second = start(repo, name="second identical change")
    second_tree = Path(second["worktree"])
    (second_tree / "same.txt").write_text("same\n", encoding="utf-8")
    commit_task(
        repo,
        task_id=second["id"],
        lease=second["lease"],
        message="test: second candidate",
    )
    second_ready = ready(repo, task_id=second["id"], lease=second["lease"])
    proof_path = repo.local_dir / "proofs" / f"{second_ready['ready_proof']}.json"
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    assert proof["profile_proofs"][0]["reused"] is True
    assert first_ready["ready_proof"] != second_ready["ready_proof"]

    abandon(repo, task_id=first["id"], lease=first["lease"], confirm=first["id"])
    abandon(
        repo,
        task_id=second["id"],
        lease=second["lease"],
        confirm=second["id"],
    )


def test_validation_failure_preserves_task(git_repo: Path) -> None:
    repo = GitRepo(git_repo)
    initialize(
        repo,
        slots=1,
        commands=["git diff --quiet main...HEAD"],
        accept=True,
        accept_static_only=False,
        compatible=False,
    )
    task = start(repo, name="expected validation failure")
    worktree = Path(task["worktree"])
    (worktree / "change.txt").write_text("change\n", encoding="utf-8")
    commit_task(
        repo,
        task_id=task["id"],
        lease=task["lease"],
        message="test: failing candidate",
    )
    with pytest.raises(SoloAIError, match="Validation failed"):
        ready(repo, task_id=task["id"], lease=task["lease"])
    assert StateStore(repo).task(task["id"])["status"] == "active"
    assert (worktree / "change.txt").exists()
    abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])


def test_parallel_conflict_stays_in_later_worktree(git_repo: Path) -> None:
    repo = initialized(git_repo)
    first = start(repo, name="first edit")
    second = start(repo, name="conflicting edit")
    first_tree = Path(first["worktree"])
    second_tree = Path(second["worktree"])
    (first_tree / "README.md").write_text("first\n", encoding="utf-8")
    (second_tree / "README.md").write_text("second\n", encoding="utf-8")
    commit_task(
        repo,
        task_id=first["id"],
        lease=first["lease"],
        message="test: first edit",
    )
    commit_task(
        repo,
        task_id=second["id"],
        lease=second["lease"],
        message="test: second edit",
    )
    ready(repo, task_id=first["id"], lease=first["lease"])
    finish(repo, task_id=first["id"], lease=first["lease"])
    with pytest.raises(SoloAIError, match="Command failed"):
        ready(repo, task_id=second["id"], lease=second["lease"])
    assert "UU README.md" in repo.status_lines(second_tree)
    assert StateStore(repo).task(second["id"])["status"] == "active"
    abandon(
        repo,
        task_id=second["id"],
        lease=second["lease"],
        confirm=second["id"],
    )
