from __future__ import annotations

import json
import subprocess
from pathlib import Path


def _runner() -> Path:
    return (
        Path(__file__).parents[2]
        / "plugins"
        / "develop-with-worktrees"
        / "skills"
        / "develop-with-worktrees"
        / "scripts"
        / "dww.py"
    )


def test_orchestration_cli_rejects_new_batches_and_points_to_native_tasks(
    git_repo: Path,
) -> None:
    completed = subprocess.run(
        [
            "uv",
            "run",
            "--script",
            str(_runner()),
            "--repo",
            str(git_repo),
            "--json",
            "orchestrate",
            "plan",
            "--controller",
            "legacy-controller",
            "--goal",
            "new work",
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=90,
    )

    assert completed.returncode == 2
    payload = json.loads(completed.stdout)
    assert payload["ok"] is False
    assert "native task/subagent system" in payload["error"]
