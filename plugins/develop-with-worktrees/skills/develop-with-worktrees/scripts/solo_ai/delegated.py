from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import re
import secrets
import select
import shutil
import signal
import subprocess
import sys
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
MAX_ADAPTER_LAUNCH_PAYLOAD_BYTES = 2 * 1024 * 1024
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


@dataclass
class _OwnedPosixFd:
    """先移交状态、再关闭，避免异步异常后重复消费同一 FD 数值。"""

    value: int | None

    def close(self) -> None:
        if self.value is None:
            return
        descriptor = self.value
        self.value = None
        os.close(descriptor)


@dataclass
class _PosixLauncherIdentity:
    value: tuple[int, int] | None = None

    def require(self) -> tuple[int, int]:
        if self.value is None:
            raise ValueError("POSIX delegated launcher identity is unavailable")
        pid, process_group = self.value
        if pid <= 0 or pid != process_group:
            raise ValueError("POSIX delegated launcher identity is invalid")
        return pid, process_group


def _create_cloexec_pipe() -> tuple[_OwnedPosixFd, _OwnedPosixFd]:
    if hasattr(os, "pipe2"):
        read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
    else:  # pragma: no cover - 当前支持的 POSIX Python 都提供 pipe2
        read_fd, write_fd = os.pipe()
        os.set_inheritable(read_fd, False)
        os.set_inheritable(write_fd, False)
    return _OwnedPosixFd(read_fd), _OwnedPosixFd(write_fd)


_POSIX_GATE_LAUNCHER = r"""
import os
import sys

status_fd = int(sys.argv[1])
gate_fd = int(sys.argv[2])
payload_fd = int(sys.argv[3])
try:
    pid = os.getpid()
    pgid = os.getpgrp()
    frame = ("DWW1 %d %d\n" % (pid, pgid)).encode("ascii")
    pipe_buf = os.fpathconf(status_fd, "PC_PIPE_BUF")
    if pid <= 0 or pgid != pid or len(frame) > pipe_buf:
        os._exit(124)
    if os.write(status_fd, frame) != len(frame):
        os._exit(124)
except BaseException:
    os._exit(124)
finally:
    try:
        os.close(status_fd)
    except OSError:
        pass

try:
    gate = os.read(gate_fd, 2)
    tail = os.read(gate_fd, 1)
except BaseException:
    os._exit(125)
finally:
    try:
        os.close(gate_fd)
    except OSError:
        pass
if gate != b"G" or tail != b"":
    os._exit(125)

try:
    chunks = []
    size = 0
    while True:
        chunk = os.read(payload_fd, 65536)
        if not chunk:
            break
        size += len(chunk)
        if size > 2097152:
            os._exit(126)
        chunks.append(chunk)
    os.close(payload_fd)
    import json

    payload = json.loads(b"".join(chunks).decode("utf-8"))
    argv = payload["argv"]
    environment = payload["environment"]
    cwd = payload["cwd"]
    if not isinstance(argv, list) or not argv:
        os._exit(126)
    if not all(isinstance(item, str) and item for item in argv):
        os._exit(126)
    if not isinstance(environment, dict):
        os._exit(126)
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in environment.items()):
        os._exit(126)
    if not isinstance(cwd, str) or not cwd:
        os._exit(126)
    os.chdir(cwd)
    os.execvpe(argv[0], argv, environment)
except BaseException:
    os._exit(126)
"""


def _posix_launcher_environment() -> dict[str, str]:
    blocked_prefixes = ("PYTHON", "LD_", "DYLD_")
    return {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(blocked_prefixes)
    }


def _posix_launch_payload(
    argv: list[str], *, root: Path, environment: dict[str, str] | None
) -> bytes:
    effective_environment = dict(os.environ if environment is None else environment)
    payload = json.dumps(
        {
            "argv": argv,
            "cwd": str(root),
            "environment": effective_environment,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > MAX_ADAPTER_LAUNCH_PAYLOAD_BYTES:
        raise DelegatedContractError(
            "Delegated adapter launch environment exceeds the "
            f"{MAX_ADAPTER_LAUNCH_PAYLOAD_BYTES}-byte limit"
        )
    return payload


def _read_posix_launcher_status(
    status_fd: _OwnedPosixFd,
    identity: _PosixLauncherIdentity,
    *,
    timeout: float,
) -> None:
    if status_fd.value is None:
        raise DelegatedProcessTerminationError(
            "POSIX delegated launcher status descriptor was already closed"
        )
    descriptor = status_fd.value
    deadline = time.monotonic() + timeout
    frame = bytearray()
    os.set_blocking(descriptor, False)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("POSIX delegated launcher status timed out")
        readable, _, _ = select.select([descriptor], [], [], remaining)
        if not readable:
            raise TimeoutError("POSIX delegated launcher status timed out")
        chunk = os.read(descriptor, 128 - len(frame))
        if not chunk:
            break
        frame.extend(chunk)
        if len(frame) >= 128:
            raise ValueError("POSIX delegated launcher status frame is oversized")
    match = re.fullmatch(rb"DWW1 ([1-9][0-9]*) ([1-9][0-9]*)\n", bytes(frame))
    if match is None:
        raise ValueError("POSIX delegated launcher status frame is invalid")
    pid = int(match.group(1))
    process_group = int(match.group(2))
    if pid != process_group:
        raise ValueError("POSIX delegated launcher is not its process-group leader")
    # 调用方预建唯一 identity 对象；即使赋值后的返回边界被中断仍可收束该组。
    identity.value = (pid, process_group)


def _posix_direct_child_exited_unreaped(pid: int) -> bool:
    try:
        status = os.waitid(
            os.P_PID,
            pid,
            os.WEXITED | os.WNOHANG | os.WNOWAIT,
        )
    except ChildProcessError as exc:
        raise DelegatedProcessTerminationError(
            "Lost POSIX launcher direct child was reaped outside its owner"
        ) from exc
    return status is not None and status.si_pid == pid


def _wait_and_reap_lost_posix_launcher(pid: int, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            waited_pid, _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError as exc:
            raise DelegatedProcessTerminationError(
                "Lost POSIX launcher direct child was reaped outside its owner"
            ) from exc
        if waited_pid == pid:
            return
        if time.monotonic() >= deadline:
            raise DelegatedProcessTerminationError(
                "Lost POSIX launcher direct child could not be reaped"
            )
        time.sleep(ADAPTER_TERMINATION_POLL_SECONDS)


def _wait_for_posix_group_absence(process_group: int, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while _posix_process_group_exists(process_group):
        if time.monotonic() >= deadline:
            raise DelegatedProcessTerminationError(
                "Lost POSIX launcher process group could not be confirmed stopped"
            )
        time.sleep(ADAPTER_TERMINATION_POLL_SECONDS)


def _stop_unreturned_posix_launcher(pid: int, process_group: int) -> None:
    if pid <= 0 or pid != process_group:
        raise DelegatedProcessTerminationError(
            "Lost POSIX launcher identity could not be validated"
        )
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError as exc:
        raise DelegatedProcessTerminationError(
            "Could not request lost POSIX launcher process-group termination"
        ) from exc
    graceful_deadline = time.monotonic() + ADAPTER_TERMINATION_GRACE_SECONDS
    while time.monotonic() < graceful_deadline:
        if _posix_direct_child_exited_unreaped(pid):
            break
        time.sleep(ADAPTER_TERMINATION_POLL_SECONDS)
    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError as exc:
        raise DelegatedProcessTerminationError(
            "Could not force lost POSIX launcher process-group termination"
        ) from exc
    _wait_and_reap_lost_posix_launcher(
        pid, timeout=ADAPTER_TERMINATION_GRACE_SECONDS
    )
    # 信号发送期间始终保留未 reap 的直接子身份；reap 后只查询、不再按裸 PGID 发信号。
    _wait_for_posix_group_absence(
        process_group, timeout=ADAPTER_TERMINATION_GRACE_SECONDS
    )


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


def _close_posix_launch_child_ends(
    status_write: _OwnedPosixFd,
    gate_read: _OwnedPosixFd,
    payload_handle: Any,
) -> BaseException | None:
    failures: list[BaseException] = []
    for close in (status_write.close, gate_read.close, payload_handle.close):
        try:
            close()
        except BaseException as exc:  # noqa: BLE001 - 每个 child-end 都必须尝试关闭
            failures.append(exc)
    if not failures:
        return None
    if len(failures) == 1:
        return failures[0]
    return BaseExceptionGroup("POSIX launcher child-end close failures", failures)


def _close_posix_parent_ends(
    status_read: _OwnedPosixFd, gate_write: _OwnedPosixFd
) -> BaseException | None:
    failures: list[BaseException] = []
    for descriptor in (gate_write, status_read):
        try:
            descriptor.close()
        except BaseException as exc:  # noqa: BLE001 - 防止后一 FD 因前一失败而泄漏
            failures.append(exc)
    if not failures:
        return None
    if len(failures) == 1:
        return failures[0]
    return BaseExceptionGroup("POSIX launcher parent-end close failures", failures)


def _prepare_posix_launch_resources(
    argv: list[str], *, root: Path, environment: dict[str, str] | None
) -> tuple[_OwnedPosixFd, _OwnedPosixFd, _OwnedPosixFd, _OwnedPosixFd, Any]:
    payload = _posix_launch_payload(argv, root=root, environment=environment)
    status_read = _OwnedPosixFd(None)
    status_write = _OwnedPosixFd(None)
    gate_read = _OwnedPosixFd(None)
    gate_write = _OwnedPosixFd(None)
    payload_handle: Any | None = None
    try:
        status_read, status_write = _create_cloexec_pipe()
        gate_read, gate_write = _create_cloexec_pipe()
        payload_handle = tempfile.TemporaryFile()
        payload_handle.write(payload)
        payload_handle.flush()
        payload_handle.seek(0)
        return status_read, status_write, gate_read, gate_write, payload_handle
    except BaseException as original_error:
        failures: list[BaseException] = []
        for descriptor in (status_read, status_write, gate_read, gate_write):
            try:
                descriptor.close()
            except BaseException as exc:  # noqa: BLE001 - 未启动时也不泄漏 FD
                failures.append(exc)
        if payload_handle is not None:
            try:
                payload_handle.close()
            except BaseException as exc:  # noqa: BLE001 - 与准备错误保留双证据
                failures.append(exc)
        if failures:
            raise _combined_failures(
                "POSIX launcher preparation and cleanup both failed",
                original_error,
                failures[0]
                if len(failures) == 1
                else BaseExceptionGroup(
                    "POSIX launcher preparation cleanup failures", failures
                ),
            ) from None
        raise


def _launch_posix_adapter_process(
    argv: list[str],
    *,
    root: Path,
    stdin_handle: Any,
    stdout_handle: Any,
    stderr_handle: Any,
    environment: dict[str, str] | None,
) -> subprocess.Popen[bytes]:
    (
        status_read,
        status_write,
        gate_read,
        gate_write,
        payload_handle,
    ) = _prepare_posix_launch_resources(
        argv, root=root, environment=environment
    )
    process: subprocess.Popen[bytes] | None = None
    launcher_identity = _PosixLauncherIdentity()
    launch_error: BaseException | None = None
    child_close_error: BaseException | None = None
    try:
        try:
            assert status_write.value is not None
            assert gate_read.value is not None
            process = subprocess.Popen(
                [
                    str(Path(sys.executable).resolve()),
                    "-I",
                    "-S",
                    "-c",
                    _POSIX_GATE_LAUNCHER,
                    str(status_write.value),
                    str(gate_read.value),
                    str(payload_handle.fileno()),
                ],
                cwd=Path(sys.executable).resolve().parent,
                stdin=stdin_handle,
                stdout=stdout_handle,
                stderr=stderr_handle,
                env=_posix_launcher_environment(),
                start_new_session=True,
                close_fds=True,
                pass_fds=(
                    status_write.value,
                    gate_read.value,
                    payload_handle.fileno(),
                ),
            )
        except BaseException as exc:  # noqa: BLE001 - 可能已 fork/exec 但尚未返回对象
            launch_error = exc
        finally:
            child_close_error = _close_posix_launch_child_ends(
                status_write, gate_read, payload_handle
            )

        if process is None:
            failures: list[BaseException] = []
            try:
                gate_write.close()
            except BaseException as exc:  # noqa: BLE001 - EOF 是禁止 launcher exec 的门禁
                failures.append(exc)
            identity: tuple[int, int] | None = None
            try:
                _read_posix_launcher_status(
                    status_read,
                    launcher_identity,
                    timeout=ADAPTER_TERMINATION_GRACE_SECONDS,
                )
                identity = launcher_identity.require()
            except BaseException as exc:  # noqa: BLE001 - 无身份不得猜 PID/PGID
                try:
                    identity = launcher_identity.require()
                except (TypeError, ValueError):
                    failures.append(
                        DelegatedProcessTerminationError(
                            "Unreturned POSIX launcher identity could not be proven"
                        )
                    )
                    failures[-1].__cause__ = exc
            try:
                status_read.close()
            except BaseException as exc:  # noqa: BLE001 - 单次消费后继续终止
                failures.append(exc)
            if identity is not None:
                try:
                    _stop_unreturned_posix_launcher(*identity)
                except BaseException as exc:  # noqa: BLE001 - 未返回根仍须确认整个组
                    failures.append(exc)
            if child_close_error is not None:
                failures.append(child_close_error)
            assert launch_error is not None
            if failures:
                termination_error = DelegatedProcessTerminationError(
                    "Unreturned POSIX delegated launcher could not be safely closed"
                )
                termination_error.__cause__ = (
                    failures[0]
                    if len(failures) == 1
                    else BaseExceptionGroup(
                        "Unreturned POSIX launcher cleanup failures", failures
                    )
                )
                raise _combined_failures(
                    "Delegated adapter launch failed after POSIX child creation "
                    "could no longer be excluded",
                    launch_error,
                    termination_error,
                ) from None
            if isinstance(launch_error, OSError):
                raise DelegatedContractError(
                    f"Could not start the delegated adapter: {launch_error}"
                ) from launch_error
            raise launch_error

        try:
            if child_close_error is not None:
                raise child_close_error
            _read_posix_launcher_status(
                status_read,
                launcher_identity,
                timeout=ADAPTER_TERMINATION_GRACE_SECONDS,
            )
            launcher_pid, process_group = launcher_identity.require()
            if launcher_pid != process.pid or process_group != process.pid:
                raise DelegatedContractError(
                    "POSIX delegated launcher status does not match its direct child"
                )
            status_read.close()
            # 写入一开始就按“适配器可能执行”处理；任意中断都进入整组收束。
            if gate_write.value is None:
                raise DelegatedContractError(
                    "POSIX delegated launcher gate was already closed"
                )
            if os.write(gate_write.value, b"G") != 1:
                raise DelegatedContractError(
                    "POSIX delegated launcher gate write was incomplete"
                )
            gate_write.close()
            return process
        except BaseException as original_error:
            parent_close_error = _close_posix_parent_ends(
                status_read, gate_write
            )
            try:
                _stop_posix_adapter_process_group(process)
            except BaseException as termination_error:  # noqa: BLE001 - 协议失败仍须收束
                failures = [termination_error]
                if parent_close_error is not None:
                    failures.append(parent_close_error)
                wrapped = DelegatedProcessTerminationError(
                    "POSIX delegated launcher protocol failed and its process "
                    "group could not be confirmed stopped"
                )
                wrapped.__cause__ = (
                    failures[0]
                    if len(failures) == 1
                    else BaseExceptionGroup(
                        "POSIX launcher protocol cleanup failures", failures
                    )
                )
                raise _combined_failures(
                    "POSIX delegated launcher protocol and termination both failed",
                    original_error,
                    wrapped,
                ) from None
            if parent_close_error is not None:
                raise _combined_failures(
                    "POSIX delegated launcher protocol and descriptor cleanup failed",
                    original_error,
                    parent_close_error,
                ) from None
            raise
    finally:
        # 正常路径已消费；异常路径只会尝试尚未消费的 FD。
        _close_posix_parent_ends(status_read, gate_write)


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


class _WindowsStartupInformation(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("lpReserved", ctypes.c_wchar_p),
        ("lpDesktop", ctypes.c_wchar_p),
        ("lpTitle", ctypes.c_wchar_p),
        ("dwX", ctypes.c_uint32),
        ("dwY", ctypes.c_uint32),
        ("dwXSize", ctypes.c_uint32),
        ("dwYSize", ctypes.c_uint32),
        ("dwXCountChars", ctypes.c_uint32),
        ("dwYCountChars", ctypes.c_uint32),
        ("dwFillAttribute", ctypes.c_uint32),
        ("dwFlags", ctypes.c_uint32),
        ("wShowWindow", ctypes.c_uint16),
        ("cbReserved2", ctypes.c_uint16),
        ("lpReserved2", ctypes.POINTER(ctypes.c_ubyte)),
        ("hStdInput", ctypes.c_void_p),
        ("hStdOutput", ctypes.c_void_p),
        ("hStdError", ctypes.c_void_p),
    ]


class _WindowsStartupInformationEx(ctypes.Structure):
    _fields_ = [
        ("StartupInfo", _WindowsStartupInformation),
        ("lpAttributeList", ctypes.c_void_p),
    ]


class _WindowsProcessInformation(ctypes.Structure):
    _fields_ = [
        ("hProcess", ctypes.c_void_p),
        ("hThread", ctypes.c_void_p),
        ("dwProcessId", ctypes.c_uint32),
        ("dwThreadId", ctypes.c_uint32),
    ]


@dataclass
class _WindowsAdapterJob:
    """CreateProcessW 前建立的 Windows 所有权边界；不依赖可复用 PID。"""

    handle: int | None
    empty_confirmed: bool = False
    close_attempted: bool = False
    close_outcome_uncertain: bool = False
    closed_confirmed: bool = False


class _WindowsAdapterProcess:
    """PROCESS_INFORMATION 是唯一句柄所有者，API 直接写入预建字段。"""

    def __init__(self) -> None:
        self.information = _WindowsProcessInformation()
        self.returncode: int | None = None
        self.job_ownership_confirmed = False

    @property
    def pid(self) -> int:
        return int(self.information.dwProcessId)

    def process_handle(self) -> int:
        handle = self.information.hProcess
        if not handle:
            raise OSError("Windows delegated adapter process handle is unavailable")
        return int(handle)

    def thread_handle(self) -> int:
        handle = self.information.hThread
        if not handle:
            raise OSError("Windows delegated adapter thread handle is unavailable")
        return int(handle)

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        exit_code = ctypes.c_uint32()
        kernel32 = _windows_kernel32()
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        get_exit_code.restype = ctypes.c_int
        if not get_exit_code(
            ctypes.c_void_p(self.process_handle()), ctypes.byref(exit_code)
        ):
            raise OSError(ctypes.get_last_error(), "GetExitCodeProcess failed")
        if exit_code.value == 259:
            return None
        self.returncode = int(exit_code.value)
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is not None:
            return self.returncode
        milliseconds = 0xFFFFFFFF
        if timeout is not None:
            milliseconds = min(0xFFFFFFFE, max(0, int(timeout * 1000 + 0.999)))
        kernel32 = _windows_kernel32()
        wait = kernel32.WaitForSingleObject
        wait.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        wait.restype = ctypes.c_uint32
        result = int(
            wait(ctypes.c_void_p(self.process_handle()), milliseconds)
        )
        if result == 0x00000102:
            raise subprocess.TimeoutExpired([], timeout)
        if result == 0xFFFFFFFF:
            raise OSError(ctypes.get_last_error(), "WaitForSingleObject failed")
        if result != 0:
            raise OSError(f"Unexpected WaitForSingleObject result 0x{result:08x}")
        returncode = self.poll()
        if returncode is None:
            raise OSError("Windows process was signaled but remained active")
        return returncode


@dataclass
class _WindowsLaunchResources:
    attribute_buffer: Any | None = None
    attribute_list_initialized: bool = False
    standard_handles: Any = None
    job_handles: Any = None

    def __post_init__(self) -> None:
        if self.standard_handles is None:
            self.standard_handles = (ctypes.c_void_p * 3)()
        if self.job_handles is None:
            self.job_handles = (ctypes.c_void_p * 1)()



def _windows_kernel32() -> Any:
    return ctypes.WinDLL("kernel32", use_last_error=True)


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
        if job.close_outcome_uncertain:
            raise DelegatedProcessTerminationError(
                "Windows adapter Job handle close outcome is indeterminate"
            )
        return
    handle = job.handle
    # 数值句柄在原生调用前即从对象移除；即使 CloseHandle 返回边界被中断也绝不重试。
    job.handle = None
    job.close_attempted = True
    kernel32 = _windows_kernel32()
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    try:
        closed = close_handle(ctypes.c_void_p(handle))
    except BaseException as exc:  # noqa: BLE001 - 关闭结果可能已生效，禁止复用数值
        job.close_outcome_uncertain = True
        raise DelegatedProcessTerminationError(
            "Windows adapter Job handle close outcome is indeterminate"
        ) from exc
    if not closed:
        job.close_outcome_uncertain = True
        raise DelegatedProcessTerminationError(
            "Windows adapter Job handle could not be confirmed closed"
        ) from OSError(ctypes.get_last_error(), "CloseHandle failed for adapter job")
    job.closed_confirmed = True


def _confirm_windows_adapter_job_ownership(
    job: _WindowsAdapterJob, process: _WindowsAdapterProcess
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
        ctypes.c_void_p(process.process_handle()),
        ctypes.c_void_p(job.handle),
        ctypes.byref(belongs),
    ):
        raise OSError(ctypes.get_last_error(), "IsProcessInJob failed")
    if not belongs.value:
        raise OSError("Suspended delegated adapter root is not owned by its Job Object")


def _consume_windows_process_handle(
    process: _WindowsAdapterProcess, field: str, *, label: str
) -> None:
    raw_handle = getattr(process.information, field)
    if not raw_handle:
        return
    setattr(process.information, field, None)
    kernel32 = _windows_kernel32()
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    try:
        closed = close_handle(ctypes.c_void_p(int(raw_handle)))
    except BaseException as exc:  # noqa: BLE001 - 已从唯一所有者移除，禁止重试
        raise DelegatedProcessTerminationError(
            f"Windows adapter {label} handle close outcome is indeterminate"
        ) from exc
    if not closed:
        raise DelegatedProcessTerminationError(
            f"Windows adapter {label} handle could not be confirmed closed"
        ) from OSError(ctypes.get_last_error(), f"CloseHandle failed for {label}")


def _close_windows_adapter_process_handles(
    process: _WindowsAdapterProcess,
) -> None:
    failures: list[BaseException] = []
    for field, label in (("hThread", "thread"), ("hProcess", "process")):
        try:
            _consume_windows_process_handle(process, field, label=label)
        except BaseException as exc:  # noqa: BLE001 - 两个句柄都只消费一次
            failures.append(exc)
    if failures:
        error = DelegatedProcessTerminationError(
            "Windows adapter native handles could not be confirmed closed"
        )
        error.__cause__ = (
            failures[0]
            if len(failures) == 1
            else BaseExceptionGroup("Windows adapter handle close failures", failures)
        )
        raise error


def _resume_windows_adapter_process(process: _WindowsAdapterProcess) -> None:
    """只恢复原子入 Job 的 CREATE_SUSPENDED 主线程。"""
    kernel32 = _windows_kernel32()
    resume = kernel32.ResumeThread
    resume.argtypes = [ctypes.c_void_p]
    resume.restype = ctypes.c_uint32
    previous_count = int(resume(ctypes.c_void_p(process.thread_handle())))
    if previous_count == 0xFFFFFFFF:
        raise OSError(ctypes.get_last_error(), "ResumeThread failed")
    if previous_count != 1:
        raise OSError(
            "Suspended delegated adapter thread had an unexpected suspend count "
            f"of {previous_count}"
        )


def _initialize_windows_launch_resources(
    resources: _WindowsLaunchResources,
    job: _WindowsAdapterJob,
    standard_streams: tuple[Any, Any, Any],
) -> _WindowsStartupInformationEx:
    if job.handle is None:
        raise OSError("Windows adapter Job Object was already closed")
    import msvcrt

    kernel32 = _windows_kernel32()
    initialize = kernel32.InitializeProcThreadAttributeList
    initialize.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    initialize.restype = ctypes.c_int
    attribute_bytes = ctypes.c_size_t()
    ctypes.set_last_error(0)
    if initialize(None, 2, 0, ctypes.byref(attribute_bytes)):
        raise OSError("InitializeProcThreadAttributeList size probe unexpectedly succeeded")
    if ctypes.get_last_error() != 122 or attribute_bytes.value <= 0:
        raise OSError(
            ctypes.get_last_error(),
            "InitializeProcThreadAttributeList size probe failed",
        )
    resources.attribute_buffer = ctypes.create_string_buffer(attribute_bytes.value)
    if not initialize(
        resources.attribute_buffer,
        2,
        0,
        ctypes.byref(attribute_bytes),
    ):
        raise OSError(
            ctypes.get_last_error(), "InitializeProcThreadAttributeList failed"
        )
    resources.attribute_list_initialized = True

    current_process = kernel32.GetCurrentProcess
    current_process.argtypes = []
    current_process.restype = ctypes.c_void_p
    duplicate_handle = kernel32.DuplicateHandle
    duplicate_handle.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint32,
    ]
    duplicate_handle.restype = ctypes.c_int
    owner = current_process()
    for index, stream in enumerate(standard_streams):
        source = msvcrt.get_osfhandle(stream.fileno())
        target = ctypes.cast(
            ctypes.byref(
                resources.standard_handles,
                index * ctypes.sizeof(ctypes.c_void_p),
            ),
            ctypes.POINTER(ctypes.c_void_p),
        )
        if not duplicate_handle(
            owner,
            ctypes.c_void_p(source),
            owner,
            target,
            0,
            True,
            0x00000002,
        ):
            raise OSError(ctypes.get_last_error(), "DuplicateHandle failed")

    resources.job_handles[0] = job.handle
    update = kernel32.UpdateProcThreadAttribute
    update.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    update.restype = ctypes.c_int
    attribute_pointer = ctypes.c_void_p(
        ctypes.addressof(resources.attribute_buffer)
    )
    if not update(
        attribute_pointer,
        0,
        0x00020002,
        resources.standard_handles,
        ctypes.sizeof(resources.standard_handles),
        None,
        None,
    ):
        raise OSError(
            ctypes.get_last_error(), "PROC_THREAD_ATTRIBUTE_HANDLE_LIST failed"
        )
    if not update(
        attribute_pointer,
        0,
        0x0002000D,
        resources.job_handles,
        ctypes.sizeof(resources.job_handles),
        None,
        None,
    ):
        raise OSError(
            ctypes.get_last_error(), "PROC_THREAD_ATTRIBUTE_JOB_LIST failed"
        )

    startup = _WindowsStartupInformationEx()
    startup.StartupInfo.cb = ctypes.sizeof(startup)
    startup.StartupInfo.dwFlags = 0x00000100
    startup.StartupInfo.hStdInput = resources.standard_handles[0]
    startup.StartupInfo.hStdOutput = resources.standard_handles[1]
    startup.StartupInfo.hStdError = resources.standard_handles[2]
    startup.lpAttributeList = attribute_pointer
    return startup


def _close_windows_launch_resources(resources: _WindowsLaunchResources) -> None:
    failures: list[BaseException] = []
    if resources.attribute_list_initialized:
        resources.attribute_list_initialized = False
        buffer = resources.attribute_buffer
        resources.attribute_buffer = None
        try:
            kernel32 = _windows_kernel32()
            delete = kernel32.DeleteProcThreadAttributeList
            delete.argtypes = [ctypes.c_void_p]
            delete.restype = None
            assert buffer is not None
            delete(ctypes.c_void_p(ctypes.addressof(buffer)))
        except BaseException as exc:  # noqa: BLE001 - 属性列表同样只消费一次
            failures.append(exc)
    for index in range(3):
        raw_handle = resources.standard_handles[index]
        if not raw_handle:
            continue
        resources.standard_handles[index] = None
        try:
            kernel32 = _windows_kernel32()
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = [ctypes.c_void_p]
            close_handle.restype = ctypes.c_int
            if not close_handle(ctypes.c_void_p(int(raw_handle))):
                raise OSError(
                    ctypes.get_last_error(),
                    "CloseHandle failed for inherited standard handle",
                )
        except BaseException as exc:  # noqa: BLE001 - 后续句柄仍须消费
            failures.append(exc)
    if failures:
        error = DelegatedProcessTerminationError(
            "Windows adapter launch resources could not be confirmed closed"
        )
        error.__cause__ = (
            failures[0]
            if len(failures) == 1
            else BaseExceptionGroup("Windows launch resource close failures", failures)
        )
        raise error


def _windows_environment_block(
    environment: dict[str, str] | None,
) -> Any | None:
    if environment is None:
        return None
    entries: list[str] = []
    for key, value in sorted(environment.items(), key=lambda item: item[0].upper()):
        invalid_equals = "=" in (key[1:] if key.startswith("=") else key)
        if not key or invalid_equals or "\0" in key or "\0" in value:
            raise ValueError("Windows delegated adapter environment is invalid")
        entries.append(f"{key}={value}")
    return ctypes.create_unicode_buffer("\0".join(entries) + "\0\0")


def _call_windows_create_process(
    process: _WindowsAdapterProcess,
    startup: _WindowsStartupInformationEx,
    command_line: Any,
    environment_block: Any | None,
    root: Path,
    creation_flags: int,
) -> None:
    kernel32 = _windows_kernel32()
    create_process = kernel32.CreateProcessW
    create_process.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_void_p,
        ctypes.POINTER(_WindowsProcessInformation),
    ]
    create_process.restype = ctypes.c_int
    environment_pointer = (
        None
        if environment_block is None
        else ctypes.c_void_p(ctypes.addressof(environment_block))
    )
    if not create_process(
        None,
        command_line,
        None,
        None,
        True,
        creation_flags,
        environment_pointer,
        str(root),
        ctypes.byref(startup),
        ctypes.byref(process.information),
    ):
        raise OSError(ctypes.get_last_error(), "CreateProcessW failed")


def _launch_windows_adapter_process(
    process: _WindowsAdapterProcess,
    job: _WindowsAdapterJob,
    argv: list[str],
    *,
    root: Path,
    stdin_handle: Any,
    stdout_handle: Any,
    stderr_handle: Any,
    environment: dict[str, str] | None,
) -> None:
    resources = _WindowsLaunchResources()
    launch_error: BaseException | None = None
    try:
        startup = _initialize_windows_launch_resources(
            resources,
            job,
            (stdin_handle, stdout_handle, stderr_handle),
        )
        command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
        environment_block = _windows_environment_block(environment)
        creation_flags = 0x00000004 | 0x00000200 | 0x00080000
        if environment_block is not None:
            creation_flags |= 0x00000400
        _call_windows_create_process(
            process,
            startup,
            command_line,
            environment_block,
            root,
            creation_flags,
        )
        if process.pid <= 0 or not process.information.hThread:
            raise OSError("CreateProcessW returned incomplete process information")
        _confirm_windows_adapter_job_ownership(job, process)
        process.job_ownership_confirmed = True
    except BaseException as exc:  # noqa: BLE001 - process fields may already own native handles
        launch_error = exc
    try:
        _close_windows_launch_resources(resources)
    except BaseException as resource_error:  # noqa: BLE001 - 与创建错误保留双证据
        if launch_error is None:
            raise
        raise _combined_failures(
            "Windows adapter launch and resource cleanup both failed",
            launch_error,
            resource_error,
        ) from None
    if launch_error is not None:
        raise launch_error


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
    _close_windows_adapter_job(job)


def _ensure_adapter_process_boundary_empty(
    process: subprocess.Popen[bytes] | _WindowsAdapterProcess,
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


def _ensure_windows_adapter_root_stopped(
    process: _WindowsAdapterProcess,
) -> None:
    if not process.information.hProcess:
        # PROCESS_INFORMATION 由内核直接写入预建对象；空值证明未创建根。
        return
    if process.poll() is None:
        kernel32 = _windows_kernel32()
        terminate = kernel32.TerminateProcess
        terminate.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        terminate.restype = ctypes.c_int
        if not terminate(ctypes.c_void_p(process.process_handle()), 1):
            raise OSError(ctypes.get_last_error(), "TerminateProcess failed")
    process.wait(timeout=ADAPTER_TERMINATION_GRACE_SECONDS)


def _stop_adapter_process(
    process: subprocess.Popen[bytes] | _WindowsAdapterProcess,
    *,
    windows_job: _WindowsAdapterJob | None = None,
) -> None:
    try:
        if os.name == "nt":
            if windows_job is None:
                raise DelegatedProcessTerminationError(
                    "Delegated adapter has no Windows Job Object ownership boundary"
                )
            assert isinstance(process, _WindowsAdapterProcess)
            failures: list[BaseException] = []
            try:
                _stop_windows_adapter_job(windows_job)
            except BaseException as exc:  # noqa: BLE001 - 句柄也必须继续单次消费
                failures.append(exc)
            root_stopped = process.job_ownership_confirmed and windows_job.empty_confirmed
            if not root_stopped:
                try:
                    _ensure_windows_adapter_root_stopped(process)
                    root_stopped = True
                except BaseException as exc:  # noqa: BLE001 - 精确 HANDLE 是最后安全边界
                    failures.append(exc)
            if root_stopped:
                try:
                    _close_windows_adapter_process_handles(process)
                except BaseException as exc:  # noqa: BLE001 - Job 失败不跳过原生句柄
                    failures.append(exc)
            else:
                failures.append(
                    DelegatedProcessTerminationError(
                        "Windows adapter root could not be confirmed stopped; "
                        "its native handles were retained"
                    )
                )
            if failures:
                error = DelegatedProcessTerminationError(
                    "Windows delegated adapter ownership could not be fully released"
                )
                error.__cause__ = (
                    failures[0]
                    if len(failures) == 1
                    else BaseExceptionGroup(
                        "Windows adapter ownership release failures", failures
                    )
                )
                raise error
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
        process: subprocess.Popen[bytes] | _WindowsAdapterProcess
        if os.name == "nt":
            assert windows_job is not None
            process = _WindowsAdapterProcess()
            try:
                _launch_windows_adapter_process(
                    process,
                    windows_job,
                    argv,
                    root=root,
                    stdin_handle=stdin_handle,
                    stdout_handle=stdout_handle,
                    stderr_handle=stderr_handle,
                    environment=environment,
                )
            except BaseException as launch_error:
                try:
                    _stop_adapter_process(process, windows_job=windows_job)
                except BaseException as termination_error:
                    raise _combined_failures(
                        "Delegated adapter launch and atomic Job cleanup both failed",
                        launch_error,
                        termination_error,
                    ) from None
                if isinstance(launch_error, OSError):
                    raise DelegatedContractError(
                        f"Could not start the delegated adapter: {launch_error}"
                    ) from launch_error
                raise
        else:
            process = _launch_posix_adapter_process(
                argv,
                root=root,
                stdin_handle=stdin_handle,
                stdout_handle=stdout_handle,
                stderr_handle=stderr_handle,
                environment=environment,
            )
        resume_attempted = False
        resume_completed = False
        try:
            if os.name == "nt":
                assert windows_job is not None
                assert isinstance(process, _WindowsAdapterProcess)
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
                _stop_adapter_process(process, windows_job=windows_job)
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
