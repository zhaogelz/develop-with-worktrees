from __future__ import annotations

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
