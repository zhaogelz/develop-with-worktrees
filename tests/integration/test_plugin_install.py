from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from solo_ai.state import STATE_SCHEMA


@pytest.mark.dww_stress
def test_plugin_install_and_clean_uninstall_in_temporary_codex_home(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Exercise Codex's real local marketplace install/remove flow, never the user's home."""
    if os.environ.get("DWW_SKIP_CODEX_CLI_INTEGRATION") == "1":
        pytest.skip("Codex CLI install integration is exercised on a local Codex host")
    # 缩短缓存根目录，避免仍受 MAX_PATH 约束的 Windows Python 无法导入深层模块。
    tmp_path = tmp_path_factory.mktemp("plugin")
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
    local_app_data = tmp_path / "local-app-data"
    local_app_data.mkdir()
    environment = {
        **os.environ,
        "CODEX_HOME": str(codex_home),
        # 安装后的 `version` 会读取机器验证队列。真实安装测试必须隔离该
        # 机器级状态，避免访问或污染运行测试的用户配置目录。
        "LOCALAPPDATA": str(local_app_data),
    }

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
    assert installed_hook_definitions, (
        "installed plugin does not expose its hook definition"
    )
    source_hook_definition = source / "hooks" / "hooks.json"
    assert hashlib.sha256(installed_hook_definitions[0].read_bytes()).digest() == (
        hashlib.sha256(source_hook_definition.read_bytes()).digest()
    )
    installed_hook_runners = [
        path
        for path in codex_home.rglob("worktree_guard.py")
        if "develop-with-worktrees" in str(path).replace("\\", "/")
        and path.parent.name == "hooks"
    ]
    assert installed_hook_runners, "installed plugin does not expose its Hook runner"
    source_hook_runner = source / "hooks" / "worktree_guard.py"
    assert hashlib.sha256(installed_hook_runners[0].read_bytes()).digest() == (
        hashlib.sha256(source_hook_runner.read_bytes()).digest()
    )

    runners = [
        path
        for path in codex_home.rglob("dww.py")
        if "develop-with-worktrees" in str(path).replace("\\", "/")
    ]
    assert runners, "installed plugin does not expose its lifecycle runner"
    runner = runners[0]
    installed_manifests = [
        path
        for path in codex_home.rglob("plugin.json")
        if path.parent.name == ".codex-plugin"
        and "develop-with-worktrees" in str(path).replace("\\", "/")
    ]
    assert len(installed_manifests) == 1, "installed plugin root is ambiguous"
    installed_root = installed_manifests[0].parent.parent
    reference_paths = tuple(
        path.relative_to(source).as_posix()
        for path in sorted(
            (source / "skills" / "develop-with-worktrees" / "references").glob("*.md")
        )
    )
    shipped_paths = (
        ".codex-plugin/plugin.json",
        "maintain-dww-plugin.ps1",
        "skills/develop-with-worktrees/SKILL.md",
        *reference_paths,
        "skills/develop-with-worktrees/scripts/dww.py",
        "skills/develop-with-worktrees/scripts/solo_ai/candidate_batches.py",
        "skills/develop-with-worktrees/scripts/solo_ai/cli.py",
        "skills/develop-with-worktrees/scripts/solo_ai/config.py",
        "skills/develop-with-worktrees/scripts/solo_ai/lifecycle.py",
        "skills/develop-with-worktrees/scripts/solo_ai/root_context.py",
        "skills/develop-with-worktrees/scripts/solo_ai/state.py",
        "skills/develop-with-worktrees/scripts/solo_ai/host_context.py",
        "skills/develop-with-worktrees/scripts/solo_ai/task_context.py",
    )
    for relative_path in shipped_paths:
        source_path = source / relative_path
        installed_path = installed_root / relative_path
        assert installed_path.is_file(), f"installed plugin misses {relative_path}"
        assert hashlib.sha256(installed_path.read_bytes()).digest() == (
            hashlib.sha256(source_path.read_bytes()).digest()
        ), f"installed plugin differs from source at {relative_path}"
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
    version_payload = json.loads(version.stdout)["result"]
    assert version_payload["version"] == expected_version.partition("+codex.")[0]
    assert version_payload["plugin_version"] == expected_version
    assert version_payload["verification_schema"] == 3
    assert version_payload["state_schema"] == STATE_SCHEMA
    root_plan = smoke_repo / "installed-root-plan.md"
    root_plan.write_text(
        "# 已安装完整方案 V1\n\n用于验证非交互式 UTF-8 输出。\n",
        encoding="utf-8",
    )
    acceptance_index = smoke_repo / "installed-root-index.json"
    acceptance_index.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "id": "A01",
                        "locator": "installed root plan",
                        "quote": "UTF-8",
                        "required": True,
                        "plan_version": None,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    created_root = run_runner(
        "--json",
        "root-anchor",
        "create",
        "--purpose",
        "exercise the installed structured root lifecycle",
        "--target",
        "verify an installed runner appends and reviews a root plan",
        "--scope",
        "temporary installation smoke repository only",
        "--acceptance",
        "the installed runner publishes and closes against the current plan version",
        "--plan-file",
        str(root_plan),
        "--plan-source",
        "installed test confirmed v1",
        "--acceptance-index-file",
        str(acceptance_index),
        "--request-id",
        "installed-runner-root-smoke",
    )
    assert created_root.returncode == 0, created_root.stderr
    created_root_payload = json.loads(created_root.stdout)["result"]
    assert "content" not in created_root_payload
    assert "confirmed_plan" not in created_root_payload
    root_id = created_root_payload["root_id"]
    root_plan.unlink()
    started = run_runner(
        "start",
        "--name",
        "installed artifact smoke",
        "--target",
        "initial installed target",
        "--scope",
        "initial installed scope",
        "--acceptance",
        "initial installed acceptance",
        "--root-anchor",
        root_id,
    )
    assert started.returncode == 0, started.stderr
    headers, separator, root_body = started.stdout.partition(
        "\n\nRoot anchor (complete plan):\n"
    )
    assert separator
    assert root_body.count("# 已安装完整方案 V1") == 1
    assert "用于验证非交互式 UTF-8 输出。" in root_body
    values = dict(line.split(": ", 1) for line in headers.splitlines())
    task_id = values["Task"]
    lease = values["Lease"]
    worktree = Path(values["Worktree"])
    root_change = smoke_repo / "installed-root-change.md"
    root_change.write_text("Installed V2 exact correction.\n", encoding="utf-8")
    amended_root = run_runner(
        "--json",
        "root-anchor",
        "amend",
        "--root",
        root_id,
        "--change-file",
        str(root_change),
        "--source",
        "installed test confirmed v2",
        "--summary",
        "append the installed exact correction",
        "--acceptance-index-file",
        str(acceptance_index),
        "--expected-sha256",
        created_root_payload["sha256"],
        "--content",
    )
    assert amended_root.returncode == 0, amended_root.stderr
    root_change.unlink()
    acceptance_index.unlink()
    amended_payload = json.loads(amended_root.stdout)["result"]
    assert amended_payload["content"].count("Installed V2 exact correction.") == 1
    assert "confirmed_plan" not in amended_payload
    refreshed_root = run_runner(
        "--json",
        "anchor",
        "refresh-root",
        "--task",
        task_id,
        "--lease",
        lease,
        cwd=worktree,
    )
    assert refreshed_root.returncode == 0, refreshed_root.stderr
    assert (
        json.loads(refreshed_root.stdout)["result"]["root_plan_review"]["record_kind"]
        == "read"
    )
    refreshed_payload = json.loads(refreshed_root.stdout)["result"]
    assert "initial installed target" in refreshed_payload["task_anchor"]["content"]
    assert (
        refreshed_payload["root_anchor"]["content"].count(
            "Installed V2 exact correction."
        )
        == 1
    )
    assert "confirmed_plan" not in refreshed_payload["root_anchor"]
    shown_anchor = run_runner(
        "--json", "anchor", "show", "--task", task_id, "--content", cwd=worktree
    )
    assert shown_anchor.returncode == 0, shown_anchor.stderr
    shown_anchor_payload = json.loads(shown_anchor.stdout)["result"]
    anchor_input = worktree / "anchor-input.md"
    anchor_content = shown_anchor_payload["content"]
    anchor_content = anchor_content.replace(
        "- Implementation target: initial installed target",
        "- Implementation target: installed anchor update",
    )
    anchor_content = anchor_content.replace(
        "- Scope boundary: initial installed scope",
        "- Scope boundary: plugin install smoke test",
    )
    anchor_content = anchor_content.replace(
        "- Acceptance criteria: initial installed acceptance",
        "- Acceptance criteria: installed show and update pass",
    )
    anchor_content = anchor_content.replace(
        "- Current progress: task started",
        "- Current progress: installed anchor verified",
    )
    anchor_input.write_text(anchor_content, encoding="utf-8", newline="\n")
    updated_anchor = run_runner(
        "--json",
        "anchor",
        "update",
        "--task",
        task_id,
        "--lease",
        lease,
        "--file",
        str(anchor_input),
        "--expected-sha256",
        shown_anchor_payload["sha256"],
        cwd=worktree,
    )
    assert updated_anchor.returncode == 0, updated_anchor.stderr
    assert json.loads(updated_anchor.stdout)["result"]["changed"] is True
    anchor_input.unlink()
    refreshed_anchor = run_runner(
        "--json", "anchor", "show", "--task", task_id, "--content", cwd=worktree
    )
    assert refreshed_anchor.returncode == 0, refreshed_anchor.stderr
    assert json.loads(refreshed_anchor.stdout)["result"]["content"] == anchor_content
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
    finished = run_runner(
        "--json", "finish", "--task", task_id, "--lease", lease, cwd=worktree
    )
    assert finished.returncode == 0, finished.stderr
    candidate_id = json.loads(finished.stdout)["result"]["candidate_id"]
    tail = run_runner(
        "--json",
        "batch",
        "seal",
        "--candidate",
        candidate_id,
        "--cause",
        "round-complete",
        "--reason",
        "the installation smoke task is the complete round",
    )
    assert tail.returncode == 0, tail.stderr
    assert json.loads(tail.stdout)["result"]["status"] == "completed"
    assert (smoke_repo / "smoke.txt").exists()
    root_evidence = smoke_repo / "installed-root-evidence.json"
    root_evidence.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "id": "A01",
                        "status": "passed",
                        "observation": "Installed V2 was checked after local delivery.",
                        "evidence": "installed runner smoke delivery",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    current_root = run_runner("--json", "root-anchor", "show", "--root", root_id)
    assert current_root.returncode == 0, current_root.stderr
    accepted_root = run_runner(
        "root-anchor",
        "accept",
        "--root",
        root_id,
        "--status",
        "accepted",
        "--evidence-file",
        str(root_evidence),
        "--expected-sha256",
        json.loads(current_root.stdout)["result"]["sha256"],
    )
    assert accepted_root.returncode == 0, accepted_root.stderr
    root_evidence.unlink()
    closed_root = run_runner(
        "root-anchor", "close", "--root", root_id, "--confirm", root_id
    )
    assert closed_root.returncode == 0, closed_root.stderr
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
