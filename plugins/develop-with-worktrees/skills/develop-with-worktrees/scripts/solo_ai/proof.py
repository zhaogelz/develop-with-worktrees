from __future__ import annotations

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
# 审批计划和验证收据的演进速度不同：前者描述可执行的策略，后者绑定现场证据。
APPROVAL_PLAN_SCHEMA = 4
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
    return {
        "argv_digest": command.fingerprint,
        "executable": redact_text(executable),
        "path": str(Path(resolved).resolve()) if resolved else None,
        "version": version,
    }


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
        "config_hashes": config_hashes,
        "lockfiles": {
            relative: sha256_file(cwd / relative)
            for relative in tracked
            if Path(relative).name in LOCKFILES and (cwd / relative).is_file()
        },
        "tools": [tool_facts(command) for command in unique.values()],
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
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
            "tail_policy": integration.tail_policy,
            "tail_quiet_seconds": integration.tail_quiet_seconds,
            "worktree_mode": integration.worktree_mode,
        },
    }


def _approval_policy_inputs(
    shared: dict[str, Any], *, repo_config: Any, verification: VerificationConfig
) -> dict[str, Any]:
    """Keep semantic execution approval separate from byte-exact proof evidence."""
    return {
        **{key: value for key, value in shared.items() if key != "config_hashes"},
        "configuration": {
            "repository": _repo_config_policy(repo_config),
            "verification": verification.normalized(),
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
    return {
        **shared,
        # 旧证明未执行单元输入前后复核，不能作为新复用契约的成功证明。
        "reuse_contract": 1,
        "profile_id": profile.profile_id,
        "paths": list(profile.paths),
        "command_digests": [command.fingerprint for command in profile.commands],
        "cross_task_reuse": profile.cross_task_reuse,
        "external_state": profile.external_state,
        "input_closure": profile.input_closure,
        "timeout_seconds": profile.timeout_seconds,
        "resource_class": profile.resource_class,
        "level": profile.level,
        "tracked_inputs": _matching_hashes(cwd, tracked, profile.input_paths),
        "environment": {
            name: sha256_text(os.environ[name]) if name in os.environ else "absent"
            for name in profile.environment
        },
        # DWW 注入的冻结基线会影响项目选择器；它与声明环境一样属于证明身份。
        "dww_validation_environment": dict(validation_environment or {}),
    }


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


def approval_plan(
    repo: GitRepo, *, cwd: Path, verification: VerificationConfig
) -> dict[str, Any]:
    repo_config = load_repo_config(repo, cwd=cwd)
    runtime_adapter = repo_config.runtime_adapter
    adapter_commands = [
        command
        for command in (
            runtime_adapter.activate,
            runtime_adapter.release,
            runtime_adapter.batch_activate,
            runtime_adapter.batch_release,
            runtime_adapter.verify_effective,
        )
        if command is not None
    ]
    adapter_input_hashes = _matching_hashes(
        cwd,
        _tracked(repo, cwd),
        runtime_adapter.input_paths,
    )
    if adapter_commands and not adapter_input_hashes:
        raise SoloAIError("Runtime Adapter input_paths did not match any tracked file")
    commands = [*verification.commands, *adapter_commands]
    shared = _shared_inputs(repo, cwd, commands, verification)
    return {
        "schema_version": APPROVAL_PLAN_SCHEMA,
        "contract": "execution-policy-v1",
        "git_common_dir": sha256_text(str(repo.common_dir)),
        "policy": _approval_policy_inputs(
            shared, repo_config=repo_config, verification=verification
        ),
        "profiles": [
            {
                "id": profile.profile_id,
                "paths": list(profile.paths),
                "commands": [command.redacted() for command in profile.commands],
                "command_digests": [
                    command.fingerprint for command in profile.commands
                ],
                "cross_task_reuse": profile.cross_task_reuse,
                "external_state": profile.external_state,
                "input_paths": list(profile.input_paths),
                "environment": list(profile.environment),
                "input_closure": profile.input_closure,
                "timeout_seconds": profile.timeout_seconds,
                "resource_class": profile.resource_class,
                "level": profile.level,
            }
            for profile in verification.profiles
        ],
        "runtime_adapter": {
            "activate": runtime_adapter.activate.redacted()
            if runtime_adapter.activate
            else None,
            "release": runtime_adapter.release.redacted()
            if runtime_adapter.release
            else None,
            "batch_activate": runtime_adapter.batch_activate.redacted()
            if runtime_adapter.batch_activate
            else None,
            "batch_release": runtime_adapter.batch_release.redacted()
            if runtime_adapter.batch_release
            else None,
            "verify_effective": runtime_adapter.verify_effective.redacted()
            if runtime_adapter.verify_effective
            else None,
            "command_digests": [command.fingerprint for command in adapter_commands],
            "input_paths": list(runtime_adapter.input_paths),
            "input_hashes": adapter_input_hashes,
            "timeout_seconds": runtime_adapter.timeout_seconds,
            "context_contract": "dww-runtime-adapter-v1",
        },
        "static_only": verification.static_only,
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


def require_approved_plan(
    repo: GitRepo,
    *,
    cwd: Path,
    verification: VerificationConfig,
    message: str,
) -> str:
    """Require an exact approval and persist a field-level drift report on failure."""
    plan = approval_plan(repo, cwd=cwd, verification=verification)
    fingerprint = sha256_text(stable_json(plan))
    approvals = read_json(repo.local_dir / "approvals.json", {"accepted": {}})
    accepted = approvals.get("accepted", {})
    if fingerprint in accepted:
        return fingerprint

    comparisons: list[tuple[int, str, str, list[dict[str, Any]]]] = []
    for approved_fingerprint, record in accepted.items():
        approved_plan = record.get("plan") if isinstance(record, dict) else None
        if not isinstance(approved_plan, dict):
            continue
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
        "schema_version": 1,
        "current_fingerprint": fingerprint,
        "nearest_approved_fingerprint": nearest[2] if nearest else None,
        "nearest_approved_at": nearest[1] if nearest else None,
        "difference_count": nearest[0] if nearest else None,
        "differences": nearest[3] if nearest else [],
    }
    report_path = repo.local_dir / "approval-mismatches" / f"{fingerprint}.json"
    atomic_write_json(report_path, report)
    detail = (
        f"{nearest[0]} normalized field(s) differ from the nearest accepted plan"
        if nearest
        else "no accepted plan exists"
    )
    raise SoloAIError(
        f"{message} {detail}. Local report: {report_path}. "
        "Review `doctor` then run `approve --accept`."
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
    shared = _shared_inputs(repo, cwd, commands, verification, tool_cache)
    candidate_head = repo.head(cwd)
    candidate_tree = repo.tree(cwd=cwd)
    records: list[tuple[VerificationProfile, dict[str, Any], str]] = []
    for profile in profiles:
        inputs = _profile_inputs(
            profile,
            cwd=cwd,
            tracked=tracked,
            shared=shared,
            validation_environment=_profile_validation_environment(
                profile, frozen_environment
            ),
        )
        # 同一任务可在输入闭包未变时复用；跨任务复用仍需显式闭包和无外部状态。
        if force_task_scope and task_id:
            scope = f"task:{task_id}"
        elif profile.cross_task_reuse and profile.external_state == "none":
            scope = "cross-task"
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
    commands: list[CommandSpec],
    tool_cache: dict[tuple[Any, ...], dict[str, str | None]],
    validation_environment: dict[str, str],
) -> None:
    """HEAD未变不足以证明输入稳定；同时复核文件、配置、工具和环境。"""
    current = _profile_inputs(
        profile,
        cwd=cwd,
        tracked=_tracked(repo, cwd),
        shared=_shared_inputs(repo, cwd, commands, verification, tool_cache),
        validation_environment=_profile_validation_environment(
            profile, validation_environment
        ),
    )
    if any(inputs.get(key) != value for key, value in current.items()):
        raise SoloAIError(
            f"Validation inputs changed for profile {profile.profile_id}; "
            "no successful proof may certify this execution"
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
) -> dict[str, Any]:
    proof_path = repo.local_dir / "profile-proofs" / f"{fingerprint}.json"
    from .util import read_json

    check_inputs()
    existing = read_json(proof_path, {})
    _require_stored_proof_identity(existing, fingerprint=fingerprint, inputs=inputs)
    if existing.get("result") == "passed" and _logs_exist(existing):
        _require_expected_candidate_head(
            repo, cwd=cwd, expected_candidate_head=expected_candidate_head
        )
        existing["reused_at"] = utc_timestamp()
        atomic_write_json(proof_path, existing)
        return {**existing, "reused": True}
    if (
        existing.get("result") == "failed"
        and _logs_exist(existing)
        and _deterministic_failure(profile, existing)
    ):
        raise SoloAIError(
            f"Validation profile {profile.profile_id} already failed with the same complete deterministic inputs. Change the candidate or policy, or explicitly reclassify its external state before retrying."
        )
    run_id = new_id(f"profile-{profile.profile_id}")
    temp_dir = repo.local_dir / "logs" / "pending" / run_id
    runs: list[dict[str, Any]] = []
    with claim_validation_slot(profile.resource_class) as queue_claim:
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
            result = run_logged(
                command.argv,
                cwd=cwd,
                log_path=pending,
                timeout_seconds=profile.timeout_seconds,
                environment=environment,
                receipt_path=receipt_path,
                receipt_metadata={
                    "task_id": task_id,
                    "profile_id": profile.profile_id,
                    "profile_fingerprint": fingerprint,
                    "queue_ticket": queue_claim["id"],
                },
            )
            _require_expected_candidate_head(
                repo, cwd=cwd, expected_candidate_head=expected_candidate_head
            )
            check_inputs()
            log_path, log_digest = _content_address_log(repo, pending)
            runs.append(
                {
                    "command_digest": command.fingerprint,
                    "command": command.redacted(),
                    "exit_code": result.returncode,
                    "duration_seconds": round(result.duration_seconds, 3),
                    "timed_out": result.timed_out,
                    "process": result.process,
                    "receipt": str(receipt_path),
                    "log": str(log_path),
                    "log_sha256": log_digest,
                }
            )
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
) -> dict[str, Any]:
    from .util import read_json

    if full_scope not in {"integration", "complete"}:
        raise SoloAIError("full_scope must be integration or complete")
    if level != "full" and full_scope != "integration":
        raise SoloAIError("A complete validation scope requires level full")
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
    commands = [command for profile, _, _ in records for command in profile.commands]
    if existing.get("result") == "passed" and _logs_exist(existing):
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
                commands=commands,
                tool_cache=tool_cache,
                validation_environment=validation_environment,
            )
        existing["reused_at"] = utc_timestamp()
        atomic_write_json(proof_path, existing)
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
                commands=commands,
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
        )
        profile_proofs.append(
            {
                "profile_id": profile.profile_id,
                "fingerprint": profile_fingerprint,
                "reused": result["reused"],
            }
        )
        runs.extend(
            {**item, "profile_id": profile.profile_id, "reused": result["reused"]}
            for item in result["runs"]
        )
    if not records:
        log_path = repo.local_dir / "logs" / "content" / "static-only-placeholder.log"
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
            commands=commands,
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
    return {**proof, "reused": False}
