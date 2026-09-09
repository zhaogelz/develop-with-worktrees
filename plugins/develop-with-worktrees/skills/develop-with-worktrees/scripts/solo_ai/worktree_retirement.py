"""精确退役旧专用工作树；Git只移除已不存在目录的登记，不递归清用户内容。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import batch_workspace
from .cleanup import (
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
    pinned_plain_directory,
    read_json,
    sha256_file,
    snapshot_link_path,
    snapshot_plain_path,
)


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
