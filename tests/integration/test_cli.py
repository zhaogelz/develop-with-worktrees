from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

from conftest import git
from solo_ai import __version__


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
    assert payload["version"] == "0.3.0-beta.1"
    assert payload["version"] == payload["plugin_version"] == manifest["version"]
    assert payload["version"] == pyproject["project"]["version"]
    assert payload["version"] == __version__
    assert f"## {payload['version']}" in (repository_root / "CHANGELOG.md").read_text(
        encoding="utf-8"
    )
    assert payload["verification_schema"] == 3
    assert payload["state_schema"] == 3
    assert "PreToolUse deny" in payload["codex_guard"]
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
    assert "--json route" in text
    assert "mature workflow" in text
    assert "multi-AI" in text
    assert "one-confirmation" in text


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


def test_cli_route_is_compact_and_read_only_for_mature_workflow(
    git_repo: Path,
) -> None:
    runner = (
        Path(__file__).parents[2]
        / "plugins"
        / "develop-with-worktrees"
        / "skills"
        / "develop-with-worktrees"
        / "scripts"
        / "dww.py"
    )
    marker = git_repo / "scripts" / "worktree-flow.ps1"
    marker.parent.mkdir()
    marker.write_text("# existing\n", encoding="utf-8")
    before = git(git_repo, "status", "--porcelain")

    completed = subprocess.run(
        [
            "uv",
            "run",
            "--script",
            str(runner),
            "--repo",
            str(git_repo),
            "--json",
            "route",
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=90,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload == {
        "ok": True,
        "result": {
            "action": "defer",
            "reason": "existing-workflow",
            "workflows": ["repository worktree-flow"],
        },
    }
    assert git(git_repo, "status", "--porcelain") == before
    assert not (git_repo / ".solo-ai").exists()


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


def test_cli_choose_current_task_redacts_session_and_delegation_code(
    git_repo: Path,
) -> None:
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
            "uv",
            "run",
            "--script",
            str(runner),
            "--repo",
            str(git_repo),
            "--json",
            "choose",
            "--mode",
            "current-task",
            "--session",
            "private-session",
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=90,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["result"] == {"choice": "current-task", "delegated": False}
    assert "private-session" not in completed.stdout
    assert "delegation_code" not in completed.stdout


def test_cli_plan_and_verify_cover_registered_development_ready_and_full_levels(
    git_repo: Path,
) -> None:
    runner = (
        Path(__file__).parents[2]
        / "plugins"
        / "develop-with-worktrees"
        / "skills"
        / "develop-with-worktrees"
        / "scripts"
        / "dww.py"
    )

    def call(
        *arguments: str, repo_path: Path = git_repo
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "uv",
                "run",
                "--script",
                str(runner),
                "--repo",
                str(repo_path),
                *arguments,
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=90,
        )

    def call_json(*arguments: str, repo_path: Path = git_repo) -> dict:
        completed = call("--json", *arguments, repo_path=repo_path)
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout)["result"]

    call_json(
        "init", "--accept", "--verify", '["git", "diff", "--check", "main...HEAD"]'
    )
    policy = git_repo / ".solo-ai" / "verification.toml"
    policy.write_text(
        """schema_version = 3
static_only = false

[[profiles]]
id = "development"
level = "development"
paths = ["**"]
commands = [["git", "diff", "--check", "main...HEAD"]]

[[profiles]]
id = "ready"
level = "ready"
paths = ["**"]
commands = [["git", "diff", "--check", "main...HEAD"]]

[[profiles]]
id = "full"
level = "full"
paths = ["**"]
commands = [["git", "diff", "--check", "main...HEAD"]]
""",
        encoding="utf-8",
    )
    git(git_repo, "add", ".solo-ai/verification.toml")
    git(git_repo, "commit", "-m", "test: configure validation levels")
    call_json("approve", "--accept")
    started = call("start", "--name", "verify levels")
    assert started.returncode == 0, started.stderr
    values = dict(line.split(": ", 1) for line in started.stdout.splitlines())
    task_id = values["Task"]
    lease = values["Lease"]
    worktree = Path(values["Worktree"])
    (worktree / "levels.txt").write_text("levels\n", encoding="utf-8")
    call_json(
        "commit",
        "--task",
        task_id,
        "--lease",
        lease,
        "--message",
        "test: commit validation level fixture",
        "--path",
        "levels.txt",
        repo_path=worktree,
    )
    plan = call_json("plan", "--task", task_id, repo_path=worktree)
    assert {profile["level"] for profile in plan["profiles"]} == {
        "development",
        "ready",
        "full",
    }
    development = call_json(
        "verify",
        "--task",
        task_id,
        "--lease",
        lease,
        "--level",
        "development",
        repo_path=worktree,
    )
    development_proof = json.loads(
        (
            git_repo / ".git" / "solo-ai" / "proofs" / f"{development['proof']}.json"
        ).read_text(encoding="utf-8")
    )
    assert [item["profile_id"] for item in development_proof["profile_proofs"]] == [
        "development"
    ]
    full = call_json(
        "verify",
        "--task",
        task_id,
        "--lease",
        lease,
        "--level",
        "full",
        repo_path=worktree,
    )
    full_proof = json.loads(
        (git_repo / ".git" / "solo-ai" / "proofs" / f"{full['proof']}.json").read_text(
            encoding="utf-8"
        )
    )
    assert [item["profile_id"] for item in full_proof["profile_proofs"]] == [
        "ready",
        "full",
    ]
    call_json(
        "abandon",
        "--task",
        task_id,
        "--lease",
        lease,
        "--confirm",
        task_id,
        repo_path=worktree,
    )


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

    started_direct = subprocess.run(
        [
            "uv",
            "run",
            "--script",
            str(runner),
            "--repo",
            str(git_repo),
            "start",
            "--name",
            "cli current worktree",
            "--in-place",
            "--session",
            "cli-session",
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=90,
    )
    assert started_direct.returncode == 0, started_direct.stderr
    direct_values = dict(
        line.split(": ", 1) for line in started_direct.stdout.splitlines()
    )
    assert direct_values["Mode"] == "in-place"
    assert direct_values["Worktree"] == str(git_repo)
    (git_repo / "cli-current.txt").write_text("current\n", encoding="utf-8")
    call_json(
        "commit",
        "--task",
        direct_values["Task"],
        "--lease",
        direct_values["Lease"],
        "--session",
        "cli-session",
        "--message",
        "test: cli current worktree",
        "--path",
        "cli-current.txt",
    )
    direct_plan = call_json(
        "plan",
        "--task",
        direct_values["Task"],
    )
    direct_ready = call_json(
        "ready",
        "--task",
        direct_values["Task"],
        "--lease",
        direct_values["Lease"],
        "--session",
        "cli-session",
    )
    direct_proof = json.loads(
        (
            git_repo
            / ".git"
            / "solo-ai"
            / "proofs"
            / f"{direct_ready['ready_proof']}.json"
        ).read_text(encoding="utf-8")
    )
    assert (
        direct_plan["profiles"][0]["fingerprint"]
        == direct_proof["profile_proofs"][0]["fingerprint"]
    )
    finished_direct = call_json(
        "finish",
        "--task",
        direct_values["Task"],
        "--lease",
        direct_values["Lease"],
        "--session",
        "cli-session",
    )
    assert finished_direct["mode"] == "in-place"
    assert (git_repo / "cli-current.txt").exists()
