from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest
from conftest import git

from solo_ai import candidate_batches as batches
from solo_ai.config import load_repo_config, load_verification_config
from solo_ai.lifecycle import approve, initialize
from solo_ai.repo import GitRepo
from solo_ai.state import StateStore
from solo_ai.util import SoloAIError, path_identity
from test_candidate_batches import (
    initialized_batched,
    install_runtime_adapter,
    publish,
    VERIFY,
)


def reusable_repo(root: Path):
    (root / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    git(root, "add", ".gitignore")
    git(root, "commit", "-m", "test: ignored reusable dependencies")
    return initialized_batched(root, auto_full=False, reusable=True)


def test_reusable_batches_resolve_one_workspace(git_repo: Path) -> None:
    """连续批次的实际目录入口不能继续按批次ID膨胀。"""
    repo = initialized_batched(git_repo)
    first = batches._integration_worktree(
        repo,
        {"id": "batch-" + "1" * 24, "worktree_mode": "reusable"},
    )
    second = batches._integration_worktree(
        repo,
        {"id": "batch-" + "2" * 24, "worktree_mode": "reusable"},
    )
    assert first == second
    assert first == git_repo / ".worktrees" / "solo-ai-integration"


def test_legacy_batches_keep_their_frozen_dedicated_location(git_repo: Path) -> None:
    """旧批次没有新绑定，不能因安装更新便改写其目录归属。"""
    repo = initialized_batched(git_repo)
    batch_id = "batch-" + "3" * 24
    assert batches._integration_worktree(repo, {"id": batch_id}) == (
        git_repo / ".worktrees" / f"solo-ai-batch-{batch_id}"
    )
    assert load_repo_config(repo).integration.worktree_mode == "dedicated"


def test_workspace_mode_changes_the_candidate_lane(git_repo: Path) -> None:
    """旧候选不混入启用复用位置后的新策略通道。"""
    from dataclasses import replace

    repo = initialized_batched(git_repo)
    config = load_repo_config(repo)
    previous = StateStore.integration_policy(config)
    updated = replace(
        config, integration=replace(config.integration, worktree_mode="reusable")
    )
    current = StateStore.integration_policy(updated)
    assert previous["worktree_mode"] == "dedicated"
    assert current["worktree_mode"] == "reusable"
    assert previous["activation_epoch"] != current["activation_epoch"]


def test_success_returns_workspace_without_deleting_dependencies(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """走实际封批/Full/推进路径，完成不是递归删除依赖。"""
    repo = reusable_repo(git_repo)
    candidate = publish(repo, name="return workspace", relative="delivered.txt")
    promote = batches._promote
    dependency = (
        git_repo
        / ".worktrees"
        / "solo-ai-integration"
        / "node_modules"
        / "package"
        / "index.js"
    )

    def add_dependency(repo, store, batch):
        dependency.parent.mkdir(parents=True)
        dependency.write_text("generated dependency\n", encoding="utf-8")
        return promote(repo, store, batch)

    monkeypatch.setattr(batches, "_promote", add_dependency)
    completed = batches.seal_batch(repo, candidate_ids=[candidate["candidate_id"]])

    assert completed["status"] == "completed"
    assert dependency.read_text(encoding="utf-8") == "generated dependency\n"
    assert completed["worktree_released_at"]
    workspace = batches.CandidateBatchStore(repo).read()["integration_workspace"]
    assert workspace["owner"] is None
    assert workspace["generation"] == completed["worktree_generation"]
    assert repo.ref_head(completed["integration_ref"]) == completed["integration_head"]
    saved_candidate = batches.CandidateBatchStore(repo).candidate(
        candidate["candidate_id"]
    )
    assert repo.ref_head(saved_candidate["ref"]) == saved_candidate["head"]


def test_candidate_conflict_preserves_partial_result_and_unblocks_next_batch(
    git_repo: Path,
) -> None:
    """同批第二候选冲突后，保留首候选组合提交并让下一独立批次正常使用位置。"""
    repo = reusable_repo(git_repo)
    first = publish(repo, name="first change", relative="shared.txt")
    second = publish(repo, name="second change", relative="shared.txt")
    base_head = repo.head(git_repo)
    with pytest.raises(SoloAIError, match="conflicts with the sealed batch"):
        batches.seal_batch(
            repo, candidate_ids=[first["candidate_id"], second["candidate_id"]]
        )
    store = batches.CandidateBatchStore(repo)
    failed = next(iter(store.read()["batches"].values()))
    workspace = Path(failed["worktree"])
    assert failed["applied_candidate_ids"] == [first["candidate_id"]]
    assert failed["failed_candidate_id"] == second["candidate_id"]
    assert failed["worktree_released_at"]
    assert store.read()["integration_workspace"]["owner"] is None
    assert repo.is_clean(workspace)
    assert repo.head(git_repo) == base_head
    assert (workspace / "shared.txt").read_text() == "first change\n"
    partial_ref = failed["integration_ref"]
    assert repo.ref_head(partial_ref) == failed["integration_head"]
    for item in (first, second):
        candidate = store.candidate(item["candidate_id"])
        assert candidate["status"] == "retained"
        assert repo.ref_head(candidate["ref"]) == candidate["head"]
    following = publish(repo, name="independent task", relative="following.txt")
    result = batches.seal_batch(repo, candidate_ids=[following["candidate_id"]])
    assert result["status"] == "completed"
    assert result["worktree"] == str(workspace)
    assert result["worktree_generation"] == failed["worktree_generation"] + 1
    assert repo.ref_head(partial_ref) == failed["integration_head"]
    assert not (git_repo / "shared.txt").exists()


def test_add_add_conflict_probe_does_not_dirty_reusable_workspace(
    git_repo: Path,
) -> None:
    """新增同名文件的三方探测只能污染临时位置，不能锁死共享工作树。"""
    repo = reusable_repo(git_repo)
    candidate = publish(repo, name="candidate add", relative="same.txt")
    (git_repo / "same.txt").write_text("main add\n", encoding="utf-8")
    git(git_repo, "add", "same.txt")
    git(git_repo, "commit", "-m", "test: add competing main file")

    with pytest.raises(SoloAIError, match="conflicts with the sealed batch"):
        batches.seal_batch(repo, candidate_ids=[candidate["candidate_id"]])

    failed = next(iter(batches.CandidateBatchStore(repo).read()["batches"].values()))
    workspace = Path(failed["worktree"])
    assert failed["failed_candidate_id"] == candidate["candidate_id"]
    assert failed["worktree_released_at"]
    assert (
        batches.CandidateBatchStore(repo).read()["integration_workspace"]["owner"]
        is None
    )
    assert repo.is_clean(workspace)
    assert repo.head(workspace) == failed["integration_head"]
    assert (workspace / "same.txt").read_text(encoding="utf-8") == "main add\n"


def test_new_repository_defaults_to_reusable_batches(git_repo: Path) -> None:
    repo = GitRepo(git_repo)
    initialize(repo, slots=3, commands=[VERIFY], accept=True, accept_static_only=False)
    assert load_repo_config(repo).integration.worktree_mode == "reusable"


@pytest.mark.parametrize("interrupted_after_apply", [False, True])
def test_isolated_index_interruption_leaves_workspace_recoverable(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, interrupted_after_apply: bool
) -> None:
    """预检前后中断都不留下真实索引改动，恢复仍使用原工作区代次。"""
    repo = reusable_repo(git_repo)
    candidate = publish(repo, name="index interruption", relative="candidate.txt")
    run = batches.subprocess.run

    def interrupted(args, **kwargs):
        if "apply" in args and "--cached" in args:
            if interrupted_after_apply:
                run(args, **kwargs)
            raise KeyboardInterrupt("isolated index interrupted")
        return run(args, **kwargs)

    monkeypatch.setattr(batches.subprocess, "run", interrupted)
    with pytest.raises(KeyboardInterrupt, match="isolated index interrupted"):
        batches.seal_batch(repo, candidate_ids=[candidate["candidate_id"]])
    store = batches.CandidateBatchStore(repo)
    record = store.read()["integration_workspace"]
    batch = store.batch(record["owner"])
    assert batch["status"] == "composing"
    assert repo.is_clean(Path(batch["worktree"]))
    assert repo.head(Path(batch["worktree"])) == batch["integration_head"]
    assert not list(repo.local_dir.glob("candidate-index-*"))
    monkeypatch.setattr(batches.subprocess, "run", run)
    recovered = batches.recover_batch(repo, batch_id=batch["id"])
    assert recovered["status"] == "completed"
    assert recovered["worktree_generation"] == record["generation"]
    assert store.read()["integration_workspace"]["owner"] is None


def test_ten_success_and_failure_rounds_reuse_one_workspace(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真实Full命令交替成功/失败，十轮不重建场地、不扫描依赖内容。"""
    from solo_ai import util
    from solo_ai import batch_workspace

    repo = reusable_repo(git_repo)
    counter = repo.local_dir / "test-full-count"
    command = [
        sys.executable,
        "-c",
        "from pathlib import Path; "
        f"p=Path({str(counter)!r}); "
        "n=int(p.read_text())+1 if p.exists() else 1; "
        "p.write_text(str(n)); raise SystemExit(n % 2)",
    ]
    verification = git_repo / ".solo-ai" / "verification.toml"
    verification.write_text(
        verification.read_text(encoding="utf-8")
        + '\n[[profiles]]\nid = "alternating-full"\nlevel = "full"\n'
        + 'paths = ["**"]\ninput_paths = ["**"]\n'
        + 'external_state = "unknown"\ninput_closure = "declared"\n'
        + "commands = ["
        + json.dumps(command)
        + "]\n",
        encoding="utf-8",
    )
    git(git_repo, "add", ".solo-ai/verification.toml")
    git(git_repo, "commit", "-m", "test: actual alternating Full outcome")
    approve(repo, load_verification_config(repo))
    workspace = git_repo / ".worktrees" / "solo-ai-integration"
    dependency = workspace / "node_modules" / "package" / "index.js"
    activate = batches._activate_batch_runtime
    inspect = batch_workspace.inspect_untracked
    sha256_file = util.sha256_file

    def add_retained_dependency(repo, store, batch):
        active = activate(repo, store, batch)
        if not dependency.exists():
            dependency.parent.mkdir(parents=True)
            dependency.write_text("keep this generated dependency\n", encoding="utf-8")
        return active

    def reject_expansion(repo, *, cwd, **kwargs):
        if cwd == workspace:
            assert kwargs.get("expand_dependencies") is False
        return inspect(repo, cwd=cwd, **kwargs)

    def reject_dependency_hash(path):
        assert not Path(path).is_relative_to(workspace / "node_modules")
        return sha256_file(path)

    monkeypatch.setattr(batches, "_activate_batch_runtime", add_retained_dependency)
    monkeypatch.setattr(batch_workspace, "inspect_untracked", reject_expansion)
    monkeypatch.setattr(util, "sha256_file", reject_dependency_hash)
    identity = None
    completed_count = 0
    failed_count = 0
    for index in range(10):
        candidate = publish(repo, name=f"round {index}", relative=f"round-{index}.txt")
        if index % 2 == 0:
            with pytest.raises(SoloAIError, match="Validation failed"):
                batches.seal_batch(repo, candidate_ids=[candidate["candidate_id"]])
            failed_count += 1
        else:
            result = batches.seal_batch(repo, candidate_ids=[candidate["candidate_id"]])
            assert result["status"] == "completed"
            completed_count += 1
        pool = batches.CandidateBatchStore(repo).read()
        record = pool["integration_workspace"]
        assert record["owner"] is None
        assert record["generation"] == index + 1
        identity = identity or path_identity(workspace)
        assert path_identity(workspace) == identity
        assert (
            dependency.read_text(encoding="utf-8") == "keep this generated dependency\n"
        )
        assert [
            p
            for p in workspace.parent.iterdir()
            if p.name.startswith("solo-ai-integration")
        ] == [workspace]
        assert not list(workspace.parent.glob("solo-ai-batch-*"))
        for batch in pool["batches"].values():
            assert batch["worktree_released_at"]
            assert repo.ref_head(batch["integration_ref"]) == batch["integration_head"]
    assert counter.read_text() == "10"
    assert (completed_count, failed_count) == (5, 5)


def test_recover_after_promotion_and_later_main_commit_does_not_repeat_full(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """主线已推进但状态落盘中断，后续提交不应导致重复Full或误报未交付。"""
    repo = reusable_repo(git_repo)
    candidate = publish(repo, name="promotion interruption", relative="delivery.txt")
    update = batches.CandidateBatchStore.update_batch

    def interrupted_update(self, batch_id, **changes):
        if changes.get("status") == "promoted":
            raise KeyboardInterrupt("after actual fast-forward")
        return update(self, batch_id, **changes)

    monkeypatch.setattr(batches.CandidateBatchStore, "update_batch", interrupted_update)
    with pytest.raises(KeyboardInterrupt, match="actual fast-forward"):
        batches.seal_batch(repo, candidate_ids=[candidate["candidate_id"]])
    store = batches.CandidateBatchStore(repo)
    pending = next(iter(store.read()["batches"].values()))
    assert pending["status"] == "validated"
    assert repo.head(git_repo) == pending["integration_head"]
    (git_repo / "later.txt").write_text("later main progress\n", encoding="utf-8")
    git(git_repo, "add", "later.txt")
    git(git_repo, "commit", "-m", "test: later main progress")
    later_head = repo.head(git_repo)
    projection = store.summary()["candidates"][0]
    assert projection["delivered"] is True
    assert projection["finalization_pending"] is True
    monkeypatch.setattr(batches.CandidateBatchStore, "update_batch", update)

    def forbid_full(*args, **kwargs):
        pytest.fail("恢复已经通过的Full不应再次执行验证")

    monkeypatch.setattr(batches, "_validate_batch", forbid_full)
    completed = batches.recover_batch(repo, batch_id=pending["id"])
    assert completed["status"] == "completed"
    assert repo.head(git_repo) == later_head
    assert store.summary()["candidates"][0]["finalization_pending"] is False
    assert store.read()["integration_workspace"]["owner"] is None


def test_old_failed_retirement_does_not_touch_new_owner(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """旧失败批次迟到退役，不可依据同名目录影响新代次。"""
    from solo_ai import batch_workspace

    repo = reusable_repo(git_repo)
    first = publish(repo, name="failed owner", relative="first.txt")

    def failed_full(*args, **kwargs):
        raise SoloAIError("synthetic deterministic failure")

    # 故障注入验证命令层，保留真实的资源释放/失败归还路径。
    validate = batches.validate
    monkeypatch.setattr(batches, "validate", failed_full)
    with pytest.raises(SoloAIError, match="deterministic failure"):
        batches.seal_batch(repo, candidate_ids=[first["candidate_id"]])
    store = batches.CandidateBatchStore(repo)
    old = next(iter(store.read()["batches"].values()))
    assert old["worktree_released_at"]
    monkeypatch.setattr(batches, "validate", validate)
    second = publish(repo, name="new owner", relative="second.txt")

    def interrupted_full(*args, **kwargs):
        raise KeyboardInterrupt("new owner still active")

    monkeypatch.setattr(batches, "_validate_batch", interrupted_full)
    with pytest.raises(KeyboardInterrupt):
        batches.seal_batch(repo, candidate_ids=[second["candidate_id"]])
    before = store.read()["integration_workspace"]
    head = repo.head(Path(before["worktree"]))
    assert before["owner"] != old["id"]
    assert before["generation"] == 2
    batches.retire_failed_batch(repo, batch_id=old["id"])
    assert store.read()["integration_workspace"] == before
    assert repo.head(Path(before["worktree"])) == head
    with pytest.raises(batch_workspace.BatchWorkspacePending, match="no longer owns"):
        batch_workspace.require_owner(repo, store, old)


def test_missing_idle_workspace_repairs_only_its_registration(git_repo: Path) -> None:
    """手动删除已归还的临时场地后，精确移除登记并重建，不使用全局prune。"""
    repo = reusable_repo(git_repo)
    first = publish(repo, name="idle missing", relative="first.txt")
    completed = batches.seal_batch(repo, candidate_ids=[first["candidate_id"]])
    workspace = Path(completed["worktree"])
    assert workspace.is_relative_to(git_repo / ".worktrees")
    shutil.rmtree(workspace)
    assert any(item.path == workspace for item in repo.worktrees())
    second = publish(repo, name="after missing", relative="second.txt")
    # Start可正常分配另一个开发槽；仅比较修复操作前后的无关登记。
    registrations = {item.path for item in repo.worktrees()} - {workspace}
    repaired = batches.seal_batch(repo, candidate_ids=[second["candidate_id"]])
    assert repaired["status"] == "completed"
    assert repaired["worktree_generation"] == 2
    assert {item.path for item in repo.worktrees()} - {workspace} == registrations
    assert repo.ref_head(completed["integration_ref"]) == completed["integration_head"]


@pytest.mark.parametrize("change", ["missing", "replacement", "junction"])
def test_active_workspace_path_change_preserves_owner_and_target(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, directory_link, change: str
) -> None:
    """活动场地缺失、目录替换或junction替换都不能被自动收养或回收。"""
    from solo_ai import batch_workspace

    repo = reusable_repo(git_repo)
    candidate = publish(repo, name=f"path {change}", relative="candidate.txt")
    validate_batch = batches._validate_batch

    def interrupted_full(*args, **kwargs):
        raise KeyboardInterrupt("preserved scene")

    monkeypatch.setattr(batches, "_validate_batch", interrupted_full)
    with pytest.raises(KeyboardInterrupt):
        batches.seal_batch(repo, candidate_ids=[candidate["candidate_id"]])
    store = batches.CandidateBatchStore(repo)
    pending = next(iter(store.read()["batches"].values()))
    workspace = Path(pending["worktree"])
    saved = workspace.with_name("saved-active-scene")
    target = git_repo / "outside-target"
    target.mkdir()
    marker = target / "keep.txt"
    marker.write_text("untouched\n", encoding="utf-8")
    assert workspace.is_relative_to(git_repo / ".worktrees")
    workspace.rename(saved)
    if change == "replacement":
        workspace.mkdir()
    elif change == "junction":
        directory_link(workspace, target)
    before = store.read()["integration_workspace"]
    main_head = repo.ref_head("refs/heads/main")
    monkeypatch.setattr(batches, "_validate_batch", validate_batch)
    with pytest.raises(
        batch_workspace.BatchWorkspacePending, match="changed or disappeared"
    ):
        batches.recover_batch(repo, batch_id=pending["id"])
    assert store.read()["integration_workspace"] == before
    assert marker.read_text(encoding="utf-8") == "untouched\n"
    assert repo.ref_head("refs/heads/main") == main_head
    assert saved.is_dir()


@pytest.mark.parametrize(
    "stage", ["before-registration", "after-registration", "busy-checkout"]
)
def test_workspace_admission_interruption_recovers_exact_generation(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    """登记/检出失败应保留可恢复阶段，而不是把位置困在已失败的旧持有者。"""
    from solo_ai import batch_workspace

    repo = reusable_repo(git_repo)
    if stage == "busy-checkout":
        first = publish(repo, name="prior idle owner", relative="first.txt")
        batches.seal_batch(repo, candidate_ids=[first["candidate_id"]])
        # 让最新main不再等于空闲场地HEAD，实际触发非强制checkout。
        (git_repo / "later.txt").write_text("later\n", encoding="utf-8")
        git(git_repo, "add", "later.txt")
        git(git_repo, "commit", "-m", "test: advance base before checkout")
    candidate = publish(repo, name=f"admission {stage}", relative="candidate.txt")
    command = repo.git

    def interrupted_command(args, **kwargs):
        is_add = args[:2] == ["worktree", "add"] and "solo-ai-integration" in " ".join(
            args
        )
        if is_add and stage == "before-registration":
            raise KeyboardInterrupt(stage)
        if args[:2] == ["checkout", "--detach"] and stage == "busy-checkout":
            raise SoloAIError("checkout target is temporarily occupied")
        result = command(args, **kwargs)
        if is_add and stage == "after-registration":
            raise KeyboardInterrupt(stage)
        return result

    monkeypatch.setattr(repo, "git", interrupted_command)
    expected_error = (
        batch_workspace.BatchWorkspacePending
        if stage == "busy-checkout"
        else KeyboardInterrupt
    )
    with pytest.raises(expected_error):
        batches.seal_batch(repo, candidate_ids=[candidate["candidate_id"]])
    store = batches.CandidateBatchStore(repo)
    record = store.read()["integration_workspace"]
    pending = store.batch(record["owner"])
    assert pending["status"] == "sealed"
    generation = record["generation"]
    monkeypatch.setattr(repo, "git", command)
    completed = batches.recover_batch(repo, batch_id=pending["id"])
    assert completed["status"] == "completed"
    assert completed["worktree_generation"] == generation
    assert store.read()["integration_workspace"]["owner"] is None


@pytest.mark.parametrize("content", ["normal", "protected", "unknown"])
def test_real_adapter_binding_and_retained_content_gate(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    """真实Adapter子进程接收同代次绑定；受保护/未知内容保留场地并挡住推进。"""
    from solo_ai import batch_workspace

    repo = reusable_repo(git_repo)
    marker = repo.local_dir / "adapter-contexts.jsonl"
    adapter = (
        "import json, sys; from pathlib import Path; "
        "c=json.loads(Path(sys.argv[-1]).read_text(encoding='utf-8')); "
        "b=c['worktree_binding']; "
        "assert b['mode']=='reusable' and b['owner']==c['batch_id']; "
        "assert type(b['generation']) is int and b['generation']>0; "
        f"p=Path({str(marker)!r}); "
        "p.open('a',encoding='utf-8').write(json.dumps(c)+'\\n')"
    )
    install_runtime_adapter(
        repo,
        release_script="pass",
        verify_script="pass",
        batch_activate_script=adapter,
        batch_release_script=adapter,
    )
    repo.add_local_exclude("unknown-output/")
    candidate = publish(repo, name=f"binding {content}", relative="candidate.txt")
    initial_head = repo.head(git_repo)
    promote = batches._promote
    blocked = []

    def with_late_content(repo, store, batch):
        if content != "normal" and not blocked:
            relative = (
                ".tmp/state.db"
                if content == "protected"
                else "unknown-output/result.bin"
            )
            path = Path(batch["worktree"]) / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"must survive")
            blocked.append(path)
        return promote(repo, store, batch)

    monkeypatch.setattr(batches, "_promote", with_late_content)
    if content == "normal":
        completed = batches.seal_batch(repo, candidate_ids=[candidate["candidate_id"]])
    else:
        with pytest.raises(
            batch_workspace.BatchWorkspacePending, match="Protected or unknown"
        ):
            batches.seal_batch(repo, candidate_ids=[candidate["candidate_id"]])
        store = batches.CandidateBatchStore(repo)
        pending = next(iter(store.read()["batches"].values()))
        assert pending["status"] == "validated"
        assert store.read()["integration_workspace"]["owner"] == pending["id"]
        assert repo.head(git_repo) == initial_head
        assert blocked[0].read_bytes() == b"must survive"
        blocked[0].rename(tmp_path / f"preserved-{content}.bin")
        blocked[0].parent.rmdir()
        completed = batches.recover_batch(repo, batch_id=pending["id"])
    contexts = [
        json.loads(line) for line in marker.read_text(encoding="utf-8").splitlines()
    ]
    assert [item["operation"] for item in contexts] == [
        "batch-activate",
        "batch-release",
    ]
    assert contexts[0]["worktree_binding"] == contexts[1]["worktree_binding"]
    binding = contexts[0]["worktree_binding"]
    assert binding["owner"] == completed["id"]
    assert binding["generation"] == completed["worktree_generation"]
    assert binding["worktree_identity"] == path_identity(Path(completed["worktree"]))
    assert completed["status"] == "completed"
