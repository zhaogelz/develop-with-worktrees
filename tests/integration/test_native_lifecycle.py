from __future__ import annotations

import difflib
import argparse
import json
import sys
from pathlib import Path

import pytest
from conftest import git
from solo_ai import native_batches
from solo_ai import cli as cli_module
from solo_ai.candidate_batches import CandidateBatchStore
from solo_ai.config import CommandSpec, load_repo_config, load_verification_config
from solo_ai.lifecycle import (
    abandon,
    approve,
    commit_task,
    finish,
    initialize,
    ready,
    recover,
    start,
    withdraw_ready,
)
from solo_ai.native_batches import (
    repair_native_batch,
    run_native_batch,
    seal_native_batch,
)
from solo_ai.repo import GitRepo
from solo_ai.runtime_adapter import prepare_task_runtime
from solo_ai.safety import Finding, SensitiveContentError
from solo_ai.state import STATE_SCHEMA, StateStore
from solo_ai.util import ActionableSoloAIError, SoloAIError, path_identity


def _native_repo(
    path: Path, *, slots: int = 1, commands: list[CommandSpec] | None = None
) -> tuple[GitRepo, StateStore]:
    repo = GitRepo(path)
    initialize(
        repo,
        slots=slots,
        commands=commands or [CommandSpec(("git", "diff", "--check", "main...HEAD"))],
        accept=True,
        accept_static_only=False,
    )
    store = StateStore(repo)
    store.mutate(lambda state: state.update(schema_version=STATE_SCHEMA))
    store.ensure_slots(load_repo_config(repo))
    return repo, store


def test_native_start_defers_adapter_until_runtime_is_used(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, store = _native_repo(git_repo)
    script = git_repo / "adapter.py"
    script.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "context_path = Path(sys.argv[-1])\n"
        "context = json.loads(context_path.read_text(encoding='utf-8'))\n"
        "calls = context_path.parent.parent / 'calls.txt'\n"
        "if sys.argv[1] == 'activate':\n"
        "    output = Path(context['worktree']) / 'dist'\n"
        "    output.mkdir(exist_ok=True)\n"
        "    (output / 'bundle.txt').write_text(str(context['source_head']), encoding='utf-8')\n"
        "with calls.open('a', encoding='utf-8') as handle:\n"
        "    handle.write(sys.argv[1] + ':' + str(context.get('slot_generation')) + '\\n')\n",
        encoding="utf-8",
    )
    (git_repo / "lock.txt").write_text("dependency-v1\n", encoding="utf-8")
    ignore = git_repo / ".gitignore"
    previous_ignore = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
    ignore.write_text(previous_ignore + "dist/\n", encoding="utf-8")
    config_path = git_repo / ".solo-ai" / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "cleanup = { owned_paths = [] }", 'cleanup = { owned_paths = ["dist"] }'
        )
        + "\n[runtime_adapter]\n"
        + f"activate = ['{Path(sys.executable).as_posix()}', 'adapter.py', 'activate']\n"
        + f"release = ['{Path(sys.executable).as_posix()}', 'adapter.py', 'release']\n"
        + "input_paths = ['adapter.py', 'lock.txt']\n"
        + "environment = ['DWW_TEST_RUNTIME_SIGNATURE']\n"
        + "required_outputs = ['dist']\n",
        encoding="utf-8",
    )
    git(git_repo, "add", "adapter.py", "lock.txt", ".gitignore", ".solo-ai/config.toml")
    git(git_repo, "commit", "-m", "test: configure project runtime adapter")
    calls = repo.local_dir / "runtime-adapter" / "calls.txt"
    monkeypatch.setenv("DWW_TEST_RUNTIME_SIGNATURE", "first")

    task = start(repo, name="defer adapter until needed")
    assert task["runtime_activation"]["deferred"] is True
    assert not calls.exists()
    approve(repo, load_verification_config(repo))
    first = prepare_task_runtime(repo, task_id=task["id"], lease=task["lease"])
    second = prepare_task_runtime(repo, task_id=task["id"], lease=task["lease"])
    assert first["runtime_activation"]["reused"] is False
    assert second["runtime_activation"]["reused"] is True
    assert calls.read_text(encoding="utf-8").splitlines() == ["activate:1"]

    worktree = Path(task["worktree"])
    (worktree / "dist" / "bundle.txt").unlink()
    (worktree / "dist").rmdir()
    rebuilt = prepare_task_runtime(repo, task_id=task["id"], lease=task["lease"])
    assert rebuilt["runtime_activation"]["reused"] is False

    monkeypatch.setenv("DWW_TEST_RUNTIME_SIGNATURE", "second")
    third = prepare_task_runtime(repo, task_id=task["id"], lease=task["lease"])
    assert third["runtime_activation"]["reused"] is False
    (worktree / "feature.txt").write_text("ready\n", encoding="utf-8")
    commit_task(
        repo,
        task_id=task["id"],
        lease=task["lease"],
        message="test: source after prepare",
        paths=["feature.txt"],
    )
    finish(repo, task_id=task["id"], lease=task["lease"])
    assert calls.read_text(encoding="utf-8").splitlines() == [
        "activate:1",
        "activate:1",
        "activate:1",
        "release:1",
    ]
    assert store.task(task["id"])["runtime_release"]["result"] == "passed"


def test_native_fixed_slot_waits_for_real_delivery_then_reuses_branch(
    git_repo: Path,
) -> None:
    repo, store = _native_repo(git_repo)
    first = start(repo, name="first native task")
    worktree = Path(first["worktree"])
    branch = first["branch"]
    assert branch == "codex/slot-01"
    assert repo.branch(worktree) == branch
    assert repo.head(worktree) == first["base_head"]

    (worktree / "feature.txt").write_text("first\n", encoding="utf-8")
    committed = commit_task(
        repo,
        task_id=first["id"],
        lease=first["lease"],
        message="test: native source commit",
        paths=["feature.txt"],
    )
    source_head = committed["candidate_head"]
    assert ready(repo, task_id=first["id"], lease=first["lease"])["status"] == "ready"
    waiting = finish(repo, task_id=first["id"], lease=first["lease"])
    assert waiting["status"] == "waiting-integration"
    assert waiting["delivered"] is False
    assert store.read()["slots"]["01"]["task_id"] == first["id"]
    assert CandidateBatchStore(repo).read()["candidates"] == {}
    base_before = repo.head(git_repo)
    git(git_repo, "merge", "--no-ff", source_head, "-m", "test: deliver source")
    delivered_head = repo.head(git_repo)
    batch = {
        "id": "native-test-batch",
        "base_ref": "main",
        "base_head": base_before,
        "status": "sealed",
        "integration_head": delivered_head,
        "tasks": [
            {
                "task_id": first["id"],
                "slot_generation": first["slot_generation"],
                "branch": branch,
                "ready_head": source_head,
            }
        ],
    }
    store.seal_native_batch(batch)
    store.update_batch(batch["id"], status="validated")
    store.mark_native_promoted(batch["id"], integration_head=delivered_head)
    store.complete_native_delivery(
        first["id"],
        batch_id=batch["id"],
        integration_head=delivered_head,
        release_receipt={"result": "passed", "head": delivered_head},
    )
    git(worktree, "switch", "--detach", delivered_head)
    git(git_repo, "branch", "-D", branch)
    assert repo.branch(worktree) is None
    assert repo.ref_head(f"refs/heads/{branch}") is None

    second = start(repo, name="second native task")
    assert second["id"] != first["id"]
    assert second["slot_generation"] == first["slot_generation"] + 1
    assert second["worktree"] == first["worktree"]
    assert second["branch"] == branch
    assert repo.head(worktree) == delivered_head
    assert CandidateBatchStore(repo).read()["candidates"] == {}


def _deliver_slot_selection_tasks(
    repo: GitRepo,
    store: StateStore,
    git_repo: Path,
    tasks: list[dict[str, object]],
) -> None:
    sources = []
    for task in tasks:
        filename = f"feature-{task['slot_id']}.txt"
        (Path(str(task["worktree"])) / filename).write_text(
            f"slot {task['slot_id']}\n", encoding="utf-8"
        )
        committed = commit_task(
            repo,
            task_id=str(task["id"]),
            lease=str(task["lease"]),
            message=f"test: change from slot {task['slot_id']}",
            paths=[filename],
        )
        sources.append((task, committed["candidate_head"]))
        ready(repo, task_id=str(task["id"]), lease=str(task["lease"]))
        finish(repo, task_id=str(task["id"]), lease=str(task["lease"]))

    base_before = repo.head(git_repo)
    for task, source_head in sources:
        git(
            git_repo,
            "merge",
            "--no-ff",
            source_head,
            "-m",
            f"test: deliver slot {task['slot_id']}",
        )
    delivered_head = repo.head(git_repo)
    batch = {
        "id": "native-slot-selection-batch",
        "base_ref": "main",
        "base_head": base_before,
        "status": "sealed",
        "integration_head": delivered_head,
        "tasks": [
            {
                "task_id": task["id"],
                "slot_generation": task["slot_generation"],
                "branch": task["branch"],
                "ready_head": source_head,
            }
            for task, source_head in sources
        ],
    }
    store.seal_native_batch(batch)
    store.update_batch(batch["id"], status="validated")
    store.mark_native_promoted(batch["id"], integration_head=delivered_head)
    for task, _ in sources:
        store.complete_native_delivery(
            str(task["id"]),
            batch_id=batch["id"],
            integration_head=delivered_head,
            release_receipt={"result": "passed", "head": delivered_head},
        )


def test_native_start_prefers_latest_delivered_slot_and_keeps_parallel_slots_distinct(
    git_repo: Path,
) -> None:
    repo, store = _native_repo(git_repo, slots=3)
    first = start(repo, name="first parallel task")
    second = start(repo, name="second parallel task")
    assert first["slot_id"] != second["slot_id"]
    _deliver_slot_selection_tasks(repo, store, git_repo, [first, second])

    latest = start(repo, name="latest delivered task", request_id="latest-delivered")
    assert latest["slot_id"] == second["slot_id"]
    assert (
        start(repo, name="latest delivered task", request_id="latest-delivered")["id"]
        == latest["id"]
    )
    parallel = start(repo, name="another parallel task")
    assert parallel["slot_id"] == first["slot_id"]


def test_native_start_does_not_prefer_recent_abandonment_over_delivery(
    git_repo: Path,
) -> None:
    repo, store = _native_repo(git_repo, slots=3)
    first = start(repo, name="first delivered task")
    second = start(repo, name="second delivered task")
    _deliver_slot_selection_tasks(repo, store, git_repo, [first, second])

    recent = start(repo, name="maintenance to abandon")
    assert recent["slot_id"] == second["slot_id"]
    abandon(
        repo,
        task_id=recent["id"],
        lease=recent["lease"],
        confirm=recent["id"],
    )
    following = start(repo, name="prefer remaining successful slot")
    assert following["slot_id"] == first["slot_id"]


def test_native_start_skips_incompatible_idle_slot_without_capacity_delivery(
    git_repo: Path,
) -> None:
    repo, store = _native_repo(git_repo, slots=2)
    first = start(repo, name="first delivered task")
    second = start(repo, name="second delivered task")
    _deliver_slot_selection_tasks(repo, store, git_repo, [first, second])

    second_worktree = Path(second["worktree"])
    (second_worktree / "independent.txt").write_text("preserve\n", encoding="utf-8")
    git(second_worktree, "add", "independent.txt")
    git(second_worktree, "commit", "-m", "test: divergent idle branch")
    divergent_head = repo.head(second_worktree)
    chosen = start(repo, name="skip incompatible idle slot")
    assert chosen["slot_id"] == first["slot_id"]

    with pytest.raises(ActionableSoloAIError) as error:
        start(repo, name="no compatible idle slot")
    assert error.value.code == "NO_COMPATIBLE_SLOT"
    assert repo.head(second_worktree) == divergent_head
    assert store.read()["slots"][str(second["slot_id"])]["status"] == "idle"
    assert (
        store.read()["batches"]["native-slot-selection-batch"]["status"] == "completed"
    )


def test_native_start_preserves_detached_slot_with_unique_content(
    git_repo: Path,
) -> None:
    repo, store = _native_repo(git_repo)
    worktree = Path(str(store.read()["slots"]["01"]["path"]))
    git(git_repo, "worktree", "add", "--detach", str(worktree), "main")
    (worktree / "untracked.txt").write_text("unique\n", encoding="utf-8")
    old_head = repo.head(worktree)

    with pytest.raises(SoloAIError, match="Detached fixed slot contains"):
        start(repo, name="preserve detached slot content")

    assert repo.head(worktree) == old_head
    assert repo.branch(worktree) is None
    assert repo.ref_head("refs/heads/codex/slot-01") is None
    assert (worktree / "untracked.txt").read_text(encoding="utf-8") == "unique\n"
    assert store.read()["slots"]["01"]["status"] == "quarantined"


@pytest.mark.parametrize("advance_base", [False, True])
def test_native_zero_change_abandon_releases_and_reuses_fixed_slot(
    git_repo: Path, advance_base: bool
) -> None:
    repo, store = _native_repo(git_repo)
    first = start(repo, name="zero-change maintenance")
    worktree = Path(first["worktree"])
    assert (
        abandon(
            repo,
            task_id=first["id"],
            lease=first["lease"],
            confirm=first["id"],
            reason="maintenance complete without source changes",
        )["status"]
        == "abandoned"
    )
    assert repo.branch(worktree) is None
    assert repo.ref_head(f"refs/heads/{first['branch']}") is None
    if advance_base:
        (git_repo / "later.txt").write_text("accepted\n", encoding="utf-8")
        git(git_repo, "add", "later.txt")
        git(git_repo, "commit", "-m", "test: advance base after maintenance")
    second = start(repo, name="follow-up maintenance")
    assert second["slot_predecessor_task_id"] == first["id"]
    assert second["slot_generation"] == first["slot_generation"] + 1
    assert second["worktree"] == first["worktree"]
    assert second["branch"] == first["branch"]
    assert repo.branch(worktree) == first["branch"]
    assert repo.head(worktree) == repo.head(git_repo)


def test_abandon_releases_duplicate_task_heads_delivered_through_another_task(
    git_repo: Path,
) -> None:
    repo, store = _native_repo(git_repo, slots=2)
    first = start(repo, name="earlier source")
    first_worktree = Path(first["worktree"])
    (first_worktree / "earlier.txt").write_text("delivered\n", encoding="utf-8")
    git(first_worktree, "add", "earlier.txt")
    git(first_worktree, "commit", "-m", "test: earlier source")
    first_head = repo.head(first_worktree)

    second = start(repo, name="later source containing the earlier commit")
    second_worktree = Path(second["worktree"])
    git(second_worktree, "merge", "--no-ff", "--no-edit", first_head)
    (second_worktree / "later.txt").write_text("delivered\n", encoding="utf-8")
    git(second_worktree, "add", "later.txt")
    git(second_worktree, "commit", "-m", "test: later source")
    second_head = repo.head(second_worktree)
    git(git_repo, "merge", "--no-ff", "--no-edit", second_head)

    with pytest.raises(SoloAIError, match="referenced by active task"):
        abandon(
            repo,
            task_id=first["id"],
            lease=first["lease"],
            confirm=first["id"],
            reason="duplicate source already delivered",
        )
    own_delivery = dict(store.task(second["id"])["native_delivery"])
    store.update_task(
        second["id"], native_delivery={**own_delivery, "batch_id": "batch-own"}
    )
    with pytest.raises(SoloAIError, match="own delivery record"):
        abandon(
            repo,
            task_id=second["id"],
            lease=second["lease"],
            confirm=second["id"],
            reason="duplicate source already delivered",
        )
    store.update_task(second["id"], native_delivery=own_delivery)
    for task in (second, first):
        assert (
            abandon(
                repo,
                task_id=task["id"],
                lease=task["lease"],
                confirm=task["id"],
                reason="duplicate source already delivered",
            )["status"]
            == "abandoned"
        )
        assert repo.ref_head(f"refs/heads/{task['branch']}") is None
        assert store.task(task["id"])["abandonment"]["branch_tip"] in {
            first_head,
            second_head,
        }
        assert store.task(task["id"])["abandonment"]["integrated_elsewhere"] is True


def test_native_start_accepts_completed_abandonment_predecessor(git_repo: Path) -> None:
    repo, store = _native_repo(git_repo)
    first = start(repo, name="maintenance with exact release record")
    abandon(repo, task_id=first["id"], lease=first["lease"], confirm=first["id"])
    store.mutate(
        lambda state: state["slots"]["01"].update(released_task_id=first["id"])
    )
    second = start(repo, name="reuse exact released predecessor")
    assert second["slot_predecessor_task_id"] == first["id"]
    assert second["worktree"] == first["worktree"]
    assert repo.head(Path(second["worktree"])) == repo.head(git_repo)


def test_quarantined_start_preserves_unique_detached_commit(git_repo: Path) -> None:
    repo, store = _native_repo(git_repo)
    worktree = Path(str(store.read()["slots"]["01"]["path"]))
    git(git_repo, "worktree", "add", "--detach", str(worktree), "main")
    store.mutate(
        lambda state: state["slots"]["01"].update(
            released_worktree_identity=path_identity(worktree),
            released_managed_root_identity=path_identity(worktree.parent),
            released_worktree_resolved=str(worktree.resolve()),
            released_managed_root_resolved=str(worktree.parent.resolve()),
        )
    )
    task = store.allocate(
        load_repo_config(repo),
        name="interrupted fixed-slot start",
        branch="unused-native-branch",
        base_head=repo.head(git_repo),
        base_ref="main",
        base_worktree=git_repo,
        anchor_contract={
            "implementation_target": "fixed slot",
            "scope_boundary": "unactivated task",
            "acceptance_criteria": "preserve unique detached commit",
        },
    )
    store.quarantine(task["id"], "simulated interrupted activation")
    (worktree / "unique.txt").write_text("preserve me\n", encoding="utf-8")
    git(worktree, "add", "unique.txt")
    git(worktree, "commit", "-m", "test: unique detached commit")
    unique_head = repo.head(worktree)
    with pytest.raises(SoloAIError, match="detached HEAD"):
        recover(repo, task_id=task["id"])
    assert repo.head(worktree) == unique_head
    assert repo.branch(worktree) is None
    assert (worktree / "unique.txt").read_text(encoding="utf-8") == "preserve me\n"
    assert store.task(task["id"])["status"] == "quarantined"


@pytest.mark.parametrize("unique_commit", [False, True])
def test_quarantined_native_start_checks_branch_before_resuming(
    git_repo: Path, unique_commit: bool
) -> None:
    repo, store = _native_repo(git_repo)
    worktree = Path(str(store.read()["slots"]["01"]["path"]))
    git(git_repo, "worktree", "add", "--detach", str(worktree), "main")
    old_head = repo.head(worktree)
    store.mutate(
        lambda state: state["slots"]["01"].update(
            released_worktree_identity=path_identity(worktree),
            released_managed_root_identity=path_identity(worktree.parent),
            released_worktree_resolved=str(worktree.resolve()),
            released_managed_root_resolved=str(worktree.parent.resolve()),
        )
    )
    (git_repo / "later.txt").write_text("accepted\n", encoding="utf-8")
    git(git_repo, "add", "later.txt")
    git(git_repo, "commit", "-m", "test: advance base before interrupted start")
    base_head = repo.head(git_repo)
    task = store.allocate(
        load_repo_config(repo),
        name="interrupted fixed-slot branch creation",
        branch="unused-native-branch",
        base_head=base_head,
        base_ref="main",
        base_worktree=git_repo,
        anchor_contract={
            "implementation_target": "fixed slot",
            "scope_boundary": "interrupted native start",
            "acceptance_criteria": "exact base or preserve unique commit",
        },
    )
    store.quarantine(task["id"], "simulated interruption after branch creation")
    git(worktree, "switch", "-c", task["branch"], old_head)
    if unique_commit:
        (worktree / "unique.txt").write_text("preserve me\n", encoding="utf-8")
        git(worktree, "add", "unique.txt")
        git(worktree, "commit", "-m", "test: unique branch commit")
    before = repo.head(worktree)
    if unique_commit:
        with pytest.raises(SoloAIError, match="recorded base history"):
            recover(repo, task_id=task["id"])
        assert repo.head(worktree) == before
        assert (worktree / "unique.txt").read_text(encoding="utf-8") == "preserve me\n"
        assert store.task(task["id"])["status"] == "quarantined"
    else:
        assert recover(repo, task_id=task["id"])["status"] == "active"
        assert repo.head(worktree) == base_head
        assert store.task(task["id"])["candidate_head"] == base_head
        assert repo.branch(worktree) == task["branch"]


def test_native_release_transaction_drift_blocks_start_and_recovery(
    git_repo: Path,
) -> None:
    repo, store = _native_repo(git_repo)
    first = start(repo, name="completed maintenance")
    abandon(
        repo,
        task_id=first["id"],
        lease=first["lease"],
        confirm=first["id"],
        reason="completed without source changes",
    )
    worktree = Path(first["worktree"])
    released_head = repo.head(worktree)
    store.mutate(
        lambda state: state["tasks"][first["id"]]["abandonment"].update(
            phase="prepared"
        )
    )
    with pytest.raises(SoloAIError, match="released predecessor"):
        start(repo, name="blocked follow-up")
    quarantined_id = store.read()["slots"]["01"]["task_id"]
    assert quarantined_id is not None
    with pytest.raises(SoloAIError, match="released predecessor"):
        recover(repo, task_id=quarantined_id)
    assert repo.head(worktree) == released_head
    assert repo.branch(worktree) is None
    assert repo.ref_head(f"refs/heads/{first['branch']}") is None
    assert store.task(quarantined_id)["status"] == "quarantined"


def test_quarantined_native_start_rejects_slot_generation_drift_before_git_write(
    git_repo: Path,
) -> None:
    repo, store = _native_repo(git_repo)
    worktree = Path(str(store.read()["slots"]["01"]["path"]))
    git(git_repo, "worktree", "add", "--detach", str(worktree), "main")
    old_head = repo.head(worktree)
    store.mutate(
        lambda state: state["slots"]["01"].update(
            released_worktree_identity=path_identity(worktree),
            released_managed_root_identity=path_identity(worktree.parent),
            released_worktree_resolved=str(worktree.resolve()),
            released_managed_root_resolved=str(worktree.parent.resolve()),
        )
    )
    task = store.allocate(
        load_repo_config(repo),
        name="generation drift",
        branch="unused-native-branch",
        base_head=old_head,
        base_ref="main",
        base_worktree=git_repo,
        anchor_contract={
            "implementation_target": "fixed slot",
            "scope_boundary": "quarantined pre-activation",
            "acceptance_criteria": "no Git write on generation drift",
        },
    )
    store.quarantine(task["id"], "simulated interruption")
    store.mutate(
        lambda state: state["slots"]["01"].update(
            generation=task["slot_generation"] + 1
        )
    )
    with pytest.raises(SoloAIError, match="slot generation"):
        recover(repo, task_id=task["id"])
    assert repo.branch(worktree) is None
    assert repo.head(worktree) == old_head
    assert repo.ref_head(f"refs/heads/{task['branch']}") is None
    assert store.task(task["id"])["status"] == "quarantined"


def test_native_abandonment_release_recovers_once_and_reuses_slot(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, store = _native_repo(git_repo)
    first = start(repo, name="interrupted maintenance close")
    original = StateStore.publish_abandonment_release

    def interrupted(*args: object, **kwargs: object) -> None:
        raise RuntimeError("interrupted release publication")

    monkeypatch.setattr(StateStore, "publish_abandonment_release", interrupted)
    with pytest.raises(RuntimeError, match="interrupted release publication"):
        abandon(repo, task_id=first["id"], lease=first["lease"], confirm=first["id"])
    assert store.task(first["id"])["status"] == "abandoned"
    assert store.read()["slots"]["01"]["status"] == "release-checking"
    assert repo.ref_head(f"refs/heads/{first['branch']}") is None
    monkeypatch.setattr(StateStore, "publish_abandonment_release", original)
    assert recover(repo, task_id=first["id"])["status"] == "abandoned"
    assert recover(repo, task_id=first["id"])["status"] == "abandoned"
    assert store.read()["slots"]["01"]["status"] == "idle"
    second = start(repo, name="reuse recovered close")
    assert second["slot_predecessor_task_id"] == first["id"]
    assert second["slot_generation"] == first["slot_generation"] + 1
    assert repo.head(Path(second["worktree"])) == repo.head(git_repo)


def test_recover_explicitly_ends_clean_unactivated_fixed_slot_failure(
    git_repo: Path,
) -> None:
    repo, store = _native_repo(git_repo)
    slot = store.read()["slots"]["01"]
    worktree = Path(str(slot["path"]))
    git(git_repo, "worktree", "add", "--detach", str(worktree), "main")
    old_head = repo.head(worktree)
    store.mutate(
        lambda state: state["slots"]["01"].update(
            released_worktree_identity=path_identity(worktree),
            released_managed_root_identity=path_identity(worktree.parent),
            released_worktree_resolved=str(worktree.resolve()),
            released_managed_root_resolved=str(worktree.parent.resolve()),
        )
    )
    task = store.allocate(
        load_repo_config(repo),
        name="unactivated duplicate",
        branch="unused-native-branch",
        base_head=old_head,
        base_ref="main",
        base_worktree=git_repo,
        anchor_contract={
            "implementation_target": "fixed slot",
            "scope_boundary": "clean unactivated task",
            "acceptance_criteria": "terminal audit",
        },
    )
    store.quarantine(task["id"], "Fixed slot is not attached to its recorded branch")
    (git_repo / "later.txt").write_text("accepted\n", encoding="utf-8")
    git(git_repo, "add", "later.txt")
    git(git_repo, "commit", "-m", "test: advance accepted base")
    reason = "Duplicate task has no source; replacement is already delivered"

    with pytest.raises(SoloAIError, match="exact task id"):
        recover(
            repo,
            task_id=task["id"],
            abandon_unactivated=True,
            confirm="other-task",
            reason=reason,
        )
    unique = worktree / "unique.txt"
    unique.write_text("preserve me\n", encoding="utf-8")
    with pytest.raises(SoloAIError, match="cleanliness"):
        recover(
            repo,
            task_id=task["id"],
            abandon_unactivated=True,
            confirm=task["id"],
            reason=reason,
        )
    assert unique.read_text(encoding="utf-8") == "preserve me\n"
    assert store.task(task["id"])["status"] == "quarantined"
    unique.unlink()
    result = recover(
        repo,
        task_id=task["id"],
        abandon_unactivated=True,
        confirm=task["id"],
        reason=reason,
    )

    assert result["status"] == "abandoned"
    assert result["recovery"] == "preactivation-fixed-slot-release"
    assert result["reason"] == reason
    assert store.task(task["id"])["preactivation_release"]["tracked_files"] == []
    assert store.read()["slots"]["01"]["status"] == "idle"
    assert repo.branch(worktree) is None
    assert repo.head(worktree) == repo.head(git_repo)
    assert recover(repo, task_id=task["id"]) == result

    reused = start(repo, name="next fixed slot task")
    assert reused["slot_id"] == "01"
    assert repo.branch(worktree) == "codex/slot-01"


def test_native_batch_keeps_source_in_real_merge_history(git_repo: Path) -> None:
    repo, store = _native_repo(git_repo)
    task = start(repo, name="merge original task head")
    worktree = Path(task["worktree"])
    (worktree / "feature.txt").write_text("delivered\n", encoding="utf-8")
    source = commit_task(
        repo,
        task_id=task["id"],
        lease=task["lease"],
        message="test: preserve native source",
        paths=["feature.txt"],
    )["candidate_head"]
    finish(repo, task_id=task["id"], lease=task["lease"])
    batch = seal_native_batch(
        repo,
        task_ids=[task["id"]],
        cause="round-complete",
        reason="the single task round is complete",
    )
    approve(repo, load_verification_config(repo))

    completed = run_native_batch(repo, batch_id=batch["id"])

    assert completed["status"] == "completed"
    assert completed["validation_outcome"] == "passed"
    assert repo.head(git_repo) == completed["integration_head"]
    assert repo.is_ancestor(source, repo.head(git_repo))
    merge = completed["merge_records"][0]
    assert merge["source_head"] == source
    assert merge["previous_head"] == batch["base_before"]
    assert repo.git(
        ["rev-list", "--parents", "-n", "1", merge["merge_head"]]
    ).stdout.strip().split() == [
        merge["merge_head"],
        batch["base_before"],
        source,
    ]
    assert store.read()["slots"]["01"]["status"] == "idle"
    assert CandidateBatchStore(repo).read()["candidates"] == {}


def test_native_batch_activates_and_releases_runtime_around_full(
    git_repo: Path,
) -> None:
    repo, store = _native_repo(git_repo)
    adapter = git_repo / "batch_adapter.py"
    adapter.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "context = json.loads(Path(sys.argv[-1]).read_text(encoding='utf-8'))\n"
        "events = Path(sys.argv[-1]).parent.parent / 'batch-events.jsonl'\n"
        "with events.open('a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps({'operation': sys.argv[1], "
        "'cycle': context['runtime_cycle'], "
        "'task_ids': context['task_ids'], "
        "'binding': context['worktree_binding'], "
        "'outcome': context.get('validation_outcome')}) + '\\n')\n",
        encoding="utf-8",
    )
    config = git_repo / ".solo-ai" / "config.toml"
    config.write_text(
        config.read_text(encoding="utf-8")
        + "\n[runtime_adapter]\n"
        + f"batch_activate = ['{Path(sys.executable).as_posix()}', 'batch_adapter.py', 'activate']\n"
        + f"batch_release = ['{Path(sys.executable).as_posix()}', 'batch_adapter.py', 'release']\n"
        + "input_paths = ['batch_adapter.py']\n",
        encoding="utf-8",
    )
    git(git_repo, "add", "batch_adapter.py", ".solo-ai/config.toml")
    git(git_repo, "commit", "-m", "test: configure batch adapter")
    task = start(repo, name="native batch runtime")
    (Path(task["worktree"]) / "feature.txt").write_text("ready\n", encoding="utf-8")
    commit_task(
        repo,
        task_id=task["id"],
        lease=task["lease"],
        message="test: native batch source",
        paths=["feature.txt"],
    )
    finish(repo, task_id=task["id"], lease=task["lease"])
    batch = seal_native_batch(
        repo, task_ids=[task["id"]], cause="user", reason="deliver now"
    )
    approve(repo, load_verification_config(repo))
    delivered = run_native_batch(repo, batch_id=batch["id"])
    events = [
        json.loads(line)
        for line in (repo.local_dir / "runtime-adapter" / "batch-events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [event["operation"] for event in events] == ["activate", "release"]
    assert [event["cycle"] for event in events] == [1, 1]
    assert all(event["task_ids"] == [task["id"]] for event in events)
    assert all(event["binding"]["owner"] == batch["id"] for event in events)
    assert events[-1]["outcome"] == "passed"
    assert delivered["runtime_release"]["result"] == "passed"
    request = cli_module._approval_request(
        repo,
        argparse.Namespace(
            task=None,
            slot=None,
            batch=batch["id"],
            candidate=None,
            scope="batch-full",
        ),
    )
    assert request["cwd"] == Path(batch["worktree"])


@pytest.mark.parametrize("operation", ["activate", "release"])
def test_native_batch_preserves_runtime_workspace_changes_before_promotion(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    repo, store = _native_repo(git_repo)
    (git_repo / "guard.txt").write_text("clean\n", encoding="utf-8")
    git(git_repo, "add", "guard.txt")
    git(git_repo, "commit", "-m", "test: tracked runtime guard")
    task = start(repo, name=f"dirty batch {operation}")
    (Path(task["worktree"]) / "feature.txt").write_text("ready\n", encoding="utf-8")
    commit_task(
        repo,
        task_id=task["id"],
        lease=task["lease"],
        message="test: native runtime source",
        paths=["feature.txt"],
    )
    finish(repo, task_id=task["id"], lease=task["lease"])
    batch = seal_native_batch(
        repo, task_ids=[task["id"]], cause="user", reason="deliver now"
    )
    approve(repo, load_verification_config(repo))
    target_before = repo.head(git_repo)
    original = (
        native_batches.activate_batch_runtime
        if operation == "activate"
        else native_batches.release_batch_runtime
    )
    full_calls = []
    real_validate = native_batches.validate

    def record_full(*args: object, **kwargs: object) -> dict[str, object]:
        full_calls.append(True)
        return real_validate(*args, **kwargs)

    def dirty_runtime(*args: object, **kwargs: object) -> dict[str, object]:
        receipt = original(*args, **kwargs)
        (Path(batch["worktree"]) / "guard.txt").write_text("dirty\n", encoding="utf-8")
        return receipt

    monkeypatch.setattr(native_batches, "validate", record_full)
    monkeypatch.setattr(
        native_batches,
        "activate_batch_runtime"
        if operation == "activate"
        else "release_batch_runtime",
        dirty_runtime,
    )
    with pytest.raises(SoloAIError, match="workspace changed"):
        run_native_batch(repo, batch_id=batch["id"])
    paused = store.native_batch(batch["id"])
    assert paused["status"] == (
        "runtime_activation_pending"
        if operation == "activate"
        else "runtime_release_pending"
    )
    assert repo.head(git_repo) == target_before
    assert (Path(batch["worktree"]) / "guard.txt").read_text(
        encoding="utf-8"
    ) == "dirty\n"
    assert len(full_calls) == (0 if operation == "activate" else 1)
    attempt = paused.get("validation_attempt")

    with pytest.raises(SoloAIError, match="workspace changed"):
        run_native_batch(repo, batch_id=batch["id"])
    assert repo.head(git_repo) == target_before
    assert len(full_calls) == (0 if operation == "activate" else 1)

    monkeypatch.setattr(
        native_batches,
        "activate_batch_runtime"
        if operation == "activate"
        else "release_batch_runtime",
        original,
    )
    (Path(batch["worktree"]) / "guard.txt").write_text("clean\n", encoding="utf-8")
    completed = run_native_batch(repo, batch_id=batch["id"])
    assert completed["status"] == "completed"
    assert len(full_calls) == 1
    if operation == "release":
        assert completed["validation_attempt"] == attempt


def test_native_finish_explicit_tail_delivers_without_candidate(git_repo: Path) -> None:
    repo, store = _native_repo(git_repo)
    task = start(repo, name="finish a native tail")
    worktree = Path(task["worktree"])
    (worktree / "tail.txt").write_text("ready\n", encoding="utf-8")
    source = commit_task(
        repo,
        task_id=task["id"],
        lease=task["lease"],
        message="test: native tail",
        paths=["tail.txt"],
    )["candidate_head"]
    approve(repo, load_verification_config(repo))

    result = finish(
        repo,
        task_id=task["id"],
        lease=task["lease"],
        cause="round-complete",
        reason="the development round has ended",
    )

    assert result["outcome"] == "delivered"
    assert result["delivered"] is True
    assert repo.is_ancestor(source, repo.head(git_repo))
    assert store.read()["slots"]["01"]["status"] == "idle"
    assert CandidateBatchStore(repo).read()["candidates"] == {}


def test_native_serial_batches_freeze_the_promoted_main_as_next_base(
    git_repo: Path,
) -> None:
    repo, store = _native_repo(git_repo, slots=2)
    approve(repo, load_verification_config(repo))
    first = start(repo, name="first sibling")
    second = start(repo, name="second sibling")
    sources = []
    for task, filename in ((first, "one.txt"), (second, "two.txt")):
        (Path(task["worktree"]) / filename).write_text(filename, encoding="utf-8")
        sources.append(
            commit_task(
                repo,
                task_id=task["id"],
                lease=task["lease"],
                message=f"test: {filename}",
                paths=[filename],
            )["candidate_head"]
        )
    finish(repo, task_id=first["id"], lease=first["lease"])
    second_result = finish(
        repo,
        task_id=second["id"],
        lease=second["lease"],
        cause="round-complete",
        reason="both development producers have finished",
    )
    assert second_result["delivered"] is True
    first_batch = store.native_batch(second_result["batch_id"])
    assert first_batch["status"] == "completed"
    assert len(first_batch["tasks"]) == 2
    assert all(repo.is_ancestor(source, repo.head(git_repo)) for source in sources)

    next_task = start(repo, name="next batch producer")
    (Path(next_task["worktree"]) / "next.txt").write_text("next", encoding="utf-8")
    next_source = commit_task(
        repo,
        task_id=next_task["id"],
        lease=next_task["lease"],
        message="test: next batch source",
        paths=["next.txt"],
    )["candidate_head"]
    next_result = finish(
        repo,
        task_id=next_task["id"],
        lease=next_task["lease"],
        cause="round-complete",
        reason="the next development round has finished",
    )
    second_batch = store.native_batch(next_result["batch_id"])
    assert second_batch["base_before"] == first_batch["integration_head"]
    assert second_batch["status"] == "completed"
    assert repo.is_ancestor(next_source, repo.head(git_repo))


def test_native_target_has_only_one_active_batch(git_repo: Path) -> None:
    repo, _store = _native_repo(git_repo, slots=2)
    tasks = [start(repo, name=f"sibling {index}") for index in (1, 2)]
    for index, task in enumerate(tasks, start=1):
        filename = f"sibling-{index}.txt"
        (Path(task["worktree"]) / filename).write_text(filename, encoding="utf-8")
        commit_task(
            repo,
            task_id=task["id"],
            lease=task["lease"],
            message=f"test: {filename}",
            paths=[filename],
        )
        finish(repo, task_id=task["id"], lease=task["lease"])

    first = seal_native_batch(
        repo,
        task_ids=[tasks[0]["id"]],
        cause="user",
        reason="integrate the first source now",
    )
    assert first["status"] == "sealed"
    with pytest.raises(SoloAIError, match="Another batch owns this target"):
        seal_native_batch(
            repo,
            task_ids=[tasks[1]["id"]],
            cause="user",
            reason="integrate another source now",
        )


def test_native_promoted_release_failure_retries_only_cleanup(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, store = _native_repo(git_repo)
    task = start(repo, name="release after promotion")
    worktree = Path(task["worktree"])
    (worktree / "delivery.txt").write_text("delivered\n", encoding="utf-8")
    commit_task(
        repo,
        task_id=task["id"],
        lease=task["lease"],
        message="test: retain promoted result",
        paths=["delivery.txt"],
    )
    finish(repo, task_id=task["id"], lease=task["lease"])
    batch = seal_native_batch(
        repo,
        task_ids=[task["id"]],
        cause="round-complete",
        reason="the delivery round has ended",
    )
    approve(repo, load_verification_config(repo))
    original_release = native_batches._release
    unknown = worktree / "unknown.txt"

    def block_first_release(*args: object, **kwargs: object) -> dict[str, object]:
        unknown.write_text("preserve me", encoding="utf-8")
        return original_release(*args, **kwargs)

    monkeypatch.setattr(native_batches, "_release", block_first_release)
    with pytest.raises(ActionableSoloAIError, match="preserve it"):
        run_native_batch(repo, batch_id=batch["id"])
    promoted = store.native_batch(batch["id"])
    assert promoted["status"] == "promoted"
    assert store.task(task["id"])["status"] == "delivered-pending-release"
    promoted_head = repo.head(git_repo)
    attempt = promoted["validation_attempt"]
    proof = promoted["proof"]

    monkeypatch.setattr(native_batches, "_release", original_release)
    unknown.unlink()
    completed = run_native_batch(repo, batch_id=batch["id"])
    assert completed["status"] == "completed"
    assert completed["validation_attempt"] == attempt
    assert completed["proof"] == proof
    assert repo.head(git_repo) == promoted_head


def test_native_start_uses_real_capacity_pressure_to_deliver_two_ready_tasks(
    git_repo: Path,
) -> None:
    repo, store = _native_repo(git_repo, slots=10)
    approve(repo, load_verification_config(repo))
    ready_tasks = [start(repo, name=f"ready source {index}") for index in (1, 2)]
    sources = []
    for index, task in enumerate(ready_tasks, start=1):
        filename = f"capacity-{index}.txt"
        (Path(task["worktree"]) / filename).write_text(
            f"source {index}\n", encoding="utf-8"
        )
        sources.append(
            commit_task(
                repo,
                task_id=task["id"],
                lease=task["lease"],
                message=f"test: capacity source {index}",
                paths=[filename],
            )["candidate_head"]
        )
        assert (
            finish(repo, task_id=task["id"], lease=task["lease"])["status"]
            == "waiting-integration"
        )
    producers = [start(repo, name=f"producer {index}") for index in range(8)]
    assert len(store.read()["batches"]) == 0

    next_task = start(repo, name="capacity successor", request_id="capacity-next")

    assert next_task["status"] == "active"
    assert len(store.read()["batches"]) == 1
    batch = next(iter(store.read()["batches"].values()))
    assert batch["status"] == "completed"
    assert batch["tail_request"]["cause"] == "capacity"
    assert batch["capacity_request_id"] == "capacity-next"
    assert [member["task_id"] for member in batch["tasks"]] == [
        task["id"] for task in ready_tasks
    ]
    assert all(repo.is_ancestor(source, repo.head(git_repo)) for source in sources)
    assert all(store.task(task["id"])["status"] == "active" for task in producers)
    assert CandidateBatchStore(repo).read()["candidates"] == {}


def test_native_capacity_does_not_seal_when_all_slots_are_developing(
    git_repo: Path,
) -> None:
    repo, store = _native_repo(git_repo, slots=10)
    tasks = [start(repo, name=f"producer {index}") for index in range(10)]
    with pytest.raises(ActionableSoloAIError) as blocked:
        start(repo, name="no available task slot")
    assert blocked.value.code == "NO_FREE_SLOT"
    assert len(store.read()["batches"]) == 0
    assert all(store.task(task["id"])["status"] == "active" for task in tasks)


def test_native_capacity_waits_for_existing_target_batch(git_repo: Path) -> None:
    repo, store = _native_repo(git_repo)
    task = start(repo, name="waiting source")
    (Path(task["worktree"]) / "queued.txt").write_text("ready", encoding="utf-8")
    commit_task(
        repo,
        task_id=task["id"],
        lease=task["lease"],
        message="test: queued source",
        paths=["queued.txt"],
    )
    finish(repo, task_id=task["id"], lease=task["lease"])
    batch = seal_native_batch(
        repo, task_ids=[task["id"]], cause="user", reason="explicit first batch"
    )
    with pytest.raises(ActionableSoloAIError) as blocked:
        start(repo, name="wait for target owner")
    assert blocked.value.code == "NO_FREE_SLOT"
    assert store.native_batch(batch["id"])["status"] == "sealed"
    assert len(store.read()["batches"]) == 1


def test_native_ready_withdrawal_requires_exact_owner_and_new_head(
    git_repo: Path,
) -> None:
    repo, store = _native_repo(git_repo)
    task = start(repo, name="withdraw a frozen head")
    worktree = Path(task["worktree"])
    (worktree / "source.txt").write_text("first", encoding="utf-8")
    first_head = commit_task(
        repo,
        task_id=task["id"],
        lease=task["lease"],
        message="test: first ready source",
        paths=["source.txt"],
    )["candidate_head"]
    ready(repo, task_id=task["id"], lease=task["lease"])
    with pytest.raises(SoloAIError):
        withdraw_ready(
            repo, task_id=task["id"], lease="stale-lease", reason="repair source"
        )
    (worktree / "untracked.txt").write_text("unknown", encoding="utf-8")
    with pytest.raises(SoloAIError, match="changed|clean"):
        withdraw_ready(
            repo, task_id=task["id"], lease=task["lease"], reason="repair source"
        )
    (worktree / "untracked.txt").unlink()
    withdrawn = withdraw_ready(
        repo, task_id=task["id"], lease=task["lease"], reason="repair source"
    )
    assert withdrawn["status"] == "active"
    with pytest.raises(SoloAIError, match="new task head"):
        ready(repo, task_id=task["id"], lease=task["lease"])
    (worktree / "source.txt").write_text("second", encoding="utf-8")
    second_head = commit_task(
        repo,
        task_id=task["id"],
        lease=task["lease"],
        message="test: repaired ready source",
        paths=["source.txt"],
    )["candidate_head"]
    assert second_head != first_head
    assert ready(repo, task_id=task["id"], lease=task["lease"])["status"] == "ready"
    assert (
        store.task(task["id"])["native_delivery"]["attempts"][-1]["ready_head"]
        == first_head
    )


def test_native_full_repair_uses_new_head_and_keeps_failed_attempt(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, store = _native_repo(
        git_repo,
        commands=[CommandSpec(("git", "grep", "-q", "pass", "--", "repair.txt"))],
    )
    approve(repo, load_verification_config(repo))
    task = start(repo, name="repair one shared script")
    (Path(task["worktree"]) / "repair.txt").write_text("fail\n", encoding="utf-8")
    source = commit_task(
        repo,
        task_id=task["id"],
        lease=task["lease"],
        message="test: source needs integration repair",
        paths=["repair.txt"],
    )["candidate_head"]
    finish(repo, task_id=task["id"], lease=task["lease"])
    batch = seal_native_batch(
        repo, task_ids=[task["id"]], cause="user", reason="deliver this task"
    )
    with pytest.raises(SoloAIError):
        run_native_batch(repo, batch_id=batch["id"])
    failed = store.native_batch(batch["id"])
    assert failed["status"] == "validation-failed"
    failed_attempt = failed["validation_attempt"]
    old_head = failed["integration_head"]
    with pytest.raises(SoloAIError, match="unchanged inputs"):
        run_native_batch(repo, batch_id=batch["id"])
    assert store.native_batch(batch["id"])["validation_attempts"] == [failed_attempt]

    patch = git_repo.parent / "repair.patch"
    patch.write_text(
        "diff --git a/repair.txt b/repair.txt\n"
        "--- a/repair.txt\n"
        "+++ b/repair.txt\n"
        "@@ -1 +1 @@\n"
        "-fail\n"
        "+pass\n",
        encoding="utf-8",
    )
    real_read = native_batches.read_validation_attempt
    monkeypatch.setattr(
        native_batches,
        "read_validation_attempt",
        lambda *_args: {"state": "planned"},
    )
    with pytest.raises(SoloAIError, match="stopped ordinary failure"):
        repair_native_batch(
            repo,
            batch_id=batch["id"],
            expected_head=old_head,
            patch_file=patch,
            paths=["repair.txt"],
            message="test: fix integration compatibility",
            reason="shared script needs a portable value",
        )
    monkeypatch.setattr(native_batches, "read_validation_attempt", real_read)
    real_remember = native_batches.batch_workspace.remember_head

    def interrupted_receipt(*_args: object) -> str:
        raise SoloAIError("interrupted after repair commit")

    monkeypatch.setattr(
        native_batches.batch_workspace, "remember_head", interrupted_receipt
    )
    with pytest.raises(SoloAIError, match="interrupted after repair commit"):
        repair_native_batch(
            repo,
            batch_id=batch["id"],
            expected_head=old_head,
            patch_file=patch,
            paths=["repair.txt"],
            message="test: fix integration compatibility",
            reason="shared script needs a portable value",
        )
    assert store.native_batch(batch["id"])["status"] == "repairing"
    assert repo.head(Path(batch["worktree"])) != old_head
    monkeypatch.setattr(native_batches.batch_workspace, "remember_head", real_remember)
    repaired = repair_native_batch(
        repo,
        batch_id=batch["id"],
        expected_head=old_head,
        patch_file=patch,
        paths=["repair.txt"],
        message="test: fix integration compatibility",
        reason="shared script needs a portable value",
    )
    assert repaired["status"] == "composed"
    repair_head = repaired["integration_head"]
    assert repair_head != old_head
    assert repo.is_ancestor(source, repair_head)
    assert repaired["repair_records"][0]["validation_attempt"] == failed_attempt
    assert len(store.read()["tasks"]) == 1
    assert CandidateBatchStore(repo).read()["candidates"] == {}

    completed = run_native_batch(repo, batch_id=batch["id"])
    assert completed["status"] == "completed"
    assert completed["validation_attempts"][0] == failed_attempt
    assert len(completed["validation_attempts"]) == 2
    assert completed["integration_head"] == repair_head
    assert repo.head(git_repo) == repair_head
    assert repo.is_ancestor(source, repo.head(git_repo))


def test_native_sensitive_preflight_records_failure_then_repairs_exact_policy(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, store = _native_repo(git_repo)
    approve(repo, load_verification_config(repo))
    task = start(repo, name="repair generated asset scan policy")
    (Path(task["worktree"]) / "source.txt").write_text("safe\n", encoding="utf-8")
    commit_task(
        repo,
        task_id=task["id"],
        lease=task["lease"],
        message="test: safe source",
        paths=["source.txt"],
    )
    finish(repo, task_id=task["id"], lease=task["lease"])
    batch = seal_native_batch(
        repo, task_ids=[task["id"]], cause="user", reason="deliver this task"
    )
    config_path = Path(task["worktree"]) / ".solo-ai" / "config.toml"
    original = config_path.read_text(encoding="utf-8")
    updated = original.replace(
        "sensitive_allowlist = []", 'sensitive_allowlist = ["source.txt"]'
    )
    assert updated != original
    patch = git_repo.parent / "preflight-policy.patch"
    patch.write_text(
        "diff --git a/.solo-ai/config.toml b/.solo-ai/config.toml\n"
        + "".join(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                updated.splitlines(keepends=True),
                fromfile="a/.solo-ai/config.toml",
                tofile="b/.solo-ai/config.toml",
            )
        ),
        encoding="utf-8",
    )
    real_require_safe = native_batches.require_safe

    def preflight_scan(*args: object, **kwargs: object) -> None:
        scan_worktree = Path(str(kwargs["cwd"]))
        if (
            "source.txt"
            not in load_repo_config(repo, cwd=scan_worktree).sensitive_allowlist
        ):
            raise SensitiveContentError([Finding("source.txt", 1, "assigned-secret")])
        real_require_safe(*args, **kwargs)

    monkeypatch.setattr(native_batches, "require_safe", preflight_scan)
    with pytest.raises(SensitiveContentError):
        run_native_batch(repo, batch_id=batch["id"])
    failed = store.native_batch(batch["id"])
    assert failed["status"] == "preflight-failed"
    assert failed.get("validation_attempt") is None
    assert failed["preflight_failure"]["findings"] == [
        {"path": "source.txt", "line": 1, "rule": "assigned-secret"}
    ]
    with pytest.raises(SoloAIError, match="unchanged inputs"):
        run_native_batch(repo, batch_id=batch["id"])
    assert store.native_batch(batch["id"]).get("validation_attempt") is None
    with pytest.raises(SoloAIError, match="only the exact policy path"):
        repair_native_batch(
            repo,
            batch_id=batch["id"],
            expected_head=failed["integration_head"],
            patch_file=patch,
            paths=["source.txt"],
            message="test: reject source path",
            reason="source repair is outside this recovery path",
        )
    repaired = repair_native_batch(
        repo,
        batch_id=batch["id"],
        expected_head=failed["integration_head"],
        patch_file=patch,
        paths=[".solo-ai/config.toml"],
        message="test: allow reviewed generated asset",
        reason="generated asset has the reviewed false positive",
    )
    assert repaired["status"] == "composed"
    assert repaired["preflight_failure"] is None
    assert repaired["repair_records"][-1]["previous_status"] == "preflight-failed"
    assert (
        repaired["repair_records"][-1]["preflight_failure"]
        == failed["preflight_failure"]
    )
    with pytest.raises(SoloAIError, match="failed preflight"):
        repair_native_batch(
            repo,
            batch_id=batch["id"],
            expected_head=repaired["integration_head"],
            patch_file=patch,
            paths=[".solo-ai/config.toml"],
            message="test: reject unfailed composed batch",
            reason="the recorded preflight finding was already repaired",
        )
    completed = run_native_batch(repo, batch_id=batch["id"])
    assert completed["status"] == "completed"
    assert completed["validation_attempt"] is not None
    assert repo.head(git_repo) == completed["integration_head"]


def test_native_merge_conflict_repair_preserves_both_source_parents(
    git_repo: Path,
) -> None:
    repo, store = _native_repo(git_repo, slots=2)
    approve(repo, load_verification_config(repo))
    (git_repo / "shared.txt").write_text("base\n", encoding="utf-8")
    git(git_repo, "add", "shared.txt")
    git(git_repo, "commit", "-m", "test: common baseline")
    tasks = [start(repo, name=f"conflicting source {index}") for index in (1, 2)]
    sources = []
    for index, task in enumerate(tasks, start=1):
        (Path(task["worktree"]) / "shared.txt").write_text(
            f"source {index}\n", encoding="utf-8"
        )
        sources.append(
            commit_task(
                repo,
                task_id=task["id"],
                lease=task["lease"],
                message=f"test: conflicting source {index}",
                paths=["shared.txt"],
            )["candidate_head"]
        )
        finish(repo, task_id=task["id"], lease=task["lease"])
    batch = seal_native_batch(
        repo, task_ids=[task["id"] for task in tasks], cause="user", reason="merge both"
    )
    with pytest.raises(ActionableSoloAIError) as conflict:
        run_native_batch(repo, batch_id=batch["id"])
    assert conflict.value.code == "NATIVE_MERGE_CONFLICT"
    conflicted = store.native_batch(batch["id"])
    assert conflicted["status"] == "conflicted"
    worktree = Path(conflicted["worktree"])
    original = (worktree / "shared.txt").read_text(encoding="utf-8")
    resolved = "source 1\nsource 2\n"
    patch = git_repo.parent / "conflict-repair.patch"
    patch.write_text(
        "diff --git a/shared.txt b/shared.txt\n"
        + "".join(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                resolved.splitlines(keepends=True),
                fromfile="a/shared.txt",
                tofile="b/shared.txt",
            )
        ),
        encoding="utf-8",
    )
    repaired = repair_native_batch(
        repo,
        batch_id=batch["id"],
        expected_head=conflicted["integration_head"],
        patch_file=patch,
        paths=["shared.txt"],
        message="test: resolve both source edits",
        reason="both values are required",
    )
    assert repaired["status"] == "composing"
    merge_head = repaired["integration_head"]
    assert repo.git(
        ["rev-list", "--parents", "-n", "1", merge_head], cwd=worktree
    ).stdout.strip().split() == [
        merge_head,
        conflicted["integration_head"],
        sources[1],
    ]
    completed = run_native_batch(repo, batch_id=batch["id"])
    assert completed["status"] == "completed"
    assert all(repo.is_ancestor(source, repo.head(git_repo)) for source in sources)
    assert CandidateBatchStore(repo).read()["candidates"] == {}
