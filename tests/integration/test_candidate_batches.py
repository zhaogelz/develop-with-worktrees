from __future__ import annotations

import calendar
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from conftest import git
from solo_ai import candidate_batches as batch_module
from solo_ai.candidate_batches import (
    CandidateBatchStore,
    prepare_candidate_repair,
    reconcile_batches,
    seal_batch,
)
from solo_ai.config import CommandSpec, load_repo_config, load_verification_config
from solo_ai.lifecycle import (
    abandon,
    adopt_task_anchor,
    approve,
    commit_task,
    finish,
    initialize,
    ready,
    recover,
    start,
)
from solo_ai.repo import GitRepo
from solo_ai.proof import approval_plan
from solo_ai.runtime_adapter import verify_runtime_effective
from solo_ai.state import StateStore
from solo_ai.task_context import anchor_path
from solo_ai.util import SoloAIError
from solo_ai.util import atomic_write_json, read_json

VERIFY = CommandSpec(("git", "diff", "--check", "main...HEAD"))


def initialized_batched(path: Path, *, auto_full: bool = True) -> GitRepo:
    repo = GitRepo(path)
    initialize(repo, slots=3, commands=[VERIFY], accept=True, accept_static_only=False)
    if not auto_full:
        config = path / ".solo-ai" / "config.toml"
        config.write_text(
            config.read_text(encoding="utf-8").replace(
                'seal_policy = "auto_full"', 'seal_policy = "explicit"'
            ),
            encoding="utf-8",
        )
        git(path, "add", ".solo-ai/config.toml")
        git(path, "commit", "-m", "test: require explicit candidate batches")
    approve(repo, load_verification_config(repo))
    return repo


def install_runtime_adapter(
    repo: GitRepo,
    *,
    release_script: str,
    verify_script: str,
    activate_script: str = "pass",
    batch_activate_script: str | None = None,
    batch_release_script: str | None = None,
) -> None:
    config = repo.root / ".solo-ai" / "config.toml"
    gitignore = repo.root / ".gitignore"
    ignored = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    if ".tmp/" not in ignored.splitlines():
        gitignore.write_text(ignored + ".tmp/\n", encoding="utf-8")
    batch_activate_line = (
        "batch_activate = " + json.dumps([sys.executable, "-c", batch_activate_script])
        if batch_activate_script is not None
        else ""
    )
    batch_release_line = (
        "batch_release = " + json.dumps([sys.executable, "-c", batch_release_script])
        if batch_release_script is not None
        else ""
    )
    config.write_text(
        config.read_text(encoding="utf-8")
        + f"""

[runtime_adapter]
activate = {json.dumps([sys.executable, "-c", activate_script])}
release = {json.dumps([sys.executable, "-c", release_script])}
{batch_activate_line}
{batch_release_line}
verify_effective = {json.dumps([sys.executable, "-c", verify_script])}
input_paths = [".solo-ai/config.toml"]
timeout_seconds = 30
""",
        encoding="utf-8",
    )
    git(repo.root, "add", ".solo-ai/config.toml", ".gitignore")
    git(repo.root, "commit", "-m", "test: install project runtime adapter")
    approve(repo, load_verification_config(repo))


def publish(repo: GitRepo, *, name: str, relative: str) -> dict[str, str]:
    task = start(repo, name=name)
    worktree = Path(task["worktree"])
    (worktree / relative).write_text(f"{name}\n", encoding="utf-8")
    commit_task(
        repo,
        task_id=task["id"],
        lease=task["lease"],
        message=f"test: {name}",
        paths=[relative],
    )
    ready(repo, task_id=task["id"], lease=task["lease"])
    return finish(repo, task_id=task["id"], lease=task["lease"])


def test_start_request_is_idempotent_and_anchor_is_first_class(git_repo: Path) -> None:
    repo = initialized_batched(git_repo, auto_full=False)
    first = start(repo, name="same work", request_id="request-123")
    second = start(repo, name="same work", request_id="request-123")

    assert second["id"] == first["id"]
    assert second["worktree"] == first["worktree"]
    assert second["request_reused"] is True
    assert Path(first["anchor_path"]) == anchor_path(repo, first["id"])
    assert Path(first["anchor_path"]).is_file()
    active = [
        item
        for item in StateStore(repo).read()["tasks"].values()
        if item["status"] not in {"finished", "abandoned", "candidate-published"}
    ]
    assert len(active) == 1
    assert StateStore(repo).read()["slots"][first["slot_id"]]["status"] == "active"


def test_runtime_adapter_activate_prepares_the_exact_slot_before_start_returns(
    git_repo: Path,
) -> None:
    repo = initialized_batched(git_repo, auto_full=False)
    context_marker = repo.local_dir / "runtime-activate-context.json"
    activate_script = (
        "from pathlib import Path; import sys; "
        f"marker=Path({str(context_marker)!r}); "
        "context=Path(sys.argv[-1]).read_text(encoding='utf-8'); "
        "marker.write_text(context, encoding='utf-8'); "
        "runtime=Path('.tmp/project-runtime/active.txt'); "
        "runtime.parent.mkdir(parents=True, exist_ok=True); "
        "runtime.write_text('active\\n', encoding='utf-8')"
    )
    install_runtime_adapter(
        repo,
        activate_script=activate_script,
        release_script="pass",
        verify_script="pass",
    )

    task = start(repo, name="activate exact runtime")

    context = json.loads(context_marker.read_text(encoding="utf-8"))
    plan = approval_plan(
        repo, cwd=repo.root, verification=load_verification_config(repo)
    )
    assert task["status"] == "active"
    assert plan["runtime_adapter"]["activate"][-1] == activate_script
    assert plan["runtime_adapter"]["input_hashes"]
    assert task["runtime_activation"]["operation"] == "activate"
    assert context["operation"] == "activate"
    assert context["task_id"] == task["id"]
    assert context["slot_id"] == task["slot_id"]
    assert context["worktree"] == str(Path(task["worktree"]).resolve())
    assert context["port_block_end"] - context["port_block_start"] == 99
    assert (Path(task["worktree"]) / ".tmp/project-runtime/active.txt").is_file()


def test_runtime_adapter_activate_failure_is_retryable_with_the_same_request(
    git_repo: Path,
) -> None:
    repo = initialized_batched(git_repo, auto_full=False)
    gate = repo.local_dir / "runtime-activate-allowed"
    activate_script = (
        "from pathlib import Path; "
        f"raise SystemExit(0 if Path({str(gate)!r}).exists() else 1)"
    )
    install_runtime_adapter(
        repo,
        activate_script=activate_script,
        release_script="pass",
        verify_script="pass",
    )

    with pytest.raises(SoloAIError, match="Runtime Adapter activate failed"):
        start(repo, name="retry runtime activation", request_id="activate-retry")

    pending = next(
        task
        for task in StateStore(repo).read()["tasks"].values()
        if task.get("request_id") == "activate-retry"
    )
    assert pending["status"] == "starting"
    assert StateStore(repo).read()["slots"][pending["slot_id"]]["status"] == (
        "starting"
    )
    gate.write_text("allowed\n", encoding="utf-8")

    retried = start(repo, name="retry runtime activation", request_id="activate-retry")

    assert retried["id"] == pending["id"]
    assert retried["status"] == "active"
    assert retried["request_reused"] is True


def test_runtime_adapter_activate_failure_is_retryable_by_recover(
    git_repo: Path,
) -> None:
    repo = initialized_batched(git_repo, auto_full=False)
    gate = repo.local_dir / "runtime-recover-allowed"
    activate_script = (
        "from pathlib import Path; "
        f"raise SystemExit(0 if Path({str(gate)!r}).exists() else 1)"
    )
    install_runtime_adapter(
        repo,
        activate_script=activate_script,
        release_script="pass",
        verify_script="pass",
    )

    with pytest.raises(SoloAIError, match="Runtime Adapter activate failed"):
        start(repo, name="recover runtime activation")
    pending = max(
        StateStore(repo).read()["tasks"].values(),
        key=lambda item: item["created_at"],
    )
    gate.write_text("allowed\n", encoding="utf-8")

    recovered = recover(repo, task_id=pending["id"])

    assert recovered["id"] == pending["id"]
    assert recovered["status"] == "active"
    assert StateStore(repo).read()["slots"][pending["slot_id"]]["status"] == ("active")


def test_runtime_adapter_activate_success_receipt_is_reused_after_interruption(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized_batched(git_repo, auto_full=False)
    counter = repo.local_dir / "runtime-activate-count.txt"
    activate_script = (
        "from pathlib import Path; "
        f"counter=Path({str(counter)!r}); "
        "value=int(counter.read_text(encoding='utf-8')) if counter.exists() else 0; "
        "counter.write_text(str(value + 1), encoding='utf-8')"
    )
    install_runtime_adapter(
        repo,
        activate_script=activate_script,
        release_script="pass",
        verify_script="pass",
    )
    original_activate = StateStore.activate_started_task

    def interrupt_projection(*args: object, **kwargs: object) -> dict[str, object]:
        raise KeyboardInterrupt("synthetic interruption after Adapter success")

    monkeypatch.setattr(StateStore, "activate_started_task", interrupt_projection)
    with pytest.raises(KeyboardInterrupt, match="synthetic interruption"):
        start(repo, name="reuse activate receipt", request_id="activate-interrupt")
    monkeypatch.setattr(StateStore, "activate_started_task", original_activate)

    retried = start(
        repo, name="reuse activate receipt", request_id="activate-interrupt"
    )

    assert retried["status"] == "active"
    assert retried["runtime_activation"]["reused"] is True
    assert counter.read_text(encoding="utf-8") == "1"


def test_runtime_adapter_activate_contamination_quarantines_and_preserves(
    git_repo: Path,
) -> None:
    repo = initialized_batched(git_repo, auto_full=False)
    install_runtime_adapter(
        repo,
        activate_script=(
            "from pathlib import Path; "
            "Path('adapter-pollution.txt').write_text('preserve\\n', encoding='utf-8')"
        ),
        release_script="pass",
        verify_script="pass",
    )

    with pytest.raises(SoloAIError, match="changed while the runtime Adapter"):
        start(repo, name="quarantine activate contamination")

    task = max(
        StateStore(repo).read()["tasks"].values(),
        key=lambda item: item["created_at"],
    )
    assert task["status"] == "quarantined"
    assert (Path(task["worktree"]) / "adapter-pollution.txt").read_text(
        encoding="utf-8"
    ) == "preserve\n"


def test_ready_requires_the_managed_task_anchor(git_repo: Path) -> None:
    repo = initialized_batched(git_repo, auto_full=False)
    task = start(repo, name="anchor required")
    anchor_path(repo, task["id"]).unlink()

    with pytest.raises(SoloAIError, match="anchor is missing"):
        ready(repo, task_id=task["id"], lease=task["lease"])

    adopted = adopt_task_anchor(
        repo,
        task_id=task["id"],
        objective="恢复旧任务目标",
        target="旧任务候选",
        scope="只恢复已确认范围",
        acceptance="Ready 验证通过",
        confirm=task["id"],
    )
    assert Path(adopted["anchor_path"]).is_file()


def test_preupgrade_task_gets_explicit_policy_snapshot_before_finish(
    git_repo: Path,
) -> None:
    repo = initialized_batched(git_repo)
    task = start(repo, name="legacy active task")
    worktree = Path(task["worktree"])
    (worktree / "legacy-active.txt").write_text("legacy\n", encoding="utf-8")
    commit_task(
        repo,
        task_id=task["id"],
        lease=task["lease"],
        message="test: legacy active task",
        paths=["legacy-active.txt"],
    )
    state_path = repo.local_dir / "state.json"
    legacy = read_json(state_path, {})
    legacy["schema_version"] = 5
    legacy["tasks"][task["id"]].pop("integration_policy", None)
    atomic_write_json(state_path, legacy)

    ready(repo, task_id=task["id"], lease=task["lease"])
    result = finish(repo, task_id=task["id"], lease=task["lease"])

    assert result["outcome"] == "candidate_published"
    candidate = CandidateBatchStore(repo).candidate_for_task(task["id"])
    assert candidate is not None
    assert candidate["integration_policy"]["seal_policy"] == "explicit"
    assert CandidateBatchStore(repo).summary()["batches"] == []


def test_finish_publishes_then_explicit_seal_integrates_exact_candidates(
    git_repo: Path,
) -> None:
    repo = initialized_batched(git_repo, auto_full=False)
    base_before = repo.head(git_repo)
    first = publish(repo, name="candidate a", relative="a.txt")
    second = publish(repo, name="candidate b", relative="b.txt")

    assert first["outcome"] == "candidate_published"
    assert second["outcome"] == "candidate_published"
    assert repo.head(git_repo) == base_before
    assert not (git_repo / "a.txt").exists()
    assert Path(first["anchor_path"]).is_file()

    result = seal_batch(
        repo, candidate_ids=[first["candidate_id"], second["candidate_id"]]
    )

    assert result["status"] == "completed"
    assert (git_repo / "a.txt").read_text(encoding="utf-8") == "candidate a\n"
    assert (git_repo / "b.txt").read_text(encoding="utf-8") == "candidate b\n"
    assert not Path(first["anchor_path"]).exists()
    assert not Path(second["anchor_path"]).exists()
    recovered = recover(repo, task_id=first["task_id"])
    assert recovered["status"] == "integrated"
    assert recovered["batch_id"] == result["id"]
    pool = CandidateBatchStore(repo).summary()
    assert {item["status"] for item in pool["candidates"]} == {"integrated"}


def test_four_candidates_wait_and_fifth_finish_auto_integrates_oldest_five(
    git_repo: Path,
) -> None:
    repo = initialized_batched(git_repo)
    base_before = repo.head(git_repo)
    published = [
        publish(repo, name=f"candidate {index}", relative=f"{index}.txt")
        for index in range(1, 5)
    ]

    assert all(item["outcome"] == "candidate_published" for item in published)
    assert repo.head(git_repo) == base_before
    assert CandidateBatchStore(repo).summary()["batches"] == []

    fifth = publish(repo, name="candidate 5", relative="5.txt")

    assert fifth["outcome"] == "batch_integrated"
    assert fifth["candidate_count"] == 5
    assert repo.head(git_repo) == fifth["integrated_head"]
    assert all((git_repo / f"{index}.txt").is_file() for index in range(1, 6))
    pool = CandidateBatchStore(repo).summary()
    assert [item["trigger"] for item in pool["batches"]] == ["auto_full"]
    assert {item["status"] for item in pool["candidates"]} == {"integrated"}


def test_quiet_tail_waits_for_all_producers_and_the_full_stability_period(
    git_repo: Path,
) -> None:
    repo = initialized_batched(git_repo)
    blocker = start(repo, name="still modifying")
    candidate = publish(repo, name="quiet candidate", relative="quiet.txt")
    base_before = repo.head(git_repo)

    while_active = reconcile_batches(
        repo,
        cause="heartbeat",
        now_epoch=time.time() + 86_400,
    )

    assert while_active["status"] == "waiting"
    assert while_active["waiting"][0]["active_candidate_producers"] == 1
    assert while_active["waiting"][0]["next_reconcile_at"] is None
    assert repo.head(git_repo) == base_before

    abandon(
        repo,
        task_id=blocker["id"],
        lease=blocker["lease"],
        confirm=blocker["id"],
    )
    policy = candidate["reconciliation"]["waiting"][0]["activation_epoch"]
    snapshot = StateStore(repo).candidate_producer_snapshot(
        base_ref="main", activation_epoch=policy
    )
    quiet_epoch = calendar.timegm(
        time.strptime(snapshot["quiet_since"], "%Y-%m-%dT%H:%M:%SZ")
    )

    too_early = reconcile_batches(repo, cause="session-end", now_epoch=quiet_epoch + 89)
    assert too_early["status"] == "waiting"
    assert repo.head(git_repo) == base_before

    completed = reconcile_batches(repo, cause="heartbeat", now_epoch=quiet_epoch + 90)
    assert completed["status"] == "completed"
    assert completed["batch"]["trigger"] == "quiet_tail"
    assert completed["delivered"] is True
    assert (git_repo / "quiet.txt").read_text(encoding="utf-8") == "quiet candidate\n"


def test_forced_tail_requires_an_explicit_authorized_cause(git_repo: Path) -> None:
    repo = initialized_batched(git_repo)
    candidate = publish(repo, name="forced candidate", relative="forced.txt")

    with pytest.raises(SoloAIError, match="requires cause"):
        reconcile_batches(repo, force=True, cause="heartbeat")

    completed = reconcile_batches(repo, force=True, cause="dependency")

    assert completed["status"] == "completed"
    assert completed["batch"]["candidate_ids"] == [candidate["candidate_id"]]
    assert completed["batch"]["trigger"] == "explicit_tail"


def test_repeating_the_same_exact_seal_returns_the_same_completed_batch(
    git_repo: Path,
) -> None:
    repo = initialized_batched(git_repo, auto_full=False)
    candidate = publish(repo, name="idempotent candidate", relative="same.txt")

    first = seal_batch(repo, candidate_ids=[candidate["candidate_id"]])
    second = seal_batch(repo, candidate_ids=[candidate["candidate_id"]])

    assert first["id"] == second["id"]
    assert second["status"] == "completed"
    assert len(CandidateBatchStore(repo).summary()["batches"]) == 1


def test_concurrent_reconcile_resumes_one_frozen_batch_without_duplication(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized_batched(git_repo, auto_full=False)
    candidate = publish(repo, name="concurrent reconcile", relative="once.txt")
    store = CandidateBatchStore(repo)
    frozen = store.seal([candidate["candidate_id"]], batch_size=5)
    rendezvous = threading.Barrier(2)
    original_run = batch_module.run_batch

    def synchronized_run(target_repo: GitRepo, *, batch_id: str) -> dict[str, object]:
        rendezvous.wait(timeout=10)
        return original_run(target_repo, batch_id=batch_id)

    monkeypatch.setattr(batch_module, "run_batch", synchronized_run)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(lambda _: reconcile_batches(repo, cause="heartbeat"), range(2))
        )

    assert {item["batch"]["id"] for item in results} == {frozen["id"]}
    assert {item["status"] for item in results} == {"completed"}
    assert len(store.summary()["batches"]) == 1


def test_start_cannot_cross_a_tail_freeze_decision(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized_batched(git_repo)
    candidate = publish(repo, name="before freeze", relative="before.txt")
    policy = candidate["reconciliation"]["waiting"][0]["activation_epoch"]
    snapshot = StateStore(repo).candidate_producer_snapshot(
        base_ref="main", activation_epoch=policy
    )
    quiet_epoch = calendar.timegm(
        time.strptime(snapshot["quiet_since"], "%Y-%m-%dT%H:%M:%SZ")
    )
    entered = threading.Event()
    release = threading.Event()
    original_reconcile = CandidateBatchStore.reconcile

    def delayed_reconcile(
        store: CandidateBatchStore, **kwargs: object
    ) -> dict[str, object]:
        entered.set()
        assert release.wait(timeout=10)
        return original_reconcile(store, **kwargs)

    monkeypatch.setattr(CandidateBatchStore, "reconcile", delayed_reconcile)
    monkeypatch.setattr(
        batch_module,
        "run_batch",
        lambda target_repo, *, batch_id: CandidateBatchStore(target_repo).batch(
            batch_id
        ),
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        reconcile_future = executor.submit(
            reconcile_batches,
            repo,
            cause="heartbeat",
            now_epoch=quiet_epoch + 90,
        )
        assert entered.wait(timeout=10)
        start_future = executor.submit(start, repo, name="after freeze")
        time.sleep(0.2)
        assert not start_future.done()
        release.set()
        reconciliation = reconcile_future.result(timeout=10)
        started = start_future.result(timeout=10)

    assert reconciliation["batch"]["candidate_ids"] == [candidate["candidate_id"]]
    assert started["status"] == "active"
    assert started["id"] not in reconciliation["batch"]["candidate_ids"]


def test_candidate_publication_is_not_reported_as_delivery(git_repo: Path) -> None:
    repo = initialized_batched(git_repo)
    candidate = publish(repo, name="delivery projection", relative="delivery.txt")

    assert candidate["delivered"] is False
    assert candidate["delivery_status"] == "awaiting-integration"
    projected = CandidateBatchStore(repo).summary()["candidates"][0]
    assert projected["delivered"] is False
    assert projected["delivery_status"] == "awaiting-integration"


def test_runtime_release_holds_the_fifth_candidate_until_adapter_success(
    git_repo: Path,
) -> None:
    repo = initialized_batched(git_repo)
    gate = repo.local_dir / "runtime-release-allowed"
    release_marker = repo.local_dir / "runtime-release-context.json"
    verify_marker = repo.local_dir / "runtime-verify-context.json"
    gate.write_text("allowed\n", encoding="utf-8")
    release_script = (
        "from pathlib import Path; import sys; "
        f"gate=Path({str(gate)!r}); marker=Path({str(release_marker)!r}); "
        "marker.write_text(Path(sys.argv[-1]).read_text(encoding='utf-8'), encoding='utf-8'); "
        "raise SystemExit(0 if gate.exists() else 1)"
    )
    verify_script = (
        "from pathlib import Path; import sys; "
        f"marker=Path({str(verify_marker)!r}); "
        "marker.write_text(Path(sys.argv[-1]).read_text(encoding='utf-8'), encoding='utf-8')"
    )
    install_runtime_adapter(
        repo,
        release_script=release_script,
        verify_script=verify_script,
    )
    for index in range(1, 5):
        publish(repo, name=f"adapter {index}", relative=f"adapter-{index}.txt")
    gate.unlink()
    fifth = start(repo, name="adapter 5")
    worktree = Path(fifth["worktree"])
    (worktree / "adapter-5.txt").write_text("adapter 5\n", encoding="utf-8")
    commit_task(
        repo,
        task_id=fifth["id"],
        lease=fifth["lease"],
        message="test: adapter 5",
        paths=["adapter-5.txt"],
    )
    ready(repo, task_id=fifth["id"], lease=fifth["lease"])
    base_before = repo.head(git_repo)

    with pytest.raises(SoloAIError, match="Runtime Adapter release failed"):
        finish(repo, task_id=fifth["id"], lease=fifth["lease"])

    held = CandidateBatchStore(repo).candidate_for_task(fifth["id"])
    assert held is not None
    assert held["status"] == "held"
    assert CandidateBatchStore(repo).summary()["batches"] == []
    assert repo.head(git_repo) == base_before
    assert StateStore(repo).task(fifth["id"])["status"] == "publishing"

    gate.write_text("allowed\n", encoding="utf-8")
    completed = finish(repo, task_id=fifth["id"], lease=fifth["lease"])

    assert completed["outcome"] == "batch_integrated"
    assert completed["candidate_count"] == 5
    release_context = json.loads(release_marker.read_text(encoding="utf-8"))
    assert release_context["operation"] == "release"
    runtime = verify_runtime_effective(repo, candidate_id=completed["candidate_id"])
    assert runtime["runtime_effective"] is True
    verify_context = json.loads(verify_marker.read_text(encoding="utf-8"))
    assert verify_context["operation"] == "verify-effective"
    assert verify_context["candidate_id"] == completed["candidate_id"]


def test_recover_activates_a_held_candidate_after_post_release_interruption(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized_batched(git_repo, auto_full=False)
    task = start(repo, name="activate after interruption")
    worktree = Path(task["worktree"])
    (worktree / "recover-held.txt").write_text("recover\n", encoding="utf-8")
    commit_task(
        repo,
        task_id=task["id"],
        lease=task["lease"],
        message="test: recover held candidate",
        paths=["recover-held.txt"],
    )
    ready(repo, task_id=task["id"], lease=task["lease"])
    original_activate = CandidateBatchStore.activate

    def interrupt_activation(*args: object, **kwargs: object) -> dict[str, object]:
        raise KeyboardInterrupt("synthetic interruption before candidate activation")

    monkeypatch.setattr(CandidateBatchStore, "activate", interrupt_activation)
    with pytest.raises(KeyboardInterrupt, match="synthetic interruption"):
        finish(repo, task_id=task["id"], lease=task["lease"])

    candidate = CandidateBatchStore(repo).candidate_for_task(task["id"])
    assert candidate is not None
    assert candidate["status"] == "held"
    assert StateStore(repo).task(task["id"])["status"] == "candidate-published"
    monkeypatch.setattr(CandidateBatchStore, "activate", original_activate)

    recovered = recover(repo, task_id=task["id"])

    assert recovered["status"] == "candidate-published"
    assert recovered["delivery_status"] == "awaiting-integration"
    activated = CandidateBatchStore(repo).candidate_for_task(task["id"])
    assert activated is not None
    assert activated["status"] == "pending"


def test_runtime_adapter_worktree_contamination_fails_closed(git_repo: Path) -> None:
    repo = initialized_batched(git_repo, auto_full=False)
    install_runtime_adapter(
        repo,
        release_script=(
            "from pathlib import Path; "
            "Path('adapter-pollution.txt').write_text('preserve\\n', encoding='utf-8')"
        ),
        verify_script="pass",
    )
    task = start(repo, name="adapter contamination")
    worktree = Path(task["worktree"])
    (worktree / "candidate.txt").write_text("candidate\n", encoding="utf-8")
    commit_task(
        repo,
        task_id=task["id"],
        lease=task["lease"],
        message="test: preserve adapter contamination",
        paths=["candidate.txt"],
    )
    ready(repo, task_id=task["id"], lease=task["lease"])

    with pytest.raises(SoloAIError, match="changed or contaminated"):
        finish(repo, task_id=task["id"], lease=task["lease"])

    candidate = CandidateBatchStore(repo).candidate_for_task(task["id"])
    assert candidate is not None
    assert candidate["status"] == "held"
    assert StateStore(repo).task(task["id"])["status"] == "publishing"
    assert (worktree / "adapter-pollution.txt").read_text(encoding="utf-8") == (
        "preserve\n"
    )
    assert CandidateBatchStore(repo).summary()["batches"] == []


def test_finish_releases_candidate_without_waiting_on_an_existing_batch(
    git_repo: Path,
) -> None:
    repo = initialized_batched(git_repo, auto_full=False)
    first = publish(repo, name="already frozen", relative="frozen.txt")
    store = CandidateBatchStore(repo)
    frozen = store.seal([first["candidate_id"]], batch_size=5)

    second = publish(repo, name="released promptly", relative="released.txt")

    assert second["outcome"] == "candidate_published"
    assert second["reconciliation"]["status"] == "active-batch"
    assert second["reconciliation"]["batch"]["id"] == frozen["id"]
    assert store.batch(frozen["id"])["status"] == "sealed"


def test_enabling_auto_full_does_not_capture_legacy_explicit_candidates(
    git_repo: Path,
) -> None:
    repo = initialized_batched(git_repo, auto_full=False)
    legacy = [
        publish(repo, name=f"legacy {index}", relative=f"legacy-{index}.txt")
        for index in range(1, 5)
    ]
    config = git_repo / ".solo-ai" / "config.toml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            'seal_policy = "explicit"', 'seal_policy = "auto_full"'
        ),
        encoding="utf-8",
    )
    git(git_repo, "add", ".solo-ai/config.toml")
    git(git_repo, "commit", "-m", "test: activate automatic full batches")
    approve(repo, load_verification_config(repo))

    first_new = publish(repo, name="new 1", relative="new-1.txt")

    assert first_new["outcome"] == "candidate_published"
    after_first = CandidateBatchStore(repo).summary()
    legacy_ids = {item["candidate_id"] for item in legacy}
    assert all(
        first_new["candidate_id"] not in batch["candidate_ids"]
        for batch in after_first["batches"]
    )
    assert all(
        set(batch["candidate_ids"]) <= legacy_ids for batch in after_first["batches"]
    )

    remaining_new = [
        publish(repo, name=f"new {index}", relative=f"new-{index}.txt")
        for index in range(2, 6)
    ]

    assert remaining_new[-1]["outcome"] == "batch_integrated"
    pool = CandidateBatchStore(repo).summary()
    new_ids = {
        first_new["candidate_id"],
        *(item["candidate_id"] for item in remaining_new),
    }
    assert any(
        batch["trigger"] == "auto_full" and set(batch["candidate_ids"]) == new_ids
        for batch in pool["batches"]
    )
    assert all(
        not (set(batch["candidate_ids"]) & legacy_ids)
        or not (set(batch["candidate_ids"]) & new_ids)
        for batch in pool["batches"]
    )


def test_concurrent_fifth_and_sixth_publications_create_only_one_full_batch(
    git_repo: Path,
) -> None:
    repo = initialized_batched(git_repo)
    store = CandidateBatchStore(repo)
    head = repo.head(git_repo)
    policy = StateStore.integration_policy(load_repo_config(repo))

    def publish_raw(index: int) -> dict[str, object]:
        candidate_id = f"candidate-concurrent-{index}"
        return store.publish(
            {
                "candidate_id": candidate_id,
                "task_id": f"task-concurrent-{index}",
                "name": candidate_id,
                "ref": f"refs/dww/candidates/{candidate_id}",
                "head": head,
                "base_head": head,
                "base_ref": "main",
                "proof": f"proof-{index}",
                "integration_policy": policy,
            },
            capacity=10,
            batch_size=5,
            seal_policy="auto_full",
        )

    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(publish_raw, range(1, 7)))

    auto_batches = [item["auto_batch"] for item in results if item["auto_batch"]]
    summary = store.summary()
    assert len({item["id"] for item in auto_batches}) == 1
    assert len(summary["batches"]) == 1
    assert len(summary["batches"][0]["candidate_ids"]) == 5
    assert sum(item["status"] == "pending" for item in summary["candidates"]) == 1

    for index in range(7, 11):
        publish_raw(index)
    with pytest.raises(SoloAIError, match="pool is full"):
        publish_raw(11)
    full = store.summary()
    assert len(full["batches"]) == 1
    assert len(full["candidates"]) == 10
    assert sum(item["status"] == "pending" for item in full["candidates"]) == 5


def test_failed_combined_validation_preserves_base_and_generation_is_not_rerun(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized_batched(git_repo, auto_full=False)
    candidate = publish(repo, name="candidate failure", relative="failure.txt")
    base_before = repo.head(git_repo)

    def fail_validation(*args: object, **kwargs: object) -> dict[str, object]:
        raise SoloAIError("combined validation failed")

    monkeypatch.setattr(batch_module, "validate", fail_validation)
    with pytest.raises(SoloAIError, match="combined validation failed"):
        seal_batch(repo, candidate_ids=[candidate["candidate_id"]])

    assert repo.head(git_repo) == base_before
    assert not (git_repo / "failure.txt").exists()
    failed = CandidateBatchStore(repo).summary()["batches"][0]
    assert failed["status"] == "failed"
    retained = CandidateBatchStore(repo).candidate_for_task(candidate["task_id"])
    assert retained is not None
    assert retained["status"] == "retained"
    assert failed["failure_kind"] == "validation_failed"
    with pytest.raises(SoloAIError, match="Automatic repair is limited"):
        prepare_candidate_repair(repo, candidate_id=candidate["candidate_id"])
    with pytest.raises(SoloAIError, match="will not be rerun automatically"):
        batch_module.recover_batch(repo, batch_id=failed["id"])


def test_batch_runtime_adapter_wraps_full_validation_and_uses_dedicated_ports(
    git_repo: Path,
) -> None:
    repo = initialized_batched(git_repo, auto_full=False)
    activate_marker = repo.local_dir / "batch-activate-context.json"
    release_marker = repo.local_dir / "batch-release-context.json"
    active_marker = repo.local_dir / "batch-runtime-active"
    activate_script = (
        "from pathlib import Path; import sys; "
        f"Path({str(activate_marker)!r}).write_text(Path(sys.argv[-1]).read_text(encoding='utf-8'), encoding='utf-8'); "
        f"Path({str(active_marker)!r}).write_text('active\\n', encoding='utf-8')"
    )
    release_script = (
        "from pathlib import Path; import sys; "
        f"active=Path({str(active_marker)!r}); "
        "assert active.is_file(); active.unlink(); "
        f"Path({str(release_marker)!r}).write_text(Path(sys.argv[-1]).read_text(encoding='utf-8'), encoding='utf-8')"
    )
    install_runtime_adapter(
        repo,
        release_script="pass",
        verify_script="pass",
        batch_activate_script=activate_script,
        batch_release_script=release_script,
    )
    candidate = publish(repo, name="batch runtime", relative="batch-runtime.txt")

    completed = seal_batch(repo, candidate_ids=[candidate["candidate_id"]])

    activation = json.loads(activate_marker.read_text(encoding="utf-8"))
    release = json.loads(release_marker.read_text(encoding="utf-8"))
    assert completed["status"] == "completed"
    assert activation["operation"] == "batch-activate"
    assert release["operation"] == "batch-release"
    assert activation["runtime_cycle"] == 1
    assert release["runtime_cycle"] == 1
    assert completed["runtime_cycle"] == 1
    assert release["validation_outcome"] == "passed"
    assert activation["batch_id"] == completed["id"]
    assert activation["candidate_ids"] == [candidate["candidate_id"]]
    assert activation["integration_head"] == completed["integrated_head"]
    assert activation["port_block_start"] == 23200
    assert activation["port_block_end"] == 23299
    assert activation["port_block_start"] > 20000 + 31 * 100 + 99
    assert not active_marker.exists()
    assert completed["runtime_activation"]["result"] == "passed"
    assert completed["runtime_release"]["result"] == "passed"


def test_batch_runtime_activation_failure_is_recoverable_without_unsealing(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized_batched(git_repo, auto_full=False)
    gate = repo.local_dir / "allow-batch-activate"
    activate_script = (
        "from pathlib import Path; "
        f"raise SystemExit(0 if Path({str(gate)!r}).exists() else 1)"
    )
    install_runtime_adapter(
        repo,
        release_script="pass",
        verify_script="pass",
        batch_activate_script=activate_script,
        batch_release_script="pass",
    )
    candidate = publish(repo, name="activation retry", relative="activation.txt")
    base_before = repo.head(git_repo)
    validation_calls = 0

    def count_validation(*args: object, **kwargs: object) -> dict[str, object]:
        nonlocal validation_calls
        validation_calls += 1
        return {"fingerprint": "unexpected"}

    monkeypatch.setattr(batch_module, "validate", count_validation)

    with pytest.raises(batch_module.BatchRuntimePending, match="activation is pending"):
        seal_batch(repo, candidate_ids=[candidate["candidate_id"]])

    pending = CandidateBatchStore(repo).summary()["batches"][0]
    retained = CandidateBatchStore(repo).candidate(candidate["candidate_id"])
    assert pending["status"] == "runtime_activation_pending"
    assert pending["runtime_cycle"] == 1
    assert retained["status"] == "sealed"
    assert repo.head(git_repo) == base_before
    assert validation_calls == 0
    gate.write_text("allowed\n", encoding="utf-8")
    monkeypatch.undo()

    completed = batch_module.recover_batch(repo, batch_id=pending["id"])

    assert completed["status"] == "completed"
    assert completed["runtime_cycle"] == 1


def test_batch_validation_failure_releases_runtime_before_failed_closed(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized_batched(git_repo, auto_full=False)
    active = repo.local_dir / "validation-failure-runtime"
    release_context = repo.local_dir / "validation-failure-release.json"
    install_runtime_adapter(
        repo,
        release_script="pass",
        verify_script="pass",
        batch_activate_script=(
            "from pathlib import Path; "
            f"Path({str(active)!r}).write_text('active\\n', encoding='utf-8')"
        ),
        batch_release_script=(
            "from pathlib import Path; import sys; "
            f"active=Path({str(active)!r}); assert active.is_file(); active.unlink(); "
            f"Path({str(release_context)!r}).write_text(Path(sys.argv[-1]).read_text(encoding='utf-8'), encoding='utf-8')"
        ),
    )
    candidate = publish(repo, name="validation release", relative="validation.txt")
    base_before = repo.head(git_repo)

    def fail_validation(*args: object, **kwargs: object) -> dict[str, object]:
        raise SoloAIError("synthetic combined failure")

    monkeypatch.setattr(batch_module, "validate", fail_validation)
    with pytest.raises(SoloAIError, match="synthetic combined failure"):
        seal_batch(repo, candidate_ids=[candidate["candidate_id"]])

    context = json.loads(release_context.read_text(encoding="utf-8"))
    failed = CandidateBatchStore(repo).summary()["batches"][0]
    assert context["validation_outcome"] == "failed"
    assert failed["status"] == "failed"
    assert failed["failure_kind"] == "validation_failed"
    assert repo.head(git_repo) == base_before
    assert not active.exists()


def test_batch_runtime_release_failure_blocks_promotion_and_recovery_reuses_full(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized_batched(git_repo, auto_full=False)
    gate = repo.local_dir / "allow-batch-release"
    release_script = (
        "from pathlib import Path; "
        f"raise SystemExit(0 if Path({str(gate)!r}).exists() else 1)"
    )
    install_runtime_adapter(
        repo,
        release_script="pass",
        verify_script="pass",
        batch_activate_script="pass",
        batch_release_script=release_script,
    )
    candidate = publish(repo, name="release retry", relative="release.txt")
    base_before = repo.head(git_repo)
    original_validate = batch_module.validate
    validation_calls = 0

    def count_validation(*args: object, **kwargs: object) -> dict[str, object]:
        nonlocal validation_calls
        validation_calls += 1
        return original_validate(*args, **kwargs)

    monkeypatch.setattr(batch_module, "validate", count_validation)
    with pytest.raises(batch_module.BatchRuntimePending, match="release is pending"):
        seal_batch(repo, candidate_ids=[candidate["candidate_id"]])

    pending = CandidateBatchStore(repo).summary()["batches"][0]
    assert pending["status"] == "runtime_release_pending"
    assert pending["runtime_cycle"] == 1
    assert repo.head(git_repo) == base_before
    gate.write_text("allowed\n", encoding="utf-8")

    completed = batch_module.recover_batch(repo, batch_id=pending["id"])

    assert completed["status"] == "completed"
    assert completed["runtime_cycle"] == 1
    assert validation_calls == 1


def test_interrupted_batch_validation_releases_then_starts_a_new_runtime_cycle(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized_batched(git_repo, auto_full=False)
    activate_count = repo.local_dir / "batch-activate-count.txt"
    release_context = repo.local_dir / "batch-interrupt-release.json"
    activate_script = (
        "from pathlib import Path; "
        f"counter=Path({str(activate_count)!r}); "
        "value=int(counter.read_text(encoding='utf-8')) if counter.exists() else 0; "
        "counter.write_text(str(value + 1), encoding='utf-8')"
    )
    release_script = (
        "from pathlib import Path; import sys; "
        f"Path({str(release_context)!r}).write_text(Path(sys.argv[-1]).read_text(encoding='utf-8'), encoding='utf-8')"
    )
    install_runtime_adapter(
        repo,
        release_script="pass",
        verify_script="pass",
        batch_activate_script=activate_script,
        batch_release_script=release_script,
    )
    candidate = publish(repo, name="interrupt full", relative="interrupt.txt")
    original_validate = batch_module.validate
    interrupted = False

    def interrupt_once(*args: object, **kwargs: object) -> dict[str, object]:
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise KeyboardInterrupt("synthetic Full interruption")
        return original_validate(*args, **kwargs)

    monkeypatch.setattr(batch_module, "validate", interrupt_once)
    with pytest.raises(KeyboardInterrupt, match="synthetic Full interruption"):
        seal_batch(repo, candidate_ids=[candidate["candidate_id"]])

    interrupted_context = json.loads(release_context.read_text(encoding="utf-8"))
    pending = CandidateBatchStore(repo).summary()["batches"][0]
    assert interrupted_context["validation_outcome"] == "interrupted"
    assert interrupted_context["runtime_cycle"] == 1
    assert pending["status"] == "composed"
    assert pending["runtime_cycle"] == 1

    completed = batch_module.recover_batch(repo, batch_id=pending["id"])
    completed_release = json.loads(release_context.read_text(encoding="utf-8"))

    assert completed["status"] == "completed"
    assert completed["runtime_cycle"] == 2
    assert completed_release["runtime_cycle"] == 2
    assert completed_release["validation_outcome"] == "passed"
    assert activate_count.read_text(encoding="utf-8") == "2"


def test_interruption_after_batch_activation_reuses_same_cycle_receipt(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized_batched(git_repo, auto_full=False)
    activate_count = repo.local_dir / "batch-activation-projection-count.txt"
    activate_script = (
        "from pathlib import Path; "
        f"counter=Path({str(activate_count)!r}); "
        "value=int(counter.read_text(encoding='utf-8')) if counter.exists() else 0; "
        "counter.write_text(str(value + 1), encoding='utf-8')"
    )
    install_runtime_adapter(
        repo,
        release_script="pass",
        verify_script="pass",
        batch_activate_script=activate_script,
        batch_release_script="pass",
    )
    candidate = publish(
        repo, name="activation projection", relative="activation-projection.txt"
    )
    original_update = CandidateBatchStore.update_batch
    interrupted = False

    def interrupt_runtime_active(
        self: CandidateBatchStore, batch_id: str, **changes: object
    ) -> dict[str, object]:
        nonlocal interrupted
        if changes.get("status") == "runtime_active" and not interrupted:
            interrupted = True
            raise KeyboardInterrupt("synthetic activation projection interruption")
        return original_update(self, batch_id, **changes)

    monkeypatch.setattr(CandidateBatchStore, "update_batch", interrupt_runtime_active)
    with pytest.raises(KeyboardInterrupt, match="activation projection interruption"):
        seal_batch(repo, candidate_ids=[candidate["candidate_id"]])
    monkeypatch.setattr(CandidateBatchStore, "update_batch", original_update)

    pending = CandidateBatchStore(repo).summary()["batches"][0]
    assert pending["status"] == "runtime_activating"
    assert pending["runtime_cycle"] == 1
    completed = batch_module.recover_batch(repo, batch_id=pending["id"])

    assert completed["status"] == "completed"
    assert completed["runtime_cycle"] == 1
    assert completed["runtime_activation"]["reused"] is True
    assert activate_count.read_text(encoding="utf-8") == "1"


def test_interruption_after_batch_release_reuses_exact_release_receipt(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized_batched(git_repo, auto_full=False)
    release_count = repo.local_dir / "batch-release-count.txt"
    release_script = (
        "from pathlib import Path; "
        f"counter=Path({str(release_count)!r}); "
        "value=int(counter.read_text(encoding='utf-8')) if counter.exists() else 0; "
        "counter.write_text(str(value + 1), encoding='utf-8')"
    )
    install_runtime_adapter(
        repo,
        release_script="pass",
        verify_script="pass",
        batch_activate_script="pass",
        batch_release_script=release_script,
    )
    candidate = publish(repo, name="release projection", relative="projection.txt")
    original_update = CandidateBatchStore.update_batch
    interrupted = False

    def interrupt_validated(
        self: CandidateBatchStore, batch_id: str, **changes: object
    ) -> dict[str, object]:
        nonlocal interrupted
        if changes.get("status") == "validated" and not interrupted:
            interrupted = True
            raise KeyboardInterrupt("synthetic release projection interruption")
        return original_update(self, batch_id, **changes)

    monkeypatch.setattr(CandidateBatchStore, "update_batch", interrupt_validated)
    with pytest.raises(KeyboardInterrupt, match="release projection interruption"):
        seal_batch(repo, candidate_ids=[candidate["candidate_id"]])
    monkeypatch.setattr(CandidateBatchStore, "update_batch", original_update)

    pending = CandidateBatchStore(repo).summary()["batches"][0]
    assert pending["status"] == "runtime_releasing"
    assert pending["runtime_cycle"] == 1
    completed = batch_module.recover_batch(repo, batch_id=pending["id"])

    assert completed["status"] == "completed"
    assert completed["runtime_cycle"] == 1
    assert release_count.read_text(encoding="utf-8") == "1"


def test_composition_conflict_prepares_bounded_repair_and_replacement_candidate(
    git_repo: Path,
) -> None:
    repo = initialized_batched(git_repo)
    candidate = publish(repo, name="candidate conflict", relative="shared.txt")
    (git_repo / "shared.txt").write_text("main change\n", encoding="utf-8")
    git(git_repo, "add", "shared.txt")
    git(git_repo, "commit", "-m", "test: advance conflicting base")
    base_before = repo.head(git_repo)

    with pytest.raises(SoloAIError, match="conflicts with the sealed batch"):
        seal_batch(repo, candidate_ids=[candidate["candidate_id"]])

    source = CandidateBatchStore(repo).candidate(candidate["candidate_id"])
    assert source["status"] == "retained"
    assert source["last_failure_kind"] == "composition_conflict"
    assert source["repair_eligible"] is True

    repair = prepare_candidate_repair(repo, candidate_id=candidate["candidate_id"])
    repair_worktree = Path(repair["worktree"])
    assert repair["outcome"] == "conflicted"
    assert repair["repair_attempt"] == 1
    assert repair["manual_notification_required"] is False
    assert repair["conflict_paths"] == ["shared.txt"]
    assert repo.head(git_repo) == base_before
    reused = prepare_candidate_repair(repo, candidate_id=candidate["candidate_id"])
    assert reused["id"] == repair["id"]
    assert reused["request_reused"] is True
    assert reused["outcome"] == "conflicted"

    (repair_worktree / "shared.txt").write_text(
        "main change\ncandidate conflict\n", encoding="utf-8"
    )
    committed = commit_task(
        repo,
        task_id=repair["id"],
        lease=repair["lease"],
        message="test: resolve candidate conflict",
        paths=["shared.txt"],
    )
    assert committed["supersedes"] == candidate["candidate_id"]
    ready(repo, task_id=repair["id"], lease=repair["lease"])
    replacement = finish(repo, task_id=repair["id"], lease=repair["lease"])

    assert replacement["outcome"] == "candidate_published"
    pool = CandidateBatchStore(repo)
    assert pool.candidate(candidate["candidate_id"])["status"] == "superseded"
    assert pool.candidate(replacement["candidate_id"])["repair_attempt"] == 1

    integrated = seal_batch(repo, candidate_ids=[replacement["candidate_id"]])
    assert integrated["status"] == "completed"
    assert (git_repo / "shared.txt").read_text(encoding="utf-8") == (
        "main change\ncandidate conflict\n"
    )


def test_candidate_repair_stops_after_bounded_attempts(git_repo: Path) -> None:
    repo = initialized_batched(git_repo)
    candidate = publish(repo, name="bounded conflict", relative="bounded.txt")
    (git_repo / "bounded.txt").write_text("main\n", encoding="utf-8")
    git(git_repo, "add", "bounded.txt")
    git(git_repo, "commit", "-m", "test: create bounded conflict")
    with pytest.raises(SoloAIError, match="conflicts with the sealed batch"):
        seal_batch(repo, candidate_ids=[candidate["candidate_id"]])

    store = CandidateBatchStore(repo)

    def exhaust_attempts(value: dict[str, object]) -> None:
        candidates = value["candidates"]
        assert isinstance(candidates, dict)
        source = candidates[candidate["candidate_id"]]
        assert isinstance(source, dict)
        source["repair_attempt"] = batch_module.AUTOMATIC_REPAIR_LIMIT

    store.mutate(exhaust_attempts)
    with pytest.raises(SoloAIError, match="manual product or implementation review"):
        prepare_candidate_repair(repo, candidate_id=candidate["candidate_id"])
