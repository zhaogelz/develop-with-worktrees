from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_ROOT = (
    Path(__file__).parents[1]
    / "plugins"
    / "develop-with-worktrees"
    / "skills"
    / "develop-with-worktrees"
    / "scripts"
)
sys.path.insert(0, str(SCRIPT_ROOT))


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        raise AssertionError(completed.stderr or completed.stdout)
    return completed.stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    root = tmp_path / "example"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Test User")
    git(root, "config", "user.email", "test@example.invalid")
    (root / "README.md").write_text("# Example\n", encoding="utf-8")
    git(root, "add", "README.md")
    git(root, "commit", "-m", "initial")
    return root
