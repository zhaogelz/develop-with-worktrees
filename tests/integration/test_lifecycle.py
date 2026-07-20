from __future__ import annotations

import json
import socket
import sys
import time
from pathlib import Path

import pytest

from solo_ai.config import CommandSpec, load_verification_config
from solo_ai.cli import _prune
from solo_ai.lifecycle import (
    abandon,
    approve,
    commit_task,
    deinit,
    dev_start,
    dev_stop,
    finish,
    initialize,
    ready,
    set_local_enabled,
    start,
)
from solo_ai.repo import GitRepo
from solo_ai.state import StateStore
from solo_ai.util import SoloAIError

from conftest import git


VERIFY = CommandSpec(("git", "diff", "--check", "main...HEAD"))


def initialized(path: Path) -> GitRepo:
    repo = GitRepo(path)
    result = initialize(
        repo, slots=3, commands=[VERIFY], accept=True, accept_static_only=False
    )
    assert result["decision"] == "adopted"
    return repo


def commit_one(
    repo: GitRepo, task: dict[str, str], relative: str, contents: str, message: str
) -> None:
    worktree = Path(task["worktree"])
    (worktree / relative).write_text(contents, encoding="utf-8")
    commit_task(
        repo, task_id=task["id"], lease=task["lease"], message=message, paths=[relative]
    )


def test_full_managed_lifecycle_and_exact_ready_proof_reuse(git_repo: Path) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="add greeting")
    commit_one(repo, task, "hello.txt", "hello\n", "feat: add greeting")
    prepared = ready(repo, task_id=task["id"], lease=task["lease"])
    result = finish(repo, task_id=task["id"], lease=task["lease"])
    assert result["proof"] == prepared["ready_proof"]
    assert result["proof_reused"] is True
    assert (git_repo / "hello.txt").read_text(encoding="utf-8") == "hello\n"
    assert StateStore(repo).task(task["id"])["status"] == "finished"


def test_dirty_primary_bootstrap_is_pending_then_first_finish_integrates(
    git_repo: Path,
) -> None:
    (git_repo / "README.md").write_text("dirty primary\n", encoding="utf-8")
    repo = GitRepo(git_repo)
    result = initialize(
        repo, slots=1, commands=[VERIFY], accept=True, accept_static_only=False
    )
    assert result["decision"] == "pending-primary-clean"
    assert not (git_repo / ".solo-ai").exists()
    task = start(repo, name="isolated task")
    commit_one(repo, task, "task.txt", "isolated\n", "feat: isolated")
    ready(repo, task_id=task["id"], lease=task["lease"])
    with pytest.raises(SoloAIError, match="clean before integration"):
        finish(repo, task_id=task["id"], lease=task["lease"])
    git(git_repo, "restore", "README.md")
    finish(repo, task_id=task["id"], lease=task["lease"])
    assert (git_repo / ".solo-ai" / "config.toml").exists()
    assert (git_repo / "task.txt").exists()


def test_existing_workflow_defers_with_zero_policy_writes(git_repo: Path) -> None:
    marker = git_repo / "scripts" / "worktree-flow.ps1"
    marker.parent.mkdir()
    marker.write_text("# existing\n", encoding="utf-8")
    git(git_repo, "add", "scripts/worktree-flow.ps1")
    git(git_repo, "commit", "-m", "add existing workflow")
    repo = GitRepo(git_repo)
    result = initialize(
        repo, slots=3, commands=[VERIFY], accept=True, accept_static_only=False
    )
    assert result == {
        "decision": "deferred",
        "reason": "existing-workflow",
        "workflows": ["repository worktree-flow"],
    }
    assert not (git_repo / ".solo-ai").exists()
    assert not (git_repo / "AGENTS.md").exists()
    assert not repo.local_dir.exists()


def test_decline_is_local_and_blocks_future_start(git_repo: Path) -> None:
    repo = GitRepo(git_repo)
    result = initialize(
        repo,
        slots=3,
        commands=[VERIFY],
        accept=False,
        accept_static_only=False,
        decline=True,
    )
    assert result["decision"] == "declined"
    assert not (git_repo / ".solo-ai").exists()
    with pytest.raises(SoloAIError, match="disabled"):
        start(repo, name="must not start")
    set_local_enabled(repo, enabled=True)


def test_exact_path_manifest_blocks_unknown_changes(git_repo: Path) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="two changes")
    worktree = Path(task["worktree"])
    (worktree / "one.txt").write_text("one\n", encoding="utf-8")
    (worktree / "two.txt").write_text("two\n", encoding="utf-8")
    with pytest.raises(SoloAIError, match="Exact staging manifest"):
        commit_task(
            repo,
            task_id=task["id"],
            lease=task["lease"],
            message="feat: partial",
            paths=["one.txt"],
        )
    assert "one.txt" in repo.changed_paths(worktree)
    abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])


def test_sensitive_candidate_is_blocked_and_preserved(git_repo: Path) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="unsafe task")
    worktree = Path(task["worktree"])
    (worktree / ".env.production").write_text(
        "PASSWORD=not-for-commit\n", encoding="utf-8"
    )
    with pytest.raises(SoloAIError, match="Sensitive-content gate"):
        commit_task(
            repo,
            task_id=task["id"],
            lease=task["lease"],
            message="feat: bad",
            paths=[".env.production"],
        )
    assert (worktree / ".env.production").exists()


def test_validation_policy_change_requires_full_local_reapproval(
    git_repo: Path,
) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="change validation")
    worktree = Path(task["worktree"])
    verification = worktree / ".solo-ai" / "verification.toml"
    verification.write_text(
        """schema_version = 2
static_only = false

[[profiles]]
id = "default"
paths = ["**"]
cross_task_reuse = false
external_state = "unknown"
input_paths = ["**"]
environment = []
commands = [["git", "diff", "--check"]]
""",
        encoding="utf-8",
    )
    commit_task(
        repo,
        task_id=task["id"],
        lease=task["lease"],
        message="chore: change validation",
        paths=[".solo-ai/verification.toml"],
    )
    with pytest.raises(SoloAIError, match="not approved"):
        ready(repo, task_id=task["id"], lease=task["lease"])
    approve(repo, load_verification_config(repo, cwd=worktree), cwd=worktree)
    assert ready(repo, task_id=task["id"], lease=task["lease"])["status"] == "ready"
    abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])


def test_status_never_reveals_lease_and_recover_rejects_live_operation(
    git_repo: Path,
) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="lease")
    assert task["lease"] not in json.dumps(
        {"tasks": [StateStore.public_task(StateStore(repo).task(task["id"]))]}
    )
    with StateStore(repo).operation(task["id"], task["lease"], "test"):
        with pytest.raises(SoloAIError, match="live operation"):
            StateStore(repo).recover(task["id"])
    recovered = StateStore(repo).recover(task["id"])
    assert recovered["lease"] != task["lease"]
    abandon(repo, task_id=task["id"], lease=recovered["lease"], confirm=task["id"])


def test_cross_task_profile_reuse_is_off_by_default(git_repo: Path) -> None:
    repo = initialized(git_repo)
    first = start(repo, name="first")
    commit_one(repo, first, "same.txt", "same\n", "test: first")
    first_ready = ready(repo, task_id=first["id"], lease=first["lease"])
    second = start(repo, name="second")
    commit_one(repo, second, "same.txt", "same\n", "test: second")
    second_ready = ready(repo, task_id=second["id"], lease=second["lease"])
    assert first_ready["ready_proof"] != second_ready["ready_proof"]
    proof = json.loads(
        (repo.local_dir / "proofs" / f"{second_ready['ready_proof']}.json").read_text(
            encoding="utf-8"
        )
    )
    assert proof["profile_proofs"][0]["reused"] is False
    abandon(repo, task_id=first["id"], lease=first["lease"], confirm=first["id"])
    abandon(repo, task_id=second["id"], lease=second["lease"], confirm=second["id"])


def test_deinit_removes_only_exact_adopted_policy_and_slots(git_repo: Path) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="discard before uninstall")
    abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])
    result = deinit(
        repo, confirm="DEINIT", message="chore: remove local worktree workflow"
    )
    assert result["status"] == "deinitialized"
    assert not (git_repo / ".solo-ai").exists()
    assert not (git_repo / "AGENTS.md").exists()
    assert not repo.local_dir.exists()


def test_prune_slot_removes_only_declared_local_dependency_paths(
    git_repo: Path,
) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="prepare local cache")
    worktree = Path(task["worktree"])
    abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])
    (worktree / ".venv").mkdir()
    (worktree / ".venv" / "marker").write_text("cache", encoding="utf-8")
    result = _prune(repo, kind="slot", slot="01")
    assert result["worktree_retained"] is True
    assert not (worktree / ".venv").exists()
    assert (worktree / "README.md").exists()


def test_dev_supervisor_owns_and_stops_http_process_tree(git_repo: Path) -> None:
    repo = initialized(git_repo)
    config = git_repo / ".solo-ai" / "config.toml"
    config.write_text(
        config.read_text(encoding="utf-8")
        + f"""\ndev_start = [{json.dumps(sys.executable)}, "-m", "http.server", "{{port}}"]\n\n[lifecycle.readiness]\nkind = "tcp"\ntarget = "127.0.0.1:{{port}}"\ntimeout_seconds = 10\n""",
        encoding="utf-8",
    )
    git(git_repo, "add", ".solo-ai/config.toml")
    git(git_repo, "commit", "-m", "chore: add local development command")
    approve(repo, load_verification_config(repo))
    task = start(repo, name="run development server")
    started = dev_start(repo, task_id=task["id"], lease=task["lease"])
    assert started["port"] in range(20000, 20100)
    assert started["supervisor_pid"] > 0
    dev_stop(repo, task_id=task["id"], lease=task["lease"])
    time.sleep(0.1)
    assert not StateStore(repo).task(task["id"])["processes"]
    with socket.socket() as connection:
        assert connection.connect_ex(("127.0.0.1", started["port"])) != 0
    abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])
