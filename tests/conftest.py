from __future__ import annotations

import json
import getpass
import os
import subprocess
import sys
import tempfile
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
        "tests/unit/test_host_context.py",
        "tests/unit/test_host_handoff_state.py",
        "tests/unit/test_test_layers.py",
    }
)
_TEST_ROOT = Path(__file__).parents[1].resolve()


def _managed_worktree_root() -> Path:
    """返回本仓库 DWW 受管工作树的共同目录。"""

    return GitRepo(_TEST_ROOT).primary_path / ".worktrees"


def _is_within(path: Path | str, directory: Path) -> bool:
    """同时按原始和解析路径判断，避免链接绕过临时目录边界。"""

    path = Path(path)
    for candidate in (path.absolute(), path.resolve()):
        try:
            candidate.relative_to(directory.resolve())
        except ValueError:
            continue
        return True
    return False


def _pytest_fallback_temp_root() -> Path:
    """默认系统临时目录不可用或不安全时的受管工作树外回退位置。"""

    return GitRepo(_TEST_ROOT).primary_path / ".tmp" / "pytest-runs"


def _pytest_machine_state_root() -> Path:
    """测试进程专用的机器级状态目录，绝不复用开发者用户配置。"""

    return GitRepo(_TEST_ROOT).primary_path / ".tmp" / "pytest-machine-state"


def _configure_pytest_machine_state() -> None:
    """隔离 Windows 机器级验证队列，避免测试写入用户 AppData。"""

    if os.name == "nt":
        os.environ["LOCALAPPDATA"] = str(_pytest_machine_state_root())


def _is_usable_pytest_temp_root(root: Path) -> bool:
    """确认 pytest 的用户子目录可创建且可扫描。"""

    try:
        user = getpass.getuser() or "unknown"
    except OSError:
        user = "unknown"
    pytest_root = root / f"pytest-of-{user}"
    try:
        pytest_root.mkdir(parents=True, exist_ok=True)
        with os.scandir(pytest_root):
            pass
    except OSError:
        return False
    return True


def _default_pytest_temp_root() -> Path:
    """选择 pytest 编号临时目录的上级，不创建或清理任何目录。"""

    configured = os.environ.get("PYTEST_DEBUG_TEMPROOT")
    try:
        candidate = Path(configured) if configured else Path(tempfile.gettempdir())
    except OSError:
        return _pytest_fallback_temp_root()
    if _is_within(candidate, _managed_worktree_root()):
        return _pytest_fallback_temp_root()
    if not _is_usable_pytest_temp_root(candidate):
        return _pytest_fallback_temp_root()
    return candidate


def _configure_pytest_temp_root(basetemp: Path | str | None) -> Path | None:
    """拒绝不安全显式目录；回退时返回供 pytest 直接使用的短基目录。"""

    if basetemp is not None:
        if _is_within(basetemp, _managed_worktree_root()):
            raise pytest.UsageError(
                f"--basetemp must be outside DWW managed worktrees: {basetemp}"
            )
        return None
    selected = _default_pytest_temp_root()
    if selected == _pytest_fallback_temp_root():
        if not _is_usable_pytest_temp_root(selected):
            raise pytest.UsageError(
                f"Cannot create a safe pytest temporary directory: {selected}"
            )
        # pytest 默认会在调试临时根下再添加用户名和轮次目录。插件安装后的
        # Python 模块路径较深，Windows 非 long-path 环境会因此无法导入模块。
        # 使用该受管回退根内唯一的短 basetemp，既不触及 worktree，也保留
        # pytest 对本次测试临时内容的常规清理责任。
        return selected / "p"
    os.environ["PYTEST_DEBUG_TEMPROOT"] = str(selected)
    return None


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    """让内置临时目录工厂始终在受管工作树之外创建测试产物。"""

    _configure_pytest_machine_state()
    fallback_basetemp = _configure_pytest_temp_root(config.option.basetemp)
    if fallback_basetemp is not None:
        config.option.basetemp = str(fallback_basetemp)


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
