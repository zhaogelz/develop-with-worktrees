from __future__ import annotations

import fnmatch
import platform
import shutil
from pathlib import Path
from typing import Any

from .config import VerificationConfig, VerificationProfile
from .repo import GitRepo
from .util import (
    SoloAIError,
    atomic_write_json,
    new_id,
    read_json,
    run,
    run_logged,
    sha256_file,
    sha256_text,
    stable_json,
    utc_timestamp,
)


LOCKFILES = (
    "uv.lock",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "Cargo.lock",
    "go.sum",
)


def changed_files(repo: GitRepo, *, cwd: Path, base: str) -> list[str]:
    output = repo.git(
        ["diff", "--name-only", "--no-renames", f"{base}...HEAD"], cwd=cwd
    ).stdout
    return sorted(item for item in output.splitlines() if item)


def select_profiles(
    config: VerificationConfig, files: list[str]
) -> list[VerificationProfile]:
    selected: list[VerificationProfile] = []
    for profile in config.profiles:
        if not files or any(
            any(fnmatch.fnmatchcase(path, pattern) for pattern in profile.paths)
            for path in files
        ):
            selected.append(profile)
    return selected


def _tool(command: str, cwd: Path) -> dict[str, str | None]:
    executable = command.split()[0]
    resolved = shutil.which(executable)
    version = None
    if resolved:
        result = run([resolved, "--version"], cwd=cwd, check=False, timeout=10)
        version = (
            (result.stdout or result.stderr).splitlines()[0][:300]
            if (result.stdout or result.stderr)
            else None
        )
    return {"name": executable, "path": resolved, "version": version}


def proof_inputs(
    repo: GitRepo, *, cwd: Path, base: str, verification: VerificationConfig
) -> dict[str, Any]:
    files = changed_files(repo, cwd=cwd, base=base)
    profiles = select_profiles(verification, files)
    commands = [command for profile in profiles for command in profile.commands]
    tracked = repo.git(["ls-files", "-z"], cwd=cwd).stdout.split("\0")
    locks = {
        relative: sha256_file(cwd / relative)
        for relative in tracked
        if relative and Path(relative).name in LOCKFILES and (cwd / relative).exists()
    }
    configs = {}
    for relative in (".solo-ai/config.toml", ".solo-ai/verification.toml"):
        path = cwd / relative
        configs[relative] = sha256_file(path)
    return {
        "schema_version": 1,
        "candidate_head": repo.head(cwd),
        "candidate_tree": repo.tree(cwd=cwd),
        "base_head": repo.git(["rev-parse", base], cwd=cwd).stdout.strip(),
        "files": files,
        "profiles": [profile.profile_id for profile in profiles],
        "commands": commands,
        "config_hashes": configs,
        "lockfiles": locks,
        "tools": [
            _tool(name, cwd)
            for name in dict.fromkeys(
                ["git", "uv", *(command.split()[0] for command in commands)]
            )
        ],
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
    }


def validate(
    repo: GitRepo, *, cwd: Path, base: str, verification: VerificationConfig
) -> dict[str, Any]:
    inputs = proof_inputs(repo, cwd=cwd, base=base, verification=verification)
    fingerprint = sha256_text(stable_json(inputs))
    proof_path = repo.local_dir / "proofs" / f"{fingerprint}.json"
    existing = read_json(proof_path, {})
    if existing.get("result") == "passed" and all(
        Path(item["log"]).exists() for item in existing.get("runs", [])
    ):
        existing["reused_at"] = utc_timestamp()
        atomic_write_json(proof_path, existing)
        return existing

    run_id = new_id("verify")
    log_dir = repo.local_dir / "logs" / run_id
    runs: list[dict[str, Any]] = []
    if verification.static_only and not inputs["commands"]:
        log_path = log_dir / "01-static-only.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            "Static-only gate completed: Git candidate integrity and sensitive-content checks only.\n",
            encoding="utf-8",
            newline="\n",
        )
        runs.append(
            {
                "command": None,
                "exit_code": 0,
                "duration_seconds": 0.0,
                "log": str(log_path),
            }
        )
    for index, command in enumerate(inputs["commands"], 1):
        log_path = log_dir / f"{index:02d}.log"
        code, duration = run_logged(command, cwd=cwd, log_path=log_path)
        runs.append(
            {
                "command": command,
                "exit_code": code,
                "duration_seconds": round(duration, 3),
                "log": str(log_path),
            }
        )
        if code != 0:
            proof = {
                "fingerprint": fingerprint,
                "result": "failed",
                "kind": "commands",
                "inputs": inputs,
                "runs": runs,
                "created_at": utc_timestamp(),
            }
            atomic_write_json(proof_path, proof)
            raise SoloAIError(
                f"Validation failed: {command}. Local redacted log: {log_path}"
            )
    kind = (
        "static-only"
        if verification.static_only and not inputs["commands"]
        else "commands"
    )
    proof = {
        "fingerprint": fingerprint,
        "result": "passed",
        "kind": kind,
        "inputs": inputs,
        "runs": runs,
        "created_at": utc_timestamp(),
    }
    atomic_write_json(proof_path, proof)
    return proof
