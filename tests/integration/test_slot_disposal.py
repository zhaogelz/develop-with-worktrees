from __future__ import annotations

from pathlib import Path

import pytest

from conftest import git
from solo_ai import slot_disposal
from solo_ai.cli import _prune
from solo_ai.config import CommandSpec, load_repo_config
from solo_ai.lifecycle import abandon, initialize, start
from solo_ai.repo import GitRepo
from solo_ai.state import LEGACY_STATE_SCHEMA, STATE_SCHEMA, StateStore
from solo_ai.util import SoloAIError

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


def _execute(repo: GitRepo, plan: dict[str, object]) -> dict[str, object]:
    return _prune(
        repo,
        kind="slot",
        slot="01",
        dispose=True,
        plan_id=str(plan["plan_id"]),
        confirm=str(plan["digest"]),
    )


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

    def interrupt(path: Path, snapshot: dict[str, object]) -> None:
        nonlocal fired
        original(path, snapshot)
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
