from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .routing import WORKFLOW_MARKERS

DELEGATED_SCHEMA = 1
DELEGATED_CONTRACT = ".solo-ai/delegated.toml"
APPROVAL_SCHEMA = 1
APPROVAL_FILE = "delegated-adapter-approval.json"
MAX_CONTRACT_BYTES = 128 * 1024
MAX_TRACKED_INPUTS = 32
MAX_INPUT_BYTES = 4 * 1024 * 1024
MAX_TOTAL_INPUT_BYTES = 16 * 1024 * 1024
MAX_ADAPTER_REQUEST_BYTES = 1024 * 1024
MAX_ADAPTER_STDOUT_BYTES = 1024 * 1024
MAX_ADAPTER_STDERR_BYTES = 64 * 1024
MAX_ADAPTER_ERROR_CHARS = 1200
ADAPTER_POLL_SECONDS = 0.05
ADAPTER_TERMINATION_GRACE_SECONDS = 5.0
ALLOWED_RUNTIMES = {"python", "powershell", "sh"}
ALLOWED_CAPABILITIES = {
    "start",
    "status",
}
_ADAPTER_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
_OPAQUE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_GIT_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


class DelegatedContractError(RuntimeError):
    """委托契约无效；调用方必须失败关闭，不能猜测仓库意图。"""


@dataclass(frozen=True)
class DelegatedContract:
    adapter_id: str
    runtime: str
    entrypoint: str
    workflow_markers: tuple[str, ...]
    tracked_inputs: tuple[str, ...]
    capabilities: tuple[str, ...]
    max_parallel: int
    fingerprint: str

    def public(self) -> dict[str, Any]:
        return {
            "id": self.adapter_id,
            "runtime": self.runtime,
            "entrypoint": self.entrypoint,
            "workflow_markers": list(self.workflow_markers),
            "tracked_inputs": list(self.tracked_inputs),
            "capabilities": list(self.capabilities),
            "max_parallel": self.max_parallel,
            "fingerprint": self.fingerprint,
        }


def _stable_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _strict_json_loads(value: str) -> Any:
    def reject_constant(constant: str) -> None:
        raise ValueError(f"non-standard JSON constant: {constant}")

    return json.loads(value, parse_constant=reject_constant)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _exact_relative_path(raw: Any, *, field: str) -> str:
    if not isinstance(raw, str) or not raw:
        raise DelegatedContractError(f"{field} must be a non-empty string")
    if "\\" in raw:
        raise DelegatedContractError(f"{field} must use forward slashes")
    candidate = PurePosixPath(raw)
    if (
        raw.startswith("/")
        or candidate == PurePosixPath(".")
        or ".." in candidate.parts
        or any(character in raw for character in "*?[")
        or (len(raw) >= 3 and raw[0].isalpha() and raw[1:3] == ":/")
    ):
        raise DelegatedContractError(
            f"{field} must be an exact repository-relative path"
        )
    return candidate.as_posix()


def _string_list(raw: Any, *, field: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        raise DelegatedContractError(f"{field} must be a non-empty array")
    if not all(isinstance(item, str) and item for item in raw):
        raise DelegatedContractError(f"{field} must contain non-empty strings")
    normalized = tuple(sorted(raw))
    if len({item.casefold() for item in normalized}) != len(normalized):
        raise DelegatedContractError(f"{field} contains duplicate values")
    return normalized


def _path_list(raw: Any, *, field: str) -> tuple[str, ...]:
    values = _string_list(raw, field=field)
    normalized = tuple(
        sorted(_exact_relative_path(value, field=field) for value in values)
    )
    if len({item.casefold() for item in normalized}) != len(normalized):
        raise DelegatedContractError(f"{field} contains duplicate paths")
    return normalized


def _tracked(root: Path, relative: str) -> bool:
    completed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--error-unmatch", "--", relative],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    return completed.returncode == 0


def _plain_tracked_file(root: Path, relative: str, *, field: str) -> Path:
    path = root / relative
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve())
    except (OSError, ValueError) as exc:
        raise DelegatedContractError(
            f"{field} must resolve to a file inside the repository: {relative}"
        ) from exc
    if path.is_symlink() or not path.is_file() or resolved != path.absolute():
        raise DelegatedContractError(
            f"{field} must be a regular file, not a link: {relative}"
        )
    if not _tracked(root, relative):
        raise DelegatedContractError(f"{field} must be tracked by Git: {relative}")
    return path


def _load_contract(root: Path) -> DelegatedContract:
    contract_path = root / DELEGATED_CONTRACT
    try:
        if contract_path.stat().st_size > MAX_CONTRACT_BYTES:
            raise DelegatedContractError(
                f"{DELEGATED_CONTRACT} exceeds the {MAX_CONTRACT_BYTES}-byte limit"
            )
        raw_bytes = contract_path.read_bytes()
        data = tomllib.loads(raw_bytes.decode("utf-8"))
    except DelegatedContractError:
        raise
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise DelegatedContractError(f"Invalid {DELEGATED_CONTRACT}: {exc}") from exc
    allowed = {
        "schema_version",
        "id",
        "runtime",
        "entrypoint",
        "workflow_markers",
        "tracked_inputs",
        "capabilities",
        "max_parallel",
    }
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise DelegatedContractError(
            f"Unsupported delegated contract fields: {', '.join(unknown)}"
        )
    schema = data.get("schema_version")
    if isinstance(schema, bool) or schema != DELEGATED_SCHEMA:
        raise DelegatedContractError(
            f"Unsupported delegated contract schema; expected {DELEGATED_SCHEMA}"
        )
    adapter_id = data.get("id")
    if not isinstance(adapter_id, str) or not _ADAPTER_ID.fullmatch(adapter_id):
        raise DelegatedContractError(
            "id must be a lowercase adapter identifier up to 64 characters"
        )
    runtime = data.get("runtime")
    if runtime not in ALLOWED_RUNTIMES:
        raise DelegatedContractError("runtime must be one of: python, powershell, sh")
    entrypoint = _exact_relative_path(data.get("entrypoint"), field="entrypoint")
    workflow_markers = _path_list(
        data.get("workflow_markers"), field="workflow_markers"
    )
    tracked_inputs = _path_list(data.get("tracked_inputs"), field="tracked_inputs")
    if len(tracked_inputs) > MAX_TRACKED_INPUTS:
        raise DelegatedContractError(
            f"tracked_inputs cannot contain more than {MAX_TRACKED_INPUTS} files"
        )
    capabilities = _string_list(data.get("capabilities"), field="capabilities")
    unsupported = sorted(set(capabilities) - ALLOWED_CAPABILITIES)
    if unsupported:
        raise DelegatedContractError(
            f"Unsupported delegated capabilities: {', '.join(unsupported)}"
        )
    max_parallel = data.get("max_parallel")
    if (
        isinstance(max_parallel, bool)
        or not isinstance(max_parallel, int)
        or not 1 <= max_parallel <= 32
    ):
        raise DelegatedContractError("max_parallel must be between 1 and 32")
    detected = tuple(
        sorted(relative for relative in WORKFLOW_MARKERS if (root / relative).exists())
    )
    if detected != workflow_markers:
        raise DelegatedContractError(
            "workflow_markers must exactly match the mature workflows detected in this checkout"
        )
    required_inputs = {entrypoint, *workflow_markers}
    missing_inputs = sorted(required_inputs - set(tracked_inputs))
    if missing_inputs:
        raise DelegatedContractError(
            "tracked_inputs must include the entrypoint and every workflow marker: "
            + ", ".join(missing_inputs)
        )
    input_records: list[dict[str, str]] = []
    total_input_bytes = 0
    _plain_tracked_file(root, DELEGATED_CONTRACT, field="contract")
    for relative in tracked_inputs:
        path = _plain_tracked_file(root, relative, field="tracked_inputs")
        size = path.stat().st_size
        if size > MAX_INPUT_BYTES:
            raise DelegatedContractError(
                f"tracked input exceeds the {MAX_INPUT_BYTES}-byte limit: {relative}"
            )
        total_input_bytes += size
        if total_input_bytes > MAX_TOTAL_INPUT_BYTES:
            raise DelegatedContractError(
                f"tracked inputs exceed the {MAX_TOTAL_INPUT_BYTES}-byte total limit"
            )
        input_records.append({"path": relative, "sha256": _sha256_file(path)})
    suffixes = {"python": ".py", "powershell": ".ps1", "sh": ".sh"}
    if Path(entrypoint).suffix.casefold() != suffixes[runtime]:
        raise DelegatedContractError(
            f"entrypoint extension must be {suffixes[runtime]} for runtime {runtime}"
        )
    normalized = {
        "schema_version": DELEGATED_SCHEMA,
        "id": adapter_id,
        "runtime": runtime,
        "entrypoint": entrypoint,
        "workflow_markers": list(workflow_markers),
        "tracked_inputs": input_records,
        "capabilities": list(capabilities),
        "max_parallel": max_parallel,
        "contract_sha256": _sha256_bytes(raw_bytes),
    }
    fingerprint = _sha256_bytes(_stable_json(normalized).encode("utf-8"))
    return DelegatedContract(
        adapter_id=adapter_id,
        runtime=runtime,
        entrypoint=entrypoint,
        workflow_markers=workflow_markers,
        tracked_inputs=tracked_inputs,
        capabilities=capabilities,
        max_parallel=max_parallel,
        fingerprint=fingerprint,
    )


def _approval_path(common_dir: Path) -> Path:
    return common_dir / "solo-ai" / APPROVAL_FILE


def _approval(common_dir: Path) -> tuple[dict[str, Any] | None, str | None]:
    path = _approval_path(common_dir)
    if not path.exists():
        return None, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"Local delegated approval is unreadable: {exc}"
    if (
        not isinstance(data, dict)
        or isinstance(data.get("schema_version"), bool)
        or data.get("schema_version") != APPROVAL_SCHEMA
    ):
        return None, "Local delegated approval has an unsupported schema"
    return data, None


def inspect_delegated(root: Path, common_dir: Path) -> dict[str, Any]:
    """只读检查契约和本机批准；所有异常都折叠为可路由的失败关闭状态。"""
    contract_path = root / DELEGATED_CONTRACT
    if not contract_path.exists():
        return {"declared": False}
    try:
        contract = _load_contract(root)
    except DelegatedContractError as exc:
        return {
            "declared": True,
            "valid": False,
            "approved": False,
            "reason": "invalid-contract",
            "error": str(exc),
        }
    approval, approval_error = _approval(common_dir)
    approved = bool(
        approval
        and approval.get("adapter_id") == contract.adapter_id
        and isinstance(approval.get("fingerprint"), str)
        and secrets.compare_digest(str(approval["fingerprint"]), contract.fingerprint)
    )
    result = {
        "declared": True,
        "valid": True,
        "approved": approved,
        "reason": "approved" if approved else "approval-required",
        "adapter": contract.public(),
    }
    if approval_error:
        result["reason"] = "invalid-approval"
        result["approval_error"] = approval_error
    return result


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{uuid.uuid4().hex}.tmp"
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    os.replace(temporary, path)


def approve_delegated(
    root: Path, common_dir: Path, *, fingerprint: str
) -> dict[str, Any]:
    inspection = inspect_delegated(root, common_dir)
    if not inspection.get("valid"):
        raise DelegatedContractError(
            str(inspection.get("error") or "No valid delegated contract is declared")
        )
    adapter = inspection["adapter"]
    actual = str(adapter["fingerprint"])
    if not secrets.compare_digest(actual, fingerprint):
        raise DelegatedContractError(
            "Fingerprint does not match the current delegated contract and tracked inputs"
        )
    payload = {
        "schema_version": APPROVAL_SCHEMA,
        "adapter_id": adapter["id"],
        "fingerprint": actual,
        "accepted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _atomic_write_json(_approval_path(common_dir), payload)
    return {"approved": True, "adapter": adapter}


def revoke_delegated(
    common_dir: Path, *, adapter_id: str, fingerprint: str
) -> dict[str, Any]:
    approval, approval_error = _approval(common_dir)
    if approval_error:
        raise DelegatedContractError(approval_error)
    if approval is None:
        return {"revoked": False, "reason": "not-approved"}
    approved_id = approval.get("adapter_id")
    approved_fingerprint = approval.get("fingerprint")
    if approved_id != adapter_id or not isinstance(approved_fingerprint, str):
        raise DelegatedContractError(
            "Adapter id does not match the current local delegated approval"
        )
    if not secrets.compare_digest(approved_fingerprint, fingerprint):
        raise DelegatedContractError(
            "Fingerprint does not match the current local delegated approval"
        )
    try:
        _approval_path(common_dir).unlink()
    except OSError as exc:
        raise DelegatedContractError(
            f"Could not revoke the local delegated approval: {exc}"
        ) from exc
    return {
        "revoked": True,
        "adapter_id": adapter_id,
        "fingerprint": fingerprint,
    }


def _adapter_argv(root: Path, contract: DelegatedContract) -> list[str]:
    entrypoint = str(root / contract.entrypoint)
    if contract.runtime == "python":
        executable = shutil.which("uv")
        if not executable:
            raise DelegatedContractError("The approved Python adapter requires uv")
        return [executable, "run", "--script", entrypoint]
    if contract.runtime == "powershell":
        executable = (
            shutil.which("pwsh")
            or shutil.which("powershell.exe")
            or shutil.which("powershell")
        )
        if not executable:
            raise DelegatedContractError(
                "The approved PowerShell adapter requires pwsh or powershell"
            )
        return [executable, "-NoProfile", "-File", entrypoint]
    executable = shutil.which("sh")
    if not executable:
        raise DelegatedContractError("The approved shell adapter requires sh")
    return [executable, entrypoint]


@dataclass(frozen=True)
class _AdapterProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes


def _bounded_file_bytes(handle: Any, *, limit: int, stream: str) -> bytes:
    size = os.fstat(handle.fileno()).st_size
    if size > limit:
        raise DelegatedContractError(
            f"Delegated adapter {stream} exceeds the {limit}-byte limit"
        )
    handle.seek(0)
    return handle.read(limit + 1)


def _stop_adapter_process(process: subprocess.Popen[bytes]) -> None:
    # Hook 只导入本模块执行路由检查，不能因此加载主 CLI 的 psutil 依赖。
    from .util import _stop_process_tree

    if process.poll() is not None:
        return
    _stop_process_tree(process.pid, force=False)
    try:
        process.wait(timeout=ADAPTER_TERMINATION_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        _stop_process_tree(process.pid, force=True)
    try:
        process.wait(timeout=ADAPTER_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise DelegatedContractError(
            "Delegated adapter process tree could not be confirmed stopped"
        ) from exc


def _run_adapter_process(
    argv: list[str],
    *,
    root: Path,
    request_bytes: bytes,
    timeout_seconds: float,
) -> _AdapterProcessResult:
    if len(request_bytes) > MAX_ADAPTER_REQUEST_BYTES:
        raise DelegatedContractError(
            f"Delegated adapter request exceeds the {MAX_ADAPTER_REQUEST_BYTES}-byte limit"
        )
    creation_flags = (
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    )
    with (
        tempfile.TemporaryFile() as stdin_handle,
        tempfile.TemporaryFile() as stdout_handle,
        tempfile.TemporaryFile() as stderr_handle,
    ):
        stdin_handle.write(request_bytes)
        stdin_handle.seek(0)
        try:
            process = subprocess.Popen(
                argv,
                cwd=root,
                stdin=stdin_handle,
                stdout=stdout_handle,
                stderr=stderr_handle,
                start_new_session=os.name != "nt",
                creationflags=creation_flags,
            )
        except OSError as exc:
            raise DelegatedContractError(
                f"Could not start the delegated adapter: {exc}"
            ) from exc
        deadline = time.monotonic() + timeout_seconds
        violation: DelegatedContractError | None = None
        while process.poll() is None:
            if os.fstat(stdout_handle.fileno()).st_size > MAX_ADAPTER_STDOUT_BYTES:
                violation = DelegatedContractError(
                    f"Delegated adapter stdout exceeds the {MAX_ADAPTER_STDOUT_BYTES}-byte limit"
                )
                break
            if os.fstat(stderr_handle.fileno()).st_size > MAX_ADAPTER_STDERR_BYTES:
                violation = DelegatedContractError(
                    f"Delegated adapter stderr exceeds the {MAX_ADAPTER_STDERR_BYTES}-byte limit"
                )
                break
            if time.monotonic() >= deadline:
                violation = DelegatedContractError(
                    f"Delegated adapter timed out after {timeout_seconds:g} seconds"
                )
                break
            time.sleep(ADAPTER_POLL_SECONDS)
        if violation is not None:
            _stop_adapter_process(process)
            raise violation
        returncode = process.wait()
        stdout = _bounded_file_bytes(
            stdout_handle, limit=MAX_ADAPTER_STDOUT_BYTES, stream="stdout"
        )
        stderr = _bounded_file_bytes(
            stderr_handle, limit=MAX_ADAPTER_STDERR_BYTES, stream="stderr"
        )
        return _AdapterProcessResult(returncode, stdout, stderr)


def _require_operation_text(
    request: dict[str, Any], field: str, *, opaque: bool = False
) -> str:
    value = request.get(field)
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or any(ord(character) < 32 for character in value)
        or (opaque and _OPAQUE_ID.fullmatch(value) is None)
    ):
        raise DelegatedContractError(
            f"Delegated {field} must be a valid non-empty string up to 128 characters"
        )
    return value


def _validate_operation_request(operation: str, request: Any) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise DelegatedContractError("Delegated request must be an object")
    if operation == "status":
        if request:
            raise DelegatedContractError(
                "Delegated status request must be an empty object"
            )
        return request
    if operation == "start":
        if set(request) != {"name", "request_id"}:
            raise DelegatedContractError(
                "Delegated start request requires exactly name and request_id"
            )
        _require_operation_text(request, "name")
        _require_operation_text(request, "request_id", opaque=True)
        return request
    raise DelegatedContractError(f"Unsupported delegated operation: {operation}")


def _require_result_text(
    result: dict[str, Any],
    field: str,
    *,
    pattern: re.Pattern[str] | None = None,
    max_length: int = 4096,
) -> str:
    value = result.get(field)
    if (
        not isinstance(value, str)
        or not value
        or len(value) > max_length
        or any(ord(character) < 32 for character in value)
        or (pattern is not None and pattern.fullmatch(value) is None)
    ):
        raise DelegatedContractError(
            f"Successful delegated result requires valid {field}"
        )
    return value


def _validate_operation_result(
    operation: str,
    result: dict[str, Any],
    *,
    request: dict[str, Any],
    max_parallel: int,
) -> None:
    if operation == "status":
        if set(result) != {"available_slots"}:
            raise DelegatedContractError(
                "Successful delegated status result requires exactly available_slots"
            )
        available_slots = result.get("available_slots")
        if (
            isinstance(available_slots, bool)
            or not isinstance(available_slots, int)
            or not 0 <= available_slots <= max_parallel
        ):
            raise DelegatedContractError(
                "Delegated status result requires available_slots between zero and max_parallel"
            )
        return
    if operation == "start":
        required = {
            "request_id",
            "task_id",
            "worktree",
            "slot_id",
            "branch",
            "base_head",
            "request_reused",
        }
        missing = sorted(required - set(result))
        unexpected = sorted(set(result) - required)
        if missing or unexpected:
            detail = []
            if missing:
                detail.append("missing " + ", ".join(missing))
            if unexpected:
                detail.append("unexpected " + ", ".join(unexpected))
            raise DelegatedContractError(
                "Successful delegated start result fields are invalid: "
                + "; ".join(detail)
            )
        request_id = _require_result_text(result, "request_id", pattern=_OPAQUE_ID)
        if not secrets.compare_digest(request_id, str(request["request_id"])):
            raise DelegatedContractError(
                "Delegated start result request_id does not match the request"
            )
        _require_result_text(result, "task_id", max_length=128)
        worktree = _require_result_text(result, "worktree", max_length=32768)
        if not Path(worktree).is_absolute():
            raise DelegatedContractError(
                "Successful delegated start result requires an absolute worktree"
            )
        _require_result_text(result, "slot_id", max_length=128)
        _require_result_text(result, "branch", max_length=1024)
        _require_result_text(result, "base_head", pattern=_GIT_OBJECT_ID)
        if not isinstance(result.get("request_reused"), bool):
            raise DelegatedContractError(
                "Successful delegated start result requires boolean request_reused"
            )
        return
    raise DelegatedContractError(f"Unsupported delegated operation: {operation}")


def invoke_delegated(
    root: Path,
    common_dir: Path,
    *,
    operation: str,
    request: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    if operation not in ALLOWED_CAPABILITIES:
        raise DelegatedContractError(f"Unsupported delegated operation: {operation}")
    if not 0 < timeout_seconds <= 3600:
        raise DelegatedContractError("timeout_seconds must be between 0 and 3600")
    request = _validate_operation_request(operation, request)
    inspection = inspect_delegated(root, common_dir)
    if not inspection.get("approved"):
        raise DelegatedContractError(
            "The current delegated contract and tracked inputs are not locally approved"
        )
    adapter = inspection["adapter"]
    if operation not in adapter["capabilities"]:
        raise DelegatedContractError(
            f"Adapter {adapter['id']} does not declare capability {operation}"
        )
    contract = _load_contract(root)
    if not secrets.compare_digest(contract.fingerprint, str(adapter["fingerprint"])):
        raise DelegatedContractError(
            "Delegated contract changed after approval inspection; retry from inspect"
        )
    envelope = {
        "schema_version": DELEGATED_SCHEMA,
        "adapter_id": contract.adapter_id,
        "fingerprint": contract.fingerprint,
        "operation": operation,
        "request": request,
    }
    try:
        request_bytes = _stable_json(envelope).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise DelegatedContractError("Delegated request is not strict JSON") from exc
    completed = _run_adapter_process(
        _adapter_argv(root, contract),
        root=root,
        request_bytes=request_bytes,
        timeout_seconds=timeout_seconds,
    )
    if completed.returncode != 0:
        # 与进程树工具一样延迟导入，保持只读 Hook 的依赖面不变。
        from .util import redact_text

        detail = ""
        try:
            detail = redact_text(
                completed.stderr.decode("utf-8", errors="strict")
            ).strip()
        except UnicodeDecodeError:
            detail = "adapter stderr is not valid UTF-8"
        if detail:
            detail = " ".join(detail.split())[:MAX_ADAPTER_ERROR_CHARS]
            detail = f": {detail}"
        raise DelegatedContractError(
            f"Delegated adapter failed with exit code {completed.returncode}{detail}"
        )
    try:
        stdout = completed.stdout.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise DelegatedContractError(
            "Delegated adapter stdout must be valid UTF-8"
        ) from exc
    try:
        response = _strict_json_loads(stdout)
    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise DelegatedContractError(
            "Delegated adapter must emit exactly one JSON response on stdout"
        ) from exc
    if not isinstance(response, dict):
        raise DelegatedContractError("Delegated adapter response must be an object")
    common = {
        "schema_version",
        "adapter_id",
        "fingerprint",
        "operation",
        "ok",
    }
    expected = common | ({"result"} if response.get("ok") is True else {"error"})
    if set(response) != expected:
        raise DelegatedContractError(
            "Delegated adapter response fields do not match its outcome"
        )
    if (
        isinstance(response.get("schema_version"), bool)
        or response.get("schema_version") != DELEGATED_SCHEMA
        or response.get("adapter_id") != contract.adapter_id
        or response.get("fingerprint") != contract.fingerprint
        or response.get("operation") != operation
        or not isinstance(response.get("ok"), bool)
    ):
        raise DelegatedContractError(
            "Delegated adapter response does not match the approved request envelope"
        )
    if response["ok"]:
        if not isinstance(response.get("result"), dict):
            raise DelegatedContractError(
                "Successful delegated adapter response requires an object result"
            )
        _validate_operation_result(
            operation,
            response["result"],
            request=request,
            max_parallel=contract.max_parallel,
        )
        return response
    if not isinstance(response.get("error"), str) or not response["error"]:
        raise DelegatedContractError(
            "Failed delegated adapter response requires a non-empty error"
        )
    from .util import redact_text

    detail = redact_text(str(response["error"]))
    detail = " ".join(detail.split())[:MAX_ADAPTER_ERROR_CHARS]
    raise DelegatedContractError(
        f"Delegated adapter reported {operation} failure: {detail}"
    )
