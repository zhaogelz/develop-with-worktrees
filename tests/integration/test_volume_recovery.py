from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import git
from solo_ai import cleanup, volume_recovery
from solo_ai.config import CommandSpec, load_repo_config
from solo_ai.lifecycle import abandon, commit_task, finish, initialize, ready, start
from solo_ai.repo import GitRepo
from solo_ai.state import STATE_SCHEMA, StateStore
from solo_ai.util import SoloAIError, path_identity


@pytest.fixture
def cloned_repo(git_repo: Path, monkeypatch: pytest.MonkeyPatch):
    repo = GitRepo(git_repo)
    (repo.common_dir / "info" / "exclude").write_text(".tmp/\n", encoding="utf-8")
    initialize(
        repo,
        slots=1,
        commands=[CommandSpec(("git", "diff", "--check", "main...HEAD"))],
        accept=True,
        accept_static_only=False,
    )
    store = StateStore(repo)
    store.mutate(lambda state: state.update(schema_version=STATE_SCHEMA))
    store.ensure_slots(load_repo_config(repo))
    task = start(repo, name="volume recovery fixture")
    abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])

    # 仅模拟卷编号改变；真实 Git、目录编号和状态来自正常领取及归还。
    def migrated_identity(path):
        result = path_identity(path)
        return {**result, "device": result["device"] + 1}

    monkeypatch.setattr(volume_recovery, "path_identity", migrated_identity)
    monkeypatch.setattr(volume_recovery, "_queue_snapshot", lambda: {})
    return repo, store, task


def test_recovery_preserves_history_git_and_ignored_files(cloned_repo):
    repo, store, task = cloned_repo
    worktree = Path(task["worktree"])
    ignored = worktree / ".tmp" / "keep.txt"
    ignored.parent.mkdir(exist_ok=True)
    ignored.write_text("preserve", encoding="utf-8")
    before = store.read()
    head = repo.head(worktree)
    preview = volume_recovery.recover_volume(repo)
    assert store.read() == before
    result = volume_recovery.recover_volume(
        repo, confirm=preview["confirmation"], confirm_no_live_work=True
    )
    assert result["changed"]
    assert json.loads(Path(result["backup"]).read_text(encoding="utf-8")) == before
    after = store.read()
    assert after["tasks"][task["id"]]["status"] == before["tasks"][task["id"]]["status"]
    assert after["tasks"][task["id"]]["lease"] == before["tasks"][task["id"]]["lease"]
    assert after["batches"] == before["batches"]
    assert repo.head(worktree) == head
    assert ignored.read_text(encoding="utf-8") == "preserve"
    assert volume_recovery.recover_volume(repo)["confirmation"] is None
    assert volume_recovery.recover_volume(
        repo, confirm=preview["confirmation"], confirm_no_live_work=True
    )["already_applied"]


@pytest.mark.parametrize("change", ["inode", "mode", "volume", "path", "dirty"])
def test_recovery_rejects_ambiguous_scene(cloned_repo, monkeypatch, change):
    repo, store, task = cloned_repo
    before = store.read()
    worktree = Path(task["worktree"])
    if change in {"inode", "mode", "volume"}:
        migrated = volume_recovery.path_identity

        def altered(path):
            result = migrated(path)
            if Path(path) == worktree:
                key = "device" if change == "volume" else change
                result[key] += 1
            return result

        monkeypatch.setattr(volume_recovery, "path_identity", altered)
    elif change == "path":
        store.mutate(
            lambda state: state["slots"][task["slot_id"]].update(
                released_worktree_resolved=str(worktree / "wrong")
            )
        )
        before = store.read()
    else:
        (worktree / "unknown.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(SoloAIError):
        volume_recovery.recover_volume(repo)
    assert store.read() == before
    assert not (repo.local_dir / "volume-recovery").exists()


@pytest.mark.parametrize("change", ["head", "branch", "state"])
def test_confirmation_rejects_git_or_state_change_after_preview(cloned_repo, change):
    repo, store, task = cloned_repo
    preview = volume_recovery.recover_volume(repo)
    worktree = Path(task["worktree"])
    if change == "head":
        git(worktree, "commit", "--allow-empty", "-m", "test: changed idle head")
    elif change == "branch":
        git(worktree, "switch", "-c", "codex/changed-idle-branch")
    else:
        store.mutate(
            lambda state: state["slots"][task["slot_id"]].update(generation=99)
        )
    before = store.read()
    with pytest.raises(SoloAIError, match="preview changed"):
        volume_recovery.recover_volume(
            repo, confirm=preview["confirmation"], confirm_no_live_work=True
        )
    assert store.read() == before


@pytest.mark.parametrize("phase", ["state", "receipt"])
def test_interrupted_write_can_resume_without_replacing_backup(
    cloned_repo, monkeypatch, phase
):
    repo, store, _ = cloned_repo
    before = store.read()
    preview = volume_recovery.recover_volume(repo)
    writer = volume_recovery.atomic_write_json

    def interrupt(path, value):
        if (phase == "state" and path == store.path) or (
            phase == "receipt"
            and str(path).endswith(".receipt.json")
            and value.get("status") == "completed"
        ):
            raise OSError("simulated interruption")
        writer(path, value)

    monkeypatch.setattr(volume_recovery, "atomic_write_json", interrupt)
    with pytest.raises(OSError, match="simulated interruption"):
        volume_recovery.recover_volume(
            repo, confirm=preview["confirmation"], confirm_no_live_work=True
        )
    monkeypatch.setattr(volume_recovery, "atomic_write_json", writer)
    result = volume_recovery.recover_volume(
        repo, confirm=preview["confirmation"], confirm_no_live_work=True
    )
    assert result["status"] == "completed"
    assert json.loads(Path(result["backup"]).read_text(encoding="utf-8")) == before
    assert volume_recovery.recover_volume(repo)["confirmation"] is None


def test_running_validation_and_missing_confirmation_are_rejected(
    cloned_repo, monkeypatch
):
    repo, store, _ = cloned_repo
    before = store.read()
    preview = volume_recovery.recover_volume(repo)
    with pytest.raises(SoloAIError, match="stopped host"):
        volume_recovery.recover_volume(repo, confirm=preview["confirmation"])
    monkeypatch.setattr(
        volume_recovery, "_queue_snapshot", lambda: {"active": [{"pid": 123}]}
    )
    with pytest.raises(SoloAIError, match="Stop live validation"):
        volume_recovery.recover_volume(
            repo, confirm=preview["confirmation"], confirm_no_live_work=True
        )
    assert store.read() == before


@pytest.mark.parametrize("waiting", [False, True])
def test_frozen_task_keeps_exact_commit_and_owner(cloned_repo, waiting):
    repo, store, _ = cloned_repo
    task = start(repo, name="frozen source")
    worktree = Path(task["worktree"])
    (worktree / "feature.txt").write_text("source", encoding="utf-8")
    commit_task(
        repo,
        task_id=task["id"],
        lease=task["lease"],
        message="test: frozen source",
        paths=["feature.txt"],
    )
    ready(repo, task_id=task["id"], lease=task["lease"])
    if waiting:
        finish(repo, task_id=task["id"], lease=task["lease"])
    before = store.task(task["id"])
    preview = volume_recovery.recover_volume(repo)
    volume_recovery.recover_volume(
        repo, confirm=preview["confirmation"], confirm_no_live_work=True
    )
    after = store.task(task["id"])
    for key in ("status", "lease", "host_origin", "candidate_head", "branch"):
        assert after.get(key) == before.get(key)
    assert repo.head(worktree) == before["candidate_head"]


def test_clone_blocked_start_can_rebind_without_activation(cloned_repo, monkeypatch):
    repo, store, _ = cloned_repo
    # 模拟迁盘后先领取再被目录核验拒绝的真实 Start 失败顺序。
    monkeypatch.setattr(cleanup, "path_identity", volume_recovery.path_identity)
    with pytest.raises(SoloAIError, match="root directory object was replaced"):
        start(repo, name="blocked by clone")
    state = store.read()
    task_id = state["slots"]["01"]["task_id"]
    assert state["tasks"][task_id]["status"] == "quarantined"
    preview = volume_recovery.recover_volume(repo)
    volume_recovery.recover_volume(
        repo, confirm=preview["confirmation"], confirm_no_live_work=True
    )
    assert store.task(task_id)["status"] == "quarantined"
    assert repo.branch(Path(store.task(task_id)["worktree"])) is None


def test_idle_integration_workspace_rebinds_and_owned_one_blocks(cloned_repo):
    repo, store, task = cloned_repo
    root = Path(task["worktree"]).parent
    worktree = root / "solo-ai-integration"
    head = repo.head()
    head_ref = "refs/dww/batch-heads/volume-test"
    git(repo.primary_path, "worktree", "add", "--detach", str(worktree), head)
    git(repo.primary_path, "update-ref", head_ref, head)
    record = {
        "owner": "unfinished",
        "registering": False,
        "worktree": str(worktree),
        "head": head,
        "head_ref": head_ref,
        "worktree_resolved": str(worktree.resolve()),
        "worktree_identity": path_identity(worktree),
        "managed_root_resolved": str(root.resolve()),
        "managed_root_identity": path_identity(root),
    }
    store.mutate(lambda state: state.update(integration_workspace=record))
    with pytest.raises(SoloAIError, match="Owned integration"):
        volume_recovery.recover_volume(repo)
    store.mutate(lambda state: state["integration_workspace"].update(owner=None))
    preview = volume_recovery.recover_volume(repo)
    volume_recovery.recover_volume(
        repo, confirm=preview["confirmation"], confirm_no_live_work=True
    )
    assert store.read()["integration_workspace"][
        "worktree_identity"
    ] == volume_recovery.path_identity(worktree)
    assert repo.head(worktree) == head


def test_queue_preview_does_not_write_and_detects_waiting_ticket(tmp_path, monkeypatch):
    state = tmp_path / "queue.json"
    state.write_text('{"schema_version": 1, "active": {}}', encoding="utf-8")
    monkeypatch.setattr(volume_recovery, "_queue_state_path", lambda: state)
    monkeypatch.setattr(volume_recovery, "_ticket_root", lambda: tmp_path / "tickets")
    before = state.read_bytes()
    assert not volume_recovery._queue_snapshot()["active"]
    assert state.read_bytes() == before
    (tmp_path / "tickets").mkdir()
    (tmp_path / "tickets" / "waiting.json").write_text("{}", encoding="utf-8")
    assert volume_recovery._queue_snapshot()["waiting"]
    assert state.read_bytes() == before


def test_cli_preview_and_apply_round_trip(cloned_repo):
    from solo_ai.cli import _dispatch, _parser

    repo, _, _ = cloned_repo
    args = ["--repo", str(repo.primary_path), "migration", "recover-volume"]
    preview = _dispatch(_parser().parse_args(args))
    applied = _dispatch(
        _parser().parse_args(
            args + ["--confirm", preview["confirmation"], "--confirm-no-live-work"]
        )
    )
    assert applied["changed"]
