from __future__ import annotations

from pathlib import Path
from typing import Any

WORKFLOW_MARKERS = {
    ".config/wt.toml": "Worktrunk",
    ".conductor": "Conductor",
    ".parallel-code": "Parallel Code",
    "scripts/worktree-flow.ps1": "repository worktree-flow",
    ".sdd": "agent orchestrator workspace",
}


def detect_existing_workflows(root: Path) -> list[str]:
    return [
        name
        for relative, name in WORKFLOW_MARKERS.items()
        if (root / relative).exists()
    ]


def decide_route(
    *,
    workflows: list[str],
    local_enabled: bool,
    current_task: bool,
    adopted: bool,
    delegated: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """按稳定优先级返回极小、只读的仓库路由结果。"""
    if delegated and delegated.get("declared"):
        if delegated.get("approved"):
            return {
                "action": "delegated",
                "reason": "approved-adapter",
                "workflows": workflows,
                "adapter": delegated["adapter"],
            }
        result: dict[str, Any] = {
            "action": "defer",
            "reason": f"delegated-{delegated.get('reason', 'invalid-contract')}",
            "workflows": workflows,
        }
        if delegated.get("adapter"):
            result["adapter"] = delegated["adapter"]
        if delegated.get("error"):
            result["error"] = delegated["error"]
        if delegated.get("approval_error"):
            result["error"] = delegated["approval_error"]
        return result
    if workflows:
        return {
            "action": "defer",
            "reason": "existing-workflow",
            "workflows": workflows,
        }
    if not local_enabled:
        return {"action": "disabled", "reason": "local-preference"}
    if current_task:
        return {"action": "current-task", "reason": "session-override"}
    if adopted:
        return {"action": "managed", "reason": "adopted"}
    return {"action": "ask", "reason": "unchosen"}
