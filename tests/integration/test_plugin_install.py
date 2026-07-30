from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


def test_plugin_install_and_clean_uninstall_in_temporary_codex_home(
    tmp_path: Path,
) -> None:
    """Exercise Codex's real local marketplace install/remove flow, never the user's home."""
    if os.environ.get("DWW_SKIP_CODEX_CLI_INTEGRATION") == "1":
        pytest.skip("Codex CLI install integration is exercised on a local Codex host")
    repository_root = Path(__file__).parents[2]
    source = repository_root / "plugins" / "develop-with-worktrees"
    marketplace_source = repository_root / ".agents" / "plugins" / "marketplace.json"
    marketplace_root = tmp_path / "marketplace-root"
    marketplace_dir = marketplace_root / ".agents" / "plugins"
    plugin_copy = marketplace_root / "plugins" / "develop-with-worktrees"
    plugin_copy.parent.mkdir(parents=True)
    marketplace_dir.mkdir(parents=True)
    shutil.copytree(
        source,
        plugin_copy,
        ignore=shutil.ignore_patterns(".git", ".venv", ".tmp", ".cache", "__pycache__"),
    )
    shutil.copy2(marketplace_source, marketplace_dir / "marketplace.json")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    environment = {**os.environ, "CODEX_HOME": str(codex_home)}

    # The desktop AppX executable may reject direct child-process launching on
    # Windows; the npm command shim is the portable CLI entry point here.
    executable = (
        shutil.which("codex.cmd") or shutil.which("codex.exe") or shutil.which("codex")
    )
    if executable is None:
        pytest.skip("Codex CLI is not installed in this CI environment")

    def call(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [executable, *args],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            env=environment,
            timeout=60,
        )

    added_marketplace = call(
        "plugin", "marketplace", "add", str(marketplace_root), "--json"
    )
    assert added_marketplace.returncode == 0, added_marketplace.stderr
    installed = call(
        "plugin", "add", "develop-with-worktrees@develop-with-worktrees", "--json"
    )
    assert installed.returncode == 0, installed.stderr
    listed = call("plugin", "list", "--marketplace", "develop-with-worktrees", "--json")
    assert listed.returncode == 0, listed.stderr
    assert "develop-with-worktrees" in listed.stdout

    installed_hook_definitions = [
        path
        for path in codex_home.rglob("hooks.json")
        if "develop-with-worktrees" in str(path).replace("\\", "/")
        and path.parent.name == "hooks"
    ]
    assert installed_hook_definitions, "installed plugin does not expose its hook definition"
    source_hook_definition = source / "hooks" / "hooks.json"
    assert hashlib.sha256(installed_hook_definitions[0].read_bytes()).digest() == (
        hashlib.sha256(source_hook_definition.read_bytes()).digest()
    )

    runners = [
        path
        for path in codex_home.rglob("dww.py")
        if "develop-with-worktrees" in str(path).replace("\\", "/")
    ]
    assert runners, "installed plugin does not expose its lifecycle runner"
    runner = runners[0]
    smoke_repo = tmp_path / "installed-runner-smoke"

    def git(*args: str) -> None:
        completed = subprocess.run(
            ["git", *args],
            cwd=smoke_repo if smoke_repo.exists() else tmp_path,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=30,
        )
        assert completed.returncode == 0, completed.stderr

    smoke_repo.mkdir()
    git("init", "-b", "main")
    git("config", "user.name", "Installed runner test")
    git("config", "user.email", "runner@example.invalid")
    (smoke_repo / "README.md").write_text("smoke\n", encoding="utf-8")
    git("add", "README.md")
    git("commit", "-m", "test: initialize installed runner smoke repository")

    def run_runner(
        *args: str, cwd: Path = smoke_repo
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["uv", "run", "--script", str(runner), "--repo", str(cwd), *args],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=120,
        )

    initialized = run_runner(
        "--json",
        "init",
        "--accept",
        "--verify",
        '["git", "diff", "--check", "main...HEAD"]',
    )
    assert initialized.returncode == 0, initialized.stderr
    version = run_runner("--json", "version")
    assert version.returncode == 0, version.stderr
    expected_version = json.loads(
        (source / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )["version"]
    assert f'"version": "{expected_version}"' in version.stdout
    assert f'"plugin_version": "{expected_version}"' in version.stdout
    assert '"verification_schema": 3' in version.stdout
    assert '"state_schema": 3' in version.stdout
    started = run_runner("start", "--name", "installed artifact smoke")
    assert started.returncode == 0, started.stderr
    values = dict(line.split(": ", 1) for line in started.stdout.splitlines())
    task_id = values["Task"]
    lease = values["Lease"]
    worktree = Path(values["Worktree"])
    (worktree / "smoke.txt").write_text("installed\n", encoding="utf-8")
    committed = run_runner(
        "commit",
        "--task",
        task_id,
        "--lease",
        lease,
        "--message",
        "test: commit through installed runner",
        "--path",
        "smoke.txt",
        cwd=worktree,
    )
    assert committed.returncode == 0, committed.stderr
    planned = run_runner("--json", "plan", "--task", task_id, cwd=worktree)
    assert planned.returncode == 0, planned.stderr
    verified = run_runner(
        "--json",
        "verify",
        "--task",
        task_id,
        "--lease",
        lease,
        "--level",
        "ready",
        cwd=worktree,
    )
    assert verified.returncode == 0, verified.stderr
    prepared = run_runner("ready", "--task", task_id, "--lease", lease, cwd=worktree)
    assert prepared.returncode == 0, prepared.stderr
    finished = run_runner("finish", "--task", task_id, "--lease", lease, cwd=worktree)
    assert finished.returncode == 0, finished.stderr
    assert (smoke_repo / "smoke.txt").exists()
    in_place = run_runner(
        "start",
        "--name",
        "installed current worktree",
        "--in-place",
        "--session",
        "installed-codex",
    )
    assert in_place.returncode == 0, in_place.stderr
    direct = dict(line.split(": ", 1) for line in in_place.stdout.splitlines())
    assert direct["Worktree"] == str(smoke_repo)
    (smoke_repo / "current.txt").write_text("current\n", encoding="utf-8")
    committed = run_runner(
        "commit",
        "--task",
        direct["Task"],
        "--lease",
        direct["Lease"],
        "--session",
        "installed-codex",
        "--message",
        "test: commit through installed current worktree runner",
        "--path",
        "current.txt",
    )
    assert committed.returncode == 0, committed.stderr
    prepared = run_runner(
        "ready",
        "--task",
        direct["Task"],
        "--lease",
        direct["Lease"],
        "--session",
        "installed-codex",
    )
    assert prepared.returncode == 0, prepared.stderr
    completed_direct = run_runner(
        "finish",
        "--task",
        direct["Task"],
        "--lease",
        direct["Lease"],
        "--session",
        "installed-codex",
    )
    assert completed_direct.returncode == 0, completed_direct.stderr
    assert (smoke_repo / "current.txt").exists()
    pruned = run_runner("--json", "prune-slot", "--slot", "01")
    assert pruned.returncode == 0, pruned.stderr
    plan = json.loads(pruned.stdout)["result"]
    pruned = run_runner(
        "--json",
        "prune-slot",
        "--slot",
        "01",
        "--plan",
        plan["plan_id"],
        "--confirm",
        plan["digest"],
    )
    assert pruned.returncode == 0, pruned.stderr
    removed_policy = run_runner(
        "deinit",
        "--confirm",
        "DEINIT",
        "--message",
        "test: deinitialize installed runner smoke repository",
    )
    assert removed_policy.returncode == 0, removed_policy.stderr
    assert not (smoke_repo / ".solo-ai").exists()

    removed = call(
        "plugin", "remove", "develop-with-worktrees@develop-with-worktrees", "--json"
    )
    assert removed.returncode == 0, removed.stderr
    removed_marketplace = call(
        "plugin", "marketplace", "remove", "develop-with-worktrees", "--json"
    )
    assert removed_marketplace.returncode == 0, removed_marketplace.stderr
    cache_root = codex_home / "plugins" / "cache" / "develop-with-worktrees"
    assert not cache_root.exists() or not any(cache_root.iterdir())
