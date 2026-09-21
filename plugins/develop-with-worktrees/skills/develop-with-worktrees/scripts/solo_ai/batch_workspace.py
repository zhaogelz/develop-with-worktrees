from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from .cleanup import inspect_untracked, require_managed_directory_identity
from .repo import GitRepo
from .util import (
    ActionableSoloAIError,
    SoloAIError,
    is_link_or_junction,
    path_identity,
    utc_timestamp,
)


class BatchWorkspacePending(ActionableSoloAIError):
    """位置事实不明确时保留现场；不能标成普通失败后另占一个目录。"""

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            code="OWNERSHIP_DRIFT",
            context={"scope": "integration_workspace"},
            next_action={"kind": "preserve_and_inspect_workspace_ownership"},
        )


def _generation(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise BatchWorkspacePending("Invalid integration workspace generation")
    return value


def _location(worktree: Path) -> dict[str, Any]:
    return {
        "worktree": str(worktree),
        "worktree_resolved": str(worktree.resolve()),
        "worktree_identity": path_identity(worktree),
        "managed_root_resolved": str(worktree.parent.resolve()),
        "managed_root_identity": path_identity(worktree.parent),
    }


def _check_directory(worktree: Path, record: dict[str, Any]) -> None:
    if str(worktree) != record.get("worktree"):
        raise BatchWorkspacePending("Integration workspace path changed")
    if any(not record.get(key) for key in _LOCATION_KEYS):
        raise BatchWorkspacePending("Integration workspace identity is incomplete")
    try:
        require_managed_directory_identity(
            worktree,
            managed_root=worktree.parent,
            expected_resolved=record["worktree_resolved"],
            expected_root_resolved=record["managed_root_resolved"],
            expected_identity=record["worktree_identity"],
            expected_root_identity=record["managed_root_identity"],
        )
    except (OSError, SoloAIError) as exc:
        raise BatchWorkspacePending(
            "Integration workspace directory changed or disappeared"
        ) from exc


def require_retained_contents(repo: GitRepo, worktree: Path) -> None:
    """归还和复用只检查保留边界，不展开依赖，不删除任何内容。"""
    try:
        inventory = inspect_untracked(repo, cwd=worktree, expand_dependencies=False)
    except (OSError, SoloAIError) as exc:
        raise BatchWorkspacePending(
            f"Workspace retention facts are unreadable: {exc}"
        ) from exc
    blocked = sorted(
        {
            *inventory["keep"],
            *inventory["protected"],
            *inventory["ordinary"],
            *inventory["unknown_ignored"],
        }
    )
    if blocked:
        raise BatchWorkspacePending(
            "Protected or unknown content prevents workspace return/reuse:\n"
            + "\n".join(f"- {item}" for item in blocked[:20])
        )


def _check_git(repo: GitRepo, worktree: Path, expected_head: str) -> None:
    if not any(item.path == worktree for item in repo.worktrees()):
        raise BatchWorkspacePending("Integration workspace registration is missing")
    branch = repo.branch(worktree)
    if branch is not None:
        raise BatchWorkspacePending(
            f"Integration workspace is attached to branch {branch}"
        )
    actual_head = repo.head(worktree)
    if actual_head != expected_head:
        raise BatchWorkspacePending(
            "Integration workspace HEAD changed: "
            f"expected {expected_head}, found {actual_head}"
        )
    if not repo.is_clean(worktree):
        raise BatchWorkspacePending("Integration workspace contains uncommitted changes")
    common = repo.git(
        ["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=worktree
    ).stdout.strip()
    if Path(common).resolve() != repo.common_dir:
        raise BatchWorkspacePending(
            "Integration workspace belongs to another repository"
        )


def require_owner(repo: GitRepo, store: Any, batch: dict[str, Any]) -> dict[str, Any]:
    """每次操作前比较持有者及代次，路径相同不构成权限。"""
    record = store.read().get("integration_workspace")
    if (
        not isinstance(record, dict)
        or record.get("owner") != batch["id"]
        or _generation(record.get("generation"))
        != _generation(batch.get("worktree_generation"))
        or batch.get("worktree_released_at")
    ):
        raise BatchWorkspacePending("Batch no longer owns this workspace generation")
    for key in _LOCATION_KEYS:
        if record.get(key) != batch.get(key):
            raise BatchWorkspacePending("Batch workspace binding changed")
    _check_directory(Path(str(batch["worktree"])), record)
    return record


_LOCATION_KEYS = (
    "worktree",
    "worktree_resolved",
    "worktree_identity",
    "managed_root_resolved",
    "managed_root_identity",
)


def remember_head(repo: GitRepo, batch: dict[str, Any], head: str) -> str:
    """候选组合的提交不能只靠JSON中的SHA存活。"""
    ref = f"refs/dww/batch-heads/{batch['id']}"
    existing = repo.ref_head(ref)
    if existing not in {None, batch["integration_head"], head}:
        raise BatchWorkspacePending("Persistent batch head reference changed")
    if existing != head:
        repo.git(["update-ref", ref, head, existing or "0" * len(head)])
    return ref


def _require_saved_idle_head(repo: GitRepo, record: dict[str, Any]) -> None:
    head = record.get("head")
    ref = record.get("head_ref")
    if (
        not isinstance(head, str)
        or not isinstance(ref, str)
        or repo.ref_head(ref) != head
    ):
        raise BatchWorkspacePending("Idle workspace has no exact persistent Git result")


def acquire(
    repo: GitRepo, store: Any, batch: dict[str, Any], worktree: Path
) -> dict[str, Any]:
    """在现有集成锁内领取唯一位置；保留依赖，非强制切换源码基线。"""
    try:
        return _acquire(repo, store, batch, worktree)
    except BatchWorkspacePending:
        raise
    except (OSError, SoloAIError) as exc:
        # 文件占用或登记故障不是候选失败；保持原阶段/原代次可恢复。
        raise BatchWorkspacePending(
            "Integration workspace admission is pending exact recovery"
        ) from exc


def _acquire(
    repo: GitRepo, store: Any, batch: dict[str, Any], worktree: Path
) -> dict[str, Any]:
    prior = store.read().get("integration_workspace")
    if prior is not None and not isinstance(prior, dict):
        raise BatchWorkspacePending("Invalid integration workspace binding")
    if prior and prior.get("owner") == batch["id"]:
        require_owner(repo, store, batch)
        return _prepare_checkout(repo, store, batch, prior)
    if prior and prior.get("owner") is not None:
        raise BatchWorkspacePending(
            f"Integration workspace is still held by {prior.get('owner')}"
        )
    if batch.get("worktree_generation") or batch.get("worktree_released_at"):
        raise BatchWorkspacePending(
            "A previous batch cannot reacquire a new generation"
        )

    registered = any(item.path == worktree for item in repo.worktrees())
    exists = worktree.exists() or is_link_or_junction(worktree)
    creating = not exists
    if prior:
        if str(worktree) != prior.get("worktree"):
            raise BatchWorkspacePending("Configured integration workspace moved")
        _generation(prior.get("generation"))
        require_managed_directory_identity(
            worktree.parent,
            managed_root=worktree.parent,
            expected_resolved=prior["managed_root_resolved"],
            expected_root_identity=prior["managed_root_identity"],
        )
        _require_saved_idle_head(repo, prior)
        if exists:
            _check_directory(worktree, prior)
            _check_git(repo, worktree, str(prior["head"]))
            require_retained_contents(repo, worktree)
        elif registered:
            # 只修复这个已确认空闲且成果独立保存的缺失位置；不用全局prune。
            repo.git(["worktree", "remove", str(worktree)])
            if any(item.path == worktree for item in repo.worktrees()):
                raise BatchWorkspacePending(
                    "Missing idle registration was not repaired"
                )
    elif exists or registered:
        raise BatchWorkspacePending("Unowned integration workspace already exists")

    if creating:
        require_managed_directory_identity(
            worktree.parent, managed_root=worktree.parent
        )
        worktree.mkdir()
    location = _location(worktree)
    generation = _generation(prior["generation"]) + 1 if prior else 1
    record = {
        **location,
        "generation": generation,
        "owner": batch["id"],
        "head": prior["head"] if prior and not creating else batch["base_before"],
        "head_ref": prior.get("head_ref") if prior else None,
        "registering": creating,
    }

    def reserve(value: dict[str, Any]) -> dict[str, Any]:
        if value.get("integration_workspace") != prior:
            raise BatchWorkspacePending("Workspace ownership changed before admission")
        current = value["batches"][batch["id"]]
        if current.get("worktree_generation") or current.get("status") != "sealed":
            raise BatchWorkspacePending("Batch changed before workspace admission")
        value["integration_workspace"] = copy.deepcopy(record)
        current.update(
            {
                **location,
                "worktree_generation": generation,
                "worktree_checkout_from": record["head"],
            }
        )
        return copy.deepcopy(current)

    reserved = store.mutate(reserve)
    return _prepare_checkout(repo, store, reserved, record)


def _prepare_checkout(
    repo: GitRepo, store: Any, batch: dict[str, Any], record: dict[str, Any]
) -> dict[str, Any]:
    worktree = Path(str(batch["worktree"]))
    require_owner(repo, store, batch)
    registered = any(item.path == worktree for item in repo.worktrees())
    if record.get("registering"):
        if not registered:
            if any(worktree.iterdir()):
                raise BatchWorkspacePending(
                    "Unregistered workspace contains unknown data"
                )
            repo.git(
                ["worktree", "add", "--detach", str(worktree), batch["base_before"]]
            )
        _check_git(repo, worktree, str(batch["base_before"]))
    elif not registered:
        raise BatchWorkspacePending(
            "Active integration workspace registration is missing"
        )

    head = repo.head(worktree)
    target = str(batch["integration_head"])
    if head != target:
        if (
            batch["status"] != "sealed"
            or batch.get("applied_candidate_ids")
            or head != batch["worktree_checkout_from"]
        ):
            raise BatchWorkspacePending(
                "Interrupted workspace checkout has ambiguous Git facts"
            )
        _check_git(repo, worktree, head)
        require_retained_contents(repo, worktree)
        repo.git(
            ["checkout", "--detach", "--no-overwrite-ignore", target], cwd=worktree
        )
    _check_git(repo, worktree, target)
    require_retained_contents(repo, worktree)
    ref = remember_head(repo, batch, target)

    def prepared(value: dict[str, Any]) -> dict[str, Any]:
        current = value["integration_workspace"]
        if current != record:
            raise BatchWorkspacePending("Workspace binding changed during checkout")
        current.update({"registering": False, "head": target, "head_ref": ref})
        stored = value["batches"][batch["id"]]
        stored["integration_ref"] = ref
        return copy.deepcopy(stored)

    return store.mutate(prepared)


def return_workspace(
    repo: GitRepo, store: Any, batch: dict[str, Any]
) -> dict[str, Any]:
    """成果、资源和内容确认后归还使用权，不物理删除位置或依赖。"""
    try:
        return _return_workspace(repo, store, batch)
    except BatchWorkspacePending:
        raise
    except (OSError, SoloAIError) as exc:
        raise BatchWorkspacePending(
            "Integration workspace return is pending exact recovery"
        ) from exc


def _return_workspace(
    repo: GitRepo, store: Any, batch: dict[str, Any]
) -> dict[str, Any]:
    if batch.get("worktree_released_at"):
        return batch
    record = require_owner(repo, store, batch)
    release = batch.get("runtime_release") or {}
    if int(batch.get("runtime_cycle", 0)) > 0 and not (
        release.get("configured") is False
        or (release.get("result") == "passed" and release.get("exit_code") == 0)
    ):
        raise BatchWorkspacePending(
            "Runtime release is not confirmed; workspace retained"
        )
    worktree = Path(str(batch["worktree"]))
    _check_git(repo, worktree, str(batch["integration_head"]))
    require_retained_contents(repo, worktree)
    ref = remember_head(repo, batch, str(batch["integration_head"]))

    def returned(value: dict[str, Any]) -> dict[str, Any]:
        current = value["integration_workspace"]
        if current != record:
            raise BatchWorkspacePending("Workspace owner changed before return")
        current.update(
            {"owner": None, "head": batch["integration_head"], "head_ref": ref}
        )
        stored = value["batches"][batch["id"]]
        stored.update({"integration_ref": ref, "worktree_released_at": utc_timestamp()})
        return copy.deepcopy(stored)

    return store.mutate(returned)


def context_binding(repo: GitRepo, store: Any, batch: dict[str, Any]) -> dict[str, Any]:
    require_owner(repo, store, batch)
    return {
        "mode": "reusable",
        "owner": batch["id"],
        "generation": batch["worktree_generation"],
        **{key: copy.deepcopy(batch[key]) for key in _LOCATION_KEYS},
    }
