from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterable
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

from solo_ai.delegated import approve_delegated, inspect_delegated
from solo_ai.repo import GitRepo


_DWW_TEST_LAYERS = ("dww_fast", "dww_full", "dww_stress")
_DWW_FAST_MODULES = frozenset(
    {
        "tests/unit/test_config.py",
        "tests/unit/test_proof.py",
        "tests/unit/test_routing.py",
        "tests/unit/test_safety.py",
        "tests/unit/test_task_context.py",
        "tests/unit/test_test_layers.py",
    }
)
_TEST_ROOT = Path(__file__).parents[1].resolve()


def dww_test_layer(path: Path) -> str:
    """为未显式标记的测试给出保守层级；新增测试默认进入完整回归。"""

    relative = path.resolve().relative_to(_TEST_ROOT).as_posix()
    return "dww_fast" if relative in _DWW_FAST_MODULES else "dww_full"


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """每个测试恰好属于一个执行层，避免选择器悄悄遗漏安全回归。"""

    violations: list[str] = []
    for item in items:
        layers = [
            name
            for name in _DWW_TEST_LAYERS
            if item.get_closest_marker(name) is not None
        ]
        if not layers:
            item.add_marker(getattr(pytest.mark, dww_test_layer(Path(str(item.path)))))
            continue
        if len(layers) != 1:
            violations.append(f"{item.nodeid}: {', '.join(layers)}")
    if violations:
        raise pytest.UsageError(
            "Every DWW test must have exactly one execution layer:\n"
            + "\n".join(violations[:20])
        )


@pytest.fixture
def directory_link():
    """Windows 用真实 junction；其他平台使用目录 symlink。"""

    def create(link: Path, target: Path) -> None:
        link.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            subprocess.run(
                [
                    "pwsh",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "New-Item -ItemType Junction -Path $env:DWW_TEST_LINK "
                    "-Target $env:DWW_TEST_TARGET | Out-Null",
                ],
                env={
                    **os.environ,
                    "DWW_TEST_LINK": str(link),
                    "DWW_TEST_TARGET": str(target),
                },
                capture_output=True,
                check=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            link.symlink_to(target, target_is_directory=True)

    return create


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
    (scripts / "worktree-flow.ps1").write_text("# mature lifecycle\n", encoding="utf-8")
    (scripts / "dww_adapter.py").write_text(
        f"""import json
import sys
from pathlib import Path

request = json.load(sys.stdin)
operation = request["operation"]
operation_request = request["request"]
if operation == "status":
    if operation_request != {{}}:
        raise SystemExit("status request must be empty")
    result = {{
        "available_slots": {available_slots},
    }}
elif operation == "start":
    if set(operation_request) != {{"name", "request_id"}}:
        raise SystemExit("start request fields are invalid")
    result = {{
        "request_id": operation_request["request_id"],
        "task_id": "0123456789abcdef0123456789abcdef",
        "worktree": str(Path(__file__).resolve().parent / "example-worktree"),
        "slot_id": "slot-01",
        "branch": "codex/example",
        "base_head": "0" * 40,
        "request_reused": False,
    }}
else:
    raise SystemExit("unsupported operation")
json.dump(
    {{
        "schema_version": 1,
        "adapter_id": request["adapter_id"],
        "fingerprint": request["fingerprint"],
        "operation": request["operation"],
        "ok": True,
        "result": result,
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
