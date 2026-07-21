# /// script
# requires-python = ">=3.11"
# ///

"""Lightweight, stdlib-only Codex hook for the plugin's supported local tools.

The Codex hook protocol can inject a system message before a tool call, but it
does not expose a portable hard-deny decision for PreToolUse. Keep this hook
small and advisory; the lifecycle CLI is the authoritative mutation gate.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any


WORKFLOW_MARKERS = (
    ".config/wt.toml",
    ".conductor",
    ".parallel-code",
    "scripts/worktree-flow.ps1",
    ".sdd",
)
WRITE_WORDS = (
    "apply_patch",
    "write",
    "move-item",
    "copy-item",
    "remove-item",
    "new-item",
    "set-content",
    "add-content",
    "out-file",
    "git add",
    "git commit",
    "git merge",
    "git switch",
    "git checkout",
    "git reset",
    "git clean",
    " >",
    " >>",
)
READ_PREFIXES = (
    "git status",
    "git diff",
    "git log",
    "git show",
    "git branch",
    "git rev-parse",
    "git worktree list",
    "rg ",
    "get-content",
    "ls",
    "dir",
    "pwd",
    "where",
    "get-command",
)


def git_root(cwd: str) -> Path | None:
    completed = subprocess.run(
        ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        return None
    return Path(completed.stdout.strip())


def command_from(payload: dict[str, Any]) -> str:
    tool_input = (
        payload.get("tool_input")
        or payload.get("toolInput")
        or payload.get("input")
        or {}
    )
    if not isinstance(tool_input, dict):
        return ""
    for key in ("command", "patch", "text", "content"):
        value = tool_input.get(key)
        if isinstance(value, str):
            return value.lower()
    return ""


def is_probable_write(payload: dict[str, Any]) -> bool:
    name = str(payload.get("tool_name") or payload.get("toolName") or "")
    if name in {"apply_patch", "Edit", "Write"}:
        return True
    command = command_from(payload).strip()
    if not command:
        # For a write-matched editor tool whose input schema we do not yet know,
        # prefer a safe model-visible warning over pretending it was read-only.
        return bool(name)
    if command.startswith(READ_PREFIXES):
        return False
    return any(word in command for word in WRITE_WORDS)


def preference_disabled(root: Path) -> bool:
    completed = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--git-common-dir"],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        return False
    common = Path(completed.stdout.strip())
    if not common.is_absolute():
        common = root / common
    path = common / "solo-ai" / "preferences.json"
    try:
        return not bool(
            json.loads(path.read_text(encoding="utf-8")).get("enabled", True)
        )
    except (OSError, json.JSONDecodeError):
        return False


def message(payload: dict[str, Any]) -> str | None:
    root = git_root(str(payload.get("cwd") or "."))
    if root is None:
        return None
    if preference_disabled(root):
        return "develop-with-worktrees is locally disabled for this repository; do not claim or repair a managed slot. Follow the user's explicit direction."
    markers = [marker for marker in WORKFLOW_MARKERS if (root / marker).exists()]
    if markers:
        return (
            "This repository already has a mature worktree/orchestration workflow ("
            + ", ".join(markers)
            + "). develop-with-worktrees must defer: make no .solo-ai files, AGENTS edits, slots, process, or validation changes."
        )
    adopted = (root / ".solo-ai" / "config.toml").exists()
    if payload.get("hook_event_name") == "SessionStart":
        if adopted:
            return "Repository adopts develop-with-worktrees. For a modifying task, use its Start → exact-path Commit → Ready → Finish lifecycle. Read-only work stays in place. The CLI is the hard gate; this hook is an advisory Codex guardrail."
        return "For a new Git repository modifying task, develop-with-worktrees requires one user confirmation before adoption. Run its read-only init plan; do not write .solo-ai or AGENTS.md directly."
    if is_probable_write(payload):
        if adopted:
            return "Potential repository mutation detected. This repository policy requires work only in a Start-returned managed worktree and an exact-path Commit → Ready → Finish lifecycle. Do not edit the primary worktree."
        return "Potential repository mutation detected in an unadopted Git repository. First show the user the develop-with-worktrees initialization plan and obtain one explicit acceptance or local decline. Do not create policy files directly."
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        result = message(payload)
    except Exception:
        # Hook failures must not disguise themselves as policy success. Codex cannot
        # hard-deny PreToolUse, so emit a conservative warning for the model.
        result = "develop-with-worktrees guard could not inspect this tool call. Treat a possible write as unsafe until `dww doctor` is checked."
    if result:
        print(json.dumps({"systemMessage": result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
