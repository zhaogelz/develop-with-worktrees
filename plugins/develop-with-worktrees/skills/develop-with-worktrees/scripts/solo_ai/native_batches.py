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
from .runtime_adapter import (
    activate_batch_runtime,
    release_batch_runtime,
    require_exact_passed_batch_release,
)
from .safety import SensitiveContentError, require_safe
from .state import STATE_SCHEMA, StateStore, candidate_admission_lock
from .util import (
    ActionableSoloAIError,
    SoloAIError,
    atomic_write_text,
    ensure_within,
    read_json,
    sha256_text,
    utc_timestamp,
)


_TAIL_CAUSES = {"round-complete", "user", "dependency", "deploy"}


def seal_native_batch(
    repo: GitRepo,
    *,
    task_ids: list[str],
    cause: str | None = None,
    reason: str | None = None,
    capacity_request_id: str | None = None,
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
        capacity_cause = cause == "capacity" and bool(capacity_request_id)
        if len(task_ids) < config.integration.batch_size:
            if (
                not (cause in _TAIL_CAUSES or capacity_cause)
                or not reason
                or "\n" in reason
            ):
                raise SoloAIError("Native tail requires an explicit cause and reason")
        elif cause is not None and cause not in _TAIL_CAUSES and not capacity_cause:
            raise SoloAIError("Unsupported native batch cause")
        if cause == "capacity":
            if not capacity_cause or any(
                slot.get("status") == "idle" and int(slot["id"]) <= config.slots
                for slot in state["slots"].values()
            ):
                raise SoloAIError(
                    "Capacity tail requires a blocked Start and no free slot"
                )
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
            "capacity_request_id": capacity_request_id if capacity_cause else None,
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


def _require_runtime_workspace(
    repo: GitRepo, store: StateStore, batch: dict[str, Any]
) -> None:
    worktree = Path(str(batch["worktree"]))
    if repo.head(worktree) != batch["integration_head"] or not repo.is_clean(worktree):
        raise SoloAIError("Native runtime workspace changed; preserve its files")
    batch_workspace.require_owner(repo, store, batch)


def _activate_runtime(
    repo: GitRepo, store: StateStore, batch: dict[str, Any]
) -> dict[str, Any]:
    if batch["status"] == "composed":
        batch = store.update_batch(
            str(batch["id"]),
            status="runtime_activating",
            runtime_cycle=int(batch.get("runtime_cycle", 0)) + 1,
            runtime_activation=None,
            runtime_release=None,
            validation_outcome=None,
            validation_error=None,
            proof=None,
        )
    try:
        _require_runtime_workspace(repo, store, batch)
        receipt = activate_batch_runtime(repo, batch=batch)
        _require_runtime_workspace(repo, store, batch)
    except BaseException as exc:
        store.update_batch(
            str(batch["id"]),
            status="runtime_activation_pending",
            runtime_activation_error=str(exc),
        )
        raise
    return store.update_batch(
        str(batch["id"]),
        status="runtime_active",
        runtime_activation=receipt,
        runtime_activation_error=None,
    )


def _release_runtime(
    repo: GitRepo, store: StateStore, batch: dict[str, Any]
) -> dict[str, Any]:
    try:
        if batch["status"] == "runtime_release_pending":
            _require_runtime_workspace(repo, store, batch)
        receipt = release_batch_runtime(
            repo,
            batch=batch,
            validation_outcome=str(batch["validation_outcome"]),
            validation_error=batch.get("validation_error"),
        )
        _require_runtime_workspace(repo, store, batch)
    except BaseException as exc:
        store.update_batch(
            str(batch["id"]),
            status="runtime_release_pending",
            runtime_release_error=str(exc),
        )
        raise
    outcome = str(batch["validation_outcome"])
    return store.update_batch(
        str(batch["id"]),
        status=(
            "validated"
            if outcome == "passed"
            else "validation-failed"
            if outcome == "failed"
            else "composed"
        ),
        runtime_release=receipt,
        runtime_release_error=None,
    )


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
    try:
        require_safe(
            repo,
            cwd=worktree,
            base=str(batch["base_before"]),
            allowlist=config.sensitive_allowlist,
        )
    except SensitiveContentError as exc:
        store.update_batch(
            str(batch["id"]),
            status="preflight-failed",
            validation_error=str(exc),
            preflight_failure={
                "head": batch["integration_head"],
                "base_before": batch["base_before"],
                "findings": [
                    {"path": item.path, "line": item.line, "rule": item.rule}
                    for item in exc.findings
                ],
            },
        )
        raise
    batch = _activate_runtime(repo, store, batch)
    return _run_full(repo, store, batch, verification)


def _run_full(
    repo: GitRepo,
    store: StateStore,
    batch: dict[str, Any],
    verification: Any,
) -> dict[str, Any]:
    worktree = Path(str(batch["worktree"]))
    _require_runtime_workspace(repo, store, batch)
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
    except (KeyboardInterrupt, SystemExit) as exc:
        releasing = store.update_batch(
            str(batch["id"]),
            status="runtime_releasing",
            validation_outcome="interrupted",
            validation_error=str(exc),
        )
        _release_runtime(repo, store, releasing)
        raise
    except Exception as exc:
        releasing = store.update_batch(
            str(batch["id"]),
            status="runtime_releasing",
            validation_outcome="failed",
            validation_error=str(exc),
        )
        _release_runtime(repo, store, releasing)
        raise
    if (
        repo.ref_head(f"refs/heads/{batch['base_ref']}") != batch["base_before"]
        or repo.head(worktree) != batch["integration_head"]
        or not repo.is_clean(worktree)
    ):
        releasing = store.update_batch(
            str(batch["id"]),
            status="runtime_releasing",
            validation_outcome="failed",
            validation_error="Native batch target or workspace changed during Full validation",
        )
        _release_runtime(repo, store, releasing)
        raise SoloAIError(
            "Native batch target or workspace changed during Full validation"
        )
    releasing = store.update_batch(
        str(batch["id"]),
        status="runtime_releasing",
        proof=str(proof["fingerprint"]),
        validation_outcome="passed",
    )
    return _release_runtime(repo, store, releasing)


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
        releasing = store.update_batch(
            str(batch["id"]),
            status="runtime_releasing",
            validation_outcome="failed",
            validation_error=str(attempt.get("error") or attempt["result"]),
        )
        _release_runtime(repo, store, releasing)
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
    releasing = store.update_batch(
        str(batch["id"]),
        status="runtime_releasing",
        proof=fingerprint,
        validation_outcome="passed",
    )
    return _release_runtime(repo, store, releasing)


def _promote(repo: GitRepo, store: StateStore, batch: dict[str, Any]) -> dict[str, Any]:
    _require_runtime_workspace(repo, store, batch)
    require_exact_passed_batch_release(
        repo, receipt=dict(batch.get("runtime_release") or {})
    )
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


def repair_native_batch(
    repo: GitRepo,
    *,
    batch_id: str,
    expected_head: str,
    patch_file: Path,
    paths: list[str],
    message: str,
    reason: str,
) -> dict[str, Any]:
    """在拥有者集成区提交精确修复；中断后用同一输入恢复，不重复应用补丁。"""
    if not paths or len(set(paths)) != len(paths):
        raise SoloAIError("Native repair requires unique exact paths")
    if not message.strip() or not reason.strip() or "\n" in reason or "\r" in reason:
        raise SoloAIError("Native repair requires a commit message and one-line reason")
    if not patch_file.is_file() or patch_file.is_symlink():
        raise SoloAIError("Native repair patch must be one readable regular file")
    patch_text = patch_file.read_text(encoding="utf-8")
    if not patch_text.strip() or any(
        marker in patch_text
        for marker in (
            "GIT binary patch",
            "rename from ",
            "copy from ",
            "new file mode 120000",
        )
    ):
        raise SoloAIError("Native repair accepts only an exact text patch")
    digest = sha256_text(patch_text)
    store = StateStore(repo)
    with integration_turn(repo, batch_id):
        batch = store.native_batch(batch_id)
        if batch["status"] not in {
            "preflight-failed",
            "validation-failed",
            "conflicted",
            "repairing",
        }:
            raise SoloAIError(
                "Native repair requires a failed preflight, failed Full, or merge conflict"
            )
        batch_workspace.require_owner(repo, store, batch)
        worktree = Path(str(batch["worktree"])).resolve()
        current_head = repo.head(worktree)
        if expected_head != batch["integration_head"] or (
            current_head != expected_head and batch["status"] != "repairing"
        ):
            raise SoloAIError("Native repair HEAD no longer matches the frozen batch")
        requested = set(paths)
        for name in requested:
            target = ensure_within(worktree / name, worktree)
            if (
                Path(name).is_absolute()
                or ".." in Path(name).parts
                or ".git" in Path(name).parts
                or target == worktree
            ):
                raise SoloAIError(
                    "Native repair path escapes its integration workspace"
                )
        intent = batch.get("repair_intent")
        if intent is None:
            if batch["status"] == "preflight-failed":
                failure = batch.get("preflight_failure") or {}
                if (
                    failure.get("head") != expected_head
                    or failure.get("base_before") != batch["base_before"]
                    or not failure.get("findings")
                    or batch.get("validation_attempt")
                    or not repo.is_clean(worktree)
                    or requested != {".solo-ai/config.toml"}
                ):
                    raise SoloAIError(
                        "Native preflight repair requires its recorded finding, "
                        "a clean workspace, and only the exact policy path"
                    )
            elif batch["status"] == "validation-failed":
                attempt = read_validation_attempt(
                    repo, str(batch.get("validation_attempt") or "")
                )
                if (
                    attempt.get("state") != "completed"
                    or attempt.get("result") != "failed"
                    or attempt.get("task_id") != batch_id
                    or not repo.is_clean(worktree)
                ):
                    raise SoloAIError(
                        "Native Full is not a stopped ordinary failure with a clean workspace"
                    )
            elif batch["status"] == "conflicted" and not batch.get("merge_intent"):
                raise SoloAIError("Native merge conflict lost its exact source intent")
            snapshot = repo.local_dir / "repair-patches" / f"{batch_id}-{digest}.patch"
            atomic_write_text(snapshot, patch_text)
            numstat = repo.git(
                ["apply", "--numstat", "-z", str(snapshot)], cwd=worktree
            ).stdout
            entries = [entry.split("\t", 2) for entry in numstat.split("\0") if entry]
            if (
                not entries
                or any(len(entry) != 3 for entry in entries)
                or {entry[2] for entry in entries} != requested
            ):
                raise SoloAIError(
                    "Native repair patch paths differ from exact path list"
                )
            intent = {
                "head": expected_head,
                "paths": sorted(requested),
                "patch_sha256": digest,
                "patch_file": str(snapshot),
                "message": message,
                "reason": reason,
                "previous_status": batch["status"],
                "validation_attempt": batch.get("validation_attempt"),
                "started_at": utc_timestamp(),
            }
            batch = store.update_batch(
                batch_id, status="repairing", repair_intent=intent
            )
        elif any(
            intent.get(key) != value
            for key, value in (
                ("head", expected_head),
                ("paths", sorted(requested)),
                ("patch_sha256", digest),
                ("message", message),
                ("reason", reason),
            )
        ):
            raise SoloAIError("Native repair retry changed its frozen inputs")
        snapshot = Path(str(intent["patch_file"]))
        if sha256_text(snapshot.read_text(encoding="utf-8")) != digest:
            raise SoloAIError("Native repair patch snapshot changed")
        actual_head = repo.head(worktree)
        if actual_head == expected_head:
            reverse = repo.git(
                ["apply", "--reverse", "--check", str(snapshot)],
                cwd=worktree,
                check=False,
            )
            if reverse.returncode:
                repo.git(["apply", "--check", str(snapshot)], cwd=worktree)
                repo.git(["apply", str(snapshot)], cwd=worktree)
            repo.git(["add", "--", *sorted(requested)], cwd=worktree)
            if repo.git(["ls-files", "-u"], cwd=worktree).stdout.strip():
                raise SoloAIError("Native merge still has unresolved paths")
            repo.git(["diff", "--cached", "--check"], cwd=worktree)
            repo.git(["commit", "-m", message], cwd=worktree)
            actual_head = repo.head(worktree)
        if actual_head == expected_head or not repo.is_clean(worktree):
            raise SoloAIError("Native repair did not produce one clean new commit")
        parents = (
            repo.git(["rev-list", "--parents", "-n", "1", actual_head], cwd=worktree)
            .stdout.strip()
            .split()
        )
        if intent["previous_status"] == "conflicted":
            merge_intent = batch.get("merge_intent") or {}
            if parents != [
                actual_head,
                expected_head,
                merge_intent.get("source_head"),
            ]:
                raise SoloAIError("Native conflict repair lost exact merge parents")
            batch = _record_merge_result(repo, store, batch)
        elif parents != [actual_head, expected_head]:
            raise SoloAIError("Native Full repair must be one child of validated HEAD")
        else:
            batch_workspace.remember_head(repo, batch, actual_head)
            batch = store.update_batch(
                batch_id, status="composed", integration_head=actual_head
            )
        record = {
            "previous_head": expected_head,
            "repair_head": actual_head,
            "paths": sorted(requested),
            "patch_sha256": digest,
            "reason": reason,
            "previous_status": intent["previous_status"],
            "validation_attempt": intent.get("validation_attempt"),
            "preflight_failure": (
                batch.get("preflight_failure")
                if intent["previous_status"] == "preflight-failed"
                else None
            ),
            "completed_at": utc_timestamp(),
        }
        return store.update_batch(
            batch_id,
            repair_intent=None,
            repair_records=[*batch.get("repair_records", []), record],
            validation_error=None,
            preflight_failure=None,
        )


def run_native_batch(repo: GitRepo, *, batch_id: str) -> dict[str, Any]:
    """按当前持久阶段恢复；验证失败必须先修复，绝不盲目重跑。"""

    store = StateStore(repo)
    with integration_turn(repo, batch_id):
        batch = store.native_batch(batch_id)
        if batch["status"] in {"sealed", "composing"}:
            batch = _compose(repo, store, batch)
        if batch["status"] in {"conflicted", "repairing"}:
            raise SoloAIError("Native merge conflict requires an exact managed repair")
        if batch["status"] == "composed":
            batch = _validate(repo, store, batch)
        if batch["status"] in {"runtime_activating", "runtime_activation_pending"}:
            batch = _activate_runtime(repo, store, batch)
        if batch["status"] == "runtime_active":
            verification = load_verification_config(
                repo, cwd=Path(str(batch["worktree"]))
            )
            batch = _run_full(repo, store, batch, verification)
        if batch["status"] == "validating":
            batch = _recover_validation(repo, store, batch)
        if batch["status"] in {"runtime_releasing", "runtime_release_pending"}:
            batch = _release_runtime(repo, store, batch)
        if batch["status"] == "validation-failed":
            raise SoloAIError("Native Full failed; unchanged inputs will not be rerun")
        if batch["status"] == "preflight-failed":
            raise SoloAIError(
                "Native preflight failed; unchanged inputs will not be rerun"
            )
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
    capacity_request_id: str | None = None,
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
        capacity_request_id=capacity_request_id,
    )
    return run_native_batch(repo, batch_id=str(batch["id"]))
