from __future__ import annotations

import os
from pathlib import Path

import pytest

from conftest import git
from solo_ai import slot_disposal
from solo_ai.cli import _prune
from solo_ai.config import CommandSpec, load_repo_config
from solo_ai.lifecycle import (
    abandon,
    initialize,
    reclaim_retained_worktree,
    start,
)
from solo_ai.native_migration import enable_native_migration
from solo_ai.repo import GitRepo
from solo_ai.state import LEGACY_STATE_SCHEMA, STATE_SCHEMA, StateStore
from solo_ai.util import (
    SoloAIError,
    atomic_write_json,
    path_identity,
    read_json,
    sha256_text,
    stable_json,
)

pytestmark = pytest.mark.dww_fast


def _repo(path: Path, *, native: bool = True) -> tuple[GitRepo, StateStore]:
    repo = GitRepo(path)
    initialize(
        repo,
        slots=1,
        commands=[CommandSpec(("git", "diff", "--check", "main...HEAD"))],
        accept=True,
        accept_static_only=False,
    )
    store = StateStore(repo)
    store.mutate(
        lambda state: state.update(
            schema_version=STATE_SCHEMA if native else LEGACY_STATE_SCHEMA
        )
    )
    store.ensure_slots(load_repo_config(repo))
    return repo, store


def _execute(
    repo: GitRepo, plan: dict[str, object], *, recover_volume: bool = False
) -> dict[str, object]:
    return _prune(
        repo,
        kind="slot",
        slot="01",
        dispose=True,
        recover_volume=recover_volume,
        plan_id=str(plan["plan_id"]),
        confirm=str(plan["digest"]),
    )


def _old_volume_identity(store: StateStore, root: Path) -> tuple[int, int]:
    worktree = path_identity(root)
    parent = path_identity(root.parent)
    old_device = max(int(worktree["device"]), int(parent["device"])) + 1

    def update(state: dict[str, object]) -> None:
        slot = state["slots"]["01"]
        slot["released_worktree_identity"] = {**worktree, "device": old_device}
        slot["released_managed_root_identity"] = {**parent, "device": old_device}
        slot["released_worktree_resolved"] = str(root.resolve())
        slot["released_managed_root_resolved"] = str(root.parent.resolve())

    store.mutate(update)
    return old_device, int(worktree["device"])


@pytest.mark.parametrize("native", [True, False])
def test_dispose_idle_slot_then_start_and_abandon(git_repo: Path, native: bool) -> None:
    repo, store = _repo(git_repo, native=native)
    first = start(repo, name="first slot user")
    root = Path(first["worktree"])
    abandon(repo, task_id=first["id"], lease=first["lease"], confirm=first["id"])
    (root / "old-cache").write_text("approved disposal\n", encoding="utf-8")
    before = store.read()["slots"]["01"]["generation"]
    plan = _prune(repo, kind="slot", slot="01", dispose=True)
    result = _execute(repo, plan)
    assert result["git_unregistered"] is True
    assert not root.exists()
    assert not any(item.path == root for item in repo.worktrees())
    assert store.read()["slots"]["01"]["generation"] == before
    assert (git_repo / ".solo-ai" / "config.toml").exists()

    second = start(repo, name="rebuild disposed slot")
    assert Path(second["worktree"]) == root
    assert repo.head(root) == repo.head(git_repo)
    abandon(repo, task_id=second["id"], lease=second["lease"], confirm=second["id"])
    assert store.read()["slots"]["01"]["status"] == "idle"


def test_dispose_initial_detached_registered_slot_without_predecessor(
    git_repo: Path,
) -> None:
    repo, store = _repo(git_repo)
    root = Path(store.read()["slots"]["01"]["path"])
    git(git_repo, "worktree", "add", "--detach", str(root), "main")
    plan = _prune(repo, kind="slot", slot="01", dispose=True)
    assert plan["detached"] is True
    _execute(repo, plan)
    task = start(repo, name="recreate old detached slot")
    assert Path(task["worktree"]) == root
    abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])


def _legacy_reclaimed_slot(
    git_repo: Path, *, ahead_branch: bool = False
) -> tuple[GitRepo, StateStore, dict[str, object], Path]:
    repo, store = _repo(git_repo, native=False)
    earlier = start(repo, name="earlier released slot")
    abandon(
        repo,
        task_id=earlier["id"],
        lease=earlier["lease"],
        confirm=earlier["id"],
    )
    task = start(repo, name="retained legacy slot")
    root = Path(task["worktree"])
    if ahead_branch:
        (root / "tracked.txt").write_text("branch only\n", encoding="utf-8")
        git(root, "add", "tracked.txt")
        git(root, "commit", "-m", "test: retain a branch-only commit")
    (root / "discard.txt").write_text("approved\n", encoding="utf-8")
    abandon(
        repo,
        task_id=task["id"],
        lease=task["lease"],
        confirm=task["id"],
        reason="retain before exact reclaim",
        retain_worktree=True,
    )
    reclaim = reclaim_retained_worktree(repo, task_id=task["id"])
    reclaim_retained_worktree(repo, task_id=task["id"], confirm=reclaim["confirmation"])

    def keep_old_pointer(state: dict[str, object]) -> None:
        state["slots"]["01"]["released_candidate_task_id"] = earlier["id"]

    store.mutate(keep_old_pointer)
    return repo, store, task, root


@pytest.mark.parametrize("missing_branch", [False, True])
def test_dispose_legacy_completed_reclaim_with_stale_candidate_pointer(
    git_repo: Path, missing_branch: bool
) -> None:
    repo, store, task, root = _legacy_reclaimed_slot(git_repo)
    branch_ref = f"refs/heads/{task['branch']}"
    branch_tip = repo.ref_head(branch_ref)
    assert branch_tip
    if missing_branch:
        git(git_repo, "update-ref", "-d", branch_ref, branch_tip)
    old_device, current_device = _old_volume_identity(store, root)

    def move_recorded_volume(state: dict[str, object]) -> None:
        abandonment = state["tasks"][task["id"]]["abandonment"]
        abandonment["worktree_identity"]["device"] = old_device
        abandonment["managed_root_identity"]["device"] = old_device

    store.mutate(move_recorded_volume)
    plan = _prune(repo, kind="slot", slot="01", dispose=True, recover_volume=True)
    assert plan["legacy_retained_reclaim"]["task_id"] == task["id"]
    assert plan["legacy_retained_reclaim"]["branch_ref_head"] == (
        None if missing_branch else branch_tip
    )
    assert plan["volume_recovery"]["to_device"] == current_device
    assert plan["detached"] is True
    _execute(repo, plan, recover_volume=True)
    assert not root.exists()
    assert store.read()["slots"]["01"]["generation"] == task["slot_generation"]
    rebuilt = start(repo, name="rebuild reclaimed legacy slot")
    assert Path(rebuilt["worktree"]) == root
    abandon(
        repo,
        task_id=rebuilt["id"],
        lease=rebuilt["lease"],
        confirm=rebuilt["id"],
    )


@pytest.mark.parametrize(
    "drift",
    (
        "reclaim-incomplete",
        "identity",
        "duplicate-generation",
        "branch-after-plan",
        "missing-ref-old-tip",
        "ref-disappears-after-plan",
        "ref-reappears-after-plan",
    ),
)
def test_dispose_legacy_reclaim_requires_exact_completed_proof(
    git_repo: Path, drift: str
) -> None:
    repo, store, task, root = _legacy_reclaimed_slot(
        git_repo, ahead_branch=drift == "missing-ref-old-tip"
    )
    if drift == "branch-after-plan":
        plan = _prune(repo, kind="slot", slot="01", dispose=True)
        git(git_repo, "commit", "--allow-empty", "-m", "test: advance base")
        git(git_repo, "branch", "-f", task["branch"], "main")
        with pytest.raises(SoloAIError, match="reclaim proof changed"):
            _execute(repo, plan)
    elif drift in {
        "missing-ref-old-tip",
        "ref-disappears-after-plan",
        "ref-reappears-after-plan",
    }:
        branch_ref = f"refs/heads/{task['branch']}"
        tip = repo.ref_head(branch_ref)
        assert tip
        if drift == "ref-disappears-after-plan":
            plan = _prune(repo, kind="slot", slot="01", dispose=True)
        git(git_repo, "update-ref", "-d", branch_ref, tip)
        if drift == "missing-ref-old-tip":
            with pytest.raises(SoloAIError, match="reclaim proof changed"):
                _prune(repo, kind="slot", slot="01", dispose=True)
        elif drift == "ref-disappears-after-plan":
            with pytest.raises(SoloAIError, match="changed after review"):
                _execute(repo, plan)
        else:
            plan = _prune(repo, kind="slot", slot="01", dispose=True)
            git(git_repo, "update-ref", branch_ref, tip, "0" * len(tip))
            with pytest.raises(SoloAIError, match="changed after review"):
                _execute(repo, plan)
    else:

        def change_proof(state: dict[str, object]) -> None:
            current = state["tasks"][task["id"]]
            if drift == "reclaim-incomplete":
                current["retained_reclaim"]["phase"] = "detached"
            elif drift == "identity":
                current["abandonment"]["worktree_identity"]["inode"] += 1
            else:
                state["tasks"]["task-duplicate"] = {
                    **current,
                    "id": "task-duplicate",
                }

        store.mutate(change_proof)
        with pytest.raises(SoloAIError, match="reclaim|predecessor"):
            _prune(repo, kind="slot", slot="01", dispose=True)
    assert root.exists()
    assert store.read()["slots"]["01"]["status"] == "idle"


def _migrated_reclaimed_slot(
    git_repo: Path, *, historical_ahead: bool = False
) -> tuple[GitRepo, StateStore, dict[str, object], Path]:
    repo, store = _repo(git_repo, native=False)
    earlier = start(repo, name="earlier legacy user")
    abandon(
        repo,
        task_id=earlier["id"],
        lease=earlier["lease"],
        confirm=earlier["id"],
    )
    historical = "release/old-target"
    historical_ref = f"refs/heads/{historical}"
    if historical_ahead:
        tree = git(git_repo, "rev-parse", "main^{tree}")
        parent = git(git_repo, "rev-parse", "main")
        tip = git(
            git_repo,
            "commit-tree",
            tree,
            "-p",
            parent,
            "-m",
            "test: old target only",
        )
        git(git_repo, "update-ref", historical_ref, tip)
    else:
        git(git_repo, "branch", historical, "main")
    git(git_repo, "checkout", historical)
    task = start(repo, name="retained before migration", base=historical)
    root = Path(task["worktree"])
    abandon(
        repo,
        task_id=task["id"],
        lease=task["lease"],
        confirm=task["id"],
        reason="retain through migration",
        retain_worktree=True,
    )
    git(git_repo, "checkout", "main")
    migration = enable_native_migration(
        repo, base_ref="main", confirm=f"main:{repo.head(git_repo)}"
    )
    assert migration["status"] == "enabled"
    assert migration["migration"]["retained_slot_ids"] == ["01"]
    assert "01" not in migration["migration"]["slot_ids"]
    reclaim = reclaim_retained_worktree(repo, task_id=task["id"])
    reclaim_retained_worktree(repo, task_id=task["id"], confirm=reclaim["confirmation"])

    def keep_old_pointer(state: dict[str, object]) -> None:
        state["slots"]["01"]["released_candidate_task_id"] = earlier["id"]

    store.mutate(keep_old_pointer)
    task_ref = f"refs/heads/{task['branch']}"
    git(git_repo, "update-ref", "-d", task_ref, repo.ref_head(task_ref))
    git(git_repo, "update-ref", "-d", historical_ref, repo.ref_head(historical_ref))
    return repo, store, task, root


@pytest.mark.parametrize("advance_target", [False, True])
def test_dispose_migrated_retained_reclaim_with_removed_historical_base(
    git_repo: Path, advance_target: bool
) -> None:
    repo, store, task, root = _migrated_reclaimed_slot(git_repo)
    plan = _prune(repo, kind="slot", slot="01", dispose=True)
    proof = plan["legacy_retained_reclaim"]
    assert proof["task_id"] == task["id"]
    assert proof["historical_base_ref_head"] is None
    assert proof["effective_base_ref"] == "main"
    assert proof["native_migration"]["retained_slot_ids"] == ["01"]
    assert plan["base_ref"] == "main"
    if advance_target:
        git(git_repo, "commit", "--allow-empty", "-m", "test: advance target")
    _execute(repo, plan)
    assert not root.exists()
    rebuilt = start(repo, name="rebuild migrated reclaimed slot")
    assert Path(rebuilt["worktree"]) == root
    abandon(
        repo,
        task_id=rebuilt["id"],
        lease=rebuilt["lease"],
        confirm=rebuilt["id"],
    )
    assert store.read()["slots"]["01"]["status"] == "idle"


@pytest.mark.parametrize(
    "drift",
    (
        "no-migration",
        "not-retained",
        "native-task",
        "migration-after-plan",
        "historical-ref-appears",
        "historical-ref-diverges",
        "target-disappears",
        "target-diverges",
        "head-not-in-target",
    ),
)
def test_dispose_migrated_retained_reclaim_requires_exact_target_and_provenance(
    git_repo: Path, drift: str
) -> None:
    repo, store, task, root = _migrated_reclaimed_slot(
        git_repo, historical_ahead=drift == "head-not-in-target"
    )
    if drift in {"no-migration", "not-retained", "native-task"}:

        def change_provenance(state: dict[str, object]) -> None:
            if drift == "no-migration":
                state["native_migration"] = None
            elif drift == "not-retained":
                state["native_migration"]["retained_slot_ids"] = []
            else:
                state["tasks"][task["id"]]["native_delivery"] = {}

        store.mutate(change_provenance)
        with pytest.raises(SoloAIError, match="migration"):
            _prune(repo, kind="slot", slot="01", dispose=True)
    elif drift == "head-not-in-target":
        with pytest.raises(SoloAIError, match="recorded base"):
            _prune(repo, kind="slot", slot="01", dispose=True)
    elif drift == "migration-after-plan":
        plan = _prune(repo, kind="slot", slot="01", dispose=True)

        def change_migration(state: dict[str, object]) -> None:
            state["native_migration"]["enabled_at"] = "2099-01-01T00:00:00Z"

        store.mutate(change_migration)
        with pytest.raises(SoloAIError, match="changed after review"):
            _execute(repo, plan)
    else:
        old_ref = f"refs/heads/{task['base_ref']}"
        if drift == "historical-ref-diverges":
            git(git_repo, "branch", task["base_ref"], "main")
        plan = _prune(repo, kind="slot", slot="01", dispose=True)
        if drift == "historical-ref-appears":
            git(git_repo, "branch", task["base_ref"], "main")
        elif drift == "target-disappears":
            git(git_repo, "update-ref", "-d", "refs/heads/main")
        else:
            tree = git(git_repo, "rev-parse", "main^{tree}")
            unrelated = git(
                git_repo, "commit-tree", tree, "-m", "test: unrelated target"
            )
            changed_ref = (
                old_ref if drift == "historical-ref-diverges" else "refs/heads/main"
            )
            git(git_repo, "update-ref", changed_ref, unrelated)
        with pytest.raises(SoloAIError, match="base|migration|changed"):
            _execute(repo, plan)
    assert root.exists()
    assert store.read()["slots"]["01"]["status"] == "idle"


@pytest.mark.skipif(os.name != "nt", reason="Windows 目录句柄复用")
def test_dispose_windows_preserves_late_content_with_shared_parent_pin(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, store = _repo(git_repo)
    root = Path(store.read()["slots"]["01"]["path"])
    git(git_repo, "worktree", "add", "--detach", str(root), "main")
    nested = root / "nested"
    nested.mkdir()
    for index in range(20):
        (nested / f"approved-{index:02d}.txt").write_text("remove\n", encoding="utf-8")
    plan = _prune(repo, kind="slot", slot="01", dispose=True)
    stage = root.parent / f".{root.name}.dww-dispose-{plan['plan_id']}"
    original_remaining = slot_disposal._remaining
    original_pin = slot_disposal.pinned_plain_directory
    pinned: list[Path] = []

    def add_late_content(
        path: Path, expected: dict[str, dict[str, object]]
    ) -> dict[str, dict[str, object]]:
        observed = original_remaining(path, expected)
        (stage / "nested" / "late.txt").write_text("preserve\n", encoding="utf-8")
        return observed

    def record_pin(path: Path, snapshot: dict[str, object]) -> object:
        pinned.append(path)
        return original_pin(path, snapshot)

    monkeypatch.setattr(slot_disposal, "_remaining", add_late_content)
    monkeypatch.setattr(slot_disposal, "pinned_plain_directory", record_pin)
    with pytest.raises(SoloAIError):
        _execute(repo, plan)
    assert (stage / "nested" / "late.txt").read_text(encoding="utf-8") == "preserve\n"
    assert pinned.count(stage / "nested") == 1
    assert store.read()["slots"]["01"]["status"] == "disposing"


@pytest.mark.parametrize("native", [True, False])
def test_dispose_explicit_volume_only_recovery_then_rebuild(
    git_repo: Path, native: bool
) -> None:
    repo, store = _repo(git_repo, native=native)
    root = Path(store.read()["slots"]["01"]["path"])
    git(git_repo, "worktree", "add", "--detach", str(root), "main")
    old_device, current_device = _old_volume_identity(store, root)

    with pytest.raises(SoloAIError) as caught:
        _prune(repo, kind="slot", slot="01", dispose=True)
    assert str(old_device) in str(caught.value)
    assert str(current_device) in str(caught.value)

    plan = _prune(repo, kind="slot", slot="01", dispose=True, recover_volume=True)
    recovery = plan["volume_recovery"]
    assert recovery["from_device"] == old_device
    assert recovery["to_device"] == current_device
    with pytest.raises(SoloAIError, match="recovery mode changed"):
        _execute(repo, plan)
    _execute(repo, plan, recover_volume=True)
    slot = store.read()["slots"]["01"]
    assert slot["released_worktree_identity"] is None
    assert slot["released_managed_root_identity"] is None
    assert root.parent.exists()
    assert not root.exists()

    task = start(repo, name="rebuild after volume migration")
    assert Path(task["worktree"]) == root
    abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])


def test_dispose_volume_only_recovery_rejects_inode_change(git_repo: Path) -> None:
    repo, store = _repo(git_repo)
    root = Path(store.read()["slots"]["01"]["path"])
    git(git_repo, "worktree", "add", "--detach", str(root), "main")
    _old_volume_identity(store, root)
    recorded = store.read()["slots"]["01"]["released_worktree_identity"]

    def change_inode(state: dict[str, object]) -> None:
        state["slots"]["01"]["released_worktree_identity"]["inode"] = (
            int(recorded["inode"]) + 1
        )

    store.mutate(change_inode)
    with pytest.raises(SoloAIError) as caught:
        _prune(repo, kind="slot", slot="01", dispose=True, recover_volume=True)
    assert str(int(recorded["inode"]) + 1) in str(caught.value)
    assert str(path_identity(root)["inode"]) in str(caught.value)
    assert root.exists()


def test_dispose_volume_recovery_rechecks_released_identity_at_claim(
    git_repo: Path,
) -> None:
    repo, store = _repo(git_repo)
    root = Path(store.read()["slots"]["01"]["path"])
    git(git_repo, "worktree", "add", "--detach", str(root), "main")
    _old_volume_identity(store, root)
    plan = _prune(repo, kind="slot", slot="01", dispose=True, recover_volume=True)

    def change_identity(state: dict[str, object]) -> None:
        state["slots"]["01"]["released_managed_root_identity"]["device"] += 1

    store.mutate(change_identity)
    with pytest.raises(SoloAIError, match="replaced|transition|released identity"):
        _execute(repo, plan, recover_volume=True)
    assert store.read()["slots"]["01"]["status"] == "idle"
    assert root.exists()


def test_dispose_rejects_active_slot_and_new_generation(git_repo: Path) -> None:
    repo, store = _repo(git_repo)
    first = start(repo, name="active task")
    with pytest.raises(SoloAIError, match="exact idle generation"):
        _prune(repo, kind="slot", slot="01", dispose=True)
    abandon(repo, task_id=first["id"], lease=first["lease"], confirm=first["id"])
    plan = _prune(repo, kind="slot", slot="01", dispose=True)
    second = start(repo, name="new generation")
    with pytest.raises(SoloAIError, match="exact idle generation"):
        _execute(repo, plan)
    assert Path(second["worktree"]).exists()
    assert store.read()["slots"]["01"]["generation"] > plan["slot_generation"]


def test_dispose_rejects_path_traversal_and_forged_worktree(
    git_repo: Path,
) -> None:
    repo, store = _repo(git_repo)
    root = Path(store.read()["slots"]["01"]["path"])
    git(git_repo, "worktree", "add", "--detach", str(root), "main")
    plan = _prune(repo, kind="slot", slot="01", dispose=True)
    with pytest.raises(SoloAIError, match="exact filename"):
        _prune(
            repo,
            kind="slot",
            slot="01",
            dispose=True,
            plan_id="../other.json",
            confirm=str(plan["digest"]),
        )

    plan_path = repo.local_dir / "cleanup-plans" / f"{plan['plan_id']}.json"
    saved = read_json(plan_path, {})
    payload = dict(saved["payload"])
    payload["worktree"] = str(git_repo)
    saved["payload"] = payload
    saved["digest"] = sha256_text(stable_json(payload))
    saved["status"] = "claiming"
    atomic_write_json(plan_path, saved)
    with pytest.raises(SoloAIError, match="generation or owner changed"):
        _prune(
            repo,
            kind="slot",
            slot="01",
            dispose=True,
            plan_id=str(plan["plan_id"]),
            confirm=str(saved["digest"]),
        )
    assert root.exists()
    assert store.read()["slots"]["01"]["status"] == "idle"


def test_dispose_rejects_changed_content_and_replaced_directory(
    git_repo: Path,
) -> None:
    repo, store = _repo(git_repo)
    root = Path(store.read()["slots"]["01"]["path"])
    git(git_repo, "worktree", "add", "--detach", str(root), "main")
    plan = _prune(repo, kind="slot", slot="01", dispose=True)
    (root / "late.txt").write_text("late\n", encoding="utf-8")
    with pytest.raises(SoloAIError, match="changed after review"):
        _execute(repo, plan)
    assert (root / "late.txt").exists()
    (root / "late.txt").unlink()
    other = root.with_name("saved-original")
    root.rename(other)
    root.mkdir()
    with pytest.raises(SoloAIError, match="identity|registration|worktree"):
        _execute(repo, plan)
    assert other.exists()
    assert root.exists()


def test_dispose_cancels_pre_move_claim_after_content_drift(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, store = _repo(git_repo)
    root = Path(store.read()["slots"]["01"]["path"])
    git(git_repo, "worktree", "add", "--detach", str(root), "main")
    plan = _prune(repo, kind="slot", slot="01", dispose=True)
    original = slot_disposal._claim

    def add_content_after_claim(
        current_repo: GitRepo, payload: dict[str, object], plan_id: str
    ) -> None:
        original(current_repo, payload, plan_id)
        (root / "late-content.txt").write_text("keep\n", encoding="utf-8")

    monkeypatch.setattr(slot_disposal, "_claim", add_content_after_claim)
    with pytest.raises(SoloAIError, match="plan cancelled"):
        _execute(repo, plan)
    monkeypatch.setattr(slot_disposal, "_claim", original)
    assert store.read()["slots"]["01"]["status"] == "idle"
    assert (root / "late-content.txt").read_text(encoding="utf-8") == "keep\n"
    with pytest.raises(SoloAIError, match="was cancelled"):
        _execute(repo, plan)
    next_plan = _prune(repo, kind="slot", slot="01", dispose=True)
    assert "late-content.txt" in next_plan["entries"]


def test_dispose_resumes_partial_delete_and_blocks_start(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, store = _repo(git_repo)
    root = Path(store.read()["slots"]["01"]["path"])
    git(git_repo, "worktree", "add", "--detach", str(root), "main")
    (root / "approved.txt").write_text("remove\n", encoding="utf-8")
    plan = _prune(repo, kind="slot", slot="01", dispose=True)
    original = slot_disposal.delete_plain_path_if_unchanged
    fired = False

    def interrupt(
        path: Path, snapshot: dict[str, object], *, posix_disposition: bool = False
    ) -> None:
        nonlocal fired
        original(path, snapshot, posix_disposition=posix_disposition)
        if not fired:
            fired = True
            raise RuntimeError("simulated interruption")

    monkeypatch.setattr(slot_disposal, "delete_plain_path_if_unchanged", interrupt)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        _execute(repo, plan)
    assert store.read()["slots"]["01"]["status"] == "disposing"
    with pytest.raises(SoloAIError, match="slots are busy|no task was queued"):
        start(repo, name="must wait for cleanup")
    monkeypatch.setattr(slot_disposal, "delete_plain_path_if_unchanged", original)
    _execute(repo, plan)
    assert not root.exists()
    assert store.read()["slots"]["01"]["status"] == "idle"


@pytest.mark.parametrize("advance", ["forward", "diverge"])
def test_dispose_recovery_requires_forward_only_base(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, advance: str
) -> None:
    repo, store = _repo(git_repo)
    root = Path(store.read()["slots"]["01"]["path"])
    git(git_repo, "worktree", "add", "--detach", str(root), "main")
    (root / "approved.txt").write_text("remove\n", encoding="utf-8")
    plan = _prune(repo, kind="slot", slot="01", dispose=True)
    original = slot_disposal.delete_plain_path_if_unchanged
    interrupted = False

    def interrupt(
        path: Path, snapshot: dict[str, object], *, posix_disposition: bool = False
    ) -> None:
        nonlocal interrupted
        original(path, snapshot, posix_disposition=posix_disposition)
        if not interrupted:
            interrupted = True
            raise RuntimeError("simulated interruption")

    monkeypatch.setattr(slot_disposal, "delete_plain_path_if_unchanged", interrupt)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        _execute(repo, plan)
    monkeypatch.setattr(slot_disposal, "delete_plain_path_if_unchanged", original)

    if advance == "forward":
        (git_repo / "later.txt").write_text("accepted\n", encoding="utf-8")
        git(git_repo, "add", "later.txt")
        git(git_repo, "commit", "-m", "test: advance base")
        result = _execute(repo, plan)
        assert result["status"] == "disposed"
        assert store.read()["slots"]["01"]["status"] == "idle"
    else:
        tree = git(git_repo, "rev-parse", "HEAD^{tree}").strip()
        unrelated = git(git_repo, "commit-tree", tree, "-m", "unrelated").strip()
        git(git_repo, "update-ref", "refs/heads/main", unrelated)
        with pytest.raises(SoloAIError, match="base changed"):
            _execute(repo, plan)
        assert store.read()["slots"]["01"]["status"] == "disposing"


def test_dispose_refuses_replaced_parent_link_before_delete(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    directory_link: object,
) -> None:
    repo, store = _repo(git_repo)
    root = Path(store.read()["slots"]["01"]["path"])
    git(git_repo, "worktree", "add", "--detach", str(root), "main")
    cache = root / "cache"
    cache.mkdir()
    (cache / "payload.txt").write_text("preserve\n", encoding="utf-8")
    plan = _prune(repo, kind="slot", slot="01", dispose=True)
    original = slot_disposal._remaining
    replaced = False
    backup = root.parent / "saved-cache"

    def replace_after_inventory(
        stage: Path, expected: dict[str, dict[str, object]]
    ) -> dict[str, dict[str, object]]:
        nonlocal replaced
        observed = original(stage, expected)
        if not replaced:
            replaced = True
            (stage / "cache").rename(backup)
            directory_link(stage / "cache", backup)
        return observed

    monkeypatch.setattr(slot_disposal, "_remaining", replace_after_inventory)
    with pytest.raises(SoloAIError, match="directory changed|link|busy"):
        _execute(repo, plan)
    assert (backup / "payload.txt").read_text(encoding="utf-8") == "preserve\n"
    assert store.read()["slots"]["01"]["status"] == "disposing"


def test_dispose_resumes_completed_receipt_before_releasing_slot(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, store = _repo(git_repo)
    root = Path(store.read()["slots"]["01"]["path"])
    git(git_repo, "worktree", "add", "--detach", str(root), "main")
    plan = _prune(repo, kind="slot", slot="01", dispose=True)
    original = slot_disposal._release

    def fail_once(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated release interruption")

    monkeypatch.setattr(slot_disposal, "_release", fail_once)
    with pytest.raises(RuntimeError, match="simulated release interruption"):
        _execute(repo, plan)
    assert store.read()["slots"]["01"]["status"] == "disposing"
    monkeypatch.setattr(slot_disposal, "_release", original)
    _execute(repo, plan)
    assert store.read()["slots"]["01"]["status"] == "idle"
    assert not root.exists()
    with pytest.raises(SoloAIError, match="already completed"):
        _execute(repo, plan)
