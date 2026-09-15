from __future__ import annotations

import json
import math
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .repo import GitRepo
from .routing import WORKFLOW_MARKERS
from .util import SoloAIError, redact_text, sha256_file, sha256_text, stable_json

CONFIG_SCHEMA = 2
VERIFICATION_SCHEMA = 3
DEFAULT_CLEANUP_OWNED_PATHS: tuple[str, ...] = ()
STRESS_VERIFICATION_FILENAME = "stress-verification.toml"


@dataclass(frozen=True)
class CommandSpec:
    argv: tuple[str, ...]

    def redacted(self) -> list[str]:
        return [redact_text(value) for value in self.argv]

    @property
    def fingerprint(self) -> str:
        return sha256_text(stable_json(list(self.argv)))


@dataclass(frozen=True)
class ReadinessSpec:
    kind: str
    target: str | None
    timeout_seconds: float


@dataclass(frozen=True)
class IntegrationSpec:
    mode: str
    batch_size: int
    candidate_capacity: int
    seal_policy: str
    candidate_validation: str
    tail_policy: str
    tail_quiet_seconds: float
    worktree_mode: str = "dedicated"


@dataclass(frozen=True)
class RuntimeAdapterSpec:
    activate: CommandSpec | None
    release: CommandSpec | None
    batch_activate: CommandSpec | None
    batch_release: CommandSpec | None
    verify_effective: CommandSpec | None
    input_paths: tuple[str, ...]
    timeout_seconds: float


@dataclass(frozen=True)
class RepoConfig:
    schema_version: int
    mode: str
    slots: int
    branch_prefix: str
    worktree_directory: str
    port_base: int
    remote_policy: str
    sensitive_allowlist: tuple[str, ...]
    agents_file_created: bool
    secret_scanner: CommandSpec | None
    warm_commands: tuple[CommandSpec, ...]
    dev_start: CommandSpec | None
    readiness: ReadinessSpec | None
    cleanup_owned_paths: tuple[str, ...]
    integration: IntegrationSpec
    runtime_adapter: RuntimeAdapterSpec


@dataclass(frozen=True)
class VerificationProfile:
    profile_id: str
    paths: tuple[str, ...]
    commands: tuple[CommandSpec, ...]
    cross_task_reuse: bool
    external_state: str
    input_paths: tuple[str, ...]
    environment: tuple[str, ...]
    input_closure: str
    timeout_seconds: float
    resource_class: str
    level: str
    frozen_base: bool
    full_scope: str | None


@dataclass(frozen=True)
class VerificationConfig:
    schema_version: int
    static_only: bool
    profiles: tuple[VerificationProfile, ...]

    def normalized(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "static_only": self.static_only,
            "profiles": [
                {
                    "id": profile.profile_id,
                    "paths": list(profile.paths),
                    "commands": [list(command.argv) for command in profile.commands],
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
                for profile in self.profiles
            ],
        }

    @property
    def commands(self) -> tuple[CommandSpec, ...]:
        ordered: list[CommandSpec] = []
        seen: set[tuple[str, ...]] = set()
        for profile in self.profiles:
            for command in profile.commands:
                if command.argv not in seen:
                    ordered.append(command)
                    seen.add(command.argv)
        return tuple(ordered)


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SoloAIError(
            f"Repository is not initialized for develop-with-worktrees: missing {path}"
        )
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SoloAIError(f"Invalid TOML: {path}: {exc}") from exc


def _read_verification_file(path: Path) -> tuple[Path, str]:
    """一次读取经人工审阅的验证策略，避免检查与复制使用不同版本。"""

    try:
        source = path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise SoloAIError(f"Cannot read verification policy: {path}: {exc}") from exc
    if not source.is_file():
        raise SoloAIError(f"Verification policy must be a regular file: {source}")
    try:
        text = source.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SoloAIError(f"Cannot read verification policy: {source}: {exc}") from exc
    return source, text


def _command(raw: Any, *, field: str) -> CommandSpec:
    if (
        not isinstance(raw, list)
        or not raw
        or not all(isinstance(item, str) for item in raw)
    ):
        raise SoloAIError(f"{field} must be a non-empty argv array of strings")
    if any(not item for item in raw):
        raise SoloAIError(f"{field} cannot contain an empty argument")
    return CommandSpec(tuple(raw))


def _integer(raw: Any, *, field: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise SoloAIError(f"{field} must be an integer")
    return raw


def _boolean(raw: Any, *, field: str) -> bool:
    if not isinstance(raw, bool):
        raise SoloAIError(f"{field} must be a boolean")
    return raw


def _number(raw: Any, *, field: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise SoloAIError(f"{field} must be a finite number")
    value = float(raw)
    if not math.isfinite(value):
        raise SoloAIError(f"{field} must be a finite number")
    return value


def _string(raw: Any, *, field: str, non_empty: bool = False) -> str:
    if not isinstance(raw, str):
        raise SoloAIError(f"{field} must be a string")
    if non_empty and not raw:
        raise SoloAIError(f"{field} must be a non-empty string")
    return raw


def _strings(raw: Any, *, field: str, allow_empty: bool) -> tuple[str, ...]:
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise SoloAIError(f"{field} must be an array of strings")
    if not allow_empty and not raw:
        raise SoloAIError(f"{field} must not be empty")
    if any(not item for item in raw):
        raise SoloAIError(f"{field} cannot contain an empty string")
    return tuple(raw)


def _sensitive_allowlist(raw: Any) -> tuple[str, ...]:
    values = _strings(raw, field="sensitive_allowlist", allow_empty=True)
    normalized: list[str] = []
    for value in values:
        path = value.replace("\\", "/")
        candidate = PurePosixPath(path)
        if (
            path.startswith("/")
            or (len(path) >= 3 and path[0].isalpha() and path[1:3] == ":/")
            or candidate == PurePosixPath(".")
            or ".." in candidate.parts
            or any(character in path for character in "*?[")
        ):
            raise SoloAIError(
                "sensitive_allowlist must contain exact repository-relative paths, not globs"
            )
        normalized.append(path)
    return tuple(normalized)


def _repository_patterns(raw: Any, *, field: str) -> tuple[str, ...]:
    values = _strings(raw, field=field, allow_empty=True)
    normalized: list[str] = []
    for value in values:
        path = value.replace("\\", "/")
        candidate = PurePosixPath(path)
        if (
            path.startswith("/")
            or (len(path) >= 3 and path[0].isalpha() and path[1:3] == ":/")
            or candidate == PurePosixPath(".")
            or ".." in candidate.parts
        ):
            raise SoloAIError(
                f"{field} must contain repository-relative paths or patterns"
            )
        normalized.append(path)
    return tuple(normalized)


def _cleanup_paths(
    raw: Any, *, field: str, default: tuple[str, ...], allow_patterns: bool
) -> tuple[str, ...]:
    values = default if raw is None else _strings(raw, field=field, allow_empty=True)
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        path = value.replace("\\", "/")
        candidate = PurePosixPath(path)
        if (
            path.startswith("/")
            or (len(path) >= 3 and path[0].isalpha() and path[1:3] == ":/")
            or candidate == PurePosixPath(".")
            or ".." in candidate.parts
            or len(candidate.parts) != 1
            or (not allow_patterns and any(character in path for character in "*?["))
        ):
            raise SoloAIError(
                f"{field} must contain only top-level repository-relative {'patterns' if allow_patterns else 'paths'}"
            )
        identity = path.casefold()
        if identity in seen:
            raise SoloAIError(f"{field} contains duplicate paths for this filesystem")
        seen.add(identity)
        normalized.append(path)
    return tuple(normalized)


def _commands(raw: Any, *, field: str) -> tuple[CommandSpec, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise SoloAIError(f"{field} must be an array of argv arrays")
    return tuple(
        _command(item, field=f"{field}[{index}]") for index, item in enumerate(raw)
    )


def _worktree_directory(repo: GitRepo, raw: Any) -> str:
    value = _string(raw, field="worktree_directory", non_empty=True)
    candidate = Path(value)
    if (
        not value
        or candidate == Path(".")
        or candidate.is_absolute()
        or candidate.anchor
        or ".." in candidate.parts
    ):
        raise SoloAIError(
            "worktree_directory must be a non-empty repository-relative child path"
        )
    target = (repo.root / candidate).resolve()
    try:
        target.relative_to(repo.root.resolve())
    except ValueError as exc:
        raise SoloAIError(
            "worktree_directory must resolve inside the repository root"
        ) from exc
    return str(candidate)


def _branch_prefix(repo: GitRepo, raw: Any) -> str:
    value = _string(raw, field="branch_prefix", non_empty=True)
    probe = repo.git(
        ["check-ref-format", "--branch", f"{value}solo-ai-policy-check"],
        check=False,
    )
    if probe.returncode != 0:
        raise SoloAIError(
            "branch_prefix must form a valid Git branch when combined with a task suffix"
        )
    return value


def load_repo_config(repo: GitRepo, *, cwd: Path | None = None) -> RepoConfig:
    data = _read_toml((cwd or repo.policy_path()) / ".solo-ai" / "config.toml")
    if _integer(data.get("schema_version", 0), field="schema_version") != CONFIG_SCHEMA:
        raise SoloAIError(
            f"Unsupported .solo-ai/config.toml schema; expected {CONFIG_SCHEMA}"
        )
    lifecycle = data.get("lifecycle", {})
    if not isinstance(lifecycle, dict):
        raise SoloAIError("lifecycle must be a TOML table")
    slots = _integer(data.get("slots", 3), field="slots")
    if not 1 <= slots <= 32:
        raise SoloAIError("slots must be between 1 and 32")
    mode = _string(data.get("mode", "managed"), field="mode")
    if mode != "managed":
        raise SoloAIError('Only mode = "managed" is valid in an adopted repository')
    port_base = _integer(data.get("port_base", 20000), field="port_base")
    if not 1024 <= port_base <= 62336:
        raise SoloAIError("port_base must leave room for all 32 100-port slot blocks")
    remote_policy = _string(
        data.get("remote_policy", "local-only"), field="remote_policy"
    )
    if remote_policy != "local-only":
        raise SoloAIError('Version 1 supports only remote_policy = "local-only"')
    readiness: ReadinessSpec | None = None
    dev_start: CommandSpec | None = None
    cleanup = data.get("cleanup", {})
    if not isinstance(cleanup, dict):
        raise SoloAIError("cleanup must be a TOML table")
    integration_declared = "integration" in data
    integration_raw = data.get("integration", {})
    if not isinstance(integration_raw, dict):
        raise SoloAIError("integration must be a TOML table")
    integration_mode = _string(
        integration_raw.get("mode", "direct"), field="integration.mode"
    )
    if integration_mode not in {"direct", "batched"}:
        raise SoloAIError('integration.mode must be "direct" or "batched"')
    worktree_mode = _string(
        integration_raw.get("worktree_mode", "dedicated"),
        field="integration.worktree_mode",
    )
    if worktree_mode not in {"dedicated", "reusable"}:
        raise SoloAIError('integration.worktree_mode must be "dedicated" or "reusable"')
    if integration_mode != "batched" and worktree_mode != "dedicated":
        raise SoloAIError('Reusable integration worktrees require mode = "batched"')
    batch_size = _integer(
        integration_raw.get("batch_size", 5), field="integration.batch_size"
    )
    if not 1 <= batch_size <= 5:
        raise SoloAIError("integration.batch_size must be between 1 and 5")
    candidate_capacity = _integer(
        integration_raw.get("candidate_capacity", 10),
        field="integration.candidate_capacity",
    )
    if not batch_size <= candidate_capacity <= 100:
        raise SoloAIError(
            "integration.candidate_capacity must be between batch_size and 100"
        )
    seal_policy = _string(
        integration_raw.get("seal_policy", "explicit"),
        field="integration.seal_policy",
    )
    if seal_policy not in {"explicit", "auto_full"}:
        raise SoloAIError('integration.seal_policy must be "explicit" or "auto_full"')
    if integration_mode == "direct" and seal_policy != "explicit":
        raise SoloAIError(
            'integration.seal_policy = "auto_full" requires mode = "batched"'
        )
    candidate_validation = _string(
        # 未声明该字段的仓库来自候选发布仍要求 Ready 的旧契约，不能在升级时
        # 静默放宽；新模板会显式写入 batch。
        integration_raw.get("candidate_validation", "ready"),
        field="integration.candidate_validation",
    )
    if candidate_validation not in {"ready", "batch"}:
        raise SoloAIError('integration.candidate_validation must be "ready" or "batch"')
    if integration_mode == "direct" and candidate_validation != "ready":
        raise SoloAIError(
            'integration.candidate_validation = "batch" requires mode = "batched"'
        )
    tail_policy = _string(
        integration_raw.get("tail_policy", "explicit"),
        field="integration.tail_policy",
    )
    if tail_policy not in {"explicit", "quiet_or_explicit"}:
        raise SoloAIError(
            'integration.tail_policy must be "explicit" or "quiet_or_explicit"'
        )
    tail_quiet_seconds = _number(
        integration_raw.get("tail_quiet_seconds", 90),
        field="integration.tail_quiet_seconds",
    )
    if not 1 <= tail_quiet_seconds <= 3600:
        raise SoloAIError("integration.tail_quiet_seconds must be between 1 and 3600")
    if integration_mode == "direct" and tail_policy != "explicit":
        raise SoloAIError(
            'integration.tail_policy = "quiet_or_explicit" requires mode = "batched"'
        )
    # Repositories adopted before candidate-first integration often have no
    # integration table. Treat that absence as the old direct policy; only a
    # newly rendered or explicitly upgraded table opts into the new default.
    if not integration_declared:
        integration_mode = "direct"
        seal_policy = "explicit"
        candidate_validation = "ready"
        tail_policy = "explicit"
    runtime_adapter_raw = data.get("runtime_adapter", {})
    if not isinstance(runtime_adapter_raw, dict):
        raise SoloAIError("runtime_adapter must be a TOML table")
    runtime_activate = (
        _command(runtime_adapter_raw["activate"], field="runtime_adapter.activate")
        if "activate" in runtime_adapter_raw
        else None
    )
    runtime_release = (
        _command(runtime_adapter_raw["release"], field="runtime_adapter.release")
        if "release" in runtime_adapter_raw
        else None
    )
    runtime_batch_activate = (
        _command(
            runtime_adapter_raw["batch_activate"],
            field="runtime_adapter.batch_activate",
        )
        if "batch_activate" in runtime_adapter_raw
        else None
    )
    runtime_batch_release = (
        _command(
            runtime_adapter_raw["batch_release"],
            field="runtime_adapter.batch_release",
        )
        if "batch_release" in runtime_adapter_raw
        else None
    )
    runtime_verify_effective = (
        _command(
            runtime_adapter_raw["verify_effective"],
            field="runtime_adapter.verify_effective",
        )
        if "verify_effective" in runtime_adapter_raw
        else None
    )
    runtime_input_paths = _repository_patterns(
        runtime_adapter_raw.get("input_paths", []),
        field="runtime_adapter.input_paths",
    )
    if (
        runtime_activate
        or runtime_release
        or runtime_batch_activate
        or runtime_batch_release
        or runtime_verify_effective
    ) and not runtime_input_paths:
        raise SoloAIError(
            "runtime_adapter.input_paths is required when Adapter commands are configured"
        )
    if bool(runtime_batch_activate) != bool(runtime_batch_release):
        raise SoloAIError(
            "runtime_adapter.batch_activate and batch_release must be configured together"
        )
    if runtime_batch_activate and port_base + 3299 > 65535:
        raise SoloAIError(
            "port_base must leave room for the dedicated batch Adapter port block"
        )
    runtime_timeout_seconds = _number(
        runtime_adapter_raw.get("timeout_seconds", 300),
        field="runtime_adapter.timeout_seconds",
    )
    if not 1 <= runtime_timeout_seconds <= 3600:
        raise SoloAIError("runtime_adapter.timeout_seconds must be between 1 and 3600")
    readiness_raw = lifecycle.get("readiness")
    if "dev_start" in lifecycle:
        dev_start = _command(lifecycle["dev_start"], field="lifecycle.dev_start")
        if not isinstance(readiness_raw, dict):
            raise SoloAIError(
                "lifecycle.readiness is required when lifecycle.dev_start is configured"
            )
        kind = _string(readiness_raw.get("kind", ""), field="lifecycle.readiness.kind")
        if kind not in {"tcp", "http"}:
            raise SoloAIError("lifecycle.readiness.kind must be tcp or http")
        target = _string(
            readiness_raw.get("target", ""),
            field="lifecycle.readiness.target",
            non_empty=True,
        )
        readiness = ReadinessSpec(
            kind=kind,
            target=target,
            timeout_seconds=_number(
                readiness_raw.get("timeout_seconds", 30),
                field="lifecycle.readiness.timeout_seconds",
            ),
        )
        if readiness.timeout_seconds <= 0:
            raise SoloAIError("lifecycle.readiness.timeout_seconds must be positive")
    return RepoConfig(
        schema_version=CONFIG_SCHEMA,
        mode=mode,
        slots=slots,
        branch_prefix=_branch_prefix(repo, data.get("branch_prefix", "codex/")),
        worktree_directory=_worktree_directory(
            repo, data.get("worktree_directory", ".worktrees")
        ),
        port_base=port_base,
        remote_policy=remote_policy,
        sensitive_allowlist=_sensitive_allowlist(data.get("sensitive_allowlist", [])),
        agents_file_created=_boolean(
            data.get("agents_file_created", False), field="agents_file_created"
        ),
        secret_scanner=_command(data["secret_scanner"], field="secret_scanner")
        if "secret_scanner" in data
        else None,
        warm_commands=_commands(data.get("warm"), field="warm"),
        dev_start=dev_start,
        readiness=readiness,
        cleanup_owned_paths=_cleanup_paths(
            cleanup.get("owned_paths"),
            field="cleanup.owned_paths",
            default=DEFAULT_CLEANUP_OWNED_PATHS,
            allow_patterns=False,
        ),
        integration=IntegrationSpec(
            mode=integration_mode,
            batch_size=batch_size,
            candidate_capacity=candidate_capacity,
            seal_policy=seal_policy,
            candidate_validation=candidate_validation,
            tail_policy=tail_policy,
            tail_quiet_seconds=tail_quiet_seconds,
            worktree_mode=worktree_mode,
        ),
        runtime_adapter=RuntimeAdapterSpec(
            activate=runtime_activate,
            release=runtime_release,
            batch_activate=runtime_batch_activate,
            batch_release=runtime_batch_release,
            verify_effective=runtime_verify_effective,
            input_paths=runtime_input_paths,
            timeout_seconds=runtime_timeout_seconds,
        ),
    )


def _parse_verification_config(
    data: dict[str, Any],
    *,
    source: Path,
    stress_only: bool = False,
) -> VerificationConfig:
    schema_version = _integer(data.get("schema_version", 0), field="schema_version")
    if schema_version != VERIFICATION_SCHEMA:
        raise SoloAIError(
            f"Unsupported {source} schema; expected {VERIFICATION_SCHEMA}"
        )
    raw_profiles = data.get("profiles", [])
    if not isinstance(raw_profiles, list):
        raise SoloAIError("profiles must be an array of TOML tables")
    profiles: list[VerificationProfile] = []
    for index, raw in enumerate(raw_profiles):
        if not isinstance(raw, dict):
            raise SoloAIError(f"profiles[{index}] must be a TOML table")
        profile_id = _string(raw.get("id", ""), field=f"profiles[{index}].id")
        paths = _strings(
            raw.get("paths", ["**"]),
            field=f"profiles[{index}].paths",
            allow_empty=False,
        )
        commands = _commands(raw.get("commands"), field=f"profiles[{index}].commands")
        reuse = _boolean(
            raw.get("cross_task_reuse", False),
            field=f"profiles[{index}].cross_task_reuse",
        )
        external_state = _string(
            raw.get("external_state", "unknown"),
            field=f"profiles[{index}].external_state",
            non_empty=True,
        )
        if not profile_id:
            raise SoloAIError("Every verification profile needs a non-empty id")
        if not commands:
            raise SoloAIError(f"Verification profile {profile_id!r} has no commands")
        if reuse and external_state != "none":
            raise SoloAIError(
                f'Profile {profile_id!r} enables cross_task_reuse but external_state is not "none"'
            )
        input_closure = _string(
            raw.get("input_closure", "declared"),
            field=f"profiles[{index}].input_closure",
            non_empty=True,
        )
        if input_closure not in {"declared", "complete"}:
            raise SoloAIError(
                f"Profile {profile_id!r} input_closure must be declared or complete"
            )
        if reuse and input_closure != "complete":
            raise SoloAIError(
                f"Profile {profile_id!r} enables cross_task_reuse but input_closure is not complete"
            )
        timeout_seconds = _number(
            raw.get("timeout_seconds", 2700),
            field=f"profiles[{index}].timeout_seconds",
        )
        if timeout_seconds <= 0:
            raise SoloAIError(
                f"Profile {profile_id!r} timeout_seconds must be positive"
            )
        resource_class = _string(
            raw.get("resource_class", "normal"),
            field=f"profiles[{index}].resource_class",
            non_empty=True,
        )
        if resource_class not in {"normal", "heavy"}:
            raise SoloAIError(
                f"Profile {profile_id!r} resource_class must be normal or heavy"
            )
        level = _string(
            raw.get("level", "ready"),
            field=f"profiles[{index}].level",
            non_empty=True,
        )
        if level not in {"development", "ready", "full", "stress"}:
            raise SoloAIError(
                f"Profile {profile_id!r} level must be development, ready, full, or stress"
            )
        if stress_only and level != "stress":
            raise SoloAIError(
                f"Profile {profile_id!r} in {source.name} must run at level stress"
            )
        if resource_class == "heavy" and level not in {"full", "stress"}:
            raise SoloAIError(
                f"Profile {profile_id!r} is heavy and must run at level full or stress"
            )
        full_scope: str | None = None
        if level == "full":
            full_scope = _string(
                raw.get("full_scope", "integration"),
                field=f"profiles[{index}].full_scope",
                non_empty=True,
            )
            if full_scope not in {"integration", "complete"}:
                raise SoloAIError(
                    f"Profile {profile_id!r} full_scope must be integration or complete"
                )
        elif "full_scope" in raw:
            raise SoloAIError(
                f"Profile {profile_id!r} may declare full_scope only at level full"
            )
        frozen_base = _boolean(
            raw.get("frozen_base", False), field=f"profiles[{index}].frozen_base"
        )
        if frozen_base and level not in {"ready", "full"}:
            raise SoloAIError(
                f"Profile {profile_id!r} may declare frozen_base only at level ready or full"
            )
        profiles.append(
            VerificationProfile(
                profile_id=profile_id,
                paths=paths,
                commands=commands,
                cross_task_reuse=reuse,
                external_state=external_state,
                input_paths=_strings(
                    raw.get("input_paths", list(paths)),
                    field=f"profiles[{index}].input_paths",
                    allow_empty=False,
                ),
                environment=_strings(
                    raw.get("environment", []),
                    field=f"profiles[{index}].environment",
                    allow_empty=True,
                ),
                input_closure=input_closure,
                timeout_seconds=timeout_seconds,
                resource_class=resource_class,
                level=level,
                frozen_base=frozen_base,
                full_scope=full_scope,
            )
        )
    static_only = _boolean(data.get("static_only", False), field="static_only")
    if not profiles and not static_only:
        raise SoloAIError(
            "No validation commands configured; explicitly enable static_only or add a profile"
        )
    if profiles and static_only:
        raise SoloAIError(
            "static_only cannot be combined with verification profiles; map every changed path explicitly"
        )
    return VerificationConfig(schema_version, static_only, tuple(profiles))


def load_verification_config(
    repo: GitRepo, *, cwd: Path | None = None
) -> VerificationConfig:
    config_directory = (cwd or repo.policy_path()) / ".solo-ai"
    primary_path = config_directory / "verification.toml"
    primary = _parse_verification_config(_read_toml(primary_path), source=primary_path)
    stress_path = config_directory / STRESS_VERIFICATION_FILENAME
    if not stress_path.exists():
        return primary
    stress = _parse_verification_config(
        _read_toml(stress_path), source=stress_path, stress_only=True
    )
    if stress.static_only:
        raise SoloAIError(
            f"{stress_path.name} must declare stress profiles and cannot enable static_only"
        )
    if primary.static_only:
        raise SoloAIError(
            "static_only cannot be combined with stress verification profiles"
        )
    profiles = primary.profiles + stress.profiles
    profile_ids = [profile.profile_id for profile in profiles]
    if len(profile_ids) != len(set(profile_ids)):
        raise SoloAIError(
            "Verification profile ids must be unique across configuration files"
        )
    return VerificationConfig(primary.schema_version, False, profiles)


def verification_config_from_text(text: str, *, source: Path) -> VerificationConfig:
    """按与已跟踪策略相同的 schema 解析一次 UTF-8 TOML 文本。"""

    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SoloAIError(f"Invalid verification TOML: {source}: {exc}") from exc
    if not isinstance(data, dict):
        raise SoloAIError(f"Invalid verification TOML: {source}: root must be a table")
    return _parse_verification_config(data, source=source)


def read_verification_config_file(path: Path) -> tuple[Path, str, VerificationConfig]:
    """读取单个可交付的 schema-3 策略文件，供首次采用时原样提交。"""

    source, text = _read_verification_file(path)
    return source, text, verification_config_from_text(text, source=source)


def _package_json_commands(root: Path) -> list[CommandSpec]:
    path = root / "package.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    scripts = data.get("scripts") or {}
    manager = "npm"
    if (root / "pnpm-lock.yaml").exists():
        manager = "pnpm"
    elif (root / "yarn.lock").exists():
        manager = "yarn"
    return [
        CommandSpec((manager, "run", name))
        for name in ("lint", "typecheck", "check", "test", "build")
        if name in scripts
    ]


def discover_validation_commands(root: Path) -> list[CommandSpec]:
    commands = _package_json_commands(root)
    if (root / "pyproject.toml").exists():
        pyproject = (root / "pyproject.toml").read_text(
            encoding="utf-8", errors="replace"
        )
        if (root / "tests").exists() or "pytest" in pyproject:
            commands.append(CommandSpec(("uv", "run", "pytest")))
        elif any(root.glob("test*.py")):
            commands.append(
                CommandSpec(("uv", "run", "python", "-m", "unittest", "discover"))
            )
    if (root / "Cargo.toml").exists():
        commands.extend(
            (
                CommandSpec(("cargo", "test")),
                CommandSpec(
                    (
                        "cargo",
                        "clippy",
                        "--all-targets",
                        "--all-features",
                        "--",
                        "-D",
                        "warnings",
                    )
                ),
            )
        )
    if (root / "go.mod").exists():
        commands.append(CommandSpec(("go", "test", "./...")))
    for candidate in ("scripts/verify.sh", "scripts/verify.ps1", "scripts/verify.py"):
        path = root / candidate
        if not path.exists():
            continue
        if path.suffix == ".sh":
            commands.insert(0, CommandSpec(("sh", candidate)))
        elif path.suffix == ".ps1":
            interpreter = (
                shutil.which("pwsh")
                or shutil.which("powershell.exe")
                or shutil.which("powershell")
            )
            if interpreter:
                commands.insert(0, CommandSpec((interpreter, "-File", candidate)))
        else:
            commands.insert(0, CommandSpec(("uv", "run", candidate)))
        break
    unique: list[CommandSpec] = []
    seen: set[tuple[str, ...]] = set()
    for command in commands:
        if command.argv not in seen:
            unique.append(command)
            seen.add(command.argv)
    return unique


def workflow_marker_fingerprint(root: Path) -> str:
    records: list[dict[str, str]] = []
    for relative in WORKFLOW_MARKERS:
        path = root / relative
        if path.is_file():
            records.append({"path": relative, "hash": sha256_file(path)})
        elif path.is_dir():
            records.append({"path": relative, "kind": "directory"})
    return sha256_text(stable_json(records))


def quote_toml(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_array(values: tuple[str, ...] | list[str]) -> str:
    return "[" + ", ".join(quote_toml(value) for value in values) + "]"


def render_repo_config(*, slots: int = 3, agents_file_created: bool = False) -> str:
    return f"""schema_version = {CONFIG_SCHEMA}
mode = "managed"
slots = {slots}
branch_prefix = "codex/"
worktree_directory = ".worktrees" # repository-relative only
port_base = 20000
remote_policy = "local-only"
sensitive_allowlist = []
agents_file_created = {"true" if agents_file_created else "false"}

# Optional repository-declared scanner, for example: ["gitleaks", "protect", "--staged"]
# secret_scanner = []
# Optional serial preparation commands for an idle slot. No environment is copied.
# warm = [["uv", "sync"]]

# Only exact top-level paths explicitly declared here may be removed by prune-slot.
# An empty list means no dependencies or caches are ever removed automatically.
cleanup = {{ owned_paths = [] }}
# 默认每满 3 个候选自动封批。候选保存只固定源码；项目检查在组合后的
# 批次中按影响运行。收尾候选由宿主明确封存，不依赖静默计时。
integration = {{ mode = "batched", batch_size = 3, candidate_capacity = 10, seal_policy = "auto_full", candidate_validation = "batch", tail_policy = "explicit", tail_quiet_seconds = 30, worktree_mode = "reusable" }}

# 可选项目运行时 Adapter；DWW 只传递上下文文件，不解释端口、数据库或浏览器语义。
# [runtime_adapter]
# activate = ["uv", "run", "scripts/dww-runtime-adapter.py", "activate"]
# release = ["uv", "run", "scripts/dww-runtime-adapter.py", "release"]
# batch_activate = ["uv", "run", "scripts/dww-runtime-adapter.py", "batch-activate"]
# batch_release = ["uv", "run", "scripts/dww-runtime-adapter.py", "batch-release"]
# verify_effective = ["uv", "run", "scripts/dww-runtime-adapter.py", "verify-effective"]
# input_paths = ["scripts/dww-runtime-adapter.py", "deploy/**"]
# timeout_seconds = 300

[lifecycle]
# dev_start = ["npm", "run", "dev", "--", "--port", "{{port}}"]
# [lifecycle.readiness]
# kind = "http" # tcp or http
# target = "http://127.0.0.1:{{port}}/health"
# timeout_seconds = 30
"""


def render_verification_config(
    commands: list[CommandSpec], *, static_only: bool, discovery_fallback: bool = False
) -> str:
    lines = [
        f"schema_version = {VERIFICATION_SCHEMA}",
        f"static_only = {'true' if static_only else 'false'}",
        "",
    ]
    if commands:
        lines.extend(
            (
                "[[profiles]]",
                'id = "default"',
                'paths = ["**"]',
                "cross_task_reuse = false",
                'external_state = "unknown"',
                'input_paths = ["**"]',
                "environment = []",
                'input_closure = "declared"',
                "timeout_seconds = 2700",
                'resource_class = "normal"',
                f"level = {quote_toml('full' if discovery_fallback else 'ready')}",
                *(('full_scope = "integration"',) if discovery_fallback else ()),
                "commands = [",
            )
        )
        lines.extend(f"  {_toml_array(list(command.argv))}," for command in commands)
        lines.extend(("]", ""))
    return "\n".join(lines)


MANAGED_START = "<!-- develop-with-worktrees:managed:start -->"
MANAGED_END = "<!-- develop-with-worktrees:managed:end -->"


def _pre_simplification_managed_block() -> str:
    """0.5.0-beta.2 早期生成的完整托管块；只用于无歧义升级。"""

    return f"""{MANAGED_START}
## Isolated coding tasks

For every task that may modify repository files, use the installed `develop-with-worktrees` skill before editing. Run `start`, work only in the returned worktree, and stage an exact reviewed path list with `commit`. Run `ready` when useful development evidence or a legacy task requires it, then `finish`; new default batched tasks may finish directly from `active` after the exact commit. Read-only analysis does not claim a slot. Do not bypass a failed gate. The DWW lifecycle is local-only and must not fetch, pull, push, create PRs, rebase, squash, amend, or rewrite history. After a successful Finish, an explicit user request may be fulfilled with an ordinary non-force push of the current branch from the clean base worktree; that publishing step is separate from DWW.
Before proposing or choosing an implementation, do not overdesign. Start from current evidence and the requested outcome; choose the simplest design that can meet the acceptance criteria, preserves existing user work and contracts, and remains easy to understand and maintain. Add a persistent layer, abstraction, workflow, or human gate only when an observed requirement cannot be met by the existing mechanism; state that reason and its verification. Do not expand product scope merely because a more general system could be built.
Start writes the known task purpose, implementation target, scope, acceptance criteria, and baseline into one local task anchor; keep it current and reread it after continuation or context loss. When the user has explicitly confirmed a complete plan or asked to set it as the objective, create one DWW root anchor before the first child Start with the full plan file, its source, and a stable request id. The root is the single durable objective: it keeps the complete final plan, full prior versions of plan-changing amendments, explicit user amendments, progress, and the checked overall result; children bind it but do not duplicate it. Anchors, plan inputs, historical versions, and exact cross-repository closure state have no DWW content-size quota. A legacy guide root first upgraded by a complete plan amendment keeps its pre-structured text as a separate legacy snapshot, while the new complete plan becomes version 1. When a structured root is supplied at Start or bound to an active task, the host returns the complete root body once and records that read internally; that record is not proof of understanding and does not require a user receipt. On continuation, model/context recovery, a root-plan version change, or candidate repair, the host uses one internal root-context refresh that returns the complete current body and records its version without copying a SHA or version. Before Commit, actual Ready, or Finish candidate publication, a bound structured root must have that current read record; recover by refreshing and retrying without losing work. This is not a gate on every edit. A structured root closes only after every child is terminal and root-anchor accept records accepted or cancelled evidence for its current plan version. A child in another repository uses the same root only through an explicit external root-anchor file, never a duplicated root. DWW verifies and records that exact non-linked root locator and its child-state locator, but it never searches repositories or becomes a scope id, candidate group, DAG, scheduler, or batch boundary. A configured project Adapter may establish project runtime identity only after the exact isolated task exists and before Start returns it as active. New repositories publish exact source candidates, release project resources through the same Adapter, then release the task worktree. Each configured full batch freezes automatically; an exact smaller tail freezes only after the host explicitly ends the round or requests immediate integration. Host heartbeat may wake batch reconcile but cannot choose candidates; UI task counts, raw worktree counts, Hook delivery, and session end never prove completion. There is no candidate-age or quiet-period auto-seal. Use the host native task/subagent system for task orchestration; legacy dww orchestrate state is drain-only. Candidate publication is not delivery; only integration into the current base is delivery. Explicit legacy direct policy remains upgrade compatibility only.
The host remains responsible for the whole requested delivery, not only for one candidate or one task transition. After `Finish`, follow the applicable full batch or explicit tail through integration, inspect a recorded failure, and continue a deterministic repair or request a user decision only when the stated boundaries leave materially different outcomes.
{MANAGED_END}
"""


def _pre_root_output_managed_block() -> str:
    """根方案终端输出修复前的托管块；仅用于精确升级。"""

    return f"""{MANAGED_START}
## Isolated coding tasks

For every task that may modify repository files, use the installed `develop-with-worktrees` skill before editing. Run `start`, work only in its returned worktree, review exact paths before `commit`, then `finish`; do not bypass failed gates. DWW is local-only: do not fetch, pull, push, rebase, squash, amend, or rewrite history through it.

Keep one task anchor per task. `Start` records the known purpose, scope, acceptance criteria, baseline, and progress; update it only when that execution contract or progress materially changes. When the user has confirmed a complete plan, create one root anchor before its child tasks: the root keeps the complete plan, full plan-changing history, amendments, and overall result without a content-size limit, while children keep only their execution slice.

Normal `Start` or `bind-root` returns the complete root context once without a separate acknowledgement step. Refresh it after continuation, model/context recovery, a root-plan change, or candidate repair; the current root must have been refreshed before Commit, Ready, or Finish, but this is not a gate on every edit. Close a structured root only after all children are terminal and accepted or cancelled evidence is recorded.

An exact full batch freezes automatically. A smaller tail freezes only on an explicit `round-complete`, `user`, `deploy`, or `dependency` cause with one short reason; heartbeat, idle time, and task counts never seal a batch. Candidate publication is not delivery: after `Finish`, follow integration, inspect failures, and repair deterministically within the agreed scope.
{MANAGED_END}
"""


def managed_block() -> str:
    """当前 AGENTS.md 托管块：只保留执行时必须遵守的边界。"""

    return f"""{MANAGED_START}
## Isolated coding tasks

For every task that may modify repository files, use the installed `develop-with-worktrees` skill before editing. Run `start`, work only in its returned worktree, review exact paths before `commit`, then `finish`; do not bypass failed gates. DWW is local-only: do not fetch, pull, push, rebase, squash, amend, or rewrite history through it.

Keep one task anchor per task. `Start` records the known purpose, scope, acceptance criteria, baseline, and progress; update it only when that execution contract or progress materially changes. When the user has confirmed a complete plan, create one root anchor before its child tasks: the root keeps the complete plan, full plan-changing history, amendments, and overall result without a content-size limit, while children keep only their execution slice.

Normal `Start` or `bind-root` prints the complete root context once without a separate acknowledgement step. After continuation, model/context recovery, a root-plan change, or candidate repair, use one `anchor refresh-root` operation to return the current task anchor and complete root context together; it records the current version without copying SHA or version parameters. This refresh is required before Commit, Ready, or Finish when the recorded root is stale, not on every edit. Close a structured root only after all children are terminal and accepted or cancelled evidence is recorded.

An exact full batch freezes automatically. A smaller tail freezes only on an explicit `round-complete`, `user`, `deploy`, or `dependency` cause with one short reason; heartbeat, idle time, and task counts never seal a batch. Candidate publication is not delivery: after `Finish`, follow integration, inspect failures, and repair deterministically within the agreed scope.
{MANAGED_END}
"""


def _legacy_managed_block() -> str:
    """0.5.0-beta.1 已发布的完整托管块，只用于无歧义迁移。"""

    return f"""{MANAGED_START}
## Isolated coding tasks

For every task that may modify repository files, use the installed `develop-with-worktrees` skill before editing. Run `start`, work only in the returned worktree, and stage an exact reviewed path list with `commit`. Run `ready` when useful development evidence or a legacy task requires it, then `finish`; new default batched tasks may finish directly from `active` after the exact commit. Read-only analysis does not claim a slot. Do not bypass a failed gate. The DWW lifecycle is local-only and must not fetch, pull, push, create PRs, rebase, squash, amend, or rewrite history. After a successful Finish, an explicit user request may be fulfilled with an ordinary non-force push of the current branch from the clean base worktree; that publishing step is separate from DWW.
`Start` creates the local task anchor; keep it current and reread it after continuation or context loss. When the user has explicitly confirmed a complete plan or asked to set it as the objective, create one DWW `root-anchor` before the first child Start with the full plan file, its source, and a stable request id. The root is the single durable objective: it keeps the complete final plan, explicit user amendments, progress, and the checked overall result; children bind it but do not duplicate it. On continuation or candidate repair, read `anchor show --with-root` and acknowledge the reviewed plan version before editing. A structured root closes only after every child is terminal and `root-anchor accept` records accepted or cancelled evidence. A child in another repository uses the same root only through explicit `--root-anchor-file <absolute-path>`, never a duplicated root. DWW verifies and records that exact non-linked root locator and its child-state locator, but it never searches repositories or becomes a `scope_id`, candidate group, DAG, scheduler, or batch boundary. A configured project Adapter may establish project runtime identity only after the exact isolated task exists and before Start returns it as active. New repositories publish exact source candidates, release project resources through the same Adapter, then release the task worktree. Each configured full batch freezes automatically; an exact smaller tail freezes only after the host explicitly ends the round or requests immediate integration. Host heartbeat may wake `batch reconcile` but cannot choose candidates; UI task counts, raw worktree counts, Hook delivery, and session end never prove completion. There is no candidate-age or quiet-period auto-seal. Use the host's native task/subagent system for task orchestration; legacy `dww orchestrate` state is drain-only. Candidate publication is not delivery; only integration into the current base is delivery. Explicit legacy direct policy remains upgrade compatibility only.
{MANAGED_END}
"""


def _pre_refresh_root_context_managed_block() -> str:
    """上一版完整托管块；只供精确升级，避免把用户改写误当成可替换文本。"""

    current = _pre_simplification_managed_block()
    current_rule = "Start writes the known task purpose, implementation target, scope, acceptance criteria, and baseline into one local task anchor; keep it current and reread it after continuation or context loss. When the user has explicitly confirmed a complete plan or asked to set it as the objective, create one DWW root anchor before the first child Start with the full plan file, its source, and a stable request id. The root is the single durable objective: it keeps the complete final plan, full prior versions of plan-changing amendments, explicit user amendments, progress, and the checked overall result; children bind it but do not duplicate it. Anchors, plan inputs, historical versions, and exact cross-repository closure state have no DWW content-size quota. A legacy guide root first upgraded by a complete plan amendment keeps its pre-structured text as a separate legacy snapshot, while the new complete plan becomes version 1. When a structured root is supplied at Start or bound to an active task, the host returns the complete root body once and records that read internally; that record is not proof of understanding and does not require a user receipt. On continuation, model/context recovery, a root-plan version change, or candidate repair, the host uses one internal root-context refresh that returns the complete current body and records its version without copying a SHA or version. Before Commit, actual Ready, or Finish candidate publication, a bound structured root must have that current read record; recover by refreshing and retrying without losing work. This is not a gate on every edit. A structured root closes only after every child is terminal and root-anchor accept records accepted or cancelled evidence for its current plan version. A child in another repository uses the same root only through an explicit external root-anchor file, never a duplicated root. DWW verifies and records that exact non-linked root locator and its child-state locator, but it never searches repositories or becomes a scope id, candidate group, DAG, scheduler, or batch boundary. A configured project Adapter may establish project runtime identity only after the exact isolated task exists and before Start returns it as active. New repositories publish exact source candidates, release project resources through the same Adapter, then release the task worktree. Each configured full batch freezes automatically; an exact smaller tail freezes only after the host explicitly ends the round or requests immediate integration. Host heartbeat may wake batch reconcile but cannot choose candidates; UI task counts, raw worktree counts, Hook delivery, and session end never prove completion. There is no candidate-age or quiet-period auto-seal. Use the host native task/subagent system for task orchestration; legacy dww orchestrate state is drain-only. Candidate publication is not delivery; only integration into the current base is delivery. Explicit legacy direct policy remains upgrade compatibility only."
    previous_rule = "`Start` creates the local task anchor; keep it current and reread it after continuation or context loss. When the user has explicitly confirmed a complete plan or asked to set it as the objective, create one DWW `root-anchor` before the first child Start with the full plan file, its source, and a stable request id. The root is the single durable objective: it keeps the complete final plan, full prior versions of plan-changing amendments, explicit user amendments, progress, and the checked overall result; children bind it but do not duplicate it. Anchors, plan inputs, historical versions, and exact cross-repository closure state have no DWW content-size quota. On continuation, model/context recovery, first execution after `bind-root`, a root-plan version change, or candidate repair, the host automatically reads `anchor show --with-root --content --root-content` and records that version with `acknowledge-root` once for the continuous work; it does not ask the user or claim that the record proves understanding. Before Commit, actual Ready, or Finish candidate publication, a bound structured root must have that current reviewed version; recover by reading, acknowledging, and retrying without losing work. This is not a gate on every edit. For a pre-existing active or ready task that missed the normal path, use idempotent `anchor bind-root` rather than recreating the task. A structured root closes only after every child is terminal and `root-anchor accept` records accepted or cancelled evidence for its current plan version. A child in another repository uses the same root only through explicit `--root-anchor-file <absolute-path>`, never a duplicated root. DWW verifies and records that exact non-linked root locator and its child-state locator, but it never searches repositories or becomes a `scope_id`, candidate group, DAG, scheduler, or batch boundary. A configured project Adapter may establish project runtime identity only after the exact isolated task exists and before Start returns it as active. New repositories publish exact source candidates, release project resources through the same Adapter, then release the task worktree. Each configured full batch freezes automatically; an exact smaller tail freezes only after the host explicitly ends the round or requests immediate integration. Host heartbeat may wake `batch reconcile` but cannot choose candidates; UI task counts, raw worktree counts, Hook delivery, and session end never prove completion. There is no candidate-age or quiet-period auto-seal. Use the host's native task/subagent system for task orchestration; legacy `dww orchestrate` state is drain-only. Candidate publication is not delivery; only integration into the current base is delivery. Explicit legacy direct policy remains upgrade compatibility only."
    if current.count(current_rule) != 1:
        raise RuntimeError("Current managed block no longer has the root-context rule")
    return current.replace(current_rule, previous_rule)


def _released_root_review_managed_block() -> str:
    """e73bc6b 发布过的完整托管块，只用于无歧义迁移。"""

    released_base = _legacy_managed_block()
    old_sentence = (
        "The root is the single durable objective: it keeps the complete final plan, "
        "explicit user amendments, progress, and the checked overall result; children "
        "bind it but do not duplicate it. On continuation or candidate repair, read "
        "`anchor show --with-root` and acknowledge the reviewed plan version before editing."
    )
    if released_base.count(old_sentence) != 1:
        raise RuntimeError("Known legacy managed block no longer has its release text")
    released_sentence = (
        "The root is the single durable objective: it keeps the complete final plan, "
        "full prior versions of plan-changing amendments, explicit user amendments, "
        "progress, and the checked overall result; children bind it but do not duplicate "
        "it. Anchors, plan inputs, historical versions, and exact cross-repository closure "
        "state have no DWW content-size quota. On continuation or candidate repair, read "
        "the complete execution basis with `anchor show --with-root --content --root-content` "
        "when needed; `acknowledge-root` is an optional review record, not a per-operation "
        "gate. For a pre-existing active or ready task that missed the normal path, use "
        "idempotent `anchor bind-root` rather than recreating the task."
    )
    return released_base.replace(old_sentence, released_sentence)


def _managed_region(existing: str) -> tuple[int, int]:
    if existing.count(MANAGED_START) != 1 or existing.count(MANAGED_END) != 1:
        raise SoloAIError(
            "AGENTS.md must contain exactly one develop-with-worktrees managed block"
        )
    start = existing.index(MANAGED_START)
    end = existing.index(MANAGED_END, start) + len(MANAGED_END)
    return start, end


def managed_agents_status(existing: str) -> str:
    """识别可安全同步的版本；用户改写的内容保持失败关闭。"""

    start, end = _managed_region(existing)
    # 区间精确止于结束标记；模板字符串惯例上带一个最终换行，不能把
    # 文件中块后的空行误当成托管内容，也不能因此误判历史生成块。
    block = existing[start:end].replace("\r\n", "\n") + "\n"
    if block == managed_block():
        return "current"
    if block == _pre_root_output_managed_block():
        return "known-legacy-root-output"
    if block == _pre_simplification_managed_block():
        return "known-legacy-0.5.0-beta.2-pre-simplification"
    if block == _legacy_managed_block():
        return "known-legacy-0.5.0-beta.1"
    if block == _pre_refresh_root_context_managed_block():
        return "known-legacy-root-context-refresh"
    if block == _released_root_review_managed_block():
        return "known-legacy-root-review"
    raise SoloAIError(
        "AGENTS.md managed block contains user changes or an unknown version; refusing to overwrite it"
    )


def render_agents(existing: str) -> str:
    if MANAGED_START in existing or MANAGED_END in existing:
        start, end = _managed_region(existing)
        managed_agents_status(existing)
        replacement = managed_block()
        if existing[end:].startswith(("\r\n", "\n")):
            replacement = replacement.rstrip("\n")
        return existing[:start] + replacement + existing[end:]
    prefix = existing.rstrip()
    return (prefix + "\n\n" if prefix else "") + managed_block()


def remove_managed_agents_block(existing: str) -> str:
    start, end = _managed_region(existing)
    managed_agents_status(existing)
    before = existing[:start].rstrip()
    after = existing[end:].lstrip("\r\n")
    return ((before + "\n\n") if before and after else before) + after
