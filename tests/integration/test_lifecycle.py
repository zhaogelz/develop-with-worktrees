from __future__ import annotations

import json
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

from solo_ai.config import CommandSpec, load_repo_config, load_verification_config
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
    retarget,
    set_local_enabled,
    start,
    warm_slot,
)
import solo_ai.lifecycle as lifecycle
from solo_ai.repo import GitRepo
from solo_ai.state import StateStore
from solo_ai.util import SoloAIError, atomic_write_json, process_snapshot, read_json

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


def test_finish_retains_standard_tool_caches_created_by_validation(
    git_repo: Path,
) -> None:
    cache_command = CommandSpec(
        (
            sys.executable,
            "-c",
            "from pathlib import Path\n"
            "for name in ('.pytest_cache', '.ruff_cache'):\n"
            "    cache = Path(name)\n"
            "    cache.mkdir(exist_ok=True)\n"
            "    (cache / 'marker').write_text('cache', encoding='utf-8')\n",
        )
    )
    (git_repo / ".gitignore").write_text(
        ".pytest_cache/\n.ruff_cache/\n", encoding="utf-8"
    )
    git(git_repo, "add", ".gitignore")
    git(git_repo, "commit", "-m", "test: ignore standard tool caches")
    repo = GitRepo(git_repo)
    initialize(
        repo, slots=3, commands=[cache_command], accept=True, accept_static_only=False
    )
    task = start(repo, name="validate with standard caches")
    commit_one(repo, task, "cached.txt", "cached\n", "test: cache validation")

    ready(repo, task_id=task["id"], lease=task["lease"])
    result = finish(repo, task_id=task["id"], lease=task["lease"])

    worktree = Path(task["worktree"])
    assert result["integrated_head"] == repo.head(git_repo)
    assert (worktree / ".pytest_cache" / "marker").read_text(
        encoding="utf-8"
    ) == "cache"
    assert (worktree / ".ruff_cache" / "marker").read_text(encoding="utf-8") == "cache"


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


def test_commit_requires_both_paths_for_a_staged_rename(git_repo: Path) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="rename an exact path")
    worktree = Path(task["worktree"])
    repo.git(["mv", "README.md", "RENAMED.md"], cwd=worktree)

    with pytest.raises(SoloAIError, match="README.md"):
        commit_task(
            repo,
            task_id=task["id"],
            lease=task["lease"],
            message="test: incomplete rename manifest",
            paths=["RENAMED.md"],
        )

    commit_task(
        repo,
        task_id=task["id"],
        lease=task["lease"],
        message="test: rename exact path",
        paths=["README.md", "RENAMED.md"],
    )
    assert repo.is_clean(worktree)
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


def test_recover_rejects_live_validation_and_marks_stale_receipt_interrupted(
    git_repo: Path,
) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="validation recovery")
    receipt_path = repo.local_dir / "validation-runs" / "test" / "01.json"
    atomic_write_json(
        receipt_path,
        {
            "schema_version": 1,
            "status": "running",
            "process": process_snapshot(),
            "metadata": {"task_id": task["id"]},
        },
    )
    with pytest.raises(SoloAIError, match="live validation"):
        StateStore(repo).recover(task["id"])

    stale = read_json(receipt_path, {})
    stale["process"] = {"pid": -1}
    atomic_write_json(receipt_path, stale)
    recovered = StateStore(repo).recover(task["id"])
    assert recovered["lease"] != task["lease"]
    assert read_json(receipt_path, {})["status"] == "interrupted"
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


def test_start_uses_current_branch_and_finish_integrates_back_to_it(
    git_repo: Path,
) -> None:
    repo = initialized(git_repo)
    git(git_repo, "switch", "-c", "release")

    task = start(repo, name="release hotfix")
    assert task["base_ref"] == "release"
    assert Path(task["base_worktree"]) == git_repo.resolve()
    commit_one(repo, task, "release.txt", "release\n", "fix: release hotfix")
    ready(repo, task_id=task["id"], lease=task["lease"])
    finish(repo, task_id=task["id"], lease=task["lease"])

    assert repo.branch(git_repo) == "release"
    assert (git_repo / "release.txt").read_text(encoding="utf-8") == "release\n"


def test_start_rejects_a_child_of_an_active_managed_task(git_repo: Path) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="parent")

    with pytest.raises(SoloAIError, match="child task"):
        start(GitRepo(Path(task["worktree"])), name="child")

    abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])


def test_rewritten_base_blocks_ready_until_explicit_retarget(git_repo: Path) -> None:
    repo = initialized(git_repo)
    (git_repo / "base.txt").write_text("base\n", encoding="utf-8")
    git(git_repo, "add", "base.txt")
    git(git_repo, "commit", "-m", "test: advance base")
    task = start(repo, name="candidate with rewritten base")
    commit_one(repo, task, "candidate.txt", "candidate\n", "test: candidate")

    git(git_repo, "reset", "--hard", "HEAD~1")
    with pytest.raises(SoloAIError, match="rewritten"):
        ready(repo, task_id=task["id"], lease=task["lease"])

    retarget(
        repo,
        task_id=task["id"],
        lease=task["lease"],
        base="main",
        confirm=f"{task['id']}:main",
    )
    assert ready(repo, task_id=task["id"], lease=task["lease"])["status"] == "ready"
    abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])


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
    plan = _prune(repo, kind="slot", slot="01")
    result = _prune(
        repo,
        kind="slot",
        slot="01",
        plan_id=plan["plan_id"],
        confirm=plan["digest"],
    )
    assert result["worktree_retained"] is True
    assert not (worktree / ".venv").exists()
    assert (worktree / "README.md").exists()


def test_prune_slot_rejects_a_plan_when_declared_target_changed(git_repo: Path) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="prepare changing cache")
    worktree = Path(task["worktree"])
    abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])
    (worktree / ".venv").mkdir()
    (worktree / ".venv" / "before").write_text("before", encoding="utf-8")
    plan = _prune(repo, kind="slot", slot="01")
    (worktree / ".venv" / "after").write_text("after", encoding="utf-8")

    with pytest.raises(SoloAIError, match="changed after review"):
        _prune(
            repo,
            kind="slot",
            slot="01",
            plan_id=plan["plan_id"],
            confirm=plan["digest"],
        )

    assert (worktree / ".venv" / "before").exists()
    assert (worktree / ".venv" / "after").exists()


def test_prune_slot_retains_protected_paths_even_when_other_cleanup_runs(
    git_repo: Path,
) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="preserve credentials")
    worktree = Path(task["worktree"])
    abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])
    (worktree / ".venv").mkdir()
    (worktree / ".env.local").write_text("marker=kept\n", encoding="utf-8")

    plan = _prune(repo, kind="slot", slot="01")
    result = _prune(
        repo,
        kind="slot",
        slot="01",
        plan_id=plan["plan_id"],
        confirm=plan["digest"],
    )

    assert not (worktree / ".venv").exists()
    assert (worktree / ".env.local").read_text(encoding="utf-8") == "marker=kept\n"
    assert result["protected_retained"] == [".env.local"]


def test_slot_configuration_can_expand_to_six_without_reinitializing(
    git_repo: Path,
) -> None:
    repo = initialized(git_repo)
    config_path = git_repo / ".solo-ai" / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace("slots = 3", "slots = 6"),
        encoding="utf-8",
    )

    state = StateStore(repo).ensure_slots(load_repo_config(repo))
    assert state["slots"]["06"]["status"] == "idle"


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


def test_ready_rejects_worktree_directory_change_before_it_can_integrate(
    git_repo: Path,
) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="must not change the slot root")
    worktree = Path(task["worktree"])
    config = worktree / ".solo-ai" / "config.toml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            'worktree_directory = ".worktrees"',
            'worktree_directory = ".new-worktrees"',
        ),
        encoding="utf-8",
    )
    commit_task(
        repo,
        task_id=task["id"],
        lease=task["lease"],
        message="test: change slot root",
        paths=[".solo-ai/config.toml"],
    )
    approve(repo, load_verification_config(repo, cwd=worktree), cwd=worktree)

    with pytest.raises(SoloAIError, match="worktree_directory is immutable"):
        ready(repo, task_id=task["id"], lease=task["lease"])

    assert StateStore(repo).task(task["id"])["status"] == "active"
    assert 'worktree_directory = ".worktrees"' in (
        git_repo / ".solo-ai" / "config.toml"
    ).read_text(encoding="utf-8")


def test_ready_rejects_invalid_branch_prefix_before_it_can_integrate(
    git_repo: Path,
) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="must not change prefix to an invalid branch")
    worktree = Path(task["worktree"])
    config = worktree / ".solo-ai" / "config.toml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            'branch_prefix = "codex/"',
            'branch_prefix = "bad..prefix/"',
        ),
        encoding="utf-8",
    )
    commit_task(
        repo,
        task_id=task["id"],
        lease=task["lease"],
        message="test: change branch prefix",
        paths=[".solo-ai/config.toml"],
    )
    approve(repo, load_verification_config(repo, cwd=worktree), cwd=worktree)

    with pytest.raises(SoloAIError, match="branch_prefix"):
        ready(repo, task_id=task["id"], lease=task["lease"])

    assert StateStore(repo).task(task["id"])["status"] == "active"
    assert 'branch_prefix = "codex/"' in (
        git_repo / ".solo-ai" / "config.toml"
    ).read_text(encoding="utf-8")


def test_finish_rejects_slot_directory_change_arriving_from_default(
    git_repo: Path,
) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="candidate protected from changed default policy")
    commit_one(repo, task, "candidate.txt", "candidate\n", "test: candidate")
    ready(repo, task_id=task["id"], lease=task["lease"])

    config = git_repo / ".solo-ai" / "config.toml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            'worktree_directory = ".worktrees"',
            'worktree_directory = ".new-worktrees"',
        ),
        encoding="utf-8",
    )
    git(git_repo, "add", ".solo-ai/config.toml")
    git(git_repo, "commit", "-m", "test: externally change slot root")
    approve(repo, load_verification_config(repo))

    with pytest.raises(SoloAIError, match="worktree_directory is immutable"):
        finish(repo, task_id=task["id"], lease=task["lease"])

    assert not (git_repo / "candidate.txt").exists()
    assert StateStore(repo).task(task["id"])["status"] == "ready"


def test_dev_supervisor_owns_and_stops_tcp_process_tree(git_repo: Path) -> None:
    repo = initialized(git_repo)
    listener_command = (
        "import socket,time;listener=socket.socket();"
        "listener.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);"
        "listener.bind(('127.0.0.1',{port}));listener.listen();time.sleep(30)"
    )
    config = git_repo / ".solo-ai" / "config.toml"
    config.write_text(
        config.read_text(encoding="utf-8")
        + f"""\ndev_start = [{json.dumps(sys.executable)}, "-c", {json.dumps(listener_command)}]\n\n[lifecycle.readiness]\nkind = "tcp"\ntarget = "127.0.0.1:{{port}}"\ntimeout_seconds = 10\n""",
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


def test_dev_start_fails_fast_when_first_free_port_never_becomes_ready(
    git_repo: Path,
) -> None:
    repo = initialized(git_repo)
    config = git_repo / ".solo-ai" / "config.toml"
    config.write_text(
        config.read_text(encoding="utf-8")
        + f"""\ndev_start = [{json.dumps(sys.executable)}, "-c", "import time; time.sleep(5)"]

[lifecycle.readiness]
kind = "tcp"
target = "127.0.0.1:{{port}}"
timeout_seconds = 0.2
""",
        encoding="utf-8",
    )
    git(git_repo, "add", ".solo-ai/config.toml")
    git(git_repo, "commit", "-m", "chore: add an unready development command")
    approve(repo, load_verification_config(repo))
    task = start(repo, name="reject unready development server")

    started_at = time.monotonic()
    with pytest.raises(SoloAIError, match="did not become ready on port"):
        dev_start(repo, task_id=task["id"], lease=task["lease"])
    assert time.monotonic() - started_at < 3
    assert not StateStore(repo).task(task["id"])["processes"]
    abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])
