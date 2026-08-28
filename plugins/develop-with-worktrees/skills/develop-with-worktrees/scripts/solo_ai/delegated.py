from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import tempfile
import time
import tomllib
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
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
ADAPTER_TERMINATION_POLL_SECONDS = 0.02
VERIFIED_INPUT_ROOT_ENV = "DWW_VERIFIED_INPUT_ROOT"
REPOSITORY_ROOT_ENV = "DWW_REPOSITORY_ROOT"
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


class DelegatedProcessTerminationError(DelegatedContractError):
    """委托进程树未能确认停止；执行闭包必须保留。"""


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


@dataclass(frozen=True)
class DelegatedMaterial:
    """最终指纹核验实际读取的原始契约与声明输入字节。"""

    contract_bytes: bytes
    tracked_input_bytes: tuple[tuple[str, bytes], ...]


@dataclass(frozen=True)
class VerifiedInputClosure:
    """仓库外私有执行闭包及其中的批准入口。"""

    root: Path
    entrypoint: Path


@dataclass(frozen=True)
class _VerifiedClosureManifest:
    files: tuple[tuple[Path, dict[str, Any]], ...]
    directories: tuple[tuple[Path, dict[str, Any]], ...]
    children: tuple[tuple[Path, tuple[str, ...]], ...]


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


def _bounded_file_bytes_for_fingerprint(
    path: Path, *, limit: int, description: str
) -> bytes:
    try:
        with path.open("rb") as handle:
            content = handle.read(limit + 1)
    except OSError as exc:
        raise DelegatedContractError(f"Could not read {description}: {exc}") from exc
    if len(content) > limit:
        raise DelegatedContractError(f"{description} exceeds the {limit}-byte limit")
    return content


def _load_contract_material(
    root: Path,
) -> tuple[DelegatedContract, DelegatedMaterial]:
    contract_path = root / DELEGATED_CONTRACT
    try:
        _plain_tracked_file(root, DELEGATED_CONTRACT, field="contract")
        raw_bytes = _bounded_file_bytes_for_fingerprint(
            contract_path,
            limit=MAX_CONTRACT_BYTES,
            description=DELEGATED_CONTRACT,
        )
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
    captured_inputs: list[tuple[str, bytes]] = []
    for relative in tracked_inputs:
        path = _plain_tracked_file(root, relative, field="tracked_inputs")
        content = _bounded_file_bytes_for_fingerprint(
            path,
            limit=MAX_INPUT_BYTES,
            description=f"tracked input {relative}",
        )
        total_input_bytes += len(content)
        if total_input_bytes > MAX_TOTAL_INPUT_BYTES:
            raise DelegatedContractError(
                f"tracked inputs exceed the {MAX_TOTAL_INPUT_BYTES}-byte total limit"
            )
        input_records.append({"path": relative, "sha256": _sha256_bytes(content)})
        captured_inputs.append((relative, content))
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
    contract = DelegatedContract(
        adapter_id=adapter_id,
        runtime=runtime,
        entrypoint=entrypoint,
        workflow_markers=workflow_markers,
        tracked_inputs=tracked_inputs,
        capabilities=capabilities,
        max_parallel=max_parallel,
        fingerprint=fingerprint,
    )
    if not any(relative == entrypoint for relative, _content in captured_inputs):
        raise DelegatedContractError("The delegated entrypoint could not be captured")
    return contract, DelegatedMaterial(
        contract_bytes=raw_bytes,
        tracked_input_bytes=tuple(captured_inputs),
    )


def _load_contract(root: Path) -> DelegatedContract:
    contract, _material = _load_contract_material(root)
    return contract


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


def _adapter_argv(
    root: Path,
    contract: DelegatedContract,
    *,
    entrypoint_path: Path | None = None,
) -> list[str]:
    entrypoint = str(entrypoint_path or (root / contract.entrypoint))
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


def _material_files(material: DelegatedMaterial) -> tuple[tuple[str, bytes], ...]:
    files: dict[str, bytes] = {DELEGATED_CONTRACT: material.contract_bytes}
    canonical_paths = {DELEGATED_CONTRACT.casefold(): DELEGATED_CONTRACT}
    for relative, content in material.tracked_input_bytes:
        canonical = relative.casefold()
        previous = canonical_paths.get(canonical)
        if previous is not None and previous != relative:
            raise DelegatedContractError(
                "Verified delegated inputs contain a case-colliding path: "
                f"{previous}, {relative}"
            )
        canonical_paths[canonical] = relative
        if relative in files and files[relative] != content:
            raise DelegatedContractError(
                f"Verified delegated input bytes conflict at {relative}"
            )
        files[relative] = content
    return tuple(sorted(files.items()))


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _cleanup_verified_input_closure(manifest: _VerifiedClosureManifest) -> None:
    # 延迟加载主 CLI 工具，避免只读路由检查因此扩大依赖面。
    from .util import SoloAIError, delete_plain_path_if_unchanged, snapshot_plain_path

    try:
        expected_children = dict(manifest.children)
        for directory, expected in manifest.directories:
            if snapshot_plain_path(directory) != expected:
                raise SoloAIError(
                    f"Verified input directory changed before cleanup: {directory}"
                )
            observed = tuple(sorted(entry.name for entry in os.scandir(directory)))
            if observed != expected_children[directory]:
                raise SoloAIError(
                    f"Verified input directory contents changed before cleanup: {directory}"
                )
        for path, expected in manifest.files:
            if snapshot_plain_path(path) != expected:
                raise SoloAIError(
                    f"Verified input file changed before cleanup: {path}"
                )
        for path, expected in sorted(
            manifest.files, key=lambda item: len(item[0].parts), reverse=True
        ):
            delete_plain_path_if_unchanged(path, expected)
        for path, expected in sorted(
            manifest.directories,
            key=lambda item: len(item[0].parts),
            reverse=True,
        ):
            delete_plain_path_if_unchanged(path, expected)
    except (FileNotFoundError, OSError, SoloAIError) as exc:
        raise DelegatedContractError(
            "Could not safely remove the verified delegated input closure; "
            f"the changed path was preserved: {exc}"
        ) from exc


def _exception_requires_closure_preservation(error: BaseException) -> bool:
    if isinstance(error, DelegatedProcessTerminationError):
        return True
    if isinstance(error, BaseExceptionGroup):
        return any(
            _exception_requires_closure_preservation(item)
            for item in error.exceptions
        )
    return False


def _combined_failures(
    message: str, first: BaseException, second: BaseException
) -> BaseExceptionGroup:
    return BaseExceptionGroup(message, [first, second])


@contextmanager
def _verified_input_closure(
    repository_root: Path,
    contract: DelegatedContract,
    material: DelegatedMaterial,
) -> Iterator[VerifiedInputClosure]:
    """把最终核验字节冻结到仓库外私有树，并保留声明文件相对结构。"""
    from .util import SoloAIError, snapshot_plain_path

    closure_root = Path(tempfile.mkdtemp(prefix="dww-verified-delegated-")).resolve(
        strict=True
    )
    files: dict[Path, dict[str, Any]] = {}
    directories: dict[Path, dict[str, Any]] = {}
    children: dict[Path, set[str]] = {closure_root: set()}
    try:
        try:
            os.chmod(closure_root, 0o700)
            directories[closure_root] = snapshot_plain_path(closure_root)
        except (OSError, SoloAIError) as exc:
            raise DelegatedContractError(
                f"Could not freeze the delegated input closure root: {exc}"
            ) from exc
        if _path_is_within(closure_root, repository_root):
            raise DelegatedContractError(
                "Verified delegated input closure must be outside the repository"
            )

        for relative, content in _material_files(material):
            destination = closure_root.joinpath(*PurePosixPath(relative).parts)
            parent = closure_root
            for part in PurePosixPath(relative).parts[:-1]:
                child = parent / part
                if child not in directories:
                    try:
                        os.mkdir(child, 0o700)
                        directories[child] = snapshot_plain_path(child)
                    except (OSError, SoloAIError) as exc:
                        raise DelegatedContractError(
                            "Could not create the verified delegated input directory "
                            f"{relative}: {exc}"
                        ) from exc
                    children[parent].add(part)
                    children[child] = set()
                parent = child
            descriptor = -1
            try:
                descriptor = os.open(
                    destination,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                with os.fdopen(descriptor, "wb") as handle:
                    descriptor = -1
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                files[destination] = snapshot_plain_path(destination)
                children[parent].add(destination.name)
            except (OSError, SoloAIError) as exc:
                if descriptor >= 0:
                    os.close(descriptor)
                raise DelegatedContractError(
                    f"Could not create verified delegated input {relative}: {exc}"
                ) from exc

        manifest = _VerifiedClosureManifest(
            files=tuple(files.items()),
            directories=tuple(directories.items()),
            children=tuple(
                (path, tuple(sorted(names))) for path, names in children.items()
            ),
        )
    except BaseException:
        if directories:
            partial_manifest = _VerifiedClosureManifest(
                files=tuple(files.items()),
                directories=tuple(directories.items()),
                children=tuple(
                    (path, tuple(sorted(names))) for path, names in children.items()
                ),
            )
            _cleanup_verified_input_closure(partial_manifest)
        raise

    closure = VerifiedInputClosure(
        root=closure_root,
        entrypoint=closure_root.joinpath(*PurePosixPath(contract.entrypoint).parts),
    )
    try:
        yield closure
    except BaseException as body_error:
        if _exception_requires_closure_preservation(body_error):
            body_error.add_note(
                "Verified delegated input closure was preserved because the "
                f"owned process tree was not confirmed stopped: {closure.root}"
            )
            raise
        try:
            _cleanup_verified_input_closure(manifest)
        except BaseException as cleanup_error:  # noqa: BLE001 - 保留中断与清理双重证据
            raise _combined_failures(
                "Delegated invocation failed and its verified input closure "
                "also could not be cleaned safely",
                body_error,
                cleanup_error,
            ) from None
        raise
    else:
        _cleanup_verified_input_closure(manifest)


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


def _adapter_poll_pause(seconds: float) -> None:
    """独立轮询 seam，确保中断回归不会干扰终止阶段的等待。"""
    time.sleep(seconds)


def _posix_process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError as exc:
        raise DelegatedProcessTerminationError(
            "Delegated adapter process group still exists but cannot be inspected"
        ) from exc


def _wait_for_posix_process_group_exit(
    process: subprocess.Popen[bytes], *, timeout: float
) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        root_finished = process.poll() is not None
        group_exists = _posix_process_group_exists(process.pid)
        if root_finished and not group_exists:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(ADAPTER_TERMINATION_POLL_SECONDS)


def _stop_posix_adapter_process_group(process: subprocess.Popen[bytes]) -> None:
    if not _posix_process_group_exists(process.pid):
        if process.poll() is None:
            raise DelegatedProcessTerminationError(
                "Delegated adapter root left its owned process group and could "
                "not be confirmed stopped"
            )
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        if process.poll() is None:
            raise DelegatedProcessTerminationError(
                "Delegated adapter root could not be confirmed stopped after "
                "its process group disappeared"
            )
        return
    except OSError as exc:
        raise DelegatedProcessTerminationError(
            "Could not request delegated adapter process-group termination"
        ) from exc
    if _wait_for_posix_process_group_exit(
        process, timeout=ADAPTER_TERMINATION_GRACE_SECONDS
    ):
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        process.poll()
    except OSError as exc:
        raise DelegatedProcessTerminationError(
            "Could not force delegated adapter process-group termination"
        ) from exc
    if not _wait_for_posix_process_group_exit(
        process, timeout=ADAPTER_TERMINATION_GRACE_SECONDS
    ):
        raise DelegatedProcessTerminationError(
            "Delegated adapter process group could not be confirmed stopped"
        )


class _WindowsBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _WindowsIoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _WindowsExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _WindowsBasicLimitInformation),
        ("IoInfo", _WindowsIoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _WindowsBasicAccountingInformation(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_int64),
        ("TotalKernelTime", ctypes.c_int64),
        ("ThisPeriodTotalUserTime", ctypes.c_int64),
        ("ThisPeriodTotalKernelTime", ctypes.c_int64),
        ("TotalPageFaultCount", ctypes.c_uint32),
        ("TotalProcesses", ctypes.c_uint32),
        ("ActiveProcesses", ctypes.c_uint32),
        ("TotalTerminatedProcesses", ctypes.c_uint32),
    ]


@dataclass
class _WindowsAdapterJob:
    """Popen 前建立的 Windows 所有权边界；不依赖可复用的 PID。"""

    handle: int | None
    empty_confirmed: bool = False


def _windows_kernel32() -> Any:
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _windows_ntdll() -> Any:
    return ctypes.WinDLL("ntdll", use_last_error=True)


def _create_windows_adapter_job() -> _WindowsAdapterJob:
    kernel32 = _windows_kernel32()
    create_job = kernel32.CreateJobObjectW
    create_job.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    create_job.restype = ctypes.c_void_p
    handle = create_job(None, None)
    if not handle:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
    job = _WindowsAdapterJob(int(handle))
    info = _WindowsExtendedLimitInformation()
    info.BasicLimitInformation.LimitFlags = 0x00002000
    set_information = kernel32.SetInformationJobObject
    set_information.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    set_information.restype = ctypes.c_int
    try:
        configured = set_information(
            ctypes.c_void_p(job.handle),
            9,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
    except BaseException as configuration_error:
        try:
            _close_windows_adapter_job(job)
        except BaseException as close_error:
            raise _combined_failures(
                "Windows adapter Job configuration and close both failed",
                configuration_error,
                close_error,
            ) from None
        raise
    if not configured:
        error = ctypes.get_last_error()
        try:
            _close_windows_adapter_job(job)
        except OSError as close_error:
            raise OSError(
                error,
                "SetInformationJobObject failed and its empty Job could not close",
            ) from close_error
        raise OSError(error, "SetInformationJobObject failed")
    return job


def _close_windows_adapter_job(job: _WindowsAdapterJob) -> None:
    if job.handle is None:
        return
    handle = job.handle
    kernel32 = _windows_kernel32()
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    if not close_handle(ctypes.c_void_p(handle)):
        raise OSError(ctypes.get_last_error(), "CloseHandle failed for adapter job")
    job.handle = None


def _windows_process_handle(process: subprocess.Popen[bytes]) -> int:
    handle = getattr(process, "_handle", None)
    if handle is None:
        raise OSError("Popen did not expose its native Windows process handle")
    return int(handle)


def _assign_windows_adapter_job(
    job: _WindowsAdapterJob, process: subprocess.Popen[bytes]
) -> None:
    if job.handle is None:
        raise OSError("Windows adapter Job Object was already closed")
    kernel32 = _windows_kernel32()
    assign = kernel32.AssignProcessToJobObject
    assign.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    assign.restype = ctypes.c_int
    if not assign(
        ctypes.c_void_p(job.handle),
        ctypes.c_void_p(_windows_process_handle(process)),
    ):
        raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")


def _confirm_windows_adapter_job_ownership(
    job: _WindowsAdapterJob, process: subprocess.Popen[bytes]
) -> None:
    if job.handle is None:
        raise OSError("Windows adapter Job Object was already closed")
    kernel32 = _windows_kernel32()
    is_process_in_job = kernel32.IsProcessInJob
    is_process_in_job.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_int),
    ]
    is_process_in_job.restype = ctypes.c_int
    belongs = ctypes.c_int()
    if not is_process_in_job(
        ctypes.c_void_p(_windows_process_handle(process)),
        ctypes.c_void_p(job.handle),
        ctypes.byref(belongs),
    ):
        raise OSError(ctypes.get_last_error(), "IsProcessInJob failed")
    if not belongs.value:
        raise OSError("Suspended delegated adapter root is not owned by its Job Object")


def _resume_windows_adapter_process(process: subprocess.Popen[bytes]) -> None:
    """恢复 CREATE_SUSPENDED 根进程，同时继续以原生 HANDLE 标识所有权。"""
    ntdll = _windows_ntdll()
    resume = ntdll.NtResumeProcess
    resume.argtypes = [ctypes.c_void_p]
    resume.restype = ctypes.c_long
    status = int(resume(ctypes.c_void_p(_windows_process_handle(process))))
    if status != 0:
        convert_status = ntdll.RtlNtStatusToDosError
        convert_status.argtypes = [ctypes.c_long]
        convert_status.restype = ctypes.c_uint32
        raise OSError(
            int(convert_status(status)),
            f"NtResumeProcess failed with NTSTATUS 0x{status & 0xFFFFFFFF:08x}",
        )


def _query_windows_adapter_job_active_processes(job: _WindowsAdapterJob) -> int:
    if job.handle is None:
        if job.empty_confirmed:
            return 0
        raise OSError("Windows adapter Job Object closed before empty confirmation")
    info = _WindowsBasicAccountingInformation()
    kernel32 = _windows_kernel32()
    query = kernel32.QueryInformationJobObject
    query.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    query.restype = ctypes.c_int
    if not query(
        ctypes.c_void_p(job.handle),
        1,
        ctypes.byref(info),
        ctypes.sizeof(info),
        None,
    ):
        raise OSError(ctypes.get_last_error(), "QueryInformationJobObject failed")
    return int(info.ActiveProcesses)


def _terminate_windows_adapter_job(job: _WindowsAdapterJob) -> None:
    if job.handle is None:
        raise OSError("Windows adapter Job Object was already closed")
    kernel32 = _windows_kernel32()
    terminate = kernel32.TerminateJobObject
    terminate.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    terminate.restype = ctypes.c_int
    if not terminate(ctypes.c_void_p(job.handle), 1):
        raise OSError(ctypes.get_last_error(), "TerminateJobObject failed")


def _wait_for_windows_adapter_job_exit(
    job: _WindowsAdapterJob, *, timeout: float
) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if _query_windows_adapter_job_active_processes(job) == 0:
            job.empty_confirmed = True
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(ADAPTER_TERMINATION_POLL_SECONDS)


def _ensure_windows_adapter_job_empty(job: _WindowsAdapterJob) -> None:
    if job.empty_confirmed:
        return
    failure: BaseException | None = None
    try:
        active_processes = _query_windows_adapter_job_active_processes(job)
        if active_processes:
            _terminate_windows_adapter_job(job)
        if not _wait_for_windows_adapter_job_exit(
            job, timeout=ADAPTER_TERMINATION_GRACE_SECONDS
        ):
            raise TimeoutError("Windows adapter Job Object remained active")
    except BaseException as exc:  # noqa: BLE001 - 关闭句柄仍须触发 KILL_ON_JOB_CLOSE
        failure = exc
    if failure is not None:
        try:
            _close_windows_adapter_job(job)
        except BaseException as close_error:  # noqa: BLE001 - 与确认失败合并报告
            failure = _combined_failures(
                "Windows adapter Job Object confirmation and close both failed",
                failure,
                close_error,
            )
        raise DelegatedProcessTerminationError(
            "Delegated adapter Job Object could not be confirmed empty"
        ) from failure


def _stop_windows_adapter_job(job: _WindowsAdapterJob) -> None:
    _ensure_windows_adapter_job_empty(job)
    try:
        _close_windows_adapter_job(job)
    except OSError as exc:
        raise DelegatedContractError(
            "Confirmed-empty delegated adapter Job Object handle could not be closed"
        ) from exc


def _ensure_adapter_process_boundary_empty(
    process: subprocess.Popen[bytes],
    *,
    windows_job: _WindowsAdapterJob | None = None,
) -> None:
    if os.name == "nt":
        if windows_job is None:
            raise DelegatedProcessTerminationError(
                "Delegated adapter has no Windows Job Object ownership boundary"
            )
        _ensure_windows_adapter_job_empty(windows_job)
    else:
        _stop_posix_adapter_process_group(process)


def _discard_suspended_windows_root(
    process: subprocess.Popen[bytes], job: _WindowsAdapterJob
) -> None:
    failures: list[BaseException] = []
    try:
        process.terminate()
        process.wait(timeout=ADAPTER_TERMINATION_GRACE_SECONDS)
    except BaseException as exc:  # noqa: BLE001 - 根进程尚未执行但仍必须确认终止
        failures.append(exc)
    try:
        _stop_windows_adapter_job(job)
    except BaseException as exc:  # noqa: BLE001 - 两个所有权边界均须保留证据
        failures.append(exc)
    if failures:
        error = DelegatedProcessTerminationError(
            "Suspended delegated adapter root could not be confirmed stopped"
        )
        if len(failures) == 1:
            raise error from failures[0]
        raise error from BaseExceptionGroup(
            "Suspended adapter cleanup failures", failures
        )


def _stop_adapter_process(
    process: subprocess.Popen[bytes],
    *,
    windows_job: _WindowsAdapterJob | None = None,
) -> None:
    try:
        if os.name == "nt":
            if windows_job is None:
                raise DelegatedProcessTerminationError(
                    "Delegated adapter has no Windows Job Object ownership boundary"
                )
            _stop_windows_adapter_job(windows_job)
            process.wait(timeout=ADAPTER_TERMINATION_GRACE_SECONDS)
        else:
            _stop_posix_adapter_process_group(process)
    except DelegatedContractError:
        raise
    except BaseException as exc:
        raise DelegatedProcessTerminationError(
            "Unexpected failure while stopping the delegated adapter process tree"
        ) from exc


def _run_adapter_process(
    argv: list[str],
    *,
    root: Path,
    request_bytes: bytes,
    timeout_seconds: float,
    environment: dict[str, str] | None = None,
) -> _AdapterProcessResult:
    if len(request_bytes) > MAX_ADAPTER_REQUEST_BYTES:
        raise DelegatedContractError(
            f"Delegated adapter request exceeds the {MAX_ADAPTER_REQUEST_BYTES}-byte limit"
        )
    creation_flags = 0
    if os.name == "nt":
        creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "CREATE_SUSPENDED", 0x00000004
        )
    with (
        tempfile.TemporaryFile() as stdin_handle,
        tempfile.TemporaryFile() as stdout_handle,
        tempfile.TemporaryFile() as stderr_handle,
    ):
        stdin_handle.write(request_bytes)
        stdin_handle.seek(0)
        windows_job: _WindowsAdapterJob | None = None
        if os.name == "nt":
            try:
                windows_job = _create_windows_adapter_job()
            except OSError as exc:
                raise DelegatedContractError(
                    f"Could not create the delegated adapter Job Object: {exc}"
                ) from exc
        try:
            process = subprocess.Popen(
                argv,
                cwd=root,
                stdin=stdin_handle,
                stdout=stdout_handle,
                stderr=stderr_handle,
                env=environment,
                start_new_session=os.name != "nt",
                creationflags=creation_flags,
            )
        except BaseException as launch_error:
            if windows_job is not None:
                try:
                    windows_job.empty_confirmed = True
                    _close_windows_adapter_job(windows_job)
                except BaseException as close_error:
                    raise _combined_failures(
                        "Delegated adapter launch and empty Job cleanup both failed",
                        launch_error,
                        close_error,
                    ) from None
            if isinstance(launch_error, OSError):
                raise DelegatedContractError(
                    f"Could not start the delegated adapter: {launch_error}"
                ) from launch_error
            raise
        resume_attempted = False
        resume_completed = False
        try:
            if os.name == "nt":
                assert windows_job is not None
                try:
                    _assign_windows_adapter_job(windows_job, process)
                    _confirm_windows_adapter_job_ownership(windows_job, process)
                except BaseException as assignment_error:
                    try:
                        _discard_suspended_windows_root(process, windows_job)
                    except BaseException as termination_error:
                        raise _combined_failures(
                            "Delegated adapter Job assignment failed and its "
                            "suspended root could not be confirmed stopped",
                            assignment_error,
                            termination_error,
                        ) from None
                    if isinstance(assignment_error, OSError):
                        raise DelegatedContractError(
                            "Could not assign the suspended delegated adapter "
                            f"to its Job Object: {assignment_error}"
                        ) from assignment_error
                    raise
                resume_attempted = True
                _resume_windows_adapter_process(process)
                resume_completed = True
            deadline = time.monotonic() + timeout_seconds
            while process.poll() is None:
                if os.fstat(stdout_handle.fileno()).st_size > MAX_ADAPTER_STDOUT_BYTES:
                    raise DelegatedContractError(
                        "Delegated adapter stdout exceeds the "
                        f"{MAX_ADAPTER_STDOUT_BYTES}-byte limit"
                    )
                if os.fstat(stderr_handle.fileno()).st_size > MAX_ADAPTER_STDERR_BYTES:
                    raise DelegatedContractError(
                        "Delegated adapter stderr exceeds the "
                        f"{MAX_ADAPTER_STDERR_BYTES}-byte limit"
                    )
                if time.monotonic() >= deadline:
                    raise DelegatedContractError(
                        "Delegated adapter timed out after "
                        f"{timeout_seconds:g} seconds"
                    )
                _adapter_poll_pause(ADAPTER_POLL_SECONDS)
            returncode = process.wait()
            _ensure_adapter_process_boundary_empty(
                process, windows_job=windows_job
            )
            stdout = _bounded_file_bytes(
                stdout_handle, limit=MAX_ADAPTER_STDOUT_BYTES, stream="stdout"
            )
            stderr = _bounded_file_bytes(
                stderr_handle, limit=MAX_ADAPTER_STDERR_BYTES, stream="stderr"
            )
            if windows_job is not None:
                _close_windows_adapter_job(windows_job)
            return _AdapterProcessResult(returncode, stdout, stderr)
        except BaseException as original_error:
            try:
                _stop_adapter_process(
                    process, windows_job=windows_job
                )
            except BaseException as termination_error:  # noqa: BLE001 - 终止失败必须保留闭包
                if not isinstance(termination_error, DelegatedContractError):
                    unexpected_termination_error = termination_error
                    termination_error = DelegatedProcessTerminationError(
                        "Unexpected failure while stopping the delegated adapter "
                        "process tree"
                    )
                    termination_error.__cause__ = unexpected_termination_error
                raise _combined_failures(
                    "Delegated adapter monitoring failed and its owned process "
                    "tree could not be confirmed stopped",
                    original_error,
                    termination_error,
                ) from None
            if os.name == "nt" and resume_attempted and not resume_completed:
                raise _combined_failures(
                    "Delegated adapter resume failed after execution could no "
                    "longer be excluded",
                    original_error,
                    DelegatedProcessTerminationError(
                        "Verified input closure was preserved because the "
                        "adapter resume outcome was indeterminate"
                    ),
                ) from None
            raise


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
    contract, material = _load_contract_material(root)
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
    with _verified_input_closure(root, contract, material) as closure:
        environment = dict(os.environ)
        environment[VERIFIED_INPUT_ROOT_ENV] = str(closure.root)
        environment[REPOSITORY_ROOT_ENV] = str(root.resolve())
        # Python 的 sibling import 默认会在脚本目录生成 __pycache__；执行闭包
        # 必须保持只读且可精确清理，因此强制关闭字节码落盘。
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = _run_adapter_process(
            _adapter_argv(root, contract, entrypoint_path=closure.entrypoint),
            root=root,
            request_bytes=request_bytes,
            timeout_seconds=timeout_seconds,
            environment=environment,
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
