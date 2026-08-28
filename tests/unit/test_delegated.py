from __future__ import annotations

import ctypes
import dis
import errno
import gc
import inspect
import json
import os
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import declare_delegated_adapter, git
from solo_ai import delegated
from solo_ai.delegated import (
    ALLOWED_CAPABILITIES,
    DelegatedContractError,
    approve_delegated,
    inspect_delegated,
    invoke_delegated,
    revoke_delegated,
)
from solo_ai.lifecycle import repository_route
from solo_ai.repo import GitRepo


def assert_posix_descriptors_closed(descriptors: list[int]) -> None:
    assert descriptors
    for descriptor in descriptors:
        with pytest.raises(OSError) as raised:
            os.fstat(descriptor)
        assert raised.value.errno == errno.EBADF


def declare_adapter(root: Path, *, max_parallel: int = 4) -> None:
    declare_delegated_adapter(
        root,
        max_parallel=max_parallel,
        capabilities=("start", "status"),
        approve=False,
    )


def declare_runtime_adapter(
    root: Path,
    *,
    runtime: str,
    entrypoint: str,
    files: dict[str, str],
    max_parallel: int = 5,
) -> tuple[GitRepo, dict[str, object]]:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    tracked_inputs = sorted(files)
    policy = root / ".solo-ai"
    policy.mkdir(exist_ok=True)
    (policy / "delegated.toml").write_text(
        "schema_version = 1\n"
        'id = "runtime-closure-test"\n'
        f'runtime = {json.dumps(runtime)}\n'
        f'entrypoint = {json.dumps(entrypoint)}\n'
        'workflow_markers = ["scripts/worktree-flow.ps1"]\n'
        f"tracked_inputs = {json.dumps(tracked_inputs)}\n"
        'capabilities = ["status"]\n'
        f"max_parallel = {max_parallel}\n",
        encoding="utf-8",
        newline="\n",
    )
    git(root, "add", ".solo-ai/delegated.toml", *tracked_inputs)
    git(root, "commit", "-m", f"declare {runtime} closure fixture")
    repo = GitRepo(root)
    inspection = inspect_delegated(repo.root, repo.common_dir)
    assert inspection["valid"] is True
    approve_delegated(
        repo.root,
        repo.common_dir,
        fingerprint=inspection["adapter"]["fingerprint"],
    )
    return repo, inspection


def exception_tree_contains(
    error: BaseException, expected_type: type[BaseException], text: str
) -> bool:
    if isinstance(error, expected_type) and text in str(error):
        return True
    if isinstance(error, BaseExceptionGroup) and any(
        exception_tree_contains(item, expected_type, text)
        for item in error.exceptions
    ):
        return True
    if error.__cause__ is not None:
        return exception_tree_contains(error.__cause__, expected_type, text)
    return False


def test_exact_local_approval_enables_delegated_route_and_limits_slots(
    git_repo: Path,
) -> None:
    declare_adapter(git_repo, max_parallel=3)
    repo = GitRepo(git_repo)

    inspection = inspect_delegated(repo.root, repo.common_dir)
    assert inspection["valid"] is True
    assert inspection["approved"] is False
    assert repository_route(repo)["reason"] == "delegated-approval-required"

    approved = approve_delegated(
        repo.root,
        repo.common_dir,
        fingerprint=inspection["adapter"]["fingerprint"],
    )

    assert approved["approved"] is True
    route = repository_route(repo)
    assert route["action"] == "delegated"
    assert route["adapter"]["id"] == "example-worktree-flow"
    assert route["adapter"]["max_parallel"] == 3


def test_tracked_input_drift_invalidates_approval_without_executing(
    git_repo: Path,
) -> None:
    declare_adapter(git_repo)
    repo = GitRepo(git_repo)
    before = inspect_delegated(repo.root, repo.common_dir)
    approve_delegated(
        repo.root,
        repo.common_dir,
        fingerprint=before["adapter"]["fingerprint"],
    )

    (git_repo / "scripts" / "worktree-flow.ps1").write_text(
        "# changed lifecycle\n", encoding="utf-8"
    )

    after = inspect_delegated(repo.root, repo.common_dir)
    assert after["valid"] is True
    assert after["approved"] is False
    assert after["adapter"]["fingerprint"] != before["adapter"]["fingerprint"]
    assert repository_route(repo)["action"] == "defer"
    assert repository_route(repo)["reason"] == "delegated-approval-required"


def test_marker_set_drift_fails_closed(git_repo: Path) -> None:
    declare_adapter(git_repo)
    repo = GitRepo(git_repo)
    (git_repo / ".sdd").mkdir()

    inspection = inspect_delegated(repo.root, repo.common_dir)

    assert inspection["valid"] is False
    assert "exactly match" in inspection["error"]
    route = repository_route(repo)
    assert route["action"] == "defer"
    assert route["reason"] == "delegated-invalid-contract"


def test_invoke_uses_approved_json_envelope(git_repo: Path) -> None:
    declare_adapter(git_repo)
    repo = GitRepo(git_repo)
    inspection = inspect_delegated(repo.root, repo.common_dir)
    approve_delegated(
        repo.root,
        repo.common_dir,
        fingerprint=inspection["adapter"]["fingerprint"],
    )

    response = invoke_delegated(
        repo.root,
        repo.common_dir,
        operation="status",
        request={},
        timeout_seconds=30,
    )

    assert response["ok"] is True
    assert response["result"] == {"available_slots": 2}


def test_invoke_never_executes_entrypoint_drift_after_final_approval_check(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    declare_adapter(git_repo)
    repo = GitRepo(git_repo)
    inspection = inspect_delegated(repo.root, repo.common_dir)
    approve_delegated(
        repo.root,
        repo.common_dir,
        fingerprint=inspection["adapter"]["fingerprint"],
    )
    adapter_path = git_repo / "scripts" / "dww_adapter.py"
    approved_source = adapter_path.read_text(encoding="utf-8")
    marker = git_repo / "unapproved-adapter-ran.txt"
    original_run = delegated._run_adapter_process

    def drift_then_spawn(*args: object, **kwargs: object) -> object:
        adapter_path.write_text(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('ran', encoding='utf-8')\n"
            + approved_source,
            encoding="utf-8",
        )
        return original_run(*args, **kwargs)

    monkeypatch.setattr(delegated, "_run_adapter_process", drift_then_spawn)

    response = invoke_delegated(
        repo.root,
        repo.common_dir,
        operation="status",
        request={},
        timeout_seconds=30,
    )

    assert response["ok"] is True
    assert response["result"] == {"available_slots": 2}
    assert adapter_path.read_text(encoding="utf-8") != approved_source
    assert not marker.exists()


def test_verified_python_input_closure_preserves_repo_root_and_sibling_imports(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    declare_adapter(git_repo)
    adapter_path = git_repo / "scripts" / "dww_adapter.py"
    helper_path = git_repo / "scripts" / "adapter_helper.py"
    helper_path.write_text(
        "from pathlib import Path\n"
        "AVAILABLE_SLOTS = 3\n"
        "HELPER_SOURCE = str(Path(__file__).resolve())\n",
        encoding="utf-8",
    )
    adapter_path.write_text(
        """import json
import os
import sys
from pathlib import Path

from adapter_helper import AVAILABLE_SLOTS, HELPER_SOURCE

verified_root = Path(os.environ["DWW_VERIFIED_INPUT_ROOT"])
repository_root = Path(os.environ["DWW_REPOSITORY_ROOT"])
if repository_root.resolve() != Path.cwd().resolve():
    raise SystemExit("repository working directory semantics changed")
if Path(__file__).resolve() != verified_root / "scripts" / "dww_adapter.py":
    raise SystemExit("entrypoint does not come from the verified closure")
if HELPER_SOURCE != str(verified_root / "scripts" / "adapter_helper.py"):
    raise SystemExit("sibling import does not come from the verified closure")
request = json.load(sys.stdin)
json.dump(
    {
        "schema_version": 1,
        "adapter_id": request["adapter_id"],
        "fingerprint": request["fingerprint"],
        "operation": request["operation"],
        "ok": True,
        "result": {"available_slots": AVAILABLE_SLOTS},
    },
    sys.stdout,
)
""",
        encoding="utf-8",
    )
    contract_path = git_repo / ".solo-ai" / "delegated.toml"
    contract_path.write_text(
        contract_path.read_text(encoding="utf-8").replace(
            'tracked_inputs = ["scripts/dww_adapter.py", "scripts/worktree-flow.ps1"]',
            "tracked_inputs = ["
            '"scripts/adapter_helper.py", '
            '"scripts/dww_adapter.py", '
            '"scripts/worktree-flow.ps1"]',
        ),
        encoding="utf-8",
    )
    git(git_repo, "add", ".solo-ai/delegated.toml", "scripts")
    git(git_repo, "commit", "-m", "add adapter import fixture")
    repo = GitRepo(git_repo)
    inspection = inspect_delegated(repo.root, repo.common_dir)
    approve_delegated(
        repo.root,
        repo.common_dir,
        fingerprint=inspection["adapter"]["fingerprint"],
    )
    snapshots: list[Path] = []
    closure_roots: list[Path] = []
    original_run = delegated._run_adapter_process

    def capture_snapshot(*args: object, **kwargs: object) -> object:
        argv = args[0]
        assert isinstance(argv, list)
        snapshots.append(Path(argv[-1]))
        environment = kwargs["environment"]
        assert isinstance(environment, dict)
        closure_root = Path(environment["DWW_VERIFIED_INPUT_ROOT"])
        closure_roots.append(closure_root)
        with pytest.raises(ValueError):
            closure_root.resolve().relative_to(git_repo.resolve())
        assert snapshots[-1] == closure_root / "scripts" / "dww_adapter.py"
        assert Path(environment["DWW_REPOSITORY_ROOT"]).resolve() == git_repo.resolve()
        assert (closure_root / "scripts" / "adapter_helper.py").read_text(
            encoding="utf-8"
        ) == helper_path.read_text(encoding="utf-8")
        assert "dww-verified" not in git(git_repo, "status", "--short")
        return original_run(*args, **kwargs)

    monkeypatch.setattr(delegated, "_run_adapter_process", capture_snapshot)

    response = invoke_delegated(
        repo.root,
        repo.common_dir,
        operation="status",
        request={},
        timeout_seconds=30,
    )

    assert response["result"] == {"available_slots": 3}
    assert len(snapshots) == 1
    assert snapshots[0] != adapter_path
    assert snapshots[0].suffix == ".py"
    assert len(closure_roots) == 1
    assert not closure_roots[0].exists()
    assert git(git_repo, "status", "--short") == ""


def test_invoke_never_executes_python_helper_drift_after_final_approval_check(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    declare_adapter(git_repo)
    adapter_path = git_repo / "scripts" / "dww_adapter.py"
    helper_path = git_repo / "scripts" / "adapter_helper.py"
    marker = git_repo / "unapproved-python-helper-ran.txt"
    helper_path.write_text("AVAILABLE_SLOTS = 3\n", encoding="utf-8")
    adapter_path.write_text(
        """import json
import sys

from adapter_helper import AVAILABLE_SLOTS

request = json.load(sys.stdin)
json.dump(
    {
        "schema_version": 1,
        "adapter_id": request["adapter_id"],
        "fingerprint": request["fingerprint"],
        "operation": request["operation"],
        "ok": True,
        "result": {"available_slots": AVAILABLE_SLOTS},
    },
    sys.stdout,
)
""",
        encoding="utf-8",
    )
    contract_path = git_repo / ".solo-ai" / "delegated.toml"
    contract_path.write_text(
        contract_path.read_text(encoding="utf-8").replace(
            'tracked_inputs = ["scripts/dww_adapter.py", "scripts/worktree-flow.ps1"]',
            "tracked_inputs = ["
            '"scripts/adapter_helper.py", '
            '"scripts/dww_adapter.py", '
            '"scripts/worktree-flow.ps1"]',
        ),
        encoding="utf-8",
    )
    git(git_repo, "add", ".solo-ai/delegated.toml", "scripts")
    git(git_repo, "commit", "-m", "add approved python helper")
    repo = GitRepo(git_repo)
    inspection = inspect_delegated(repo.root, repo.common_dir)
    approve_delegated(
        repo.root,
        repo.common_dir,
        fingerprint=inspection["adapter"]["fingerprint"],
    )
    original_run = delegated._run_adapter_process

    def drift_then_spawn(*args: object, **kwargs: object) -> object:
        helper_path.write_text(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('ran', encoding='utf-8')\n"
            "AVAILABLE_SLOTS = 1\n",
            encoding="utf-8",
        )
        return original_run(*args, **kwargs)

    monkeypatch.setattr(delegated, "_run_adapter_process", drift_then_spawn)

    response = invoke_delegated(
        repo.root,
        repo.common_dir,
        operation="status",
        request={},
        timeout_seconds=30,
    )

    assert response["result"] == {"available_slots": 3}
    assert not marker.exists()


def test_python_pep723_metadata_is_loaded_from_the_verified_closure(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    declare_adapter(git_repo)
    adapter_path = git_repo / "scripts" / "dww_adapter.py"
    approved_source = """# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
import json
import sys

request = json.load(sys.stdin)
json.dump(
    {
        "schema_version": 1,
        "adapter_id": request["adapter_id"],
        "fingerprint": request["fingerprint"],
        "operation": request["operation"],
        "ok": True,
        "result": {"available_slots": 2},
    },
    sys.stdout,
)
"""
    adapter_path.write_text(approved_source, encoding="utf-8")
    git(git_repo, "add", "scripts/dww_adapter.py")
    git(git_repo, "commit", "-m", "add approved PEP 723 metadata")
    repo = GitRepo(git_repo)
    inspection = inspect_delegated(repo.root, repo.common_dir)
    approve_delegated(
        repo.root,
        repo.common_dir,
        fingerprint=inspection["adapter"]["fingerprint"],
    )
    original_run = delegated._run_adapter_process

    def drift_then_spawn(*args: object, **kwargs: object) -> object:
        adapter_path.write_text(
            approved_source.replace('requires-python = ">=3.11"', 'requires-python = ">=99"'),
            encoding="utf-8",
        )
        return original_run(*args, **kwargs)

    monkeypatch.setattr(delegated, "_run_adapter_process", drift_then_spawn)

    response = invoke_delegated(
        repo.root,
        repo.common_dir,
        operation="status",
        request={},
        timeout_seconds=30,
    )

    assert response["result"] == {"available_slots": 2}


def test_contract_bytes_are_available_only_from_the_verified_closure(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    declare_adapter(git_repo, max_parallel=4)
    adapter_path = git_repo / "scripts" / "dww_adapter.py"
    adapter_path.write_text(
        """import json
import os
import sys
import tomllib
from pathlib import Path

contract = tomllib.loads(
    (Path(os.environ["DWW_VERIFIED_INPUT_ROOT"]) / ".solo-ai" / "delegated.toml")
    .read_text(encoding="utf-8")
)
request = json.load(sys.stdin)
json.dump(
    {
        "schema_version": 1,
        "adapter_id": request["adapter_id"],
        "fingerprint": request["fingerprint"],
        "operation": request["operation"],
        "ok": True,
        "result": {"available_slots": contract["max_parallel"]},
    },
    sys.stdout,
)
""",
        encoding="utf-8",
    )
    git(git_repo, "add", "scripts/dww_adapter.py")
    git(git_repo, "commit", "-m", "read approved contract from closure")
    repo = GitRepo(git_repo)
    inspection = inspect_delegated(repo.root, repo.common_dir)
    approve_delegated(
        repo.root,
        repo.common_dir,
        fingerprint=inspection["adapter"]["fingerprint"],
    )
    contract_path = git_repo / ".solo-ai" / "delegated.toml"
    original_run = delegated._run_adapter_process

    def drift_then_spawn(*args: object, **kwargs: object) -> object:
        contract_path.write_text(
            contract_path.read_text(encoding="utf-8").replace(
                "max_parallel = 4", "max_parallel = 1"
            ),
            encoding="utf-8",
        )
        return original_run(*args, **kwargs)

    monkeypatch.setattr(delegated, "_run_adapter_process", drift_then_spawn)

    response = invoke_delegated(
        repo.root,
        repo.common_dir,
        operation="status",
        request={},
        timeout_seconds=30,
    )

    assert response["result"] == {"available_slots": 4}


@pytest.mark.skipif(
    not (shutil.which("pwsh") or shutil.which("powershell.exe")),
    reason="PowerShell runtime is unavailable",
)
@pytest.mark.parametrize(
    ("drift_relative", "drift_content"),
    (
        (
            "scripts/worktree-flow.ps1",
            "Set-Content -LiteralPath (Join-Path $env:DWW_REPOSITORY_ROOT "
            "'unapproved-powershell-input-ran.txt') -Value 'ran'\n"
            "function Get-ControllerSlots { 0 }\n",
        ),
        (
            "scripts/AdapterSupport.psm1",
            "Set-Content -LiteralPath (Join-Path $env:DWW_REPOSITORY_ROOT "
            "'unapproved-powershell-input-ran.txt') -Value 'ran'\n"
            "function Get-ModuleSlots { 0 }\nExport-ModuleMember -Function Get-ModuleSlots\n",
        ),
        ("config/adapter.json", '{"slots": 0}\n'),
    ),
)
def test_powershell_controller_module_and_config_drift_use_verified_closure(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift_relative: str,
    drift_content: str,
) -> None:
    files = {
        "scripts/dww_adapter.ps1": """$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'worktree-flow.ps1')
Import-Module (Join-Path $PSScriptRoot 'AdapterSupport.psm1') -Force
$config = Get-Content -Raw -LiteralPath (Join-Path $env:DWW_VERIFIED_INPUT_ROOT 'config/adapter.json') | ConvertFrom-Json
if ((Resolve-Path -LiteralPath '.').Path -ne (Resolve-Path -LiteralPath $env:DWW_REPOSITORY_ROOT).Path) { throw 'repository cwd changed' }
$request = [Console]::In.ReadToEnd() | ConvertFrom-Json
$response = [ordered]@{
  schema_version = 1
  adapter_id = $request.adapter_id
  fingerprint = $request.fingerprint
  operation = $request.operation
  ok = $true
  result = [ordered]@{ available_slots = ((Get-ControllerSlots) + (Get-ModuleSlots) + [int]$config.slots) }
}
[Console]::Out.Write(($response | ConvertTo-Json -Compress -Depth 10))
""",
        "scripts/worktree-flow.ps1": "function Get-ControllerSlots { 1 }\n",
        "scripts/AdapterSupport.psm1": (
            "function Get-ModuleSlots { 1 }\n"
            "Export-ModuleMember -Function Get-ModuleSlots\n"
        ),
        "config/adapter.json": '{"slots": 1}\n',
    }
    repo, _inspection = declare_runtime_adapter(
        git_repo,
        runtime="powershell",
        entrypoint="scripts/dww_adapter.ps1",
        files=files,
    )
    marker = git_repo / "unapproved-powershell-input-ran.txt"
    drift_path = git_repo / drift_relative
    original_run = delegated._run_adapter_process

    def drift_then_spawn(*args: object, **kwargs: object) -> object:
        drift_path.write_text(drift_content, encoding="utf-8")
        return original_run(*args, **kwargs)

    monkeypatch.setattr(delegated, "_run_adapter_process", drift_then_spawn)

    response = invoke_delegated(
        repo.root,
        repo.common_dir,
        operation="status",
        request={},
        timeout_seconds=30,
    )

    assert response["result"] == {"available_slots": 3}
    assert not marker.exists()


@pytest.mark.skipif(not shutil.which("sh"), reason="sh runtime is unavailable")
def test_shell_helper_drift_uses_verified_directory_semantics(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = {
        "scripts/dww_adapter.sh": """#!/bin/sh
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$SCRIPT_DIR/adapter_helper.sh"
payload=$(cat)
AVAILABLE_SLOTS=$(approved_slots)
export AVAILABLE_SLOTS
printf '%s' "$payload" | "$DWW_TEST_PYTHON" -c 'import json,os,sys; r=json.load(sys.stdin); json.dump({"schema_version":1,"adapter_id":r["adapter_id"],"fingerprint":r["fingerprint"],"operation":r["operation"],"ok":True,"result":{"available_slots":int(os.environ["AVAILABLE_SLOTS"])}},sys.stdout)'
""",
        "scripts/adapter_helper.sh": "approved_slots() { printf '3'; }\n",
        "scripts/worktree-flow.ps1": "# mature lifecycle marker\n",
    }
    repo, _inspection = declare_runtime_adapter(
        git_repo,
        runtime="sh",
        entrypoint="scripts/dww_adapter.sh",
        files=files,
    )
    marker = git_repo / "unapproved-shell-helper-ran.txt"
    helper_path = git_repo / "scripts" / "adapter_helper.sh"
    monkeypatch.setenv("DWW_TEST_PYTHON", sys.executable)
    original_run = delegated._run_adapter_process

    def drift_then_spawn(*args: object, **kwargs: object) -> object:
        helper_path.write_text(
            "printf 'ran' > \"$DWW_REPOSITORY_ROOT/unapproved-shell-helper-ran.txt\"\n"
            "approved_slots() { printf '1'; }\n",
            encoding="utf-8",
        )
        return original_run(*args, **kwargs)

    monkeypatch.setattr(delegated, "_run_adapter_process", drift_then_spawn)

    response = invoke_delegated(
        repo.root,
        repo.common_dir,
        operation="status",
        request={},
        timeout_seconds=30,
    )

    assert response["result"] == {"available_slots": 3}
    assert not marker.exists()


@pytest.mark.parametrize(
    ("runtime", "suffix"),
    (("python", ".py"), ("powershell", ".ps1"), ("sh", ".sh")),
)
def test_verified_input_closure_keeps_cross_runtime_directory_semantics(
    tmp_path: Path, runtime: str, suffix: str
) -> None:
    root = tmp_path / "repo"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    entrypoint = scripts / f"adapter{suffix}"
    content = b"approved entrypoint bytes\n"
    entrypoint.write_bytes(content)
    contract = delegated.DelegatedContract(
        adapter_id="portable-adapter",
        runtime=runtime,
        entrypoint=f"scripts/adapter{suffix}",
        workflow_markers=("scripts/worktree-flow.ps1",),
        tracked_inputs=(f"scripts/adapter{suffix}",),
        capabilities=("status",),
        max_parallel=1,
        fingerprint="0" * 64,
    )

    material = delegated.DelegatedMaterial(
        contract_bytes=b"approved contract bytes\n",
        tracked_input_bytes=((f"scripts/adapter{suffix}", content),),
    )

    with delegated._verified_input_closure(root, contract, material) as closure:
        with pytest.raises(ValueError):
            closure.root.resolve().relative_to(root.resolve())
        assert closure.entrypoint == closure.root / "scripts" / f"adapter{suffix}"
        assert closure.entrypoint.suffix == suffix
        assert closure.entrypoint.read_bytes() == content
        assert (closure.root / ".solo-ai" / "delegated.toml").read_bytes() == (
            material.contract_bytes
        )

    assert not closure.root.exists()


def test_verified_input_closure_is_removed_when_launch_fails(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    declare_adapter(git_repo)
    repo = GitRepo(git_repo)
    inspection = inspect_delegated(repo.root, repo.common_dir)
    approve_delegated(
        repo.root,
        repo.common_dir,
        fingerprint=inspection["adapter"]["fingerprint"],
    )
    closure_roots: list[Path] = []

    def fail_launch(*args: object, **kwargs: object) -> object:
        argv = args[0]
        assert isinstance(argv, list)
        environment = kwargs["environment"]
        assert isinstance(environment, dict)
        closure_roots.append(Path(environment["DWW_VERIFIED_INPUT_ROOT"]))
        assert Path(argv[-1]).exists()
        raise DelegatedContractError("synthetic launch failure")

    monkeypatch.setattr(delegated, "_run_adapter_process", fail_launch)

    with pytest.raises(DelegatedContractError, match="synthetic launch failure"):
        invoke_delegated(
            repo.root,
            repo.common_dir,
            operation="status",
            request={},
            timeout_seconds=30,
        )

    assert len(closure_roots) == 1
    assert not closure_roots[0].exists()


def test_changed_verified_input_path_is_preserved_instead_of_deleted(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    declare_adapter(git_repo)
    repo = GitRepo(git_repo)
    inspection = inspect_delegated(repo.root, repo.common_dir)
    approve_delegated(
        repo.root,
        repo.common_dir,
        fingerprint=inspection["adapter"]["fingerprint"],
    )
    replacements: list[Path] = []
    closure_roots: list[Path] = []

    def replace_snapshot(*args: object, **kwargs: object) -> object:
        argv = args[0]
        assert isinstance(argv, list)
        environment = kwargs["environment"]
        assert isinstance(environment, dict)
        closure_roots.append(Path(environment["DWW_VERIFIED_INPUT_ROOT"]))
        snapshot = Path(argv[-1])
        snapshot.unlink()
        snapshot.write_text("replacement must survive\n", encoding="utf-8")
        replacements.append(snapshot)
        raise KeyboardInterrupt("synthetic invocation interruption")

    monkeypatch.setattr(delegated, "_run_adapter_process", replace_snapshot)

    with pytest.raises(BaseExceptionGroup) as raised:
        invoke_delegated(
            repo.root,
            repo.common_dir,
            operation="status",
            request={},
            timeout_seconds=30,
        )

    assert exception_tree_contains(
        raised.value, KeyboardInterrupt, "synthetic invocation interruption"
    )
    assert exception_tree_contains(
        raised.value, DelegatedContractError, "changed path was preserved"
    )
    assert len(replacements) == 1
    assert replacements[0].read_text(encoding="utf-8") == "replacement must survive\n"
    assert len(closure_roots) == 1
    assert closure_roots[0].exists()
    shutil.rmtree(closure_roots[0])


def test_verified_input_closure_is_removed_after_adapter_timeout(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    declare_adapter(git_repo)
    adapter_path = git_repo / "scripts" / "dww_adapter.py"
    adapter_path.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    git(git_repo, "add", "scripts/dww_adapter.py")
    git(git_repo, "commit", "-m", "add timeout adapter fixture")
    repo = GitRepo(git_repo)
    inspection = inspect_delegated(repo.root, repo.common_dir)
    approve_delegated(
        repo.root,
        repo.common_dir,
        fingerprint=inspection["adapter"]["fingerprint"],
    )
    closure_roots: list[Path] = []
    original_run = delegated._run_adapter_process

    def capture_then_run(*args: object, **kwargs: object) -> object:
        environment = kwargs["environment"]
        assert isinstance(environment, dict)
        closure_roots.append(Path(environment["DWW_VERIFIED_INPUT_ROOT"]))
        return original_run(*args, **kwargs)

    monkeypatch.setattr(delegated, "_run_adapter_process", capture_then_run)

    with pytest.raises(DelegatedContractError, match="timed out"):
        invoke_delegated(
            repo.root,
            repo.common_dir,
            operation="status",
            request={},
            timeout_seconds=0.1,
        )

    assert len(closure_roots) == 1
    assert not closure_roots[0].exists()


@pytest.mark.parametrize(
    "interruption", (KeyboardInterrupt, SystemExit, RuntimeError)
)
def test_abnormal_monitor_exit_stops_owned_tree_before_closure_cleanup(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    interruption: type[BaseException],
) -> None:
    declare_adapter(git_repo)
    adapter_path = git_repo / "scripts" / "dww_adapter.py"
    spawned = git_repo / "adapter-grandchild-spawned.txt"
    pid_path = git_repo / "adapter-parent.pid"
    late_marker = git_repo / "late-adapter-grandchild.txt"
    grandchild = (
        "import time; from pathlib import Path; time.sleep(0.8); "
        f"Path({str(late_marker)!r}).write_text('late', encoding='utf-8')"
    )
    child = (
        "import subprocess,sys,time; from pathlib import Path; "
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}]); "
        f"Path({str(spawned)!r}).write_text('spawned', encoding='utf-8'); "
        "time.sleep(2)"
    )
    adapter_path.write_text(
        "import os, subprocess, sys, time\n"
        "from pathlib import Path\n"
        f"Path({str(pid_path)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
        f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
        "time.sleep(2)\n",
        encoding="utf-8",
    )
    git(git_repo, "add", "scripts/dww_adapter.py")
    git(git_repo, "commit", "-m", "add abnormal monitor fixture")
    repo = GitRepo(git_repo)
    inspection = inspect_delegated(repo.root, repo.common_dir)
    approve_delegated(
        repo.root,
        repo.common_dir,
        fingerprint=inspection["adapter"]["fingerprint"],
    )
    closure_roots: list[Path] = []
    original_run = delegated._run_adapter_process
    original_sleep = time.sleep

    def capture_then_run(*args: object, **kwargs: object) -> object:
        environment = kwargs["environment"]
        assert isinstance(environment, dict)
        closure_roots.append(Path(environment["DWW_VERIFIED_INPUT_ROOT"]))
        return original_run(*args, **kwargs)

    def interrupt_after_spawn(_seconds: float) -> None:
        deadline = time.monotonic() + 5
        while not spawned.exists() and time.monotonic() < deadline:
            original_sleep(0.01)
        assert spawned.exists()
        raise interruption("synthetic monitor interruption")

    monkeypatch.setattr(delegated, "_run_adapter_process", capture_then_run)
    monkeypatch.setattr(delegated, "_adapter_poll_pause", interrupt_after_spawn)

    try:
        with pytest.raises(interruption, match="synthetic monitor interruption"):
            invoke_delegated(
                repo.root,
                repo.common_dir,
                operation="status",
                request={},
                timeout_seconds=30,
            )

        assert len(closure_roots) == 1
        assert not closure_roots[0].exists()
        original_sleep(1)
        assert not late_marker.exists()
    finally:
        if pid_path.exists():
            from solo_ai.util import _stop_process_tree

            _stop_process_tree(int(pid_path.read_text(encoding="utf-8")), force=True)


@pytest.mark.skipif(os.name != "nt", reason="Windows process trees are required")
def test_windows_short_lived_launcher_cannot_leave_detached_grandchild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import psutil

    base_python = str(getattr(sys, "_base_executable", sys.executable))
    root_pid_path = tmp_path / "job-root.pid"
    grandchild_pid_path = tmp_path / "job-grandchild.pid"
    launcher_exited = tmp_path / "job-launcher-exited.txt"
    late_marker = tmp_path / "job-late-grandchild.txt"
    grandchild = (
        "import os,time; from pathlib import Path; "
        f"Path({str(grandchild_pid_path)!r}).write_text(str(os.getpid()), encoding='utf-8'); "
        "time.sleep(0.8); "
        f"Path({str(late_marker)!r}).write_text('late', encoding='utf-8')"
    )
    launcher = (
        "import subprocess,sys; "
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}])"
    )
    root = (
        "import os,subprocess,sys,time; from pathlib import Path; "
        f"Path({str(root_pid_path)!r}).write_text(str(os.getpid()), encoding='utf-8'); "
        f"subprocess.Popen([sys.executable, '-c', {launcher!r}]).wait(); "
        f"Path({str(launcher_exited)!r}).write_text('exited', encoding='utf-8'); "
        "time.sleep(30)"
    )
    original_sleep = time.sleep

    def interrupt_after_launcher_exits(_seconds: float) -> None:
        deadline = time.monotonic() + 5
        while not launcher_exited.exists() and time.monotonic() < deadline:
            original_sleep(0.01)
        assert launcher_exited.exists()
        assert root_pid_path.exists()
        observed_root = psutil.Process(
            int(root_pid_path.read_text(encoding="utf-8"))
        )
        assert observed_root.children(recursive=True) == []
        raise KeyboardInterrupt("synthetic detached-grandchild interruption")

    monkeypatch.setattr(delegated, "_adapter_poll_pause", interrupt_after_launcher_exits)

    try:
        with pytest.raises(
            KeyboardInterrupt, match="synthetic detached-grandchild interruption"
        ):
            delegated._run_adapter_process(
                [base_python, "-c", root],
                root=tmp_path,
                request_bytes=b"{}",
                timeout_seconds=30,
            )

        original_sleep(1)
        assert not late_marker.exists()
    finally:
        from solo_ai.util import _stop_process_tree

        for pid_path in (root_pid_path, grandchild_pid_path):
            if pid_path.exists():
                raw_pid = pid_path.read_text(encoding="utf-8").strip()
                if raw_pid.isdigit():
                    _stop_process_tree(int(raw_pid), force=True)


@pytest.mark.parametrize("returncode", (0, 7))
def test_natural_adapter_exit_cannot_leave_inherited_stdio_descendant(
    tmp_path: Path, returncode: int
) -> None:
    marker = tmp_path / f"natural-{returncode}-late-child.txt"
    child = (
        "import time; from pathlib import Path; "
        "time.sleep(0.8); "
        f"Path({str(marker)!r}).write_text('late', encoding='utf-8')"
    )
    root = (
        "import subprocess,sys; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
        f"raise SystemExit({returncode})"
    )

    result = delegated._run_adapter_process(
        [sys.executable, "-c", root],
        root=tmp_path,
        request_bytes=b"{}",
        timeout_seconds=30,
    )

    assert result.returncode == returncode
    time.sleep(1)
    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects are required")
def test_windows_job_contains_descendant_spawned_after_root_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base_python = str(getattr(sys, "_base_executable", sys.executable))
    parent_pid_path = tmp_path / "dynamic-parent.pid"
    root_ready = tmp_path / "dynamic-root-ready.txt"
    spawn_trigger = tmp_path / "dynamic-spawn-trigger.txt"
    tree_spawned = tmp_path / "dynamic-tree-spawned.txt"
    child_pid_path = tmp_path / "dynamic-child.pid"
    grandchild_pid_path = tmp_path / "dynamic-grandchild.pid"
    late_child_marker = tmp_path / "late-dynamic-child.txt"
    late_marker = tmp_path / "late-dynamic-grandchild.txt"
    grandchild = (
        "import os,time; from pathlib import Path; "
        f"Path({str(grandchild_pid_path)!r}).write_text(str(os.getpid()), encoding='utf-8'); "
        "time.sleep(0.8); "
        f"Path({str(late_marker)!r}).write_text('late', encoding='utf-8')"
    )
    child = (
        "import os,subprocess,sys,time; from pathlib import Path; "
        f"Path({str(child_pid_path)!r}).write_text(str(os.getpid()), encoding='utf-8'); "
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}]); "
        f"Path({str(tree_spawned)!r}).write_text('spawned', encoding='utf-8'); "
        "time.sleep(0.8); "
        f"Path({str(late_child_marker)!r}).write_text('late', encoding='utf-8')"
    )
    parent = (
        "import os,subprocess,sys,time; from pathlib import Path; "
        f"Path({str(parent_pid_path)!r}).write_text(str(os.getpid()), encoding='utf-8'); "
        f"ready=Path({str(root_ready)!r}); trigger=Path({str(spawn_trigger)!r}); "
        "ready.write_text('ready', encoding='utf-8'); "
        "deadline=time.monotonic()+5; "
        "exec(\"while not trigger.exists() and time.monotonic() < deadline:\\n time.sleep(0.01)\"); "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
        "time.sleep(30)"
    )
    original_sleep = time.sleep

    def interrupt_after_root_ready(_seconds: float) -> None:
        deadline = time.monotonic() + 5
        while not root_ready.exists() and time.monotonic() < deadline:
            original_sleep(0.01)
        assert root_ready.exists()
        spawn_trigger.write_text("spawn", encoding="utf-8")
        deadline = time.monotonic() + 5
        while not tree_spawned.exists() and time.monotonic() < deadline:
            original_sleep(0.01)
        assert tree_spawned.exists()
        raise KeyboardInterrupt("synthetic Windows Job interruption")

    monkeypatch.setattr(delegated, "_adapter_poll_pause", interrupt_after_root_ready)

    try:
        with pytest.raises(
            KeyboardInterrupt, match="synthetic Windows Job interruption"
        ):
            delegated._run_adapter_process(
                [base_python, "-c", parent],
                root=tmp_path,
                request_bytes=b"{}",
                timeout_seconds=30,
            )

        original_sleep(1)
        assert not late_child_marker.exists()
        assert not late_marker.exists()
    finally:
        from solo_ai.util import _stop_process_tree

        for pid_path in (parent_pid_path, child_pid_path, grandchild_pid_path):
            if pid_path.exists():
                _stop_process_tree(
                    int(pid_path.read_text(encoding="utf-8")), force=True
                )


@pytest.mark.skipif(os.name != "nt", reason="Windows process handles are required")
def test_windows_job_ownership_query_uses_native_handle_before_pid_can_be_reused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import psutil

    assigned: list[tuple[int, int]] = []

    class Call:
        argtypes: object = None
        restype: object = None

        def __call__(
            self, process: object, job: object, belongs: object
        ) -> int:
            assigned.append((job.value, process.value))
            belongs._obj.value = 1
            return 1

    class Kernel32:
        IsProcessInJob = Call()

    process = delegated._WindowsAdapterProcess()
    process.information.hProcess = 0x123456

    monkeypatch.setattr(
        psutil,
        "Process",
        lambda _pid: pytest.fail("psutil PID identity must not establish ownership"),
    )
    monkeypatch.setattr(delegated, "_windows_kernel32", lambda: Kernel32())

    delegated._confirm_windows_adapter_job_ownership(
        delegated._WindowsAdapterJob(0xABCDEF), process
    )

    assert assigned == [(0xABCDEF, 0x123456)]


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects are required")
def test_windows_job_configuration_failure_occurs_before_popen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_job_configuration() -> object:
        raise OSError("synthetic Job configuration failure")

    def fail_if_started(*_args: object, **_kwargs: object) -> object:
        pytest.fail("root process must remain uncreated when Job setup fails")

    monkeypatch.setattr(
        delegated, "_create_windows_adapter_job", fail_job_configuration
    )
    monkeypatch.setattr(delegated, "_call_windows_create_process", fail_if_started)

    with pytest.raises(
        delegated.DelegatedContractError, match="Job configuration failure"
    ):
        delegated._run_adapter_process(
            [sys.executable, "-c", "raise SystemExit(0)"],
            root=tmp_path,
            request_bytes=b"{}",
            timeout_seconds=30,
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows Job handles are required")
def test_windows_job_close_interruption_consumes_handle_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    live_handles = {0xABCDEF: "job"}

    class CloseHandle:
        argtypes: object = None
        restype: object = None

        def __call__(self, handle: object) -> int:
            raw_handle = int(handle.value)
            calls.append(raw_handle)
            assert live_handles.pop(raw_handle) == "job"
            # 模拟内核已经关闭 Job，数值立即被无关对象复用，随后才交付异步异常。
            live_handles[raw_handle] = "unrelated-reused-handle"
            raise KeyboardInterrupt("synthetic post-CloseHandle interruption")

    class Kernel32:
        pass

    Kernel32.CloseHandle = CloseHandle()

    monkeypatch.setattr(delegated, "_windows_kernel32", lambda: Kernel32())
    job = delegated._WindowsAdapterJob(0xABCDEF)

    with pytest.raises(
        delegated.DelegatedProcessTerminationError,
        match="close outcome is indeterminate",
    ) as first:
        delegated._close_windows_adapter_job(job)

    assert isinstance(first.value.__cause__, KeyboardInterrupt)
    assert job.handle is None
    assert job.close_attempted
    assert job.close_outcome_uncertain
    assert delegated._exception_requires_closure_preservation(first.value)

    with pytest.raises(
        delegated.DelegatedProcessTerminationError,
        match="close outcome is indeterminate",
    ):
        delegated._close_windows_adapter_job(job)

    assert calls == [0xABCDEF]
    assert live_handles == {0xABCDEF: "unrelated-reused-handle"}


@pytest.mark.skipif(os.name != "nt", reason="Windows Job handles are required")
def test_windows_job_detach_interruption_is_explicit_and_never_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    class CloseHandle:
        argtypes: object = None
        restype: object = None

        def __call__(self, handle: object) -> int:
            calls.append(int(handle.value))
            return 1

    class Kernel32:
        pass

    class InterruptingJob(delegated._WindowsAdapterJob):
        def __setattr__(self, name: str, value: object) -> None:
            super().__setattr__(name, value)
            if name == "handle" and value is None:
                raise KeyboardInterrupt("synthetic post-detach interruption")

    Kernel32.CloseHandle = CloseHandle()
    monkeypatch.setattr(delegated, "_windows_kernel32", lambda: Kernel32())
    job = InterruptingJob(0x123456)

    with pytest.raises(
        delegated.DelegatedProcessTerminationError,
        match="close outcome is indeterminate",
    ) as raised:
        delegated._close_windows_adapter_job(job)

    assert isinstance(raised.value.__cause__, KeyboardInterrupt)
    assert job.handle is None
    assert job.close_outcome_uncertain
    assert calls == []
    with pytest.raises(
        delegated.DelegatedProcessTerminationError,
        match="close outcome is indeterminate",
    ):
        delegated._close_windows_adapter_job(job)
    assert calls == []


@pytest.mark.skipif(os.name != "nt", reason="Windows process handles are required")
def test_windows_process_handle_close_interruption_is_explicit_after_detach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    class CloseHandle:
        argtypes: object = None
        restype: object = None

        def __call__(self, handle: object) -> int:
            calls.append(int(handle.value))
            raise KeyboardInterrupt("synthetic process handle close interruption")

    class Kernel32:
        pass

    Kernel32.CloseHandle = CloseHandle()
    monkeypatch.setattr(delegated, "_windows_kernel32", lambda: Kernel32())
    process = delegated._WindowsAdapterProcess()
    process.information.hProcess = 0x654321

    with pytest.raises(
        delegated.DelegatedProcessTerminationError,
        match="process handle close outcome is indeterminate",
    ):
        delegated._consume_windows_process_handle(
            process, "hProcess", label="process"
        )

    assert not process.information.hProcess
    assert calls == [0x654321]
    delegated._consume_windows_process_handle(process, "hProcess", label="process")
    assert calls == [0x654321]


@pytest.mark.skipif(os.name != "nt", reason="Windows suspended launch is required")
def test_windows_create_process_return_interruption_cannot_leak_suspended_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "interrupted-popen-root-ran.txt"
    original_create = delegated._call_windows_create_process
    observer_handles: list[int] = []

    def create_then_interrupt(
        process: object, *args: object, **kwargs: object
    ) -> None:
        original_create(process, *args, **kwargs)
        kernel32 = delegated._windows_kernel32()
        current_process = kernel32.GetCurrentProcess
        current_process.argtypes = []
        current_process.restype = ctypes.c_void_p
        duplicate = kernel32.DuplicateHandle
        duplicate.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.c_uint32,
        ]
        duplicate.restype = ctypes.c_int
        observer = ctypes.c_void_p()
        owner = current_process()
        assert duplicate(
            owner,
            ctypes.c_void_p(process.process_handle()),
            owner,
            ctypes.byref(observer),
            0,
            False,
            0x00000002,
        )
        observer_handles.append(int(observer.value))
        raise KeyboardInterrupt("synthetic interruption after CreateProcess")

    monkeypatch.setattr(
        delegated, "_call_windows_create_process", create_then_interrupt
    )

    try:
        with pytest.raises(
            KeyboardInterrupt, match="synthetic interruption after CreateProcess"
        ):
            delegated._run_adapter_process(
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; "
                    f"Path({str(marker)!r}).write_text('ran', encoding='utf-8')",
                ],
                root=tmp_path,
                request_bytes=b"{}",
                timeout_seconds=30,
            )

        assert len(observer_handles) == 1
        kernel32 = delegated._windows_kernel32()
        wait = kernel32.WaitForSingleObject
        wait.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        wait.restype = ctypes.c_uint32
        assert wait(ctypes.c_void_p(observer_handles[0]), 5000) == 0
        assert not marker.exists()
    finally:
        kernel32 = delegated._windows_kernel32()
        close = kernel32.CloseHandle
        close.argtypes = [ctypes.c_void_p]
        close.restype = ctypes.c_int
        for observer_handle in observer_handles:
            assert close(ctypes.c_void_p(observer_handle))


@pytest.mark.skipif(os.name != "nt", reason="Windows atomic Job launch is required")
def test_windows_post_create_interruption_cleans_confirmed_unused_closure(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    declare_adapter(git_repo)
    adapter_path = git_repo / "scripts" / "dww_adapter.py"
    marker = git_repo / "post-create-closure-adapter-ran.txt"
    adapter_path.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('ran', encoding='utf-8')\n",
        encoding="utf-8",
    )
    git(git_repo, "add", "scripts/dww_adapter.py")
    git(git_repo, "commit", "-m", "add post-create closure fixture")
    repo = GitRepo(git_repo)
    inspection = inspect_delegated(repo.root, repo.common_dir)
    approve_delegated(
        repo.root,
        repo.common_dir,
        fingerprint=inspection["adapter"]["fingerprint"],
    )
    closure_roots: list[Path] = []
    original_run = delegated._run_adapter_process
    original_create = delegated._call_windows_create_process

    def capture_then_run(*args: object, **kwargs: object) -> object:
        environment = kwargs["environment"]
        assert isinstance(environment, dict)
        closure_roots.append(Path(environment["DWW_VERIFIED_INPUT_ROOT"]))
        return original_run(*args, **kwargs)

    def create_then_interrupt(
        process: object, *args: object, **kwargs: object
    ) -> None:
        original_create(process, *args, **kwargs)
        raise KeyboardInterrupt("synthetic post-create closure interruption")

    monkeypatch.setattr(delegated, "_run_adapter_process", capture_then_run)
    monkeypatch.setattr(
        delegated, "_call_windows_create_process", create_then_interrupt
    )

    with pytest.raises(
        KeyboardInterrupt, match="synthetic post-create closure interruption"
    ):
        invoke_delegated(
            repo.root,
            repo.common_dir,
            operation="status",
            request={},
            timeout_seconds=30,
        )

    assert not marker.exists()
    assert len(closure_roots) == 1
    assert not closure_roots[0].exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups are required")
def test_posix_popen_return_interruption_cannot_leak_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "interrupted-popen-root-ran.txt"
    original_popen = subprocess.Popen
    spawned: list[subprocess.Popen[bytes]] = []
    launch_kwargs: list[dict[str, object]] = []
    termination_requests: list[str] = []
    original_request = delegated._request_posix_supervisor_self_termination

    def create_then_interrupt(*args: object, **kwargs: object) -> object:
        launch_kwargs.append(dict(kwargs))
        process = original_popen(*args, **kwargs)
        spawned.append(process)
        raise KeyboardInterrupt("synthetic interruption after fork-exec")

    def record_termination_request(
        ownership: delegated._PosixSupervisorOwnership,
    ) -> BaseException | None:
        termination_requests.append(ownership.termination_delivery)
        return original_request(ownership)

    monkeypatch.setattr(subprocess, "Popen", create_then_interrupt)
    monkeypatch.setattr(
        delegated,
        "_request_posix_supervisor_self_termination",
        record_termination_request,
    )

    try:
        with pytest.raises(
            KeyboardInterrupt, match="synthetic interruption after fork-exec"
        ):
            delegated._run_adapter_process(
                [
                    sys.executable,
                    "-c",
                    "import time; from pathlib import Path; time.sleep(1); "
                    f"Path({str(marker)!r}).write_text('ran', encoding='utf-8'); "
                    "time.sleep(30)",
                ],
                root=tmp_path,
                request_bytes=b"{}",
                timeout_seconds=30,
            )

        assert len(spawned) == 1
        assert len(launch_kwargs) == 1
        assert launch_kwargs[0]["start_new_session"] is True
        assert launch_kwargs[0]["close_fds"] is True
        assert len(launch_kwargs[0]["pass_fds"]) == 5
        assert Path(launch_kwargs[0]["cwd"]) != tmp_path
        launcher_environment = launch_kwargs[0]["env"]
        assert isinstance(launcher_environment, dict)
        assert launcher_environment == {"LC_ALL": "C", "LANG": "C"}
        deadline = time.monotonic() + 2
        while spawned[0].poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert spawned[0].poll() is not None
        assert not marker.exists()
        assert termination_requests
    finally:
        for process in spawned:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)


@pytest.mark.skipif(os.name == "nt", reason="POSIX gate launch is required")
def test_posix_lost_popen_without_status_uses_only_control_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "unidentified-launcher-adapter-ran.txt"
    original_popen = subprocess.Popen
    spawned: list[subprocess.Popen[bytes]] = []
    parent_signals: list[tuple[int, int]] = []

    def create_then_interrupt(*args: object, **kwargs: object) -> object:
        process = original_popen(*args, **kwargs)
        spawned.append(process)
        raise KeyboardInterrupt("synthetic Popen return interruption")

    def interrupt_status(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt("synthetic status read interruption")

    original_killpg = delegated.os.killpg

    def record_parent_signal(process_group: int, requested_signal: int) -> None:
        if requested_signal:
            parent_signals.append((process_group, requested_signal))
        original_killpg(process_group, requested_signal)

    monkeypatch.setattr(subprocess, "Popen", create_then_interrupt)
    monkeypatch.setattr(delegated, "_read_posix_launcher_status", interrupt_status)
    monkeypatch.setattr(delegated.os, "killpg", record_parent_signal)

    with pytest.raises(BaseException) as raised:
        delegated._run_adapter_process(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; "
                f"Path({str(marker)!r}).write_text('ran', encoding='utf-8')",
            ],
            root=tmp_path,
            request_bytes=b"{}",
            timeout_seconds=30,
        )

    assert exception_tree_contains(
        raised.value, KeyboardInterrupt, "synthetic Popen return interruption"
    )
    assert exception_tree_contains(
        raised.value,
        delegated.DelegatedProcessTerminationError,
        "stopping the delegated adapter process tree",
    )
    assert len(spawned) == 1
    deadline = time.monotonic() + 3
    while spawned[0].poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert spawned[0].poll() is not None
    assert parent_signals == []
    assert not marker.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX gate launch is required")
def test_posix_post_fork_interruption_cleans_confirmed_unused_closure(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    declare_adapter(git_repo)
    adapter_path = git_repo / "scripts" / "dww_adapter.py"
    marker = git_repo / "post-fork-closure-adapter-ran.txt"
    adapter_path.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('ran', encoding='utf-8')\n",
        encoding="utf-8",
    )
    git(git_repo, "add", "scripts/dww_adapter.py")
    git(git_repo, "commit", "-m", "add post-fork closure fixture")
    repo = GitRepo(git_repo)
    inspection = inspect_delegated(repo.root, repo.common_dir)
    approve_delegated(
        repo.root,
        repo.common_dir,
        fingerprint=inspection["adapter"]["fingerprint"],
    )
    closure_roots: list[Path] = []
    original_run = delegated._run_adapter_process
    original_popen = subprocess.Popen

    def capture_then_run(*args: object, **kwargs: object) -> object:
        environment = kwargs["environment"]
        assert isinstance(environment, dict)
        closure_roots.append(Path(environment["DWW_VERIFIED_INPUT_ROOT"]))

        def create_then_interrupt(*popen_args: object, **popen_kwargs: object) -> object:
            original_popen(*popen_args, **popen_kwargs)
            raise KeyboardInterrupt("synthetic post-fork closure interruption")

        monkeypatch.setattr(subprocess, "Popen", create_then_interrupt)
        try:
            return original_run(*args, **kwargs)
        finally:
            monkeypatch.setattr(subprocess, "Popen", original_popen)

    monkeypatch.setattr(delegated, "_run_adapter_process", capture_then_run)

    with pytest.raises(
        KeyboardInterrupt, match="synthetic post-fork closure interruption"
    ):
        invoke_delegated(
            repo.root,
            repo.common_dir,
            operation="status",
            request={},
            timeout_seconds=30,
        )

    assert not marker.exists()
    assert len(closure_roots) == 1
    assert not closure_roots[0].exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX gate launch is required")
def test_posix_unproven_identity_preserves_verified_input_closure_after_k(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    declare_adapter(git_repo)
    adapter_path = git_repo / "scripts" / "dww_adapter.py"
    marker = git_repo / "unproven-identity-adapter-ran.txt"
    adapter_path.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('ran', encoding='utf-8')\n",
        encoding="utf-8",
    )
    git(git_repo, "add", "scripts/dww_adapter.py")
    git(git_repo, "commit", "-m", "add unproven identity closure fixture")
    repo = GitRepo(git_repo)
    inspection = inspect_delegated(repo.root, repo.common_dir)
    approve_delegated(
        repo.root,
        repo.common_dir,
        fingerprint=inspection["adapter"]["fingerprint"],
    )
    closure_roots: list[Path] = []
    spawned: list[subprocess.Popen[bytes]] = []
    original_run = delegated._run_adapter_process
    original_popen = subprocess.Popen

    def capture_then_run(*args: object, **kwargs: object) -> object:
        environment = kwargs["environment"]
        assert isinstance(environment, dict)
        closure_roots.append(Path(environment["DWW_VERIFIED_INPUT_ROOT"]))

        def create_then_interrupt(
            *popen_args: object, **popen_kwargs: object
        ) -> object:
            process = original_popen(*popen_args, **popen_kwargs)
            spawned.append(process)
            raise KeyboardInterrupt("synthetic lost Popen identity")

        monkeypatch.setattr(subprocess, "Popen", create_then_interrupt)
        monkeypatch.setattr(
            delegated,
            "_read_posix_launcher_status",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                KeyboardInterrupt("synthetic unproven status identity")
            ),
        )
        try:
            return original_run(*args, **kwargs)
        finally:
            monkeypatch.setattr(subprocess, "Popen", original_popen)

    monkeypatch.setattr(delegated, "_run_adapter_process", capture_then_run)

    try:
        with pytest.raises(BaseException) as raised:
            invoke_delegated(
                repo.root,
                repo.common_dir,
                operation="status",
                request={},
                timeout_seconds=30,
            )

        assert exception_tree_contains(
            raised.value,
            delegated.DelegatedProcessTerminationError,
            "stopping the delegated adapter process tree",
        )
        assert len(spawned) == 1
        deadline = time.monotonic() + 2
        while spawned[0].poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert spawned[0].poll() is not None
        assert not marker.exists()
        assert len(closure_roots) == 1
        assert closure_roots[0].exists()
    finally:
        for closure_root in closure_roots:
            if closure_root.exists():
                shutil.rmtree(closure_root)


@pytest.mark.skipif(os.name == "nt", reason="POSIX launcher isolation is required")
def test_posix_launcher_cwd_is_outside_repo_when_interpreter_is_inside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    interpreter = repository / ".venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    monkeypatch.setattr(delegated.sys, "executable", str(interpreter))

    launcher_cwd = Path(delegated._posix_launcher_cwd()).resolve()

    assert launcher_cwd == Path(os.path.abspath(os.sep)).resolve()
    assert not launcher_cwd.is_relative_to(repository.resolve())


@pytest.mark.skipif(os.name == "nt", reason="POSIX launcher isolation is required")
def test_posix_launcher_does_not_load_adapter_sitecustomize_before_go(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site_marker = tmp_path / "sitecustomize-ran.txt"
    adapter_marker = tmp_path / "adapter-ran.txt"
    (tmp_path / "sitecustomize.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(site_marker)!r}).write_text('ran', encoding='utf-8')\n",
        encoding="utf-8",
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(tmp_path)
    original_write = delegated.os.write

    def interrupt_gate(descriptor: int, content: bytes) -> int:
        if content == b"G":
            raise KeyboardInterrupt("synthetic interruption before GO")
        return original_write(descriptor, content)

    monkeypatch.setattr(delegated.os, "write", interrupt_gate)

    with pytest.raises(KeyboardInterrupt, match="synthetic interruption before GO"):
        delegated._run_adapter_process(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; "
                f"Path({str(adapter_marker)!r}).write_text('ran', encoding='utf-8')",
            ],
            root=tmp_path,
            request_bytes=b"{}",
            timeout_seconds=30,
            environment=environment,
        )

    assert not site_marker.exists()
    assert not adapter_marker.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX launcher gate is required")
def test_posix_gate_write_interruption_after_go_stops_adapter_before_late_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "post-go-adapter-ran.txt"
    original_write = delegated.os.write

    def write_then_interrupt(descriptor: int, content: bytes) -> int:
        written = original_write(descriptor, content)
        if content == b"G":
            raise KeyboardInterrupt("synthetic interruption after GO")
        return written

    monkeypatch.setattr(delegated.os, "write", write_then_interrupt)

    with pytest.raises(KeyboardInterrupt, match="synthetic interruption after GO"):
        delegated._run_adapter_process(
            [
                sys.executable,
                "-c",
                "import time; from pathlib import Path; time.sleep(1); "
                f"Path({str(marker)!r}).write_text('ran', encoding='utf-8')",
            ],
            root=tmp_path,
            request_bytes=b"{}",
            timeout_seconds=30,
        )

    time.sleep(1.2)
    assert not marker.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX launcher status is required")
def test_posix_launcher_rejects_status_that_does_not_match_direct_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "mismatched-status-adapter-ran.txt"
    original_read_status = delegated._read_posix_launcher_status
    original_popen = subprocess.Popen
    spawned: list[subprocess.Popen[bytes]] = []
    parent_signals: list[tuple[int, int]] = []

    def mismatched_status(*args: object, **kwargs: object) -> None:
        original_read_status(*args, **kwargs)
        identity = args[1]
        pid, process_group = identity.require()
        assert pid == process_group
        identity.value = (pid + 1, process_group + 1)

    def capture_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        process = original_popen(*args, **kwargs)
        spawned.append(process)
        return process

    original_killpg = delegated.os.killpg

    def record_parent_signal(process_group: int, requested_signal: int) -> None:
        if requested_signal:
            parent_signals.append((process_group, requested_signal))
        original_killpg(process_group, requested_signal)

    monkeypatch.setattr(
        delegated, "_read_posix_launcher_status", mismatched_status
    )
    monkeypatch.setattr(subprocess, "Popen", capture_popen)
    monkeypatch.setattr(delegated.os, "killpg", record_parent_signal)

    with pytest.raises(BaseException) as raised:
        delegated._run_adapter_process(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; "
                f"Path({str(marker)!r}).write_text('ran', encoding='utf-8')",
            ],
            root=tmp_path,
            request_bytes=b"{}",
            timeout_seconds=30,
        )

    assert exception_tree_contains(
        raised.value,
        delegated.DelegatedContractError,
        "status does not match its direct child",
    )
    assert exception_tree_contains(
        raised.value,
        delegated.DelegatedProcessTerminationError,
        "stopping the delegated adapter process tree",
    )
    assert len(spawned) == 1
    assert spawned[0].returncode == -signal.SIGKILL
    assert parent_signals == []
    assert not marker.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX launcher status is required")
def test_posix_status_read_interruption_after_identity_stops_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "status-interruption-adapter-ran.txt"
    original_read_status = delegated._read_posix_launcher_status

    def read_then_interrupt(*args: object, **kwargs: object) -> None:
        original_read_status(*args, **kwargs)
        raise KeyboardInterrupt("synthetic post-status interruption")

    monkeypatch.setattr(
        delegated, "_read_posix_launcher_status", read_then_interrupt
    )

    with pytest.raises(
        KeyboardInterrupt, match="synthetic post-status interruption"
    ):
        delegated._run_adapter_process(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; "
                f"Path({str(marker)!r}).write_text('ran', encoding='utf-8')",
            ],
            root=tmp_path,
            request_bytes=b"{}",
            timeout_seconds=30,
        )

    assert not marker.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX launcher status is required")
def test_posix_initial_status_write_failure_holds_for_controlled_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "status-write-failure-adapter-ran.txt"
    original_pipe = delegated._create_cloexec_pipe
    original_popen = subprocess.Popen
    pipe_count = 0
    spawned: list[subprocess.Popen[bytes]] = []

    def create_pipe_with_broken_status_reader() -> object:
        nonlocal pipe_count
        pipe_count += 1
        channel = original_pipe()
        if pipe_count == 1:
            channel.read.close()
        return channel

    def capture_popen(*args: object, **kwargs: object) -> object:
        process = original_popen(*args, **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(
        delegated, "_create_cloexec_pipe", create_pipe_with_broken_status_reader
    )
    monkeypatch.setattr(subprocess, "Popen", capture_popen)

    with pytest.raises(BaseException) as raised:
        delegated._run_adapter_process(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; "
                f"Path({str(marker)!r}).write_text('ran', encoding='utf-8')",
            ],
            root=tmp_path,
            request_bytes=b"{}",
            timeout_seconds=30,
        )

    assert exception_tree_contains(
        raised.value,
        delegated.DelegatedProcessTerminationError,
        "status descriptor was already closed",
    )
    assert exception_tree_contains(
        raised.value,
        delegated.DelegatedProcessTerminationError,
        "stopping the delegated adapter process tree",
    )
    assert len(spawned) == 1
    assert spawned[0].returncode == -signal.SIGKILL
    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects are required")
@pytest.mark.parametrize("failure_point", ("create", "ownership-query"))
def test_windows_job_setup_failure_never_executes_root_and_cleans_closure(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, failure_point: str
) -> None:
    declare_adapter(git_repo)
    adapter_path = git_repo / "scripts" / "dww_adapter.py"
    marker = git_repo / f"{failure_point}-failure-adapter-ran.txt"
    adapter_path.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('ran', encoding='utf-8')\n",
        encoding="utf-8",
    )
    git(git_repo, "add", "scripts/dww_adapter.py")
    git(git_repo, "commit", "-m", f"add {failure_point} failure fixture")
    repo = GitRepo(git_repo)
    inspection = inspect_delegated(repo.root, repo.common_dir)
    approve_delegated(
        repo.root,
        repo.common_dir,
        fingerprint=inspection["adapter"]["fingerprint"],
    )
    closure_roots: list[Path] = []
    original_run = delegated._run_adapter_process

    def capture_then_run(*args: object, **kwargs: object) -> object:
        environment = kwargs["environment"]
        assert isinstance(environment, dict)
        closure_roots.append(Path(environment["DWW_VERIFIED_INPUT_ROOT"]))
        return original_run(*args, **kwargs)

    def fail_setup(*_args: object, **_kwargs: object) -> None:
        raise OSError(f"synthetic Job {failure_point} failure")

    monkeypatch.setattr(delegated, "_run_adapter_process", capture_then_run)
    if failure_point == "create":
        monkeypatch.setattr(delegated, "_call_windows_create_process", fail_setup)
    else:
        monkeypatch.setattr(
            delegated, "_confirm_windows_adapter_job_ownership", fail_setup
        )

    with pytest.raises(
        delegated.DelegatedContractError,
        match=f"Job {failure_point} failure",
    ):
        invoke_delegated(
            repo.root,
            repo.common_dir,
            operation="status",
            request={},
            timeout_seconds=30,
        )

    assert not marker.exists()
    assert len(closure_roots) == 1
    assert not closure_roots[0].exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows suspended launch is required")
def test_windows_resume_failure_never_writes_marker_and_preserves_closure(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    declare_adapter(git_repo)
    adapter_path = git_repo / "scripts" / "dww_adapter.py"
    marker = git_repo / "resume-failure-adapter-ran.txt"
    adapter_path.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('ran', encoding='utf-8')\n",
        encoding="utf-8",
    )
    git(git_repo, "add", "scripts/dww_adapter.py")
    git(git_repo, "commit", "-m", "add resume failure fixture")
    repo = GitRepo(git_repo)
    inspection = inspect_delegated(repo.root, repo.common_dir)
    approve_delegated(
        repo.root,
        repo.common_dir,
        fingerprint=inspection["adapter"]["fingerprint"],
    )
    closure_roots: list[Path] = []
    original_run = delegated._run_adapter_process

    def capture_then_run(*args: object, **kwargs: object) -> object:
        environment = kwargs["environment"]
        assert isinstance(environment, dict)
        closure_roots.append(Path(environment["DWW_VERIFIED_INPUT_ROOT"]))
        return original_run(*args, **kwargs)

    def fail_resume(*_args: object, **_kwargs: object) -> None:
        raise OSError("synthetic resume failure")

    monkeypatch.setattr(delegated, "_run_adapter_process", capture_then_run)
    monkeypatch.setattr(delegated, "_resume_windows_adapter_process", fail_resume)

    try:
        with pytest.raises(BaseExceptionGroup) as raised:
            invoke_delegated(
                repo.root,
                repo.common_dir,
                operation="status",
                request={},
                timeout_seconds=30,
            )

        assert exception_tree_contains(
            raised.value, OSError, "synthetic resume failure"
        )
        assert exception_tree_contains(
            raised.value,
            delegated.DelegatedProcessTerminationError,
            "resume outcome was indeterminate",
        )
        assert not marker.exists()
        assert len(closure_roots) == 1
        assert closure_roots[0].exists()
    finally:
        for closure_root in closure_roots:
            if closure_root.exists():
                shutil.rmtree(closure_root)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects are required")
@pytest.mark.parametrize("failure_point", ("terminate", "query"))
def test_windows_job_confirmation_failure_preserves_verified_input_closure(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    declare_adapter(git_repo)
    adapter_path = git_repo / "scripts" / "dww_adapter.py"
    started = git_repo / f"job-{failure_point}-started.txt"
    adapter_path.write_text(
        "import time\n"
        "from pathlib import Path\n"
        f"Path({str(started)!r}).write_text('started', encoding='utf-8')\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    git(git_repo, "add", "scripts/dww_adapter.py")
    git(git_repo, "commit", "-m", f"add Job {failure_point} fixture")
    repo = GitRepo(git_repo)
    inspection = inspect_delegated(repo.root, repo.common_dir)
    approve_delegated(
        repo.root,
        repo.common_dir,
        fingerprint=inspection["adapter"]["fingerprint"],
    )
    closure_roots: list[Path] = []
    original_run = delegated._run_adapter_process
    original_sleep = time.sleep

    def capture_then_run(*args: object, **kwargs: object) -> object:
        environment = kwargs["environment"]
        assert isinstance(environment, dict)
        closure_roots.append(Path(environment["DWW_VERIFIED_INPUT_ROOT"]))
        return original_run(*args, **kwargs)

    def interrupt_after_start(_seconds: float) -> None:
        deadline = time.monotonic() + 5
        while not started.exists() and time.monotonic() < deadline:
            original_sleep(0.01)
        assert started.exists()
        raise KeyboardInterrupt(f"synthetic Job {failure_point} interruption")

    def fail_job_api(*_args: object, **_kwargs: object) -> None:
        raise OSError(f"synthetic Job {failure_point} failure")

    monkeypatch.setattr(delegated, "_run_adapter_process", capture_then_run)
    monkeypatch.setattr(delegated, "_adapter_poll_pause", interrupt_after_start)
    if failure_point == "terminate":
        monkeypatch.setattr(delegated, "_terminate_windows_adapter_job", fail_job_api)
    else:
        monkeypatch.setattr(
            delegated, "_query_windows_adapter_job_active_processes", fail_job_api
        )

    try:
        with pytest.raises(BaseExceptionGroup) as raised:
            invoke_delegated(
                repo.root,
                repo.common_dir,
                operation="status",
                request={},
                timeout_seconds=30,
            )

        assert exception_tree_contains(
            raised.value,
            KeyboardInterrupt,
            f"synthetic Job {failure_point} interruption",
        )
        assert exception_tree_contains(
            raised.value,
            delegated.DelegatedProcessTerminationError,
            "Job Object could not be confirmed empty",
        )
        assert len(closure_roots) == 1
        assert closure_roots[0].exists()
    finally:
        for closure_root in closure_roots:
            if closure_root.exists():
                shutil.rmtree(closure_root)


def test_output_read_interruption_after_natural_exit_keeps_original_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stop_polls: list[int | None] = []
    job_close_calls: list[int | None] = []
    original_stop = delegated._stop_adapter_process
    original_close_job = delegated._close_windows_adapter_job

    def fail_read(*args: object, **kwargs: object) -> bytes:
        if os.name == "nt":
            assert job_close_calls == []
        raise RuntimeError("synthetic output read failure")

    def capture_close_job(job: object) -> None:
        job_close_calls.append(job.handle)
        original_close_job(job)

    def capture_stop(
        process: object,
        *,
        windows_job: object | None = None,
    ) -> None:
        assert hasattr(process, "poll")
        stop_polls.append(process.poll())
        original_stop(process, windows_job=windows_job)

    monkeypatch.setattr(delegated, "_bounded_file_bytes", fail_read)
    monkeypatch.setattr(delegated, "_stop_adapter_process", capture_stop)
    monkeypatch.setattr(delegated, "_close_windows_adapter_job", capture_close_job)

    with pytest.raises(RuntimeError, match="synthetic output read failure"):
        delegated._run_adapter_process(
            [sys.executable, "-c", "print('done')"],
            root=tmp_path,
            request_bytes=b"{}",
            timeout_seconds=30,
        )

    assert len(stop_polls) == 1
    assert stop_polls[0] is not None
    if os.name == "nt":
        assert len(job_close_calls) == 1


@pytest.mark.parametrize(
    "termination_failure",
    (delegated.DelegatedProcessTerminationError, RuntimeError),
)
def test_unconfirmed_termination_preserves_verified_input_closure(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    termination_failure: type[BaseException],
) -> None:
    declare_adapter(git_repo)
    adapter_path = git_repo / "scripts" / "dww_adapter.py"
    pid_path = git_repo / "unconfirmed-adapter.pid"
    adapter_path.write_text(
        "import os, time\n"
        "from pathlib import Path\n"
        f"Path({str(pid_path)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    git(git_repo, "add", "scripts/dww_adapter.py")
    git(git_repo, "commit", "-m", "add unconfirmed termination fixture")
    repo = GitRepo(git_repo)
    inspection = inspect_delegated(repo.root, repo.common_dir)
    approve_delegated(
        repo.root,
        repo.common_dir,
        fingerprint=inspection["adapter"]["fingerprint"],
    )
    closure_roots: list[Path] = []
    interrupted_ownerships: list[tuple[object, object | None]] = []
    original_run = delegated._run_adapter_process
    original_stop = delegated._stop_adapter_process
    original_sleep = time.sleep

    def capture_then_run(*args: object, **kwargs: object) -> object:
        environment = kwargs["environment"]
        assert isinstance(environment, dict)
        closure_roots.append(Path(environment["DWW_VERIFIED_INPUT_ROOT"]))
        return original_run(*args, **kwargs)

    def interrupt_after_start(_seconds: float) -> None:
        deadline = time.monotonic() + 5
        while not pid_path.exists() and time.monotonic() < deadline:
            original_sleep(0.01)
        assert pid_path.exists()
        raise KeyboardInterrupt("synthetic monitor interruption")

    def fail_stop(process: object, **_kwargs: object) -> None:
        interrupted_ownerships.append((process, _kwargs.get("windows_job")))
        raise termination_failure("synthetic termination confirmation failure")

    monkeypatch.setattr(delegated, "_run_adapter_process", capture_then_run)
    monkeypatch.setattr(delegated, "_adapter_poll_pause", interrupt_after_start)
    monkeypatch.setattr(delegated, "_stop_adapter_process", fail_stop)

    try:
        with pytest.raises(BaseExceptionGroup) as raised:
            invoke_delegated(
                repo.root,
                repo.common_dir,
                operation="status",
                request={},
                timeout_seconds=30,
            )

        assert exception_tree_contains(
            raised.value, KeyboardInterrupt, "synthetic monitor interruption"
        )
        assert exception_tree_contains(
            raised.value,
            delegated.DelegatedProcessTerminationError,
            (
                "synthetic termination confirmation failure"
                if termination_failure is delegated.DelegatedProcessTerminationError
                else "Unexpected failure while stopping"
            ),
        )
        assert exception_tree_contains(
            raised.value,
            termination_failure,
            "synthetic termination confirmation failure",
        )
        assert len(closure_roots) == 1
        assert closure_roots[0].exists()
        assert any(
            str(closure_roots[0]) in note
            for note in getattr(raised.value, "__notes__", ())
        )
    finally:
        cleanup_failure: BaseException | None = None
        ownerships = {
            id(process): (process, windows_job)
            for process, windows_job in interrupted_ownerships
        }
        for process, windows_job in ownerships.values():
            try:
                original_stop(process, windows_job=windows_job)
                if os.name == "nt":
                    assert isinstance(process, delegated._WindowsAdapterProcess)
                    assert isinstance(windows_job, delegated._WindowsAdapterJob)
                    assert windows_job.empty_confirmed
                    assert windows_job.closed_confirmed
                    assert windows_job.handle is None
                    assert not process.information.hProcess
                    assert not process.information.hThread
                else:
                    assert isinstance(process, delegated._PosixAdapterProcess)
                    assert process.ownership.termination_confirmed
                    assert process.supervisor is not None
                    assert process.supervisor.poll() is not None
            except BaseException as exc:
                cleanup_failure = exc
        if cleanup_failure is not None and pid_path.exists():
            # 精确 owner cleanup 失败时才以 adapter PID 子树作测试卫生兜底；
            # cleanup_failure 仍在 finally 末尾抛出，绝不把兜底冒充通过。
            from solo_ai.util import _stop_process_tree

            _stop_process_tree(int(pid_path.read_text(encoding="utf-8")), force=True)
        for closure_root in closure_roots:
            if closure_root.exists():
                shutil.rmtree(closure_root)
        if cleanup_failure is not None:
            raise cleanup_failure


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups are required")
def test_posix_lost_launcher_never_signals_after_direct_child_was_reaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals: list[tuple[int, int]] = []

    def reaped_elsewhere(*_args: object, **_kwargs: object) -> object:
        raise ChildProcessError("synthetic external SIGCHLD reap")

    monkeypatch.setattr(delegated.os, "waitid", reaped_elsewhere)
    monkeypatch.setattr(
        delegated.os,
        "killpg",
        lambda process_group, requested_signal: signals.append(
            (process_group, requested_signal)
        ),
    )

    with pytest.raises(
        delegated.DelegatedProcessTerminationError,
        match="termination descriptor is unavailable",
    ):
        delegated._stop_unreturned_posix_launcher(4242, 4242)

    # 直接子一旦已被外部 reaper 消费，4242 可能已属于无关进程组。
    assert signals == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups are required")
def test_posix_normal_cleanup_never_polls_then_signals_a_reused_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals: list[tuple[int, int]] = []

    def reaped_elsewhere(*_args: object, **_kwargs: object) -> object:
        raise ChildProcessError("synthetic normal-path external reap")

    ownership = delegated._PosixSupervisorOwnership(
        pid=4343,
        process_group=4343,
        control_write=delegated._OwnedPosixFd(99),
    )
    process = delegated._PosixAdapterProcess(
        supervisor=object(),  # type: ignore[arg-type]
        ownership=ownership,
        result_channel=delegated._OwnedPosixChannel(),
        result_buffer=bytearray(),
    )
    monkeypatch.setattr(delegated.os, "waitid", reaped_elsewhere)
    monkeypatch.setattr(
        delegated.os,
        "killpg",
        lambda process_group, requested_signal: signals.append(
            (process_group, requested_signal)
        ),
    )
    with pytest.raises(delegated.DelegatedProcessTerminationError) as raised:
        delegated._stop_posix_adapter_process_group(process)

    # 父方不得在释放直接子身份后再对裸 PGID 发送任何 destructive signal。
    assert exception_tree_contains(
        raised.value,
        delegated.DelegatedProcessTerminationError,
        "reaped outside its owner",
    )
    assert signals == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX supervisor is required")
@pytest.mark.parametrize("returncode", (0, 7))
def test_posix_adapter_result_arrives_while_supervisor_is_still_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
) -> None:
    observed: list[tuple[int, int]] = []
    original_ensure = delegated._ensure_adapter_process_boundary_empty

    def inspect_then_stop(process: object, **kwargs: object) -> None:
        assert isinstance(process, delegated._PosixAdapterProcess)
        assert process.adapter_returncode == returncode
        assert not delegated._posix_direct_child_exited_unreaped(process.pid)
        # adapter 是 supervisor 的子进程；父方没有可被外部 SIGCHLD handler 抢走的
        # adapter 直接子，且 supervisor 在接受结果后仍未自然退出。
        assert os.waitpid(-1, os.WNOHANG) == (0, 0)
        observed.append((process.pid, process.ownership.process_group))
        original_ensure(process, **kwargs)

    monkeypatch.setattr(
        delegated, "_ensure_adapter_process_boundary_empty", inspect_then_stop
    )

    result = delegated._run_adapter_process(
        [sys.executable, "-c", f"raise SystemExit({returncode})"],
        root=tmp_path,
        request_bytes=b"{}",
        timeout_seconds=30,
    )

    assert result.returncode == returncode
    assert len(observed) == 1
    assert observed[0][0] == observed[0][1]


@pytest.mark.skipif(os.name == "nt", reason="POSIX supervisor is required")
def test_posix_parent_never_sends_destructive_group_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_killpg = delegated.os.killpg
    destructive_signals: list[int] = []

    def allow_probe_only(process_group: int, requested_signal: int) -> None:
        if requested_signal != 0:
            destructive_signals.append(requested_signal)
            pytest.fail(
                "parent must never send a destructive signal to a reusable PGID"
            )
        original_killpg(process_group, requested_signal)

    monkeypatch.setattr(
        delegated.os,
        "killpg",
        allow_probe_only,
    )

    result = delegated._run_adapter_process(
        [sys.executable, "-c", "raise SystemExit(0)"],
        root=tmp_path,
        request_bytes=b"{}",
        timeout_seconds=30,
    )

    assert result.returncode == 0
    assert destructive_signals == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX supervisor is required")
def test_posix_supervisor_ignores_term_and_adapter_restores_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_ensure = delegated._ensure_adapter_process_boundary_empty

    def term_then_stop(process: object, **kwargs: object) -> None:
        assert isinstance(process, delegated._PosixAdapterProcess)
        os.kill(process.pid, signal.SIGTERM)
        time.sleep(0.05)
        assert not delegated._posix_direct_child_exited_unreaped(process.pid)
        original_ensure(process, **kwargs)

    monkeypatch.setattr(
        delegated, "_ensure_adapter_process_boundary_empty", term_then_stop
    )
    adapter = (
        "import signal; "
        "raise SystemExit(0 if signal.getsignal(signal.SIGTERM) == signal.SIG_DFL else 9)"
    )

    result = delegated._run_adapter_process(
        [sys.executable, "-c", adapter],
        root=tmp_path,
        request_bytes=b"{}",
        timeout_seconds=30,
    )

    assert result.returncode == 0


@pytest.mark.skipif(os.name == "nt", reason="POSIX supervisor is required")
def test_posix_exec_failure_is_reported_before_supervisor_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed_active: list[bool] = []
    original_ensure = delegated._ensure_adapter_process_boundary_empty

    def inspect_then_stop(process: object, **kwargs: object) -> None:
        assert isinstance(process, delegated._PosixAdapterProcess)
        observed_active.append(
            not delegated._posix_direct_child_exited_unreaped(process.pid)
        )
        original_ensure(process, **kwargs)

    monkeypatch.setattr(
        delegated, "_ensure_adapter_process_boundary_empty", inspect_then_stop
    )

    result = delegated._run_adapter_process(
        ["/definitely/missing/dww-adapter"],
        root=tmp_path,
        request_bytes=b"{}",
        timeout_seconds=30,
    )

    assert result.returncode == 126
    assert observed_active == [True]


@pytest.mark.skipif(os.name == "nt", reason="POSIX result protocol is required")
@pytest.mark.parametrize(
    ("frame", "message"),
    (
        (b"forged\n", "invalid"),
        (b"DWWR1 0", "invalid"),
        (b"DWWR1 " + b"1" * 80 + b"\n", "oversized"),
    ),
)
def test_posix_result_protocol_rejects_untrusted_frames(
    monkeypatch: pytest.MonkeyPatch,
    frame: bytes,
    message: str,
) -> None:
    read_fd, write_fd = os.pipe()
    os.write(write_fd, frame)
    os.close(write_fd)
    ownership = delegated._PosixSupervisorOwnership(
        pid=4545,
        process_group=4545,
        control_write=delegated._OwnedPosixFd(None),
    )
    process = delegated._PosixAdapterProcess(
        supervisor=object(),  # type: ignore[arg-type]
        ownership=ownership,
        result_channel=delegated._OwnedPosixChannel(
            read=delegated._OwnedPosixFd(read_fd)
        ),
        result_buffer=bytearray(),
    )
    monkeypatch.setattr(
        delegated, "_posix_direct_child_exited_unreaped", lambda _pid: False
    )

    try:
        with pytest.raises(delegated.DelegatedContractError, match=message):
            delegated._poll_posix_adapter_result(process)
    finally:
        process.result_read.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX supervisor is required")
def test_posix_reused_group_after_final_reap_is_read_only_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control_read, control_write = os.pipe()
    signals: list[tuple[int, int]] = []
    ownership = delegated._PosixSupervisorOwnership(
        pid=4646,
        process_group=4646,
        control_write=delegated._OwnedPosixFd(control_write),
    )
    monkeypatch.setattr(
        delegated, "_posix_direct_child_exited_unreaped", lambda _pid: False
    )
    monkeypatch.setattr(
        delegated.os, "waitpid", lambda _pid, _options: (4646, signal.SIGKILL)
    )
    monkeypatch.setattr(
        delegated,
        "_posix_process_group_exists",
        lambda _process_group: True,
    )
    monkeypatch.setattr(
        delegated.os,
        "killpg",
        lambda process_group, requested_signal: signals.append(
            (process_group, requested_signal)
        ),
    )
    monkeypatch.setattr(delegated, "ADAPTER_TERMINATION_GRACE_SECONDS", 0.0)

    try:
        with pytest.raises(
            delegated.DelegatedProcessTerminationError,
            match="could not be confirmed stopped",
        ):
            delegated._stop_posix_supervisor(ownership)
    finally:
        os.close(control_read)

    assert ownership.termination_requested
    assert signals == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX supervisor is required")
def test_posix_unexpected_supervisor_exit_never_signals_its_old_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals: list[tuple[int, int]] = []
    ownership = delegated._PosixSupervisorOwnership(
        pid=4747,
        process_group=4747,
        control_write=delegated._OwnedPosixFd(98),
    )
    monkeypatch.setattr(
        delegated, "_posix_direct_child_exited_unreaped", lambda _pid: True
    )
    monkeypatch.setattr(
        delegated.os,
        "killpg",
        lambda process_group, requested_signal: signals.append(
            (process_group, requested_signal)
        ),
    )

    with pytest.raises(
        delegated.DelegatedProcessTerminationError,
        match="reaped outside its owner",
    ):
        delegated._stop_posix_supervisor(ownership)

    assert ownership.termination_requested
    assert ownership.control_write.value == 98
    assert signals == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX supervisor is required")
def test_posix_stop_retries_control_write_interrupted_before_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control_read, control_write = os.pipe()
    attempts: list[bytes] = []
    original_write = delegated.os.write
    ownership = delegated._PosixSupervisorOwnership(
        pid=4848,
        process_group=4848,
        control_write=delegated._OwnedPosixFd(control_write),
    )

    def interrupt_first_write(descriptor: int, payload: bytes) -> int:
        attempts.append(payload)
        if len(attempts) == 1:
            raise KeyboardInterrupt("synthetic interruption before control delivery")
        return original_write(descriptor, payload)

    def reap_only_after_retry(
        _ownership: delegated._PosixSupervisorOwnership, *, timeout: float
    ) -> None:
        assert timeout > 0
        assert attempts == [b"K", b"K"]

    monkeypatch.setattr(delegated.os, "write", interrupt_first_write)
    monkeypatch.setattr(
        delegated, "_posix_direct_child_exited_unreaped", lambda _pid: False
    )
    monkeypatch.setattr(
        delegated, "_wait_and_reap_posix_supervisor", reap_only_after_retry
    )
    monkeypatch.setattr(
        delegated,
        "_wait_for_posix_group_absence",
        lambda _process_group, *, timeout: None,
    )

    try:
        with pytest.raises(
            KeyboardInterrupt, match="synthetic interruption before control delivery"
        ):
            delegated._stop_posix_supervisor(ownership)
    finally:
        if ownership.control_write.value is not None:
            ownership.control_write.close()
        os.close(control_read)

    assert attempts == [b"K", b"K"]
    assert ownership.termination_confirmed


@pytest.mark.skipif(os.name == "nt", reason="POSIX supervisor is required")
def test_posix_duplicate_control_bytes_each_request_group_termination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "duplicate-control-late-marker.txt"
    process = delegated._PosixAdapterProcess()
    delegated._launch_posix_adapter_process(
        process,
        [
            sys.executable,
            "-c",
            "import time; from pathlib import Path; time.sleep(1); "
            f"Path({str(marker)!r}).write_text('late', encoding='utf-8')",
        ],
        root=tmp_path,
        stdin_handle=subprocess.DEVNULL,
        stdout_handle=subprocess.DEVNULL,
        stderr_handle=subprocess.DEVNULL,
        environment=None,
    )
    assert process.ownership.control_write.value is not None

    try:
        os.kill(process.pid, signal.SIGSTOP)
        os.write(process.ownership.control_write.value, b"KK")
        os.kill(process.pid, signal.SIGCONT)
        delegated._wait_and_reap_posix_supervisor(process.ownership, timeout=2)
        delegated._wait_for_posix_group_absence(process.pid, timeout=2)
        process.ownership.termination_confirmed = True
        time.sleep(1.1)
        assert not marker.exists()
    finally:
        if not process.ownership.termination_confirmed:
            try:
                os.kill(process.pid, signal.SIGCONT)
            except ProcessLookupError:
                pass
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.supervisor.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        for descriptor in (process.result_read, process.ownership.control_write):
            try:
                descriptor.close()
            except OSError:
                pass


@pytest.mark.skipif(os.name == "nt", reason="POSIX supervisor is required")
def test_posix_launch_return_boundary_interruption_keeps_precreated_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "launch-return-boundary-late-marker.txt"
    captured: list[delegated._PosixAdapterProcess] = []
    original_launch = delegated._launch_posix_adapter_process

    def launch_then_interrupt(*args: object, **kwargs: object) -> None:
        result = original_launch(*args, **kwargs)  # type: ignore[arg-type]
        process = (
            args[0]
            if args and isinstance(args[0], delegated._PosixAdapterProcess)
            else result
        )
        assert isinstance(process, delegated._PosixAdapterProcess)
        captured.append(process)
        raise KeyboardInterrupt("synthetic launch return boundary interruption")

    monkeypatch.setattr(
        delegated, "_launch_posix_adapter_process", launch_then_interrupt
    )

    try:
        with pytest.raises(
            KeyboardInterrupt, match="synthetic launch return boundary interruption"
        ):
            delegated._run_adapter_process(
                [
                    sys.executable,
                    "-c",
                    "import time; from pathlib import Path; time.sleep(0.8); "
                    f"Path({str(marker)!r}).write_text('late', encoding='utf-8')",
                ],
                root=tmp_path,
                request_bytes=b"{}",
                timeout_seconds=30,
            )

        assert len(captured) == 1
        assert captured[0].ownership.termination_confirmed
        time.sleep(0.9)
        assert not marker.exists()
    finally:
        for process in captured:
            if not process.ownership.termination_confirmed:
                delegated._stop_posix_adapter_process_group(process)


@pytest.mark.skipif(os.name == "nt", reason="POSIX supervisor is required")
def test_posix_child_end_close_return_interruption_uses_caller_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "child-end-close-boundary-late-marker.txt"
    spawned: list[subprocess.Popen[bytes]] = []
    original_popen = subprocess.Popen
    original_close_child_ends = delegated._close_posix_launch_child_ends

    def record_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        process = original_popen(*args, **kwargs)
        spawned.append(process)
        return process

    def close_then_interrupt(*args: object, **kwargs: object) -> None:
        result = original_close_child_ends(*args, **kwargs)  # type: ignore[arg-type]
        assert result is None
        raise KeyboardInterrupt("synthetic child-end close return interruption")

    monkeypatch.setattr(subprocess, "Popen", record_popen)
    monkeypatch.setattr(
        delegated, "_close_posix_launch_child_ends", close_then_interrupt
    )

    try:
        with pytest.raises(
            KeyboardInterrupt, match="synthetic child-end close return interruption"
        ):
            delegated._run_adapter_process(
                [
                    sys.executable,
                    "-c",
                    "import time; from pathlib import Path; time.sleep(0.8); "
                    f"Path({str(marker)!r}).write_text('late', encoding='utf-8')",
                ],
                root=tmp_path,
                request_bytes=b"{}",
                timeout_seconds=30,
            )

        assert len(spawned) == 1
        assert spawned[0].returncode is not None
        time.sleep(0.9)
        assert not marker.exists()
    finally:
        for process in spawned:
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=2)


@pytest.mark.skipif(os.name == "nt", reason="POSIX resource ownership is required")
def test_posix_channel_factory_return_before_store_closes_both_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_descriptors: list[int] = []
    original_socketpair = delegated.socket.socketpair

    def capture_socketpair(*args: object, **kwargs: object) -> object:
        endpoints = original_socketpair(*args, **kwargs)
        created_descriptors.extend(endpoint.fileno() for endpoint in endpoints)
        return endpoints

    monkeypatch.setattr(delegated.socket, "socketpair", capture_socketpair)

    class Holder:
        channel: object | None = None

    holder = Holder()

    def allocate_then_store() -> None:
        holder.channel = delegated._create_cloexec_pipe()

    opcodes = {
        instruction.offset: instruction.opname
        for instruction in dis.get_instructions(allocate_then_store)
    }

    def interrupt_store(frame: object, event: str, _arg: object) -> object:
        if getattr(frame, "f_code", None) is allocate_then_store.__code__:
            if event == "call":
                frame.f_trace_opcodes = True  # type: ignore[attr-defined]
            elif event == "opcode" and opcodes.get(frame.f_lasti) == "STORE_ATTR":  # type: ignore[attr-defined]
                raise KeyboardInterrupt("synthetic channel STORE_ATTR interruption")
        return interrupt_store

    test_runner_trace = sys.gettrace()

    def sentinel_trace(_frame: object, _event: str, _arg: object) -> object:
        return sentinel_trace

    sys.settrace(sentinel_trace)
    try:
        previous_trace = sys.gettrace()
        sys.settrace(interrupt_store)
        try:
            with pytest.raises(
                KeyboardInterrupt, match="synthetic channel STORE_ATTR interruption"
            ):
                allocate_then_store()
        finally:
            sys.settrace(previous_trace)
        assert sys.gettrace() is sentinel_trace
    finally:
        sys.settrace(test_runner_trace)
    assert sys.gettrace() is test_runner_trace
    gc.collect()
    assert len(created_descriptors) == 2
    assert_posix_descriptors_closed(created_descriptors)


@pytest.mark.skipif(os.name == "nt", reason="POSIX resource ownership is required")
def test_posix_endpoint_close_interruption_retains_owning_cleanup() -> None:
    created = delegated._create_cloexec_pipe()
    read_end, write_end = created.read, created.write
    created_descriptors = [read_end.value, write_end.value]
    assert all(descriptor is not None for descriptor in created_descriptors)
    write_end.close()
    source, first_line = inspect.getsourcelines(type(read_end).close)
    native_close_line = next(
        first_line + offset
        for offset, line in enumerate(source)
        if line.strip() in {"os.close(descriptor)", "resource.close()"}
    )

    def interrupt_native_close(frame: object, event: str, _arg: object) -> object:
        if (
            getattr(frame, "f_code", None) is type(read_end).close.__code__
            and event == "line"
            and getattr(frame, "f_lineno", None) == native_close_line
        ):
            raise KeyboardInterrupt("synthetic endpoint native close interruption")
        return interrupt_native_close

    previous_trace = sys.gettrace()
    sys.settrace(interrupt_native_close)
    try:
        with pytest.raises(
            KeyboardInterrupt, match="synthetic endpoint native close interruption"
        ):
            read_end.close()
    finally:
        sys.settrace(previous_trace)
    assert sys.gettrace() is previous_trace
    # 中断发生在 native close 前，owner 必须仍保有同一 socket 对象，
    # 从而可安全重试，而不是遗失或按可能复用的裸 FD 再次关闭。
    assert read_end.value is not None
    read_end.close()
    gc.collect()
    assert_posix_descriptors_closed(
        [descriptor for descriptor in created_descriptors if descriptor is not None]
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX resource ownership is required")
def test_temporary_file_return_before_payload_store_closes_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = delegated._PosixAdapterProcess()
    channel_descriptors: list[int] = []
    payload_descriptors: list[int] = []
    original_channel_factory = delegated._create_cloexec_pipe
    original_temporary_file = delegated.tempfile.TemporaryFile

    def capture_channel() -> object:
        channel = original_channel_factory()
        assert channel.read.value is not None
        assert channel.write.value is not None
        channel_descriptors.extend((channel.read.value, channel.write.value))
        return channel

    def capture_temporary_file(*args: object, **kwargs: object) -> object:
        handle = original_temporary_file(*args, **kwargs)
        payload_descriptors.append(handle.fileno())
        return handle

    instructions = {
        instruction.offset: instruction
        for instruction in dis.get_instructions(
            delegated._prepare_posix_launch_resources
        )
    }

    def interrupt_store(frame: object, event: str, _arg: object) -> object:
        if (
            getattr(frame, "f_code", None)
            is delegated._prepare_posix_launch_resources.__code__
        ):
            if event == "call":
                frame.f_trace_opcodes = True  # type: ignore[attr-defined]
            elif event == "opcode":
                instruction = instructions.get(frame.f_lasti)  # type: ignore[attr-defined]
                if (
                    instruction is not None
                    and instruction.opname == "STORE_ATTR"
                    and instruction.argval == "payload_handle"
                ):
                    raise KeyboardInterrupt(
                        "synthetic payload STORE_ATTR interruption"
                    )
        return interrupt_store

    monkeypatch.setattr(delegated, "_create_cloexec_pipe", capture_channel)
    monkeypatch.setattr(delegated.tempfile, "TemporaryFile", capture_temporary_file)
    previous_trace = sys.gettrace()
    sys.settrace(interrupt_store)
    try:
        with pytest.raises(
            KeyboardInterrupt, match="synthetic payload STORE_ATTR interruption"
        ):
            delegated._prepare_posix_launch_resources(
                process,
                [sys.executable, "-c", "raise SystemExit(0)"],
                root=tmp_path,
                environment={},
            )
    finally:
        sys.settrace(previous_trace)
    assert sys.gettrace() is previous_trace
    delegated._stop_posix_adapter_process_group(process)
    gc.collect()
    assert process.payload_handle is None
    assert len(channel_descriptors) == 8
    assert len(payload_descriptors) == 1
    assert_posix_descriptors_closed([*channel_descriptors, *payload_descriptors])


@pytest.mark.skipif(os.name == "nt", reason="POSIX resource ownership is required")
def test_posix_resource_prepare_return_interruption_uses_caller_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[delegated._PosixAdapterProcess] = []
    original_prepare = delegated._prepare_posix_launch_resources

    def prepare_then_interrupt(*args: object, **kwargs: object) -> None:
        original_prepare(*args, **kwargs)  # type: ignore[arg-type]
        process = args[0]
        assert isinstance(process, delegated._PosixAdapterProcess)
        captured.append(process)
        raise KeyboardInterrupt("synthetic resource prepare return interruption")

    monkeypatch.setattr(
        delegated, "_prepare_posix_launch_resources", prepare_then_interrupt
    )

    with pytest.raises(
        KeyboardInterrupt, match="synthetic resource prepare return interruption"
    ):
        delegated._run_adapter_process(
            [sys.executable, "-c", "raise SystemExit(0)"],
            root=tmp_path,
            request_bytes=b"{}",
            timeout_seconds=30,
        )

    assert len(captured) == 1
    process = captured[0]
    assert not process.child_creation_possible
    assert process.ownership.termination_confirmed
    assert process.payload_handle is None
    assert all(
        descriptor.value is None
        for descriptor in (
            process.status_read,
            process.status_write,
            process.gate_read,
            process.gate_write,
            process.control_read,
            process.ownership.control_write,
            process.result_read,
            process.result_write,
        )
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX supervisor is required")
def test_posix_stop_retries_after_control_was_delivered_then_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "delivered-then-interrupted-late-marker.txt"
    process = delegated._PosixAdapterProcess()
    delegated._launch_posix_adapter_process(
        process,
        [
            sys.executable,
            "-c",
            "import time; from pathlib import Path; time.sleep(1); "
            f"Path({str(marker)!r}).write_text('late', encoding='utf-8')",
        ],
        root=tmp_path,
        stdin_handle=subprocess.DEVNULL,
        stdout_handle=subprocess.DEVNULL,
        stderr_handle=subprocess.DEVNULL,
        environment=None,
    )
    original_write = delegated.os.write
    original_wait = delegated._wait_and_reap_posix_supervisor
    attempts: list[bytes] = []

    def deliver_then_interrupt(descriptor: int, payload: bytes) -> int:
        written = original_write(descriptor, payload)
        if payload == b"K":
            attempts.append(payload)
            if len(attempts) == 1:
                raise KeyboardInterrupt(
                    "synthetic interruption after kernel accepted control K"
                )
        return written

    def resume_then_wait(
        ownership: delegated._PosixSupervisorOwnership, *, timeout: float
    ) -> None:
        os.kill(ownership.pid, signal.SIGCONT)
        original_wait(ownership, timeout=timeout)

    monkeypatch.setattr(delegated.os, "write", deliver_then_interrupt)
    monkeypatch.setattr(
        delegated, "_wait_and_reap_posix_supervisor", resume_then_wait
    )

    try:
        os.kill(process.pid, signal.SIGSTOP)
        with pytest.raises(BaseException) as raised:
            delegated._stop_posix_supervisor(process.ownership)
        assert exception_tree_contains(
            raised.value,
            KeyboardInterrupt,
            "after kernel accepted control K",
        )
        assert attempts == [b"K", b"K"]
        assert process.ownership.termination_confirmed
        time.sleep(1.1)
        assert not marker.exists()
    finally:
        if not process.ownership.termination_confirmed:
            try:
                os.kill(process.pid, signal.SIGCONT)
            except ProcessLookupError:
                pass
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if process.supervisor is not None:
                process.supervisor.wait(timeout=2)


@pytest.mark.skipif(os.name == "nt", reason="POSIX supervisor is required")
def test_posix_continuous_indeterminate_control_writes_retain_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control_read, control_write = os.pipe()
    attempts: list[bytes] = []
    ownership = delegated._PosixSupervisorOwnership(
        pid=4949,
        process_group=4949,
        control_write=delegated._OwnedPosixFd(control_write),
    )

    def always_interrupt(_descriptor: int, payload: bytes) -> int:
        attempts.append(payload)
        raise KeyboardInterrupt("synthetic continuous indeterminate write")

    monkeypatch.setattr(delegated.os, "write", always_interrupt)
    monkeypatch.setattr(
        delegated, "_posix_direct_child_exited_unreaped", lambda _pid: False
    )

    try:
        with pytest.raises(
            delegated.DelegatedProcessTerminationError,
            match="delivery could not be confirmed",
        ):
            delegated._stop_posix_supervisor(ownership)
        assert attempts == [b"K", b"K", b"K"]
        assert ownership.termination_delivery == "indeterminate"
        assert ownership.control_write.value == control_write
        assert not ownership.termination_confirmed
    finally:
        ownership.control_write.close()
        os.close(control_read)


@pytest.mark.skipif(os.name == "nt", reason="POSIX supervisor is required")
def test_posix_control_writer_close_interruption_is_single_consumption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control_read, control_write = os.pipe()
    original_close = delegated.os.close
    close_calls: list[int] = []
    ownership = delegated._PosixSupervisorOwnership(
        pid=5050,
        process_group=5050,
        control_write=delegated._OwnedPosixFd(control_write),
    )

    def close_then_interrupt(descriptor: int) -> None:
        if descriptor == control_write:
            close_calls.append(descriptor)
            original_close(descriptor)
            raise KeyboardInterrupt("synthetic post-close interruption")
        original_close(descriptor)

    monkeypatch.setattr(delegated.os, "close", close_then_interrupt)
    monkeypatch.setattr(
        delegated, "_wait_and_reap_posix_supervisor", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        delegated,
        "_wait_for_posix_group_absence",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(
        KeyboardInterrupt, match="synthetic post-close interruption"
    ):
        delegated._stop_posix_supervisor(ownership)

    assert ownership.termination_confirmed
    assert ownership.control_write.value is None
    delegated._stop_posix_supervisor(ownership)
    assert close_calls == [control_write]
    original_close(control_read)


@pytest.mark.skipif(os.name == "nt", reason="POSIX supervisor is required")
@pytest.mark.parametrize("fault_mode", ("interrupt", "delayed-return"))
def test_posix_supervisor_retries_self_kill_until_signal_takes_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault_mode: str
) -> None:
    original_launcher = delegated._POSIX_GATE_LAUNCHER
    fault = (
        "            if _dww_kill_attempts == 1:\n"
        "                raise KeyboardInterrupt('synthetic pre-kill interruption')\n"
        if fault_mode == "interrupt"
        else "            if _dww_kill_attempts == 1:\n                continue\n"
    )
    needle = (
        "    while True:\n"
        "        try:\n"
        "            os.killpg(os.getpgrp(), signal.SIGKILL)\n"
    )
    injected = (
        "    _dww_kill_attempts = 0\n"
        "    while True:\n"
        "        try:\n"
        "            _dww_kill_attempts += 1\n"
        f"{fault}"
        "            os.killpg(os.getpgrp(), signal.SIGKILL)\n"
    )
    launcher = original_launcher.replace(needle, injected, 1)
    assert launcher != original_launcher
    monkeypatch.setattr(delegated, "_POSIX_GATE_LAUNCHER", launcher)

    result = delegated._run_adapter_process(
        [sys.executable, "-c", "raise SystemExit(0)"],
        root=tmp_path,
        request_bytes=b"{}",
        timeout_seconds=30,
    )

    assert result.returncode == 0


@pytest.mark.skipif(os.name == "nt", reason="POSIX supervisor is required")
def test_posix_adapter_child_never_execs_after_control_fd_close_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "adapter-ran-after-control-close-failure.txt"
    original_launcher = delegated._POSIX_GATE_LAUNCHER
    launcher = original_launcher.replace(
        "        if not close_control_fds_for_exec():\n",
        "        if True:\n",
        1,
    )
    assert launcher != original_launcher
    monkeypatch.setattr(delegated, "_POSIX_GATE_LAUNCHER", launcher)

    result = delegated._run_adapter_process(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; "
            f"Path({str(marker)!r}).write_text('ran', encoding='utf-8')",
        ],
        root=tmp_path,
        request_bytes=b"{}",
        timeout_seconds=30,
    )

    assert result.returncode == 126
    assert not marker.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX supervisor is required")
def test_posix_unidentified_cleanup_reenters_after_request_helper_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "unidentified-request-boundary-late-marker.txt"
    original_popen = subprocess.Popen
    original_request = delegated._request_posix_supervisor_self_termination
    spawned: list[subprocess.Popen[bytes]] = []
    requests = 0

    def create_then_interrupt(*args: object, **kwargs: object) -> object:
        process = original_popen(*args, **kwargs)
        spawned.append(process)
        raise KeyboardInterrupt("synthetic Popen return interruption")

    def unreadable_status(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt("synthetic status identity interruption")

    def request_then_interrupt(
        ownership: delegated._PosixSupervisorOwnership,
    ) -> BaseException | None:
        nonlocal requests
        requests += 1
        if requests == 1:
            raise KeyboardInterrupt("synthetic request helper return interruption")
        return original_request(ownership)

    monkeypatch.setattr(subprocess, "Popen", create_then_interrupt)
    monkeypatch.setattr(delegated, "_read_posix_launcher_status", unreadable_status)
    monkeypatch.setattr(
        delegated,
        "_request_posix_supervisor_self_termination",
        request_then_interrupt,
    )

    with pytest.raises(BaseException) as raised:
        delegated._run_adapter_process(
            [
                sys.executable,
                "-c",
                "import time; from pathlib import Path; time.sleep(0.8); "
                f"Path({str(marker)!r}).write_text('late', encoding='utf-8')",
            ],
            root=tmp_path,
            request_bytes=b"{}",
            timeout_seconds=30,
        )

    assert exception_tree_contains(
        raised.value,
        KeyboardInterrupt,
        "synthetic Popen return interruption",
    )
    assert exception_tree_contains(
        raised.value,
        delegated.DelegatedProcessTerminationError,
        "stopping the delegated adapter process tree",
    )
    assert requests >= 2
    assert len(spawned) == 1
    deadline = time.monotonic() + 2
    while spawned[0].poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert spawned[0].poll() is not None
    time.sleep(0.9)
    assert not marker.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups are required")
def test_posix_stop_forces_term_ignoring_child_after_root_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawned = tmp_path / "posix-tree-spawned.txt"
    late_marker = tmp_path / "late-posix-child.txt"
    child = (
        "import signal,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"Path({str(spawned)!r}).write_text('spawned', encoding='utf-8'); "
        "time.sleep(0.8); "
        f"Path({str(late_marker)!r}).write_text('late', encoding='utf-8')"
    )
    parent = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
        "time.sleep(30)"
    )
    original_sleep = time.sleep

    def interrupt_after_spawn(_seconds: float) -> None:
        deadline = time.monotonic() + 5
        while not spawned.exists() and time.monotonic() < deadline:
            original_sleep(0.01)
        assert spawned.exists()
        raise KeyboardInterrupt("synthetic POSIX interruption")

    monkeypatch.setattr(delegated, "ADAPTER_TERMINATION_GRACE_SECONDS", 0.2)
    monkeypatch.setattr(delegated, "_adapter_poll_pause", interrupt_after_spawn)

    with pytest.raises(KeyboardInterrupt, match="synthetic POSIX interruption"):
        delegated._run_adapter_process(
            [sys.executable, "-c", parent],
            root=tmp_path,
            request_bytes=b"{}",
            timeout_seconds=30,
        )

    original_sleep(1)
    assert not late_marker.exists()


def test_invoke_returns_the_exact_idempotent_start_receipt(git_repo: Path) -> None:
    declare_adapter(git_repo)
    repo = GitRepo(git_repo)
    inspection = inspect_delegated(repo.root, repo.common_dir)
    approve_delegated(
        repo.root,
        repo.common_dir,
        fingerprint=inspection["adapter"]["fingerprint"],
    )

    response = invoke_delegated(
        repo.root,
        repo.common_dir,
        operation="start",
        request={"name": "task", "request_id": "request-1"},
        timeout_seconds=30,
    )

    assert response["ok"] is True
    assert response["result"]["request_id"] == "request-1"
    assert Path(response["result"]["worktree"]).is_absolute()
    assert set(response["result"]) == {
        "request_id",
        "task_id",
        "worktree",
        "slot_id",
        "branch",
        "base_head",
        "request_reused",
    }


def test_invoke_rejects_undeclared_capability(git_repo: Path) -> None:
    declare_delegated_adapter(
        git_repo,
        capabilities=("status",),
        approve=False,
    )
    repo = GitRepo(git_repo)
    inspection = inspect_delegated(repo.root, repo.common_dir)
    approve_delegated(
        repo.root,
        repo.common_dir,
        fingerprint=inspection["adapter"]["fingerprint"],
    )

    with pytest.raises(DelegatedContractError, match="does not declare capability"):
        invoke_delegated(
            repo.root,
            repo.common_dir,
            operation="start",
            request={"name": "task", "request_id": "request-1"},
            timeout_seconds=30,
        )


def test_v1_capability_interface_contains_only_proven_operations() -> None:
    assert ALLOWED_CAPABILITIES == {"status", "start"}
    assert delegated.VERIFIED_INPUT_ROOT_ENV == "DWW_VERIFIED_INPUT_ROOT"
    assert delegated.REPOSITORY_ROOT_ENV == "DWW_REPOSITORY_ROOT"


def test_operation_requests_are_exact_before_the_adapter_runs(git_repo: Path) -> None:
    declare_adapter(git_repo)
    repo = GitRepo(git_repo)
    inspection = inspect_delegated(repo.root, repo.common_dir)
    approve_delegated(
        repo.root,
        repo.common_dir,
        fingerprint=inspection["adapter"]["fingerprint"],
    )

    with pytest.raises(DelegatedContractError, match="status request must be an empty"):
        invoke_delegated(
            repo.root,
            repo.common_dir,
            operation="status",
            request={"purpose": "orchestration-capacity"},
            timeout_seconds=30,
        )
    with pytest.raises(DelegatedContractError, match="exactly name and request_id"):
        invoke_delegated(
            repo.root,
            repo.common_dir,
            operation="start",
            request={"name": "task"},
            timeout_seconds=30,
        )


def test_operation_results_are_exact_and_typed() -> None:
    with pytest.raises(DelegatedContractError, match="exactly available_slots"):
        delegated._validate_operation_result(
            "status",
            {"available_slots": 1, "slots": []},
            request={},
            max_parallel=2,
        )
    with pytest.raises(DelegatedContractError, match="unexpected fields"):
        delegated._validate_operation_result(
            "start",
            {
                "request_id": "request-1",
                "task_id": "task-1",
                "worktree": "/tmp/worktree",
                "slot_id": "slot-01",
                "branch": "codex/example",
                "base_head": "0" * 40,
                "request_reused": False,
                "fields": {},
            },
            request={"name": "task", "request_id": "request-1"},
            max_parallel=2,
        )


def test_adapter_reported_failure_is_not_wrapped_as_success(git_repo: Path) -> None:
    declare_adapter(git_repo)
    adapter_path = git_repo / "scripts" / "dww_adapter.py"
    adapter_path.write_text(
        """import json
import sys

request = json.load(sys.stdin)
json.dump(
    {
        "schema_version": 1,
        "adapter_id": request["adapter_id"],
        "fingerprint": request["fingerprint"],
        "operation": request["operation"],
        "ok": False,
        "error": "native controller refused the request",
    },
    sys.stdout,
)
""",
        encoding="utf-8",
    )
    repo = GitRepo(git_repo)
    inspection = inspect_delegated(repo.root, repo.common_dir)
    approve_delegated(
        repo.root,
        repo.common_dir,
        fingerprint=inspection["adapter"]["fingerprint"],
    )

    with pytest.raises(DelegatedContractError, match="reported status failure"):
        invoke_delegated(
            repo.root,
            repo.common_dir,
            operation="status",
            request={},
            timeout_seconds=30,
        )


def test_adapter_process_bounds_output_and_terminates_child_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(delegated, "MAX_ADAPTER_STDOUT_BYTES", 128)
    with pytest.raises(DelegatedContractError, match="stdout exceeds"):
        delegated._run_adapter_process(
            [sys.executable, "-c", "print('x' * 1024)"],
            root=tmp_path,
            request_bytes=b"{}",
            timeout_seconds=5,
        )

    marker = tmp_path / "late-child.txt"
    child = (
        "import time; from pathlib import Path; time.sleep(1); "
        f"Path({str(marker)!r}).write_text('late', encoding='utf-8')"
    )
    parent = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(30)"
    )
    started = time.monotonic()
    with pytest.raises(DelegatedContractError, match="timed out"):
        delegated._run_adapter_process(
            [sys.executable, "-c", parent],
            root=tmp_path,
            request_bytes=json.dumps({}).encode("utf-8"),
            timeout_seconds=0.1,
        )
    assert time.monotonic() - started < 15
    time.sleep(1.2)
    assert not marker.exists()


def test_approval_requires_the_exact_reported_fingerprint(git_repo: Path) -> None:
    declare_adapter(git_repo)
    repo = GitRepo(git_repo)

    with pytest.raises(DelegatedContractError, match="does not match"):
        approve_delegated(repo.root, repo.common_dir, fingerprint="0" * 64)

    assert not (repo.local_dir / "delegated-adapter-approval.json").exists()


def test_exact_revoke_returns_route_to_safe_defer(git_repo: Path) -> None:
    declare_adapter(git_repo)
    repo = GitRepo(git_repo)
    inspection = inspect_delegated(repo.root, repo.common_dir)
    fingerprint = inspection["adapter"]["fingerprint"]
    approve_delegated(repo.root, repo.common_dir, fingerprint=fingerprint)

    with pytest.raises(DelegatedContractError, match="Adapter id does not match"):
        revoke_delegated(
            repo.common_dir,
            adapter_id="wrong-adapter",
            fingerprint=fingerprint,
        )
    assert repository_route(repo)["action"] == "delegated"

    revoked = revoke_delegated(
        repo.common_dir,
        adapter_id="example-worktree-flow",
        fingerprint=fingerprint,
    )

    assert revoked["revoked"] is True
    assert repository_route(repo)["action"] == "defer"
    assert repository_route(repo)["reason"] == "delegated-approval-required"
