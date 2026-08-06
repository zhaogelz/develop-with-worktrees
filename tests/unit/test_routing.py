from __future__ import annotations

from solo_ai.routing import decide_route


def test_mature_workflow_has_absolute_routing_precedence() -> None:
    result = decide_route(
        workflows=["repository worktree-flow"],
        local_enabled=False,
        current_task=True,
        adopted=True,
    )

    assert result == {
        "action": "defer",
        "reason": "existing-workflow",
        "workflows": ["repository worktree-flow"],
    }


def test_route_orders_local_and_managed_dww_modes_after_defer() -> None:
    assert decide_route(
        workflows=[],
        local_enabled=False,
        current_task=True,
        adopted=True,
    ) == {"action": "disabled", "reason": "local-preference"}
    assert decide_route(
        workflows=[],
        local_enabled=True,
        current_task=True,
        adopted=True,
    ) == {"action": "current-task", "reason": "session-override"}
    assert decide_route(
        workflows=[],
        local_enabled=True,
        current_task=False,
        adopted=True,
    ) == {"action": "managed", "reason": "adopted"}
    assert decide_route(
        workflows=[],
        local_enabled=True,
        current_task=False,
        adopted=False,
    ) == {"action": "ask", "reason": "unchosen"}
