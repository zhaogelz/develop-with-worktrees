"""原生 task-head 批次：冻结来源、真实合并、验证和精确交付。"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from . import batch_workspace
from .candidate_batches import _require_approval, _run_secret_scanner
from .cleanup import inspect_untracked
from .config import load_repo_config, load_verification_config
from .integration import integration_turn
from .proof import new_validation_attempt_id, read_validation_attempt, validate
from .repo import GitRepo
from .safety import require_safe
from .state import STATE_SCHEMA, StateStore, candidate_admission_lock
from .util import ActionableSoloAIError, SoloAIError, read_json, utc_timestamp


_TAIL_CAUSES = {"round-complete", "user", "dependency", "deploy"}


def seal_native_batch(
    repo: GitRepo,
    *,
    task_ids: list[str],
    cause: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """只冻结已 Finish 的同目标任务；小批必须有明确原因。"""

    from .lifecycle import maintenance_lock

    if not task_ids or len(set(task_ids)) != len(task_ids):
        raise SoloAIError("Native batch requires unique task ids")
    with maintenance_lock(repo), candidate_admission_lock(repo):
        store = StateStore(repo)
        state = store.read()
        if state["schema_version"] != STATE_SCHEMA:
            raise SoloAIError("Native task delivery has not been enabled")
        config = load_repo_config(repo)
        if len(task_ids) > config.integration.batch_size:
            raise SoloAIError("Native batch exceeds configured size")
        if len(task_ids) < config.integration.batch_size:
            if cause not in _TAIL_CAUSES or not reason or "\n" in reason:
                raise SoloAIError("Native tail requires an explicit cause and reason")
        elif cause is not None and cause not in _TAIL_CAUSES:
            raise SoloAIError("Unsupported native batch cause")
        tasks = []
        for task_id in task_ids:
            task = state["tasks"].get(task_id)
            if (
                not isinstance(task, dict)
                or task.get("status") != "waiting-integration"
            ):
                raise SoloAIError("Native batch member is not waiting for integration")
            tasks.append(task)
        target = str(tasks[0]["base_ref"])
        if any(task.get("base_ref") != target for task in tasks):
            raise SoloAIError("Native batch cannot mix target branches")
        if cause == "round-complete" and any(
            task.get("base_ref") == target
            and task.get("status") in {"starting", "active"}
            for task in state["tasks"].values()
        ):
            raise SoloAIError("Round still has active development producers")
        base_head = repo.ref_head(f"refs/heads/{target}")
        if base_head is None:
            raise SoloAIError("Native batch target branch disappeared")
        members: list[dict[str, Any]] = []
        for task in tasks:
            source = str((task.get("native_delivery") or {}).get("ready_head") or "")
            worktree = Path(str(task["worktree"])).resolve()
            if (
                not source
                or repo.branch(worktree) != task.get("branch")
                or repo.head(worktree) != source
                or repo.ref_head(f"refs/heads/{task['branch']}") != source
                or not repo.is_clean(worktree)
                or not repo.is_ancestor(str(task["base_head"]), base_head)
            ):
                raise SoloAIError("Native batch member moved after Ready")
            members.append(
                {
                    "task_id": str(task["id"]),
                    "slot_id": str(task["slot_id"]),
                    "slot_generation": task["slot_generation"],
                    "branch": str(task["branch"]),
                    "ready_head": source,
                    "source_base": str(task["base_head"]),
                }
            )
        batch_id = f"batch-native-{uuid.uuid4().hex[:16]}"
        worktree = store.managed_worktree_root(config) / "solo-ai-integration"
        batch = {
            "id": batch_id,
            "schema_version": 1,
            "status": "sealed",
            "base_ref": target,
            "base_before": base_head,
            "base_head": base_head,
            "integration_head": base_head,
            "worktree": str(worktree.absolute()),
            "worktree_mode": "reusable",
            "runtime_cycle": 0,
            "tasks": members,
            "applied_task_ids": [],
            "merge_records": [],
            "merge_intent": None,
            "tail_request": {"cause": cause, "reason": reason} if cause else None,
            "created_at": utc_timestamp(),
        }
        return store.seal_native_batch(batch)


def _verify_member(repo: GitRepo, store: StateStore, member: dict[str, Any]) -> None:
    task = store.task(str(member["task_id"]))
    path = Path(str(task["worktree"])).resolve()
    slot = store.read()["slots"].get(str(member["slot_id"]))
    if (
        task.get("status") != "integrating"
        or task.get("slot_generation") != member["slot_generation"]
        or task.get("branch") != member["branch"]
        or (task.get("native_delivery") or {}).get("ready_head") != member["ready_head"]
        or slot is None
        or slot.get("task_id") != task["id"]
        or slot.get("generation") != member["slot_generation"]
        or repo.branch(path) != member["branch"]
        or repo.head(path) != member["ready_head"]
        or repo.ref_head(f"refs/heads/{member['branch']}") != member["ready_head"]
        or not repo.is_clean(path)
    ):
        raise SoloAIError("Frozen native task or slot changed during integration")


def _record_merge_result(
    repo: GitRepo, store: StateStore, batch: dict[str, Any]
) -> dict[str, Any]:
    intent = batch.get("merge_intent")
    if not isinstance(intent, dict):
        raise SoloAIError("Native merge has no durable intent")
    worktree = Path(str(batch["worktree"]))
    head = repo.head(worktree)
    parents = repo.git(["rev-list", "--parents", "-n", "1", head], cwd=worktree)
    parts = parents.stdout.strip().split()
    if parts != [head, intent["previous_head"], intent["source_head"]]:
        raise SoloAIError("Native merge result does not retain exact Git parents")
    if not repo.is_clean(worktree):
        raise SoloAIError("Native merge worktree is not clean")
    batch_workspace.remember_head(repo, batch, head)
    records = [*batch.get("merge_records", [])]
    records.append(
        {
            "task_id": intent["task_id"],
            "source_head": intent["source_head"],
            "previous_head": intent["previous_head"],
            "merge_head": head,
        }
    )
    return store.update_batch(
        str(batch["id"]),
        status="composing",
        integration_head=head,
        applied_task_ids=[*batch.get("applied_task_ids", []), intent["task_id"]],
        merge_records=records,
        merge_intent=None,
    )


def _compose(repo: GitRepo, store: StateStore, batch: dict[str, Any]) -> dict[str, Any]:
    if not batch.get("worktree_generation"):
        batch = batch_workspace.acquire(
            repo, store, batch, Path(str(batch["worktree"]))
        )
    else:
        batch_workspace.require_owner(repo, store, batch)
    batch = store.native_batch(str(batch["id"]))
    worktree = Path(str(batch["worktree"]))
    for member in batch["tasks"]:
        task_id = str(member["task_id"])
        if task_id in batch.get("applied_task_ids", []):
            if not repo.is_ancestor(
                str(member["ready_head"]), str(batch["integration_head"])
            ):
                raise SoloAIError("Recorded native source is not in the composed head")
            continue
        _verify_member(repo, store, member)
        intent = batch.get("merge_intent")
        if intent is None:
            previous = str(batch["integration_head"])
            source = str(member["ready_head"])
            if repo.head(worktree) != previous or not repo.is_clean(worktree):
                raise SoloAIError("Native integration workspace changed before merge")
            if repo.is_ancestor(source, previous):
                raise SoloAIError("Native source was already merged into this batch")
            batch = store.update_batch(
                str(batch["id"]),
                status="composing",
                merge_intent={
                    "task_id": task_id,
                    "source_head": source,
                    "previous_head": previous,
                },
            )
            result = repo.git(
                ["merge", "--no-ff", "--no-edit", source],
                cwd=worktree,
                check=False,
            )
            if result.returncode:
                store.update_batch(
                    str(batch["id"]), status="conflicted", merge_error=result.stderr
                )
                raise ActionableSoloAIError(
                    "Native merge requires review in the owned integration workspace",
                    code="NATIVE_MERGE_CONFLICT",
                    context={"batch_id": batch["id"], "task_id": task_id},
                    next_action={
                        "kind": "review_native_merge",
                        "batch_id": batch["id"],
                    },
                )
        elif (
            intent.get("task_id") != task_id
            or intent.get("source_head") != member["ready_head"]
        ):
            raise SoloAIError("Native merge intent changed member identity")
        batch = _record_merge_result(repo, store, batch)
    return store.update_batch(str(batch["id"]), status="composed")


def _validate(
    repo: GitRepo, store: StateStore, batch: dict[str, Any]
) -> dict[str, Any]:
    worktree = Path(str(batch["worktree"]))
    batch_workspace.require_owner(repo, store, batch)
    if repo.head(worktree) != batch["integration_head"] or not repo.is_clean(worktree):
        raise SoloAIError("Native composed HEAD moved before validation")
    config = load_repo_config(repo, cwd=worktree)
    verification = load_verification_config(repo, cwd=worktree)
    _require_approval(
        repo,
        cwd=worktree,
        base=str(batch["base_ref"]),
        verification=verification,
        include_secret_scanner=config.secret_scanner is not None,
        batch_id=str(batch["id"]),
    )
    _run_secret_scanner(repo, cwd=worktree, scanner=config.secret_scanner)
    require_safe(
        repo,
        cwd=worktree,
        base=str(batch["base_before"]),
        allowlist=config.sensitive_allowlist,
    )
    attempt = new_validation_attempt_id("full")
    batch = store.update_batch(
        str(batch["id"]),
        status="validating",
        validation_attempt=attempt,
        validation_attempts=[*batch.get("validation_attempts", []), attempt],
    )
    try:
        proof = validate(
            repo,
            cwd=worktree,
            base=str(batch["base_ref"]),
            verification=verification,
            task_id=str(batch["id"]),
            level="full",
            expected_base_head=str(batch["base_before"]),
            expected_candidate_head=str(batch["integration_head"]),
            attempt_id=attempt,
            attempt_owner={"kind": "batch", "id": str(batch["id"])},
        )
    except Exception as exc:
        store.update_batch(
            str(batch["id"]), status="validation-failed", validation_error=str(exc)
        )
        raise
    if repo.ref_head(f"refs/heads/{batch['base_ref']}") != batch["base_before"]:
        raise SoloAIError("Native batch target moved during Full validation")
    return store.update_batch(
        str(batch["id"]),
        status="validated",
        proof=str(proof["fingerprint"]),
        validation_outcome="passed",
    )


def _recover_validation(
    repo: GitRepo, store: StateStore, batch: dict[str, Any]
) -> dict[str, Any]:
    attempt_id = str(batch.get("validation_attempt") or "")
    if not attempt_id:
        raise SoloAIError("Native validation has no exact attempt identity")
    attempt = read_validation_attempt(repo, attempt_id)
    if (
        attempt.get("task_id") != batch["id"]
        or attempt.get("owner") != {"kind": "batch", "id": batch["id"]}
        or attempt.get("candidate_head") != batch["integration_head"]
        or attempt.get("base_head") != batch["base_before"]
    ):
        raise SoloAIError("Native validation attempt changed identity")
    if attempt.get("state") != "completed":
        raise ActionableSoloAIError(
            "Native validation attempt is not fully stopped and recorded",
            code="NATIVE_VALIDATION_ACTIVE",
            context={"batch_id": batch["id"], "attempt_id": attempt_id},
            next_action={"kind": "wait_for_validation", "batch_id": batch["id"]},
        )
    if attempt.get("result") != "passed":
        store.update_batch(
            str(batch["id"]),
            status="validation-failed",
            validation_error=str(attempt.get("error") or attempt["result"]),
        )
        raise SoloAIError("Native Full did not pass; preserve its failed attempt")
    fingerprint = str(attempt.get("proof") or "")
    proof = read_json(repo.local_dir / "proofs" / f"{fingerprint}.json", {})
    inputs = proof.get("inputs") if isinstance(proof, dict) else None
    if (
        not fingerprint
        or proof.get("fingerprint") != fingerprint
        or proof.get("result") != "passed"
        or not isinstance(inputs, dict)
        or inputs.get("candidate_head") != batch["integration_head"]
        or inputs.get("base_head") != batch["base_before"]
    ):
        raise SoloAIError("Native passed attempt has no matching exact proof")
    return store.update_batch(
        str(batch["id"]),
        status="validated",
        proof=fingerprint,
        validation_outcome="passed",
    )


def _promote(repo: GitRepo, store: StateStore, batch: dict[str, Any]) -> dict[str, Any]:
    base_ref = str(batch["base_ref"])
    integration_head = str(batch["integration_head"])
    matching = [
        item.path
        for item in repo.worktrees()
        if not item.bare and item.branch == f"refs/heads/{base_ref}"
    ]
    if len(matching) != 1:
        raise SoloAIError("Native target is not attached in one exact worktree")
    target = matching[0]
    current = repo.ref_head(f"refs/heads/{base_ref}")
    if (
        current != repo.head(target)
        or repo.branch(target) != base_ref
        or not repo.is_clean(target)
    ):
        raise SoloAIError("Native target worktree changed before promotion")
    if current == batch["base_before"]:
        repo.git(["merge", "--ff-only", integration_head], cwd=target)
        if repo.head(target) != integration_head:
            raise SoloAIError("Native promotion did not reach the validated HEAD")
    elif current is None or not repo.is_ancestor(integration_head, current):
        raise SoloAIError("Native target drifted before promotion")
    for member in batch["tasks"]:
        if not repo.is_ancestor(str(member["ready_head"]), repo.head(target)):
            raise SoloAIError("Native source commit is absent from promoted target")
    return store.mark_native_promoted(
        str(batch["id"]), integration_head=integration_head
    )


def _release(repo: GitRepo, store: StateStore, batch: dict[str, Any]) -> dict[str, Any]:
    if not batch.get("worktree_released_at"):
        batch = batch_workspace.return_workspace(repo, store, batch)
    target = repo.ref_head(f"refs/heads/{batch['base_ref']}")
    if target is None or not repo.is_ancestor(str(batch["integration_head"]), target):
        raise SoloAIError("Promoted native result is absent from its target")
    for member in batch["tasks"]:
        task_id = str(member["task_id"])
        task = store.task(task_id)
        if task["status"] == "finished":
            continue
        worktree = Path(str(task["worktree"])).resolve()
        inventory = inspect_untracked(repo, cwd=worktree, expand_dependencies=False)
        if (
            repo.branch(worktree) != member["branch"]
            or repo.head(worktree) != member["ready_head"]
            or not repo.is_clean(worktree)
            or inventory["keep"]
            or inventory["protected"]
            or inventory["unknown_ignored"]
        ):
            raise ActionableSoloAIError(
                "Delivered fixed slot contains unknown or changed content; preserve it",
                code="NATIVE_RELEASE_PENDING",
                context={"batch_id": batch["id"], "task_id": task_id},
                next_action={"kind": "inspect_native_slot", "task_id": task_id},
            )
        store.complete_native_delivery(
            task_id,
            batch_id=str(batch["id"]),
            integration_head=str(batch["integration_head"]),
            release_receipt={
                "source_head": member["ready_head"],
                "merge_head": next(
                    item["merge_head"]
                    for item in batch["merge_records"]
                    if item["task_id"] == task_id
                ),
                "proof": batch["proof"],
                "target_head": target,
            },
        )
    return store.native_batch(str(batch["id"]))


def run_native_batch(repo: GitRepo, *, batch_id: str) -> dict[str, Any]:
    """按当前持久阶段恢复；验证失败必须先修复，绝不盲目重跑。"""

    store = StateStore(repo)
    with integration_turn(repo, batch_id):
        batch = store.native_batch(batch_id)
        if batch["status"] in {"sealed", "composing"}:
            batch = _compose(repo, store, batch)
        if batch["status"] == "conflicted":
            raise SoloAIError("Native merge conflict requires an exact managed repair")
        if batch["status"] == "composed":
            batch = _validate(repo, store, batch)
        if batch["status"] == "validating":
            batch = _recover_validation(repo, store, batch)
        if batch["status"] == "validation-failed":
            raise SoloAIError("Native Full failed; unchanged inputs will not be rerun")
        if batch["status"] == "validated":
            batch = _promote(repo, store, batch)
        if batch["status"] in {"promoted", "completed"}:
            batch = _release(repo, store, batch)
        return batch


def reconcile_native_batches(
    repo: GitRepo,
    *,
    base_ref: str,
    cause: str | None = None,
    reason: str | None = None,
) -> dict[str, Any] | None:
    """从现有等待任务推导一批；满批或已记录的有因尾批才封。"""

    store = StateStore(repo)
    state = store.read()
    if state["schema_version"] != STATE_SCHEMA:
        raise SoloAIError("Native task delivery has not been enabled")
    if any(
        batch.get("base_ref") == base_ref
        and batch.get("status") not in {"completed", "withdrawn", "cancelled"}
        for batch in state.get("batches", {}).values()
    ):
        return None
    waiting = sorted(
        (
            task
            for task in state["tasks"].values()
            if task.get("status") == "waiting-integration"
            and task.get("base_ref") == base_ref
        ),
        key=lambda item: (
            str((item.get("native_delivery") or {}).get("ready_at") or ""),
            str(item["id"]),
        ),
    )
    if not waiting:
        return None
    size = load_repo_config(repo).integration.batch_size
    selected = waiting[:size]
    if len(selected) < size:
        if cause is None:
            requests = [
                (task.get("native_delivery") or {}).get("tail_request")
                for task in selected
            ]
            requested = next(
                (item for item in requests if isinstance(item, dict)), None
            )
            if requested is None:
                return None
            cause = str(requested.get("cause") or "")
            reason = str(requested.get("reason") or "")
        if cause == "round-complete" and any(
            task.get("base_ref") == base_ref
            and task.get("status") in {"starting", "active"}
            for task in state["tasks"].values()
        ):
            return None
    batch = seal_native_batch(
        repo,
        task_ids=[str(task["id"]) for task in selected],
        cause=cause,
        reason=reason,
    )
    return run_native_batch(repo, batch_id=str(batch["id"]))
