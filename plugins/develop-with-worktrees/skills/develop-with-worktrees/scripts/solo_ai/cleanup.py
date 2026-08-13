from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .repo import GitRepo
from .util import (
    SoloAIError,
    delete_plain_path_if_unchanged,
    ensure_within,
    is_link_or_junction,
    path_identity,
    snapshot_plain_path,
)

KNOWN_RETAINED_ROOTS = {
    ".venv",
    "node_modules",
    ".tmp",
    ".cache",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
}


@dataclass(frozen=True)
class CleanupPolicy:
    protected_directory_names: tuple[str, ...] = ("uploads", "storage")
    protected_file_suffixes: tuple[str, ...] = (".db", ".sqlite", ".sqlite3")


def classify_cleanup_path(relative: str, policy: CleanupPolicy = CleanupPolicy()) -> str:
    parts = tuple(part.casefold() for part in Path(relative).parts)
    leaf = parts[-1] if parts else ""
    if leaf == ".env" or leaf.startswith(".env."):
        return "keep"
    if any(name.casefold() in parts for name in policy.protected_directory_names):
        return "protected"
    if any(
        leaf.endswith(suffix.casefold()) for suffix in policy.protected_file_suffixes
    ):
        return "protected"
    return "ordinary"


def _require_plain_path(path: Path, root: Path) -> Path:
    root = root.resolve()
    try:
        relative = path.absolute().relative_to(root)
    except ValueError as exc:
        raise SoloAIError(f"Cleanup content is outside the managed worktree: {path}") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if is_link_or_junction(current):
            raise SoloAIError(f"Cleanup content is a link or junction: {current}")
    ensure_within(path, root)
    return path.absolute()


def require_managed_directory_identity(
    path: Path,
    *,
    managed_root: Path,
    expected_resolved: str | None = None,
    expected_root_resolved: str | None = None,
    expected_identity: dict[str, object] | None = None,
    expected_root_identity: dict[str, object] | None = None,
) -> Path:
    """按未解析路径复核受管目录，阻断 junction/symlink 替换。"""
    raw_root = managed_root.absolute()
    raw_path = path.absolute()
    try:
        relative = raw_path.relative_to(raw_root)
    except ValueError as exc:
        raise SoloAIError(f"Managed directory escaped its configured root: {path}") from exc
    if is_link_or_junction(raw_root):
        raise SoloAIError(f"Managed root became a link or junction: {raw_root}")
    current = raw_root
    for part in relative.parts:
        current = current / part
        if is_link_or_junction(current):
            raise SoloAIError(f"Managed directory became a link or junction: {current}")
    resolved_root = raw_root.resolve()
    resolved = raw_path.resolve()
    ensure_within(resolved, resolved_root)
    if expected_root_resolved and str(resolved_root) != expected_root_resolved:
        raise SoloAIError("Managed root identity changed")
    if expected_root_identity and path_identity(raw_root) != expected_root_identity:
        raise SoloAIError("Managed root directory object was replaced")
    if expected_resolved and str(resolved) != expected_resolved:
        raise SoloAIError("Managed directory identity changed")
    if expected_identity and path_identity(raw_path) != expected_identity:
        raise SoloAIError("Managed directory object was replaced")
    return resolved


def inspect_untracked(
    repo: GitRepo, *, cwd: Path, policy: CleanupPolicy = CleanupPolicy()
) -> dict[str, list[str]]:
    result = {
        "keep": [],
        "protected": [],
        "ordinary": [],
        "retained": [],
        "unknown_ignored": [],
    }
    ignored = set(repo.ignored_untracked(cwd))
    paths = sorted(set(repo.untracked(cwd)) | ignored)
    for relative in paths:
        _require_plain_path(cwd / relative, cwd)
        classification = classify_cleanup_path(relative, policy)
        if classification in {"keep", "protected"}:
            result[classification].append(relative)
        elif relative in ignored:
            parts = tuple(part.casefold() for part in Path(relative).parts)
            if any(part in KNOWN_RETAINED_ROOTS for part in parts) or Path(
                relative
            ).name.casefold() == "uv.toml":
                result["retained"].append(relative)
            else:
                result["unknown_ignored"].append(relative)
        else:
            result["ordinary"].append(relative)
    return result


def remove_abandoned_untracked(
    repo: GitRepo,
    *,
    cwd: Path,
    expected_ordinary: dict[str, dict[str, object]],
    policy: CleanupPolicy = CleanupPolicy(),
) -> None:
    inventory = inspect_untracked(repo, cwd=cwd, policy=policy)
    blocked = [
        *inventory["keep"],
        *inventory["protected"],
        *inventory["unknown_ignored"],
    ]
    if blocked:
        raise SoloAIError(
            "Retained, protected, or unknown ignored content blocks abandon:\n"
            + "\n".join(f"- {item}" for item in blocked[:20])
        )
    observed = {
        relative: snapshot_plain_path(_require_plain_path(cwd / relative, cwd))
        for relative in inventory["ordinary"]
    }
    if observed != expected_ordinary:
        raise SoloAIError("Ordinary untracked content changed during abandon")
    directories: set[Path] = set()
    for relative in inventory["ordinary"]:
        candidate = _require_plain_path(cwd / relative, cwd)
        if not candidate.exists() and not candidate.is_symlink():
            continue
        if classify_cleanup_path(relative, policy) != "ordinary":
            raise SoloAIError(f"Cleanup content changed classification: {relative}")
        if candidate.is_dir():
            for current, child_directories, files in os.walk(
                candidate, topdown=True, followlinks=False
            ):
                current_path = Path(current)
                for name in [*child_directories, *files]:
                    child = _require_plain_path(current_path / name, cwd)
                    child_relative = child.relative_to(cwd).as_posix()
                    if classify_cleanup_path(child_relative, policy) != "ordinary":
                        raise SoloAIError(
                            f"Protected or retained content is nested in cleanup target: {child_relative}"
                        )
            directories.add(candidate)
            continue
        delete_plain_path_if_unchanged(candidate, expected_ordinary[relative])
        parent = candidate.parent
        while parent != cwd:
            directories.add(parent)
            parent = parent.parent
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        _require_plain_path(directory, cwd)
        if directory.exists() and not any(directory.iterdir()):
            delete_plain_path_if_unchanged(
                directory, snapshot_plain_path(directory)
            )
    remaining = inspect_untracked(repo, cwd=cwd, policy=policy)
    blocked = [
        *remaining["keep"],
        *remaining["protected"],
        *remaining["unknown_ignored"],
        *remaining["ordinary"],
    ]
    if blocked:
        raise SoloAIError("Untracked content changed during abandon; files were preserved")
