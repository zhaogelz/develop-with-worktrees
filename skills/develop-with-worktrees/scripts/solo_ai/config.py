from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .repo import GitRepo
from .util import SoloAIError, sha256_text, stable_json


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
    dev_start: str | None
    dev_stop: str | None


@dataclass(frozen=True)
class VerificationProfile:
    profile_id: str
    paths: tuple[str, ...]
    commands: tuple[str, ...]


@dataclass(frozen=True)
class VerificationConfig:
    schema_version: int
    static_only: bool
    profiles: tuple[VerificationProfile, ...]

    @property
    def commands(self) -> tuple[str, ...]:
        ordered: list[str] = []
        for profile in self.profiles:
            for command in profile.commands:
                if command not in ordered:
                    ordered.append(command)
        return tuple(ordered)

    @property
    def command_fingerprint(self) -> str:
        return sha256_text(stable_json(self.commands))


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


def load_repo_config(repo: GitRepo, *, cwd: Path | None = None) -> RepoConfig:
    data = _read_toml((cwd or repo.root) / ".solo-ai" / "config.toml")
    lifecycle = data.get("lifecycle") or {}
    slots = int(data.get("slots", 3))
    if not 1 <= slots <= 5:
        raise SoloAIError("slots must be between 1 and 5")
    mode = str(data.get("mode", "managed"))
    if mode not in {"managed", "compatible", "disabled"}:
        raise SoloAIError(f"Unsupported mode: {mode}")
    port_base = int(data.get("port_base", 20000))
    if not 1024 <= port_base <= 65036:
        raise SoloAIError("port_base must leave room for all 100-port slot blocks")
    remote_policy = str(data.get("remote_policy", "local-only"))
    if remote_policy != "local-only":
        raise SoloAIError('Version 1 supports only remote_policy = "local-only"')
    return RepoConfig(
        schema_version=int(data.get("schema_version", 1)),
        mode=mode,
        slots=slots,
        branch_prefix=str(data.get("branch_prefix", "codex/")),
        worktree_directory=str(data.get("worktree_directory", ".worktrees")),
        port_base=port_base,
        remote_policy=remote_policy,
        sensitive_allowlist=tuple(
            str(item) for item in data.get("sensitive_allowlist", [])
        ),
        dev_start=str(lifecycle["dev_start"]) if lifecycle.get("dev_start") else None,
        dev_stop=str(lifecycle["dev_stop"]) if lifecycle.get("dev_stop") else None,
    )


def load_verification_config(
    repo: GitRepo, *, cwd: Path | None = None
) -> VerificationConfig:
    data = _read_toml((cwd or repo.root) / ".solo-ai" / "verification.toml")
    profiles: list[VerificationProfile] = []
    for raw in data.get("profiles", []):
        profile_id = str(raw.get("id", "")).strip()
        commands = tuple(
            str(item).strip() for item in raw.get("commands", []) if str(item).strip()
        )
        paths = tuple(str(item) for item in raw.get("paths", ["**"]))
        if not profile_id:
            raise SoloAIError("Every verification profile needs a non-empty id")
        if not commands:
            raise SoloAIError(f"Verification profile {profile_id!r} has no commands")
        profiles.append(VerificationProfile(profile_id, paths, commands))
    static_only = bool(data.get("static_only", False))
    if not profiles and not static_only:
        raise SoloAIError(
            "No validation commands configured; explicitly enable static_only or add a profile"
        )
    return VerificationConfig(
        int(data.get("schema_version", 1)), static_only, tuple(profiles)
    )


def _package_json_commands(root: Path) -> list[str]:
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
    commands: list[str] = []
    for name in ("lint", "typecheck", "check", "test", "build"):
        if name not in scripts:
            continue
        if manager == "npm":
            commands.append(f"npm run {name}")
        else:
            commands.append(f"{manager} {name}")
    return commands


def discover_validation_commands(root: Path) -> list[str]:
    commands = _package_json_commands(root)
    if (root / "pyproject.toml").exists():
        pyproject = (root / "pyproject.toml").read_text(
            encoding="utf-8", errors="replace"
        )
        if (root / "tests").exists() or "pytest" in pyproject:
            commands.append("uv run pytest")
        elif any(root.glob("test*.py")):
            commands.append("uv run python -m unittest discover")
    if (root / "Cargo.toml").exists():
        commands.extend(
            ["cargo test", "cargo clippy --all-targets --all-features -- -D warnings"]
        )
    if (root / "go.mod").exists():
        commands.append("go test ./...")
    for candidate in ("scripts/verify.sh", "scripts/verify.ps1", "scripts/verify.py"):
        path = root / candidate
        if not path.exists():
            continue
        if path.suffix == ".sh":
            commands.insert(0, f"sh {candidate}")
        elif path.suffix == ".ps1":
            commands.insert(0, f"pwsh -File {candidate}")
        else:
            commands.insert(0, f"uv run {candidate}")
        break
    unique: list[str] = []
    for command in commands:
        if command not in unique:
            unique.append(command)
    return unique


def detect_existing_workflows(root: Path) -> list[str]:
    markers = {
        ".config/wt.toml": "Worktrunk",
        ".conductor": "Conductor",
        ".parallel-code": "Parallel Code",
        "scripts/worktree-flow.ps1": "repository worktree-flow",
        ".sdd": "agent orchestrator workspace",
    }
    return [name for relative, name in markers.items() if (root / relative).exists()]


def quote_toml(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_repo_config(*, mode: str = "managed", slots: int = 3) -> str:
    return f"""schema_version = 1
mode = {quote_toml(mode)}
slots = {slots}
branch_prefix = "codex/"
worktree_directory = ".worktrees"
port_base = 20000
remote_policy = "local-only"
sensitive_allowlist = []

[lifecycle]
# dev_start = "npm run dev -- --port {{port}}"
# dev_stop is optional; managed process identity is checked before stopping.
"""


def render_verification_config(commands: list[str], *, static_only: bool) -> str:
    lines = [
        "schema_version = 1",
        f"static_only = {'true' if static_only else 'false'}",
        "",
    ]
    if commands:
        lines.extend(
            [
                "[[profiles]]",
                'id = "default"',
                'paths = ["**"]',
                "commands = [",
                *[f"  {quote_toml(command)}," for command in commands],
                "]",
                "",
            ]
        )
    return "\n".join(lines)


MANAGED_START = "<!-- develop-with-worktrees:managed:start -->"
MANAGED_END = "<!-- develop-with-worktrees:managed:end -->"


def render_agents(existing: str, *, compatible: bool = False) -> str:
    if MANAGED_START in existing or MANAGED_END in existing:
        raise SoloAIError(
            "AGENTS.md already contains a managed block; refusing to overwrite it"
        )
    instruction = (
        "For every task that may modify repository files, follow the repository's existing worktree or agent-orchestration workflow. The installed `develop-with-worktrees` skill is in compatible mode and must not claim its own managed slot. Read-only analysis does not claim a slot."
        if compatible
        else "For every task that may modify repository files, use the installed `develop-with-worktrees` skill before editing. Run its `start` command, perform all edits and checks only in the returned worktree, commit the task, then run `ready` followed immediately by `finish`. Read-only analysis does not claim a slot. Never bypass a failed gate; use `status` and `recover` to preserve and resume the task."
    )
    block = f"""{MANAGED_START}
## Isolated coding tasks

{instruction}
{MANAGED_END}
"""
    prefix = existing.rstrip()
    return (prefix + "\n\n" if prefix else "") + block
