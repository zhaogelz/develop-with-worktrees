from __future__ import annotations

import pytest
from solo_ai.config import CommandSpec
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
    delegated.assert_available(repo)
    assert delegated.available_slots(repo, batch_limit=5) == 5
    with pytest.raises(SoloAIError, match="requires a managed repository"):
        adapter_for("dww").assert_available(repo)
