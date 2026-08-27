from __future__ import annotations

from pathlib import Path

import pytest
from conftest import git
from solo_ai.delegated import (
    DelegatedContractError,
    approve_delegated,
    inspect_delegated,
    invoke_delegated,
)
from solo_ai.lifecycle import repository_route
from solo_ai.repo import GitRepo


def declare_adapter(root: Path, *, max_parallel: int = 4) -> None:
    scripts = root / "scripts"
    policy = root / ".solo-ai"
    scripts.mkdir()
    policy.mkdir()
    (scripts / "worktree-flow.ps1").write_text(
        "# mature lifecycle\n", encoding="utf-8"
    )
    (scripts / "dww_adapter.py").write_text(
        """import json
import sys

request = json.load(sys.stdin)
json.dump(
    {
        "schema_version": 1,
        "adapter_id": request["adapter_id"],
        "fingerprint": request["fingerprint"],
        "operation": request["operation"],
        "ok": True,
        "result": {"available_slots": 2, "received": request["request"]},
    },
    sys.stdout,
)
""",
        encoding="utf-8",
    )
    (policy / "delegated.toml").write_text(
        f"""schema_version = 1
id = "example-worktree-flow"
runtime = "python"
entrypoint = "scripts/dww_adapter.py"
workflow_markers = ["scripts/worktree-flow.ps1"]
tracked_inputs = ["scripts/dww_adapter.py", "scripts/worktree-flow.ps1"]
capabilities = ["start", "status"]
max_parallel = {max_parallel}
""",
        encoding="utf-8",
    )
    git(root, "add", ".solo-ai/delegated.toml", "scripts/dww_adapter.py")
    git(root, "add", "scripts/worktree-flow.ps1")
    git(root, "commit", "-m", "declare delegated adapter")


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
        request={"task_id": "task-1"},
        timeout_seconds=30,
    )

    assert response["ok"] is True
    assert response["result"] == {
        "available_slots": 2,
        "received": {"task_id": "task-1"},
    }


def test_invoke_rejects_undeclared_capability(git_repo: Path) -> None:
    declare_adapter(git_repo)
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
            operation="finish",
            request={},
            timeout_seconds=30,
        )


def test_approval_requires_the_exact_reported_fingerprint(git_repo: Path) -> None:
    declare_adapter(git_repo)
    repo = GitRepo(git_repo)

    with pytest.raises(DelegatedContractError, match="does not match"):
        approve_delegated(repo.root, repo.common_dir, fingerprint="0" * 64)

    assert not (repo.local_dir / "delegated-adapter-approval.json").exists()
