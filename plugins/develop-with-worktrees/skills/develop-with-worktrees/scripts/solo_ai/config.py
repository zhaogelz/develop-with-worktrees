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


def _cleanup_paths(
    raw: Any, *, field: str, default: tuple[str, ...], allow_patterns: bool
) -> tuple[str, ...]:
    values = default if raw is None else _strings(raw, field=field, allow_empty=True)
    normalized: list[str] = []
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
    if not 1024 <= port_base <= 62436:
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
    )


def load_verification_config(
    repo: GitRepo, *, cwd: Path | None = None
) -> VerificationConfig:
    data = _read_toml((cwd or repo.policy_path()) / ".solo-ai" / "verification.toml")
    schema_version = _integer(data.get("schema_version", 0), field="schema_version")
    if schema_version != VERIFICATION_SCHEMA:
        raise SoloAIError(
            "Unsupported .solo-ai/verification.toml schema; expected "
            f"{VERIFICATION_SCHEMA}"
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
        if level not in {"development", "ready", "full"}:
            raise SoloAIError(
                f"Profile {profile_id!r} level must be development, ready, or full"
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

[lifecycle]
# dev_start = ["npm", "run", "dev", "--", "--port", "{{port}}"]
# [lifecycle.readiness]
# kind = "http" # tcp or http
# target = "http://127.0.0.1:{{port}}/health"
# timeout_seconds = 30
"""


def render_verification_config(
    commands: list[CommandSpec], *, static_only: bool
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
                'level = "ready"',
                "commands = [",
            )
        )
        lines.extend(f"  {_toml_array(list(command.argv))}," for command in commands)
        lines.extend(("]", ""))
    return "\n".join(lines)


MANAGED_START = "<!-- develop-with-worktrees:managed:start -->"
MANAGED_END = "<!-- develop-with-worktrees:managed:end -->"


def managed_block() -> str:
    return f"""{MANAGED_START}
## Isolated coding tasks

For every task that may modify repository files, use the installed `develop-with-worktrees` skill before editing. Run `start`, work only in the returned worktree, stage an exact reviewed path list with `commit`, then run `ready` and `finish`. Read-only analysis does not claim a slot. Do not bypass a failed gate. This repository's policy is local-only: do not fetch, pull, push, create PRs, rebase, squash, amend, or rewrite history through this lifecycle.
{MANAGED_END}
"""


def render_agents(existing: str) -> str:
    if MANAGED_START in existing or MANAGED_END in existing:
        raise SoloAIError(
            "AGENTS.md already contains a managed block; refusing to overwrite it"
        )
    prefix = existing.rstrip()
    return (prefix + "\n\n" if prefix else "") + managed_block()


def remove_managed_agents_block(existing: str) -> str:
    start = existing.find(MANAGED_START)
    end = existing.find(MANAGED_END)
    if start < 0 or end < 0 or end < start:
        raise SoloAIError(
            "AGENTS.md does not contain one exact develop-with-worktrees managed block"
        )
    end += len(MANAGED_END)
    before = existing[:start].rstrip()
    after = existing[end:].lstrip("\r\n")
    return ((before + "\n\n") if before and after else before) + after
