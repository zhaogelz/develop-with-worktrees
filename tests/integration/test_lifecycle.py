from __future__ import annotations

import json
import socket
import sys
import threading
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
    disable,
    dev_start,
    dev_stop,
    finish,
    initialize,
    local_enabled,
    ready,
    set_local_enabled,
    start,
    warm_slot,
)
import solo_ai.lifecycle as lifecycle
from solo_ai.repo import GitRepo
from solo_ai.state import StateStore
from solo_ai.util import SoloAIError

from conftest import git


VERIFY = CommandSpec(("git", "diff", "--check", "main...HEAD"))
STRICT_VERIFY = CommandSpec(("git", "status", "--short"))


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


def merge_verification_policy(repo: GitRepo, command: CommandSpec) -> None:
    task = start(repo, name="change validation policy")
    worktree = Path(task["worktree"])
    (worktree / ".solo-ai" / "verification.toml").write_text(
        f"""schema_version = 2
static_only = false

[[profiles]]
id = "default"
paths = ["**"]
cross_task_reuse = false
external_state = "unknown"
input_paths = ["**"]
environment = []
commands = [{json.dumps(list(command.argv))}]
""",
        encoding="utf-8",
    )
    commit_task(
        repo,
        task_id=task["id"],
        lease=task["lease"],
        message="test: change verification policy",
        paths=[".solo-ai/verification.toml"],
    )
    approve(repo, load_verification_config(repo, cwd=worktree), cwd=worktree)
    ready(repo, task_id=task["id"], lease=task["lease"])
    finish(repo, task_id=task["id"], lease=task["lease"])


def proof_commands(repo: GitRepo, fingerprint: str) -> list[list[str] | None]:
    proof = json.loads(
        (repo.local_dir / "proofs" / f"{fingerprint}.json").read_text(encoding="utf-8")
    )
    return [item["command"] for item in proof["runs"]]


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


def test_disable_refuses_to_strand_an_active_task(git_repo: Path) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="finish before disabling")

    with pytest.raises(SoloAIError, match="Active or quarantined tasks"):
        disable(repo)

    assert local_enabled(repo) is True
    abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])
    assert disable(repo)["enabled"] is False


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


def test_ready_uses_policy_after_default_branch_synchronization(git_repo: Path) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="candidate before policy update")
    commit_one(repo, task, "candidate.txt", "candidate\n", "test: candidate")

    merge_verification_policy(repo, STRICT_VERIFY)

    prepared = ready(repo, task_id=task["id"], lease=task["lease"])
    assert proof_commands(repo, prepared["ready_proof"]) == [list(STRICT_VERIFY.argv)]
    abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])


def test_finish_uses_policy_after_default_branch_synchronization(
    git_repo: Path,
) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="ready candidate before policy update")
    commit_one(repo, task, "candidate.txt", "candidate\n", "test: candidate")
    ready(repo, task_id=task["id"], lease=task["lease"])

    merge_verification_policy(repo, STRICT_VERIFY)

    result = finish(repo, task_id=task["id"], lease=task["lease"])
    assert proof_commands(repo, result["proof"]) == [list(STRICT_VERIFY.argv)]


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


def test_deinit_preflights_slot_before_removing_tracked_policy(git_repo: Path) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="preserve unexpected slot file")
    worktree = Path(task["worktree"])
    abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])
    marker = worktree / "user-note.txt"
    marker.write_text("preserve me\n", encoding="utf-8")

    with pytest.raises(SoloAIError, match="Dirty managed slot"):
        deinit(repo, confirm="DEINIT", message="chore: remove local worktree workflow")

    assert (git_repo / ".solo-ai" / "config.toml").exists()
    assert marker.read_text(encoding="utf-8") == "preserve me\n"


def test_deinit_keeps_policy_when_a_slot_changes_after_preflight(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="simulate concurrent slot write")
    worktree = Path(task["worktree"])
    abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])
    original = lifecycle._assert_removable_managed_slot
    checks = 0

    def fail_on_second_check(current_repo: GitRepo, path: Path) -> bool:
        nonlocal checks
        if path == worktree:
            checks += 1
            if checks == 2:
                raise SoloAIError("simulated concurrent slot write")
        return original(current_repo, path)

    monkeypatch.setattr(
        lifecycle, "_assert_removable_managed_slot", fail_on_second_check
    )
    with pytest.raises(SoloAIError, match="simulated concurrent"):
        deinit(repo, confirm="DEINIT", message="chore: remove local worktree workflow")

    assert (git_repo / ".solo-ai" / "config.toml").exists()
    assert repo.local_dir.exists()
    assert worktree.exists()


def test_deinit_restores_earlier_slot_when_a_later_slot_changes(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized(git_repo)
    first = start(repo, name="create first idle slot")
    first_worktree = Path(first["worktree"])
    abandon(repo, task_id=first["id"], lease=first["lease"], confirm=first["id"])
    second = start(repo, name="create second idle slot")
    second_worktree = Path(second["worktree"])
    abandon(repo, task_id=second["id"], lease=second["lease"], confirm=second["id"])

    original = lifecycle._assert_removable_managed_slot
    second_checks = 0

    def fail_when_second_slot_is_rechecked(current_repo: GitRepo, path: Path) -> bool:
        nonlocal second_checks
        if path == second_worktree:
            second_checks += 1
            if second_checks == 2:
                raise SoloAIError("simulated later slot write")
        return original(current_repo, path)

    monkeypatch.setattr(
        lifecycle, "_assert_removable_managed_slot", fail_when_second_slot_is_rechecked
    )
    with pytest.raises(SoloAIError, match="simulated later slot write"):
        deinit(repo, confirm="DEINIT", message="chore: remove local worktree workflow")

    assert (git_repo / ".solo-ai" / "config.toml").exists()
    assert first_worktree.exists()
    assert any(item.path == first_worktree for item in repo.worktrees())
    assert second_worktree.exists()


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


def test_prune_slot_retains_an_unregistered_slot_directory(git_repo: Path) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="orphaned slot")
    worktree = Path(task["worktree"])
    abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])
    repo.git(["worktree", "remove", "--force", str(worktree)], cwd=git_repo)
    worktree.mkdir()
    marker = worktree / "user-note.txt"
    marker.write_text("preserve me\n", encoding="utf-8")

    with pytest.raises(SoloAIError, match="not registered"):
        _prune(repo, kind="slot", slot="01")

    assert marker.read_text(encoding="utf-8") == "preserve me\n"


def test_warm_slot_blocks_concurrent_start(git_repo: Path) -> None:
    repo = initialized(git_repo)
    config = git_repo / ".solo-ai" / "config.toml"
    command = [sys.executable, "-c", "import time; time.sleep(1.5)"]
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "\n[lifecycle]\n", f"\nwarm = [{json.dumps(command)}]\n\n[lifecycle]\n"
        ),
        encoding="utf-8",
    )
    git(git_repo, "add", ".solo-ai/config.toml")
    git(git_repo, "commit", "-m", "test: add slow warm command")
    approve(repo, load_verification_config(repo))
    errors: list[Exception] = []

    def run_warm() -> None:
        try:
            warm_slot(repo, slot_id="01")
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=run_warm)
    worker.start()
    lock_path = repo.common_dir / "solo-ai-maintenance.lock"
    deadline = time.monotonic() + 5
    while not lock_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert lock_path.exists()
    with pytest.raises(SoloAIError, match="Operation is already active"):
        start(repo, name="must wait for warm slot")
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert errors == []


def test_warm_slot_syncs_an_idle_slot_to_latest_default(git_repo: Path) -> None:
    repo = initialized(git_repo)
    stale = start(repo, name="create stale idle slot")
    worktree = Path(stale["worktree"])
    abandoned = abandon(
        repo, task_id=stale["id"], lease=stale["lease"], confirm=stale["id"]
    )
    assert abandoned["status"] == "abandoned"
    old_head = repo.head(worktree)

    update = start(repo, name="advance default branch")
    commit_one(repo, update, "latest.txt", "latest\n", "test: advance default")
    ready(repo, task_id=update["id"], lease=update["lease"])
    finish(repo, task_id=update["id"], lease=update["lease"])

    warm_slot(repo, slot_id="01")
    assert old_head != repo.head(git_repo)
    assert repo.head(worktree) == repo.head(git_repo)


def test_warm_slot_quarantines_a_slot_that_changes_source(git_repo: Path) -> None:
    repo = GitRepo(git_repo)
    initialize(repo, slots=1, commands=[VERIFY], accept=True, accept_static_only=False)
    config = git_repo / ".solo-ai" / "config.toml"
    command = [
        sys.executable,
        "-c",
        "from pathlib import Path; Path('generated-by-warm.txt').write_text('x', encoding='utf-8')",
    ]
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "\n[lifecycle]\n", f"\nwarm = [{json.dumps(command)}]\n\n[lifecycle]\n"
        ),
        encoding="utf-8",
    )
    git(git_repo, "add", ".solo-ai/config.toml")
    git(git_repo, "commit", "-m", "test: configure unsafe warm command")
    approve(repo, load_verification_config(repo))

    with pytest.raises(SoloAIError, match="modified source or protected files"):
        warm_slot(repo, slot_id="01")

    state = StateStore(repo).read()
    worktree = Path(state["slots"]["01"]["path"])
    assert state["slots"]["01"]["status"] == "quarantined"
    assert (worktree / "generated-by-warm.txt").exists()
    with pytest.raises(SoloAIError, match="All managed worktree slots are busy"):
        start(repo, name="must not allocate dirty warm slot")


def test_worktree_directory_change_is_rejected_before_task_allocation(
    git_repo: Path,
) -> None:
    repo = initialized(git_repo)
    config = git_repo / ".solo-ai" / "config.toml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            'worktree_directory = ".worktrees"',
            'worktree_directory = ".new-worktrees"',
        ),
        encoding="utf-8",
    )
    git(git_repo, "add", ".solo-ai/config.toml")
    git(git_repo, "commit", "-m", "test: change worktree directory")
    approve(repo, load_verification_config(repo))

    with pytest.raises(SoloAIError, match="worktree_directory is immutable"):
        start(repo, name="must not create a stranded task")

    state = StateStore(repo).read()
    assert state["tasks"] == {}
    assert state["slots"]["01"]["status"] == "idle"


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
