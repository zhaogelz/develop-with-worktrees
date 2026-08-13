from __future__ import annotations

import json
import os
import socket
import subprocess
import shutil
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from conftest import git
from solo_ai import lifecycle
from solo_ai import abandonment as abandonment_module
from solo_ai import integration as integration_module
from solo_ai import proof as proof_module
from solo_ai import state as state_module
from solo_ai.cli import _prune
from solo_ai import cli as cli_module
from solo_ai import cleanup as cleanup_module
from solo_ai.config import CommandSpec, load_repo_config, load_verification_config
from solo_ai.lifecycle import (
    abandon,
    approve,
    choose,
    commit_task,
    deinit,
    dev_start,
    dev_stop,
    disable,
    finish,
    initialize,
    local_enabled,
    ready,
    recover,
    resume_in_place,
    retarget,
    set_local_enabled,
    start,
    task_bypass_active,
    warm_slot,
)
from solo_ai.repo import GitRepo
from solo_ai.state import StateStore
from solo_ai.util import SoloAIError, atomic_write_json, process_snapshot, read_json

VERIFY = CommandSpec(("git", "diff", "--check", "main...HEAD"))
STRICT_VERIFY = CommandSpec(("git", "status", "--short"))


def initialized(path: Path) -> GitRepo:
    repo = GitRepo(path)
    result = initialize(
        repo, slots=3, commands=[VERIFY], accept=True, accept_static_only=False
    )
    assert result["decision"] == "adopted"
    return repo


def declare_cleanup(repo: GitRepo, *owned_paths: str) -> None:
    config = repo.root / ".solo-ai" / "config.toml"
    rendered = ", ".join(json.dumps(path) for path in owned_paths)
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "cleanup = { owned_paths = [] }",
            f"cleanup = {{ owned_paths = [{rendered}] }}",
        ),
        encoding="utf-8",
    )
    git(repo.root, "add", ".solo-ai/config.toml")
    git(repo.root, "commit", "-m", "test: declare local cleanup ownership")
    approve(repo, load_verification_config(repo))


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
        f"""schema_version = 3
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


def install_counting_verification_policy(repo: GitRepo, command: list[str]) -> None:
    verification = repo.root / ".solo-ai" / "verification.toml"
    verification.write_text(
        f"""schema_version = 3
static_only = false

[[profiles]]
id = "default"
paths = ["candidate.txt"]
cross_task_reuse = false
external_state = "none"
input_paths = ["candidate.txt"]
environment = []
commands = [{json.dumps(command)}]
""",
        encoding="utf-8",
    )
    git(repo.root, "add", ".solo-ai/verification.toml")
    git(repo.root, "commit", "-m", "test: install counting verification")
    approve(repo, load_verification_config(repo))


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


def test_ready_rejects_candidate_committed_after_sensitive_gate(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="candidate gate race")
    worktree = Path(task["worktree"])
    commit_one(repo, task, "candidate.txt", "first\n", "test: first candidate")
    original_require_safe = lifecycle.require_safe

    def gate_then_commit(*args: object, **kwargs: object) -> None:
        original_require_safe(*args, **kwargs)
        (worktree / "late.txt").write_text("late\n", encoding="utf-8")
        git(worktree, "add", "late.txt")
        git(worktree, "commit", "-m", "test: late candidate")

    monkeypatch.setattr(lifecycle, "require_safe", gate_then_commit)
    with pytest.raises(SoloAIError, match="Candidate changed"):
        ready(repo, task_id=task["id"], lease=task["lease"])
    assert StateStore(repo).task(task["id"])["status"] == "active"


def test_in_place_lifecycle_preserves_current_branch_and_uses_immutable_start_head(
    git_repo: Path,
) -> None:
    repo = initialized(git_repo)
    start_head = repo.head(git_repo)
    task = start(
        repo, name="use current test state", in_place=True, session_id="codex-a"
    )

    assert task["mode"] == "in-place"
    assert task["slot_id"] is None
    assert task["branch"] == "main"
    assert task["start_head"] == start_head
    (git_repo / "current.txt").write_text("current\n", encoding="utf-8")
    committed = commit_task(
        repo,
        task_id=task["id"],
        lease=task["lease"],
        message="test: current worktree task",
        paths=["current.txt"],
        session_id="codex-a",
    )
    assert committed["expected_head"] == repo.head(git_repo)
    prepared = ready(
        repo, task_id=task["id"], lease=task["lease"], session_id="codex-a"
    )
    proof = read_json(repo.local_dir / "proofs" / f"{prepared['ready_proof']}.json", {})
    assert proof["inputs"]["files"] == ["current.txt"]
    (git_repo / "current-second.txt").write_text("second\n", encoding="utf-8")
    revised = commit_task(
        repo,
        task_id=task["id"],
        lease=task["lease"],
        message="test: revise ready current worktree task",
        paths=["current-second.txt"],
        session_id="codex-a",
    )
    assert revised["status"] == "active"
    assert revised["ready_proof"] is None
    with pytest.raises(SoloAIError, match="requires a successful Ready"):
        finish(repo, task_id=task["id"], lease=task["lease"], session_id="codex-a")
    ready(repo, task_id=task["id"], lease=task["lease"], session_id="codex-a")
    result = finish(repo, task_id=task["id"], lease=task["lease"], session_id="codex-a")

    assert result["mode"] == "in-place"
    assert repo.branch(git_repo) == "main"
    assert (git_repo / "current.txt").exists()
    assert (git_repo / "current-second.txt").exists()
    assert StateStore(repo).task(task["id"])["status"] == "finished"


def test_in_place_session_mismatch_quarantines_without_cleaning_files(
    git_repo: Path,
) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="bound session", in_place=True, session_id="codex-a")
    (git_repo / "preserved.txt").write_text("keep\n", encoding="utf-8")

    with pytest.raises(SoloAIError, match="quarantined"):
        commit_task(
            repo,
            task_id=task["id"],
            lease=task["lease"],
            message="test: wrong session",
            paths=["preserved.txt"],
            session_id="codex-b",
        )

    assert (git_repo / "preserved.txt").read_text(encoding="utf-8") == "keep\n"
    assert StateStore(repo).task(task["id"])["status"] == "quarantined"
    with pytest.raises(SoloAIError, match="requires an active"):
        commit_task(
            repo,
            task_id=task["id"],
            lease=task["lease"],
            message="test: original session cannot revive task",
            paths=["preserved.txt"],
            session_id="codex-a",
        )
    with pytest.raises(SoloAIError, match="cannot enter Ready"):
        ready(repo, task_id=task["id"], lease=task["lease"], session_id="codex-a")
    git(git_repo, "add", "preserved.txt")
    git(git_repo, "commit", "-m", "test: manually preserve direct work")
    with pytest.raises(SoloAIError, match="still differs"):
        resume_in_place(
            repo,
            task_id=task["id"],
            session_id="codex-c",
            confirm=f"{task['id']}:main:{task['expected_head']}",
        )


def test_active_in_place_task_blocks_only_same_base_integration(
    git_repo: Path,
) -> None:
    repo = initialized(git_repo)
    direct = start(
        repo, name="urgent current state", in_place=True, session_id="codex-a"
    )
    isolated = start(repo, name="parallel isolated work")
    commit_one(repo, isolated, "parallel.txt", "parallel\n", "test: parallel change")
    ready(repo, task_id=isolated["id"], lease=isolated["lease"])

    with pytest.raises(SoloAIError, match="in-place task is active"):
        finish(repo, task_id=isolated["id"], lease=isolated["lease"])

    (git_repo / "urgent.txt").write_text("urgent\n", encoding="utf-8")
    commit_task(
        repo,
        task_id=direct["id"],
        lease=direct["lease"],
        message="test: urgent current change",
        paths=["urgent.txt"],
        session_id="codex-a",
    )
    ready(repo, task_id=direct["id"], lease=direct["lease"], session_id="codex-a")
    finish(repo, task_id=direct["id"], lease=direct["lease"], session_id="codex-a")
    finish(repo, task_id=isolated["id"], lease=isolated["lease"])
    assert (git_repo / "parallel.txt").exists()


def test_in_place_start_rejects_an_active_isolated_worktree(git_repo: Path) -> None:
    repo = initialized(git_repo)
    isolated = start(repo, name="isolated owner")

    with pytest.raises(SoloAIError, match="active isolated"):
        start(
            GitRepo(Path(isolated["worktree"])),
            name="must not share slot",
            in_place=True,
            session_id="codex-a",
        )


def test_resume_in_place_explicitly_transfers_a_stalled_active_task(
    git_repo: Path,
) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="active current state", in_place=True, session_id="codex-a")
    store = StateStore(repo)
    store.update_task(task["id"], status="ready", ready_proof={"fingerprint": "stale"})
    before = store.task(task["id"])

    resumed = resume_in_place(
        repo,
        task_id=task["id"],
        session_id="codex-b",
        confirm=f"{task['id']}:main:{task['expected_head']}",
    )

    assert resumed["status"] == "active"
    assert resumed["lease"] != before["lease"]
    assert resumed["session_fingerprint"] != before["session_fingerprint"]
    assert resumed["ready_proof"] is None


def test_resume_in_place_rejects_a_live_validation_process(git_repo: Path) -> None:
    repo = initialized(git_repo)
    task = start(
        repo, name="interrupted direct validation", in_place=True, session_id="codex-a"
    )
    StateStore(repo).quarantine(task["id"], "simulate interrupted Codex session")
    receipt = repo.local_dir / "validation-runs" / "direct" / "01.json"
    atomic_write_json(
        receipt,
        {
            "schema_version": 1,
            "status": "running",
            "process": process_snapshot(),
            "metadata": {"task_id": task["id"]},
        },
    )

    with pytest.raises(SoloAIError, match="live validation process"):
        resume_in_place(
            repo,
            task_id=task["id"],
            session_id="codex-b",
            confirm=f"{task['id']}:main:{task['expected_head']}",
        )


def test_schema_two_task_state_is_read_upgraded_before_isolated_finish(
    git_repo: Path,
) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="old local state")
    commit_one(repo, task, "state.txt", "state\n", "test: old state candidate")
    state_path = repo.local_dir / "state.json"
    old_state = read_json(state_path, {})
    old_state["schema_version"] = 2
    old_state.pop("pending_operation_outcomes", None)
    for item in old_state["tasks"].values():
        item.pop("mode", None)
        item.pop("integration", None)
        item.pop("abandonment", None)
    for item in old_state["slots"].values():
        item.pop("generation", None)
    atomic_write_json(state_path, old_state)

    assert StateStore(repo).task(task["id"])["mode"] == "isolated"
    ready(repo, task_id=task["id"], lease=task["lease"])
    finish(repo, task_id=task["id"], lease=task["lease"])
    assert read_json(state_path, {})["schema_version"] == 4


def test_schema_three_ready_task_already_in_main_recovers_without_second_merge(
    git_repo: Path,
) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="schema three promoted task")
    commit_one(repo, task, "legacy.txt", "legacy\n", "test: legacy candidate")
    candidate = ready(repo, task_id=task["id"], lease=task["lease"])[
        "candidate_head"
    ]
    git(git_repo, "merge", "--ff-only", candidate)
    state_path = repo.local_dir / "state.json"
    legacy = read_json(state_path, {})
    legacy["schema_version"] = 3
    legacy.pop("pending_operation_outcomes", None)
    for item in legacy["tasks"].values():
        item.pop("integration", None)
        item.pop("abandonment", None)
    for item in legacy["slots"].values():
        item.pop("generation", None)
    atomic_write_json(state_path, legacy)

    result = recover(repo, task_id=task["id"])
    finished = StateStore(repo).task(task["id"])
    assert result["integrated_head"] == candidate
    assert repo.head(git_repo) == candidate
    assert finished["status"] == "finished"
    assert finished["integration"]["transaction_id"].startswith("legacy-")
    assert read_json(state_path, {})["schema_version"] == 4
    upgraded = read_json(state_path, {})
    assert upgraded["pending_operation_outcomes"] == {}
    assert isinstance(upgraded["slots"][task["slot_id"]]["generation"], int)


def test_schema_one_receipt_is_strictly_migrated_for_finished_task(
    git_repo: Path,
) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="legacy receipt")
    commit_one(repo, task, "legacy-receipt.txt", "legacy\n", "test: old receipt")
    ready_result = ready(repo, task_id=task["id"], lease=task["lease"])
    finish(repo, task_id=task["id"], lease=task["lease"])
    finished = StateStore(repo).task(task["id"])
    StateStore(repo).update_task(task["id"], integration=None)
    atomic_write_json(
        repo.local_dir / "integration-receipts" / f"{task['id']}.json",
        {
            "schema_version": 1,
            "stage": "released",
            "task_id": task["id"],
            "branch": task["branch"],
            "base_ref": task["base_ref"],
            "candidate_head": ready_result["candidate_head"],
            "integrated_head": ready_result["candidate_head"],
            "proof": finished["ready_proof"],
            "updated_at": "2026-01-01T00:00:00Z",
        },
    )

    result = recover(repo, task_id=task["id"])
    receipt = read_json(
        repo.local_dir / "integration-receipts" / f"{task['id']}.json", {}
    )
    assert result["status"] == "completed"
    assert receipt["schema_version"] == 2
    assert receipt["transaction_id"].startswith("legacy-")


def test_finish_resumes_after_branch_cleanup_crash(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="resume finish")
    commit_one(repo, task, "resume.txt", "resume\n", "test: resume finish")
    ready(repo, task_id=task["id"], lease=task["lease"])
    original_complete = StateStore.complete_integration

    def fail_complete(
        self: StateStore, task_id: str, *, transaction_id: str
    ) -> dict[str, object]:
        raise RuntimeError("simulated crash before release")

    monkeypatch.setattr(StateStore, "complete_integration", fail_complete)
    with pytest.raises(RuntimeError, match="simulated crash"):
        finish(repo, task_id=task["id"], lease=task["lease"])

    failed = StateStore(repo).task(task["id"])
    assert failed["integration"]["phase"] == "promoted"
    assert failed["status"] == "finishing"
    assert repo.ref_head(f"refs/heads/{task['branch']}") is None

    monkeypatch.setattr(StateStore, "complete_integration", original_complete)
    result = finish(repo, task_id=task["id"], lease=task["lease"])
    assert result["task_id"] == task["id"]
    assert StateStore(repo).task(task["id"])["status"] == "finished"


def test_recover_uses_git_fact_after_merge_before_promoted_state(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="merge crash fact")
    commit_one(repo, task, "candidate.txt", "candidate\n", "test: exact candidate")
    prepared = ready(repo, task_id=task["id"], lease=task["lease"])
    candidate = prepared["candidate_head"]
    original_mark = StateStore.mark_integration_promoted

    def fail_mark(
        self: StateStore,
        task_id: str,
        *,
        transaction_id: str,
        observed_base_head: str,
    ) -> dict[str, object]:
        raise RuntimeError("simulated crash after merge")

    monkeypatch.setattr(StateStore, "mark_integration_promoted", fail_mark)
    with pytest.raises(RuntimeError, match="simulated crash after merge"):
        finish(repo, task_id=task["id"], lease=task["lease"])
    assert repo.is_ancestor(candidate, "main")
    failed = StateStore(repo).task(task["id"])
    assert failed["integration"]["phase"] == "prepared"

    monkeypatch.setattr(StateStore, "mark_integration_promoted", original_mark)
    (git_repo / "advance.txt").write_text("advance\n", encoding="utf-8")
    git(git_repo, "add", "advance.txt")
    git(git_repo, "commit", "-m", "test: advance main after crash")
    advanced_main = repo.head(git_repo)

    result = recover(repo, task_id=task["id"])
    assert result["integrated_head"] == candidate
    assert repo.head(git_repo) == advanced_main
    assert StateStore(repo).task(task["id"])["status"] == "finished"
    assert repo.branch(Path(task["worktree"])) is None
    assert repo.ref_head(f"refs/heads/{task['branch']}") is None


def test_recover_rejects_unknown_transaction_phase_before_git_write(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="unknown integration phase")
    commit_one(repo, task, "unknown-phase.txt", "candidate\n", "test: unknown phase")
    ready(repo, task_id=task["id"], lease=task["lease"])
    original_resume = lifecycle.resume_integration
    monkeypatch.setattr(
        lifecycle,
        "resume_integration",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("stop after prepare")),
    )
    with pytest.raises(RuntimeError, match="stop after prepare"):
        finish(repo, task_id=task["id"], lease=task["lease"])
    monkeypatch.setattr(lifecycle, "resume_integration", original_resume)
    state = StateStore(repo).read()
    state["tasks"][task["id"]]["integration"]["phase"] = "unknown"
    atomic_write_json(repo.local_dir / "state.json", state)
    main_before = repo.head(git_repo)

    with pytest.raises(SoloAIError, match="Unsupported integration transaction phase"):
        recover(repo, task_id=task["id"])
    assert repo.head(git_repo) == main_before
    assert StateStore(repo).task(task["id"])["integration"]["phase"] == "unknown"


def test_stale_prepared_recovery_preserves_changed_task_branch_identity(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="stale changed worktree")
    commit_one(repo, task, "stale.txt", "candidate\n", "test: stale candidate")
    ready(repo, task_id=task["id"], lease=task["lease"])
    original_resume = lifecycle.resume_integration
    monkeypatch.setattr(
        lifecycle,
        "resume_integration",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("stop after prepare")),
    )
    with pytest.raises(RuntimeError, match="stop after prepare"):
        finish(repo, task_id=task["id"], lease=task["lease"])
    monkeypatch.setattr(lifecycle, "resume_integration", original_resume)
    (git_repo / "advance-stale.txt").write_text("advance\n", encoding="utf-8")
    git(git_repo, "add", "advance-stale.txt")
    git(git_repo, "commit", "-m", "test: advance base outside candidate")
    worktree = Path(task["worktree"])
    candidate = repo.head(worktree)
    git(worktree, "switch", "-c", "other-stale", candidate)

    with pytest.raises(SoloAIError, match="candidate identity changed"):
        recover(repo, task_id=task["id"])
    preserved = StateStore(repo).task(task["id"])
    assert preserved["integration"]["phase"] == "prepared"
    assert repo.branch(worktree) == "other-stale"


def test_integration_recovery_rejects_same_path_worktree_replacement(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="replace finishing worktree")
    commit_one(repo, task, "replace.txt", "candidate\n", "test: replace worktree")
    ready(repo, task_id=task["id"], lease=task["lease"])
    original_resume = lifecycle.resume_integration
    monkeypatch.setattr(
        lifecycle,
        "resume_integration",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("stop after prepare")),
    )
    with pytest.raises(RuntimeError, match="stop after prepare"):
        finish(repo, task_id=task["id"], lease=task["lease"])
    monkeypatch.setattr(lifecycle, "resume_integration", original_resume)
    worktree = Path(task["worktree"])
    parked = worktree.with_name(worktree.name + "-integration-original")
    worktree.rename(parked)
    shutil.copytree(parked, worktree)

    with pytest.raises(SoloAIError, match="directory object was replaced"):
        recover(repo, task_id=task["id"])
    assert (parked / "replace.txt").read_text(encoding="utf-8") == "candidate\n"
    assert StateStore(repo).task(task["id"])["integration"]["phase"] == "prepared"


def test_recover_repairs_missing_completion_receipt_after_atomic_release(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="receipt crash")
    commit_one(repo, task, "receipt.txt", "receipt\n", "test: receipt candidate")
    ready(repo, task_id=task["id"], lease=task["lease"])
    original_write = integration_module.write_completed_receipt

    def fail_write(*args: object, **kwargs: object) -> dict[str, object]:
        raise RuntimeError("simulated receipt failure")

    monkeypatch.setattr(integration_module, "write_completed_receipt", fail_write)
    with pytest.raises(RuntimeError, match="simulated receipt failure"):
        finish(repo, task_id=task["id"], lease=task["lease"])
    completed = StateStore(repo).task(task["id"])
    assert completed["status"] == "finished"
    assert completed["integration"]["phase"] == "completed"

    monkeypatch.setattr(integration_module, "write_completed_receipt", original_write)
    result = recover(repo, task_id=task["id"])
    assert result["status"] == "completed"
    receipt = read_json(
        repo.local_dir / "integration-receipts" / f"{task['id']}.json", {}
    )
    assert receipt["status"] == "completed"
    assert receipt["integrated_head"] == completed["candidate_head"]


def test_recover_rejects_corrupted_completed_transaction_before_rebuilding_receipt(
    git_repo: Path,
) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="corrupt completed transaction")
    commit_one(repo, task, "completed.txt", "candidate\n", "test: completed candidate")
    ready(repo, task_id=task["id"], lease=task["lease"])
    finish(repo, task_id=task["id"], lease=task["lease"])
    receipt_path = repo.local_dir / "integration-receipts" / f"{task['id']}.json"
    receipt_path.unlink()
    state = StateStore(repo).read()
    original_candidate = state["tasks"][task["id"]]["candidate_head"]
    state["tasks"][task["id"]]["integration"]["candidate_head"] = state["tasks"][
        task["id"]
    ]["base_head"]
    atomic_write_json(repo.local_dir / "state.json", state)

    with pytest.raises(SoloAIError, match="identity changed: candidate_head"):
        recover(repo, task_id=task["id"])
    assert not receipt_path.exists()
    assert StateStore(repo).task(task["id"])["candidate_head"] == original_candidate


@pytest.mark.parametrize(
    "stage",
    (
        "after-prepare",
        "after-merge",
        "after-promoted",
        "after-detach",
        "after-ref-delete",
        "after-complete-before-release",
        "after-complete-state",
    ),
)
def test_public_recover_converges_after_real_finish_process_exit(
    git_repo: Path, stage: str
) -> None:
    repo = initialized(git_repo)
    task = start(repo, name=f"hard exit {stage}")
    commit_one(repo, task, "hard-exit.txt", stage + "\n", "test: hard exit candidate")
    candidate = ready(repo, task_id=task["id"], lease=task["lease"])[
        "candidate_head"
    ]
    source_root = str(Path(lifecycle.__file__).parent.parent)
    child = r'''
import os
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from solo_ai import integration as integration_module
from solo_ai import lifecycle as lifecycle_module
from solo_ai.lifecycle import finish
from solo_ai.repo import GitRepo
from solo_ai.state import StateStore

repo = GitRepo(Path(sys.argv[2]))
task_id, lease, stage = sys.argv[3:6]
if stage == "after-prepare":
    lifecycle_module.resume_integration = lambda *args, **kwargs: os._exit(86)
elif stage == "after-merge":
    StateStore.mark_integration_promoted = lambda *args, **kwargs: os._exit(86)
elif stage == "after-promoted":
    integration_module._cleanup = lambda *args, **kwargs: os._exit(86)
elif stage == "after-detach":
    GitRepo.delete_ref = lambda *args, **kwargs: os._exit(86)
elif stage == "after-ref-delete":
    StateStore.complete_integration = lambda *args, **kwargs: os._exit(86)
elif stage == "after-complete-before-release":
    original_complete = StateStore.complete_integration
    def complete_then_exit(self, task_id, *, transaction_id):
        original_complete(self, task_id, transaction_id=transaction_id)
        os._exit(86)
    StateStore.complete_integration = complete_then_exit
elif stage == "after-complete-state":
    original = integration_module.atomic_write_json
    def crash_receipt(path, value):
        if "integration-receipts" in str(path):
            os._exit(86)
        return original(path, value)
    integration_module.atomic_write_json = crash_receipt
finish(repo, task_id=task_id, lease=lease)
'''
    crashed = subprocess.run(
        [
            sys.executable,
            "-c",
            child,
            source_root,
            str(git_repo),
            task["id"],
            task["lease"],
            stage,
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=90,
    )
    assert crashed.returncode == 86, crashed.stderr
    crash_main = repo.head(git_repo)
    if stage == "after-prepare":
        assert not repo.is_ancestor(candidate, "main")
    else:
        assert repo.is_ancestor(candidate, "main")
    operation_path = next(
        path
        for path in (repo.local_dir / "operations").glob("*.json")
        if (operation := read_json(path, {})).get("task_id") == task["id"]
        and operation.get("kind") == "finish"
    )
    assert read_json(operation_path, {})["status"] == "running"

    if stage == "after-merge":
        runner = (
            Path(__file__).parents[2]
            / "plugins"
            / "develop-with-worktrees"
            / "skills"
            / "develop-with-worktrees"
            / "scripts"
            / "dww.py"
        )
        recovered = subprocess.run(
            [
                "uv",
                "run",
                "--script",
                str(runner),
                "--repo",
                str(git_repo),
                "--json",
                "recover",
                "--task",
                task["id"],
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=90,
        )
        assert recovered.returncode == 0, recovered.stderr
        first = json.loads(recovered.stdout)["result"]
    else:
        first = recover(repo, task_id=task["id"])
    first_receipt = read_json(
        repo.local_dir / "integration-receipts" / f"{task['id']}.json", {}
    )
    first_operation_status = read_json(operation_path, {})["status"]
    second = recover(repo, task_id=task["id"])
    second_receipt = read_json(
        repo.local_dir / "integration-receipts" / f"{task['id']}.json", {}
    )
    state = StateStore(repo).read()
    finished = state["tasks"][task["id"]]
    assert first["status"] == second["status"] == "completed"
    assert first_receipt["transaction_id"] == second_receipt["transaction_id"]
    assert first_receipt["integrated_head"] == candidate
    if stage == "after-prepare":
        assert repo.head(git_repo) == candidate
    else:
        assert repo.head(git_repo) == crash_main
    assert finished["status"] == "finished"
    assert finished["integration"]["phase"] == "completed"
    assert state["slots"][task["slot_id"]]["status"] == "idle"
    assert repo.branch(Path(task["worktree"])) is None
    assert repo.head(Path(task["worktree"])) == candidate
    assert repo.ref_head(f"refs/heads/{task['branch']}") is None
    assert first_operation_status == "interrupted"
    assert read_json(operation_path, {})["status"] == first_operation_status


def test_running_operation_receipt_failure_rolls_back_live_marker(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="running receipt failure")
    original = state_module.atomic_write_json

    def fail_running(path: Path, value: dict[str, object]) -> None:
        if "operations" in str(path) and value.get("status") == "running":
            raise OSError("simulated running receipt failure")
        original(path, value)

    monkeypatch.setattr(state_module, "atomic_write_json", fail_running)
    with pytest.raises(OSError, match="running receipt failure"):
        ready(repo, task_id=task["id"], lease=task["lease"])
    assert StateStore(repo).task(task["id"])["active_operation"] is None


def test_final_operation_receipt_failure_does_not_turn_success_into_failure(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="final receipt failure")
    commit_one(repo, task, "final-log.txt", "ok\n", "test: final operation log")
    ready(repo, task_id=task["id"], lease=task["lease"])
    original = state_module.atomic_write_json

    def fail_final(path: Path, value: dict[str, object]) -> None:
        if "operations" in str(path) and value.get("status") in {
            "succeeded",
            "failed",
        }:
            raise OSError("simulated final receipt failure")
        original(path, value)

    monkeypatch.setattr(state_module, "atomic_write_json", fail_final)
    result = finish(repo, task_id=task["id"], lease=task["lease"])
    assert result["status"] == "completed"
    assert StateStore(repo).task(task["id"])["status"] == "finished"
    pending = StateStore(repo).read()["pending_operation_outcomes"]
    assert len(pending) == 1
    operation_id = next(iter(pending))
    monkeypatch.setattr(state_module, "atomic_write_json", original)
    recover(repo, task_id=task["id"])
    operation = read_json(repo.local_dir / "operations" / f"{operation_id}.json", {})
    assert operation["status"] == "succeeded"
    assert not StateStore(repo).read()["pending_operation_outcomes"]


def test_failed_operation_receipt_failure_preserves_original_error_and_recovers_audit(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="failed operation receipt")
    worktree = Path(task["worktree"])
    (worktree / ".ENV.local").write_text("secret\n", encoding="utf-8")
    original = state_module.atomic_write_json

    def fail_final(path: Path, value: dict[str, object]) -> None:
        if "operations" in str(path) and value.get("status") == "failed":
            raise OSError("simulated failed receipt write")
        original(path, value)

    monkeypatch.setattr(state_module, "atomic_write_json", fail_final)
    with pytest.raises(SoloAIError, match="blocks abandon"):
        abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])
    state = StateStore(repo).read()
    assert state["tasks"][task["id"]]["active_operation"] is None
    operation_id = next(iter(state["pending_operation_outcomes"]))
    monkeypatch.setattr(state_module, "atomic_write_json", original)
    StateStore(repo).reconcile_operation_receipts()
    operation = read_json(repo.local_dir / "operations" / f"{operation_id}.json", {})
    assert operation["status"] == "failed"


def test_completed_task_ignores_redundant_operation_state_write_failure(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="redundant final state write")
    commit_one(repo, task, "redundant.txt", "ok\n", "test: redundant state write")
    ready(repo, task_id=task["id"], lease=task["lease"])
    original = StateStore.mutate

    def fail_operation_end(self: StateStore, callback: object) -> object:
        if getattr(callback, "__name__", "") == "end":
            raise OSError("simulated redundant operation state failure")
        return original(self, callback)  # type: ignore[arg-type]

    monkeypatch.setattr(StateStore, "mutate", fail_operation_end)
    result = finish(repo, task_id=task["id"], lease=task["lease"])
    assert result["status"] == "completed"
    assert StateStore(repo).task(task["id"])["status"] == "finished"


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_operation_records_process_interrupt_as_interrupted(
    git_repo: Path, interrupt: type[BaseException]
) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="operation interrupt")
    with pytest.raises(interrupt):
        with StateStore(repo).operation(task["id"], task["lease"], "ready"):
            raise interrupt()
    operations = [
        read_json(path, {}) for path in (repo.local_dir / "operations").glob("*.json")
    ]
    receipt = next(item for item in operations if item.get("kind") == "ready")
    assert receipt["status"] == "interrupted"
    assert StateStore(repo).task(task["id"])["active_operation"] is None


def test_recover_repairs_dead_running_operation_after_hard_exit(
    git_repo: Path,
) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="dead operation receipt")
    commit_one(repo, task, "dead-operation.txt", "ok\n", "test: dead operation")
    ready(repo, task_id=task["id"], lease=task["lease"])
    source_root = str(Path(lifecycle.__file__).parent.parent)
    child = r'''
import os
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from solo_ai import state as state_module
from solo_ai.lifecycle import finish
from solo_ai.repo import GitRepo

repo = GitRepo(Path(sys.argv[2]))
original = state_module.atomic_write_json
def exit_on_terminal(path, value):
    if "operations" in str(path) and value.get("status") != "running":
        os._exit(86)
    return original(path, value)
state_module.atomic_write_json = exit_on_terminal
finish(repo, task_id=sys.argv[3], lease=sys.argv[4])
'''
    crashed = subprocess.run(
        [
            sys.executable,
            "-c",
            child,
            source_root,
            str(git_repo),
            task["id"],
            task["lease"],
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=90,
    )
    assert crashed.returncode == 86, crashed.stderr
    operation_path = next(
        path
        for path in (repo.local_dir / "operations").glob("*.json")
        if read_json(path, {}).get("kind") == "finish"
    )
    assert read_json(operation_path, {})["status"] == "running"

    recover(repo, task_id=task["id"])
    assert read_json(operation_path, {})["status"] == "succeeded"
    assert not StateStore(repo).read()["pending_operation_outcomes"]


def test_recover_repairs_in_place_receipt_after_release(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="in-place receipt crash", in_place=True, session_id="session-a")
    (git_repo / "in-place-receipt.txt").write_text("done\n", encoding="utf-8")
    commit_task(
        repo,
        task_id=task["id"],
        lease=task["lease"],
        session_id="session-a",
        message="test: in-place receipt",
        paths=["in-place-receipt.txt"],
    )
    ready(
        repo,
        task_id=task["id"],
        lease=task["lease"],
        session_id="session-a",
    )
    original = lifecycle._write_in_place_receipt
    calls = 0

    def fail_released(repo_arg: GitRepo, receipt: dict[str, object]) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated released receipt failure")
        original(repo_arg, receipt)

    monkeypatch.setattr(lifecycle, "_write_in_place_receipt", fail_released)
    with pytest.raises(OSError, match="released receipt failure"):
        finish(
            repo,
            task_id=task["id"],
            lease=task["lease"],
            session_id="session-a",
        )
    assert StateStore(repo).task(task["id"])["status"] == "finished"

    monkeypatch.setattr(lifecycle, "_write_in_place_receipt", original)
    result = recover(repo, task_id=task["id"])
    receipt = read_json(
        repo.local_dir / "in-place-receipts" / f"{task['id']}.json", {}
    )
    assert result["status"] == "completed"
    assert receipt["stage"] == "released"


def test_in_place_finish_rejects_forged_local_completion_receipt(
    git_repo: Path,
) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="forged in-place receipt", in_place=True, session_id="session-a")
    (git_repo / "forged.txt").write_text("done\n", encoding="utf-8")
    commit_task(
        repo,
        task_id=task["id"],
        lease=task["lease"],
        session_id="session-a",
        message="test: forged receipt",
        paths=["forged.txt"],
    )
    ready(
        repo,
        task_id=task["id"],
        lease=task["lease"],
        session_id="session-a",
    )
    atomic_write_json(
        repo.local_dir / "in-place-receipts" / f"{task['id']}.json",
        {
            "schema_version": 1,
            "task_id": task["id"],
            "mode": "in-place",
            "branch": task["branch"],
            "head": task["expected_head"],
            "start_head": task["start_head"],
            "stage": "bogus",
            "proof": "fake",
            "proof_kind": "commands",
        },
    )
    with pytest.raises(SoloAIError, match="Unsupported in-place completion receipt"):
        finish(
            repo,
            task_id=task["id"],
            lease=task["lease"],
            session_id="session-a",
        )
    assert StateStore(repo).task(task["id"])["status"] == "ready"


def test_finish_preserves_dirty_worktree_created_after_promotion(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="dirty cleanup")
    worktree = Path(task["worktree"])
    commit_one(repo, task, "dirty.txt", "clean\n", "test: cleanup candidate")
    ready(repo, task_id=task["id"], lease=task["lease"])
    original_mark = StateStore.mark_integration_promoted

    def dirty_after_mark(
        self: StateStore,
        task_id: str,
        *,
        transaction_id: str,
        observed_base_head: str,
    ) -> dict[str, object]:
        result = original_mark(
            self,
            task_id,
            transaction_id=transaction_id,
            observed_base_head=observed_base_head,
        )
        (worktree / "dirty.txt").write_text("changed\n", encoding="utf-8")
        return result

    monkeypatch.setattr(StateStore, "mark_integration_promoted", dirty_after_mark)
    with pytest.raises(SoloAIError, match="worktree changed"):
        finish(repo, task_id=task["id"], lease=task["lease"])
    assert (worktree / "dirty.txt").read_text(encoding="utf-8") == "changed\n"
    assert repo.ref_head(f"refs/heads/{task['branch']}") is not None
    assert StateStore(repo).task(task["id"])["status"] == "finishing"


def test_finish_atomically_preserves_branch_advanced_before_delete(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="branch advance cleanup")
    commit_one(repo, task, "branch.txt", "branch\n", "test: branch candidate")
    ready(repo, task_id=task["id"], lease=task["lease"])
    original_delete = GitRepo.delete_ref
    advanced: dict[str, str] = {}

    def advance_then_delete(
        self: GitRepo, ref: str, *, expected: str, cwd: Path | None = None
    ) -> None:
        new_head = self.git(
            ["commit-tree", f"{expected}^{{tree}}", "-p", expected, "-m", "late"],
            cwd=cwd,
        ).stdout.strip()
        self.git(["update-ref", ref, new_head, expected], cwd=cwd)
        advanced["head"] = new_head
        original_delete(self, ref, expected=expected, cwd=cwd)

    monkeypatch.setattr(GitRepo, "delete_ref", advance_then_delete)
    with pytest.raises(SoloAIError, match="changed before deletion"):
        finish(repo, task_id=task["id"], lease=task["lease"])
    assert repo.ref_head(f"refs/heads/{task['branch']}") == advanced["head"]
    assert StateStore(repo).task(task["id"])["status"] == "finishing"


@pytest.mark.parametrize("name", [".env.local", ".ENV.production", "critical.DB"])
def test_abandon_preserves_sensitive_untracked_content(
    git_repo: Path, name: str
) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="protected abandon")
    worktree = Path(task["worktree"])
    (worktree / name).write_text("secret\n", encoding="utf-8")

    with pytest.raises(SoloAIError, match="blocks abandon"):
        abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])

    assert (worktree / name).read_text(encoding="utf-8") == "secret\n"
    assert repo.ref_head(f"refs/heads/{task['branch']}") is not None
    assert StateStore(repo).task(task["id"])["status"] == "active"


def test_abandon_rejects_other_branch_even_at_same_head(git_repo: Path) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="wrong branch abandon")
    worktree = Path(task["worktree"])
    original_head = repo.head(worktree)
    git(worktree, "switch", "-c", "other-branch", original_head)

    with pytest.raises(SoloAIError, match="identity changed"):
        abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])

    assert repo.branch(worktree) == "other-branch"
    assert repo.head(worktree) == original_head
    assert repo.ref_head(f"refs/heads/{task['branch']}") == original_head
    assert StateStore(repo).task(task["id"])["status"] == "active"


def test_recover_completes_abandonment_after_branch_delete(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="abandon crash")
    commit_one(repo, task, "discard.txt", "discard\n", "test: discard candidate")
    original_complete = StateStore.complete_abandonment

    def fail_complete(
        self: StateStore, task_id: str, *, transaction_id: str
    ) -> dict[str, object]:
        raise RuntimeError("simulated abandonment release failure")

    monkeypatch.setattr(StateStore, "complete_abandonment", fail_complete)
    with pytest.raises(RuntimeError, match="release failure"):
        abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])
    failed = StateStore(repo).task(task["id"])
    assert failed["status"] == "abandoning"
    assert repo.branch(Path(task["worktree"])) is None
    assert repo.ref_head(f"refs/heads/{task['branch']}") is None

    monkeypatch.setattr(StateStore, "complete_abandonment", original_complete)
    result = recover(repo, task_id=task["id"])
    assert result["status"] == "abandoned"
    assert StateStore(repo).task(task["id"])["status"] == "abandoned"


@pytest.mark.parametrize("stage", ["before-delete", "after-delete", "after-complete"])
def test_public_recover_completes_abandonment_after_real_process_exit(
    git_repo: Path, stage: str
) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="abandon hard exit")
    commit_one(repo, task, "discard-hard.txt", "discard\n", "test: hard abandon")
    source_root = str(Path(lifecycle.__file__).parent.parent)
    child = r'''
import os
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from solo_ai import abandonment as abandonment_module
from solo_ai.lifecycle import abandon
from solo_ai.repo import GitRepo
from solo_ai.state import StateStore

repo = GitRepo(Path(sys.argv[2]))
task_id, lease, stage = sys.argv[3:6]
if stage in {"before-delete", "after-delete"}:
    original = GitRepo.delete_ref_with_verifications
    def delete_then_exit(self, ref, *, expected, verifications, cwd=None):
        if stage == "before-delete":
            os._exit(86)
        original(self, ref, expected=expected, verifications=verifications, cwd=cwd)
        os._exit(86)
    GitRepo.delete_ref_with_verifications = delete_then_exit
else:
    original_complete = StateStore.complete_abandonment
    def complete_then_exit(self, task_id, *, transaction_id):
        original_complete(self, task_id, transaction_id=transaction_id)
        os._exit(86)
    StateStore.complete_abandonment = complete_then_exit
abandon(repo, task_id=task_id, lease=lease, confirm=task_id)
'''
    crashed = subprocess.run(
        [
            sys.executable,
            "-c",
            child,
            source_root,
            str(git_repo),
            task["id"],
            task["lease"],
            stage,
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=90,
    )
    assert crashed.returncode == 86, crashed.stderr
    if stage == "before-delete":
        assert repo.ref_head(f"refs/heads/{task['branch']}") is not None
    else:
        assert repo.ref_head(f"refs/heads/{task['branch']}") is None

    operation_path = next(
        path
        for path in (repo.local_dir / "operations").glob("*.json")
        if (receipt := read_json(path, {})).get("task_id") == task["id"]
        and receipt.get("kind") == "abandon"
    )
    assert read_json(operation_path, {})["status"] == "running"

    result = recover(repo, task_id=task["id"])
    receipt = read_json(
        repo.local_dir / "abandonment-receipts" / f"{task['id']}.json", {}
    )
    first_operation_status = read_json(operation_path, {})["status"]
    repeated = recover(repo, task_id=task["id"])
    repeated_receipt = read_json(
        repo.local_dir / "abandonment-receipts" / f"{task['id']}.json", {}
    )
    state = StateStore(repo).read()
    assert result["status"] == "abandoned"
    assert repeated["status"] == "abandoned"
    assert receipt["transaction_id"] == repeated_receipt["transaction_id"]
    assert state["tasks"][task["id"]]["status"] == "abandoned"
    assert state["slots"][task["slot_id"]]["status"] == "idle"
    assert repo.branch(Path(task["worktree"])) is None
    assert repo.head(Path(task["worktree"])) == task["base_head"]
    assert repo.ref_head(f"refs/heads/{task['branch']}") is None
    assert first_operation_status == "interrupted"
    assert read_json(operation_path, {})["status"] == first_operation_status


def test_abandon_preserves_tracked_change_created_after_transaction_prepare(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="late tracked abandon")
    worktree = Path(task["worktree"])
    original_prepare = StateStore.prepare_abandonment

    def prepare_then_edit(
        self: StateStore,
        task_id: str,
        *,
        operation_id: str,
        abandonment: dict[str, object],
    ) -> dict[str, object]:
        result = original_prepare(
            self,
            task_id,
            operation_id=operation_id,
            abandonment=abandonment,
        )
        (worktree / "README.md").write_text("late edit\n", encoding="utf-8")
        return result

    monkeypatch.setattr(StateStore, "prepare_abandonment", prepare_then_edit)
    with pytest.raises(SoloAIError, match="Tracked worktree content changed"):
        abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])
    assert (worktree / "README.md").read_text(encoding="utf-8") == "late edit\n"
    assert repo.ref_head(f"refs/heads/{task['branch']}") is not None
    assert StateStore(repo).task(task["id"])["status"] == "abandoning"


def test_abandon_cas_preserves_branch_advanced_during_cleanup(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="abandon branch race")
    commit_one(repo, task, "race.txt", "candidate\n", "test: abandon race")
    original = abandonment_module.remove_abandoned_untracked
    advanced: dict[str, str] = {}

    def advance_after_inventory(
        repo_arg: GitRepo,
        *,
        cwd: Path,
        expected_ordinary: dict[str, dict[str, object]],
        policy: object = None,
    ) -> None:
        original(repo_arg, cwd=cwd, expected_ordinary=expected_ordinary)
        expected = repo_arg.ref_head(f"refs/heads/{task['branch']}")
        assert expected
        new_head = repo_arg.git(
            ["commit-tree", f"{expected}^{{tree}}", "-p", expected, "-m", "late abandon"],
            cwd=cwd,
        ).stdout.strip()
        repo_arg.git(
            ["update-ref", f"refs/heads/{task['branch']}", new_head, expected], cwd=cwd
        )
        advanced["head"] = new_head

    monkeypatch.setattr(abandonment_module, "remove_abandoned_untracked", advance_after_inventory)
    with pytest.raises(SoloAIError, match="advanced during abandonment"):
        abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])
    assert repo.ref_head(f"refs/heads/{task['branch']}") == advanced["head"]
    assert StateStore(repo).task(task["id"])["status"] == "abandoning"


@pytest.mark.parametrize("relative", ["README.md", "scratch.txt", ".ENV.local", "late.DB"])
def test_recover_abandonment_refuses_late_worktree_content(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, relative: str
) -> None:
    repo = initialized(git_repo)
    task = start(repo, name=f"late abandon content {relative}")
    original_complete = StateStore.complete_abandonment
    monkeypatch.setattr(
        StateStore,
        "complete_abandonment",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("stop before release")),
    )
    with pytest.raises(RuntimeError, match="stop before release"):
        abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])
    monkeypatch.setattr(StateStore, "complete_abandonment", original_complete)
    target = Path(task["worktree"]) / relative
    target.write_text("late\n", encoding="utf-8")

    with pytest.raises(SoloAIError, match="changed before release|files were preserved"):
        recover(repo, task_id=task["id"])
    assert target.read_text(encoding="utf-8") == "late\n"
    assert StateStore(repo).task(task["id"])["status"] == "abandoning"


def test_abandon_refuses_candidate_referenced_by_another_active_task(
    git_repo: Path,
) -> None:
    repo = initialized(git_repo)
    task_a = start(repo, name="shared candidate a")
    commit_one(repo, task_a, "shared.txt", "shared\n", "test: shared candidate")
    candidate = repo.head(Path(task_a["worktree"]))
    task_b = start(repo, name="shared candidate b")
    git(Path(task_b["worktree"]), "merge", "--ff-only", candidate)

    with pytest.raises(SoloAIError, match="referenced by active task"):
        abandon(
            repo,
            task_id=task_a["id"],
            lease=task_a["lease"],
            confirm=task_a["id"],
        )
    assert repo.ref_head(f"refs/heads/{task_a['branch']}") == candidate
    assert StateStore(repo).task(task_a["id"])["status"] == "active"


def test_abandon_blocks_preexisting_tracked_changes_without_preparing_transaction(
    git_repo: Path,
) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="dirty tracked abandon")
    worktree = Path(task["worktree"])
    (worktree / "README.md").write_text("first dirty value\n", encoding="utf-8")

    with pytest.raises(SoloAIError, match="Tracked worktree changes block abandon"):
        abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])
    assert (worktree / "README.md").read_text(encoding="utf-8") == "first dirty value\n"
    assert StateStore(repo).task(task["id"])["status"] == "active"


def test_abandon_preserves_ordinary_file_replaced_at_conditional_delete(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="ordinary replacement abandon")
    worktree = Path(task["worktree"])
    scratch = worktree / "scratch.txt"
    scratch.write_text("reviewed\n", encoding="utf-8")
    original = cleanup_module.delete_plain_path_if_unchanged

    def replace_then_delete(path: Path, expected: dict[str, object]) -> None:
        if path == scratch:
            path.write_text("late replacement\n", encoding="utf-8")
        original(path, expected)

    monkeypatch.setattr(
        cleanup_module, "delete_plain_path_if_unchanged", replace_then_delete
    )
    with pytest.raises(SoloAIError, match="changed before deletion"):
        abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])
    assert scratch.read_text(encoding="utf-8") == "late replacement\n"
    assert StateStore(repo).task(task["id"])["status"] == "abandoning"


def test_abandon_atomically_verifies_other_active_refs_before_delete(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized(git_repo)
    task_a = start(repo, name="atomic reference a")
    commit_one(repo, task_a, "a.txt", "a\n", "test: candidate a")
    candidate = repo.head(Path(task_a["worktree"]))
    task_b = start(repo, name="atomic reference b")
    original = GitRepo.delete_ref_with_verifications

    def advance_b_then_delete(
        self: GitRepo,
        ref: str,
        *,
        expected: str,
        verifications: dict[str, str],
        cwd: Path | None = None,
    ) -> None:
        b_ref = f"refs/heads/{task_b['branch']}"
        old_b = self.ref_head(b_ref)
        assert old_b
        new_b = self.git(
            ["commit-tree", f"{candidate}^{{tree}}", "-p", candidate, "-m", "include a"],
            cwd=cwd,
        ).stdout.strip()
        self.git(["update-ref", b_ref, new_b, old_b], cwd=cwd)
        original(
            self,
            ref,
            expected=expected,
            verifications=verifications,
            cwd=cwd,
        )

    monkeypatch.setattr(GitRepo, "delete_ref_with_verifications", advance_b_then_delete)
    with pytest.raises(SoloAIError, match="active task ref changed"):
        abandon(
            repo,
            task_id=task_a["id"],
            lease=task_a["lease"],
            confirm=task_a["id"],
        )
    assert repo.ref_head(f"refs/heads/{task_a['branch']}") == candidate
    assert StateStore(repo).task(task_a["id"])["status"] == "abandoning"


def test_abandon_quarantines_released_slot_when_content_arrives_at_complete(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="late release content")
    worktree = Path(task["worktree"])
    original = StateStore.complete_abandonment

    def complete_after_write(
        self: StateStore, task_id: str, *, transaction_id: str
    ) -> dict[str, object]:
        completed = original(self, task_id, transaction_id=transaction_id)
        slot = self.read()["slots"][task["slot_id"]]
        assert slot["status"] == "release-checking"
        assert slot["task_id"] == task_id
        (worktree / ".ENV.local").write_text("late\n", encoding="utf-8")
        return completed

    monkeypatch.setattr(StateStore, "complete_abandonment", complete_after_write)
    with pytest.raises(SoloAIError, match="changed before release|files were preserved"):
        abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])
    state = StateStore(repo).read()
    assert state["tasks"][task["id"]]["status"] == "abandoned"
    assert state["slots"][task["slot_id"]]["status"] == "quarantined"
    assert (worktree / ".ENV.local").read_text(encoding="utf-8") == "late\n"


def test_abandon_quarantines_content_created_at_release_publish(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="late abandon publish")
    worktree = Path(task["worktree"])
    original = StateStore.publish_abandonment_release

    def publish_after_write(
        self: StateStore, task_id: str, *, transaction_id: str
    ) -> dict[str, object]:
        (worktree / ".ENV.local").write_text("late\n", encoding="utf-8")
        return original(self, task_id, transaction_id=transaction_id)

    monkeypatch.setattr(StateStore, "publish_abandonment_release", publish_after_write)
    with pytest.raises(SoloAIError, match="changed.*abandonment release|changed before release"):
        abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])
    state = StateStore(repo).read()
    assert state["slots"][task["slot_id"]]["status"] == "quarantined"
    assert (worktree / ".ENV.local").read_text(encoding="utf-8") == "late\n"


def test_finish_quarantines_content_created_at_release_publish(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="late finish publish")
    worktree = Path(task["worktree"])
    commit_one(repo, task, "finish.txt", "done\n", "test: finish late publish")
    ready(repo, task_id=task["id"], lease=task["lease"])
    original = StateStore.publish_integration_release

    def publish_after_write(
        self: StateStore, task_id: str, *, transaction_id: str
    ) -> dict[str, object]:
        (worktree / ".ENV.local").write_text("late\n", encoding="utf-8")
        return original(self, task_id, transaction_id=transaction_id)

    monkeypatch.setattr(StateStore, "publish_integration_release", publish_after_write)
    with pytest.raises(SoloAIError, match="changed.*integration release|changed before slot release"):
        finish(repo, task_id=task["id"], lease=task["lease"])
    state = StateStore(repo).read()
    assert state["slots"][task["slot_id"]]["status"] == "quarantined"
    assert (worktree / ".ENV.local").read_text(encoding="utf-8") == "late\n"


def test_start_rejects_ignored_protected_content_added_to_idle_slot(
    git_repo: Path,
) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="release slot identity")
    worktree = Path(task["worktree"])
    abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])
    info_exclude = repo.common_dir / "info" / "exclude"
    with info_exclude.open("a", encoding="utf-8") as handle:
        handle.write("\n.ENV.local\n")
    protected = worktree / ".ENV.local"
    protected.write_text("late-secret\n", encoding="utf-8")
    StateStore(repo).mutate(
        lambda state: [
            slot.update({"status": "quarantined"})
            for slot_id, slot in state["slots"].items()
            if slot_id != task["slot_id"]
        ]
    )

    with pytest.raises(SoloAIError, match="protected or unknown ignored content"):
        start(repo, name="must not inherit secret")
    state = StateStore(repo).read()
    assert state["slots"][task["slot_id"]]["status"] == "quarantined"
    assert protected.read_text(encoding="utf-8") == "late-secret\n"


def test_start_rejects_same_path_idle_worktree_replacement(git_repo: Path) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="release directory identity")
    worktree = Path(task["worktree"])
    abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])
    parked = worktree.with_name(worktree.name + "-released-original")
    worktree.rename(parked)
    shutil.copytree(parked, worktree)
    StateStore(repo).mutate(
        lambda state: [
            slot.update({"status": "quarantined"})
            for slot_id, slot in state["slots"].items()
            if slot_id != task["slot_id"]
        ]
    )

    with pytest.raises(SoloAIError, match="directory object was replaced"):
        start(repo, name="must not reuse replaced directory")
    state = StateStore(repo).read()
    assert state["slots"][task["slot_id"]]["status"] == "quarantined"
    assert (parked / "README.md").exists()
    assert (worktree / "README.md").exists()


def test_abandon_rejects_same_path_worktree_directory_replacement(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="replaced worktree object")
    worktree = Path(task["worktree"])
    parked = worktree.with_name(worktree.name + "-original")
    original = StateStore.prepare_abandonment

    def prepare_then_replace(
        self: StateStore,
        task_id: str,
        *,
        operation_id: str,
        abandonment: dict[str, object],
    ) -> dict[str, object]:
        result = original(
            self,
            task_id,
            operation_id=operation_id,
            abandonment=abandonment,
        )
        worktree.rename(parked)
        shutil.copytree(parked, worktree)
        return result

    monkeypatch.setattr(StateStore, "prepare_abandonment", prepare_then_replace)
    with pytest.raises(SoloAIError, match="directory object was replaced"):
        abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])
    assert (parked / "README.md").exists()
    assert repo.ref_head(f"refs/heads/{task['branch']}") is not None
    assert StateStore(repo).task(task["id"])["status"] == "abandoning"


def test_finish_retains_standard_tool_caches_created_by_validation(
    git_repo: Path,
) -> None:
    cache_command = CommandSpec(
        (
            sys.executable,
            "-c",
            (
                "from pathlib import Path\n"
                "for name in ('.pytest_cache', '.ruff_cache'):\n"
                "    cache = Path(name)\n"
                "    cache.mkdir(exist_ok=True)\n"
                "    (cache / 'marker').write_text('cache', encoding='utf-8')\n"
            ),
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


def test_choose_isolated_adopts_static_repository_without_a_second_choice(
    git_repo: Path,
) -> None:
    repo = GitRepo(git_repo)

    result = choose(
        repo,
        mode="isolated",
        slots=2,
        commands=[],
    )

    assert result["choice"] == "isolated"
    assert result["decision"] == "adopted"
    assert result["static_only"] is True
    assert (git_repo / ".solo-ai" / "config.toml").exists()
    assert (git_repo / "AGENTS.md").exists()


@pytest.mark.parametrize(
    ("mode", "session_id"),
    [
        ("isolated", None),
        ("current-task", "current-session"),
        ("current-repository", None),
    ],
)
def test_choose_defers_to_mature_workflow_without_writing_state(
    git_repo: Path,
    mode: str,
    session_id: str | None,
) -> None:
    marker = git_repo / "scripts" / "worktree-flow.ps1"
    marker.parent.mkdir()
    marker.write_text("# existing\n", encoding="utf-8")
    repo = GitRepo(git_repo)
    before = git(git_repo, "status", "--porcelain")

    result = choose(
        repo,
        mode=mode,
        slots=3,
        commands=None,
        session_id=session_id,
    )

    assert result == {
        "choice": mode,
        "decision": "deferred",
        "reason": "existing-workflow",
        "workflows": ["repository worktree-flow"],
    }
    assert git(git_repo, "status", "--porcelain") == before
    assert not repo.local_dir.exists()


def test_choose_current_task_is_session_bound_and_delegates_only_by_code(
    git_repo: Path,
) -> None:
    repo = GitRepo(git_repo)

    chosen = choose(
        repo,
        mode="current-task",
        slots=3,
        commands=None,
        session_id="parent-session",
    )

    assert chosen["choice"] == "current-task"
    assert task_bypass_active(repo, session_id="parent-session") is True
    assert task_bypass_active(repo, session_id="other-session") is False
    assert not (git_repo / ".solo-ai").exists()
    assert not (git_repo / "AGENTS.md").exists()
    state = (repo.local_dir / "session-overrides.json").read_text(encoding="utf-8")
    assert "parent-session" not in state
    assert chosen["delegation_code"] not in state

    delegated = choose(
        repo,
        mode="current-task",
        slots=3,
        commands=None,
        session_id="child-session",
        delegation_code=chosen["delegation_code"],
    )
    assert delegated == {"choice": "current-task", "delegated": True}
    assert task_bypass_active(repo, session_id="child-session") is True

    with pytest.raises(SoloAIError, match="delegation code"):
        choose(
            repo,
            mode="current-task",
            slots=3,
            commands=None,
            session_id="unrelated-session",
            delegation_code="not-the-parent-code",
        )


def test_choose_current_task_serializes_parallel_child_delegation(
    git_repo: Path,
) -> None:
    repo = GitRepo(git_repo)
    parent = choose(
        repo,
        mode="current-task",
        slots=3,
        commands=None,
        session_id="parent-session",
    )
    errors: list[Exception] = []

    def delegate(session_id: str) -> None:
        try:
            choose(
                repo,
                mode="current-task",
                slots=3,
                commands=None,
                session_id=session_id,
                delegation_code=parent["delegation_code"],
            )
        except Exception as exc:  # noqa: BLE001 - 断言并发登记没有丢失或异常。
            errors.append(exc)

    threads = [
        threading.Thread(target=delegate, args=(f"child-{number}",))
        for number in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert all(
        task_bypass_active(repo, session_id=f"child-{number}") for number in range(4)
    )


def test_choose_current_task_refuses_to_strand_an_active_task_in_the_directory(
    git_repo: Path,
) -> None:
    repo = initialized(git_repo)
    task = start(
        repo, name="existing current-worktree task", in_place=True, session_id="old"
    )

    with pytest.raises(SoloAIError, match="Finish or abandon"):
        choose(
            repo,
            mode="current-task",
            slots=3,
            commands=None,
            session_id="new",
        )

    assert task_bypass_active(repo, session_id="new") is False
    assert StateStore(repo).task(task["id"])["status"] == "active"


def test_choose_current_repository_reuses_disable_safety_checks(git_repo: Path) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="cannot strand a managed task")

    with pytest.raises(SoloAIError, match="Active or quarantined tasks"):
        choose(
            repo,
            mode="current-repository",
            slots=3,
            commands=None,
        )

    assert local_enabled(repo) is True
    abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])
    result = choose(
        repo,
        mode="current-repository",
        slots=3,
        commands=None,
    )
    assert result["choice"] == "current-repository"
    assert local_enabled(repo) is False


def test_choose_current_repository_is_local_and_creates_no_tracked_policy(
    git_repo: Path,
) -> None:
    repo = GitRepo(git_repo)

    result = choose(
        repo,
        mode="current-repository",
        slots=3,
        commands=None,
    )

    assert result["choice"] == "current-repository"
    assert local_enabled(repo) is False
    assert not (git_repo / ".solo-ai").exists()
    assert not (git_repo / "AGENTS.md").exists()


def test_choose_isolated_reenables_a_repository_after_local_direct_choice(
    git_repo: Path,
) -> None:
    repo = GitRepo(git_repo)
    choose(repo, mode="current-repository", slots=3, commands=None)

    result = choose(repo, mode="isolated", slots=1, commands=[VERIFY])

    assert result["choice"] == "isolated"
    assert result["decision"] == "adopted"
    assert local_enabled(repo) is True


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
        """schema_version = 3
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


def test_ready_resynchronizes_before_validation_after_queue_wait(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized(git_repo)
    counter = repo.local_dir / "ready-queue-counter.txt"
    command = [
        sys.executable,
        "-c",
        (
            "from pathlib import Path; "
            f"p=Path({str(counter)!r}); "
            "p.write_text(str(int(p.read_text()) + 1) if p.exists() else '1')"
        ),
    ]
    install_counting_verification_policy(repo, command)
    task = start(repo, name="base advances while queued")
    commit_one(repo, task, "candidate.txt", "candidate\n", "test: candidate")

    original_claim = proof_module.claim_validation_slot
    advanced = False

    @contextmanager
    def advance_base_before_claim_yields(resource_class: str):
        nonlocal advanced
        with original_claim(resource_class) as claim:
            if not advanced:
                advanced = True
                (repo.root / "parallel.txt").write_text("parallel\n", encoding="utf-8")
                git(repo.root, "add", "parallel.txt")
                git(repo.root, "commit", "-m", "test: parallel base advance")
            yield claim

    monkeypatch.setattr(
        proof_module, "claim_validation_slot", advance_base_before_claim_yields
    )

    prepared = ready(repo, task_id=task["id"], lease=task["lease"])

    assert prepared["base_head"] == repo.head(repo.root)
    assert prepared["convergence_retries"] == 1
    assert counter.read_text(encoding="utf-8") == "1"
    abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])


def test_ready_resynchronizes_after_validation_and_reuses_profile_proof(
    git_repo: Path,
) -> None:
    repo = initialized(git_repo)
    counter = repo.local_dir / "ready-after-counter.txt"
    marker = repo.root / "parallel.txt"
    script = (
        "from pathlib import Path; import subprocess; "
        f"counter=Path({str(counter)!r}); marker=Path({str(marker)!r}); "
        "counter.write_text(str(int(counter.read_text()) + 1) if counter.exists() else '1'); "
        f"root={str(repo.root)!r}; "
        "marker.exists() or (marker.write_text('parallel\\n', encoding='utf-8'), "
        "subprocess.run(['git', '-C', root, 'add', 'parallel.txt'], check=True), "
        "subprocess.run(['git', '-C', root, 'commit', '-m', 'test: parallel base advance'], check=True))"
    )
    install_counting_verification_policy(repo, [sys.executable, "-c", script])
    task = start(repo, name="base advances after validation")
    commit_one(repo, task, "candidate.txt", "candidate\n", "test: candidate")

    prepared = ready(repo, task_id=task["id"], lease=task["lease"])

    assert prepared["base_head"] == repo.head(repo.root)
    assert prepared["convergence_retries"] == 1
    assert counter.read_text(encoding="utf-8") == "1"
    proof = read_json(repo.local_dir / "proofs" / f"{prepared['ready_proof']}.json", {})
    assert proof["profile_proofs"][0]["reused"] is True
    result = finish(repo, task_id=task["id"], lease=task["lease"])
    assert result["proof_reused"] is True
    assert counter.read_text(encoding="utf-8") == "1"


def test_ready_discards_a_stale_validation_failure_after_base_advance(
    git_repo: Path,
) -> None:
    repo = initialized(git_repo)
    counter = repo.local_dir / "ready-stale-failure-counter.txt"
    marker = repo.root / "parallel.txt"
    script = (
        "from pathlib import Path; import subprocess; "
        f"counter=Path({str(counter)!r}); marker=Path({str(marker)!r}); "
        "counter.write_text(str(int(counter.read_text()) + 1) if counter.exists() else '1'); "
        f"root={str(repo.root)!r}; "
        "already=marker.exists(); "
        "already or (marker.write_text('parallel\\n', encoding='utf-8'), "
        "subprocess.run(['git', '-C', root, 'add', 'parallel.txt'], check=True), "
        "subprocess.run(['git', '-C', root, 'commit', '-m', 'test: parallel base advance'], check=True)); "
        "raise SystemExit(0 if already else 1)"
    )
    install_counting_verification_policy(repo, [sys.executable, "-c", script])
    task = start(repo, name="stale failure after base advance")
    commit_one(repo, task, "candidate.txt", "candidate\n", "test: candidate")

    prepared = ready(repo, task_id=task["id"], lease=task["lease"])

    assert prepared["base_head"] == repo.head(repo.root)
    assert prepared["convergence_retries"] == 1
    assert counter.read_text(encoding="utf-8") == "2"
    abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])


def test_ready_keeps_a_real_validation_failure_as_a_hard_failure(
    git_repo: Path,
) -> None:
    repo = initialized(git_repo)
    install_counting_verification_policy(
        repo, [sys.executable, "-c", "raise SystemExit(1)"]
    )
    task = start(repo, name="real validation failure")
    commit_one(repo, task, "candidate.txt", "candidate\n", "test: candidate")

    with pytest.raises(SoloAIError, match="Validation failed"):
        ready(repo, task_id=task["id"], lease=task["lease"])

    assert StateStore(repo).task(task["id"])["status"] == "active"
    abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])


def test_ready_stops_after_bounded_base_convergence_retries(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized(git_repo)
    counter = repo.local_dir / "ready-bounded-counter.txt"
    install_counting_verification_policy(
        repo,
        [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; "
                f"p=Path({str(counter)!r}); "
                "p.write_text(str(int(p.read_text()) + 1) if p.exists() else '1')"
            ),
        ],
    )
    task = start(repo, name="base never settles")
    commit_one(repo, task, "candidate.txt", "candidate\n", "test: candidate")

    original_claim = proof_module.claim_validation_slot
    advance_count = 0

    @contextmanager
    def always_advance_base(resource_class: str):
        nonlocal advance_count
        with original_claim(resource_class) as claim:
            advance_count += 1
            relative = f"parallel-{advance_count}.txt"
            (repo.root / relative).write_text("parallel\n", encoding="utf-8")
            git(repo.root, "add", relative)
            git(repo.root, "commit", "-m", f"test: base advance {advance_count}")
            yield claim

    monkeypatch.setattr(proof_module, "claim_validation_slot", always_advance_base)

    with pytest.raises(SoloAIError, match="base kept advancing"):
        ready(repo, task_id=task["id"], lease=task["lease"])

    assert advance_count == lifecycle.MAX_READY_CONVERGENCE_RETRIES + 1
    assert not counter.exists()
    assert StateStore(repo).task(task["id"])["status"] == "active"
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
    with (
        StateStore(repo).operation(task["id"], task["lease"], "test"),
        pytest.raises(SoloAIError, match="live operation"),
    ):
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
    declare_cleanup(repo, ".venv")
    task = start(repo, name="prepare local cache")
    worktree = Path(task["worktree"])
    abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])
    (worktree / ".venv").mkdir()
    (worktree / ".venv" / "marker").write_text("cache", encoding="utf-8")
    plan = _prune(repo, kind="slot", slot="01")
    target = plan["targets"][0]
    assert target["path"] == ".venv"
    assert target["bytes"] == len("cache")
    assert target["delete_reason"] == "declared cleanup.owned_paths entry"
    assert len(target["contents_digest"]) == 64
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
    declare_cleanup(repo, ".venv")
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


def test_prune_slot_stops_when_a_declared_target_contains_protected_content(
    git_repo: Path,
) -> None:
    repo = initialized(git_repo)
    declare_cleanup(repo, ".venv")
    task = start(repo, name="preserve credentials")
    worktree = Path(task["worktree"])
    abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])
    (worktree / ".venv").mkdir()
    (worktree / ".venv" / ".env.local").write_text("marker=kept\n", encoding="utf-8")

    with pytest.raises(SoloAIError, match="retained or protected"):
        _prune(repo, kind="slot", slot="01")

    assert (worktree / ".venv" / ".env.local").read_text(
        encoding="utf-8"
    ) == "marker=kept\n"


def test_prune_slot_stops_when_a_declared_target_contains_a_link(
    git_repo: Path,
) -> None:
    repo = initialized(git_repo)
    declare_cleanup(repo, ".venv")
    task = start(repo, name="preserve linked cleanup content")
    worktree = Path(task["worktree"])
    abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])
    target = worktree / ".venv"
    target.mkdir()
    link = target / "linked-readme"
    try:
        os.symlink(worktree / "README.md", link)
    except OSError as exc:
        pytest.skip(f"Current Windows policy cannot create a symlink: {exc}")

    with pytest.raises(SoloAIError, match="staging identity|link or junction"):
        _prune(repo, kind="slot", slot="01")

    assert link.is_symlink()


def test_prune_slot_preserves_uppercase_env_content(git_repo: Path) -> None:
    repo = initialized(git_repo)
    declare_cleanup(repo, ".venv")
    task = start(repo, name="prepare uppercase env cache")
    worktree = Path(task["worktree"])
    abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])
    (worktree / ".venv").mkdir()
    protected = worktree / ".venv" / ".ENV.local"
    protected.write_text("secret\n", encoding="utf-8")

    with pytest.raises(SoloAIError, match="retained or protected"):
        _prune(repo, kind="slot", slot="01")
    assert protected.read_text(encoding="utf-8") == "secret\n"


def test_prune_slot_plan_is_one_shot(git_repo: Path) -> None:
    repo = initialized(git_repo)
    declare_cleanup(repo, ".venv")
    task = start(repo, name="prepare one shot cache")
    worktree = Path(task["worktree"])
    abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])
    (worktree / ".venv").mkdir()
    (worktree / ".venv" / "marker").write_text("same", encoding="utf-8")
    plan = _prune(repo, kind="slot", slot="01")
    _prune(
        repo,
        kind="slot",
        slot="01",
        plan_id=plan["plan_id"],
        confirm=plan["digest"],
    )
    (worktree / ".venv").mkdir()
    marker = worktree / ".venv" / "marker"
    marker.write_text("same", encoding="utf-8")

    with pytest.raises(
        SoloAIError, match="already executed|source reappeared after completion"
    ):
        _prune(
            repo,
            kind="slot",
            slot="01",
            plan_id=plan["plan_id"],
            confirm=plan["digest"],
        )
    assert marker.read_text(encoding="utf-8") == "same"


@pytest.mark.parametrize(
    "stage", ["rename", "unlink", "marker-delete", "completed-write"]
)
def test_prune_slot_recovers_after_real_process_exit(
    git_repo: Path, stage: str
) -> None:
    repo = initialized(git_repo)
    declare_cleanup(repo, ".venv")
    task = start(repo, name=f"prune crash {stage}")
    worktree = Path(task["worktree"])
    abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])
    (worktree / ".venv").mkdir()
    (worktree / ".venv" / "a").write_text("a", encoding="utf-8")
    (worktree / ".venv" / "b").write_text("b", encoding="utf-8")
    sibling = worktree / "unmanaged-sibling.txt"
    sibling.write_text("preserve\n", encoding="utf-8")
    plan = _prune(repo, kind="slot", slot="01")
    source_root = str(Path(lifecycle.__file__).parent.parent)
    child = r'''
import os
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from solo_ai import cli as cli_module
from solo_ai.cli import _prune
from solo_ai.repo import GitRepo

repo = GitRepo(Path(sys.argv[2]))
stage = sys.argv[5]
if stage == "rename":
    original = Path.rename
    def crash_after_rename(self, target):
        result = original(self, target)
        if ".dww-prune-" in str(target):
            os._exit(86)
        return result
    Path.rename = crash_after_rename
elif stage in {"unlink", "marker-delete"}:
    original = cli_module.delete_plain_path_if_unchanged
    def crash_after_unlink(path, expected):
        result = original(path, expected)
        if stage == "unlink" and path.name != ".dww-staging-owner":
            os._exit(86)
        if stage == "marker-delete" and path.name == ".dww-staging-owner":
            os._exit(86)
        return result
    cli_module.delete_plain_path_if_unchanged = crash_after_unlink
else:
    original = cli_module.atomic_write_json
    def crash_completed(path, value):
        if value.get("status") == "completed":
            os._exit(86)
        return original(path, value)
    cli_module.atomic_write_json = crash_completed
_prune(repo, kind="slot", slot="01", plan_id=sys.argv[3], confirm=sys.argv[4])
'''
    crashed = subprocess.run(
        [
            sys.executable,
            "-c",
            child,
            source_root,
            str(git_repo),
            plan["plan_id"],
            plan["digest"],
            stage,
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=90,
    )
    assert crashed.returncode == 86, crashed.stderr

    result = _prune(
        repo,
        kind="slot",
        slot="01",
        plan_id=plan["plan_id"],
        confirm=plan["digest"],
    )
    stored = read_json(
        repo.local_dir / "cleanup-plans" / f"{plan['plan_id']}.json", {}
    )
    assert result["status"] == "pruned"
    assert stored["status"] == "completed"
    assert not (worktree / ".venv").exists()
    assert not (worktree / f".dww-prune-{plan['plan_id']}").exists()
    assert sibling.read_text(encoding="utf-8") == "preserve\n"


def test_prune_slot_rejects_replaced_staging_directory_before_move(
    git_repo: Path, tmp_path: Path
) -> None:
    repo = initialized(git_repo)
    declare_cleanup(repo, ".venv")
    task = start(repo, name="replaced prune staging")
    worktree = Path(task["worktree"])
    abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])
    source = worktree / ".venv"
    source.mkdir()
    (source / "marker").write_text("keep\n", encoding="utf-8")
    plan = _prune(repo, kind="slot", slot="01")
    plan_path = repo.local_dir / "cleanup-plans" / f"{plan['plan_id']}.json"
    stored = read_json(plan_path, {})
    staging = worktree / f".dww-prune-{plan['plan_id']}"
    stored.update(
        {
            "status": "executing",
            "staging": str(staging),
            "staging_resolved": str(staging.absolute()),
        }
    )
    atomic_write_json(plan_path, stored)
    external = tmp_path / "external-staging"
    external.mkdir()
    try:
        os.symlink(external, staging, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Current Windows policy cannot create a directory symlink: {exc}")

    with pytest.raises(SoloAIError, match="staging identity|link or junction"):
        _prune(
            repo,
            kind="slot",
            slot="01",
            plan_id=plan["plan_id"],
            confirm=plan["digest"],
        )
    assert (source / "marker").read_text(encoding="utf-8") == "keep\n"
    assert not any(external.iterdir())


def test_prune_preserves_file_replaced_at_conditional_delete(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized(git_repo)
    declare_cleanup(repo, ".venv")
    task = start(repo, name="prune file replacement")
    worktree = Path(task["worktree"])
    abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])
    (worktree / ".venv").mkdir()
    (worktree / ".venv" / "marker").write_text("reviewed\n", encoding="utf-8")
    plan = _prune(repo, kind="slot", slot="01")
    original = cli_module.delete_plain_path_if_unchanged
    replaced: dict[str, Path] = {}

    def replace_then_delete(path: Path, expected: dict[str, object]) -> None:
        if path.name == "marker":
            path.write_text("late replacement\n", encoding="utf-8")
            replaced["path"] = path
        original(path, expected)

    monkeypatch.setattr(cli_module, "delete_plain_path_if_unchanged", replace_then_delete)
    with pytest.raises(SoloAIError, match="changed before deletion"):
        _prune(
            repo,
            kind="slot",
            slot="01",
            plan_id=plan["plan_id"],
            confirm=plan["digest"],
        )
    assert replaced["path"].read_text(encoding="utf-8") == "late replacement\n"


def test_prune_quarantines_plan_when_source_reappears_at_completion(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized(git_repo)
    declare_cleanup(repo, ".venv")
    task = start(repo, name="prune late source")
    worktree = Path(task["worktree"])
    abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])
    source = worktree / ".venv"
    source.mkdir()
    (source / "marker").write_text("reviewed\n", encoding="utf-8")
    plan = _prune(repo, kind="slot", slot="01")
    original = cli_module.atomic_write_json

    def recreate_before_completed(path: Path, value: dict[str, object]) -> None:
        if path.name == f"{plan['plan_id']}.json" and value.get("status") == "completed":
            source.mkdir(exist_ok=True)
            (source / "late").write_text("late\n", encoding="utf-8")
        original(path, value)

    monkeypatch.setattr(cli_module, "atomic_write_json", recreate_before_completed)
    with pytest.raises(SoloAIError, match="plan was quarantined"):
        _prune(
            repo,
            kind="slot",
            slot="01",
            plan_id=plan["plan_id"],
            confirm=plan["digest"],
        )
    stored = read_json(
        repo.local_dir / "cleanup-plans" / f"{plan['plan_id']}.json", {}
    )
    assert stored["status"] == "quarantined"
    assert (source / "late").read_text(encoding="utf-8") == "late\n"
    assert StateStore(repo).read()["slots"][task["slot_id"]]["status"] == "quarantined"


def test_prune_marker_is_created_exclusively_without_overwriting_late_content(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized(git_repo)
    declare_cleanup(repo, ".venv")
    task = start(repo, name="prune marker race")
    worktree = Path(task["worktree"])
    abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])
    source = worktree / ".venv"
    source.mkdir()
    (source / "marker").write_text("reviewed\n", encoding="utf-8")
    plan = _prune(repo, kind="slot", slot="01")
    owner = worktree / f".dww-prune-{plan['plan_id']}" / ".dww-staging-owner"
    original = Path.open
    injected = False

    def create_late_then_open(self: Path, *args: object, **kwargs: object):
        nonlocal injected
        if self == owner and args and args[0] == "x" and not injected:
            injected = True
            self.write_text("late-user-data\n", encoding="utf-8")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", create_late_then_open)
    with pytest.raises(SoloAIError, match="ownership marker appeared concurrently"):
        _prune(
            repo,
            kind="slot",
            slot="01",
            plan_id=plan["plan_id"],
            confirm=plan["digest"],
        )
    assert owner.read_text(encoding="utf-8") == "late-user-data\n"
    assert (source / "marker").read_text(encoding="utf-8") == "reviewed\n"


def test_prune_rejects_same_path_staging_directory_replacement(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized(git_repo)
    declare_cleanup(repo, ".venv")
    task = start(repo, name="replace staging object")
    worktree = Path(task["worktree"])
    abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])
    source = worktree / ".venv"
    source.mkdir()
    (source / "marker").write_text("reviewed\n", encoding="utf-8")
    plan = _prune(repo, kind="slot", slot="01")
    staging = worktree / f".dww-prune-{plan['plan_id']}"
    parked = worktree / f".dww-prune-{plan['plan_id']}-original"
    original = Path.rename
    replaced = False

    def rename_after_replacement(self: Path, target: Path) -> Path:
        nonlocal replaced
        if not replaced and self == source:
            replaced = True
            staging.rename(parked)
            shutil.copytree(parked, staging)
        return original(self, target)

    monkeypatch.setattr(Path, "rename", rename_after_replacement)
    with pytest.raises(SoloAIError, match="directory object was replaced"):
        _prune(
            repo,
            kind="slot",
            slot="01",
            plan_id=plan["plan_id"],
            confirm=plan["digest"],
        )
    assert (staging / ".venv" / "marker").read_text(encoding="utf-8") == "reviewed\n"
    assert parked.exists()


def test_prune_missing_slot_plan_is_consumed_once(git_repo: Path) -> None:
    repo = initialized(git_repo)
    task = start(repo, name="missing idle slot")
    worktree = Path(task["worktree"])
    abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])
    repo.git(["worktree", "remove", "--force", str(worktree)], cwd=git_repo)
    plan = _prune(repo, kind="slot", slot="01")
    result = _prune(
        repo,
        kind="slot",
        slot="01",
        plan_id=plan["plan_id"],
        confirm=plan["digest"],
    )
    assert result["worktree_retained"] is False
    with pytest.raises(SoloAIError, match="already executed"):
        _prune(
            repo,
            kind="slot",
            slot="01",
            plan_id=plan["plan_id"],
            confirm=plan["digest"],
        )


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
        except Exception as exc:  # noqa: BLE001 - test captures worker failures.
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


@pytest.mark.parametrize("status", ["finishing", "abandoning"])
def test_dev_start_refuses_task_finalization_states(
    git_repo: Path, status: str
) -> None:
    repo = initialized(git_repo)
    config = git_repo / ".solo-ai" / "config.toml"
    config.write_text(
        config.read_text(encoding="utf-8")
        + f'''\ndev_start = [{json.dumps(sys.executable)}, "-c", "import time; time.sleep(5)"]

[lifecycle.readiness]
kind = "tcp"
target = "127.0.0.1:{{port}}"
timeout_seconds = 1
''',
        encoding="utf-8",
    )
    git(git_repo, "add", ".solo-ai/config.toml")
    git(git_repo, "commit", "-m", "test: add blocked development command")
    approve(repo, load_verification_config(repo))
    task = start(repo, name=f"blocked dev start {status}")
    StateStore(repo).update_task(task["id"], status=status)

    with pytest.raises(SoloAIError, match="cannot start during"):
        dev_start(repo, task_id=task["id"], lease=task["lease"])
    assert StateStore(repo).task(task["id"])["active_operation"] is None


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
