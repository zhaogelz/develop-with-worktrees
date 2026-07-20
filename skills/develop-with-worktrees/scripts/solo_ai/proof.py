from __future__ import annotations

import fnmatch
import os
import platform
import shutil
from pathlib import Path
from typing import Any

from .config import CommandSpec, VerificationConfig, VerificationProfile
from .repo import GitRepo
from .util import (
    SoloAIError,
    atomic_write_json,
    new_id,
    redact_text,
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
PROOF_SCHEMA = 2


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
) -> dict[str, Any]:
    tracked = _tracked(repo, cwd)
    tool_specs = [CommandSpec(("git",)), CommandSpec(("uv",)), *commands]
    unique: dict[str, CommandSpec] = {}
    for command in tool_specs:
        unique.setdefault(command.argv[0], command)
    return {
        "config_hashes": {
            # A policy's meaning cannot depend on a platform checkout changing
            # LF to CRLF. Other text changes remain approval-significant.
            ".solo-ai/config.toml": sha256_text(
                (cwd / ".solo-ai" / "config.toml")
                .read_text(encoding="utf-8")
                .replace("\r\n", "\n")
            ),
            ".solo-ai/verification.toml": sha256_text(
                (cwd / ".solo-ai" / "verification.toml")
                .read_text(encoding="utf-8")
                .replace("\r\n", "\n")
            ),
            "verification_normalized": sha256_text(
                stable_json(verification.normalized())
            ),
        },
        "lockfiles": {
            relative: sha256_file(cwd / relative)
            for relative in tracked
            if Path(relative).name in LOCKFILES and (cwd / relative).is_file()
        },
        "tools": [_tool(command, cwd) for command in unique.values()],
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
    return {
        **shared,
        "profile_id": profile.profile_id,
        "paths": list(profile.paths),
        "command_digests": [command.fingerprint for command in profile.commands],
        "cross_task_reuse": profile.cross_task_reuse,
        "external_state": profile.external_state,
        "tracked_inputs": _matching_hashes(cwd, tracked, profile.input_paths),
        "environment": {
            name: sha256_text(os.environ[name]) if name in os.environ else "absent"
            for name in profile.environment
        },
    }


def approval_plan(
    repo: GitRepo, *, cwd: Path, verification: VerificationConfig
) -> dict[str, Any]:
    commands = list(verification.commands)
    shared = _shared_inputs(repo, cwd, commands, verification)
    return {
        "schema_version": PROOF_SCHEMA,
        "git_common_dir": sha256_text(str(repo.common_dir)),
        "policy": shared,
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
            }
            for profile in verification.profiles
        ],
        "static_only": verification.static_only,
    }


def proof_inputs(
    repo: GitRepo, *, cwd: Path, base: str, verification: VerificationConfig
) -> tuple[dict[str, Any], list[tuple[VerificationProfile, dict[str, Any], str]]]:
    files = changed_files(repo, cwd=cwd, base=base)
    profiles = select_profiles(verification, files)
    commands = [command for profile in profiles for command in profile.commands]
    tracked = _tracked(repo, cwd)
    shared = _shared_inputs(repo, cwd, commands, verification)
    candidate_head = repo.head(cwd)
    candidate_tree = repo.tree(cwd=cwd)
    records: list[tuple[VerificationProfile, dict[str, Any], str]] = []
    for profile in profiles:
        inputs = _profile_inputs(profile, cwd=cwd, tracked=tracked, shared=shared)
        # A default profile may only reuse in the identical candidate. Reuse across
        # task branches is opt-in and only legal for a declared closed environment.
        scope = (
            "cross-task"
            if profile.cross_task_reuse and profile.external_state == "none"
            else f"candidate:{candidate_head}"
        )
        inputs["reuse_scope"] = scope
        records.append((profile, inputs, sha256_text(stable_json(inputs))))
    candidate = {
        "schema_version": PROOF_SCHEMA,
        **shared,
        "candidate_head": candidate_head,
        "candidate_tree": candidate_tree,
        "base_head": repo.git(["rev-parse", base], cwd=cwd).stdout.strip(),
        "files": files,
        "profiles": [profile.profile_id for profile in profiles],
        "profile_fingerprints": [item[2] for item in records],
    }
    return candidate, records


def _logs_exist(proof: dict[str, Any]) -> bool:
    return all(
        Path(item["log"]).is_file()
        and sha256_file(Path(item["log"])) == item.get("log_sha256")
        for item in proof.get("runs", [])
    )


def _content_address_log(repo: GitRepo, temporary: Path) -> tuple[Path, str]:
    digest = sha256_file(temporary)
    target = repo.local_dir / "logs" / "content" / f"{digest}.log"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        temporary.unlink(missing_ok=True)
    else:
        temporary.replace(target)
    return target, digest


def _run_profile(
    repo: GitRepo,
    *,
    cwd: Path,
    profile: VerificationProfile,
    inputs: dict[str, Any],
    fingerprint: str,
) -> dict[str, Any]:
    proof_path = repo.local_dir / "profile-proofs" / f"{fingerprint}.json"
    from .util import read_json

    existing = read_json(proof_path, {})
    if existing.get("result") == "passed" and _logs_exist(existing):
        existing["reused_at"] = utc_timestamp()
        atomic_write_json(proof_path, existing)
        return {**existing, "reused": True}
    run_id = new_id(f"profile-{profile.profile_id}")
    temp_dir = repo.local_dir / "logs" / "pending" / run_id
    runs: list[dict[str, Any]] = []
    for index, command in enumerate(profile.commands, 1):
        pending = temp_dir / f"{index:02d}.log"
        code, duration = run_logged(command.argv, cwd=cwd, log_path=pending)
        log_path, log_digest = _content_address_log(repo, pending)
        runs.append(
            {
                "command_digest": command.fingerprint,
                "command": command.redacted(),
                "exit_code": code,
                "duration_seconds": round(duration, 3),
                "log": str(log_path),
                "log_sha256": log_digest,
            }
        )
        if code != 0:
            proof = {
                "schema_version": PROOF_SCHEMA,
                "fingerprint": fingerprint,
                "result": "failed",
                "inputs": inputs,
                "runs": runs,
                "created_at": utc_timestamp(),
            }
            atomic_write_json(proof_path, proof)
            raise SoloAIError(
                f"Validation failed in profile {profile.profile_id}. Local redacted log: {log_path}"
            )
    proof = {
        "schema_version": PROOF_SCHEMA,
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
    from .util import read_json

    inputs, records = proof_inputs(repo, cwd=cwd, base=base, verification=verification)
    if not records and not verification.static_only:
        raise SoloAIError(
            "No verification profile covers the candidate changes; add an explicit path mapping or opt into static_only"
        )
    fingerprint = sha256_text(stable_json(inputs))
    proof_path = repo.local_dir / "proofs" / f"{fingerprint}.json"
    existing = read_json(proof_path, {})
    if existing.get("result") == "passed" and _logs_exist(existing):
        existing["reused_at"] = utc_timestamp()
        atomic_write_json(proof_path, existing)
        return {**existing, "reused": True}

    runs: list[dict[str, Any]] = []
    profile_proofs: list[dict[str, Any]] = []
    for profile, profile_inputs, profile_fingerprint in records:
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
