from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

from .repo import GitRepo
from .util import (
    SoloAIError,
    delete_link_path_if_unchanged,
    delete_plain_path_if_unchanged,
    ensure_within,
    filesystem_path,
    is_link_or_junction,
    path_identity,
    pinned_plain_directory,
    snapshot_plain_path,
    snapshot_link_path,
    snapshot_recreatable_file,
)

KNOWN_RETAINED_ROOTS = {
    ".venv",
    "node_modules",
    ".tmp",
    ".cache",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    ".swc",
}
KNOWN_RECREATABLE_FILE_SUFFIXES = (".tsbuildinfo",)

# 这些根目录完全由依赖锁或工具输出生成。依赖内容可以合法包含
# ``storage`` 或 ``*.db`` 等名称，这些名称不会把生成依赖变成项目数据。
# 开放式缓存根不在此集合中，其受保护后代仍会阻止自动清理。
OPAQUE_RECREATABLE_ROOTS = {
    ".venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
}


@dataclass(frozen=True)
class CleanupPolicy:
    protected_directory_names: tuple[str, ...] = ("uploads", "storage")
    protected_file_suffixes: tuple[str, ...] = (".db", ".sqlite", ".sqlite3")


def classify_cleanup_path(
    relative: str, policy: CleanupPolicy = CleanupPolicy()
) -> str:
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


def _require_plain_path(
    path: Path, root: Path, *, allow_leaf_link: bool = False
) -> Path:
    root = root.resolve()
    try:
        relative = path.absolute().relative_to(root)
    except ValueError as exc:
        raise SoloAIError(
            f"Cleanup content is outside the managed worktree: {path}"
        ) from exc
    current = root
    for part in relative.parts:
        current = current / part
        if is_link_or_junction(current):
            # 依赖叶链接也必须先逐级核对所有祖先，不提前访问目标后代。
            if allow_leaf_link and current == path.absolute():
                snapshot_link_path(current)
                return current
            raise SoloAIError(f"Cleanup content is a link or junction: {current}")
    # root 已在本次检查开头解析；再次解析同一根既重复开销，也可能
    # 把检查期间被替换的根重新接受为新的边界。只与原边界比较。
    try:
        path.resolve().relative_to(root)
    except ValueError as exc:
        raise SoloAIError(f"Refusing path outside managed root: {path}") from exc
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
        raise SoloAIError(
            f"Managed directory escaped its configured root: {path}"
        ) from exc
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


def _opaque_root(relative: str) -> str | None:
    parts = Path(relative).parts
    for index, part in enumerate(parts):
        if part.casefold() in OPAQUE_RECREATABLE_ROOTS:
            return Path(*parts[: index + 1]).as_posix()
    return None


def _require_inventory_path(path: Path, cwd: Path, *, ignored: bool) -> Path:
    relative = path.absolute().relative_to(cwd.resolve()).as_posix()
    opaque = _opaque_root(relative)
    # 只允许真实依赖根之内的叶链接；根或任一祖先链接仍被拒绝。
    return _require_plain_path(
        path,
        cwd,
        allow_leaf_link=ignored and opaque is not None and relative != opaque,
    )


def _ignored_inventory(
    repo: GitRepo, *, cwd: Path, expand_dependencies: bool
) -> set[str]:
    # Git for Windows 会递归 junction；先折叠全忽略目录，再自行不跟随链接遍历。
    pending = [
        cwd / value.rstrip("/")
        for value in repo.ignored_untracked(cwd, directories=True)
    ]
    result: set[str] = set()
    while pending:
        path = _require_inventory_path(pending.pop(), cwd, ignored=True)
        relative = path.relative_to(cwd).as_posix()
        if is_link_or_junction(path):
            result.add(relative)
        elif filesystem_path(path).is_dir():
            if _opaque_root(relative) is not None and not expand_dependencies:
                result.add(relative)
                continue
            # 枚举使用扩展路径，库存仍保留原逻辑路径，不能把前缀写入持久身份。
            children = [path / child.name for child in filesystem_path(path).iterdir()]
            if children:
                pending.extend(children)
            else:
                result.add(relative)
        else:
            result.add(relative)
    return result


def inspect_untracked(
    repo: GitRepo,
    *,
    cwd: Path,
    policy: CleanupPolicy = CleanupPolicy(),
    expand_dependencies: bool = False,
) -> dict[str, list[str]]:
    result = {
        "keep": [],
        "protected": [],
        "ordinary": [],
        "retained": [],
        "unknown_ignored": [],
    }
    ignored = _ignored_inventory(repo, cwd=cwd, expand_dependencies=expand_dependencies)
    paths = sorted(set(repo.untracked(cwd)) | ignored)
    for relative in paths:
        # ignored 已由不跟随链接的库存遍历逐项核验；分类不访问文件系统。
        # 删除前仍会重新清点、冻结对象，并在删除时复核祖先及对象身份。
        if relative not in ignored:
            _require_inventory_path(cwd / relative, cwd, ignored=False)
        parts = tuple(part.casefold() for part in Path(relative).parts)
        opaque = _opaque_root(relative) if relative in ignored else None
        # 依赖根内的生成名称可不透明；根外的 uploads/storage 不能被遮蔽。
        outer_classification = (
            classify_cleanup_path(opaque, policy) if opaque else "ordinary"
        )
        if opaque is not None and outer_classification == "ordinary":
            result["retained"].append(relative)
            continue
        classification = classify_cleanup_path(relative, policy)
        if classification in {"keep", "protected"}:
            result[classification].append(relative)
        elif relative in ignored:
            if (
                any(part in KNOWN_RETAINED_ROOTS for part in parts)
                or any(
                    Path(relative).name.casefold().endswith(suffix)
                    for suffix in KNOWN_RECREATABLE_FILE_SUFFIXES
                )
                or Path(relative).name.casefold() == "uv.toml"
            ):
                result["retained"].append(relative)
            else:
                result["unknown_ignored"].append(relative)
        else:
            result["ordinary"].append(relative)
    return result


def remove_recreatable_ignored(
    repo: GitRepo,
    *,
    cwd: Path,
    policy: CleanupPolicy = CleanupPolicy(),
) -> None:
    """按对象身份逐项删除已知可再生忽略文件，保留任何晚到内容。"""
    # 根和父目录在整个操作期间保持原对象；不锁整个仓库或用户目录树。
    parent_identity = snapshot_plain_path(cwd.parent)
    root_identity = snapshot_plain_path(cwd)
    with (
        pinned_plain_directory(cwd.parent, parent_identity),
        pinned_plain_directory(cwd, root_identity),
    ):
        _remove_recreatable_contents(repo, cwd=cwd, policy=policy)


@contextmanager
def _pinned_cleanup_ancestors(
    cwd: Path, parent: Path, directories: dict[Path, dict[str, object]]
) -> Iterator[None]:
    """每组同父目录文件共用一组有界句柄；先保护祖先，再访问后代。"""
    relative = parent.relative_to(cwd)
    with ExitStack() as stack:
        current = cwd
        stack.enter_context(pinned_plain_directory(current, directories[current]))
        for part in relative.parts:
            current /= part
            stack.enter_context(pinned_plain_directory(current, directories[current]))
        yield


def _remove_recreatable_contents(
    repo: GitRepo, *, cwd: Path, policy: CleanupPolicy
) -> None:

    inventory = inspect_untracked(
        repo, cwd=cwd, policy=policy, expand_dependencies=True
    )
    blocked = [
        *inventory["keep"],
        *inventory["protected"],
        *inventory["ordinary"],
        *inventory["unknown_ignored"],
    ]
    if blocked:
        raise SoloAIError(
            "Protected or unknown content blocks recreatable cleanup:\n"
            + "\n".join(f"- {item}" for item in blocked[:20])
        )

    expected_files: dict[str, dict[str, object]] = {}
    directories: dict[Path, dict[str, object]] = {cwd: snapshot_plain_path(cwd)}
    files_by_parent: dict[Path, list[str]] = {}
    for relative in inventory["retained"]:
        candidate = _require_inventory_path(cwd / relative, cwd, ignored=True)
        # 先冻结所有原祖先，不在删除空目录时重新收养同路径的新对象。
        parent = candidate.parent
        ancestors: list[Path] = []
        while parent not in directories:
            ancestors.append(parent)
            parent = parent.parent
        for directory in reversed(ancestors):
            directories[directory] = snapshot_plain_path(
                _require_plain_path(directory, cwd)
            )
        if is_link_or_junction(candidate):
            expected_files[relative] = snapshot_link_path(candidate)
        elif filesystem_path(candidate).is_dir():
            directories[candidate] = snapshot_plain_path(candidate)
            continue
        elif _opaque_root(relative) is not None:
            expected_files[relative] = snapshot_recreatable_file(candidate)
        else:
            expected_files[relative] = snapshot_plain_path(candidate)
        files_by_parent.setdefault(candidate.parent, []).append(relative)

    # 清单之外的新条目从不加入本次删除；无需再展开全树比较名称。
    for parent, relatives in files_by_parent.items():
        with _pinned_cleanup_ancestors(cwd, parent, directories):
            for relative in relatives:
                candidate = cwd / relative
                expected = expected_files[relative]
                if expected["kind"] == "link":
                    delete_link_path_if_unchanged(candidate, expected)
                else:
                    delete_plain_path_if_unchanged(candidate, expected)

    for directory in sorted(
        (path for path in directories if path != cwd),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        with _pinned_cleanup_ancestors(cwd, directory.parent, directories):
            with pinned_plain_directory(directory, directories[directory]):
                empty = not any(filesystem_path(directory).iterdir())
            if empty:
                delete_plain_path_if_unchanged(directory, directories[directory])

    remaining = inspect_untracked(
        repo, cwd=cwd, policy=policy, expand_dependencies=False
    )
    if any(remaining.values()):
        raise SoloAIError(
            "Untracked content changed during recreatable cleanup; files were preserved"
        )


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
    for directory in sorted(
        directories, key=lambda item: len(item.parts), reverse=True
    ):
        _require_plain_path(directory, cwd)
        if directory.exists() and not any(directory.iterdir()):
            delete_plain_path_if_unchanged(directory, snapshot_plain_path(directory))
    remaining = inspect_untracked(repo, cwd=cwd, policy=policy)
    blocked = [
        *remaining["keep"],
        *remaining["protected"],
        *remaining["unknown_ignored"],
        *remaining["ordinary"],
    ]
    if blocked:
        raise SoloAIError(
            "Untracked content changed during abandon; files were preserved"
        )
