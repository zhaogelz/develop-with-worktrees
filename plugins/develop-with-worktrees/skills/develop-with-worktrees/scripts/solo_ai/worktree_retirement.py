"""精确退役旧专用工作树；Git只移除已不存在目录的登记，不递归清用户内容。"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import batch_workspace
from .cleanup import (
    KNOWN_RETAINED_ROOTS,
    KNOWN_RECREATABLE_FILE_SUFFIXES,
    OPAQUE_RECREATABLE_ROOTS,
    classify_cleanup_path,
    _pinned_cleanup_ancestors,
    _require_plain_path,
    inspect_untracked,
    remove_recreatable_ignored,
    require_managed_directory_identity,
)
from .repo import GitRepo
from .util import (
    SoloAIError,
    atomic_write_json,
    delete_link_path_if_unchanged,
    delete_plain_path_if_unchanged,
    filesystem_path,
    is_link_or_junction,
    path_identity,
    pinned_plain_directory,
    read_json,
    sha256_file,
    snapshot_link_path,
    snapshot_plain_path,
)


FAST_RECEIPT_DIRECTORY = "fast-retirement-receipts"
FAST_RECREATABLE_ROOTS = frozenset(KNOWN_RETAINED_ROOTS)
FAST_OPAQUE_ROOTS = frozenset(OPAQUE_RECREATABLE_ROOTS)


def _fast_receipt_path(repo: GitRepo, batch: dict[str, Any]) -> Path:
    return repo.local_dir / FAST_RECEIPT_DIRECTORY / f"{batch['id']}.json"


def _fast_stage_path(batch: dict[str, Any]) -> Path:
    worktree = Path(str(batch["worktree"]))
    return worktree.parent / f".dww-fast-retire-{batch['id']}"


def _fast_registration(repo: GitRepo, worktree: Path, head: str) -> list[Any]:
    return [
        item
        for item in repo.worktrees()
        if item.path == worktree.resolve()
        and item.head == head
        and item.detached
        and not item.bare
    ]


def _fast_validate_open_root(path: Path, *, worktree: Path, policy: Any) -> None:
    """只检查开放式可再生根的名称；不读取或哈希其文件内容。"""
    if is_link_or_junction(path):
        raise SoloAIError(f"Fast retirement root is a link or junction: {path}")
    access_path = filesystem_path(path)
    if not access_path.is_dir():
        raise SoloAIError(f"Fast retirement root is not a directory: {path}")
    pending = [path]
    while pending:
        current = pending.pop()
        for entry in os.scandir(filesystem_path(current)):
            child = current / entry.name
            relative = child.relative_to(worktree).as_posix()
            if is_link_or_junction(child):
                # 只删除链接对象本身，绝不枚举其目标。
                continue
            if entry.is_dir(follow_symlinks=False):
                pending.append(child)
                continue
            classification = classify_cleanup_path(relative, policy)
            if classification in {"keep", "protected"}:
                raise SoloAIError(
                    f"Protected content blocks fast retirement: {relative}"
                )


def _fast_validate_untracked(repo: GitRepo, worktree: Path) -> dict[str, Any]:
    """验证快速退役的边界，不展开封闭依赖根，也不计算文件哈希。"""
    from .cleanup import CleanupPolicy

    policy = CleanupPolicy()
    ordinary = repo.untracked(worktree)
    if ordinary:
        raise SoloAIError(
            "Ordinary untracked content blocks fast retirement:\n"
            + "\n".join(f"- {item}" for item in ordinary[:20])
        )
    ignored = [
        item.rstrip("/") for item in repo.ignored_untracked(worktree, directories=True)
    ]
    ignored = [item for item in ignored if item]
    root_specs: dict[tuple[str, ...], tuple[tuple[str, ...], Path, str]] = {}
    for relative in ignored:
        parts = Path(relative).parts
        for root_index, part in enumerate(parts):
            root_name = part.casefold()
            if root_name not in FAST_RECREATABLE_ROOTS:
                continue
            root_parts = parts[: root_index + 1]
            folded_root_parts = tuple(value.casefold() for value in root_parts)
            if folded_root_parts in root_specs:
                continue
            root_relative = Path(*root_parts).as_posix()
            root = worktree.joinpath(*root_parts)
            _require_plain_path(root, worktree)
            if root_name not in FAST_OPAQUE_ROOTS:
                _fast_validate_open_root(root, worktree=worktree, policy=policy)
            root_specs[folded_root_parts] = (
                folded_root_parts,
                root,
                root_relative,
            )
    checked_roots = {spec[2] for spec in root_specs.values()}
    for relative in ignored:
        parts = tuple(value.casefold() for value in Path(relative).parts)
        if any(
            parts[: len(root_parts)] == root_parts or root_parts[: len(parts)] == parts
            for root_parts, _, _ in root_specs.values()
        ):
            continue
        leaf = Path(relative).name.casefold()
        if len(Path(relative).parts) == 1 and (
            leaf == "uv.toml"
            or any(leaf.endswith(suffix) for suffix in KNOWN_RECREATABLE_FILE_SUFFIXES)
        ):
            continue
        classification = classify_cleanup_path(relative, policy)
        if classification in {"keep", "protected"}:
            raise SoloAIError(
                f"Protected ignored content blocks fast retirement: {relative}"
            )
        raise SoloAIError(f"Unknown ignored content blocks fast retirement: {relative}")
    return {"ordinary_untracked": 0, "ignored_roots_checked": sorted(checked_roots)}


def _fast_remove_link(path: Path) -> None:
    """删除链接/junction 对象，不跟随目标。"""
    access_path = filesystem_path(path)
    if os.name == "nt" and access_path.is_dir():
        access_path.rmdir()
    else:
        access_path.unlink()


def _fast_remove_tree(path: Path) -> None:
    """递归删除已隔离目录；遇到链接只删除链接对象。"""
    if is_link_or_junction(path):
        _fast_remove_link(path)
        return
    access_path = filesystem_path(path)
    if not access_path.exists():
        return
    if not access_path.is_dir():
        access_path.unlink()
        return
    with os.scandir(access_path) as entries:
        children = [path / entry.name for entry in entries]
    for child in children:
        _fast_remove_tree(child)
    access_path.rmdir()


def _fast_write_receipt(
    repo: GitRepo,
    batch: dict[str, Any],
    *,
    stage: Path,
    preflight: dict[str, Any],
    started_at: str,
    completed_at: str,
) -> str:
    path = _fast_receipt_path(repo, batch)
    payload = {
        "schema_version": 1,
        "mode": "fast",
        "batch_id": batch["id"],
        "worktree": batch["worktree"],
        "staging_worktree": str(stage),
        "head": batch["integration_head"],
        "candidate_ids": list(batch.get("candidate_ids", [])),
        "preflight": preflight,
        "started_at": started_at,
        "completed_at": completed_at,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and read_json(path, None) != payload:
        raise SoloAIError("Existing fast-retirement receipt has different facts")
    if not path.exists():
        atomic_write_json(path, payload)
    return sha256_file(path)


def _identity(batch: dict[str, Any], worktree: Path) -> None:
    require_managed_directory_identity(
        worktree,
        managed_root=worktree.parent,
        expected_resolved=batch["worktree_resolved"],
        expected_root_resolved=batch["managed_root_resolved"],
        expected_identity=batch["worktree_identity"],
        expected_root_identity=batch["managed_root_identity"],
    )


def _manifest_path(repo: GitRepo, batch: dict[str, Any]) -> Path:
    return repo.local_dir / "worktree-removal-manifests" / f"{batch['id']}.json"


def _prepare(repo: GitRepo, batch: dict[str, Any]) -> dict[str, Any]:
    worktree = Path(batch["worktree"])
    _identity(batch, worktree)
    if (
        not repo.is_clean(worktree)
        or repo.branch(worktree) is not None
        or repo.head(worktree) != batch["integration_head"]
    ):
        raise SoloAIError("Retirement requires the exact clean detached Git head")
    remove_recreatable_ignored(repo, cwd=worktree)
    if any(inspect_untracked(repo, cwd=worktree).values()):
        raise SoloAIError("Untracked content arrived before worktree retirement")
    paths = [
        value
        for value in repo.git(["ls-files", "-z"], cwd=worktree).stdout.split("\0")
        if value
    ]
    directories = {".": snapshot_plain_path(worktree)}
    files: dict[str, Any] = {}
    for relative in [*paths, ".git"]:
        path = _require_plain_path(
            worktree / relative, worktree, allow_leaf_link=relative != ".git"
        )
        parent = path.parent
        ancestors = []
        while parent != worktree:
            ancestors.append(parent)
            parent = parent.parent
        for directory in reversed(ancestors):
            key = directory.relative_to(worktree).as_posix()
            if key not in directories:
                directories[key] = snapshot_plain_path(
                    _require_plain_path(directory, worktree)
                )
        if is_link_or_junction(path):
            files[relative] = snapshot_link_path(path)
        else:
            files[relative] = snapshot_plain_path(path)
            if files[relative]["kind"] != "file":
                raise SoloAIError(
                    "Tracked nested repositories require explicit retirement handling"
                )
    if (
        not repo.is_clean(worktree)
        or repo.head(worktree) != batch["integration_head"]
        or any(inspect_untracked(repo, cwd=worktree).values())
    ):
        raise SoloAIError(
            "Worktree changed while its exact retirement inventory was frozen"
        )
    _identity(batch, worktree)
    return {
        "schema_version": 1,
        "batch_id": batch["id"],
        "worktree": str(worktree),
        "head": batch["integration_head"],
        "directories": directories,
        "files": files,
    }


def _apply(repo: GitRepo, batch: dict[str, Any], manifest: dict[str, Any]) -> None:
    worktree = Path(batch["worktree"])
    directories = {
        worktree / path: value for path, value in manifest["directories"].items()
    }
    files = manifest["files"]
    exists = worktree.exists() or is_link_or_junction(worktree)
    registered = [entry for entry in repo.worktrees() if entry.path == worktree]
    if (exists or registered) and (
        len(registered) != 1
        or registered[0].head != manifest["head"]
        or not registered[0].detached
    ):
        raise SoloAIError("Worktree Git registration changed before exact retirement")
    if exists:
        _identity(batch, worktree)
        with (
            pinned_plain_directory(worktree.parent, batch["managed_root_identity"]),
            pinned_plain_directory(worktree, batch["worktree_identity"]),
        ):
            control = worktree / ".git"
            if control.exists() or is_link_or_junction(control):
                if snapshot_plain_path(control) != files[".git"]:
                    raise SoloAIError(
                        "Worktree control pointer changed before retirement"
                    )
            grouped: dict[Path, list[str]] = {}
            for relative in files:
                if relative != ".git":
                    grouped.setdefault((worktree / relative).parent, []).append(
                        relative
                    )
            for parent, relatives in grouped.items():
                _require_plain_path(parent, worktree)
                if not parent.exists():
                    continue
                with _pinned_cleanup_ancestors(worktree, parent, directories):
                    for relative in relatives:
                        path = worktree / relative
                        try:
                            filesystem_path(path).stat(follow_symlinks=False)
                        except FileNotFoundError:
                            continue
                        expected = files[relative]
                        if expected["kind"] == "link":
                            delete_link_path_if_unchanged(path, expected)
                        else:
                            delete_plain_path_if_unchanged(path, expected)
            for directory in sorted(
                (path for path in directories if path != worktree),
                key=lambda path: len(path.parts),
                reverse=True,
            ):
                _require_plain_path(directory, worktree)
                if not directory.exists():
                    continue
                with _pinned_cleanup_ancestors(worktree, directory.parent, directories):
                    with pinned_plain_directory(directory, directories[directory]):
                        empty = not any(filesystem_path(directory).iterdir())
                    if empty:
                        delete_plain_path_if_unchanged(
                            directory, directories[directory]
                        )
            # 元数据指针最后处理；未列入快照的条目永远不送入递归删除。
            unexpected = [
                item.name for item in worktree.iterdir() if item.name != ".git"
            ]
            if unexpected:
                raise SoloAIError(
                    "Late or changed content prevents final worktree removal: "
                    + ", ".join(unexpected[:20])
                )
            control = worktree / ".git"
            if control.exists() or is_link_or_junction(control):
                delete_plain_path_if_unchanged(control, files[".git"])
        # 原根句柄已关闭，父目录仍重新核验；条件删除只接受原空目录。
        with pinned_plain_directory(worktree.parent, batch["managed_root_identity"]):
            delete_plain_path_if_unchanged(worktree, directories[worktree])
    else:
        require_managed_directory_identity(
            worktree.parent,
            managed_root=worktree.parent,
            expected_root_identity=batch["managed_root_identity"],
            expected_resolved=batch["managed_root_resolved"],
        )
    if worktree.exists() or is_link_or_junction(worktree):
        raise SoloAIError("Retirement path reappeared; no Git removal was attempted")
    registered = [entry for entry in repo.worktrees() if entry.path == worktree]
    if registered:
        if (
            len(registered) != 1
            or registered[0].head != manifest["head"]
            or not registered[0].detached
        ):
            raise SoloAIError(
                "Missing worktree Git registration changed before retirement"
            )
        # 不把仍有源码/忽略文件的目录交给Git；原目录已通过逐对象空目录删除。
        repo.git(["worktree", "remove", str(worktree)])
    if (
        worktree.exists()
        or is_link_or_junction(worktree)
        or any(entry.path == worktree for entry in repo.worktrees())
    ):
        raise SoloAIError("Worktree retirement remains pending exact recovery")


def retire(repo: GitRepo, store: Any, batch: dict[str, Any]) -> dict[str, Any]:
    """使用现有批次内的收据指针恢复精确删除；不建立第二套回收状态库。"""
    if batch.get("worktree_mode", "dedicated") != "dedicated":
        raise SoloAIError(
            "Physical retirement cannot remove the reusable integration workspace"
        )
    if repo.root == Path(batch["worktree"]):
        raise SoloAIError("Run physical retirement from the stable base checkout")
    release = batch.get("runtime_release") or {}
    if int(batch.get("runtime_cycle", 0)) > 0 and not (
        release.get("configured") is False
        or (release.get("result") == "passed" and release.get("exit_code") == 0)
    ):
        raise SoloAIError("Runtime release is not confirmed before retirement")
    path = _manifest_path(repo, batch)
    digest = batch.get("worktree_removal_manifest_sha256")
    if not digest:
        manifest = _prepare(repo, batch)
        integration_ref = batch_workspace.remember_head(
            repo, batch, str(batch["integration_head"])
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        _require_plain_path(path, repo.local_dir)
        if path.exists():
            if read_json(path, None) != manifest:
                raise SoloAIError(
                    "Existing retirement manifest has a different exact inventory"
                )
        else:
            atomic_write_json(path, manifest)
        batch = store.update_batch(
            batch["id"],
            worktree_removal_manifest_sha256=sha256_file(path),
            integration_ref=integration_ref,
        )
    _require_plain_path(path, repo.local_dir)
    if sha256_file(path) != batch["worktree_removal_manifest_sha256"]:
        raise SoloAIError("Frozen worktree retirement manifest changed")
    manifest = read_json(path, None)
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 1
        or manifest.get("batch_id") != batch["id"]
        or manifest.get("worktree") != batch["worktree"]
        or manifest.get("head") != batch["integration_head"]
        or repo.ref_head(str(batch.get("integration_ref"))) != batch["integration_head"]
    ):
        raise SoloAIError("Retirement inventory or durable Git result identity changed")
    _apply(repo, batch, manifest)
    return store.batch(batch["id"])


def retire_fast(repo: GitRepo, store: Any, batch: dict[str, Any]) -> dict[str, Any]:
    """快速退役一个已失败专用批次，跳过依赖文件内容哈希。"""
    if batch.get("worktree_mode", "dedicated") == "reusable":
        raise SoloAIError("Fast physical retirement cannot remove a reusable workspace")
    worktree = Path(str(batch["worktree"]))
    stage = _fast_stage_path(batch)
    parent = worktree.parent
    # 快速路径必须有自己的意图记录；不能把旧的慢速退役时间戳误当成
    # 快速退役已开始，避免在恢复时认领一个来源不明的缺失目录。
    started_at = str(batch.get("fast_retirement_started_at") or "")
    if batch.get("worktree_retired_at"):
        if (
            worktree.exists()
            or stage.exists()
            or _fast_registration(repo, worktree, str(batch["integration_head"]))
        ):
            raise SoloAIError("Fast-retired batch worktree unexpectedly reappeared")
        return store.batch(batch["id"])

    _identity(
        batch, worktree
    ) if worktree.exists() else require_managed_directory_identity(
        parent,
        managed_root=parent,
        expected_resolved=batch["managed_root_resolved"],
        expected_root_resolved=batch["managed_root_resolved"],
        expected_root_identity=batch["managed_root_identity"],
    )
    if is_link_or_junction(stage):
        raise SoloAIError(
            f"Fast-retirement staging path is a link or junction: {stage}"
        )
    if worktree.exists() and not stage.exists():
        if not _fast_registration(repo, worktree, str(batch["integration_head"])):
            raise SoloAIError(
                "Fast retirement requires the exact registered detached worktree"
            )
        if (
            repo.branch(worktree) is not None
            or repo.head(worktree) != batch["integration_head"]
        ):
            raise SoloAIError(
                "Fast retirement requires the exact clean detached Git head"
            )
        if not repo.is_clean(worktree, include_untracked=False):
            raise SoloAIError("Tracked changes block fast retirement")
        preflight = _fast_validate_untracked(repo, worktree)
        started_at = started_at or datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        store.update_batch(
            batch["id"],
            fast_retirement_started_at=started_at,
            fast_retirement_preflight=preflight,
        )
        with pinned_plain_directory(parent, batch["managed_root_identity"]):
            if stage.exists():
                raise SoloAIError("Fast-retirement staging path appeared before rename")
            worktree.rename(stage)
        if not stage.exists() or is_link_or_junction(stage):
            raise SoloAIError(
                "Fast-retirement staging rename did not reach the expected path"
            )
        stage_identity = path_identity(stage)
        store.update_batch(
            batch["id"],
            fast_retirement_staging=str(stage),
            fast_retirement_stage_identity=stage_identity,
        )
        batch = store.batch(batch["id"])
    elif not stage.exists():
        if not started_at:
            raise SoloAIError(
                "Fast-retirement worktree disappeared before intent was recorded"
            )
        if _fast_registration(repo, worktree, str(batch["integration_head"])):
            raise SoloAIError(
                "Fast-retirement worktree is missing but remains registered"
            )

    if not stage.exists():
        if batch.get("fast_retirement_staging") != str(stage) or not batch.get(
            "fast_retirement_stage_identity"
        ):
            raise SoloAIError(
                "Fast-retirement staging facts are missing; refusing to infer completion"
            )
        registered = _fast_registration(repo, worktree, str(batch["integration_head"]))
        if registered:
            repo.git(["worktree", "remove", str(worktree)])
        if worktree.exists() or _fast_registration(
            repo, worktree, str(batch["integration_head"])
        ):
            raise SoloAIError(
                "Fast-retirement registration did not reach a terminal state"
            )
        receipt_sha = _fast_write_receipt(
            repo,
            batch,
            stage=stage,
            preflight=batch.get("fast_retirement_preflight") or {},
            started_at=started_at,
            completed_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        )
        return store.update_batch(
            batch["id"],
            fast_retirement_receipt_sha256=receipt_sha,
            worktree_retired_at=datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        )

    if worktree.exists() and stage.exists():
        raise SoloAIError("Both original and staging worktrees exist")
    if not batch.get("fast_retirement_stage_identity"):
        store.update_batch(
            batch["id"], fast_retirement_stage_identity=path_identity(stage)
        )
        batch = store.batch(batch["id"])
    expected_stage = batch.get("fast_retirement_stage_identity")
    if expected_stage and path_identity(stage) != expected_stage:
        raise SoloAIError("Fast-retirement staging directory was replaced")
    _fast_remove_tree(stage)
    if stage.exists() or is_link_or_junction(stage):
        raise SoloAIError("Fast-retirement staging directory remains")
    registered = _fast_registration(repo, worktree, str(batch["integration_head"]))
    if registered:
        repo.git(["worktree", "remove", str(worktree)])
    if worktree.exists() or _fast_registration(
        repo, worktree, str(batch["integration_head"])
    ):
        raise SoloAIError("Fast-retirement registration did not reach a terminal state")
    completed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    receipt_sha = _fast_write_receipt(
        repo,
        batch,
        stage=stage,
        preflight=batch.get("fast_retirement_preflight") or {},
        started_at=started_at,
        completed_at=completed_at,
    )
    return store.update_batch(
        batch["id"],
        fast_retirement_receipt_sha256=receipt_sha,
        worktree_retired_at=completed_at,
    )
