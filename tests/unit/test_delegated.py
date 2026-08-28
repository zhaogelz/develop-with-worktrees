from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
from conftest import declare_delegated_adapter, git
from solo_ai import delegated
from solo_ai.delegated import (
    ALLOWED_CAPABILITIES,
    DelegatedContractError,
    approve_delegated,
    inspect_delegated,
    invoke_delegated,
    revoke_delegated,
)
from solo_ai.lifecycle import repository_route
from solo_ai.repo import GitRepo


def declare_adapter(root: Path, *, max_parallel: int = 4) -> None:
    declare_delegated_adapter(
        root,
        max_parallel=max_parallel,
        capabilities=("start", "status"),
        approve=False,
    )


def test_exact_local_approval_enables_delegated_route_and_limits_slots(
    git_repo: Path,
) -> None:
    declare_adapter(git_repo, max_parallel=3)
    repo = GitRepo(git_repo)

    inspection = inspect_delegated(repo.root, repo.common_dir)
    assert inspection["valid"] is True
    assert inspection["approved"] is False
    assert repository_route(repo)["reason"] == "delegated-approval-required"

    approved = approve_delegated(
        repo.root,
        repo.common_dir,
        fingerprint=inspection["adapter"]["fingerprint"],
    )

    assert approved["approved"] is True
    route = repository_route(repo)
    assert route["action"] == "delegated"
    assert route["adapter"]["id"] == "example-worktree-flow"
    assert route["adapter"]["max_parallel"] == 3


def test_tracked_input_drift_invalidates_approval_without_executing(
    git_repo: Path,
) -> None:
    declare_adapter(git_repo)
    repo = GitRepo(git_repo)
    before = inspect_delegated(repo.root, repo.common_dir)
    approve_delegated(
        repo.root,
        repo.common_dir,
        fingerprint=before["adapter"]["fingerprint"],
    )

    (git_repo / "scripts" / "worktree-flow.ps1").write_text(
        "# changed lifecycle\n", encoding="utf-8"
    )

    after = inspect_delegated(repo.root, repo.common_dir)
    assert after["valid"] is True
    assert after["approved"] is False
    assert after["adapter"]["fingerprint"] != before["adapter"]["fingerprint"]
    assert repository_route(repo)["action"] == "defer"
    assert repository_route(repo)["reason"] == "delegated-approval-required"


def test_marker_set_drift_fails_closed(git_repo: Path) -> None:
    declare_adapter(git_repo)
    repo = GitRepo(git_repo)
    (git_repo / ".sdd").mkdir()

    inspection = inspect_delegated(repo.root, repo.common_dir)

    assert inspection["valid"] is False
    assert "exactly match" in inspection["error"]
    route = repository_route(repo)
    assert route["action"] == "defer"
    assert route["reason"] == "delegated-invalid-contract"


def test_invoke_uses_approved_json_envelope(git_repo: Path) -> None:
    declare_adapter(git_repo)
    repo = GitRepo(git_repo)
    inspection = inspect_delegated(repo.root, repo.common_dir)
    approve_delegated(
        repo.root,
        repo.common_dir,
        fingerprint=inspection["adapter"]["fingerprint"],
    )

    response = invoke_delegated(
        repo.root,
        repo.common_dir,
        operation="status",
        request={},
        timeout_seconds=30,
    )

    assert response["ok"] is True
    assert response["result"] == {"available_slots": 2}


def test_invoke_never_executes_entrypoint_drift_after_final_approval_check(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    declare_adapter(git_repo)
    repo = GitRepo(git_repo)
    inspection = inspect_delegated(repo.root, repo.common_dir)
    approve_delegated(
        repo.root,
        repo.common_dir,
        fingerprint=inspection["adapter"]["fingerprint"],
    )
    adapter_path = git_repo / "scripts" / "dww_adapter.py"
    approved_source = adapter_path.read_text(encoding="utf-8")
    marker = git_repo / "unapproved-adapter-ran.txt"
    original_run = delegated._run_adapter_process

    def drift_then_spawn(*args: object, **kwargs: object) -> object:
        adapter_path.write_text(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('ran', encoding='utf-8')\n"
            + approved_source,
            encoding="utf-8",
        )
        return original_run(*args, **kwargs)

    monkeypatch.setattr(delegated, "_run_adapter_process", drift_then_spawn)

    response = invoke_delegated(
        repo.root,
        repo.common_dir,
        operation="status",
        request={},
        timeout_seconds=30,
    )

    assert response["ok"] is True
    assert response["result"] == {"available_slots": 2}
    assert adapter_path.read_text(encoding="utf-8") != approved_source
    assert not marker.exists()


def test_verified_python_entrypoint_preserves_repo_root_and_sibling_imports(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    declare_adapter(git_repo)
    adapter_path = git_repo / "scripts" / "dww_adapter.py"
    helper_path = git_repo / "scripts" / "adapter_helper.py"
    helper_path.write_text("AVAILABLE_SLOTS = 3\n", encoding="utf-8")
    adapter_path.write_text(
        """import json
import sys
from pathlib import Path

from adapter_helper import AVAILABLE_SLOTS

if Path(__file__).resolve().parents[1] != Path.cwd().resolve():
    raise SystemExit("repository root semantics changed")
request = json.load(sys.stdin)
json.dump(
    {
        "schema_version": 1,
        "adapter_id": request["adapter_id"],
        "fingerprint": request["fingerprint"],
        "operation": request["operation"],
        "ok": True,
        "result": {"available_slots": AVAILABLE_SLOTS},
    },
    sys.stdout,
)
""",
        encoding="utf-8",
    )
    contract_path = git_repo / ".solo-ai" / "delegated.toml"
    contract_path.write_text(
        contract_path.read_text(encoding="utf-8").replace(
            'tracked_inputs = ["scripts/dww_adapter.py", "scripts/worktree-flow.ps1"]',
            "tracked_inputs = ["
            '"scripts/adapter_helper.py", '
            '"scripts/dww_adapter.py", '
            '"scripts/worktree-flow.ps1"]',
        ),
        encoding="utf-8",
    )
    git(git_repo, "add", ".solo-ai/delegated.toml", "scripts")
    git(git_repo, "commit", "-m", "add adapter import fixture")
    repo = GitRepo(git_repo)
    inspection = inspect_delegated(repo.root, repo.common_dir)
    approve_delegated(
        repo.root,
        repo.common_dir,
        fingerprint=inspection["adapter"]["fingerprint"],
    )
    snapshots: list[Path] = []
    original_run = delegated._run_adapter_process

    def capture_snapshot(*args: object, **kwargs: object) -> object:
        argv = args[0]
        assert isinstance(argv, list)
        snapshots.append(Path(argv[-1]))
        assert snapshots[-1].parent == adapter_path.parent
        return original_run(*args, **kwargs)

    monkeypatch.setattr(delegated, "_run_adapter_process", capture_snapshot)

    response = invoke_delegated(
        repo.root,
        repo.common_dir,
        operation="status",
        request={},
        timeout_seconds=30,
    )

    assert response["result"] == {"available_slots": 3}
    assert len(snapshots) == 1
    assert snapshots[0] != adapter_path
    assert snapshots[0].suffix == ".py"
    assert not snapshots[0].exists()


@pytest.mark.parametrize(
    ("runtime", "suffix"),
    (("python", ".py"), ("powershell", ".ps1"), ("sh", ".sh")),
)
def test_verified_entrypoint_snapshot_keeps_cross_runtime_directory_semantics(
    tmp_path: Path, runtime: str, suffix: str
) -> None:
    root = tmp_path / "repo"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    entrypoint = scripts / f"adapter{suffix}"
    content = b"approved entrypoint bytes\n"
    entrypoint.write_bytes(content)
    contract = delegated.DelegatedContract(
        adapter_id="portable-adapter",
        runtime=runtime,
        entrypoint=f"scripts/adapter{suffix}",
        workflow_markers=("scripts/worktree-flow.ps1",),
        tracked_inputs=(f"scripts/adapter{suffix}",),
        capabilities=("status",),
        max_parallel=1,
        fingerprint="0" * 64,
    )

    with delegated._verified_entrypoint_snapshot(
        root, contract, content
    ) as snapshot:
        assert snapshot.parent == entrypoint.parent
        assert snapshot.suffix == suffix
        assert snapshot.read_bytes() == content

    assert not snapshot.exists()


def test_verified_entrypoint_snapshot_is_removed_when_launch_fails(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    declare_adapter(git_repo)
    repo = GitRepo(git_repo)
    inspection = inspect_delegated(repo.root, repo.common_dir)
    approve_delegated(
        repo.root,
        repo.common_dir,
        fingerprint=inspection["adapter"]["fingerprint"],
    )
    snapshots: list[Path] = []

    def fail_launch(*args: object, **kwargs: object) -> object:
        argv = args[0]
        assert isinstance(argv, list)
        snapshots.append(Path(argv[-1]))
        assert snapshots[-1].exists()
        raise DelegatedContractError("synthetic launch failure")

    monkeypatch.setattr(delegated, "_run_adapter_process", fail_launch)

    with pytest.raises(DelegatedContractError, match="synthetic launch failure"):
        invoke_delegated(
            repo.root,
            repo.common_dir,
            operation="status",
            request={},
            timeout_seconds=30,
        )

    assert len(snapshots) == 1
    assert not snapshots[0].exists()


def test_changed_snapshot_path_is_preserved_instead_of_deleted(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    declare_adapter(git_repo)
    repo = GitRepo(git_repo)
    inspection = inspect_delegated(repo.root, repo.common_dir)
    approve_delegated(
        repo.root,
        repo.common_dir,
        fingerprint=inspection["adapter"]["fingerprint"],
    )
    replacements: list[Path] = []

    def replace_snapshot(*args: object, **kwargs: object) -> object:
        argv = args[0]
        assert isinstance(argv, list)
        snapshot = Path(argv[-1])
        snapshot.unlink()
        snapshot.write_text("replacement must survive\n", encoding="utf-8")
        replacements.append(snapshot)
        raise DelegatedContractError("synthetic launch failure")

    monkeypatch.setattr(delegated, "_run_adapter_process", replace_snapshot)

    with pytest.raises(DelegatedContractError, match="changed path was preserved"):
        invoke_delegated(
            repo.root,
            repo.common_dir,
            operation="status",
            request={},
            timeout_seconds=30,
        )

    assert len(replacements) == 1
    assert replacements[0].read_text(encoding="utf-8") == "replacement must survive\n"
    replacements[0].unlink()


def test_invoke_returns_the_exact_idempotent_start_receipt(git_repo: Path) -> None:
    declare_adapter(git_repo)
    repo = GitRepo(git_repo)
    inspection = inspect_delegated(repo.root, repo.common_dir)
    approve_delegated(
        repo.root,
        repo.common_dir,
        fingerprint=inspection["adapter"]["fingerprint"],
    )

    response = invoke_delegated(
        repo.root,
        repo.common_dir,
        operation="start",
        request={"name": "task", "request_id": "request-1"},
        timeout_seconds=30,
    )

    assert response["ok"] is True
    assert response["result"]["request_id"] == "request-1"
    assert Path(response["result"]["worktree"]).is_absolute()
    assert set(response["result"]) == {
        "request_id",
        "task_id",
        "worktree",
        "slot_id",
        "branch",
        "base_head",
        "request_reused",
    }


def test_invoke_rejects_undeclared_capability(git_repo: Path) -> None:
    declare_delegated_adapter(
        git_repo,
        capabilities=("status",),
        approve=False,
    )
    repo = GitRepo(git_repo)
    inspection = inspect_delegated(repo.root, repo.common_dir)
    approve_delegated(
        repo.root,
        repo.common_dir,
        fingerprint=inspection["adapter"]["fingerprint"],
    )

    with pytest.raises(DelegatedContractError, match="does not declare capability"):
        invoke_delegated(
            repo.root,
            repo.common_dir,
            operation="start",
            request={"name": "task", "request_id": "request-1"},
            timeout_seconds=30,
        )


def test_v1_capability_interface_contains_only_proven_operations() -> None:
    assert ALLOWED_CAPABILITIES == {"status", "start"}


def test_operation_requests_are_exact_before_the_adapter_runs(git_repo: Path) -> None:
    declare_adapter(git_repo)
    repo = GitRepo(git_repo)
    inspection = inspect_delegated(repo.root, repo.common_dir)
    approve_delegated(
        repo.root,
        repo.common_dir,
        fingerprint=inspection["adapter"]["fingerprint"],
    )

    with pytest.raises(DelegatedContractError, match="status request must be an empty"):
        invoke_delegated(
            repo.root,
            repo.common_dir,
            operation="status",
            request={"purpose": "orchestration-capacity"},
            timeout_seconds=30,
        )
    with pytest.raises(DelegatedContractError, match="exactly name and request_id"):
        invoke_delegated(
            repo.root,
            repo.common_dir,
            operation="start",
            request={"name": "task"},
            timeout_seconds=30,
        )


def test_operation_results_are_exact_and_typed() -> None:
    with pytest.raises(DelegatedContractError, match="exactly available_slots"):
        delegated._validate_operation_result(
            "status",
            {"available_slots": 1, "slots": []},
            request={},
            max_parallel=2,
        )
    with pytest.raises(DelegatedContractError, match="unexpected fields"):
        delegated._validate_operation_result(
            "start",
            {
                "request_id": "request-1",
                "task_id": "task-1",
                "worktree": "/tmp/worktree",
                "slot_id": "slot-01",
                "branch": "codex/example",
                "base_head": "0" * 40,
                "request_reused": False,
                "fields": {},
            },
            request={"name": "task", "request_id": "request-1"},
            max_parallel=2,
        )


def test_adapter_reported_failure_is_not_wrapped_as_success(git_repo: Path) -> None:
    declare_adapter(git_repo)
    adapter_path = git_repo / "scripts" / "dww_adapter.py"
    adapter_path.write_text(
        """import json
import sys

request = json.load(sys.stdin)
json.dump(
    {
        "schema_version": 1,
        "adapter_id": request["adapter_id"],
        "fingerprint": request["fingerprint"],
        "operation": request["operation"],
        "ok": False,
        "error": "native controller refused the request",
    },
    sys.stdout,
)
""",
        encoding="utf-8",
    )
    repo = GitRepo(git_repo)
    inspection = inspect_delegated(repo.root, repo.common_dir)
    approve_delegated(
        repo.root,
        repo.common_dir,
        fingerprint=inspection["adapter"]["fingerprint"],
    )

    with pytest.raises(DelegatedContractError, match="reported status failure"):
        invoke_delegated(
            repo.root,
            repo.common_dir,
            operation="status",
            request={},
            timeout_seconds=30,
        )


def test_adapter_process_bounds_output_and_terminates_child_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(delegated, "MAX_ADAPTER_STDOUT_BYTES", 128)
    with pytest.raises(DelegatedContractError, match="stdout exceeds"):
        delegated._run_adapter_process(
            [sys.executable, "-c", "print('x' * 1024)"],
            root=tmp_path,
            request_bytes=b"{}",
            timeout_seconds=5,
        )

    marker = tmp_path / "late-child.txt"
    child = (
        "import time; from pathlib import Path; time.sleep(1); "
        f"Path({str(marker)!r}).write_text('late', encoding='utf-8')"
    )
    parent = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(30)"
    )
    started = time.monotonic()
    with pytest.raises(DelegatedContractError, match="timed out"):
        delegated._run_adapter_process(
            [sys.executable, "-c", parent],
            root=tmp_path,
            request_bytes=json.dumps({}).encode("utf-8"),
            timeout_seconds=0.1,
        )
    assert time.monotonic() - started < 15
    time.sleep(1.2)
    assert not marker.exists()


def test_approval_requires_the_exact_reported_fingerprint(git_repo: Path) -> None:
    declare_adapter(git_repo)
    repo = GitRepo(git_repo)

    with pytest.raises(DelegatedContractError, match="does not match"):
        approve_delegated(repo.root, repo.common_dir, fingerprint="0" * 64)

    assert not (repo.local_dir / "delegated-adapter-approval.json").exists()


def test_exact_revoke_returns_route_to_safe_defer(git_repo: Path) -> None:
    declare_adapter(git_repo)
    repo = GitRepo(git_repo)
    inspection = inspect_delegated(repo.root, repo.common_dir)
    fingerprint = inspection["adapter"]["fingerprint"]
    approve_delegated(repo.root, repo.common_dir, fingerprint=fingerprint)

    with pytest.raises(DelegatedContractError, match="Adapter id does not match"):
        revoke_delegated(
            repo.common_dir,
            adapter_id="wrong-adapter",
            fingerprint=fingerprint,
        )
    assert repository_route(repo)["action"] == "delegated"

    revoked = revoke_delegated(
        repo.common_dir,
        adapter_id="example-worktree-flow",
        fingerprint=fingerprint,
    )

    assert revoked["revoked"] is True
    assert repository_route(repo)["action"] == "defer"
    assert repository_route(repo)["reason"] == "delegated-approval-required"
