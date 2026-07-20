from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_cli_json_status(git_repo: Path) -> None:
    runner = (
        Path(__file__).parents[2]
        / "skills"
        / "develop-with-worktrees"
        / "scripts"
        / "dww.py"
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(runner),
            "--repo",
            str(git_repo),
            "--json",
            "status",
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["ok"] is True
    assert payload["result"]["initialized"] is False
    assert payload["result"]["default_branch"] == "main"
