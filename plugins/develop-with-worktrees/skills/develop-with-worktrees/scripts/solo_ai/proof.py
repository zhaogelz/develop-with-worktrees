from __future__ import annotations

import copy
import fnmatch
import os
import platform
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import (
    CommandSpec,
    STRESS_VERIFICATION_FILENAME,
    VerificationConfig,
    VerificationProfile,
    load_repo_config,
)
from .repo import GitRepo
from .util import (
    ActionableSoloAIError,
    SoloAIError,
    atomic_write_json,
    new_id,
    read_json,
    redact_text,
    run,
    run_logged,
    sha256_file,
    sha256_text,
    stable_json,
    utc_timestamp,
)
from .validation_queue import (
    claim_validation_slot,
    inherited_claim_environment,
    record_profile_duration,
)

LOCKFILES = (
    "uv.lock",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "Cargo.lock",
    "go.sum",
)
PROOF_SCHEMA = 3
VALIDATION_ATTEMPT_SCHEMA = 1
PROFILE_HISTORY_SCHEMA = 1
# 审批计划和验证收据的演进速度不同：前者描述可执行的策略，后者绑定现场证据。
# 5 将验证命令从本机审批记录中改为“脱敏展示 + 原始参数指纹”。
# 旧计划可能含有原始命令参数，不能继续当作当前审批契约。
APPROVAL_PLAN_SCHEMA = 6
_EXECUTION_BASELINE = (
    "PATH",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    # Windows信息查询不可用时，标准库依靠这两个变量确认原生架构。
    "PROCESSOR_ARCHITECTURE",
    "PROCESSOR_ARCHITEW6432",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "HOME",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
    "LANG",
    "LC_ALL",
    "TERM",
)


class ValidationBaseChanged(SoloAIError):
    """验证取得机器准入后发现基线已经推进。"""

    def __init__(self, *, expected: str, current: str) -> None:
        self.expected = expected
        self.current = current
        super().__init__(
            f"Validation base advanced from {expected} to {current} while waiting"
        )


class ValidationCandidateChanged(SoloAIError):
    """验证期间候选 HEAD 发生变化。"""

    def __init__(self, *, expected: str, current: str) -> None:
        self.expected = expected
        self.current = current
        super().__init__(f"Validation candidate changed from {expected} to {current}")


def new_validation_attempt_id(level: str) -> str:
    """生成一次验证尝试的可追溯标识，不把它当作证明身份。"""

    return new_id(f"{level}-attempt")


def _validation_attempt_path(repo: GitRepo, attempt_id: str) -> Path:
    return repo.local_dir / "validation-attempts" / f"{attempt_id}.json"


def read_validation_attempt(repo: GitRepo, attempt_id: str) -> dict[str, Any]:
    """读取一次精确尝试；状态视图只读取这个小回执，不扫描证明历史。"""

    value = read_json(_validation_attempt_path(repo, attempt_id), {})
    if value and value.get("schema_version") != VALIDATION_ATTEMPT_SCHEMA:
        return {}
    return value


def start_validation_attempt(
    repo: GitRepo,
    *,
    attempt_id: str,
    level: str,
    full_scope: str,
    task_id: str | None,
    owner: dict[str, str] | None = None,
) -> dict[str, Any]:
    """在真正计算输入前留下最小回执，避免失败批次失去成本归属。"""

    existing = read_validation_attempt(repo, attempt_id)
    if existing:
        return existing
    value = {
        "schema_version": VALIDATION_ATTEMPT_SCHEMA,
        "id": attempt_id,
        "owner": dict(owner or ({"kind": "task", "id": task_id} if task_id else {})),
        "task_id": task_id,
        "level": level,
        "full_scope": full_scope,
        "state": "preparing",
        "result": None,
        "profiles": [],
        "started_at": utc_timestamp(),
        "updated_at": utc_timestamp(),
    }
    atomic_write_json(_validation_attempt_path(repo, attempt_id), value)
    return value


def _update_validation_attempt(
    repo: GitRepo, attempt_id: str, **changes: Any
) -> dict[str, Any]:
    value = read_validation_attempt(repo, attempt_id)
    if not value:
        return {}
    value.update(changes)
    value["updated_at"] = utc_timestamp()
    atomic_write_json(_validation_attempt_path(repo, attempt_id), value)
    return value


def _update_validation_attempt_profile(
    repo: GitRepo, attempt_id: str, profile_id: str, **changes: Any
) -> None:
    value = read_validation_attempt(repo, attempt_id)
    if not value:
        return
    profiles = value.get("profiles") or []
    for profile in profiles:
        if profile.get("id") == profile_id:
            profile.update(changes)
            value["updated_at"] = utc_timestamp()
            atomic_write_json(_validation_attempt_path(repo, attempt_id), value)
            return


def finish_validation_attempt(
    repo: GitRepo,
    *,
    attempt_id: str,
    result: str,
    error: str | None = None,
    proof: str | None = None,
) -> None:
    """把通过、失败、超时或中断明确写成终态，而不是猜测缺失证据。"""

    changes: dict[str, Any] = {
        "state": "completed",
        "result": result,
        "finished_at": utc_timestamp(),
    }
    if error:
        changes["error"] = redact_text(error)[:1000]
    if proof:
        changes["proof"] = proof
    _update_validation_attempt(repo, attempt_id, **changes)


def _require_expected_base_head(
    repo: GitRepo, *, cwd: Path, base: str, expected_base_head: str | None
) -> None:
    if expected_base_head is None:
        return
    current = repo.git(["rev-parse", "--verify", base], cwd=cwd).stdout.strip()
    if current != expected_base_head:
        raise ValidationBaseChanged(expected=expected_base_head, current=current)


def _require_expected_candidate_head(
    repo: GitRepo, *, cwd: Path, expected_candidate_head: str | None
) -> None:
    if expected_candidate_head is None:
        return
    current = repo.head(cwd)
    if current != expected_candidate_head:
        raise ValidationCandidateChanged(
            expected=expected_candidate_head, current=current
        )


def changed_files(repo: GitRepo, *, cwd: Path, base: str) -> list[str]:
    output = repo.git(
        ["diff", "--name-only", "--no-renames", f"{base}...HEAD"], cwd=cwd
    ).stdout
    return sorted(item for item in output.splitlines() if item)


def select_profiles(
    config: VerificationConfig,
    files: list[str],
    *,
    levels: tuple[str, ...] = ("ready",),
    full_scopes: tuple[str, ...] | None = None,
) -> list[VerificationProfile]:
    selected: list[VerificationProfile] = []
    for profile in config.profiles:
        if profile.level not in levels:
            continue
        if (
            profile.level == "full"
            and full_scopes is not None
            and profile.full_scope not in full_scopes
        ):
            continue
        if not files or any(
            any(fnmatch.fnmatchcase(path, pattern) for pattern in profile.paths)
            for path in files
        ):
            selected.append(profile)
    return selected


def selected_profile_ids(
    repo: GitRepo,
    *,
    cwd: Path,
    base: str,
    verification: VerificationConfig,
    levels: tuple[str, ...] = ("ready",),
    full_scopes: tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    """返回本次验证实际会运行的检查 ID，供批准和执行共用。"""
    files = changed_files(repo, cwd=cwd, base=base)
    return tuple(
        profile.profile_id
        for profile in select_profiles(
            verification, files, levels=levels, full_scopes=full_scopes
        )
    )


def unmapped_files(
    config: VerificationConfig,
    files: list[str],
    *,
    levels: tuple[str, ...] = ("ready",),
    full_scopes: tuple[str, ...] | None = None,
) -> list[str]:
    """只要候选改动没有 Ready 映射，就拒绝猜测该运行什么验证。"""
    available = [
        profile
        for profile in config.profiles
        if profile.level in levels
        and not (
            profile.level == "full"
            and full_scopes is not None
            and profile.full_scope not in full_scopes
        )
    ]
    return [
        path
        for path in files
        if not any(
            any(fnmatch.fnmatchcase(path, pattern) for pattern in profile.paths)
            for profile in available
        )
    ]


def _tool(command: CommandSpec, cwd: Path) -> dict[str, str | None]:
    executable = command.argv[0]
    resolved = shutil.which(executable) or (
        executable if Path(executable).is_file() else None
    )
    version = None
    if resolved:
        result = run([resolved, "--version"], cwd=cwd, check=False, timeout=10)
        output = result.stdout or result.stderr
        version = redact_text(output.splitlines()[0][:300]) if output else None
    fact = {
        "argv_digest": command.fingerprint,
        "executable": redact_text(executable),
        "path": str(Path(resolved).resolve()) if resolved else None,
        "version": version,
    }
    # uv/uvx 是运行器而非真正执行检查的工具；记录可安全识别的直接子命令，
    # 让 pytest、ruff 等实际版本参与证明身份。复杂包装命令仍保守地只复用
    # 已完整声明输入的 profile。
    invoked: list[str] | None = None
    if (
        resolved
        and executable == "uv"
        and len(command.argv) >= 3
        and command.argv[1] == "run"
        and not command.argv[2].startswith("-")
    ):
        invoked = [resolved, "run", command.argv[2], "--version"]
    elif (
        resolved
        and executable == "uvx"
        and len(command.argv) >= 2
        and not command.argv[1].startswith("-")
    ):
        invoked = [resolved, command.argv[1], "--version"]
    if invoked:
        result = run(invoked, cwd=cwd, check=False, timeout=10)
        output = result.stdout or result.stderr
        fact["invoked_tool"] = redact_text(
            command.argv[2] if executable == "uv" else command.argv[1]
        )
        fact["invoked_version"] = (
            redact_text(output.splitlines()[0][:300]) if output else None
        )
    return fact


def _tracked(repo: GitRepo, cwd: Path) -> list[str]:
    return sorted(
        item
        for item in repo.git(["ls-files", "-z"], cwd=cwd).stdout.split("\0")
        if item
    )


def _matching_hashes(
    cwd: Path, tracked: list[str], patterns: tuple[str, ...]
) -> dict[str, str]:
    return {
        relative: sha256_file(cwd / relative)
        for relative in tracked
        if (cwd / relative).is_file()
        and any(fnmatch.fnmatchcase(relative, pattern) for pattern in patterns)
    }


def _shared_inputs(
    repo: GitRepo,
    cwd: Path,
    commands: list[CommandSpec],
    verification: VerificationConfig,
    tool_cache: dict[tuple[Any, ...], dict[str, str | None]] | None = None,
    *,
    include_policy: bool = True,
) -> dict[str, Any]:
    tracked = _tracked(repo, cwd)
    tool_specs = [CommandSpec(("git",)), CommandSpec(("uv",)), *commands]
    unique: dict[str, CommandSpec] = {}
    for command in tool_specs:
        unique.setdefault(command.argv[0], command)

    def tool_facts(command: CommandSpec) -> dict[str, str | None]:
        if tool_cache is None:
            return _tool(command, cwd)
        resolved = shutil.which(command.argv[0]) or command.argv[0]
        try:
            path = Path(resolved).resolve()
            stat = path.stat()
            identity = (
                str(path),
                stat.st_dev,
                stat.st_ino,
                stat.st_size,
                stat.st_mtime_ns,
                stat.st_ctime_ns,
            )
        except OSError:
            # 无法确认文件身份时，不缓存版本探测。
            return _tool(command, cwd)
        key = (command.fingerprint, identity)
        if key not in tool_cache:
            tool_cache[key] = _tool(command, cwd)
        return tool_cache[key]

    result: dict[str, Any] = {
        "tools": [tool_facts(command) for command in unique.values()],
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
    }
    if not include_policy:
        return result

    def normalized_file_hash(path: Path) -> str:
        # 策略的意义不能随平台检出时的 LF/CRLF 转换改变；其余文本变化仍是
        # 审批与证明复用的有效输入。
        return sha256_text(path.read_text(encoding="utf-8").replace("\r\n", "\n"))

    config_hashes = {
        ".solo-ai/config.toml": normalized_file_hash(cwd / ".solo-ai" / "config.toml"),
        ".solo-ai/verification.toml": normalized_file_hash(
            cwd / ".solo-ai" / "verification.toml"
        ),
        "verification_normalized": sha256_text(stable_json(verification.normalized())),
    }
    stress_config = cwd / ".solo-ai" / STRESS_VERIFICATION_FILENAME
    if stress_config.exists():
        config_hashes[f".solo-ai/{STRESS_VERIFICATION_FILENAME}"] = (
            normalized_file_hash(stress_config)
        )
    return {
        **result,
        "config_hashes": config_hashes,
        "lockfiles": {
            relative: sha256_file(cwd / relative)
            for relative in tracked
            if Path(relative).name in LOCKFILES and (cwd / relative).is_file()
        },
    }


def _command_policy(command: CommandSpec | None) -> dict[str, Any] | None:
    """Return a redacted command together with its identity for approval comparison."""
    if command is None:
        return None
    return {"argv": command.redacted(), "fingerprint": command.fingerprint}


def _repo_config_policy(repo_config: Any) -> dict[str, Any]:
    """Normalize every repository setting that can change lifecycle execution."""
    readiness = repo_config.readiness
    integration = repo_config.integration
    return {
        "schema_version": repo_config.schema_version,
        "mode": repo_config.mode,
        "slots": repo_config.slots,
        "branch_prefix": repo_config.branch_prefix,
        "worktree_directory": repo_config.worktree_directory,
        "port_base": repo_config.port_base,
        "remote_policy": repo_config.remote_policy,
        "sensitive_allowlist": list(repo_config.sensitive_allowlist),
        "agents_file_created": repo_config.agents_file_created,
        "secret_scanner": _command_policy(repo_config.secret_scanner),
        "warm_commands": [
            _command_policy(command) for command in repo_config.warm_commands
        ],
        "dev_start": _command_policy(repo_config.dev_start),
        "readiness": (
            {
                "kind": readiness.kind,
                "target": readiness.target,
                "timeout_seconds": readiness.timeout_seconds,
            }
            if readiness is not None
            else None
        ),
        "cleanup_owned_paths": list(repo_config.cleanup_owned_paths),
        "integration": {
            "mode": integration.mode,
            "batch_size": integration.batch_size,
            "candidate_capacity": integration.candidate_capacity,
            "seal_policy": integration.seal_policy,
            "candidate_validation": integration.candidate_validation,
            "tail_policy": integration.tail_policy,
            "tail_quiet_seconds": integration.tail_quiet_seconds,
            "worktree_mode": integration.worktree_mode,
        },
    }


def _verification_policy(verification: VerificationConfig) -> dict[str, Any]:
    """Serialize executable verification policy without persisting raw argv values."""
    return {
        "schema_version": verification.schema_version,
        "static_only": verification.static_only,
        "profiles": [
            {
                "id": profile.profile_id,
                "paths": list(profile.paths),
                "commands": [_command_policy(command) for command in profile.commands],
                "cross_task_reuse": profile.cross_task_reuse,
                "external_state": profile.external_state,
                "input_paths": list(profile.input_paths),
                "environment": list(profile.environment),
                "input_closure": profile.input_closure,
                "timeout_seconds": profile.timeout_seconds,
                "resource_class": profile.resource_class,
                "level": profile.level,
                "frozen_base": profile.frozen_base,
                "full_scope": profile.full_scope,
            }
            for profile in verification.profiles
        ],
    }


def _redact_legacy_approval_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Remove raw argv values left by approval-plan schemas before version 5.

    The raw values are only used to display a drift report.  Their digest preserves
    the fact that a command changed, while the locally persisted report remains safe
    to inspect or share.
    """
    sanitized = copy.deepcopy(plan)
    policy = sanitized.get("policy", {})
    configuration = policy.get("configuration", {}) if isinstance(policy, dict) else {}
    verification = (
        configuration.get("verification", {}) if isinstance(configuration, dict) else {}
    )
    profiles = (
        verification.get("profiles", []) if isinstance(verification, dict) else []
    )
    if isinstance(policy, dict) and isinstance(policy.get("profiles"), list):
        profiles = [*profiles, *policy["profiles"]]
    if not isinstance(profiles, list):
        return sanitized
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        commands = profile.get("commands")
        if not isinstance(commands, list):
            continue
        redacted: list[Any] = []
        for command in commands:
            if isinstance(command, list) and all(
                isinstance(value, str) for value in command
            ):
                redacted.append(
                    {
                        "argv": [redact_text(value) for value in command],
                        "fingerprint": sha256_text(stable_json(command)),
                    }
                )
            else:
                redacted.append(command)
        profile["commands"] = redacted
    return sanitized


def _approval_policy_inputs(
    shared: dict[str, Any], *, repo_config: Any, verification: VerificationConfig
) -> dict[str, Any]:
    """Keep semantic execution approval separate from byte-exact proof evidence."""
    return {
        **{key: value for key, value in shared.items() if key != "config_hashes"},
        "configuration": {
            "repository": _repo_config_policy(repo_config),
            "verification": _verification_policy(verification),
        },
    }


def _profile_inputs(
    profile: VerificationProfile,
    *,
    cwd: Path,
    tracked: list[str],
    shared: dict[str, Any],
    validation_environment: dict[str, str] | None = None,
) -> dict[str, Any]:
    inputs = {
        **shared,
        # 旧证明未执行单元输入前后复核，不能作为新复用契约的成功证明。
        "reuse_contract": 2,
        "profile_id": profile.profile_id,
        "command_digests": [command.fingerprint for command in profile.commands],
        "external_state": profile.external_state,
        "input_closure": profile.input_closure,
        "timeout_seconds": profile.timeout_seconds,
        "level": profile.level,
        "tracked_inputs": _matching_hashes(cwd, tracked, profile.input_paths),
        "environment": {
            name: sha256_text(os.environ[name]) if name in os.environ else "absent"
            for name in profile.environment
        },
        # DWW 注入的冻结基线会影响项目选择器；它与声明环境一样属于证明身份。
        "dww_validation_environment": dict(validation_environment or {}),
    }
    # 对完整、无外部状态的检查，选择规则、跨任务提示和资源队列不会改变已执行
    # 命令或其输入。省略它们允许同一真实执行事实跨任务复用；其余检查仍保留
    # 所有调度边界，且历史证明因指纹自然不同而不会被误用。
    if not (
        profile.external_state == "none" and profile.input_closure == "complete"
    ):
        inputs.update(
            {
                "paths": list(profile.paths),
                "cross_task_reuse": profile.cross_task_reuse,
                "resource_class": profile.resource_class,
            }
        )
    return inputs


def _execution_environment(profile: VerificationProfile) -> dict[str, str]:
    """命令仅继承运行所需的受控基线和策略显式列出的变量。"""
    names = (*_EXECUTION_BASELINE, *profile.environment)
    return {name: os.environ[name] for name in names if name in os.environ}


def frozen_validation_environment(
    repo: GitRepo,
    *,
    cwd: Path,
    base: str,
    validation_base_ref: str | None = None,
    expected_base_head: str | None = None,
    full_scope: str | None = None,
) -> dict[str, str]:
    """Return the non-secret frozen-base facts visible to Ready/Full commands."""
    _require_expected_base_head(
        repo, cwd=cwd, base=base, expected_base_head=expected_base_head
    )
    environment = {
        "DWW_VALIDATION_BASE_REF": validation_base_ref or base,
        "DWW_VALIDATION_BASE_HEAD": expected_base_head
        or repo.git(["rev-parse", "--verify", base], cwd=cwd).stdout.strip(),
    }
    if full_scope is not None:
        environment["DWW_VALIDATION_SCOPE"] = full_scope
    return environment


def _profile_validation_environment(
    profile: VerificationProfile, validation_environment: dict[str, str]
) -> dict[str, str]:
    """Only profiles that select by base may observe the frozen base environment."""
    if not profile.frozen_base:
        return {}
    environment = dict(validation_environment)
    if profile.level != "full":
        environment.pop("DWW_VALIDATION_SCOPE", None)
    return environment


def _profile_policy(profile: VerificationProfile) -> dict[str, Any]:
    return {
        "id": profile.profile_id,
        "paths": list(profile.paths),
        "commands": [_command_policy(command) for command in profile.commands],
        "cross_task_reuse": profile.cross_task_reuse,
        "external_state": profile.external_state,
        "input_paths": list(profile.input_paths),
        "environment": list(profile.environment),
        "input_closure": profile.input_closure,
        "timeout_seconds": profile.timeout_seconds,
        "resource_class": profile.resource_class,
        "level": profile.level,
        "frozen_base": profile.frozen_base,
        "full_scope": profile.full_scope,
    }


def _runtime_adapter_policy(
    repo: GitRepo, *, cwd: Path, operation: str, adapter: Any
) -> dict[str, Any]:
    command = getattr(adapter, operation)
    input_hashes = _matching_hashes(cwd, _tracked(repo, cwd), adapter.input_paths)
    if command is not None and not input_hashes:
        raise SoloAIError("Runtime Adapter input_paths did not match any tracked file")
    return {
        "operation": operation,
        "command": _command_policy(command),
        "input_paths": list(adapter.input_paths),
        "input_hashes": input_hashes,
        "timeout_seconds": adapter.timeout_seconds,
        "context_contract": "dww-runtime-adapter-v1" if command else None,
    }


def approval_plan(
    repo: GitRepo,
    *,
    cwd: Path,
    verification: VerificationConfig,
    scope: str = "all",
    profile_ids: tuple[str, ...] | None = None,
    include_secret_scanner: bool = False,
    include_warm_commands: bool = False,
    include_dev_start: bool = False,
    adapter_operations: tuple[str, ...] = (),
) -> dict[str, Any]:
    """生成本次动作将执行的最小批准契约，不混入证明现场事实。"""
    repo_config = load_repo_config(repo, cwd=cwd)
    profiles_by_id = {profile.profile_id: profile for profile in verification.profiles}
    if scope == "all":
        profile_ids = tuple(profile.profile_id for profile in verification.profiles)
        include_secret_scanner = repo_config.secret_scanner is not None
        include_warm_commands = bool(repo_config.warm_commands)
        include_dev_start = repo_config.dev_start is not None
        adapter_operations = (
            "activate",
            "release",
            "batch_activate",
            "batch_release",
            "verify_effective",
        )
    requested_ids = tuple(profile_ids or ())
    unknown_profiles = sorted(set(requested_ids) - set(profiles_by_id))
    if unknown_profiles:
        raise SoloAIError(
            "Approval requested unknown verification profiles: "
            + ", ".join(unknown_profiles)
        )
    unknown_operations = sorted(
        set(adapter_operations)
        - {"activate", "release", "batch_activate", "batch_release", "verify_effective"}
    )
    if unknown_operations:
        raise SoloAIError(
            "Approval requested unknown Runtime Adapter operations: "
            + ", ".join(unknown_operations)
        )
    selected_profiles = [
        _profile_policy(profiles_by_id[item]) for item in requested_ids
    ]
    adapter = repo_config.runtime_adapter
    selected_adapters = [
        _runtime_adapter_policy(repo, cwd=cwd, operation=operation, adapter=adapter)
        for operation in adapter_operations
        if getattr(adapter, operation) is not None
    ]
    readiness = repo_config.readiness
    dev_start = (
        {
            "command": _command_policy(repo_config.dev_start),
            "readiness": (
                {
                    "kind": readiness.kind,
                    "target": readiness.target,
                    "timeout_seconds": readiness.timeout_seconds,
                }
                if readiness is not None
                else None
            ),
            "port_base": repo_config.port_base,
        }
        if include_dev_start and repo_config.dev_start is not None
        else None
    )
    return {
        "schema_version": APPROVAL_PLAN_SCHEMA,
        "contract": "execution-policy-v2",
        "git_common_dir": sha256_text(str(repo.common_dir)),
        "scope": scope,
        "policy": {
            "static_only": verification.static_only,
            "profiles": selected_profiles,
            "secret_scanner": (
                _command_policy(repo_config.secret_scanner)
                if include_secret_scanner and repo_config.secret_scanner is not None
                else None
            ),
            "warm_commands": (
                [_command_policy(command) for command in repo_config.warm_commands]
                if include_warm_commands
                else []
            ),
            "dev_start": dev_start,
            "runtime_adapter": selected_adapters,
        },
    }


def _approval_plan_differences(
    approved: Any, current: Any, *, path: str = "$"
) -> list[dict[str, Any]]:
    """Return exact JSON-field differences without guessing which drift is safe."""
    if isinstance(approved, dict) and isinstance(current, dict):
        differences: list[dict[str, Any]] = []
        for key in sorted(set(approved) | set(current)):
            child = f"{path}.{key}"
            if key not in approved:
                differences.append(
                    {
                        "path": child,
                        "approved": {"missing": True},
                        "current": current[key],
                    }
                )
            elif key not in current:
                differences.append(
                    {
                        "path": child,
                        "approved": approved[key],
                        "current": {"missing": True},
                    }
                )
            else:
                differences.extend(
                    _approval_plan_differences(approved[key], current[key], path=child)
                )
        return differences
    if isinstance(approved, list) and isinstance(current, list):
        differences = []
        for index in range(max(len(approved), len(current))):
            child = f"{path}[{index}]"
            if index >= len(approved):
                differences.append(
                    {
                        "path": child,
                        "approved": {"missing": True},
                        "current": current[index],
                    }
                )
            elif index >= len(current):
                differences.append(
                    {
                        "path": child,
                        "approved": approved[index],
                        "current": {"missing": True},
                    }
                )
            else:
                differences.extend(
                    _approval_plan_differences(
                        approved[index], current[index], path=child
                    )
                )
        return differences
    if approved == current:
        return []
    return [{"path": path, "approved": approved, "current": current}]


def _approved_policy_covers(approved: dict[str, Any], current: dict[str, Any]) -> bool:
    if approved.get("contract") != "execution-policy-v2":
        return False
    if approved.get("git_common_dir") != current.get("git_common_dir"):
        return False
    approved_policy = approved.get("policy")
    current_policy = current.get("policy")
    if not isinstance(approved_policy, dict) or not isinstance(current_policy, dict):
        return False
    if approved_policy.get("static_only") != current_policy.get("static_only"):
        return False
    approved_profiles = {
        item.get("id"): item
        for item in approved_policy.get("profiles", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    for profile in current_policy.get("profiles", []):
        if (
            not isinstance(profile, dict)
            or approved_profiles.get(profile.get("id")) != profile
        ):
            return False
    for field in ("secret_scanner", "dev_start"):
        requested = current_policy.get(field)
        if requested is not None and approved_policy.get(field) != requested:
            return False
    requested_warm = current_policy.get("warm_commands", [])
    if requested_warm and approved_policy.get("warm_commands") != requested_warm:
        return False
    approved_adapter = {
        item.get("operation"): item
        for item in approved_policy.get("runtime_adapter", [])
        if isinstance(item, dict) and isinstance(item.get("operation"), str)
    }
    for operation in current_policy.get("runtime_adapter", []):
        if (
            not isinstance(operation, dict)
            or approved_adapter.get(operation.get("operation")) != operation
        ):
            return False
    return True


def _legacy_approval_covers(approved: dict[str, Any], current: dict[str, Any]) -> bool:
    """仅在旧全量计划能逐项证明覆盖当前步骤时兼容。"""
    if approved.get("contract") != "execution-policy-v1":
        return False
    if approved.get("git_common_dir") != current.get("git_common_dir"):
        return False
    configuration = approved.get("policy", {}).get("configuration", {})
    repository = (
        configuration.get("repository", {}) if isinstance(configuration, dict) else {}
    )
    verification = (
        configuration.get("verification", {}) if isinstance(configuration, dict) else {}
    )
    if not isinstance(repository, dict) or not isinstance(verification, dict):
        return False
    current_policy = current.get("policy", {})
    if verification.get("static_only") != current_policy.get("static_only"):
        return False
    old_profiles = {
        item.get("id"): item
        for item in verification.get("profiles", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    for profile in current_policy.get("profiles", []):
        if old_profiles.get(profile.get("id")) != profile:
            return False
    if current_policy.get("secret_scanner") is not None and repository.get(
        "secret_scanner"
    ) != current_policy.get("secret_scanner"):
        return False
    if current_policy.get("warm_commands") and repository.get(
        "warm_commands"
    ) != current_policy.get("warm_commands"):
        return False
    requested_dev = current_policy.get("dev_start")
    if requested_dev is not None:
        old_dev = {
            "command": repository.get("dev_start"),
            "readiness": repository.get("readiness"),
            "port_base": repository.get("port_base"),
        }
        if old_dev != requested_dev:
            return False
    return not current_policy.get("runtime_adapter")


def _approval_command(scope: str, target: dict[str, str] | None) -> str:
    selectors = target or {}
    suffix = "".join(
        f" --{key.replace('_', '-')} {value}"
        for key, value in sorted(selectors.items())
    )
    return f"approve --accept --scope {scope}{suffix}"


def require_approved_plan(
    repo: GitRepo,
    *,
    cwd: Path,
    verification: VerificationConfig,
    message: str,
    scope: str = "all",
    profile_ids: tuple[str, ...] | None = None,
    include_secret_scanner: bool = False,
    include_warm_commands: bool = False,
    include_dev_start: bool = False,
    adapter_operations: tuple[str, ...] = (),
    approval_target: dict[str, str] | None = None,
) -> str:
    """要求覆盖本步骤的本机批准，并在失败时保存精确差异。"""
    plan = approval_plan(
        repo,
        cwd=cwd,
        verification=verification,
        scope=scope,
        profile_ids=profile_ids,
        include_secret_scanner=include_secret_scanner,
        include_warm_commands=include_warm_commands,
        include_dev_start=include_dev_start,
        adapter_operations=adapter_operations,
    )
    policy = plan["policy"]
    if not any(
        (
            policy["profiles"],
            policy["secret_scanner"],
            policy["warm_commands"],
            policy["dev_start"],
            policy["runtime_adapter"],
        )
    ):
        return sha256_text(stable_json(plan))
    fingerprint = sha256_text(stable_json(plan))
    approvals = read_json(repo.local_dir / "approvals.json", {"accepted": {}})
    accepted = approvals.get("accepted", {})
    if not isinstance(accepted, dict):
        accepted = {}
    sanitized_accepted: dict[str, Any] = {}
    approval_record_changed = False
    for approved_fingerprint, record in accepted.items():
        if not isinstance(record, dict):
            sanitized_accepted[str(approved_fingerprint)] = record
            continue
        sanitized_record = copy.deepcopy(record)
        approved_plan = sanitized_record.get("plan")
        if isinstance(approved_plan, dict):
            redacted_plan = _redact_legacy_approval_plan(approved_plan)
            if redacted_plan != approved_plan:
                sanitized_record["plan"] = redacted_plan
                approval_record_changed = True
        sanitized_accepted[str(approved_fingerprint)] = sanitized_record
    if approval_record_changed:
        approvals = {**approvals, "accepted": sanitized_accepted}
        atomic_write_json(repo.local_dir / "approvals.json", approvals)
    if fingerprint in sanitized_accepted:
        return fingerprint
    comparisons: list[tuple[int, str, str, list[dict[str, Any]]]] = []
    for approved_fingerprint, record in sanitized_accepted.items():
        approved_plan = record.get("plan") if isinstance(record, dict) else None
        if not isinstance(approved_plan, dict):
            continue
        if _approved_policy_covers(approved_plan, plan) or _legacy_approval_covers(
            approved_plan, plan
        ):
            return str(approved_fingerprint)
        differences = _approval_plan_differences(approved_plan, plan)
        comparisons.append(
            (
                len(differences),
                str(record.get("accepted_at") or ""),
                str(approved_fingerprint),
                differences,
            )
        )
    nearest = None
    if comparisons:
        minimum_difference_count = min(item[0] for item in comparisons)
        nearest = max(
            (item for item in comparisons if item[0] == minimum_difference_count),
            key=lambda item: (item[1], item[2]),
        )
    report = {
        "schema_version": 2,
        "current_fingerprint": fingerprint,
        "nearest_approved_fingerprint": nearest[2] if nearest else None,
        "nearest_approved_at": nearest[1] if nearest else None,
        "difference_count": nearest[0] if nearest else None,
        "differences": nearest[3] if nearest else [],
        "scope": scope,
        "target": approval_target or {},
    }
    report_path = repo.local_dir / "approval-mismatches" / f"{fingerprint}.json"
    atomic_write_json(report_path, report)
    detail = (
        f"{nearest[0]} normalized field(s) differ from the nearest accepted plan"
        if nearest
        else "no accepted plan exists"
    )
    raise ActionableSoloAIError(
        f"{message} {detail}. Local report: {report_path}. Review doctor then run {_approval_command(scope, approval_target)}.",
        code="APPROVAL_REQUIRED",
        context={"scope": scope, **(approval_target or {})},
        next_action={
            "kind": "approve_scope",
            "scope": scope,
            **(approval_target or {}),
        },
    )


def proof_inputs(
    repo: GitRepo,
    *,
    cwd: Path,
    base: str,
    verification: VerificationConfig,
    task_id: str | None = None,
    levels: tuple[str, ...] = ("ready",),
    full_scopes: tuple[str, ...] | None = None,
    force_task_scope: bool = False,
    expected_candidate_head: str | None = None,
    full_execution_id: str | None = None,
    tool_cache: dict[tuple[Any, ...], dict[str, str | None]] | None = None,
    validation_environment: dict[str, str] | None = None,
) -> tuple[dict[str, Any], list[tuple[VerificationProfile, dict[str, Any], str]]]:
    _require_expected_candidate_head(
        repo, cwd=cwd, expected_candidate_head=expected_candidate_head
    )
    frozen_environment = dict(validation_environment or {})
    files = changed_files(repo, cwd=cwd, base=base)
    profiles = select_profiles(
        verification, files, levels=levels, full_scopes=full_scopes
    )
    missing = unmapped_files(
        verification, files, levels=levels, full_scopes=full_scopes
    )
    commands = [command for profile in profiles for command in profile.commands]
    tracked = _tracked(repo, cwd)
    # 汇总证明保留完整的运行计划，便于审计；单个 profile 的复用身份只包含
    # 它自己声明的输入，避免无关检查、锁文件或策略修改使已验证的检查失效。
    shared = _shared_inputs(repo, cwd, commands, verification, tool_cache)
    candidate_head = repo.head(cwd)
    candidate_tree = repo.tree(cwd=cwd)
    records: list[tuple[VerificationProfile, dict[str, Any], str]] = []
    for profile in profiles:
        inputs = _profile_inputs(
            profile,
            cwd=cwd,
            tracked=tracked,
            shared=_shared_inputs(
                repo,
                cwd,
                list(profile.commands),
                verification,
                tool_cache,
                include_policy=False,
            ),
            validation_environment=_profile_validation_environment(
                profile, frozen_environment
            ),
        )
        # 输入闭包完整且没有外部状态的检查，天然可在任务和批次之间复用；
        # 其余检查仍固定在当前任务（或无任务时的当前候选）上。
        if force_task_scope and task_id:
            scope = f"task:{task_id}"
        elif profile.external_state == "none" and profile.input_closure == "complete":
            scope = "repository"
        elif task_id:
            scope = f"task:{task_id}"
        else:
            scope = f"candidate:{candidate_head}"
        inputs["reuse_scope"] = scope
        if {"full", "stress"}.intersection(levels) and not (
            profile.external_state == "none" and profile.input_closure == "complete"
        ):
            # 新Full或显式Stress不是旧事务收尾：未知环境及产物生产检查不能跨执行复用。
            # 已通过Full后的释放/推进恢复由批次事务处理，不重新进入validate。
            inputs["full_execution"] = full_execution_id or "plan-only"
        records.append((profile, inputs, sha256_text(stable_json(inputs))))
    visible_validation_environment = (
        frozen_environment if any(profile.frozen_base for profile in profiles) else {}
    )
    candidate = {
        "schema_version": PROOF_SCHEMA,
        **shared,
        "candidate_head": candidate_head,
        "candidate_tree": candidate_tree,
        "base_head": frozen_environment.get("DWW_VALIDATION_BASE_HEAD")
        or repo.git(["rev-parse", base], cwd=cwd).stdout.strip(),
        "validation_environment": visible_validation_environment,
        "files": files,
        "unmapped_files": missing,
        "levels": list(levels),
        "full_scopes": list(full_scopes) if full_scopes is not None else None,
        "profiles": [profile.profile_id for profile in profiles],
        "profile_fingerprints": [item[2] for item in records],
        "command_manifest": [
            {"profile_id": profile.profile_id, "command_digest": command.fingerprint}
            for profile in profiles
            for command in profile.commands
        ],
    }
    return candidate, records


def _logs_exist(proof: dict[str, Any]) -> bool:
    runs = proof.get("runs")
    if not isinstance(runs, list) or not runs:
        return False
    try:
        if proof.get("result") == "passed":
            inputs = proof.get("inputs") or {}
            if (
                "command_digests" in inputs
                and [item.get("command_digest") for item in runs]
                != inputs["command_digests"]
            ):
                return False
            if (
                inputs.get("command_manifest")
                and [
                    {
                        "profile_id": item.get("profile_id"),
                        "command_digest": item.get("command_digest"),
                    }
                    for item in runs
                ]
                != inputs["command_manifest"]
            ):
                return False
        return all(
            isinstance(item, dict)
            and (
                proof.get("result") != "passed"
                or (item.get("exit_code") == 0 and not item.get("timed_out"))
            )
            and Path(item["log"]).is_file()
            and sha256_file(Path(item["log"])) == item.get("log_sha256")
            for item in runs
        )
    except (OSError, KeyError, TypeError, ValueError, AttributeError):
        return False


def _require_profile_inputs(
    repo: GitRepo,
    *,
    cwd: Path,
    profile: VerificationProfile,
    inputs: dict[str, Any],
    verification: VerificationConfig,
    tool_cache: dict[tuple[Any, ...], dict[str, str | None]],
    validation_environment: dict[str, str],
) -> None:
    """HEAD未变不足以证明输入稳定；复核该检查声明的输入、工具和环境。"""
    current = _profile_inputs(
        profile,
        cwd=cwd,
        tracked=_tracked(repo, cwd),
        shared=_shared_inputs(
            repo,
            cwd,
            list(profile.commands),
            verification,
            tool_cache,
            include_policy=False,
        ),
        validation_environment=_profile_validation_environment(
            profile, validation_environment
        ),
    )
    if any(inputs.get(key) != value for key, value in current.items()):
        reasons = _profile_input_change_reasons(inputs, current)
        previous_paths = inputs.get("tracked_inputs", {})
        current_paths = current.get("tracked_inputs", {})
        changed_paths = sorted(
            path
            for path in set(previous_paths).union(current_paths)
            if previous_paths.get(path) != current_paths.get(path)
        )
        detail = ", ".join(reasons)
        if changed_paths:
            visible_paths = changed_paths[:20]
            suffix = "" if len(changed_paths) <= len(visible_paths) else f" (+{len(changed_paths) - len(visible_paths)} more)"
            detail += "; declared paths: " + ", ".join(visible_paths) + suffix
        raise SoloAIError(
            f"Validation inputs changed for profile {profile.profile_id}; "
            f"{detail}; no successful proof may certify this execution"
        )


def _require_stored_proof_identity(
    proof: dict[str, Any], *, fingerprint: str, inputs: dict[str, Any]
) -> None:
    if not proof:
        return
    if (
        proof.get("schema_version") != PROOF_SCHEMA
        or proof.get("fingerprint") != fingerprint
        or proof.get("inputs") != inputs
    ):
        raise SoloAIError(
            "Stored validation proof identity changed; inspect or prune proofs before rerunning"
        )


def _deterministic_failure(profile: VerificationProfile, proof: dict[str, Any]) -> bool:
    return (
        profile.external_state == "none"
        and profile.input_closure == "complete"
        and bool(proof.get("runs"))
        and not any(bool(run.get("timed_out")) for run in proof.get("runs", []))
    )


def require_exact_passed_proof(
    proof: dict[str, Any], *, fingerprint: str, candidate_head: str, base_head: str
) -> None:
    """恢复旧任务前严格核验门禁证明与不可变候选的绑定。"""
    inputs = proof.get("inputs") or {}
    if proof.get("schema_version") != PROOF_SCHEMA:
        raise SoloAIError("Unsupported validation proof schema")
    if proof.get("fingerprint") != fingerprint or proof.get("result") != "passed":
        raise SoloAIError("Validation proof identity or result is invalid")
    if inputs.get("candidate_head") != candidate_head:
        raise SoloAIError("Validation proof belongs to another candidate")
    if inputs.get("base_head") != base_head:
        raise SoloAIError("Validation proof belongs to another base snapshot")
    if not _logs_exist(proof):
        raise SoloAIError("Validation proof logs are missing or changed")


def _content_address_log(repo: GitRepo, temporary: Path) -> tuple[Path, str]:
    digest = sha256_file(temporary)
    target = repo.local_dir / "logs" / "content" / f"{digest}.log"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and sha256_file(target) == digest:
        temporary.unlink(missing_ok=True)
    else:
        # 同名缓存日志损坏时，用本次真实执行得到的同摘要日志恢复它。
        temporary.replace(target)
    return target, digest


def _profile_history_path(repo: GitRepo, profile_id: str) -> Path:
    """为同一仓库中的单个 profile 保留最近可比较成功证明的精确定位。"""
    return repo.local_dir / "profile-proof-history" / f"{sha256_text(profile_id)}.json"


def _record_profile_history(
    repo: GitRepo, *, profile_id: str, fingerprint: str
) -> None:
    atomic_write_json(
        _profile_history_path(repo, profile_id),
        {
            "schema_version": PROFILE_HISTORY_SCHEMA,
            "profile_id": profile_id,
            "fingerprint": fingerprint,
            "recorded_at": utc_timestamp(),
        },
    )


def _previous_profile_inputs(
    repo: GitRepo, *, profile_id: str, fingerprint: str
) -> dict[str, Any] | None:
    """读取一个可核验的同 profile 成功证明；历史缺失不会阻止新的执行。"""
    try:
        pointer = read_json(_profile_history_path(repo, profile_id), {})
        if (
            not isinstance(pointer, dict)
            or not isinstance(pointer.get("fingerprint"), str)
            or pointer.get("schema_version") != PROFILE_HISTORY_SCHEMA
            or pointer.get("profile_id") != profile_id
            or pointer["fingerprint"] == fingerprint
        ):
            return None
        proof = read_json(
            repo.local_dir / "profile-proofs" / f"{pointer['fingerprint']}.json", {}
        )
    except SoloAIError:
        return None
    if not isinstance(proof, dict) or (
        proof.get("schema_version") != PROOF_SCHEMA
        or proof.get("fingerprint") != pointer["fingerprint"]
        or proof.get("result") != "passed"
        or (proof.get("inputs") or {}).get("profile_id") != profile_id
        or not _logs_exist(proof)
    ):
        return None
    inputs = proof.get("inputs")
    return inputs if isinstance(inputs, dict) else None


def _profile_input_change_reasons(
    previous: dict[str, Any], current: dict[str, Any]
) -> list[str]:
    labels = {
        "tracked_inputs": "declared_inputs_changed",
        "candidate_head": "candidate_snapshot_changed",
        "base_head": "base_snapshot_changed",
        "command_digests": "command_changed",
        "tools": "tool_changed",
        "platform": "platform_changed",
        "environment": "environment_changed",
        "dww_validation_environment": "validation_environment_changed",
        "lockfiles": "dependency_lockfile_changed",
        "paths": "selection_rule_changed",
        "external_state": "external_state_changed",
        "input_closure": "input_closure_changed",
        "timeout_seconds": "timeout_changed",
        "resource_class": "resource_class_changed",
        "level": "validation_level_changed",
        "reuse_scope": "reuse_scope_changed",
        "reuse_contract": "reuse_contract_changed",
        "full_execution": "new_full_execution_required",
    }
    changes = [
        label for key, label in labels.items() if previous.get(key) != current.get(key)
    ]
    return changes or ["profile_inputs_changed"]


def profile_execution_decision(
    repo: GitRepo,
    *,
    profile: VerificationProfile,
    inputs: dict[str, Any],
    fingerprint: str,
) -> dict[str, Any]:
    """用执行路径相同的规则解释一个 profile 是复用、执行还是被阻止。"""

    proof_path = repo.local_dir / "profile-proofs" / f"{fingerprint}.json"
    existing = read_json(proof_path, {})
    if existing:
        try:
            _require_stored_proof_identity(
                existing, fingerprint=fingerprint, inputs=inputs
            )
        except SoloAIError:
            return {
                "action": "blocked",
                "reason": "stored_proof_identity_changed",
                "proof": str(proof_path),
            }
    if existing.get("result") == "passed" and _logs_exist(existing):
        return {
            "action": "reuse",
            "reason": "matching_successful_proof",
            "proof": str(proof_path),
        }
    if (
        existing.get("result") == "failed"
        and _logs_exist(existing)
        and _deterministic_failure(profile, existing)
    ):
        return {
            "action": "blocked",
            "reason": "matching_deterministic_failure",
            "proof": str(proof_path),
        }
    if existing and not _logs_exist(existing):
        reason = "stored_proof_logs_missing_or_changed"
    elif inputs.get("full_execution"):
        reason = "new_full_execution_required"
    else:
        reason = "no_matching_successful_proof"
    decision = {"action": "execute", "reason": reason, "proof": str(proof_path)}
    previous = _previous_profile_inputs(
        repo, profile_id=profile.profile_id, fingerprint=fingerprint
    )
    if previous is not None:
        decision["previous_input_changes"] = _profile_input_change_reasons(
            previous, inputs
        )
    return decision


def profile_selection_reason(
    profile: VerificationProfile, files: list[str]
) -> dict[str, Any]:
    """把触发选择的实际候选文件投影给 plan 与尝试回执。"""

    matched = [
        path
        for path in files
        if any(fnmatch.fnmatchcase(path, pattern) for pattern in profile.paths)
    ]
    return {"matched_files": matched, "path_patterns": list(profile.paths)}


def _run_profile(
    repo: GitRepo,
    *,
    cwd: Path,
    profile: VerificationProfile,
    inputs: dict[str, Any],
    fingerprint: str,
    task_id: str | None,
    base: str,
    expected_base_head: str | None,
    expected_candidate_head: str | None,
    validation_environment: dict[str, str],
    check_inputs: Callable[[], None],
    attempt_id: str,
) -> dict[str, Any]:
    proof_path = repo.local_dir / "profile-proofs" / f"{fingerprint}.json"

    check_inputs()
    decision = profile_execution_decision(
        repo, profile=profile, inputs=inputs, fingerprint=fingerprint
    )
    if decision["action"] == "reuse":
        existing = read_json(proof_path, {})
        _require_expected_candidate_head(
            repo, cwd=cwd, expected_candidate_head=expected_candidate_head
        )
        existing["reused_at"] = utc_timestamp()
        atomic_write_json(proof_path, existing)
        _record_profile_history(
            repo, profile_id=profile.profile_id, fingerprint=fingerprint
        )
        _update_validation_attempt_profile(
            repo,
            attempt_id,
            profile.profile_id,
            state="reused",
            reused=True,
            proof=fingerprint,
            completed_at=utc_timestamp(),
        )
        return {**existing, "reused": True}
    if decision["action"] == "blocked":
        _update_validation_attempt_profile(
            repo,
            attempt_id,
            profile.profile_id,
            state="blocked",
            error_reason=decision["reason"],
            completed_at=utc_timestamp(),
        )
        if decision["reason"] == "matching_deterministic_failure":
            raise ActionableSoloAIError(
                f"Validation profile {profile.profile_id} already failed with the same complete deterministic inputs. Change the candidate or policy, or explicitly reclassify its external state before retrying.",
                code="DETERMINISTIC_VALIDATION_FAILED",
                context={"profile_id": profile.profile_id},
                next_action={
                    "kind": "inspect_validation_evidence",
                    "profile_id": profile.profile_id,
                    "retry": "after_change_or_reclassification",
                },
            )
        raise SoloAIError(
            "Stored validation proof identity changed; inspect or prune proofs"
        )
    _update_validation_attempt_profile(
        repo,
        attempt_id,
        profile.profile_id,
        state="waiting",
        reused=False,
        execution_reason=decision["reason"],
        queue={"resource_class": profile.resource_class},
    )
    run_id = new_id(f"profile-{profile.profile_id}")
    temp_dir = repo.local_dir / "logs" / "pending" / run_id
    runs: list[dict[str, Any]] = []
    with claim_validation_slot(profile.resource_class) as queue_claim:
        _update_validation_attempt_profile(
            repo,
            attempt_id,
            profile.profile_id,
            state="preparing",
            queue={
                "resource_class": profile.resource_class,
                "ticket": queue_claim["id"],
                "queued_at": queue_claim.get("queued_at"),
                "acquired_at": queue_claim.get("acquired_at"),
                "wait_seconds": queue_claim.get("wait_seconds"),
            },
        )
        for index, command in enumerate(profile.commands, 1):
            _require_expected_candidate_head(
                repo, cwd=cwd, expected_candidate_head=expected_candidate_head
            )
            _require_expected_base_head(
                repo,
                cwd=cwd,
                base=base,
                expected_base_head=expected_base_head,
            )
            check_inputs()
            pending = temp_dir / f"{index:02d}.log"
            receipt_path = (
                repo.local_dir / "validation-runs" / run_id / f"{index:02d}.json"
            )
            environment = _execution_environment(profile)
            environment.update(validation_environment)
            environment.update(inherited_claim_environment(queue_claim))

            def command_started(start: dict[str, Any]) -> None:
                _update_validation_attempt_profile(
                    repo,
                    attempt_id,
                    profile.profile_id,
                    state="running",
                    current_command={
                        "index": index,
                        "count": len(profile.commands),
                        "command_digest": command.fingerprint,
                        "receipt": str(receipt_path),
                        "process": copy.deepcopy(start.get("process")),
                    },
                )

            def command_heartbeat(heartbeat: dict[str, Any]) -> None:
                _update_validation_attempt_profile(
                    repo,
                    attempt_id,
                    profile.profile_id,
                    state="running",
                    current_command={
                        "index": index,
                        "count": len(profile.commands),
                        "command_digest": command.fingerprint,
                        "receipt": str(receipt_path),
                        "process": copy.deepcopy(heartbeat.get("process")),
                        "elapsed_seconds": heartbeat.get("elapsed_seconds"),
                    },
                )

            try:
                result = run_logged(
                    command.argv,
                    cwd=cwd,
                    log_path=pending,
                    timeout_seconds=profile.timeout_seconds,
                    environment=environment,
                    on_start=command_started,
                    on_heartbeat=command_heartbeat,
                    receipt_path=receipt_path,
                    receipt_metadata={
                        "task_id": task_id,
                        "profile_id": profile.profile_id,
                        "profile_fingerprint": fingerprint,
                        "queue_ticket": queue_claim["id"],
                    },
                )
            except BaseException:
                receipt = read_json(receipt_path, {})
                if receipt:
                    runs.append(
                        {
                            "command_digest": command.fingerprint,
                            "command": command.redacted(),
                            "receipt": str(receipt_path),
                            "duration_seconds": receipt.get("duration_seconds"),
                            "status": receipt.get("status", "interrupted"),
                            "process": receipt.get("process"),
                            "log": receipt.get("log"),
                        }
                    )
                _update_validation_attempt_profile(
                    repo,
                    attempt_id,
                    profile.profile_id,
                    state="interrupted",
                    current_command=None,
                    runs=copy.deepcopy(runs),
                )
                raise
            run_record = {
                "command_digest": command.fingerprint,
                "command": command.redacted(),
                "exit_code": result.returncode,
                "duration_seconds": round(result.duration_seconds, 3),
                "timed_out": result.timed_out,
                "process": result.process,
                "receipt": str(receipt_path),
                "log": str(pending),
                "log_sha256": None,
            }
            runs.append(run_record)
            _update_validation_attempt_profile(
                repo,
                attempt_id,
                profile.profile_id,
                current_command=None,
                runs=copy.deepcopy(runs),
            )
            try:
                log_path, log_digest = _content_address_log(repo, pending)
                run_record["log"] = str(log_path)
                run_record["log_sha256"] = log_digest
                _update_validation_attempt_profile(
                    repo,
                    attempt_id,
                    profile.profile_id,
                    state="preparing",
                    runs=copy.deepcopy(runs),
                )
                _require_expected_candidate_head(
                    repo, cwd=cwd, expected_candidate_head=expected_candidate_head
                )
                check_inputs()
            except BaseException as error:
                _update_validation_attempt_profile(
                    repo,
                    attempt_id,
                    profile.profile_id,
                    state=(
                        "interrupted"
                        if isinstance(error, (KeyboardInterrupt, SystemExit))
                        else "failed"
                    ),
                    current_command=None,
                    error_reason=(
                        "interrupted_after_command"
                        if isinstance(error, (KeyboardInterrupt, SystemExit))
                        else "post_command_input_check_failed"
                    ),
                    runs=copy.deepcopy(runs),
                )
                raise
            if result.returncode != 0:
                # 旧基线上的失败不是当前候选的有效结论；交给 Ready 同步后重试。
                _require_expected_base_head(
                    repo,
                    cwd=cwd,
                    base=base,
                    expected_base_head=expected_base_head,
                )
                proof = {
                    "schema_version": PROOF_SCHEMA,
                    "fingerprint": fingerprint,
                    "result": "failed",
                    "inputs": inputs,
                    "runs": runs,
                    "queue": {
                        "resource_class": profile.resource_class,
                        "wait_seconds": queue_claim["wait_seconds"],
                    },
                    "created_at": utc_timestamp(),
                }
                atomic_write_json(proof_path, proof)
                _update_validation_attempt_profile(
                    repo,
                    attempt_id,
                    profile.profile_id,
                    state="timed_out" if result.timed_out else "failed",
                    proof=fingerprint,
                    completed_at=utc_timestamp(),
                )
                raise SoloAIError(
                    f"Validation {'timed out' if result.timed_out else 'failed'} in profile {profile.profile_id}. Local redacted log: {log_path}"
                )
    record_profile_duration(
        profile_id=profile.profile_id,
        command_digests=[command.fingerprint for command in profile.commands],
        duration_seconds=sum(float(run["duration_seconds"]) for run in runs),
    )
    proof = {
        "schema_version": PROOF_SCHEMA,
        "fingerprint": fingerprint,
        "result": "passed",
        "inputs": inputs,
        "runs": runs,
        "queue": {
            "resource_class": profile.resource_class,
            "wait_seconds": queue_claim["wait_seconds"],
        },
        "created_at": utc_timestamp(),
    }
    atomic_write_json(proof_path, proof)
    _record_profile_history(
        repo, profile_id=profile.profile_id, fingerprint=fingerprint
    )
    _update_validation_attempt_profile(
        repo,
        attempt_id,
        profile.profile_id,
        state="passed",
        proof=fingerprint,
        completed_at=utc_timestamp(),
        runs=copy.deepcopy(runs),
    )
    return {**proof, "reused": False}


def validate(
    repo: GitRepo,
    *,
    cwd: Path,
    base: str,
    verification: VerificationConfig,
    task_id: str | None = None,
    level: str = "ready",
    force_task_scope: bool = False,
    expected_base_head: str | None = None,
    expected_candidate_head: str | None = None,
    full_scope: str = "integration",
    validation_base_ref: str | None = None,
    attempt_id: str | None = None,
    attempt_owner: dict[str, str] | None = None,
) -> dict[str, Any]:
    if full_scope not in {"integration", "complete"}:
        raise SoloAIError("full_scope must be integration or complete")
    if level != "full" and full_scope != "integration":
        raise SoloAIError("A complete validation scope requires level full")
    attempt_id = attempt_id or new_validation_attempt_id(level)
    start_validation_attempt(
        repo,
        attempt_id=attempt_id,
        level=level,
        full_scope=full_scope,
        task_id=task_id,
        owner=attempt_owner,
    )
    levels = ("ready", "full") if level == "full" else (level,)
    full_scopes = (
        ("integration", "complete")
        if level == "full" and full_scope == "complete"
        else (("integration",) if level == "full" else None)
    )
    # 仅本次调用内复用版本探测；每次读取均复核解析路径与文件身份。
    tool_cache: dict[tuple[Any, ...], dict[str, str | None]] = {}
    validation_environment: dict[str, str] = {}
    if level in {"ready", "full"}:
        validation_environment = frozen_validation_environment(
            repo,
            cwd=cwd,
            base=base,
            validation_base_ref=validation_base_ref,
            expected_base_head=expected_base_head,
            full_scope=full_scope if level == "full" else None,
        )
    try:
        inputs, records = proof_inputs(
            repo,
            cwd=cwd,
            base=base,
            verification=verification,
            task_id=task_id,
            levels=levels,
            full_scopes=full_scopes,
            force_task_scope=force_task_scope,
            expected_candidate_head=expected_candidate_head,
            full_execution_id=(
                new_id(f"{level}-validation") if level in {"full", "stress"} else None
            ),
            tool_cache=tool_cache,
            validation_environment=validation_environment,
        )
        planned_profiles = [
            {
                "id": profile.profile_id,
                "level": profile.level,
                "fingerprint": profile_fingerprint,
                "state": "pending",
                "selection": profile_selection_reason(profile, inputs["files"]),
                "decision": profile_execution_decision(
                    repo,
                    profile=profile,
                    inputs=profile_inputs,
                    fingerprint=profile_fingerprint,
                ),
                "runs": [],
            }
            for profile, profile_inputs, profile_fingerprint in records
        ]
        _update_validation_attempt(
            repo,
            attempt_id,
            state="planned",
            candidate_head=inputs.get("candidate_head"),
            base_head=inputs.get("base_head"),
            changed_files=list(inputs.get("files") or []),
            unmapped_files=list(inputs.get("unmapped_files") or []),
            profiles=planned_profiles,
        )
        # Ready（以及包含 Ready 的 Full）是候选晋级门禁，必须覆盖所有候选
        # 路径。development 与显式 Stress 都是按变更选择的辅助执行层；要求
        # 每个文档或无关文件也匹配压力配置，会让它们错误地无法单独运行。
        if (
            level in {"ready", "full"}
            and inputs["unmapped_files"]
            and not verification.static_only
        ):
            raise SoloAIError(
                "No Ready verification profile covers every candidate path; add explicit path mappings:\n"
                + "\n".join(f"- {path}" for path in inputs["unmapped_files"][:20])
            )
        if not records and not verification.static_only:
            raise SoloAIError(
                "No verification profile covers the candidate changes; add an explicit path mapping or opt into static_only"
            )
        fingerprint = sha256_text(stable_json(inputs))
        proof_path = repo.local_dir / "proofs" / f"{fingerprint}.json"
        existing = read_json(proof_path, {})
        _require_stored_proof_identity(existing, fingerprint=fingerprint, inputs=inputs)
        if existing.get("result") == "passed" and _logs_exist(existing):
            _require_expected_candidate_head(
                repo, cwd=cwd, expected_candidate_head=expected_candidate_head
            )
            for profile, profile_inputs, profile_fingerprint in records:
                _require_profile_inputs(
                    repo,
                    cwd=cwd,
                    profile=profile,
                    inputs=profile_inputs,
                    verification=verification,
                    tool_cache=tool_cache,
                    validation_environment=validation_environment,
                )
                _update_validation_attempt_profile(
                    repo,
                    attempt_id,
                    profile.profile_id,
                    state="reused",
                    reused=True,
                    proof=profile_fingerprint,
                    completed_at=utc_timestamp(),
                )
            existing["reused_at"] = utc_timestamp()
            atomic_write_json(proof_path, existing)
            finish_validation_attempt(
                repo,
                attempt_id=attempt_id,
                result="passed",
                proof=fingerprint,
            )
            return {**existing, "reused": True}

        runs: list[dict[str, Any]] = []
        profile_proofs: list[dict[str, Any]] = []
        for profile, profile_inputs, profile_fingerprint in records:

            def check_inputs(profile=profile, profile_inputs=profile_inputs):
                _require_profile_inputs(
                    repo,
                    cwd=cwd,
                    profile=profile,
                    inputs=profile_inputs,
                    verification=verification,
                    tool_cache=tool_cache,
                    validation_environment=validation_environment,
                )

            result = _run_profile(
                repo,
                cwd=cwd,
                profile=profile,
                inputs=profile_inputs,
                fingerprint=profile_fingerprint,
                task_id=task_id,
                base=base,
                expected_base_head=expected_base_head,
                expected_candidate_head=expected_candidate_head,
                validation_environment=_profile_validation_environment(
                    profile, validation_environment
                ),
                check_inputs=check_inputs,
                attempt_id=attempt_id,
            )
            profile_proofs.append(
                {
                    "profile_id": profile.profile_id,
                    "fingerprint": profile_fingerprint,
                    "reused": result["reused"],
                }
            )
            runs.extend(
                {
                    **item,
                    "profile_id": profile.profile_id,
                    "reused": result["reused"],
                }
                for item in result["runs"]
            )
        if not records:
            log_path = (
                repo.local_dir / "logs" / "content" / "static-only-placeholder.log"
            )
            log_path.parent.mkdir(parents=True, exist_ok=True)
            if not log_path.exists():
                log_path.write_text(
                    "Static-only gate completed: Git candidate integrity and sensitive-content checks only. No test command ran.\n",
                    encoding="utf-8",
                    newline="\n",
                )
            runs.append(
                {
                    "command_digest": None,
                    "command": None,
                    "exit_code": 0,
                    "duration_seconds": 0.0,
                    "log": str(log_path),
                    "log_sha256": sha256_file(log_path),
                    "profile_id": None,
                    "reused": False,
                }
            )
        _require_expected_candidate_head(
            repo, cwd=cwd, expected_candidate_head=expected_candidate_head
        )
        for profile, profile_inputs, _ in records:
            _require_profile_inputs(
                repo,
                cwd=cwd,
                profile=profile,
                inputs=profile_inputs,
                verification=verification,
                tool_cache=tool_cache,
                validation_environment=_profile_validation_environment(
                    profile, validation_environment
                ),
            )
        proof = {
            "schema_version": PROOF_SCHEMA,
            "fingerprint": fingerprint,
            "result": "passed",
            "kind": "static-only" if not records else "commands",
            "inputs": inputs,
            "profile_proofs": profile_proofs,
            "runs": runs,
            "created_at": utc_timestamp(),
        }
        atomic_write_json(proof_path, proof)
        finish_validation_attempt(
            repo, attempt_id=attempt_id, result="passed", proof=fingerprint
        )
        return {**proof, "reused": False}
    except BaseException as exc:
        attempt = read_validation_attempt(repo, attempt_id)
        timed_out = any(
            profile.get("state") == "timed_out"
            for profile in attempt.get("profiles", [])
        )
        result = (
            "interrupted"
            if isinstance(exc, (KeyboardInterrupt, SystemExit))
            else ("timed_out" if timed_out else "failed")
        )
        finish_validation_attempt(
            repo, attempt_id=attempt_id, result=result, error=str(exc)
        )
        raise
