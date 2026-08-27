from __future__ import annotations

import pytest
from conftest import git
from solo_ai.config import CommandSpec
from solo_ai.delegated import approve_delegated, inspect_delegated
from solo_ai.lifecycle import initialize
from solo_ai.orchestration.adapters import adapter_for
from solo_ai.repo import GitRepo
from solo_ai.util import SoloAIError


def test_dww_adapter_requires_managed_route_and_reads_only_idle_slots(git_repo) -> None:
    repo = GitRepo(git_repo)
    with pytest.raises(SoloAIError, match="requires a managed repository"):
        adapter_for("dww").assert_available(repo)

    initialize(
        repo,
        slots=2,
        commands=[CommandSpec(("git", "diff", "--check", "main...HEAD"))],
        accept=True,
        accept_static_only=False,
    )
    adapter = adapter_for("dww")
    adapter.assert_available(repo)
    assert adapter.available_slots(repo, batch_limit=5) == 2


def test_delegated_adapter_does_not_take_over_a_mature_repository(git_repo) -> None:
    repo = GitRepo(git_repo)
    marker = git_repo / "scripts" / "worktree-flow.ps1"
    marker.parent.mkdir()
    marker.write_text("# external lifecycle\n", encoding="utf-8")

    delegated = adapter_for("delegated")
    with pytest.raises(SoloAIError, match="valid, locally approved"):
        delegated.assert_available(repo)

    entrypoint = git_repo / "scripts" / "dww_adapter.py"
    entrypoint.write_text(
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
        "result": {"available_slots": 2},
    },
    sys.stdout,
)
""",
        encoding="utf-8",
    )
    policy = git_repo / ".solo-ai"
    policy.mkdir()
    (policy / "delegated.toml").write_text(
        """schema_version = 1
id = "example-worktree-flow"
runtime = "python"
entrypoint = "scripts/dww_adapter.py"
workflow_markers = ["scripts/worktree-flow.ps1"]
tracked_inputs = ["scripts/dww_adapter.py", "scripts/worktree-flow.ps1"]
capabilities = ["status"]
max_parallel = 3
""",
        encoding="utf-8",
    )
    git(git_repo, "add", ".solo-ai/delegated.toml", "scripts")
    git(git_repo, "commit", "-m", "declare delegated adapter")
    inspection = inspect_delegated(repo.root, repo.common_dir)
    approve_delegated(
        repo.root,
        repo.common_dir,
        fingerprint=inspection["adapter"]["fingerprint"],
    )

    delegated.assert_available(repo)
    assert delegated.available_slots(repo, batch_limit=5) == 2
    with pytest.raises(SoloAIError, match="requires a managed repository"):
        adapter_for("dww").assert_available(repo)
