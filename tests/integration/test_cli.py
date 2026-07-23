from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path


def test_release_version_contract_matches_manifest_metadata_and_cli(
    git_repo: Path,
) -> None:
    repository_root = Path(__file__).parents[2]
    runner = (
        repository_root
        / "plugins"
        / "develop-with-worktrees"
        / "skills"
        / "develop-with-worktrees"
        / "scripts"
        / "dww.py"
    )
    completed = subprocess.run(
        [sys.executable, str(runner), "--repo", str(git_repo), "--json", "version"],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)["result"]
    manifest = json.loads(
        (
            repository_root
            / "plugins"
            / "develop-with-worktrees"
            / ".codex-plugin"
            / "plugin.json"
        ).read_text(encoding="utf-8")
    )
    pyproject = tomllib.loads(
        (repository_root / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert payload["version"] == "0.2.0-beta.1"
    assert payload["version"] == payload["plugin_version"] == manifest["version"]
    assert payload["version"] == pyproject["project"]["version"]
    assert payload["verification_schema"] == 3
    assert Path(payload["script"]).name == "dww.py"


def test_user_facing_docs_describe_only_the_current_contract() -> None:
    repository_root = Path(__file__).parents[2]
    documents = [
        repository_root / "README.md",
        repository_root / "README.zh-CN.md",
        repository_root / "CHANGELOG.md",
        repository_root / "总体规划.md",
        repository_root / "需求.md",
        repository_root / "方案.md",
        repository_root
        / "plugins"
        / "develop-with-worktrees"
        / "skills"
        / "develop-with-worktrees"
        / "SKILL.md",
    ]
    text = "\n".join(path.read_text(encoding="utf-8") for path in documents)
    assert "0.1.0-beta.2" not in text
    assert "01..05" not in text
    assert "schema 3 only" in text
    assert "machine-global weighted FIFO" in text


def test_cli_json_status_masks_uninitialized_state(git_repo: Path) -> None:
    runner = (
        Path(__file__).parents[2]
        / "plugins"
        / "develop-with-worktrees"
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
        / "plugins"
        / "develop-with-worktrees"
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
        / "plugins"
        / "develop-with-worktrees"
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
        / "plugins"
        / "develop-with-worktrees"
        / "skills"
        / "develop-with-worktrees"
        / "scripts"
        / "dww.py"
    )

    def call_json(*arguments: str, repo_path: Path = git_repo) -> dict:
        completed = subprocess.run(
            [
                "uv",
                "run",
                "--script",
                str(runner),
                "--repo",
                str(repo_path),
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
        repo_path=worktree,
    )
    call_json(
        "ready", "--task", task["id"], "--lease", task["lease"], repo_path=worktree
    )
    call_json(
        "finish", "--task", task["id"], "--lease", task["lease"], repo_path=worktree
    )
    assert (git_repo / "cli.txt").exists()
