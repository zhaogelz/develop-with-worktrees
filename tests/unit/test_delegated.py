from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
from conftest import declare_delegated_adapter
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
