from __future__ import annotations

import json
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .repo import GitRepo
from .util import SoloAIError, redact_text, sha256_file, sha256_text, stable_json


CONFIG_SCHEMA = 2
VERIFICATION_SCHEMA = 2


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


@dataclass(frozen=True)
class VerificationProfile:
    profile_id: str
    paths: tuple[str, ...]
    commands: tuple[CommandSpec, ...]
    cross_task_reuse: bool
    external_state: str
    input_paths: tuple[str, ...]
    environment: tuple[str, ...]


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


def _commands(raw: Any, *, field: str) -> tuple[CommandSpec, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise SoloAIError(f"{field} must be an array of argv arrays")
    return tuple(
        _command(item, field=f"{field}[{index}]") for index, item in enumerate(raw)
    )


def load_repo_config(repo: GitRepo, *, cwd: Path | None = None) -> RepoConfig:
    data = _read_toml((cwd or repo.policy_path()) / ".solo-ai" / "config.toml")
    if int(data.get("schema_version", 0)) != CONFIG_SCHEMA:
        raise SoloAIError(
            f"Unsupported .solo-ai/config.toml schema; expected {CONFIG_SCHEMA}"
        )
    lifecycle = data.get("lifecycle") or {}
    slots = int(data.get("slots", 3))
    if not 1 <= slots <= 5:
        raise SoloAIError("slots must be between 1 and 5")
    mode = str(data.get("mode", "managed"))
    if mode != "managed":
        raise SoloAIError('Only mode = "managed" is valid in an adopted repository')
    port_base = int(data.get("port_base", 20000))
    if not 1024 <= port_base <= 65036:
        raise SoloAIError("port_base must leave room for all 100-port slot blocks")
    remote_policy = str(data.get("remote_policy", "local-only"))
    if remote_policy != "local-only":
        raise SoloAIError('Version 1 supports only remote_policy = "local-only"')
    readiness: ReadinessSpec | None = None
    dev_start = lifecycle.get("dev_start")
    readiness_raw = lifecycle.get("readiness")
    if dev_start is not None:
        if not isinstance(readiness_raw, dict):
            raise SoloAIError(
                "lifecycle.readiness is required when lifecycle.dev_start is configured"
            )
        kind = str(readiness_raw.get("kind", ""))
        if kind not in {"tcp", "http"}:
            raise SoloAIError("lifecycle.readiness.kind must be tcp or http")
        readiness = ReadinessSpec(
            kind=kind,
            target=str(readiness_raw["target"])
            if readiness_raw.get("target")
            else None,
            timeout_seconds=float(readiness_raw.get("timeout_seconds", 30)),
        )
        if readiness.timeout_seconds <= 0:
            raise SoloAIError("lifecycle.readiness.timeout_seconds must be positive")
        if kind in {"tcp", "http"} and not readiness.target:
            raise SoloAIError("lifecycle.readiness.target is required for tcp and http")
    return RepoConfig(
        schema_version=CONFIG_SCHEMA,
        mode=mode,
        slots=slots,
        branch_prefix=str(data.get("branch_prefix", "codex/")),
        worktree_directory=str(data.get("worktree_directory", ".worktrees")),
        port_base=port_base,
        remote_policy=remote_policy,
        sensitive_allowlist=tuple(
            str(item) for item in data.get("sensitive_allowlist", [])
        ),
        agents_file_created=bool(data.get("agents_file_created", False)),
        secret_scanner=_command(data["secret_scanner"], field="secret_scanner")
        if data.get("secret_scanner")
        else None,
        warm_commands=_commands(data.get("warm"), field="warm"),
        dev_start=_command(dev_start, field="lifecycle.dev_start")
        if dev_start
        else None,
        readiness=readiness,
    )


def load_verification_config(
    repo: GitRepo, *, cwd: Path | None = None
) -> VerificationConfig:
    data = _read_toml((cwd or repo.policy_path()) / ".solo-ai" / "verification.toml")
    if int(data.get("schema_version", 0)) != VERIFICATION_SCHEMA:
        raise SoloAIError(
            f"Unsupported .solo-ai/verification.toml schema; expected {VERIFICATION_SCHEMA}"
        )
    profiles: list[VerificationProfile] = []
    for index, raw in enumerate(data.get("profiles", [])):
        if not isinstance(raw, dict):
            raise SoloAIError(f"profiles[{index}] must be a TOML table")
        profile_id = str(raw.get("id", "")).strip()
        paths = tuple(str(item) for item in raw.get("paths", ["**"]))
        commands = _commands(raw.get("commands"), field=f"profiles[{index}].commands")
        reuse = bool(raw.get("cross_task_reuse", False))
        external_state = str(raw.get("external_state", "unknown"))
        if not profile_id:
            raise SoloAIError("Every verification profile needs a non-empty id")
        if not commands:
            raise SoloAIError(f"Verification profile {profile_id!r} has no commands")
        if reuse and external_state != "none":
            raise SoloAIError(
                f'Profile {profile_id!r} enables cross_task_reuse but external_state is not "none"'
            )
        profiles.append(
            VerificationProfile(
                profile_id=profile_id,
                paths=paths,
                commands=commands,
                cross_task_reuse=reuse,
                external_state=external_state,
                input_paths=tuple(str(item) for item in raw.get("input_paths", paths)),
                environment=tuple(str(item) for item in raw.get("environment", [])),
            )
        )
    static_only = bool(data.get("static_only", False))
    if not profiles and not static_only:
        raise SoloAIError(
            "No validation commands configured; explicitly enable static_only or add a profile"
        )
    return VerificationConfig(VERIFICATION_SCHEMA, static_only, tuple(profiles))


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


WORKFLOW_MARKERS = {
    ".config/wt.toml": "Worktrunk",
    ".conductor": "Conductor",
    ".parallel-code": "Parallel Code",
    "scripts/worktree-flow.ps1": "repository worktree-flow",
    ".sdd": "agent orchestrator workspace",
}


def detect_existing_workflows(root: Path) -> list[str]:
    return [
        name
        for relative, name in WORKFLOW_MARKERS.items()
        if (root / relative).exists()
    ]


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
worktree_directory = ".worktrees"
port_base = 20000
remote_policy = "local-only"
sensitive_allowlist = []
agents_file_created = {"true" if agents_file_created else "false"}

# Optional repository-declared scanner, for example: ["gitleaks", "protect", "--staged"]
# secret_scanner = []
# Optional serial preparation commands for an idle slot. No environment is copied.
# warm = [["uv", "sync"]]

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
