from __future__ import annotations

import json
import os
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
def test_windows_freeze_finds_descendant_spawned_after_initial_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import psutil

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
    original_snapshot = delegated._windows_process_tree_snapshot
    original_windows_stop = delegated._stop_windows_adapter_process_tree
    initial_snapshot_seen = False

    def interrupt_after_root_ready(_seconds: float) -> None:
        deadline = time.monotonic() + 5
        while not root_ready.exists() and time.monotonic() < deadline:
            original_sleep(0.01)
        assert root_ready.exists()
        raise KeyboardInterrupt("synthetic Windows snapshot interruption")

    def stop_after_stale_snapshot(
        process: subprocess.Popen[bytes],
        *,
        root_identity: tuple[int, float] | None,
    ) -> None:
        nonlocal initial_snapshot_seen
        assert process.poll() is None
        assert parent_pid_path.exists()
        root = psutil.Process(process.pid)
        stale_snapshot = original_snapshot(root)
        assert stale_snapshot == []
        initial_snapshot_seen = True
        spawn_trigger.write_text("spawn", encoding="utf-8")
        deadline = time.monotonic() + 5
        while not tree_spawned.exists() and time.monotonic() < deadline:
            original_sleep(0.01)
        assert tree_spawned.exists()
        stale_snapshot_pending = True

        def return_stale_once(observed_root: object) -> list[object]:
            nonlocal stale_snapshot_pending
            if stale_snapshot_pending:
                stale_snapshot_pending = False
                return stale_snapshot
            return original_snapshot(observed_root)

        monkeypatch.setattr(
            delegated, "_windows_process_tree_snapshot", return_stale_once
        )
        original_windows_stop(process, root_identity=root_identity)

    monkeypatch.setattr(delegated, "_adapter_poll_pause", interrupt_after_root_ready)
    monkeypatch.setattr(
        delegated, "_stop_windows_adapter_process_tree", stop_after_stale_snapshot
    )

    try:
        with pytest.raises(
            KeyboardInterrupt, match="synthetic Windows snapshot interruption"
        ):
            delegated._run_adapter_process(
                [base_python, "-c", parent],
                root=tmp_path,
                request_bytes=b"{}",
                timeout_seconds=30,
            )

        assert initial_snapshot_seen
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


@pytest.mark.skipif(os.name != "nt", reason="Windows process identities are required")
@pytest.mark.parametrize("original_still_running", (False, True))
def test_windows_root_pid_reuse_never_targets_unrelated_process(
    monkeypatch: pytest.MonkeyPatch, original_still_running: bool
) -> None:
    import psutil

    class ReusedProcess:
        pid = 43123
        terminated = False

        def create_time(self) -> float:
            return 200.0

        def terminate(self) -> None:
            self.terminated = True

    class OriginalHandle:
        pid = 43123

        def poll(self) -> int | None:
            return None if original_still_running else 0

    unrelated = ReusedProcess()
    monkeypatch.setattr(psutil, "Process", lambda _pid: unrelated)

    if original_still_running:
        with pytest.raises(
            delegated.DelegatedProcessTerminationError, match="PID was reused"
        ):
            delegated._stop_windows_adapter_process_tree(
                OriginalHandle(), root_identity=(43123, 100.0)
            )
    else:
        delegated._stop_windows_adapter_process_tree(
            OriginalHandle(), root_identity=(43123, 100.0)
        )

    assert unrelated.terminated is False


@pytest.mark.skipif(os.name != "nt", reason="Windows process suspension is required")
def test_windows_suspend_failure_preserves_verified_input_closure(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import psutil

    declare_adapter(git_repo)
    adapter_path = git_repo / "scripts" / "dww_adapter.py"
    pid_path = git_repo / "suspend-failure-adapter.pid"
    adapter_path.write_text(
        "import os,time\n"
        "from pathlib import Path\n"
        f"Path({str(pid_path)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    git(git_repo, "add", "scripts/dww_adapter.py")
    git(git_repo, "commit", "-m", "add suspend failure fixture")
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
    original_suspend = psutil.Process.suspend

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
        raise KeyboardInterrupt("synthetic suspend failure interruption")

    def fail_root_suspend(process: psutil.Process) -> None:
        if pid_path.exists() and process.pid == int(
            pid_path.read_text(encoding="utf-8")
        ):
            raise psutil.AccessDenied(process.pid)
        original_suspend(process)

    monkeypatch.setattr(delegated, "_run_adapter_process", capture_then_run)
    monkeypatch.setattr(delegated, "_adapter_poll_pause", interrupt_after_start)
    monkeypatch.setattr(psutil.Process, "suspend", fail_root_suspend)

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
            "synthetic suspend failure interruption",
        )
        assert exception_tree_contains(
            raised.value,
            delegated.DelegatedProcessTerminationError,
            "could not be suspended",
        )
        assert len(closure_roots) == 1
        assert closure_roots[0].exists()
        assert any(
            str(closure_roots[0]) in note
            for note in getattr(raised.value, "__notes__", ())
        )
    finally:
        if pid_path.exists():
            from solo_ai.util import _stop_process_tree

            _stop_process_tree(int(pid_path.read_text(encoding="utf-8")), force=True)
        for closure_root in closure_roots:
            if closure_root.exists():
                shutil.rmtree(closure_root)


def test_output_read_interruption_after_natural_exit_keeps_original_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stop_polls: list[int | None] = []
    original_stop = delegated._stop_adapter_process

    def fail_read(*args: object, **kwargs: object) -> bytes:
        raise RuntimeError("synthetic output read failure")

    def capture_stop(
        process: object,
        *,
        windows_root_identity: tuple[int, float] | None = None,
    ) -> None:
        assert hasattr(process, "poll")
        stop_polls.append(process.poll())
        original_stop(
            process, windows_root_identity=windows_root_identity
        )

    monkeypatch.setattr(delegated, "_bounded_file_bytes", fail_read)
    monkeypatch.setattr(delegated, "_stop_adapter_process", capture_stop)

    with pytest.raises(RuntimeError, match="synthetic output read failure"):
        delegated._run_adapter_process(
            [sys.executable, "-c", "print('done')"],
            root=tmp_path,
            request_bytes=b"{}",
            timeout_seconds=30,
        )

    assert len(stop_polls) == 1
    assert stop_polls[0] is not None


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
    original_run = delegated._run_adapter_process
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

    def fail_stop(_process: object, **_kwargs: object) -> None:
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
        if pid_path.exists():
            from solo_ai.util import _stop_process_tree

            _stop_process_tree(int(pid_path.read_text(encoding="utf-8")), force=True)
        for closure_root in closure_roots:
            if closure_root.exists():
                shutil.rmtree(closure_root)


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
