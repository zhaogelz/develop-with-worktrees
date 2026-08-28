from __future__ import annotations

from pathlib import Path

import pytest
from conftest import declare_delegated_adapter
from solo_ai.config import CommandSpec
from solo_ai.lifecycle import initialize
from solo_ai.orchestration import BatchStore, create_batch
from solo_ai.orchestration.scheduler import frontier
from solo_ai.repo import GitRepo
from solo_ai.util import DirectoryLock, SoloAIError


def _tasks() -> list[dict[str, object]]:
    return [
        {
            "id": "api",
            "title": "提供查询接口",
            "acceptance": ["可以读取数据"],
            "write_scope": ["src/shared.py"],
        },
        {
            "id": "page",
            "title": "展示查询结果",
            "acceptance": ["用户能看到结果"],
            "depends_on": ["api"],
        },
        {
            "id": "copy",
            "title": "补充使用说明",
            "acceptance": ["普通人能理解用途"],
            "write_scope": ["src/shared.py"],
        },
    ]


def _create(repo: GitRepo, *, max_parallel: int = 5) -> dict[str, object]:
    declare_delegated_adapter(
        repo.root,
        available_slots=max_parallel,
        max_parallel=max_parallel,
    )
    return create_batch(
        repo,
        goal="让用户完成一次查询",
        tasks=_tasks(),
        controller="central-controller",
        adapter="delegated",
        max_parallel=max_parallel,
    )


def _state_bytes(*roots: Path) -> dict[str, bytes]:
    return {
        f"{index}:{path.relative_to(root).as_posix()}": path.read_bytes()
        for index, root in enumerate(roots)
        if root.exists()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@pytest.mark.parametrize("adapter", ["dww", "delegated"])
def test_deferred_repository_rejects_orchestration_without_writing_state(
    git_repo, adapter: str
) -> None:
    marker = git_repo / "scripts" / "worktree-flow.ps1"
    marker.parent.mkdir()
    marker.write_text("# native lifecycle\n", encoding="utf-8")
    repo = GitRepo(git_repo)

    with pytest.raises(SoloAIError):
        create_batch(
            repo,
            goal="由原生流程协调",
            tasks=_tasks(),
            controller="central-controller",
            adapter=adapter,
        )

    assert not (repo.common_dir / "solo-ai-orchestration").exists()
    assert not repo.local_dir.exists()


@pytest.mark.parametrize("adapter", ["dww", "delegated"])
def test_existing_batch_becomes_byte_stable_when_route_changes_to_defer(
    git_repo, adapter: str
) -> None:
    repo = GitRepo(git_repo)
    if adapter == "dww":
        initialize(
            repo,
            slots=2,
            commands=[CommandSpec(("git", "diff", "--check"))],
            accept=True,
            accept_static_only=False,
        )
        batch = create_batch(
            repo,
            goal="受管批次",
            tasks=_tasks(),
            controller="central-controller",
            adapter="dww",
        )
        marker = git_repo / "scripts" / "worktree-flow.ps1"
        marker.parent.mkdir()
        marker.write_text("# native lifecycle appeared\n", encoding="utf-8")
    else:
        batch = _create(repo)
        adapter_script = git_repo / "scripts" / "dww_adapter.py"
        adapter_script.write_text(
            adapter_script.read_text(encoding="utf-8") + "\n# contract drift\n",
            encoding="utf-8",
        )

    batch_id = str(batch["id"])
    store = BatchStore(repo)
    state_roots = (store.root, repo.local_dir)
    before = _state_bytes(*state_roots)
    route_error = (
        "requires a managed repository"
        if adapter == "dww"
        else "requires a valid, locally approved repository contract"
    )
    mutations = [
        lambda: store.confirm(batch_id, controller="central-controller"),
        lambda: store.pause(batch_id, controller="central-controller"),
        lambda: store.resume(batch_id, controller="central-controller"),
        lambda: store.take_over(
            batch_id, controller="replacement-controller", confirm=batch_id
        ),
        lambda: store.claim(
            batch_id,
            task_id="api",
            worker="worker-api",
            controller="central-controller",
        ),
        lambda: store.link_lifecycle_task(
            batch_id,
            task_id="api",
            lifecycle_task="native-task",
            controller="central-controller",
        ),
        lambda: store.complete(
            batch_id,
            task_id="api",
            evidence=[{"kind": "proof", "ref": "proof-api"}],
            controller="central-controller",
        ),
        lambda: store.block(
            batch_id,
            task_id="api",
            reason="blocked",
            controller="central-controller",
        ),
        lambda: store.record_attempt(
            batch_id,
            task_id="api",
            changed=False,
            summary="unchanged failure",
            controller="central-controller",
        ),
        lambda: store.cancel(
            batch_id,
            task_id="api",
            confirm="api",
            controller="central-controller",
        ),
        lambda: store.add_task(
            batch_id,
            raw_task={"id": "new", "title": "新任务", "acceptance": ["完成"]},
            inside_approved_goal=True,
            controller="central-controller",
        ),
        lambda: store.create_repair(
            batch_id,
            source_ids=["api"],
            raw_task={
                "id": "repair-api",
                "title": "修复接口",
                "acceptance": ["修复完成"],
            },
            reason="needs repair",
            controller="central-controller",
        ),
    ]

    for mutate in mutations:
        with pytest.raises(SoloAIError, match=route_error):
            mutate()
        assert _state_bytes(*state_roots) == before


def test_existing_batch_rechecks_route_after_taking_its_lock(
    git_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = GitRepo(git_repo)
    initialize(
        repo,
        slots=2,
        commands=[CommandSpec(("git", "diff", "--check"))],
        accept=True,
        accept_static_only=False,
    )
    batch = create_batch(
        repo,
        goal="受管批次",
        tasks=_tasks(),
        controller="central-controller",
        adapter="dww",
    )
    store = BatchStore(repo)
    before = _state_bytes(store.root, repo.local_dir)
    original_enter = DirectoryLock.__enter__

    def enter_then_defer(lock: DirectoryLock) -> DirectoryLock:
        acquired = original_enter(lock)
        marker = git_repo / "scripts" / "worktree-flow.ps1"
        marker.parent.mkdir(exist_ok=True)
        marker.write_text("# native lifecycle appeared\n", encoding="utf-8")
        return acquired

    monkeypatch.setattr(DirectoryLock, "__enter__", enter_then_defer)

    with pytest.raises(SoloAIError, match="requires a managed repository"):
        store.confirm(str(batch["id"]), controller="central-controller")

    assert _state_bytes(store.root, repo.local_dir) == before


def test_complex_batch_waits_for_one_confirmation_and_uses_a_separate_namespace(
    git_repo,
) -> None:
    repo = GitRepo(git_repo)
    batch = _create(repo)

    assert batch["status"] == "awaiting-confirmation"
    assert frontier(batch, available_slots=5) == []
    assert (
        repo.common_dir / "solo-ai-orchestration" / "batches" / f"{batch['id']}.json"
    ).exists()
    assert not (repo.local_dir / "state.json").exists()

    store = BatchStore(repo)
    active = store.confirm(str(batch["id"]), controller="central-controller")

    assert active["status"] == "running"
    assert [item["id"] for item in frontier(active, available_slots=5)] == [
        "api",
        "copy",
    ]
    with pytest.raises(SoloAIError, match="schedulable frontier"):
        store.claim(
            str(batch["id"]),
            task_id="api",
            worker="worker-api",
            controller="central-controller",
            available_slots=0,
        )


def test_frontier_allows_same_file_but_serializes_explicit_high_risk_resource(
    git_repo,
) -> None:
    repo = GitRepo(git_repo)
    batch = _create(repo)
    store = BatchStore(repo)
    active = store.confirm(str(batch["id"]), controller="central-controller")

    assert [item["id"] for item in frontier(active, available_slots=5)] == [
        "api",
        "copy",
    ]

    risky = create_batch(
        repo,
        goal="升级数据库",
        controller="other-controller",
        adapter="delegated",
        tasks=[
            {
                "id": "migration-a",
                "title": "增加字段",
                "acceptance": ["字段存在"],
                "exclusive_resources": ["database-schema"],
            },
            {
                "id": "migration-b",
                "title": "回填字段",
                "acceptance": ["数据正确"],
                "exclusive_resources": ["database-schema"],
            },
        ],
    )
    risky = store.confirm(str(risky["id"]), controller="other-controller")
    assert [item["id"] for item in frontier(risky, available_slots=5)] == [
        "migration-a"
    ]


def test_blocked_task_only_stops_its_downstream_and_repeated_unchanged_failure_escalates(
    git_repo,
) -> None:
    repo = GitRepo(git_repo)
    batch = _create(repo)
    store = BatchStore(repo)
    batch_id = str(batch["id"])
    store.confirm(batch_id, controller="central-controller")
    store.claim(
        batch_id, task_id="api", worker="worker-api", controller="central-controller"
    )
    first = store.record_attempt(
        batch_id,
        task_id="api",
        changed=False,
        summary="依赖服务没有启动",
        controller="central-controller",
    )
    assert first["task"]["status"] == "running"

    second = store.record_attempt(
        batch_id,
        task_id="api",
        changed=False,
        summary="依赖服务没有启动",
        controller="central-controller",
    )
    assert second["task"]["status"] == "blocked"
    current = store.batch(batch_id)
    assert current["status"] == "running"
    assert [item["id"] for item in frontier(current, available_slots=5)] == ["copy"]


def test_acceptance_ledger_requires_evidence_and_cancel_preserves_code(
    git_repo,
) -> None:
    repo = GitRepo(git_repo)
    declare_delegated_adapter(repo.root, available_slots=5, max_parallel=5)
    batch = create_batch(
        repo,
        goal="完成一个小改动",
        controller="central-controller",
        adapter="delegated",
        tasks=[{"id": "one", "title": "完成改动", "acceptance": ["测试通过"]}],
    )
    store = BatchStore(repo)
    batch_id = str(batch["id"])
    store.confirm(batch_id, controller="central-controller")
    store.claim(
        batch_id, task_id="one", worker="worker-one", controller="central-controller"
    )

    with pytest.raises(SoloAIError, match="acceptance evidence"):
        store.complete(
            batch_id, task_id="one", evidence=[], controller="central-controller"
        )

    done = store.complete(
        batch_id,
        task_id="one",
        evidence=[{"kind": "ready-proof", "ref": "proof-123"}],
        controller="central-controller",
    )
    assert done["status"] == "completed"
    assert done["acceptance"]["missing"] == []
    receipt = (
        repo.common_dir / "solo-ai-orchestration" / "receipts" / f"{batch_id}.json"
    )
    assert receipt.exists()

    cancelled = create_batch(
        repo,
        goal="保留现场",
        controller="cancel-controller",
        adapter="delegated",
        tasks=[{"id": "keep", "title": "保留分支", "acceptance": ["代码仍在"]}],
    )
    cancelled_id = str(cancelled["id"])
    store.confirm(cancelled_id, controller="cancel-controller")
    result = store.cancel(
        cancelled_id,
        task_id="keep",
        confirm="keep",
        controller="cancel-controller",
    )
    assert result["task"]["status"] == "cancelled"
    assert result["task"]["code_preserved"] is True


def test_new_controller_can_take_over_and_add_an_internal_task(git_repo) -> None:
    repo = GitRepo(git_repo)
    batch = _create(repo)
    store = BatchStore(repo)
    batch_id = str(batch["id"])
    handed_off = store.take_over(
        batch_id,
        controller="new-central-controller",
        confirm=batch_id,
    )
    assert handed_off["controller"] == "new-central-controller"
    store.confirm(batch_id, controller="new-central-controller")
    added = store.add_task(
        batch_id,
        raw_task={
            "id": "repair",
            "title": "补齐内部验证",
            "acceptance": ["验证能运行"],
            "kind": "repair",
        },
        inside_approved_goal=True,
        controller="new-central-controller",
    )
    assert added["task"]["kind"] == "repair"


def test_pause_and_resume_stop_only_new_dispatch_and_preserve_running_work(
    git_repo,
) -> None:
    repo = GitRepo(git_repo)
    batch = _create(repo)
    store = BatchStore(repo)
    batch_id = str(batch["id"])
    store.confirm(batch_id, controller="central-controller")
    store.claim(
        batch_id, task_id="api", worker="worker-api", controller="central-controller"
    )

    paused = store.pause(batch_id, controller="central-controller")
    assert paused["status"] == "paused"
    assert paused["tasks"]["api"]["status"] == "running"
    assert store.frontier(batch_id, available_slots=5) == []

    resumed = store.resume(batch_id, controller="central-controller")
    assert resumed["status"] == "running"
    assert [item["id"] for item in store.frontier(batch_id, available_slots=5)] == [
        "copy"
    ]


def test_fresh_repair_task_unblocks_downstream_without_reopening_old_task(
    git_repo,
) -> None:
    repo = GitRepo(git_repo)
    batch = _create(repo)
    store = BatchStore(repo)
    batch_id = str(batch["id"])
    store.confirm(batch_id, controller="central-controller")
    store.claim(
        batch_id, task_id="api", worker="worker-api", controller="central-controller"
    )
    store.block(
        batch_id,
        task_id="api",
        reason="接口契约需要从最新基线修复",
        controller="central-controller",
    )
    repair = store.create_repair(
        batch_id,
        source_ids=["api"],
        raw_task={
            "id": "repair-api",
            "title": "修复查询接口",
            "acceptance": ["接口重新可用"],
        },
        reason="接口契约需要从最新基线修复",
        controller="central-controller",
    )
    assert repair["task"]["kind"] == "repair"
    assert [item["id"] for item in store.frontier(batch_id, available_slots=5)] == [
        "copy",
        "repair-api",
    ]
    store.claim(
        batch_id,
        task_id="repair-api",
        worker="worker-api",
        controller="central-controller",
    )
    store.complete(
        batch_id,
        task_id="repair-api",
        evidence=[{"kind": "ready-proof", "ref": "proof-repair"}],
        controller="central-controller",
    )
    current = store.batch(batch_id)
    assert current["tasks"]["api"]["status"] == "completed"
    assert current["tasks"]["api"]["acceptance_evidence"][0] == {
        "kind": "repair-task",
        "ref": "repair-api",
    }
    assert [item["id"] for item in store.frontier(batch_id, available_slots=5)] == [
        "copy",
        "page",
    ]


def test_batch_refuses_more_than_five_parallel_workers(git_repo) -> None:
    repo = GitRepo(git_repo)
    with pytest.raises(SoloAIError, match="between 1 and 5"):
        create_batch(
            repo,
            goal="过量并发",
            controller="central-controller",
            adapter="delegated",
            max_parallel=6,
            tasks=[{"id": "one", "title": "任务", "acceptance": ["完成"]}],
        )
