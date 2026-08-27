from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Iterable

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

from solo_ai.delegated import approve_delegated, inspect_delegated
from solo_ai.repo import GitRepo


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


def declare_delegated_adapter(
    root: Path,
    *,
    adapter_id: str = "example-worktree-flow",
    available_slots: int = 2,
    max_parallel: int = 5,
    capabilities: Iterable[str] = ("status",),
    approve: bool = True,
) -> dict[str, object]:
    scripts = root / "scripts"
    policy = root / ".solo-ai"
    scripts.mkdir(exist_ok=True)
    policy.mkdir(exist_ok=True)
    (scripts / "worktree-flow.ps1").write_text(
        "# mature lifecycle\n", encoding="utf-8"
    )
    (scripts / "dww_adapter.py").write_text(
        f"""import json
import sys

request = json.load(sys.stdin)
json.dump(
    {{
        "schema_version": 1,
        "adapter_id": request["adapter_id"],
        "fingerprint": request["fingerprint"],
        "operation": request["operation"],
        "ok": True,
        "result": {{
            "available_slots": {available_slots},
            "received": request["request"],
        }},
    }},
    sys.stdout,
)
""",
        encoding="utf-8",
    )
    capability_list = ", ".join(
        json.dumps(value) for value in sorted(set(capabilities))
    )
    (policy / "delegated.toml").write_text(
        f"""schema_version = 1
id = {json.dumps(adapter_id)}
runtime = "python"
entrypoint = "scripts/dww_adapter.py"
workflow_markers = ["scripts/worktree-flow.ps1"]
tracked_inputs = ["scripts/dww_adapter.py", "scripts/worktree-flow.ps1"]
capabilities = [{capability_list}]
max_parallel = {max_parallel}
""",
        encoding="utf-8",
    )
    git(root, "add", ".solo-ai/delegated.toml", "scripts")
    git(root, "commit", "-m", "declare delegated adapter")
    repo = GitRepo(root)
    inspection = inspect_delegated(repo.root, repo.common_dir)
    if approve:
        approve_delegated(
            repo.root,
            repo.common_dir,
            fingerprint=str(inspection["adapter"]["fingerprint"]),
        )
    return inspection


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
