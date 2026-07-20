from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_cli_json_status_masks_uninitialized_state(git_repo: Path) -> None:
    runner = (
        Path(__file__).parents[2]
        / "skills"
        / "develop-with-worktrees"
        / "scripts"
        / "dww.py"
    )
    completed = subprocess.run(
        [sys.executable, str(runner), "--repo", str(git_repo), "--json", "status"],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["ok"] is True
    assert payload["result"]["mode"] == "uninitialized"
    assert "lease" not in json.dumps(payload["result"])


def test_cli_init_only_shows_plan_until_acceptance(git_repo: Path) -> None:
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
            "init",
            "--verify",
            '["git","status","--short"]',
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["result"]["decision"] == "needs-approval"
    assert {
        "profiles",
        "dependency_inputs",
        "platform_condition",
        "cross_task_policy",
    } <= set(payload["result"]["plan"])
    assert not (git_repo / ".solo-ai").exists()


def test_cli_static_only_first_shows_a_plan(git_repo: Path) -> None:
    runner = (
        Path(__file__).parents[2]
        / "skills"
        / "develop-with-worktrees"
        / "scripts"
        / "dww.py"
    )
    completed = subprocess.run(
        [sys.executable, str(runner), "--repo", str(git_repo), "--json", "init"],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["result"]["decision"] == "needs-approval"
    assert payload["result"]["plan"]["static_only"] is True


def test_full_cli_lifecycle_runs_through_uv_script(git_repo: Path) -> None:
    runner = (
        Path(__file__).parents[2]
        / "skills"
        / "develop-with-worktrees"
        / "scripts"
        / "dww.py"
    )

    def call_json(*arguments: str) -> dict:
        completed = subprocess.run(
            [
                "uv",
                "run",
                "--script",
                str(runner),
                "--repo",
                str(git_repo),
                "--json",
                *arguments,
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=90,
        )
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout)["result"]

    def call_start() -> dict[str, str]:
        completed = subprocess.run(
            [
                "uv",
                "run",
                "--script",
                str(runner),
                "--repo",
                str(git_repo),
                "start",
                "--name",
                "cli greeting",
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=90,
        )
        assert completed.returncode == 0, completed.stderr
        values = dict(line.split(": ", 1) for line in completed.stdout.splitlines())
        return {
            "id": values["Task"],
            "worktree": values["Worktree"],
            "lease": values["Lease"],
        }

    adopted = call_json(
        "init", "--accept", "--verify", '["git","diff","--check","main...HEAD"]'
    )
    assert adopted["decision"] == "adopted"
    task = call_start()
    worktree = Path(task["worktree"])
    (worktree / "cli.txt").write_text("hello\n", encoding="utf-8")
    call_json(
        "commit",
        "--task",
        task["id"],
        "--lease",
        task["lease"],
        "--message",
        "feat: cli greeting",
        "--path",
        "cli.txt",
    )
    call_json("ready", "--task", task["id"], "--lease", task["lease"])
    call_json("finish", "--task", task["id"], "--lease", task["lease"])
    assert (git_repo / "cli.txt").exists()
