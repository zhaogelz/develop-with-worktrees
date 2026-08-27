from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from conftest import git


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


def _call(repo: Path, *arguments: str) -> dict[str, object]:
    completed = subprocess.run(
        [
            "uv",
            "run",
            "--script",
            str(_runner()),
            "--repo",
            str(repo),
            "--json",
            *arguments,
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "cp1252"},
        capture_output=True,
        check=False,
        timeout=90,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    payload = json.loads(completed.stdout)
    assert payload["ok"] is True
    return payload["result"]


def _approve_test_adapter(repo: Path) -> None:
    scripts = repo / "scripts"
    policy = repo / ".solo-ai"
    scripts.mkdir()
    policy.mkdir()
    (scripts / "worktree-flow.ps1").write_text("# lifecycle\n", encoding="utf-8")
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
        "result": {"available_slots": 5},
    },
    sys.stdout,
)
""",
        encoding="utf-8",
    )
    (policy / "delegated.toml").write_text(
        """schema_version = 1
id = "orchestration-test"
runtime = "python"
entrypoint = "scripts/dww_adapter.py"
workflow_markers = ["scripts/worktree-flow.ps1"]
tracked_inputs = ["scripts/dww_adapter.py", "scripts/worktree-flow.ps1"]
capabilities = ["status"]
max_parallel = 5
""",
        encoding="utf-8",
    )
    git(repo, "add", ".solo-ai/delegated.toml", "scripts")
    git(repo, "commit", "-m", "declare delegated adapter")
    inspection = _call(repo, "delegated", "inspect")
    fingerprint = str(inspection["adapter"]["fingerprint"])
    _call(
        repo,
        "delegated",
        "approve",
        "--fingerprint",
        fingerprint,
        "--accept",
    )


def test_orchestration_cli_requires_confirmation_then_returns_frontier(
    git_repo: Path,
) -> None:
    _approve_test_adapter(git_repo)
    planned = _call(
        git_repo,
        "orchestrate",
        "plan",
        "--adapter",
        "delegated",
        "--controller",
        "central-controller",
        "--goal",
        "让用户看见结果",
        "--task",
        json.dumps({"id": "api", "title": "提供结果", "acceptance": ["可读取"]}),
        "--task",
        json.dumps(
            {
                "id": "page",
                "title": "显示结果",
                "acceptance": ["可看见"],
                "depends_on": ["api"],
            }
        ),
    )
    batch_id = str(planned["id"])
    assert planned["goal"] == "让用户看见结果"
    assert planned["tasks"]["api"]["title"] == "提供结果"
    assert planned["status"] == "awaiting-confirmation"
    assert "controller" not in planned

    before = _call(git_repo, "orchestrate", "frontier", "--batch", batch_id)
    assert before["tasks"] == []
    _call(
        git_repo,
        "orchestrate",
        "confirm",
        "--batch",
        batch_id,
        "--controller",
        "central-controller",
    )
    after = _call(git_repo, "orchestrate", "frontier", "--batch", batch_id)
    assert [task["id"] for task in after["tasks"]] == ["api"]
