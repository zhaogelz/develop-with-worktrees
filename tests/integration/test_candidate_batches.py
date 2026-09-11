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
from solo_ai import cleanup as cleanup_module
from solo_ai import lifecycle as lifecycle_module
from solo_ai.cli import _status
from solo_ai.candidate_batches import (
    CandidateBatchStore,
    prepare_candidate_repair,
    reconcile_batches,
    retire_failed_batch,
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


def initialized_batched(
    path: Path, *, auto_full: bool = True, reusable: bool = False
) -> GitRepo:
    repo = GitRepo(path)
    initialize(repo, slots=3, commands=[VERIFY], accept=True, accept_static_only=False)
    config = path / ".solo-ai" / "config.toml"
    original = config.read_text(encoding="utf-8")
    contents = original
    if not reusable:
        # 这里保留历史专用目录的完整删除/恢复测试；复用行为有独立实际流程测试。
        contents = contents.replace(
            'worktree_mode = "reusable"', 'worktree_mode = "dedicated"'
        )
    if not auto_full:
        contents = contents.replace(
            'seal_policy = "auto_full"', 'seal_policy = "explicit"'
        )
    if contents != original:
        config.write_text(contents, encoding="utf-8")
        git(path, "add", ".solo-ai/config.toml")
        git(path, "commit", "-m", "test: choose the exercised batch compatibility mode")
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
    assert context["base_head"] == task["base_head"]
    assert "candidate_head" not in context
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


def test_failed_activation_can_become_a_restricted_adapter_repair(
    git_repo: Path,
) -> None:
    repo = initialized_batched(git_repo, auto_full=False)
    release_context = repo.local_dir / "adapter-repair-release.json"
    failing_activate = "raise SystemExit(1)"
    repaired_activate = "pass"
    release_script = (
        "from pathlib import Path; import json, sys; "
        "context=json.loads(Path(sys.argv[-1]).read_text(encoding='utf-8')); "
        "assert context['reason'] == 'runtime-adapter-repair'; "
        "assert context['repair_mode'] is True; "
        f"Path({str(release_context)!r}).write_text(json.dumps(context), encoding='utf-8')"
    )
    install_runtime_adapter(
        repo,
        activate_script=failing_activate,
        release_script=release_script,
        verify_script="pass",
    )

    with pytest.raises(SoloAIError, match="Runtime Adapter activate failed"):
        start(repo, name="repair broken adapter")
    pending = max(
        StateStore(repo).read()["tasks"].values(),
        key=lambda item: item["created_at"],
    )

    repaired = recover(
        repo,
        task_id=pending["id"],
        repair_runtime_adapter_paths=[".solo-ai/config.toml"],
    )
    worktree = Path(repaired["worktree"])
    config = worktree / ".solo-ai" / "config.toml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            json.dumps([sys.executable, "-c", failing_activate]),
            json.dumps([sys.executable, "-c", repaired_activate]),
        ),
        encoding="utf-8",
    )
    committed = commit_task(
        repo,
        task_id=repaired["id"],
        lease=repaired["lease"],
        message="test: repair runtime adapter",
        paths=[".solo-ai/config.toml"],
    )
    approve(
        repo,
        load_verification_config(repo, cwd=worktree),
        cwd=worktree,
    )
    ready(repo, task_id=repaired["id"], lease=repaired["lease"])

    finished = finish(repo, task_id=repaired["id"], lease=repaired["lease"])

    release = json.loads(release_context.read_text(encoding="utf-8"))
    assert committed["runtime_adapter_repair"]["release_required"] is True
    assert repaired["runtime_activation"]["skipped"] is True
    assert finished["status"] == "candidate-published"
    assert release["candidate_head"] == finished["candidate_head"]
    assert StateStore(repo).read()["slots"][repaired["slot_id"]]["status"] == "idle"


def test_adapter_repair_refuses_changes_outside_approved_inputs(git_repo: Path) -> None:
    repo = initialized_batched(git_repo, auto_full=False)
    install_runtime_adapter(
        repo,
        activate_script="raise SystemExit(1)",
        release_script="pass",
        verify_script="pass",
    )
    with pytest.raises(SoloAIError, match="Runtime Adapter activate failed"):
        start(repo, name="restrict adapter repair")
    pending = max(
        StateStore(repo).read()["tasks"].values(),
        key=lambda item: item["created_at"],
    )
    with pytest.raises(SoloAIError, match="tracked and covered"):
        recover(
            repo,
            task_id=pending["id"],
            repair_runtime_adapter_paths=["outside.txt"],
        )
    repaired = recover(
        repo,
        task_id=pending["id"],
        repair_runtime_adapter_paths=[".solo-ai/config.toml"],
    )
    (Path(repaired["worktree"]) / "outside.txt").write_text(
        "not an adapter input\n", encoding="utf-8"
    )

    with pytest.raises(SoloAIError, match="approved config and input_paths"):
        commit_task(
            repo,
            task_id=repaired["id"],
            lease=repaired["lease"],
            message="test: reject adapter repair scope escape",
            paths=["outside.txt"],
        )


def test_adapter_repair_requires_the_exact_failed_activation_receipt(
    git_repo: Path,
) -> None:
    repo = initialized_batched(git_repo, auto_full=False)
    install_runtime_adapter(
        repo,
        activate_script="raise SystemExit(1)",
        release_script="pass",
        verify_script="pass",
    )
    with pytest.raises(SoloAIError, match="Runtime Adapter activate failed"):
        start(repo, name="require failed activation receipt")
    pending = max(
        StateStore(repo).read()["tasks"].values(),
        key=lambda item: item["created_at"],
    )
    for receipt in (repo.local_dir / "runtime-adapter" / "receipts").glob("*.json"):
        receipt.unlink()

    with pytest.raises(SoloAIError, match="exact persisted failed activation receipt"):
        recover(
            repo,
            task_id=pending["id"],
            repair_runtime_adapter_paths=[".solo-ai/config.toml"],
        )

    assert StateStore(repo).task(pending["id"])["status"] == "starting"


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
    StateStore(repo).update_task(task["id"], anchor_origin=None)

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


def test_batch_cleanup_removes_known_recreatable_ignored_content(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (git_repo / ".gitignore").write_text(
        "node_modules/\ndist/\n.swc/\n*.tsbuildinfo\n", encoding="utf-8"
    )
    git(git_repo, "add", ".gitignore")
    git(git_repo, "commit", "-m", "test: ignore reproducible dependencies")
    repo = initialized_batched(git_repo, auto_full=False)
    candidate = publish(repo, name="ignored dependency", relative="candidate.txt")
    original_promote = batch_module._promote
    observed_worktree: Path | None = None

    def promote_with_dependency(repo, store, batch):
        nonlocal observed_worktree
        observed_worktree = Path(batch["worktree"])
        dependency = observed_worktree / "node_modules" / "package" / "index.js"
        dependency.parent.mkdir(parents=True)
        dependency.write_text("generated\n", encoding="utf-8")
        (observed_worktree / "dist").mkdir()
        (observed_worktree / "dist" / "bundle.js").write_text(
            "generated\n", encoding="utf-8"
        )
        (observed_worktree / ".swc").mkdir()
        (observed_worktree / ".swc" / "cache.bin").write_bytes(b"generated")
        (observed_worktree / "tsconfig.tsbuildinfo").write_text(
            "generated\n", encoding="utf-8"
        )
        return original_promote(repo, store, batch)

    monkeypatch.setattr(batch_module, "_promote", promote_with_dependency)
    completed = seal_batch(repo, candidate_ids=[candidate["candidate_id"]])

    assert completed["status"] == "completed"
    assert observed_worktree is not None
    assert not observed_worktree.exists()
    assert all(item.path != observed_worktree for item in repo.worktrees())


def test_finish_preserves_dependency_link_and_reuses_the_slot(
    git_repo: Path, directory_link
) -> None:
    (git_repo / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    git(git_repo, "add", ".gitignore")
    git(git_repo, "commit", "-m", "test: ignore generated dependencies")
    repo = initialized_batched(git_repo, auto_full=False)
    task = start(repo, name="linked dependency")
    worktree = Path(task["worktree"])
    target = git_repo / "README.md"
    link = worktree / "node_modules" / "local-package"
    directory_link(link, git_repo)
    before = target.read_bytes()
    (worktree / "candidate.txt").write_text("candidate\n", encoding="utf-8")
    commit_task(
        repo,
        task_id=task["id"],
        lease=task["lease"],
        message="test: candidate",
        paths=["candidate.txt"],
    )
    ready(repo, task_id=task["id"], lease=task["lease"])

    result = finish(repo, task_id=task["id"], lease=task["lease"])

    assert result["outcome"] == "candidate_published"
    assert link.lstat()
    assert target.read_bytes() == before
    # 占用其他两个空槽，第三个新任务必须复用带链接的原槽位。
    start(repo, name="other slot a")
    start(repo, name="other slot b")
    reused = start(repo, name="reuse generated dependencies")
    assert reused["worktree"] == task["worktree"]
    assert link.lstat()
    assert target.read_bytes() == before


def test_batch_cleanup_unlinks_dependency_without_touching_its_target(
    git_repo: Path, tmp_path: Path, directory_link, monkeypatch: pytest.MonkeyPatch
) -> None:
    (git_repo / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    git(git_repo, "add", ".gitignore")
    git(git_repo, "commit", "-m", "test: ignore generated dependencies")
    repo = initialized_batched(git_repo, auto_full=False)
    candidate = publish(repo, name="linked batch", relative="candidate.txt")
    target = tmp_path / "external-source"
    target.mkdir()
    marker = target / ".env.local"
    marker.write_text("must survive\n", encoding="utf-8")
    original_promote = batch_module._promote
    observed = []

    def promote_with_link(repo, store, batch):
        worktree = Path(batch["worktree"])
        directory_link(worktree / "node_modules" / "package", target)
        observed.append(worktree)
        return original_promote(repo, store, batch)

    monkeypatch.setattr(batch_module, "_promote", promote_with_link)
    result = seal_batch(repo, candidate_ids=[candidate["candidate_id"]])
    assert result["status"] == "completed"
    assert not observed[0].exists()
    assert marker.read_text(encoding="utf-8") == "must survive\n"


def test_frontend_output_roots_do_not_hide_protected_content(git_repo: Path) -> None:
    (git_repo / ".gitignore").write_text("dist/\n", encoding="utf-8")
    git(git_repo, "add", ".gitignore")
    git(git_repo, "commit", "-m", "test: ignore frontend output")
    repo = GitRepo(git_repo)
    protected = git_repo / "dist" / "storage" / "runtime.db"
    protected.parent.mkdir(parents=True)
    protected.write_text("must survive\n", encoding="utf-8")

    inventory = cleanup_module.inspect_untracked(repo, cwd=git_repo)

    assert inventory["protected"] == ["dist/storage/runtime.db"]
    assert inventory["retained"] == []


def test_batch_cleanup_treats_protected_names_inside_dependencies_as_recreatable(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (git_repo / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    git(git_repo, "add", ".gitignore")
    git(git_repo, "commit", "-m", "test: ignore reproducible dependencies")
    repo = initialized_batched(git_repo, auto_full=False)
    candidate = publish(repo, name="dependency storage types", relative="candidate.txt")
    original_promote = batch_module._promote
    observed_worktree: Path | None = None

    def promote_with_dependency_storage(repo, store, batch):
        nonlocal observed_worktree
        observed_worktree = Path(batch["worktree"])
        dependency = (
            observed_worktree
            / "node_modules"
            / "@vendor"
            / "package"
            / "types"
            / "storage"
            / "cache-manager.d.ts"
        )
        dependency.parent.mkdir(parents=True)
        dependency.write_text("export {};\n", encoding="utf-8")
        return original_promote(repo, store, batch)

    monkeypatch.setattr(batch_module, "_promote", promote_with_dependency_storage)
    completed = seal_batch(repo, candidate_ids=[candidate["candidate_id"]])

    assert completed["status"] == "completed"
    assert observed_worktree is not None
    assert not observed_worktree.exists()
    assert all(item.path != observed_worktree for item in repo.worktrees())


def test_protected_ignored_content_blocks_promotion_until_exact_recovery(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (git_repo / ".gitignore").write_text(".tmp/\n", encoding="utf-8")
    git(git_repo, "add", ".gitignore")
    git(git_repo, "commit", "-m", "test: ignore temporary output")
    repo = initialized_batched(git_repo, auto_full=False)
    candidate = publish(repo, name="protected output", relative="candidate.txt")
    base_before = repo.head(git_repo)
    original_promote = batch_module._promote
    protected_path: Path | None = None

    def promote_with_protected_output(repo, store, batch):
        nonlocal protected_path
        protected_path = Path(batch["worktree"]) / ".tmp" / "validation.db"
        protected_path.parent.mkdir(parents=True)
        protected_path.write_text("must survive\n", encoding="utf-8")
        return original_promote(repo, store, batch)

    monkeypatch.setattr(batch_module, "_promote", promote_with_protected_output)
    with pytest.raises(SoloAIError, match="blocks batch worktree cleanup"):
        seal_batch(repo, candidate_ids=[candidate["candidate_id"]])

    assert repo.head(git_repo) == base_before
    assert protected_path is not None and protected_path.is_file()
    batch = CandidateBatchStore(repo).summary()["batches"][0]
    assert batch["status"] == "validated"

    monkeypatch.setattr(batch_module, "_promote", original_promote)
    protected_path.unlink()
    protected_path.parent.rmdir()
    completed = batch_module.recover_batch(repo, batch_id=batch["id"])

    assert completed["status"] == "completed"
    assert repo.head(git_repo) == completed["integrated_head"]


def test_batch_cleanup_preserves_protected_content_arriving_during_cleanup(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (git_repo / ".gitignore").write_text("node_modules/\n.tmp/\n", encoding="utf-8")
    git(git_repo, "add", ".gitignore")
    git(git_repo, "commit", "-m", "test: ignore validation output")
    repo = initialized_batched(git_repo, auto_full=False)
    candidate = publish(repo, name="late protected output", relative="candidate.txt")
    original_promote = batch_module._promote
    original_delete = cleanup_module.delete_plain_path_if_unchanged
    observed_worktree: Path | None = None
    protected_path: Path | None = None

    def promote_with_dependency(repo, store, batch):
        nonlocal observed_worktree
        observed_worktree = Path(batch["worktree"])
        dependency = observed_worktree / "node_modules" / "package" / "index.js"
        dependency.parent.mkdir(parents=True)
        dependency.write_text("generated\n", encoding="utf-8")
        return original_promote(repo, store, batch)

    def add_protected_output_then_delete(path, expected):
        nonlocal protected_path
        if protected_path is None and "node_modules" in path.parts:
            assert observed_worktree is not None
            protected_path = observed_worktree / ".tmp" / "validation.db"
            protected_path.parent.mkdir(parents=True)
            protected_path.write_text("late and protected\n", encoding="utf-8")
        return original_delete(path, expected)

    monkeypatch.setattr(batch_module, "_promote", promote_with_dependency)
    monkeypatch.setattr(
        cleanup_module,
        "delete_plain_path_if_unchanged",
        add_protected_output_then_delete,
    )
    with pytest.raises(SoloAIError, match="pending exact recovery"):
        seal_batch(repo, candidate_ids=[candidate["candidate_id"]])

    assert protected_path is not None and protected_path.is_file()
    batch = CandidateBatchStore(repo).summary()["batches"][0]
    assert batch["status"] == "promoted"

    monkeypatch.setattr(
        cleanup_module, "delete_plain_path_if_unchanged", original_delete
    )
    protected_path.unlink()
    protected_path.parent.rmdir()
    completed = batch_module.recover_batch(repo, batch_id=batch["id"])

    assert completed["status"] == "completed"
    assert observed_worktree is not None and not observed_worktree.exists()


def test_git_registration_removal_never_receives_a_populated_worktree(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Git非强制remove仍会删忽略文件，DWW只能把已消失目录的登记交给它。"""
    repo = initialized_batched(git_repo, auto_full=False)
    candidate = publish(
        repo, name="exact physical retirement", relative="candidate.txt"
    )
    command = repo.git
    observed = []

    def checked_command(args, **kwargs):
        if args[:2] == ["worktree", "remove"]:
            worktree = Path(args[2])
            observed.append(worktree)
            assert not worktree.exists(), "仍有内容的工作树不得交给Git递归删除"
        return command(args, **kwargs)

    monkeypatch.setattr(repo, "git", checked_command)
    result = seal_batch(repo, candidate_ids=[candidate["candidate_id"]])
    assert result["status"] == "completed"
    assert len(observed) == 1
    assert result["worktree_removal_manifest_sha256"]
    assert repo.ref_head(result["integration_ref"]) == result["integration_head"]


@pytest.mark.parametrize("interruption", ["exception", "late-file", "changed-file"])
def test_exact_retirement_recovers_partial_source_removal_without_full(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, interruption: str
) -> None:
    from solo_ai import worktree_retirement as retirement

    repo = initialized_batched(git_repo, auto_full=False)
    candidate = publish(repo, name="partial source removal", relative="candidate.txt")
    delete = retirement.delete_plain_path_if_unchanged
    affected = []

    def interrupt_delete(path, expected):
        if path.name == "candidate.txt" and not affected:
            affected.append(path)
            if interruption == "exception":
                delete(path, expected)
                raise KeyboardInterrupt("interrupted after exact source deletion")
            if interruption == "late-file":
                late = path.parent / ".tmp" / "late.db"
                late.parent.mkdir()
                late.write_bytes(b"preserve late data")
            else:
                # 实际写入改变必须保留，不得在恢复时重新认领成可删内容。
                path.write_bytes(b"changed source")
        return delete(path, expected)

    monkeypatch.setattr(retirement, "delete_plain_path_if_unchanged", interrupt_delete)
    error = KeyboardInterrupt if interruption == "exception" else SoloAIError
    with pytest.raises(error):
        seal_batch(repo, candidate_ids=[candidate["candidate_id"]])
    batch = CandidateBatchStore(repo).summary()["batches"][0]
    assert batch["status"] == "promoted"
    assert batch["worktree_removal_manifest_sha256"]
    assert repo.head(git_repo) == batch["integration_head"]
    assert repo.ref_head(batch["integration_ref"]) == batch["integration_head"]
    assert affected
    worktree = affected[0].parent

    def no_validation(*args, **kwargs):
        pytest.fail("物理清理恢复不得重跑已通过的Full")

    monkeypatch.setattr(batch_module, "validate", no_validation)
    monkeypatch.setattr(retirement, "delete_plain_path_if_unchanged", delete)
    if interruption == "late-file":
        late = worktree / ".tmp" / "late.db"
        assert late.read_bytes() == b"preserve late data"
        with pytest.raises(SoloAIError, match="pending exact recovery"):
            batch_module.recover_batch(repo, batch_id=batch["id"])
        assert late.read_bytes() == b"preserve late data"
        # 模拟人工保留到夹具外，再从原冻结清单恢复；不删除晚到数据。
        late.rename(git_repo / "preserved-late.db")
        late.parent.rmdir()
    elif interruption == "changed-file":
        with pytest.raises(SoloAIError, match="pending exact recovery"):
            batch_module.recover_batch(repo, batch_id=batch["id"])
        assert affected[0].read_bytes() == b"changed source"
        affected[0].rename(git_repo / "preserved-changed-source.txt")
    result = batch_module.recover_batch(repo, batch_id=batch["id"])
    assert result["status"] == "completed"
    assert not worktree.exists()
    assert all(item.path != worktree for item in repo.worktrees())


@pytest.mark.parametrize("drift", ["manifest", "control", "registration"])
def test_retirement_recovery_rejects_changed_receipt_or_git_facts(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    from solo_ai import worktree_retirement as retirement

    repo = initialized_batched(git_repo, auto_full=False)
    candidate = publish(
        repo, name="retirement evidence drift", relative="candidate.txt"
    )
    apply = retirement._apply

    def interrupt_before_deleting(*args):
        raise KeyboardInterrupt("inventory saved before deletion")

    monkeypatch.setattr(retirement, "_apply", interrupt_before_deleting)
    with pytest.raises(KeyboardInterrupt, match="inventory saved"):
        seal_batch(repo, candidate_ids=[candidate["candidate_id"]])
    batch = CandidateBatchStore(repo).summary()["batches"][0]
    worktree = Path(batch["worktree"])
    source = worktree / "candidate.txt"
    before = source.read_bytes()
    manifest = retirement._manifest_path(repo, batch)
    if drift == "manifest":
        manifest.write_bytes(manifest.read_bytes() + b" ")
    elif drift == "control":
        # 语义相同但对象内容已变的控制指针，也不能在恢复时自动认领。
        control = worktree / ".git"
        with control.open("r+b") as pointer:
            pointer.seek(0, 2)
            pointer.write(b"\n")
    else:
        repo.git(["checkout", "-b", "changed-owner"], cwd=worktree)
    monkeypatch.setattr(retirement, "_apply", apply)
    with pytest.raises(SoloAIError, match="pending exact recovery"):
        batch_module.recover_batch(repo, batch_id=batch["id"])
    assert source.read_bytes() == before
    assert repo.ref_head(batch["integration_ref"]) == batch["integration_head"]


def test_late_recreated_path_is_preserved_by_registration_removal(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized_batched(git_repo, auto_full=False)
    candidate = publish(repo, name="late root replacement", relative="candidate.txt")
    command = repo.git
    observed = []

    def recreate_before_command(args, **kwargs):
        if args[:2] == ["worktree", "remove"] and not observed:
            worktree = Path(args[2])
            assert not worktree.exists()
            worktree.mkdir()
            marker = worktree / "late.db"
            marker.write_bytes(b"new unknown data")
            observed.append(marker)
        return command(args, **kwargs)

    monkeypatch.setattr(repo, "git", recreate_before_command)
    with pytest.raises(SoloAIError, match="pending exact recovery"):
        seal_batch(repo, candidate_ids=[candidate["candidate_id"]])
    assert observed and observed[0].read_bytes() == b"new unknown data"
    batch = CandidateBatchStore(repo).summary()["batches"][0]
    assert batch["status"] == "promoted"
    assert repo.head(git_repo) == batch["integration_head"]
    monkeypatch.setattr(repo, "git", command)
    with pytest.raises(SoloAIError, match="pending exact recovery"):
        batch_module.recover_batch(repo, batch_id=batch["id"])
    assert observed[0].read_bytes() == b"new unknown data"


@pytest.mark.dww_stress
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
    base_head = candidate["reconciliation"]["waiting"][0]["base_head"]
    snapshot = StateStore(repo).candidate_producer_snapshot(
        base_ref="main", base_head=base_head, activation_epoch=policy
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


def test_reconcile_does_not_mix_candidates_from_different_frozen_bases(
    git_repo: Path,
) -> None:
    repo = initialized_batched(git_repo, auto_full=False)
    earlier = publish(repo, name="earlier baseline", relative="earlier.txt")
    earlier_record = CandidateBatchStore(repo).candidate(earlier["candidate_id"])
    (git_repo / "advance.txt").write_text("advance\n", encoding="utf-8")
    git(git_repo, "add", "advance.txt")
    git(git_repo, "commit", "-m", "test: advance base before later candidate")
    later = publish(repo, name="later baseline", relative="later.txt")
    later_record = CandidateBatchStore(repo).candidate(later["candidate_id"])

    assert earlier_record["base_head"] != later_record["base_head"]
    store = CandidateBatchStore(repo)
    lanes = store.pending_lanes()
    assert [lane["candidate_ids"] for lane in lanes] == [
        [earlier["candidate_id"]],
        [later["candidate_id"]],
    ]
    snapshots = {
        (lane["base_ref"], lane["base_head"], lane["activation_epoch"]): {
            "active_count": 0,
            "active_task_ids": [],
            "quiet_since": "2000-01-01T00:00:00Z",
        }
        for lane in lanes
    }

    sealed = store.reconcile(
        producer_snapshots=snapshots,
        cause="heartbeat",
        now_epoch=time.time(),
    )

    assert sealed["batch"]["candidate_ids"] == [earlier["candidate_id"]]
    assert store.candidate(later["candidate_id"])["status"] == "pending"


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


@pytest.mark.dww_stress
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
    base_head = candidate["reconciliation"]["waiting"][0]["base_head"]
    snapshot = StateStore(repo).candidate_producer_snapshot(
        base_ref="main", base_head=base_head, activation_epoch=policy
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


def test_status_projects_active_full_batch_and_abandon_refuses_its_candidate(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized_batched(git_repo, auto_full=False)
    task = start(repo, name="batch ownership card")
    head = repo.head(Path(task["worktree"]))
    policy = StateStore.integration_policy(load_repo_config(repo))
    store = CandidateBatchStore(repo)
    candidate_ids: list[str] = []
    owned_candidate_id = "candidate-owned-by-batch"

    for index in range(5):
        candidate_id = owned_candidate_id if index == 0 else f"candidate-peer-{index}"
        task_id = task["id"] if index == 0 else f"task-peer-{index}"
        store.publish(
            {
                "candidate_id": candidate_id,
                "task_id": task_id,
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
            seal_policy="explicit",
        )
        candidate_ids.append(candidate_id)

    batch = store.seal(candidate_ids, batch_size=5)
    entered = threading.Event()
    release = threading.Event()

    def pause_batch(
        _repo: GitRepo, _store: CandidateBatchStore, current: dict[str, object]
    ) -> dict[str, object]:
        entered.set()
        assert release.wait(timeout=10)
        return current

    monkeypatch.setattr(batch_module, "_resume", pause_batch)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(batch_module.run_batch, repo, batch_id=batch["id"])
        try:
            assert entered.wait(timeout=10)
            report = _status(repo, detailed=False)
            card = next(item for item in report["tasks"] if item["id"] == task["id"])
            ownership = card["batch_ownership"]
            assert ownership["state"] == "active_full_batch"
            assert ownership["candidate"] == {
                "id": owned_candidate_id,
                "status": "sealed",
            }
            assert ownership["batch"]["id"] == batch["id"]
            assert ownership["batch"]["kind"] == "full"
            assert ownership["batch"]["phase"] == "sealed"
            assert ownership["batch"]["process"]["live"] is True
        finally:
            release.set()
        assert future.result(timeout=10)["id"] == batch["id"]

    def fail_if_abandon_stops_processes(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Abandon must reject a batch-held candidate before side effects")

    monkeypatch.setattr(
        lifecycle_module, "_stop_registered_processes", fail_if_abandon_stops_processes
    )
    with pytest.raises(SoloAIError, match="held by active full batch"):
        abandon(repo, task_id=task["id"], lease=task["lease"], confirm=task["id"])
    assert StateStore(repo).task(task["id"])["status"] == "active"


@pytest.mark.dww_stress
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


@pytest.mark.dww_stress
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


@pytest.mark.dww_stress
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


def test_reviewed_reseal_of_exact_failed_generation_is_new_and_idempotent(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized_batched(git_repo, auto_full=False)
    candidate = publish(repo, name="reviewed reseal", relative="reseal.txt")
    base_before = repo.head(git_repo)
    original_validate = batch_module.validate

    monkeypatch.setattr(
        batch_module,
        "validate",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            SoloAIError("reviewed external failure")
        ),
    )
    with pytest.raises(SoloAIError, match="reviewed external failure"):
        seal_batch(repo, candidate_ids=[candidate["candidate_id"]])

    failed = CandidateBatchStore(repo).summary()["batches"][0]
    monkeypatch.setattr(batch_module, "validate", original_validate)
    completed = seal_batch(
        repo,
        candidate_ids=[candidate["candidate_id"]],
        after_failed_batch_id=failed["id"],
    )
    repeated = seal_batch(
        repo,
        candidate_ids=[candidate["candidate_id"]],
        after_failed_batch_id=failed["id"],
    )

    assert failed["status"] == "failed"
    assert completed["status"] == "completed"
    assert completed["id"] != failed["id"]
    assert completed["after_failed_batch"] == failed["id"]
    assert repeated["id"] == completed["id"]
    assert repo.head(git_repo) != base_before


def test_reviewed_reseal_requires_exact_failed_predecessor_and_candidates(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized_batched(git_repo, auto_full=False)
    first = publish(repo, name="first reseal candidate", relative="first.txt")
    second = publish(repo, name="second reseal candidate", relative="second.txt")
    monkeypatch.setattr(
        batch_module,
        "validate",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            SoloAIError("reviewed external failure")
        ),
    )
    with pytest.raises(SoloAIError, match="reviewed external failure"):
        seal_batch(repo, candidate_ids=[first["candidate_id"]])
    failed = CandidateBatchStore(repo).summary()["batches"][0]

    with pytest.raises(SoloAIError, match="Unknown previous failed batch"):
        CandidateBatchStore(repo).seal(
            [second["candidate_id"]],
            batch_size=5,
            after_failed_batch_id="batch-missing",
        )
    with pytest.raises(SoloAIError, match="exact ordered candidates"):
        CandidateBatchStore(repo).seal(
            [second["candidate_id"]],
            batch_size=5,
            after_failed_batch_id=failed["id"],
        )


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


def test_failed_batch_retirement_is_exact_idempotent_and_preserves_candidate(
    git_repo: Path, tmp_path: Path, directory_link, monkeypatch: pytest.MonkeyPatch
) -> None:
    (git_repo / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    git(git_repo, "add", ".gitignore")
    git(git_repo, "commit", "-m", "test: ignore generated dependencies")
    repo = initialized_batched(git_repo, auto_full=False)
    candidate = publish(repo, name="retire failed", relative="retire.txt")

    def fail_validation(*args: object, **kwargs: object) -> dict[str, object]:
        raise SoloAIError("synthetic retirement failure")

    monkeypatch.setattr(batch_module, "validate", fail_validation)
    with pytest.raises(SoloAIError, match="synthetic retirement failure"):
        seal_batch(repo, candidate_ids=[candidate["candidate_id"]])
    store = CandidateBatchStore(repo)
    failed = store.summary()["batches"][0]
    worktree = Path(failed["worktree"])
    candidate_record = store.candidate(candidate["candidate_id"])
    target = tmp_path / "retained-source"
    target.mkdir()
    marker = target / "source.txt"
    marker.write_text("preserved", encoding="utf-8")
    directory_link(worktree / "node_modules" / "package", target)

    original_inventory = cleanup_module._ignored_inventory
    inventory_runs = 0

    def count_inventory(*args, **kwargs):
        nonlocal inventory_runs
        if kwargs.get("expand_dependencies"):
            inventory_runs += 1
        return original_inventory(*args, **kwargs)

    monkeypatch.setattr(cleanup_module, "_ignored_inventory", count_inventory)
    retired = retire_failed_batch(repo, batch_id=failed["id"])
    repeated = retire_failed_batch(repo, batch_id=failed["id"])

    # 只有一次完整依赖清点；源文件末检不可再次展开依赖。
    assert inventory_runs == 1
    assert retired["status"] == "failed"
    assert retired["worktree_retirement_started_at"]
    assert retired["worktree_retired_at"]
    assert repeated["worktree_retired_at"] == retired["worktree_retired_at"]
    assert not worktree.exists()
    assert marker.read_text(encoding="utf-8") == "preserved"
    assert all(item.path != worktree for item in repo.worktrees())
    assert repo.ref_head(candidate_record["ref"]) == candidate_record["head"]
    assert (
        CandidateBatchStore(repo).candidate(candidate["candidate_id"])["status"]
        == "retained"
    )


def _failed_superseded_batch(
    repo: GitRepo,
    monkeypatch: pytest.MonkeyPatch,
    *,
    relative: str,
) -> tuple[CandidateBatchStore, dict[str, object], dict[str, str]]:
    """构造快速退役专用的失败批次；候选已由后续结果替代。"""
    candidate = publish(repo, name="fast retirement fixture", relative=relative)

    def fail_validation(*args: object, **kwargs: object) -> dict[str, object]:
        raise SoloAIError("synthetic fast-retirement failure")

    monkeypatch.setattr(batch_module, "validate", fail_validation)
    with pytest.raises(SoloAIError, match="synthetic fast-retirement failure"):
        seal_batch(repo, candidate_ids=[candidate["candidate_id"]])
    store = CandidateBatchStore(repo)

    def supersede(value: dict[str, object]) -> None:
        record = value["candidates"][candidate["candidate_id"]]
        record.update(
            {
                "status": "superseded",
                "superseded_by": "candidate-replacement-fixture",
                "repair_eligible": False,
            }
        )

    store.mutate(supersede)
    return store, store.summary()["batches"][0], candidate


def test_fast_failed_batch_retirement_skips_dependency_hashes_and_is_idempotent(
    git_repo: Path,
    tmp_path: Path,
    directory_link,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (git_repo / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    git(git_repo, "add", ".gitignore")
    git(git_repo, "commit", "-m", "test: ignore fast retirement dependencies")
    repo = initialized_batched(git_repo, auto_full=False)
    store, failed, candidate = _failed_superseded_batch(
        repo, monkeypatch, relative="fast-retire.txt"
    )
    worktree = Path(failed["worktree"])
    target = tmp_path / "fast-retained-target"
    target.mkdir()
    marker = target / "source.txt"
    marker.write_text("preserved", encoding="utf-8")
    directory_link(worktree / "node_modules" / "package", target)
    (worktree / "node_modules" / "large-generated.bin").write_bytes(b"generated")

    def no_slow_inventory(*args: object, **kwargs: object) -> None:
        pytest.fail("fast retirement must not expand the exact dependency inventory")

    monkeypatch.setattr(cleanup_module, "_ignored_inventory", no_slow_inventory)
    retired = retire_failed_batch(repo, batch_id=str(failed["id"]), fast=True)
    repeated = retire_failed_batch(repo, batch_id=str(failed["id"]), fast=True)

    assert retired["status"] == "failed"
    assert retired["fast_retirement_receipt_sha256"]
    assert retired["worktree_retired_at"]
    assert repeated["worktree_retired_at"] == retired["worktree_retired_at"]
    assert not worktree.exists()
    assert marker.read_text(encoding="utf-8") == "preserved"
    assert all(item.path != worktree for item in repo.worktrees())
    assert (
        repo.ref_head(store.candidate(candidate["candidate_id"])["ref"])
        == candidate["candidate_head"]
    )
    assert store.candidate(candidate["candidate_id"])["status"] == "superseded"


def test_fast_failed_batch_retirement_accepts_nested_opaque_dependencies(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (git_repo / ".gitignore").write_text(
        "components/api/.venv/\nweb/node_modules/\n",
        encoding="utf-8",
    )
    git(git_repo, "add", ".gitignore")
    git(git_repo, "commit", "-m", "test: accept nested fast dependencies")
    repo = initialized_batched(git_repo, auto_full=False)
    _, failed, _ = _failed_superseded_batch(
        repo, monkeypatch, relative="nested-fast-retire.txt"
    )
    worktree = Path(failed["worktree"])
    venv = worktree / "components" / "api" / ".venv"
    node_modules = worktree / "web" / "node_modules"
    venv.mkdir(parents=True)
    node_modules.mkdir(parents=True)
    (venv / "marker.bin").write_bytes(b"generated")
    (node_modules / "marker.bin").write_bytes(b"generated")

    def no_slow_inventory(*args: object, **kwargs: object) -> None:
        pytest.fail("fast retirement must not expand nested dependency inventory")

    monkeypatch.setattr(cleanup_module, "_ignored_inventory", no_slow_inventory)
    retired = retire_failed_batch(repo, batch_id=str(failed["id"]), fast=True)

    assert retired["worktree_retired_at"]
    assert retired["fast_retirement_receipt_sha256"]
    assert not worktree.exists()


def test_fast_failed_batch_retirement_rejects_protected_ignored_content(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (git_repo / ".gitignore").write_text(".tmp/\n", encoding="utf-8")
    git(git_repo, "add", ".gitignore")
    git(git_repo, "commit", "-m", "test: protect fast retirement data")
    repo = initialized_batched(git_repo, auto_full=False)
    _, failed, _ = _failed_superseded_batch(
        repo, monkeypatch, relative="fast-protected.txt"
    )
    worktree = Path(failed["worktree"])
    protected = worktree / ".tmp" / "state.db"
    protected.parent.mkdir()
    protected.write_bytes(b"do not remove")

    with pytest.raises(SoloAIError, match="Protected content"):
        retire_failed_batch(repo, batch_id=str(failed["id"]), fast=True)

    assert protected.read_bytes() == b"do not remove"
    assert worktree.exists()
    assert any(item.path == worktree for item in repo.worktrees())


def test_fast_failed_batch_retirement_resumes_after_staging_interruption(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from solo_ai import worktree_retirement as retirement

    repo = initialized_batched(git_repo, auto_full=False)
    store, failed, _ = _failed_superseded_batch(
        repo, monkeypatch, relative="fast-resume.txt"
    )
    worktree = Path(failed["worktree"])
    stage = worktree.parent / f".dww-fast-retire-{failed['id']}"
    original_remove = retirement._fast_remove_tree

    def interrupt(path: Path) -> None:
        raise KeyboardInterrupt("interrupted after staging")

    monkeypatch.setattr(retirement, "_fast_remove_tree", interrupt)
    with pytest.raises(KeyboardInterrupt, match="interrupted after staging"):
        retire_failed_batch(repo, batch_id=str(failed["id"]), fast=True)

    pending = store.batch(str(failed["id"]))
    assert pending["fast_retirement_started_at"]
    assert pending["fast_retirement_staging"] == str(stage)
    assert stage.exists() and not worktree.exists()

    monkeypatch.setattr(retirement, "_fast_remove_tree", original_remove)
    completed = retire_failed_batch(repo, batch_id=str(failed["id"]), fast=True)
    assert completed["worktree_retired_at"]
    assert not stage.exists()
    assert not worktree.exists()


def test_failed_batch_retirement_recovers_after_removal_before_final_projection(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized_batched(git_repo, auto_full=False)
    candidate = publish(repo, name="resume retirement", relative="resume-retire.txt")

    def fail_validation(*args: object, **kwargs: object) -> dict[str, object]:
        raise SoloAIError("synthetic interrupted retirement")

    monkeypatch.setattr(batch_module, "validate", fail_validation)
    with pytest.raises(SoloAIError, match="synthetic interrupted retirement"):
        seal_batch(repo, candidate_ids=[candidate["candidate_id"]])
    store = CandidateBatchStore(repo)
    failed = store.summary()["batches"][0]
    worktree = Path(failed["worktree"])
    store.update_batch(
        failed["id"], worktree_retirement_started_at="2026-09-05T00:00:00Z"
    )
    repo.git(["worktree", "remove", str(worktree)])

    retired = retire_failed_batch(repo, batch_id=failed["id"])

    assert retired["worktree_retired_at"]
    assert not worktree.exists()


def test_failed_batch_retirement_preserves_unknown_ignored_output(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (git_repo / ".gitignore").write_text("output/\n", encoding="utf-8")
    git(git_repo, "add", ".gitignore")
    git(git_repo, "commit", "-m", "test: ignore unknown output")
    repo = initialized_batched(git_repo, auto_full=False)
    candidate = publish(repo, name="unknown output", relative="unknown.txt")

    def fail_validation(*args: object, **kwargs: object) -> dict[str, object]:
        raise SoloAIError("synthetic unknown-output failure")

    monkeypatch.setattr(batch_module, "validate", fail_validation)
    with pytest.raises(SoloAIError, match="synthetic unknown-output failure"):
        seal_batch(repo, candidate_ids=[candidate["candidate_id"]])
    failed = CandidateBatchStore(repo).summary()["batches"][0]
    worktree = Path(failed["worktree"])
    unknown = worktree / "output" / "result.bin"
    unknown.parent.mkdir(parents=True)
    unknown.write_bytes(b"preserve")

    with pytest.raises(SoloAIError, match="unknown content"):
        retire_failed_batch(repo, batch_id=failed["id"])

    assert unknown.read_bytes() == b"preserve"
    assert any(item.path == worktree for item in repo.worktrees())


def test_batch_retirement_rejects_a_nonfailed_generation(git_repo: Path) -> None:
    repo = initialized_batched(git_repo, auto_full=False)
    candidate = publish(repo, name="active generation", relative="active.txt")
    batch = CandidateBatchStore(repo).seal([candidate["candidate_id"]], batch_size=5)

    with pytest.raises(SoloAIError, match="Only a failed integration batch"):
        retire_failed_batch(repo, batch_id=batch["id"])

    assert batch["status"] == "sealed"


def test_batch_metrics_are_derived_without_mutating_lifecycle_state(
    git_repo: Path,
) -> None:
    repo = GitRepo(git_repo)
    store = CandidateBatchStore(repo)
    atomic_write_json(
        store.path,
        {
            "schema_version": 3,
            "next_publication_sequence": 4,
            "updated_at": "2026-09-05T00:00:00Z",
            "candidates": {
                "candidate-a": {
                    "candidate_id": "candidate-a",
                    "status": "integrated",
                    "published_at": "2026-09-05T00:00:00Z",
                    "integrated_at": "2026-09-05T00:02:00Z",
                    "integration_policy": {"batch_size": 2},
                },
                "candidate-b": {
                    "candidate_id": "candidate-b",
                    "status": "integrated",
                    "published_at": "2026-09-05T00:01:00Z",
                    "integrated_at": "2026-09-05T00:04:00Z",
                    "integration_policy": {"batch_size": 2},
                },
                "candidate-c": {
                    "candidate_id": "candidate-c",
                    "status": "retained",
                    "published_at": "2026-09-05T00:05:00Z",
                    "integration_policy": {"batch_size": 2},
                },
            },
            "batches": {
                "batch-full": {
                    "id": "batch-full",
                    "status": "completed",
                    "candidate_ids": ["candidate-a", "candidate-b"],
                    "integration_policy": {"batch_size": 2},
                    "proof": "aggregate-proof",
                },
                "batch-tail": {
                    "id": "batch-tail",
                    "status": "failed",
                    "candidate_ids": ["candidate-c"],
                    "integration_policy": {"batch_size": 2},
                },
            },
        },
    )
    atomic_write_json(
        repo.local_dir / "proofs" / "aggregate-proof.json",
        {
            "profile_proofs": [
                {
                    "fingerprint": "full-proof",
                    "profile_id": "full",
                    "reused": False,
                }
            ]
        },
    )
    atomic_write_json(
        repo.local_dir / "profile-proofs" / "full-proof.json",
        {
            "inputs": {"level": "full"},
            "runs": [{"duration_seconds": 12.5}],
        },
    )
    before = store.path.read_bytes()

    metrics = store.metrics()

    assert metrics["terminal_batches"] == 2
    assert metrics["completed_batches"] == 1
    assert metrics["failed_batches"] == 1
    assert metrics["full_batch_rate"] == 0.5
    assert metrics["tail_batch_rate"] == 0.5
    assert metrics["candidate_wait_seconds"] == {
        "count": 2,
        "minimum": 120.0,
        "median": 150.0,
        "p95": 180.0,
        "maximum": 180.0,
        "mean": 150.0,
    }
    assert metrics["executed_full_validation_seconds"]["median"] == 12.5
    assert metrics["reused_full_profiles"] == 0
    assert metrics["missing_full_proofs"] == 0
    assert store.path.read_bytes() == before


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


def test_new_runtime_cycle_reuses_pure_check_but_repeats_external_check(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真实批次恢复不能把上次环境的分项成功当作新环境已通过。"""
    from solo_ai import proof as proof_module
    from solo_ai import validation_queue

    monkeypatch.setattr(
        validation_queue, "_machine_root", lambda: git_repo.parent / "machine"
    )
    repo = initialized_batched(git_repo, auto_full=False)
    install_runtime_adapter(
        repo,
        release_script="pass",
        verify_script="pass",
        batch_activate_script="pass",
        batch_release_script="pass",
    )
    policy = repo.root / ".solo-ai/verification.toml"
    text = policy.read_text(encoding="utf-8")
    for name, external in (("pure", "none"), ("external", "unknown"), ("last", "none")):
        counter = repo.local_dir / f"{name}-executions.txt"
        command = [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; "
                f"p=Path({str(counter)!r}); "
                "n=int(p.read_text()) if p.exists() else 0; p.write_text(str(n+1))"
            ),
        ]
        text += f'''
[[profiles]]
id = "{name}"
level = "full"
paths = ["**"]
input_paths = ["**"]
input_closure = "complete"
external_state = "{external}"
commands = [{json.dumps(command)}]
'''
    policy.write_text(text, encoding="utf-8")
    git(repo.root, "add", ".solo-ai/verification.toml")
    git(repo.root, "commit", "-m", "test: declare independent validation checks")
    approve(repo, load_verification_config(repo))
    candidate = publish(repo, name="partial Full", relative="change.txt")
    original_run = proof_module._run_profile
    interrupted = False

    def interrupt_last(*args, **kwargs):
        nonlocal interrupted
        if kwargs["profile"].profile_id == "last" and not interrupted:
            interrupted = True
            raise KeyboardInterrupt("stop after external check")
        return original_run(*args, **kwargs)

    monkeypatch.setattr(proof_module, "_run_profile", interrupt_last)
    with pytest.raises(KeyboardInterrupt, match="stop after external"):
        seal_batch(repo, candidate_ids=[candidate["candidate_id"]])
    batch = CandidateBatchStore(repo).summary()["batches"][0]
    completed = batch_module.recover_batch(repo, batch_id=batch["id"])
    assert completed["runtime_cycle"] == 2
    assert (repo.local_dir / "pure-executions.txt").read_text() == "1"
    assert (repo.local_dir / "external-executions.txt").read_text() == "2"
    assert (repo.local_dir / "last-executions.txt").read_text() == "1"


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


@pytest.mark.parametrize("reusable", [False, True])
def test_composition_conflict_prepares_bounded_repair_and_replacement_candidate(
    git_repo: Path,
    reusable: bool,
) -> None:
    repo = initialized_batched(git_repo, reusable=reusable)
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

    if reusable:
        pool = CandidateBatchStore(repo).read()
        failed = pool["batches"][source["last_failed_batch"]]
        assert pool["integration_workspace"]["owner"] is None
        assert failed["worktree_released_at"]
        assert repo.is_clean(Path(failed["worktree"]))
        assert repo.ref_head(failed["integration_ref"]) == failed["integration_head"]

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
