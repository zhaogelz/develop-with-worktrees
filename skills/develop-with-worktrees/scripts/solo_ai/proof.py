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


def _tracked(repo: GitRepo, cwd: Path) -> list[str]:
    return sorted(
        item
        for item in repo.git(["ls-files", "-z"], cwd=cwd).stdout.split("\0")
        if item
    )


def _shared_inputs(
    repo: GitRepo, cwd: Path, commands: list[str], tracked: list[str]
) -> dict[str, Any]:
    locks = {
        relative: sha256_file(cwd / relative)
        for relative in tracked
        if Path(relative).name in LOCKFILES and (cwd / relative).exists()
    }
    configs = {
        relative: sha256_file(cwd / relative)
        for relative in (".solo-ai/config.toml", ".solo-ai/verification.toml")
    }
    tool_names = dict.fromkeys(
        ["git", "uv", *(command.split()[0] for command in commands)]
    )
    return {
        "schema_version": 1,
        "config_hashes": configs,
        "lockfiles": locks,
        "tools": [_tool(name, cwd) for name in tool_names],
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
    }


def _profile_inputs(
    profile: VerificationProfile,
    *,
    cwd: Path,
    tracked: list[str],
    shared: dict[str, Any],
) -> dict[str, Any]:
    matched = [
        relative
        for relative in tracked
        if any(fnmatch.fnmatchcase(relative, pattern) for pattern in profile.paths)
    ]
    return {
        **shared,
        "profile_id": profile.profile_id,
        "paths": list(profile.paths),
        "commands": list(profile.commands),
        "tracked_inputs": {
            relative: sha256_file(cwd / relative)
            for relative in matched
            if (cwd / relative).is_file()
        },
    }


def proof_inputs(
    repo: GitRepo, *, cwd: Path, base: str, verification: VerificationConfig
) -> tuple[dict[str, Any], list[tuple[VerificationProfile, dict[str, Any], str]]]:
    files = changed_files(repo, cwd=cwd, base=base)
    profiles = select_profiles(verification, files)
    commands = [command for profile in profiles for command in profile.commands]
    tracked = _tracked(repo, cwd)
    shared = _shared_inputs(repo, cwd, commands, tracked)
    profile_records: list[tuple[VerificationProfile, dict[str, Any], str]] = []
    for profile in profiles:
        inputs = _profile_inputs(profile, cwd=cwd, tracked=tracked, shared=shared)
        profile_records.append((profile, inputs, sha256_text(stable_json(inputs))))
    candidate = {
        **shared,
        "candidate_head": repo.head(cwd),
        "candidate_tree": repo.tree(cwd=cwd),
        "base_head": repo.git(["rev-parse", base], cwd=cwd).stdout.strip(),
        "files": files,
        "profiles": [profile.profile_id for profile in profiles],
        "commands": commands,
        "profile_fingerprints": [item[2] for item in profile_records],
    }
    return candidate, profile_records


def _logs_exist(proof: dict[str, Any]) -> bool:
    return all(Path(item["log"]).exists() for item in proof.get("runs", []))


def _run_profile(
    repo: GitRepo,
    *,
    cwd: Path,
    profile: VerificationProfile,
    inputs: dict[str, Any],
    fingerprint: str,
) -> dict[str, Any]:
    proof_path = repo.local_dir / "profile-proofs" / f"{fingerprint}.json"
    existing = read_json(proof_path, {})
    if existing.get("result") == "passed" and _logs_exist(existing):
        existing["reused_at"] = utc_timestamp()
        atomic_write_json(proof_path, existing)
        return {**existing, "reused": True}

    run_id = new_id(f"profile-{profile.profile_id}")
    log_dir = repo.local_dir / "logs" / run_id
    runs: list[dict[str, Any]] = []
    for index, command in enumerate(profile.commands, 1):
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
                "inputs": inputs,
                "runs": runs,
                "created_at": utc_timestamp(),
            }
            atomic_write_json(proof_path, proof)
            raise SoloAIError(
                f"Validation failed in profile {profile.profile_id}: {command}. Local redacted log: {log_path}"
            )
    proof = {
        "fingerprint": fingerprint,
        "result": "passed",
        "inputs": inputs,
        "runs": runs,
        "created_at": utc_timestamp(),
    }
    atomic_write_json(proof_path, proof)
    return {**proof, "reused": False}


def validate(
    repo: GitRepo, *, cwd: Path, base: str, verification: VerificationConfig
) -> dict[str, Any]:
    inputs, profile_records = proof_inputs(
        repo, cwd=cwd, base=base, verification=verification
    )
    fingerprint = sha256_text(stable_json(inputs))
    proof_path = repo.local_dir / "proofs" / f"{fingerprint}.json"
    existing = read_json(proof_path, {})
    if existing.get("result") == "passed" and _logs_exist(existing):
        existing["reused_at"] = utc_timestamp()
        atomic_write_json(proof_path, existing)
        return existing

    runs: list[dict[str, Any]] = []
    profile_proofs: list[dict[str, Any]] = []
    for profile, profile_inputs, profile_fingerprint in profile_records:
        result = _run_profile(
            repo,
            cwd=cwd,
            profile=profile,
            inputs=profile_inputs,
            fingerprint=profile_fingerprint,
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

    if not inputs["commands"]:
        run_id = new_id("verify-static")
        log_path = repo.local_dir / "logs" / run_id / "01-static-only.log"
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
                "profile_id": None,
                "reused": False,
            }
        )

    kind = "static-only" if not inputs["commands"] else "commands"
    proof = {
        "fingerprint": fingerprint,
        "result": "passed",
        "kind": kind,
        "inputs": inputs,
        "profile_proofs": profile_proofs,
        "runs": runs,
        "created_at": utc_timestamp(),
    }
    atomic_write_json(proof_path, proof)
    return proof
