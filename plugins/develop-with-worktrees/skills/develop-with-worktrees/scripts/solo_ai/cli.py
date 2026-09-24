from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from . import VERSION
from .command_contract import TOP_LEVEL_COMMANDS
from .candidate_batches import (
    EXPLICIT_TAIL_CAUSES,
    CandidateBatchStore,
    prepare_candidate_repair,
    restore_candidate_branch,
    reconcile_batches,
    recover_batch,
    reopen_prevalidation_batch,
    retire_failed_batch,
    seal_batch,
    withdraw_candidate,
    verified_recovery_source,
)
from .cleanup import classify_cleanup_path, require_managed_directory_identity
from .host_context import resolve_host_reference
from .host_handoffs import HostHandoffStore
from .config import (
    CommandSpec,
    load_repo_config,
    load_verification_config,
    managed_agents_status,
)
from .delegated import (
    ALLOWED_CAPABILITIES,
    DelegatedContractError,
    approve_delegated,
    inspect_delegated,
    invoke_delegated,
    revoke_delegated,
)
from .lifecycle import (
    acknowledge_root_plan,
    amend_root_task_anchor,
    abandon,
    adopt_task_anchor,
    approve,
    bind_host_root_anchor,
    bind_task_root_anchor,
    close_root_task_anchor,
    create_root_task_anchor,
    record_root_task_acceptance,
    reindex_root_task_acceptance,
    refresh_root_context,
    show_task_anchor,
    update_task_anchor,
    choose,
    commit_task,
    deinit,
    dev_start,
    dev_stop,
    disable,
    finish,
    handoff,
    host_root_context,
    initialize,
    local_enabled,
    list_root_task_anchors,
    maintenance_lock,
    ready,
    recover,
    reclaim_retained_worktree,
    repository_route,
    resume_in_place,
    retarget,
    set_local_enabled,
    start,
    show_root_task_anchor,
    update_root_task_anchor,
    update_root_task_progress,
    upgrade_root_task_to_objective_protocol,
    warm_slot,
)
from .orchestration import BatchStore
from .orchestration.adapters import adapter_for
from .orchestration.models import MAX_DEVELOPMENT_PARALLELISM
from .proof import (
    approval_plan,
    frozen_validation_environment,
    new_validation_attempt_id,
    profile_execution_decision,
    profile_selection_reason,
    require_approved_plan,
    proof_inputs,
    selected_profile_ids,
    validate,
)
from .repo import GitRepo
from .routing import detect_existing_workflows
from .runtime_adapter import verify_runtime_effective
from .state import FINAL_TASK_STATES, STATE_SCHEMA, StateStore
from .status_views import status_view as query_status_view
from .util import (
    ActionableSoloAIError,
    SoloAIError,
    atomic_write_json,
    delete_plain_path_if_unchanged,
    directory_size,
    ensure_within,
    format_bytes,
    git_metadata_access_error,
    is_link_or_junction,
    new_id,
    path_identity,
    read_json,
    sha256_file,
    sha256_text,
    snapshot_plain_path,
    stable_json,
    utc_timestamp,
)
from .validation_queue import estimate_validation, queue_status, set_capacity


def _add_host_reference_arguments(
    parser: argparse.ArgumentParser, *, role: str
) -> None:
    parser.add_argument(
        "--host-kind",
        help=f"verified host kind that owns this {role}; requires --host-thread",
    )
    parser.add_argument(
        "--host-thread",
        help=f"verified host task or session identifier that owns this {role}; requires --host-kind",
    )


def _resolved_host_reference(args: argparse.Namespace) -> dict[str, str] | None:
    """优先使用兼容参数；Codex Desktop 未传参数时读取其精确任务上下文。"""

    return resolve_host_reference(
        getattr(args, "host_kind", None), getattr(args, "host_thread", None)
    )


def _add_tail_request_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--cause",
        choices=tuple(sorted(EXPLICIT_TAIL_CAUSES)),
        help="why this smaller tail may be sealed",
    )
    parser.add_argument(
        "--reason",
        help="one-line basis for this smaller tail",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dww",
        description="Local-first isolated worktree lifecycle for Codex coding tasks",
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path.cwd(),
        help="path inside the target Git repository",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable output; task leases are still redacted",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "version", help="show installed workflow version and runtime contract"
    )

    init = sub.add_parser(
        "init", help="show or accept the one-time repository adoption plan"
    )
    init.add_argument("--slots", type=int, default=3)
    init_validation = init.add_mutually_exclusive_group()
    init_validation.add_argument(
        "--verify",
        action="append",
        default=None,
        metavar="JSON_ARGV",
        help='explicit command argv, e.g. --verify \'["uv","run","pytest"]\'',
    )
    init_validation.add_argument(
        "--verification-file",
        type=Path,
        metavar="PATH",
        help="reviewed schema-3 verification.toml to copy into the new repository",
    )
    init.add_argument("--accept", action="store_true")
    init.add_argument("--accept-static-only", action="store_true")
    init.add_argument(
        "--decline",
        action="store_true",
        help="advanced compatibility alias for choosing this repository's local direct mode",
    )

    choose_parser = sub.add_parser(
        "choose",
        help="record one repository modification choice from the first-write prompt",
    )
    choose_parser.add_argument(
        "--mode",
        required=True,
        choices=["isolated", "current-task", "current-repository"],
    )
    choose_parser.add_argument("--slots", type=int, default=3)
    choose_validation = choose_parser.add_mutually_exclusive_group()
    choose_validation.add_argument(
        "--verify",
        action="append",
        default=None,
        metavar="JSON_ARGV",
        help="advanced explicit command argv for isolated setup",
    )
    choose_validation.add_argument(
        "--verification-file",
        type=Path,
        metavar="PATH",
        help="reviewed schema-3 verification.toml to copy for isolated setup",
    )
    choose_parser.add_argument(
        "--session",
        help="Codex session identifier from the trusted hook; only for current-task",
    )
    choose_parser.add_argument(
        "--delegate",
        help="one-time parent-task delegation code; only for a child current-task session",
    )

    approval = sub.add_parser(
        "approve",
        help="locally approve only the commands required by one lifecycle step",
    )
    approval.add_argument("--accept", action="store_true", required=True)
    approval.add_argument(
        "--scope",
        default="all",
        choices=(
            "all",
            "ready",
            "full",
            "complete",
            "stress",
            "commit",
            "finish",
            "development",
            "warm",
            "batch-full",
            "runtime-activate",
            "runtime-release",
            "runtime-batch-activate",
            "runtime-batch-release",
            "runtime-verify-effective",
        ),
        help="the exact lifecycle step whose declared commands are approved",
    )
    approval_target = approval.add_mutually_exclusive_group()
    approval_target.add_argument("--task", help="task that will execute the step")
    approval_target.add_argument("--slot", help="managed slot that will be warmed")
    approval_target.add_argument(
        "--batch", help="combined batch that will execute the step"
    )
    approval_target.add_argument(
        "--candidate", help="delivered candidate whose runtime will be checked"
    )

    sub.add_parser(
        "disable", help="opt out on this machine without changing tracked policy"
    )
    sub.add_parser("enable", help="re-enable managed tasks on this machine")
    settings = sub.add_parser(
        "settings", help="show or adjust machine-local validation capacity"
    )
    settings.add_argument(
        "--validation-capacity",
        metavar="AUTO_OR_1_TO_4",
        help="auto or 1..4; this local setting never changes tracked repository policy",
    )
    sub.add_parser(
        "doctor",
        help="read-only mode, policy, approval, task, and uninstall readiness report",
    )
    route = sub.add_parser(
        "route",
        help="return one compact read-only repository action for the Codex adapter",
    )
    route.add_argument(
        "--session",
        help="optional Codex session identifier supplied by the trusted hook",
    )

    delegated = sub.add_parser(
        "delegated",
        help="inspect, approve, or invoke an explicitly declared repository adapter",
    )
    delegated_sub = delegated.add_subparsers(dest="delegated_command", required=True)
    delegated_sub.add_parser(
        "inspect", help="read and fingerprint the declared adapter without executing it"
    )
    delegated_approve = delegated_sub.add_parser(
        "approve",
        help="approve one exact adapter and tracked-input fingerprint locally",
    )
    delegated_approve.add_argument("--fingerprint", required=True)
    delegated_approve.add_argument("--accept", action="store_true", required=True)
    delegated_revoke = delegated_sub.add_parser(
        "revoke", help="revoke one exact local adapter approval without changing Git"
    )
    delegated_revoke.add_argument("--adapter-id", required=True)
    delegated_revoke.add_argument("--fingerprint", required=True)
    delegated_revoke.add_argument("--confirm", action="store_true", required=True)
    delegated_invoke = delegated_sub.add_parser(
        "invoke",
        help="invoke one capability through the approved JSON adapter protocol",
    )
    delegated_invoke.add_argument(
        "--operation", required=True, choices=sorted(ALLOWED_CAPABILITIES)
    )
    delegated_invoke.add_argument("--request", default="{}", metavar="JSON_OBJECT")
    delegated_invoke.add_argument("--timeout-seconds", type=float, default=900)

    orchestration = sub.add_parser(
        "orchestrate",
        help="legacy drain-only task orchestration; use the host's native task system for new work",
    )
    orchestration_sub = orchestration.add_subparsers(
        dest="orchestration_command", required=True
    )
    orchestration_plan = orchestration_sub.add_parser(
        "plan", help="create a batch plan; it never dispatches workers"
    )
    orchestration_plan.add_argument("--goal", required=True)
    orchestration_plan.add_argument(
        "--task",
        action="append",
        default=[],
        metavar="JSON_TASK",
        help='task JSON, for example {"id":"api","title":"提供接口","acceptance":["可查询"]}',
    )
    orchestration_plan.add_argument("--controller", required=True)
    orchestration_plan.add_argument(
        "--adapter", choices=["dww", "delegated"], default="dww"
    )
    orchestration_plan.add_argument(
        "--max-parallel", type=int, default=MAX_DEVELOPMENT_PARALLELISM
    )
    orchestration_plan.add_argument("--max-effective-changes", type=int, default=3)
    orchestration_plan.add_argument("--max-repair-minutes", type=int, default=20)
    orchestration_confirm = orchestration_sub.add_parser(
        "confirm", help="mark the once-approved plan as schedulable"
    )
    orchestration_confirm.add_argument("--batch", required=True)
    orchestration_confirm.add_argument("--controller", required=True)
    orchestration_status = orchestration_sub.add_parser(
        "status", help="show local batch state and the current schedulable frontier"
    )
    orchestration_status.add_argument("--batch")
    orchestration_status.add_argument("--available-slots", type=int)
    orchestration_frontier = orchestration_sub.add_parser(
        "frontier", help="return only tasks the central controller may dispatch now"
    )
    orchestration_frontier.add_argument("--batch", required=True)
    orchestration_frontier.add_argument("--available-slots", type=int)
    orchestration_claim = orchestration_sub.add_parser(
        "claim", help="central controller assigns one writer to a frontier task"
    )
    orchestration_claim.add_argument("--batch", required=True)
    orchestration_claim.add_argument("--task", required=True)
    orchestration_claim.add_argument("--worker", required=True)
    orchestration_claim.add_argument("--controller", required=True)
    orchestration_link = orchestration_sub.add_parser(
        "link", help="record the DWW or delegated lifecycle task reference"
    )
    orchestration_link.add_argument("--batch", required=True)
    orchestration_link.add_argument("--task", required=True)
    orchestration_link.add_argument("--lifecycle-task", required=True)
    orchestration_link.add_argument("--controller", required=True)
    orchestration_complete = orchestration_sub.add_parser(
        "complete", help="record a completed task with existing acceptance evidence"
    )
    orchestration_complete.add_argument("--batch", required=True)
    orchestration_complete.add_argument("--task", required=True)
    orchestration_complete.add_argument(
        "--evidence", action="append", default=[], metavar="JSON_EVIDENCE"
    )
    orchestration_complete.add_argument("--controller", required=True)
    orchestration_block = orchestration_sub.add_parser(
        "block", help="locally pause one task while unrelated work can continue"
    )
    orchestration_block.add_argument("--batch", required=True)
    orchestration_block.add_argument("--task", required=True)
    orchestration_block.add_argument("--reason", required=True)
    orchestration_block.add_argument("--controller", required=True)
    orchestration_attempt = orchestration_sub.add_parser(
        "record-attempt", help="record a repair attempt without blindly rerunning"
    )
    orchestration_attempt.add_argument("--batch", required=True)
    orchestration_attempt.add_argument("--task", required=True)
    orchestration_attempt.add_argument(
        "--changed", choices=["true", "false"], required=True
    )
    orchestration_attempt.add_argument("--summary", required=True)
    orchestration_attempt.add_argument("--controller", required=True)
    for command_name, help_text in (
        ("pause", "stop dispatching while preserving in-flight work"),
        ("resume", "resume dispatching preserved work"),
    ):
        item = orchestration_sub.add_parser(command_name, help=help_text)
        item.add_argument("--batch", required=True)
        item.add_argument("--controller", required=True)
    orchestration_takeover = orchestration_sub.add_parser(
        "take-over", help="let a new central session resume the preserved local batch"
    )
    orchestration_takeover.add_argument("--batch", required=True)
    orchestration_takeover.add_argument("--controller", required=True)
    orchestration_takeover.add_argument("--confirm", required=True)
    orchestration_add = orchestration_sub.add_parser(
        "add-task", help="add an internal task that stays inside the approved goal"
    )
    orchestration_add.add_argument("--batch", required=True)
    orchestration_add.add_argument("--task", required=True, metavar="JSON_TASK")
    orchestration_add.add_argument("--inside-approved-goal", action="store_true")
    orchestration_add.add_argument("--controller", required=True)
    orchestration_repair = orchestration_sub.add_parser(
        "repair",
        help="create a fresh repair task for an attributed completed or blocked task",
    )
    orchestration_repair.add_argument("--batch", required=True)
    orchestration_repair.add_argument(
        "--source", action="append", default=[], required=True
    )
    orchestration_repair.add_argument("--task", required=True, metavar="JSON_TASK")
    orchestration_repair.add_argument("--reason", required=True)
    orchestration_repair.add_argument("--controller", required=True)
    orchestration_cancel = orchestration_sub.add_parser(
        "cancel", help="cancel scheduling only; it never deletes task code"
    )
    orchestration_cancel.add_argument("--batch", required=True)
    orchestration_cancel.add_argument("--task", required=True)
    orchestration_cancel.add_argument("--confirm", required=True)
    orchestration_cancel.add_argument("--controller", required=True)

    start_parser = sub.add_parser("start", help="claim a slot and create a task branch")
    start_parser.add_argument("--name", required=True)
    start_parser.add_argument(
        "--target",
        help="implementation target written into the first task anchor",
    )
    start_parser.add_argument(
        "--scope",
        help="scope boundary written into the first task anchor",
    )
    start_parser.add_argument(
        "--acceptance",
        help="acceptance criteria written into the first task anchor",
    )
    start_parser.add_argument(
        "--base",
        help="local branch to use as the task base; defaults to the invocation worktree's current branch",
    )
    start_parser.add_argument(
        "--in-place",
        action="store_true",
        help="use the current clean worktree for this one Codex session",
    )
    start_parser.add_argument(
        "--bind-branch",
        help="attach this exact task-prefixed branch only when a trusted linked worktree is detached",
    )
    start_parser.add_argument(
        "--session",
        help="Codex session identifier supplied by the trusted hook; required for in-place tasks",
    )
    start_parser.add_argument(
        "--request-id",
        help="optional caller identity; repeating it returns the same managed task",
    )
    start_parser.add_argument(
        "--supersedes",
        help="candidate id replaced by this repair task when it is published",
    )
    start_parser.add_argument(
        "--root-anchor",
        help="optional durable coordinator root anchor to bind to this task",
    )
    start_parser.add_argument(
        "--root-anchor-file",
        type=Path,
        help="explicit absolute external root-anchor file; requires --root-anchor",
    )
    start_parser.add_argument(
        "--independent-reason",
        help="one-line user-confirmed reason this task is independent of the current host objective",
    )
    _add_host_reference_arguments(start_parser, role="development task")

    root_anchor = sub.add_parser(
        "root-anchor", help="manage one local durable objective anchor"
    )
    root_anchor_sub = root_anchor.add_subparsers(
        dest="root_anchor_command", required=True
    )
    root_create = root_anchor_sub.add_parser(
        "create",
        help="create a complete root execution contract without claiming a slot",
    )
    root_create.add_argument("--purpose", required=True)
    root_create.add_argument("--target", required=True)
    root_create.add_argument("--scope", required=True)
    root_create.add_argument("--acceptance", required=True)
    root_create.add_argument("--base")
    root_create.add_argument(
        "--plan-file",
        type=Path,
        help="UTF-8 file containing the complete confirmed plan",
    )
    root_create.add_argument(
        "--plan-source", help="concise source for the user-confirmed plan"
    )
    root_create.add_argument(
        "--acceptance-index-file",
        type=Path,
        help="JSON acceptance index extracted from the complete confirmed plan",
    )
    root_create.add_argument(
        "--request-id", help="stable caller id; repeated creation returns the same root"
    )
    root_create.add_argument(
        "--content",
        action="store_true",
        help="include the complete root-anchor body in this response",
    )
    _add_host_reference_arguments(root_create, role="confirmed objective")
    root_show = root_anchor_sub.add_parser(
        "show", help="show one root anchor summary; use --content for its full body"
    )
    root_show.add_argument("--root", required=True)
    root_show.add_argument(
        "--version", type=int, help="read one exact historical plan version"
    )
    root_show.add_argument(
        "--content", action="store_true", help="include the complete UTF-8 anchor body"
    )
    root_bind_host = root_anchor_sub.add_parser(
        "bind-host",
        help="bind an exact existing root anchor to the current host objective",
    )
    root_bind_host.add_argument("--root", required=True)
    root_bind_host.add_argument(
        "--root-anchor-file",
        type=Path,
        help="explicit absolute external root-anchor file; omit for a local root",
    )
    _add_host_reference_arguments(root_bind_host, role="confirmed objective")
    root_context = root_anchor_sub.add_parser(
        "context",
        help="show the current exact host-to-root association without returning plan text",
    )
    _add_host_reference_arguments(root_context, role="confirmed objective")
    root_update = root_anchor_sub.add_parser(
        "update", help="atomically update one root anchor from a UTF-8 file"
    )
    root_update.add_argument("--root", required=True)
    root_update.add_argument("--file", type=Path, required=True)
    root_update.add_argument("--expected-sha256", required=True)
    root_update.add_argument(
        "--content",
        action="store_true",
        help="include the complete root-anchor body in this response",
    )
    root_amend = root_anchor_sub.add_parser(
        "amend",
        help="replace or append the effective plan after an explicit user-confirmed change",
    )
    root_amend.add_argument("--root", required=True)
    root_amend_input = root_amend.add_mutually_exclusive_group(required=True)
    root_amend_input.add_argument(
        "--plan-file",
        type=Path,
        help="UTF-8 file replacing the complete effective plan",
    )
    root_amend_input.add_argument(
        "--change-file",
        type=Path,
        help="UTF-8 file appended verbatim to the current effective plan",
    )
    root_amend.add_argument("--source", required=True)
    root_amend.add_argument("--summary", required=True)
    root_amend.add_argument("--expected-sha256", required=True)
    root_amend.add_argument("--target")
    root_amend.add_argument("--scope")
    root_amend.add_argument("--acceptance")
    root_amend.add_argument(
        "--acceptance-index-file",
        type=Path,
        help="replacement JSON acceptance index for an objective-protocol plan amendment",
    )
    root_amend.add_argument(
        "--content",
        action="store_true",
        help="include the complete root-anchor body in this response",
    )
    root_progress = root_anchor_sub.add_parser(
        "progress", help="record root progress without changing the confirmed plan"
    )
    root_progress.add_argument("--root", required=True)
    root_progress.add_argument("--progress", required=True)
    root_progress.add_argument("--expected-sha256", required=True)
    root_progress.add_argument(
        "--content",
        action="store_true",
        help="include the complete root-anchor body in this response",
    )
    root_acceptance = root_anchor_sub.add_parser(
        "accept", help="record the checked overall objective result"
    )
    root_acceptance.add_argument("--root", required=True)
    root_acceptance.add_argument(
        "--status", choices=("accepted", "cancelled"), required=True
    )
    root_acceptance_input = root_acceptance.add_mutually_exclusive_group(required=True)
    root_acceptance_input.add_argument("--evidence-file", type=Path)
    root_acceptance_input.add_argument(
        "--evidence-json",
        help="strict JSON evidence for an objective-protocol root; does not read a file",
    )
    root_acceptance.add_argument("--expected-sha256", required=True)
    root_acceptance.add_argument(
        "--content",
        action="store_true",
        help="include the complete root-anchor body in this response",
    )
    root_reindex = root_anchor_sub.add_parser(
        "reindex",
        help="replace a derived acceptance index without changing the confirmed plan",
    )
    root_reindex.add_argument("--root", required=True)
    root_reindex.add_argument("--index-file", type=Path, required=True)
    root_reindex.add_argument("--expected-sha256", required=True)
    root_reindex.add_argument("--content", action="store_true")
    root_upgrade = root_anchor_sub.add_parser(
        "upgrade-objective",
        help="upgrade one checked legacy structured root to the indexed objective protocol",
    )
    root_upgrade.add_argument("--root", required=True)
    root_upgrade.add_argument("--index-file", type=Path, required=True)
    root_upgrade.add_argument("--expected-sha256", required=True)
    root_upgrade.add_argument("--content", action="store_true")
    root_close = root_anchor_sub.add_parser(
        "close", help="delete a root anchor after every child task is terminal"
    )
    root_close.add_argument("--root", required=True)
    root_close.add_argument("--confirm", required=True)
    root_anchor_sub.add_parser("list", help="list local open root anchors")

    candidate = sub.add_parser(
        "candidate",
        help="inspect or withdraw immutable source candidates in batched mode",
    )
    candidate_sub = candidate.add_subparsers(dest="candidate_command", required=True)
    candidate_status = candidate_sub.add_parser(
        "status", help="show the local candidate pool"
    )
    candidate_status.add_argument(
        "--history", action="store_true", help="include terminal candidate history"
    )
    candidate_status.add_argument(
        "--candidate", help="show one exact candidate, including its history"
    )
    candidate_status.add_argument(
        "--check", action="store_true", help="check DWW candidate refs without changes"
    )
    candidate_status.add_argument(
        "--compact",
        action="store_true",
        help="return only the selected candidate view in JSON output",
    )
    candidate_repair = candidate_sub.add_parser(
        "repair",
        help="prepare one bounded managed repair task for a composition conflict",
    )
    candidate_repair.add_argument("--candidate", required=True)
    _add_host_reference_arguments(candidate_repair, role="repair task")
    candidate_withdraw = candidate_sub.add_parser(
        "withdraw", help="withdraw one pending candidate that is not in an active batch"
    )
    candidate_withdraw.add_argument("--candidate", required=True)
    candidate_withdraw.add_argument(
        "--reason", required=True, help="one-line reason retained with the withdrawal"
    )
    candidate_restore = candidate_sub.add_parser(
        "restore-branch",
        help="preview or restore an older candidate's original local branch",
    )
    candidate_restore.add_argument("--candidate", required=True)
    candidate_restore.add_argument(
        "--apply", action="store_true", help="create only the missing exact branch"
    )

    batch = sub.add_parser(
        "batch", help="close a smaller tail, inspect, or recover candidate integration"
    )
    batch_sub = batch.add_subparsers(dest="batch_command", required=True)
    batch_status = batch_sub.add_parser("status", help="show integration batches")
    batch_status.add_argument("--batch")
    batch_seal = batch_sub.add_parser(
        "seal",
        help="explicitly close and integrate the exact listed tail candidates",
    )
    batch_seal.add_argument("--candidate", action="append")
    batch_seal.add_argument(
        "--task", action="append", help="freeze one exact native Ready task id"
    )
    batch_seal.add_argument(
        "--after-failed-batch",
        help="create one reviewed idempotent generation after this exact failed batch",
    )
    _add_tail_request_arguments(batch_seal)
    _add_host_reference_arguments(batch_seal, role="integration batch")
    batch_reconcile = batch_sub.add_parser(
        "reconcile",
        help="freeze one full or explicitly requested tail batch from persisted facts",
    )
    batch_reconcile.add_argument(
        "--force",
        action="store_true",
        help="explicitly integrate the current exact pending tail",
    )
    batch_reconcile.add_argument(
        "--cause",
        choices=(
            "heartbeat",
            "finish",
            "abandon",
            "session-end",
            "user",
            "deploy",
            "dependency",
            "round-complete",
        ),
        default="heartbeat",
    )
    batch_reconcile.add_argument(
        "--reason",
        help="one-line basis for a forced smaller tail",
    )
    batch_reconcile.add_argument(
        "--base", help="target branch for a native task-head batch"
    )
    _add_host_reference_arguments(batch_reconcile, role="integration batch")
    batch_recover = batch_sub.add_parser(
        "recover",
        help="resume an interrupted sealed generation from recorded Git facts",
    )
    batch_recover.add_argument("--batch", required=True)
    recovery_source = batch_sub.add_parser(
        "recovery-source",
        help="verify one passed exact source for recovery installation",
    )
    recovery_source.add_argument("--commit", required=True)
    batch_reopen = batch_sub.add_parser(
        "reopen",
        help="reopen only an unactivated, prevalidation Adapter-failed reusable batch",
    )
    batch_reopen.add_argument("--batch", required=True)
    batch_reopen.add_argument(
        "--confirm",
        required=True,
        help="repeat the exact batch id to acknowledge that its candidates will return to pending",
    )
    batch_reopen.add_argument(
        "--confirm-no-runtime-started",
        required=True,
        help="repeat the exact batch id after verifying the failed Adapter did not start runtime resources",
    )
    batch_retire = batch_sub.add_parser(
        "retire",
        help="idempotently remove one exact failed batch worktree while preserving candidates",
    )
    batch_retire.add_argument("--batch", required=True)
    batch_retire.add_argument(
        "--fast",
        action="store_true",
        help="skip per-file dependency content proofs after the fast safety preflight",
    )
    batch_sub.add_parser(
        "metrics",
        help="derive batch-size and validation-cost metrics from existing facts",
    )

    host_handoff = sub.add_parser(
        "host-handoff",
        help="record and resume host-native repair handoffs without invoking host APIs",
    )
    host_handoff_sub = host_handoff.add_subparsers(
        dest="host_handoff_command", required=True
    )
    host_handoff_sub.add_parser(
        "status", help="show durable repair handoffs and next host actions"
    )
    host_handoff_batch = host_handoff_sub.add_parser(
        "batch", help="inspect or explicitly take over a batch coordinator receipt"
    )
    host_handoff_batch_sub = host_handoff_batch.add_subparsers(
        dest="host_handoff_batch_command", required=True
    )
    host_handoff_batch_take_over = host_handoff_batch_sub.add_parser(
        "take-over",
        help="replace an unavailable batch coordinator at one exact revision",
    )
    host_handoff_batch_take_over.add_argument("--batch", required=True)
    host_handoff_batch_take_over.add_argument(
        "--expected-revision", type=int, required=True
    )
    host_handoff_batch_take_over.add_argument("--reason", required=True)
    _add_host_reference_arguments(
        host_handoff_batch_take_over, role="integration batch"
    )
    host_handoff_repair = host_handoff_sub.add_parser(
        "repair",
        help="dispatch, receive, take over, or prepare one durable repair request",
    )
    host_handoff_repair_sub = host_handoff_repair.add_subparsers(
        dest="host_handoff_repair_command", required=True
    )
    host_handoff_dispatch = host_handoff_repair_sub.add_parser(
        "dispatch",
        help="return the stable message payload for the recorded repair assignee",
    )
    host_handoff_dispatch.add_argument("--request", required=True)
    host_handoff_dispatch.add_argument(
        "--retry",
        action="store_true",
        help="prepare one explicit resend after a delivery failure or uncertainty",
    )
    _add_host_reference_arguments(host_handoff_dispatch, role="repair dispatch")
    host_handoff_delivery = host_handoff_repair_sub.add_parser(
        "delivery",
        help="record the actual native-host delivery outcome for one repair request",
    )
    host_handoff_delivery.add_argument("--request", required=True)
    host_handoff_delivery.add_argument(
        "--outcome", choices=("sent", "uncertain", "failed"), required=True
    )
    host_handoff_delivery.add_argument(
        "--detail", help="optional safe one-line delivery evidence or failure detail"
    )
    host_handoff_delivery.add_argument(
        "--attempt", help="delivery attempt id returned by repair dispatch"
    )
    _add_host_reference_arguments(host_handoff_delivery, role="repair dispatch")
    host_handoff_attribute = host_handoff_repair_sub.add_parser(
        "attribute",
        help="record a coordinator-reviewed validation failure that needs one repair",
    )
    host_handoff_attribute.add_argument("--batch", required=True)
    host_handoff_attribute.add_argument("--candidate", required=True)
    host_handoff_attribute.add_argument(
        "--evidence", required=True, help="single-line test, log, or candidate evidence"
    )
    _add_host_reference_arguments(host_handoff_attribute, role="validation attribution")
    host_handoff_claim = host_handoff_repair_sub.add_parser(
        "claim", help="acknowledge receipt as the exact current repair assignee"
    )
    host_handoff_claim.add_argument("--request", required=True)
    _add_host_reference_arguments(host_handoff_claim, role="repair assignee")
    host_handoff_take_over = host_handoff_repair_sub.add_parser(
        "take-over", help="explicitly replace an unavailable repair assignee"
    )
    host_handoff_take_over.add_argument("--request", required=True)
    host_handoff_take_over.add_argument("--reason", required=True)
    _add_host_reference_arguments(host_handoff_take_over, role="repair assignee")
    host_handoff_prepare = host_handoff_repair_sub.add_parser(
        "prepare", help="claim then create or return the single managed repair task"
    )
    host_handoff_prepare.add_argument("--request", required=True)
    _add_host_reference_arguments(host_handoff_prepare, role="repair assignee")
    host_handoff_result_dispatch = host_handoff_repair_sub.add_parser(
        "result-dispatch",
        help="return a published repair candidate message for the current coordinator",
    )
    host_handoff_result_dispatch.add_argument("--request", required=True)
    host_handoff_result_dispatch.add_argument("--retry", action="store_true")
    _add_host_reference_arguments(
        host_handoff_result_dispatch, role="repair result sender"
    )
    host_handoff_result_delivery = host_handoff_repair_sub.add_parser(
        "result-delivery",
        help="record the actual native-host delivery outcome for a repair candidate",
    )
    host_handoff_result_delivery.add_argument("--request", required=True)
    host_handoff_result_delivery.add_argument(
        "--outcome", choices=("sent", "uncertain", "failed"), required=True
    )
    host_handoff_result_delivery.add_argument("--detail")
    host_handoff_result_delivery.add_argument(
        "--attempt", help="delivery attempt id returned by repair result-dispatch"
    )
    _add_host_reference_arguments(
        host_handoff_result_delivery, role="repair result sender"
    )

    runtime = sub.add_parser(
        "runtime", help="ask the project Adapter to verify a delivered runtime"
    )
    runtime_sub = runtime.add_subparsers(dest="runtime_command", required=True)
    runtime_verify = runtime_sub.add_parser(
        "verify", help="verify that one integrated candidate is effective at runtime"
    )
    runtime_verify.add_argument("--candidate", required=True)

    anchor = sub.add_parser("anchor", help="read or update the active task anchor")
    anchor_sub = anchor.add_subparsers(dest="anchor_command", required=True)
    anchor_show = anchor_sub.add_parser(
        "show", help="show one task anchor summary; use --content for its full body"
    )
    anchor_show.add_argument("--task", required=True)
    anchor_show.add_argument(
        "--with-root",
        action="store_true",
        help="include the bound root plan and review state",
    )
    anchor_show.add_argument(
        "--content", action="store_true", help="include the complete task-anchor body"
    )
    anchor_show.add_argument(
        "--root-content",
        action="store_true",
        help="with --with-root, include the complete root-anchor body",
    )
    anchor_update = anchor_sub.add_parser(
        "update", help="atomically update one task anchor from a UTF-8 file"
    )
    anchor_update.add_argument("--task", required=True)
    anchor_update.add_argument("--lease", required=True)
    anchor_update.add_argument("--file", type=Path, required=True)
    anchor_update.add_argument("--expected-sha256", required=True)
    anchor_adopt = anchor_sub.add_parser(
        "adopt", help="adopt one legacy task after reviewing its execution contract"
    )
    anchor_adopt.add_argument("--task", required=True)
    anchor_adopt.add_argument("--objective", required=True)
    anchor_adopt.add_argument("--target", required=True)
    anchor_adopt.add_argument("--scope", required=True)
    anchor_adopt.add_argument("--acceptance", required=True)
    anchor_adopt.add_argument("--confirm", required=True)
    anchor_acknowledge = anchor_sub.add_parser(
        "acknowledge-root",
        help="record that this task reviewed one exact root plan version",
    )
    anchor_acknowledge.add_argument("--task", required=True)
    anchor_acknowledge.add_argument("--lease", required=True)
    anchor_acknowledge.add_argument("--root-version", type=int, required=True)
    anchor_acknowledge.add_argument("--root-sha256", required=True)
    anchor_refresh = anchor_sub.add_parser(
        "refresh-root",
        help="read and record the current root context in one host operation",
    )
    anchor_refresh.add_argument("--task", required=True)
    anchor_refresh.add_argument("--lease", required=True)
    anchor_bind_root = anchor_sub.add_parser(
        "bind-root",
        help="bind an existing active or ready task to one exact root anchor",
    )
    anchor_bind_root.add_argument("--task", required=True)
    anchor_bind_root.add_argument("--lease", required=True)
    anchor_bind_root.add_argument("--root", required=True)
    anchor_bind_root.add_argument(
        "--root-anchor-file",
        type=Path,
        help="explicit absolute external root-anchor file for a cross-repository root",
    )

    commit = sub.add_parser(
        "commit", help="stage only an exact reviewed task path list and commit it"
    )
    commit.add_argument("--task", required=True)
    commit.add_argument("--lease", required=True)
    commit.add_argument("--message", required=True)
    commit.add_argument("--path", action="append", default=[], required=True)
    commit.add_argument("--session")

    for name in ("ready", "finish"):
        item = sub.add_parser(name)
        item.add_argument("--task", required=True)
        item.add_argument("--lease", required=True)
        item.add_argument("--session")
        if name == "finish":
            _add_tail_request_arguments(item)
            _add_host_reference_arguments(item, role="candidate publication")

    retarget_parser = sub.add_parser(
        "retarget", help="explicitly rebind a task after its base branch changed"
    )
    retarget_parser.add_argument("--task", required=True)
    retarget_parser.add_argument("--lease", required=True)
    retarget_parser.add_argument("--base", required=True)
    retarget_parser.add_argument(
        "--confirm", required=True, help="exactly TASK_ID:BASE_BRANCH"
    )

    plan = sub.add_parser(
        "plan", help="read the registered verification plan for one task"
    )
    plan.add_argument("--task", required=True)
    plan.add_argument(
        "--level",
        choices=["development", "ready", "full", "stress"],
        help="show one execution phase; omit it to compare every registered phase",
    )
    plan.add_argument(
        "--complete",
        action="store_true",
        help="with --level full, include the explicit complete-regression phase",
    )

    verify = sub.add_parser(
        "verify",
        help="run only registered development, ready, or explicit full profiles",
    )
    verify.add_argument("--task", required=True)
    verify.add_argument("--lease", required=True)
    verify.add_argument("--session")
    verify.add_argument(
        "--level",
        choices=["development", "ready", "full", "stress"],
        default="development",
    )
    verify.add_argument(
        "--complete",
        action="store_true",
        help="at level full, include complete-regression profiles as an explicit milestone check",
    )

    status = sub.add_parser("status", help="show current DWW work or one exact object")
    status.add_argument("--detailed", action="store_true")
    status.add_argument(
        "--compact",
        action="store_true",
        help="return a current, read-only JSON view instead of legacy full history",
    )
    status.add_argument(
        "--history",
        action="store_true",
        help="include historical entries in the compact view",
    )
    status_selector = status.add_mutually_exclusive_group()
    status_selector.add_argument("--task", help="show one exact task and its delivery")
    status_selector.add_argument("--root", help="show one exact root and its children")
    status_selector.add_argument("--batch", help="show one exact integration batch")

    recover = sub.add_parser(
        "recover",
        help="recover an interrupted task from persisted identity and Git facts",
    )
    recover.add_argument("--task", required=True)
    _add_host_reference_arguments(recover, role="candidate publication recovery")
    recover.add_argument(
        "--repair-runtime-adapter",
        action="store_true",
        help="convert one failed pre-activation task into a path-restricted Runtime Adapter repair",
    )
    recover.add_argument(
        "--path",
        action="append",
        dest="repair_path",
        help="exact approved Adapter input path allowed for this repair; repeat for multiple paths",
    )

    handoff_parser = sub.add_parser(
        "handoff",
        help="explicitly transfer an interrupted isolated task without changing its worktree",
    )
    handoff_parser.add_argument("--task", required=True)
    handoff_parser.add_argument(
        "--confirm", required=True, help="exactly TASK_ID:BRANCH:HEAD"
    )
    _add_host_reference_arguments(handoff_parser, role="isolated handoff recipient")

    abandoned = sub.add_parser(
        "abandon", help="explicitly discard one task after exact confirmation"
    )
    abandoned.add_argument("--task", required=True)
    abandoned.add_argument("--lease", required=True)
    abandoned.add_argument("--confirm", required=True)
    abandoned.add_argument(
        "--reason", required=True, help="one-line reason retained with the abandonment"
    )
    abandoned.add_argument(
        "--retain-worktree",
        action="store_true",
        help="mark an isolated task terminal while preserving every worktree file and quarantining its slot",
    )
    abandoned.add_argument("--session")

    reclaim_retained = sub.add_parser(
        "reclaim-retained",
        help="review then safely return one retained terminal worktree to its slot",
    )
    reclaim_retained.add_argument("--task", required=True)
    reclaim_retained.add_argument(
        "--confirm",
        help="exact confirmation returned with the current deletion checklist",
    )
    reclaim_retained.add_argument(
        "--dispose",
        action="store_true",
        help="explicitly dispose the exact retained root, then recreate its slot",
    )

    resume = sub.add_parser(
        "resume-in-place",
        help="resume a quarantined in-place task after manually restoring its recorded branch and HEAD",
    )
    resume.add_argument("--task", required=True)
    resume.add_argument("--session", required=True)
    resume.add_argument("--confirm", required=True)

    warm = sub.add_parser(
        "warm-slot", help="serially prepare declared dependencies in one idle slot"
    )
    warm.add_argument("--slot", required=True)

    dev = sub.add_parser("dev", help="manage one configured owned development process")
    dev_sub = dev.add_subparsers(dest="dev_command", required=True)
    for name in ("start", "stop"):
        item = dev_sub.add_parser(name)
        item.add_argument("--task", required=True)
        item.add_argument("--lease", required=True)

    for name, target in (("prune-proofs", "proofs"), ("prune-logs", "logs")):
        prune = sub.add_parser(
            name,
            help=f"explicitly delete local {target} and invalidate dependent reuse",
        )
        prune.add_argument("--confirm", choices=["PRUNE"], required=True)
    prune_slot = sub.add_parser(
        "prune-slot",
        help="plan or execute cleanup of declared paths in one empty managed slot",
    )
    prune_slot.add_argument("--slot", required=True)
    prune_slot.add_argument("--plan", help="plan id returned by a previous prune-slot")
    prune_slot.add_argument(
        "--confirm", help="exact digest returned by a previous prune-slot plan"
    )

    deinitialize = sub.add_parser(
        "deinit", help="safely remove adopted policy and exact managed slots"
    )
    deinitialize.add_argument("--confirm", choices=["DEINIT"], required=True)
    deinitialize.add_argument(
        "--message",
        required=True,
        help="repository-conventional cleanup commit message",
    )
    registered_commands = frozenset(sub.choices)
    if registered_commands != TOP_LEVEL_COMMANDS:
        missing = sorted(TOP_LEVEL_COMMANDS - registered_commands)
        unexpected = sorted(registered_commands - TOP_LEVEL_COMMANDS)
        raise RuntimeError(
            "DWW top-level command contract drift: "
            + ", ".join(
                [
                    *(f"missing {item}" for item in missing),
                    *(f"unexpected {item}" for item in unexpected),
                ]
            )
        )
    return parser


def _approval_request(repo: GitRepo, args: argparse.Namespace) -> dict[str, Any]:
    """将 CLI 选择器转换为实际执行目录和最小批准计划。"""
    selectors = {
        key: value
        for key, value in {
            "task": args.task,
            "slot": args.slot,
            "batch": args.batch,
            "candidate": args.candidate,
        }.items()
        if value
    }
    if args.scope != "all" and not selectors:
        raise SoloAIError(
            "approve --scope requires one of --task, --slot, --batch, or --candidate"
        )
    if not selectors:
        verification = load_verification_config(repo)
        return {
            "verification": verification,
            "cwd": repo.policy_path(),
            "scope": args.scope,
        }
    if args.task:
        task = StateStore(repo).task(args.task)
        cwd = Path(str(task["worktree"]))
        verification = load_verification_config(repo, cwd=cwd)
        config = load_repo_config(repo, cwd=cwd)
        profile_ids: tuple[str, ...] = ()
        level_for_scope = {
            "development": ("development", None),
            "ready": ("ready", None),
            "finish": ("ready", None),
            "full": ("full", "integration"),
            "complete": ("full", "complete"),
            "stress": ("stress", None),
        }.get(args.scope)
        if level_for_scope is not None:
            level, full_scope = level_for_scope
            if args.scope == "finish":
                policy = task.get("integration_policy") or {}
                requires_ready = (
                    policy.get("mode") != "batched"
                    or policy.get("candidate_validation", "ready") != "batch"
                )
                if not requires_ready:
                    level_for_scope = None
            if level_for_scope is not None:
                levels = (
                    (level,)
                    if level in {"development", "ready"}
                    else (("stress",) if level == "stress" else ("ready", "full"))
                )
                profile_ids = selected_profile_ids(
                    repo,
                    cwd=cwd,
                    base=str(
                        task.get("start_head")
                        or task.get("base_head")
                        or task["base_ref"]
                    ),
                    verification=verification,
                    levels=levels,
                    full_scopes=(
                        ("integration", "complete")
                        if full_scope == "complete"
                        else ((full_scope,) if full_scope else None)
                    ),
                )
        adapter_operations = {
            "runtime-activate": ("activate",),
            "runtime-release": ("release",),
        }.get(args.scope, ())
        return {
            "verification": verification,
            "cwd": cwd,
            "scope": args.scope,
            "profile_ids": profile_ids,
            "include_secret_scanner": args.scope in {"ready", "commit", "finish"}
            and config.secret_scanner is not None,
            "include_dev_start": args.scope == "development",
            "adapter_operations": adapter_operations,
        }
    if args.slot:
        if args.scope != "warm":
            raise SoloAIError("--slot is only valid with approve --scope warm")
        cwd = repo.policy_path()
        return {
            "verification": load_verification_config(repo, cwd=cwd),
            "cwd": cwd,
            "scope": args.scope,
            "include_warm_commands": True,
        }
    if args.batch:
        if args.scope not in {
            "batch-full",
            "runtime-batch-activate",
            "runtime-batch-release",
            "all",
        }:
            raise SoloAIError("--batch requires a batch approval scope")
        batch = CandidateBatchStore(repo).batch(args.batch)
        cwd = Path(str(batch["worktree"]))
        verification = load_verification_config(repo, cwd=cwd)
        config = load_repo_config(repo, cwd=cwd)
        adapter_operations = {
            "runtime-batch-activate": ("batch_activate",),
            "runtime-batch-release": ("batch_release",),
        }.get(args.scope, ())
        profile_ids = ()
        if args.scope == "batch-full":
            profile_ids = selected_profile_ids(
                repo,
                cwd=cwd,
                base=str(batch["base_ref"]),
                verification=verification,
                levels=("ready", "full"),
                full_scopes=("integration",),
            )
        return {
            "verification": verification,
            "cwd": cwd,
            "scope": args.scope,
            "profile_ids": profile_ids,
            "include_secret_scanner": args.scope == "batch-full"
            and config.secret_scanner is not None,
            "adapter_operations": adapter_operations,
        }
    if args.candidate:
        if args.scope not in {"runtime-verify-effective", "all"}:
            raise SoloAIError(
                "--candidate is only valid with runtime verification approval"
            )
        candidate = CandidateBatchStore(repo).candidate(args.candidate)
        if not candidate.get("integrated_batch"):
            raise SoloAIError("Candidate runtime approval requires an integrated batch")
        batch = CandidateBatchStore(repo).batch(str(candidate["integrated_batch"]))
        base_ref = str(batch["base_ref"])
        matching = [
            item.path
            for item in repo.worktrees()
            if not item.bare and repo.branch(item.path) == base_ref
        ]
        if len(matching) != 1:
            raise SoloAIError(
                "Candidate runtime approval requires one stable delivered base worktree"
            )
        cwd = matching[0]
        return {
            "verification": load_verification_config(repo, cwd=cwd),
            "cwd": cwd,
            "scope": args.scope,
            "adapter_operations": ("verify_effective",)
            if args.scope == "runtime-verify-effective"
            else (),
        }
    raise SoloAIError("No approval target was resolved")


def _parse_commands(values: list[str] | None) -> list[CommandSpec] | None:
    if values is None:
        return None
    commands: list[CommandSpec] = []
    for value in values:
        try:
            raw = json.loads(value)
        except json.JSONDecodeError as exc:
            raise SoloAIError(f"--verify must be a JSON argv array: {exc}") from exc
        if (
            not isinstance(raw, list)
            or not raw
            or not all(isinstance(item, str) and item for item in raw)
        ):
            raise SoloAIError("--verify must be a non-empty JSON argv array of strings")
        commands.append(CommandSpec(tuple(raw)))
    return commands


def _parse_json_objects(values: list[str], *, option: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for value in values:
        try:
            item = json.loads(value)
        except json.JSONDecodeError as exc:
            raise SoloAIError(f"{option} must be valid JSON: {exc}") from exc
        if not isinstance(item, dict):
            raise SoloAIError(f"{option} must be a JSON object")
        result.append(item)
    return result


def _orchestration_available_slots(
    repo: GitRepo, batch: dict[str, Any], requested: int | None
) -> int:
    if requested is not None:
        if requested < 0:
            raise SoloAIError("available_slots must not be negative")
        return requested
    return adapter_for(str(batch["adapter"])).available_slots(
        repo, batch_limit=int(batch["max_parallel"])
    )


def _orchestration_status(
    repo: GitRepo, *, batch_id: str | None, available_slots: int | None
) -> dict[str, Any]:
    store = BatchStore(repo)
    batches = [store.batch(batch_id)] if batch_id else store.list()
    return {
        "batches": [
            {
                **batch,
                "frontier": store.frontier(
                    str(batch["id"]),
                    available_slots=_orchestration_available_slots(
                        repo, batch, available_slots
                    ),
                ),
            }
            for batch in batches
        ]
    }


def _status(repo: GitRepo, *, detailed: bool) -> dict[str, Any]:
    store = StateStore(repo)
    store.reconcile_operation_receipts()
    state = store.read()
    route = repository_route(repo)
    candidate_batches = CandidateBatchStore(repo).summary()
    candidates_by_task = {
        str(candidate["task_id"]): candidate
        for candidate in candidate_batches["candidates"]
        if candidate.get("task_id")
    }
    tasks: list[dict[str, Any]] = []
    for task in state.get("tasks", {}).values():
        projected_task = StateStore.public_task(task)
        candidate = candidates_by_task.get(str(task.get("id")))
        if candidate:
            projected_task["candidate_delivery"] = {
                "id": candidate.get("candidate_id"),
                "status": candidate.get("status"),
                "delivery_status": candidate.get("delivery_status"),
                "source_host": candidate.get("host_origin"),
            }
            if candidate.get("batch_ownership"):
                projected_task["batch_ownership"] = candidate["batch_ownership"]
        tasks.append(projected_task)
    from .task_context import list_anchors

    result: dict[str, Any] = {
        "repository": str(repo.root),
        "mode": "uninitialized" if route["action"] == "ask" else route["action"],
        "existing_workflows": detect_existing_workflows(repo.root),
        "default_branch": repo.default_branch(),
        "primary_clean": repo.is_clean(repo.primary_path),
        "invocation_worktree": str(repo.root),
        "invocation_worktree_clean": repo.is_clean(repo.root),
        "local_enabled": local_enabled(repo),
        "validation_queue": queue_status(),
        "slots": list(state.get("slots", {}).values()),
        "tasks": tasks,
        "guard_alerts": store.guard_alerts(),
        "task_anchors": list_anchors(repo),
        "candidate_pool": candidate_batches["candidates"],
        "integration_batches": candidate_batches["batches"],
        "host_handoffs": HostHandoffStore(repo).status(pool=candidate_batches),
    }
    if detailed:
        for slot in result["slots"]:
            size = directory_size(Path(slot["path"]))
            slot["disk_bytes"] = size
            slot["disk"] = format_bytes(size)
        result["local_state_bytes"] = directory_size(repo.local_dir)
    return result


def _version() -> dict[str, Any]:
    plugin_root = Path(__file__).resolve().parents[4]
    manifest_path = plugin_root / ".codex-plugin" / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {
        "version": VERSION,
        "plugin_version": manifest.get("version"),
        "verification_schema": 3,
        "state_schema": STATE_SCHEMA,
        "codex_guard": "optional PreToolUse deny on supported local tool paths after user trusts this plugin hook; core lifecycle is hookless",
        "script": str(Path(sys.argv[0]).resolve()),
        "validation_queue": queue_status(),
    }


def _doctor(repo: GitRepo) -> dict[str, Any]:
    report = _status(repo, detailed=False)
    report["candidate_integrity"] = CandidateBatchStore(repo).integrity_check()
    policy = repo.policy_path()
    if report["mode"] == "managed":
        verification = load_verification_config(repo, cwd=policy)
        plan = approval_plan(repo, cwd=policy, verification=verification)
        from .lifecycle import _approval_fingerprint

        fingerprint, _ = _approval_fingerprint(repo, verification, cwd=policy)
        from .util import read_json

        report["approval_current"] = fingerprint in read_json(
            repo.local_dir / "approvals.json", {"accepted": {}}
        ).get("accepted", {})
        report["validation_plan"] = plan
        agents = repo.root / "AGENTS.md"
        try:
            report["managed_rule_block"] = {
                "status": managed_agents_status(
                    agents.read_text(encoding="utf-8") if agents.exists() else ""
                ),
                "sync": "A normal isolated task may replace only a known legacy block with the current managed block; user-edited or unknown text stays protected.",
            }
        except SoloAIError as exc:
            report["managed_rule_block"] = {
                "status": "unknown-or-user-edited",
                "detail": str(exc),
                "sync": "Inspect the block and keep user rules intact; DWW will not overwrite an unknown managed block.",
            }
    report["deinit_ready"] = (
        report["mode"] == "managed"
        and report["primary_clean"]
        and not any(
            task["status"] not in FINAL_TASK_STATES
            for task in StateStore(repo).read()["tasks"].values()
        )
        and not CandidateBatchStore(repo).active()
    )
    report["uninstall_rule"] = (
        "Run deinit successfully before removing the Codex plugin. The plugin registry never scans disks."
    )
    report["hook_trust"] = (
        "Hooks are optional hardening, not a lifecycle dependency. Codex persists trust "
        "against the exact hook definition. Ordinary updates keep "
        "hooks/hooks.json stable and need no repeated review. Only when Codex reports a "
        "new or changed hook pending review should the AI explain it, ask once, and use "
        "available host UI control after approval. If the host's review state cannot be "
        "read, report that the need for review is unconfirmed. A version change, "
        "parse/path/owner/lease error, or single denial is not evidence that review is "
        "needed; report the actual error and safe next action instead. Otherwise it "
        "must not claim the hard guard is active."
    )
    return report


def _require_idle(repo: GitRepo) -> None:
    state = StateStore(repo).read()
    if any(
        task.get("status") not in FINAL_TASK_STATES for task in state["tasks"].values()
    ):
        raise SoloAIError("Active or quarantined tasks block pruning")
    if any((repo.local_dir / "queue").glob("*.json")):
        raise SoloAIError("Integration queue tickets block pruning")
    if CandidateBatchStore(repo).active():
        raise SoloAIError("Pending candidates or integration batches block pruning")
    locks = repo.local_dir / "locks"
    # A stale lock is not removed by pruning; doctor/recover must assess it first.
    if locks.exists() and any(path.name != "state.lock" for path in locks.iterdir()):
        raise SoloAIError("A lifecycle lock exists; pruning is unsafe")


def _cleanup_target(path: Path, root: Path) -> dict[str, Any]:
    """为声明目标生成可复核摘要；任何保护项或链接都会停止整次清理。"""
    if is_link_or_junction(path):
        raise SoloAIError(f"Cleanup target contains a link or junction: {path}")
    path = ensure_within(path, root)
    entries: list[dict[str, Any]] = []
    total_bytes = 0

    def add(candidate: Path, *, directory: bool) -> None:
        nonlocal total_bytes
        if is_link_or_junction(candidate):
            raise SoloAIError(
                f"Cleanup target contains a link or junction: {candidate}"
            )
        relative = str(candidate.relative_to(root)).replace("\\", "/")
        if classify_cleanup_path(relative) != "ordinary":
            raise SoloAIError(
                f"Cleanup target contains retained or protected content: {candidate}"
            )
        if directory:
            entries.append({"path": relative, **snapshot_plain_path(candidate)})
            return
        size = candidate.stat().st_size
        total_bytes += size
        entries.append(
            {
                "path": relative,
                "kind": "file",
                "size": size,
                "sha256": sha256_file(candidate),
                **path_identity(candidate),
            }
        )

    if path.is_file():
        add(path, directory=False)
    else:
        add(path, directory=True)
        for current, directories, files in os.walk(path, followlinks=False):
            current_path = Path(current)
            for name in sorted(directories):
                add(current_path / name, directory=True)
            for name in sorted(files):
                add(current_path / name, directory=False)
    return {
        "path": str(path.relative_to(root)).replace("\\", "/"),
        "kind": "file" if path.is_file() else "directory",
        "bytes": total_bytes,
        "delete_reason": "declared cleanup.owned_paths entry",
        "contents_digest": sha256_text(stable_json(entries)),
        "entries": entries,
    }


def _slot_prune_payload(repo: GitRepo, *, slot: str) -> dict[str, Any]:
    policy = repo.policy_path()
    config = load_repo_config(repo, cwd=policy)
    store = StateStore(repo)
    state = store.require_slot_layout(config)
    details = state["slots"].get(slot)
    if not details or details.get("status") not in {"idle", "inactive"}:
        raise SoloAIError("Only an empty idle or inactive slot can be pruned")
    root = ensure_within(Path(details["path"]), store.managed_worktree_root(config))
    if not any(item.path == root for item in repo.worktrees()):
        if root.exists():
            raise SoloAIError(
                "Slot path is not registered with Git and is retained; recover or inspect it manually"
            )
        return {"slot": slot, "worktree_retained": False, "targets": []}
    store.require_slot_ownership(slot, root)
    targets: list[dict[str, Any]] = []
    for relative in config.cleanup_owned_paths:
        candidate = root / relative
        if is_link_or_junction(candidate):
            raise SoloAIError(
                f"Cleanup target contains a link or junction: {candidate}"
            )
        candidate = ensure_within(candidate, root)
        if candidate.exists() or candidate.is_symlink():
            targets.append(_cleanup_target(candidate, root))
    return {
        "slot": slot,
        "worktree": str(root),
        "worktree_retained": True,
        "worktree_identity": path_identity(root),
        "targets": targets,
        "owned_paths": list(config.cleanup_owned_paths),
        "slot_generation": int(details.get("generation", 0)),
    }


def _plan_slot_prune(repo: GitRepo, *, slot: str) -> dict[str, Any]:
    payload = _slot_prune_payload(repo, slot=slot)
    digest = sha256_text(stable_json(payload))
    plan_id = new_id(f"cleanup-slot-{slot}")
    plan = {
        "schema_version": 1,
        "id": plan_id,
        "digest": digest,
        "created_at": utc_timestamp(),
        "payload": payload,
    }
    atomic_write_json(repo.local_dir / "cleanup-plans" / f"{plan_id}.json", plan)
    return {
        "status": "planned",
        "plan_id": plan_id,
        "digest": digest,
        **payload,
        "next": f"prune-slot --slot {slot} --plan {plan_id} --confirm {digest}",
    }


def _execute_slot_prune(
    repo: GitRepo, *, slot: str, plan_id: str, confirm: str
) -> dict[str, Any]:
    plan_path = repo.local_dir / "cleanup-plans" / f"{plan_id}.json"
    plan = read_json(plan_path, {})
    if not plan or plan.get("schema_version") != 1:
        raise SoloAIError("Unknown cleanup plan; generate a new plan before pruning")
    if plan.get("payload", {}).get("slot") != slot:
        raise SoloAIError("Cleanup plan belongs to a different slot")
    if confirm != plan.get("digest"):
        raise SoloAIError("Cleanup confirmation must exactly match the planned digest")
    status = str(plan.get("status", "planned"))
    if status == "completed":
        completed_payload = plan.get("payload") or {}
        if completed_payload.get("worktree_retained"):
            completed_root = Path(str(completed_payload["worktree"]))
            require_managed_directory_identity(
                completed_root,
                managed_root=completed_root,
                expected_identity=dict(completed_payload["worktree_identity"]),
                expected_root_identity=dict(completed_payload["worktree_identity"]),
            )
            late = [
                str(completed_root / str(target["path"]))
                for target in completed_payload.get("targets", [])
                if (completed_root / str(target["path"])).exists()
                or (completed_root / str(target["path"])).is_symlink()
            ]
            if late:
                plan.update(
                    {
                        "status": "quarantined",
                        "quarantine_reason": "Cleanup source reappeared after completion",
                    }
                )
                atomic_write_json(plan_path, plan)
                StateStore(repo).quarantine_idle_slot_generation(
                    slot,
                    generation=int(completed_payload["slot_generation"]),
                    reason="Cleanup source reappeared after completed plan",
                )
                raise SoloAIError(
                    "Cleanup source reappeared after completion; the plan was quarantined"
                )
        raise SoloAIError(
            "Cleanup plan was already executed and completed; it cannot be replayed"
        )
    if status not in {"planned", "creating", "executing", "deleting"}:
        raise SoloAIError("Cleanup plan has an unsupported recovery state")
    payload = dict(plan["payload"])
    if status == "planned":
        current = _slot_prune_payload(repo, slot=slot)
        if sha256_text(stable_json(current)) != plan["digest"]:
            raise SoloAIError(
                "Cleanup plan changed after review; nothing was deleted. Generate and review a new plan."
            )
    else:
        current = payload
        state = StateStore(repo).read()
        details = state["slots"].get(slot) or {}
        if (
            details.get("status") not in {"idle", "inactive"}
            or details.get("task_id")
            or int(details.get("generation", 0))
            != int(payload.get("slot_generation", 0))
        ):
            raise SoloAIError(
                "Cleanup slot generation or ownership changed during recovery"
            )
    if not payload["worktree_retained"]:
        plan.update({"status": "completed", "completed_at": utc_timestamp()})
        atomic_write_json(plan_path, plan)
        return {
            "status": "pruned",
            "slot": slot,
            "plan_id": plan_id,
            "removed": [],
            "worktree_retained": False,
        }
    root = Path(str(payload["worktree"]))
    require_managed_directory_identity(
        root,
        managed_root=root,
        expected_identity=dict(payload["worktree_identity"]),
        expected_root_identity=dict(payload["worktree_identity"]),
    )
    staging = root / f".dww-prune-{plan_id}"
    expected_staging = str(staging.absolute())
    marker = staging / ".dww-staging-owner"

    def require_staging() -> None:
        if not plan.get("staging_identity"):
            raise SoloAIError(
                "Cleanup staging identity is missing; files were preserved"
            )
        require_managed_directory_identity(
            staging,
            managed_root=root,
            expected_resolved=expected_staging,
            expected_root_resolved=str(root.resolve()),
            expected_identity=dict(plan["staging_identity"]),
            expected_root_identity=dict(payload["worktree_identity"]),
        )

    if status == "planned":
        if staging.exists() or staging.is_symlink():
            raise SoloAIError(
                "Cleanup staging path already exists; nothing was deleted"
            )
        plan.update(
            {
                "status": "creating",
                "staging": str(staging),
                "staging_resolved": expected_staging,
                "staging_nonce": new_id("staging"),
                "started_at": utc_timestamp(),
            }
        )
        # 在创建暂存区和首次移动前先持久化事务；崩溃后只能继续这张计划。
        atomic_write_json(plan_path, plan)
        status = "creating"
    elif plan.get("staging_resolved") != expected_staging:
        raise SoloAIError("Cleanup staging identity changed")

    if status == "creating":
        if not staging.exists():
            require_managed_directory_identity(
                root,
                managed_root=root,
                expected_identity=dict(payload["worktree_identity"]),
                expected_root_identity=dict(payload["worktree_identity"]),
            )
            staging.mkdir()
        require_managed_directory_identity(staging, managed_root=root)
        unexpected = [item for item in staging.iterdir() if item != marker]
        if unexpected:
            raise SoloAIError(
                "Cleanup staging was populated before ownership was recorded"
            )
        if marker.exists():
            if marker.read_text(encoding="utf-8") != str(plan["staging_nonce"]):
                raise SoloAIError("Cleanup staging ownership marker changed")
        else:
            try:
                with marker.open("x", encoding="utf-8", newline="\n") as handle:
                    handle.write(str(plan["staging_nonce"]))
            except FileExistsError as exc:
                raise SoloAIError(
                    "Cleanup staging ownership marker appeared concurrently"
                ) from exc
        plan["staging_identity"] = path_identity(staging)
        plan["marker_identity"] = snapshot_plain_path(marker)
        plan["status"] = "executing"
        atomic_write_json(plan_path, plan)
        status = "executing"

    if status == "executing":
        require_staging()
        if snapshot_plain_path(marker) != plan.get(
            "marker_identity"
        ) or marker.read_text(encoding="utf-8") != str(plan["staging_nonce"]):
            raise SoloAIError("Cleanup staging ownership marker changed")
        for target in payload["targets"]:
            require_staging()
            source = ensure_within(root / str(target["path"]), root)
            destination = staging / str(target["path"])
            source_exists = source.exists() or source.is_symlink()
            destination_exists = destination.exists() or destination.is_symlink()
            if source_exists and destination_exists:
                raise SoloAIError(
                    "Cleanup source was recreated during staging; files were preserved"
                )
            if source_exists:
                if is_link_or_junction(source):
                    raise SoloAIError(
                        f"Cleanup target became a link or junction: {source}"
                    )
                if stable_json(_cleanup_target(source, root)) != stable_json(target):
                    raise SoloAIError("Cleanup target changed before staging")
                require_staging()
                destination.parent.mkdir(parents=True, exist_ok=True)
                require_staging()
                source.rename(destination)
            elif not destination_exists:
                raise SoloAIError(
                    "Cleanup target disappeared outside the recorded transaction"
                )
            require_staging()
            if stable_json(_cleanup_target(destination, staging)) != stable_json(
                target
            ):
                raise SoloAIError("Cleanup target changed while being staged")
        plan["status"] = "deleting"
        plan["deleting_at"] = utc_timestamp()
        atomic_write_json(plan_path, plan)
        status = "deleting"

    removed = [str(root / str(target["path"])) for target in payload["targets"]]
    if status == "deleting":
        if any(
            (root / str(target["path"])).exists()
            or (root / str(target["path"])).is_symlink()
            for target in payload["targets"]
        ):
            raise SoloAIError(
                "Cleanup source reappeared after staging; files were preserved"
            )
        if not staging.exists():
            pass
        else:
            require_staging()
            expected_entries = {
                entry["path"]: entry
                for target in payload["targets"]
                for entry in target.get("entries", [])
            }
            descendants: list[Path] = []
            for current_directory, directories, files in os.walk(
                staging, followlinks=False
            ):
                require_staging()
                current_path = Path(current_directory)
                for name in [*directories, *files]:
                    candidate = current_path / name
                    if candidate == marker:
                        continue
                    if is_link_or_junction(candidate):
                        raise SoloAIError(
                            f"Cleanup staging contains a link or junction: {candidate}"
                        )
                    relative = candidate.relative_to(staging).as_posix()
                    expected = expected_entries.get(relative)
                    if not expected or classify_cleanup_path(relative) != "ordinary":
                        raise SoloAIError(
                            f"Cleanup staging contains unreviewed content: {candidate}"
                        )
                    observed = snapshot_plain_path(candidate)
                    comparable = {
                        key: value for key, value in expected.items() if key != "path"
                    }
                    if observed != comparable:
                        raise SoloAIError(
                            "Cleanup staging content changed after review"
                        )
                    descendants.append(candidate)
            for candidate in sorted(
                descendants, key=lambda item: len(item.parts), reverse=True
            ):
                require_staging()
                relative = candidate.relative_to(staging).as_posix()
                expected = expected_entries[relative]
                delete_plain_path_if_unchanged(
                    candidate,
                    {key: value for key, value in expected.items() if key != "path"},
                )
            require_staging()
            if marker.exists():
                delete_plain_path_if_unchanged(marker, dict(plan["marker_identity"]))
            # marker 已缺失表示上次进程已完成该条件删除子阶段。
            require_managed_directory_identity(
                staging,
                managed_root=root,
                expected_resolved=expected_staging,
                expected_root_resolved=str(root.resolve()),
                expected_identity=dict(plan["staging_identity"]),
                expected_root_identity=dict(payload["worktree_identity"]),
            )
            delete_plain_path_if_unchanged(
                staging, {**dict(plan["staging_identity"]), "kind": "directory"}
            )
        if any(
            (root / str(target["path"])).exists()
            or (root / str(target["path"])).is_symlink()
            for target in payload["targets"]
        ):
            raise SoloAIError("Cleanup source reappeared before plan completion")
    plan["status"] = "completed"
    plan["completed_at"] = utc_timestamp()
    atomic_write_json(plan_path, plan)
    late_sources = [
        str(root / str(target["path"]))
        for target in payload["targets"]
        if (root / str(target["path"])).exists()
        or (root / str(target["path"])).is_symlink()
    ]
    if late_sources:
        plan.update(
            {
                "status": "quarantined",
                "quarantine_reason": "Cleanup source reappeared at completion",
            }
        )
        atomic_write_json(plan_path, plan)
        StateStore(repo).quarantine_idle_slot_generation(
            slot,
            generation=int(payload["slot_generation"]),
            reason="Cleanup source reappeared at plan completion",
        )
        raise SoloAIError(
            "Cleanup source reappeared at completion; the plan was quarantined"
        )
    return {
        "status": "pruned",
        "slot": slot,
        "plan_id": plan_id,
        "removed": removed,
        "worktree_retained": payload["worktree_retained"],
    }


def _prune(
    repo: GitRepo,
    *,
    kind: str,
    slot: str | None = None,
    plan_id: str | None = None,
    confirm: str | None = None,
) -> dict[str, Any]:
    with maintenance_lock(repo):
        _require_idle(repo)
        if kind in {"proofs", "logs"}:
            targets = [repo.local_dir / kind]
            if kind == "proofs":
                targets.append(repo.local_dir / "profile-proofs")
            else:
                targets.append(repo.local_dir / "validation-runs")
            removed: list[str] = []
            for target in targets:
                if target.exists():
                    shutil.rmtree(target)
                    removed.append(str(target))
            return {
                "removed": removed,
                "proof_reuse": "invalidated"
                if kind in {"proofs", "logs"}
                else "unchanged",
            }
        if not slot:
            raise SoloAIError("Slot is required")
        if plan_id is None and confirm is None:
            return _plan_slot_prune(repo, slot=slot)
        if not plan_id or not confirm:
            raise SoloAIError("PruneSlot execution requires both --plan and --confirm")
        return _execute_slot_prune(repo, slot=slot, plan_id=plan_id, confirm=confirm)


def _dispatch(args: argparse.Namespace) -> dict[str, Any]:
    repo = GitRepo(args.repo)
    if args.command == "delegated":
        if args.delegated_command == "inspect":
            return inspect_delegated(repo.root, repo.common_dir)
        if args.delegated_command == "approve":
            return approve_delegated(
                repo.root,
                repo.common_dir,
                fingerprint=args.fingerprint,
            )
        if args.delegated_command == "revoke":
            return revoke_delegated(
                repo.common_dir,
                adapter_id=args.adapter_id,
                fingerprint=args.fingerprint,
            )
        if args.delegated_command == "invoke":
            request = _parse_json_objects([args.request], option="--request")[0]
            return invoke_delegated(
                repo.root,
                repo.common_dir,
                operation=args.operation,
                request=request,
                timeout_seconds=args.timeout_seconds,
            )
        raise SoloAIError(f"Unknown delegated command: {args.delegated_command}")
    if args.command == "orchestrate":
        store = BatchStore(repo)
        command = args.orchestration_command
        if command == "plan":
            raise SoloAIError(
                "DWW task orchestration is retired for new work; use the host's native task/subagent system. Existing orchestration batches remain drainable."
            )
        if command == "confirm":
            return store.confirm(args.batch, controller=args.controller)
        if command == "status":
            return _orchestration_status(
                repo,
                batch_id=args.batch,
                available_slots=args.available_slots,
            )
        if command == "frontier":
            batch = store.batch(args.batch)
            return {
                "batch_id": args.batch,
                "tasks": store.frontier(
                    args.batch,
                    available_slots=_orchestration_available_slots(
                        repo, batch, args.available_slots
                    ),
                ),
            }
        if command == "claim":
            batch = store.batch(args.batch)
            return store.claim(
                args.batch,
                task_id=args.task,
                worker=args.worker,
                controller=args.controller,
                available_slots=_orchestration_available_slots(repo, batch, None),
            )
        if command == "link":
            return store.link_lifecycle_task(
                args.batch,
                task_id=args.task,
                lifecycle_task=args.lifecycle_task,
                controller=args.controller,
            )
        if command == "complete":
            return store.complete(
                args.batch,
                task_id=args.task,
                evidence=_parse_json_objects(args.evidence, option="--evidence"),
                controller=args.controller,
            )
        if command == "block":
            return store.block(
                args.batch,
                task_id=args.task,
                reason=args.reason,
                controller=args.controller,
            )
        if command == "record-attempt":
            return store.record_attempt(
                args.batch,
                task_id=args.task,
                changed=args.changed == "true",
                summary=args.summary,
                controller=args.controller,
            )
        if command == "pause":
            return store.pause(args.batch, controller=args.controller)
        if command == "resume":
            return store.resume(args.batch, controller=args.controller)
        if command == "take-over":
            return store.take_over(
                args.batch, controller=args.controller, confirm=args.confirm
            )
        if command == "add-task":
            raise SoloAIError(
                "Legacy orchestration batches cannot add new tasks; drain or cancel the existing batch"
            )
        if command == "repair":
            raise SoloAIError(
                "Create repair work with the host's native task system, not the retired DWW orchestrator"
            )
        if command == "cancel":
            return store.cancel(
                args.batch,
                task_id=args.task,
                confirm=args.confirm,
                controller=args.controller,
            )
        raise SoloAIError(f"Unknown orchestration command: {command}")
    if args.command == "init":
        return initialize(
            repo,
            slots=args.slots,
            commands=_parse_commands(args.verify),
            verification_file=args.verification_file,
            accept=args.accept,
            accept_static_only=args.accept_static_only,
            decline=args.decline,
        )
    if args.command == "choose":
        return choose(
            repo,
            mode=args.mode,
            slots=args.slots,
            commands=_parse_commands(args.verify),
            verification_file=args.verification_file,
            session_id=args.session,
            delegation_code=args.delegate,
        )
    if args.command == "version":
        return _version()
    if args.command == "approve":
        request = _approval_request(repo, args)
        return approve(repo, **request)
    if args.command == "disable":
        return disable(repo)
    if args.command == "enable":
        return set_local_enabled(repo, enabled=True)
    if args.command == "settings":
        return (
            set_capacity(args.validation_capacity)
            if args.validation_capacity is not None
            else queue_status()
        )
    if args.command == "doctor":
        return _doctor(repo)
    if args.command == "route":
        return repository_route(repo, session_id=args.session)
    if args.command == "start":
        return start(
            repo,
            name=args.name,
            base=args.base,
            in_place=args.in_place,
            bind_branch=args.bind_branch,
            session_id=args.session,
            request_id=args.request_id,
            supersedes=args.supersedes,
            root_anchor_id=args.root_anchor,
            root_anchor_file=args.root_anchor_file,
            independent_reason=args.independent_reason,
            target=args.target,
            scope=args.scope,
            acceptance=args.acceptance,
            host_origin=_resolved_host_reference(args),
        )
    if args.command == "root-anchor":
        if args.root_anchor_command == "create":
            return create_root_task_anchor(
                repo,
                purpose=args.purpose,
                target=args.target,
                scope=args.scope,
                acceptance=args.acceptance,
                base=args.base,
                plan_input_path=args.plan_file,
                plan_source=args.plan_source,
                request_id=args.request_id,
                acceptance_index_input_path=args.acceptance_index_file,
                host_origin=_resolved_host_reference(args),
                include_content=args.content,
            )
        if args.root_anchor_command == "show":
            return show_root_task_anchor(
                repo,
                root_id=args.root,
                version=args.version,
                include_content=args.content,
            )
        if args.root_anchor_command == "bind-host":
            return bind_host_root_anchor(
                repo,
                root_id=args.root,
                root_anchor_file=args.root_anchor_file,
                host_origin=_resolved_host_reference(args),
            )
        if args.root_anchor_command == "context":
            return host_root_context(repo, host_origin=_resolved_host_reference(args))
        if args.root_anchor_command == "update":
            return update_root_task_anchor(
                repo,
                root_id=args.root,
                input_path=args.file,
                expected_sha256=args.expected_sha256,
                include_content=args.content,
            )
        if args.root_anchor_command == "amend":
            return amend_root_task_anchor(
                repo,
                root_id=args.root,
                plan_input_path=args.plan_file,
                change_input_path=args.change_file,
                source=args.source,
                summary=args.summary,
                expected_sha256=args.expected_sha256,
                target=args.target,
                scope=args.scope,
                acceptance=args.acceptance,
                acceptance_index_input_path=args.acceptance_index_file,
                include_content=args.content,
            )
        if args.root_anchor_command == "progress":
            return update_root_task_progress(
                repo,
                root_id=args.root,
                progress=args.progress,
                expected_sha256=args.expected_sha256,
                include_content=args.content,
            )
        if args.root_anchor_command == "accept":
            return record_root_task_acceptance(
                repo,
                root_id=args.root,
                status=args.status,
                evidence_input_path=args.evidence_file,
                evidence_json=args.evidence_json,
                expected_sha256=args.expected_sha256,
                include_content=args.content,
            )
        if args.root_anchor_command == "reindex":
            return reindex_root_task_acceptance(
                repo,
                root_id=args.root,
                index_input_path=args.index_file,
                expected_sha256=args.expected_sha256,
                include_content=args.content,
            )
        if args.root_anchor_command == "upgrade-objective":
            return upgrade_root_task_to_objective_protocol(
                repo,
                root_id=args.root,
                index_input_path=args.index_file,
                expected_sha256=args.expected_sha256,
                include_content=args.content,
            )
        if args.root_anchor_command == "close":
            return close_root_task_anchor(repo, root_id=args.root, confirm=args.confirm)
        if args.root_anchor_command == "list":
            return list_root_task_anchors(repo)
        raise SoloAIError(f"Unknown root anchor command: {args.root_anchor_command}")
    if args.command == "host-handoff":
        handoffs = HostHandoffStore(repo)
        if args.host_handoff_command == "status":
            return handoffs.status()
        if args.host_handoff_command == "batch":
            if args.host_handoff_batch_command == "take-over":
                return CandidateBatchStore(repo).assign_host_coordinator(
                    args.batch,
                    coordinator=_resolved_host_reference(args),
                    expected_revision=args.expected_revision,
                    reason=args.reason,
                )
            raise SoloAIError(
                f"Unknown host handoff batch command: {args.host_handoff_batch_command}"
            )
        if args.host_handoff_command == "repair":
            actor = _resolved_host_reference(args)
            if args.host_handoff_repair_command == "dispatch":
                return handoffs.dispatch(
                    request_id=args.request, sender=actor, retry=args.retry
                )
            if args.host_handoff_repair_command == "delivery":
                return handoffs.record_delivery(
                    request_id=args.request,
                    sender=actor,
                    outcome=args.outcome,
                    detail=args.detail,
                    attempt_id=args.attempt,
                )
            if args.host_handoff_repair_command == "attribute":
                return handoffs.attribute_validation_failure(
                    batch_id=args.batch,
                    candidate_id=args.candidate,
                    coordinator=actor,
                    evidence=args.evidence,
                )
            if args.host_handoff_repair_command == "claim":
                return handoffs.claim(request_id=args.request, actor=actor)
            if args.host_handoff_repair_command == "take-over":
                return handoffs.take_over(
                    request_id=args.request, actor=actor, reason=args.reason
                )
            if args.host_handoff_repair_command == "prepare":
                return handoffs.prepare(request_id=args.request, actor=actor)
            if args.host_handoff_repair_command == "result-dispatch":
                return handoffs.dispatch_repair_result(
                    request_id=args.request, sender=actor, retry=args.retry
                )
            if args.host_handoff_repair_command == "result-delivery":
                return handoffs.record_repair_result_delivery(
                    request_id=args.request,
                    sender=actor,
                    outcome=args.outcome,
                    detail=args.detail,
                    attempt_id=args.attempt,
                )
            raise SoloAIError(
                f"Unknown host handoff repair command: {args.host_handoff_repair_command}"
            )
        raise SoloAIError(f"Unknown host handoff command: {args.host_handoff_command}")
    if args.command == "candidate":
        if args.candidate_command == "status":
            store = CandidateBatchStore(repo)
            if args.compact:
                return store.status_view(
                    include_history=args.history,
                    candidate_id=args.candidate,
                    check=args.check,
                )
            summary = store.summary()
            return {
                **summary,
                "status_view": store.status_view_from_summary(
                    summary,
                    include_history=args.history,
                    candidate_id=args.candidate,
                    check=args.check,
                ),
            }
        if args.candidate_command == "repair":
            return prepare_candidate_repair(
                repo,
                candidate_id=args.candidate,
                host_origin=_resolved_host_reference(args),
            )
        if args.candidate_command == "withdraw":
            return withdraw_candidate(
                repo, candidate_id=args.candidate, reason=args.reason, source="cli"
            )
        if args.candidate_command == "restore-branch":
            return restore_candidate_branch(
                repo, candidate_id=args.candidate, apply=args.apply
            )
        raise SoloAIError(f"Unknown candidate command: {args.candidate_command}")
    if args.command == "batch":
        if args.batch_command == "status":
            state = StateStore(repo).read()
            if state["schema_version"] == STATE_SCHEMA:
                batches = state.get("batches", {})
                if args.batch:
                    if args.batch not in batches:
                        raise SoloAIError(f"Unknown native batch: {args.batch}")
                    return {"batches": [batches[args.batch]]}
                return {"batches": list(batches.values())}
            store = CandidateBatchStore(repo)
            if args.batch:
                return {"batches": [store.batch(args.batch)]}
            return store.summary()
        if args.batch_command == "seal":
            if args.task:
                if args.candidate or args.after_failed_batch:
                    raise SoloAIError(
                        "Native task batches cannot name legacy candidates"
                    )
                from .native_batches import run_native_batch, seal_native_batch

                frozen = seal_native_batch(
                    repo, task_ids=args.task, cause=args.cause, reason=args.reason
                )
                return run_native_batch(repo, batch_id=str(frozen["id"]))
            if not args.candidate:
                raise SoloAIError("Batch seal requires --task or --candidate")
            return seal_batch(
                repo,
                candidate_ids=args.candidate,
                after_failed_batch_id=args.after_failed_batch,
                coordinator=_resolved_host_reference(args),
                cause=args.cause,
                reason=args.reason,
                require_tail_reason=True,
            )
        if args.batch_command == "reconcile":
            if args.reason is not None and not args.force:
                raise SoloAIError("--reason is valid only with batch reconcile --force")
            state = StateStore(repo).read()
            if state["schema_version"] == STATE_SCHEMA:
                from .native_batches import reconcile_native_batches

                targets = {
                    str(task["base_ref"])
                    for task in state["tasks"].values()
                    if task.get("status") == "waiting-integration"
                }
                if args.base:
                    targets = {args.base} if args.base in targets else set()
                if len(targets) > 1:
                    raise SoloAIError(
                        "Native reconcile requires --base for multiple targets"
                    )
                if not targets:
                    return {"status": "no_waiting_native_tasks", "batch": None}
                result = reconcile_native_batches(
                    repo,
                    base_ref=next(iter(targets)),
                    cause=args.cause if args.force else None,
                    reason=args.reason if args.force else None,
                )
                return {
                    "status": "waiting" if result is None else result["status"],
                    "batch": result,
                }
            return reconcile_batches(
                repo,
                force=args.force,
                cause=args.cause,
                coordinator=_resolved_host_reference(args),
                reason=args.reason,
                require_tail_reason=True,
            )
        if args.batch_command == "recover":
            state = StateStore(repo).read()
            if state["schema_version"] == STATE_SCHEMA:
                from .native_batches import run_native_batch

                return run_native_batch(repo, batch_id=args.batch)
            return recover_batch(repo, batch_id=args.batch)
        if args.batch_command == "recovery-source":
            return verified_recovery_source(repo, commit=args.commit)
        if args.batch_command == "reopen":
            if (
                args.confirm != args.batch
                or args.confirm_no_runtime_started != args.batch
            ):
                raise SoloAIError(
                    "Batch reopen confirmation must exactly match --batch"
                )
            return reopen_prevalidation_batch(
                repo,
                batch_id=args.batch,
                runtime_not_started_confirmation=args.confirm_no_runtime_started,
            )
        if args.batch_command == "retire":
            return retire_failed_batch(repo, batch_id=args.batch, fast=args.fast)
        if args.batch_command == "metrics":
            return CandidateBatchStore(repo).metrics()
        raise SoloAIError(f"Unknown batch command: {args.batch_command}")
    if args.command == "runtime":
        if args.runtime_command == "verify":
            return verify_runtime_effective(repo, candidate_id=args.candidate)
        raise SoloAIError(f"Unknown runtime command: {args.runtime_command}")
    if args.command == "anchor":
        if args.anchor_command == "show":
            if args.root_content and not args.with_root:
                raise SoloAIError("--root-content requires --with-root")
            return show_task_anchor(
                repo,
                task_id=args.task,
                with_root=args.with_root,
                include_content=args.content,
                include_root_content=args.root_content,
            )
        if args.anchor_command == "update":
            return update_task_anchor(
                repo,
                task_id=args.task,
                lease=args.lease,
                input_path=args.file,
                expected_sha256=args.expected_sha256,
            )
        if args.anchor_command == "acknowledge-root":
            return acknowledge_root_plan(
                repo,
                task_id=args.task,
                lease=args.lease,
                root_version=args.root_version,
                root_sha256=args.root_sha256,
            )
        if args.anchor_command == "refresh-root":
            return refresh_root_context(
                repo,
                task_id=args.task,
                lease=args.lease,
            )
        if args.anchor_command == "bind-root":
            return bind_task_root_anchor(
                repo,
                task_id=args.task,
                lease=args.lease,
                root_id=args.root,
                root_anchor_file=args.root_anchor_file,
            )
        if args.anchor_command == "adopt":
            return adopt_task_anchor(
                repo,
                task_id=args.task,
                objective=args.objective,
                target=args.target,
                scope=args.scope,
                acceptance=args.acceptance,
                confirm=args.confirm,
            )
        raise SoloAIError(f"Unknown anchor command: {args.anchor_command}")
    if args.command == "commit":
        return commit_task(
            repo,
            task_id=args.task,
            lease=args.lease,
            message=args.message,
            paths=args.path,
            session_id=args.session,
        )
    if args.command == "ready":
        return ready(repo, task_id=args.task, lease=args.lease, session_id=args.session)
    if args.command == "finish":
        return finish(
            repo,
            task_id=args.task,
            lease=args.lease,
            session_id=args.session,
            host_actor=_resolved_host_reference(args),
            cause=args.cause,
            reason=args.reason,
        )
    if args.command == "retarget":
        return retarget(
            repo,
            task_id=args.task,
            lease=args.lease,
            base=args.base,
            confirm=args.confirm,
        )
    if args.command == "plan":
        if args.complete and args.level != "full":
            raise SoloAIError("plan --complete requires --level full")
        task = StateStore(repo).task(args.task)
        worktree = Path(str(task["worktree"]))
        verification = load_verification_config(repo, cwd=worktree)
        from .lifecycle import _verification_base

        verification_base = _verification_base(task)
        validation_base_ref = str(task["base_ref"])
        force_task_scope = task.get("mode") == "in-place"
        inputs, _ = proof_inputs(
            repo,
            cwd=worktree,
            base=verification_base,
            verification=verification,
            task_id=task["id"],
            levels=("ready",),
            force_task_scope=force_task_scope,
            validation_environment=frozen_validation_environment(
                repo,
                cwd=worktree,
                base=verification_base,
                validation_base_ref=validation_base_ref,
            ),
        )
        if args.level:
            phase_specs = [
                (
                    args.level,
                    ("ready", "full") if args.level == "full" else (args.level,),
                    "complete" if args.complete else "integration",
                )
            ]
        else:
            phase_specs = [
                ("development", ("development",), "integration"),
                ("ready", ("ready",), "integration"),
                ("full", ("ready", "full"), "integration"),
                ("stress", ("stress",), "integration"),
            ]
        phases: list[dict[str, Any]] = []
        profiles_by_id: dict[str, dict[str, Any]] = {}
        for phase_level, levels, full_scope in phase_specs:
            full_scopes = (
                ("integration", "complete")
                if phase_level == "full" and full_scope == "complete"
                else (("integration",) if phase_level == "full" else None)
            )
            _, phase_records = proof_inputs(
                repo,
                cwd=worktree,
                base=verification_base,
                verification=verification,
                task_id=task["id"],
                levels=levels,
                full_scopes=full_scopes,
                force_task_scope=force_task_scope,
                full_execution_id=(
                    "plan-only" if phase_level in {"full", "stress"} else None
                ),
                validation_environment=(
                    frozen_validation_environment(
                        repo,
                        cwd=worktree,
                        base=verification_base,
                        validation_base_ref=validation_base_ref,
                        full_scope=full_scope if phase_level == "full" else None,
                    )
                    if phase_level in {"ready", "full"}
                    else {}
                ),
            )
            decisions = [
                profile_execution_decision(
                    repo,
                    profile=profile,
                    inputs=profile_inputs,
                    fingerprint=fingerprint,
                )
                for profile, profile_inputs, fingerprint in phase_records
            ]
            executable = [
                (
                    profile.profile_id,
                    [command.fingerprint for command in profile.commands],
                )
                for (profile, _, _), decision in zip(
                    phase_records, decisions, strict=True
                )
                if decision["action"] == "execute"
            ]
            estimate = estimate_validation(executable)
            executable_estimates = iter(estimate["profile_seconds"])
            phase_profiles = []
            for (profile, profile_inputs, fingerprint), decision in zip(
                phase_records, decisions, strict=True
            ):
                profile_estimate = (
                    next(executable_estimates)
                    if decision["action"] == "execute"
                    else None
                )
                profile_view = {
                    "id": profile.profile_id,
                    "level": profile.level,
                    "resource_class": profile.resource_class,
                    "depends_on": list(profile.depends_on),
                    "continue_on_failure": profile.continue_on_failure,
                    "timeout_seconds": profile.timeout_seconds,
                    "commands": [command.redacted() for command in profile.commands],
                    "fingerprint": fingerprint,
                    "estimated_seconds": profile_estimate,
                    "selection": profile_selection_reason(profile, inputs["files"]),
                    "execution": decision,
                }
                phase_profiles.append(profile_view)
                profiles_by_id.setdefault(profile.profile_id, profile_view)
            phases.append(
                {
                    "level": phase_level,
                    "full_scope": full_scope if phase_level == "full" else None,
                    "profiles": phase_profiles,
                    "estimated_execution_seconds": (
                        None
                        if any(
                            decision["action"] == "blocked" for decision in decisions
                        )
                        else estimate["estimated_seconds"]
                    ),
                    "advisory": estimate["advisory"],
                    "blocked_profile_count": sum(
                        decision["action"] == "blocked" for decision in decisions
                    ),
                    "queue_wait_seconds": None,
                    "estimate_notice": "仅估计实际命令执行时间；队列等待取决于查询时的资源占用，未被猜测为固定时长。",
                }
            )
        selected = len(phases) == 1
        selected_phase = phases[0] if selected else None
        return {
            "task_id": task["id"],
            "base_ref": verification_base,
            "changed_files": inputs["files"],
            "unmapped_files": inputs["unmapped_files"],
            "profiles": list(profiles_by_id.values()),
            "phase_estimates": phases,
            "estimated_seconds": (
                selected_phase["estimated_execution_seconds"]
                if selected_phase
                else None
            ),
            "estimate_scope": (
                "selected_phase_execution_only"
                if selected
                else "overview_not_a_remaining_time_estimate"
            ),
            "advisory": selected_phase["advisory"] if selected_phase else None,
        }
    if args.command == "verify":
        if args.complete and args.level != "full":
            raise SoloAIError("--complete requires --level full")
        store = StateStore(repo)
        with store.operation(args.task, args.lease, "verify") as task:
            worktree = Path(str(task["worktree"]))
            from .lifecycle import (
                _assert_in_place_binding,
                _candidate_first,
                _is_in_place,
                _verification_base,
            )

            if _is_in_place(task):
                _assert_in_place_binding(repo, store, task, session_id=args.session)
            if not repo.is_clean(worktree):
                raise SoloAIError(
                    "Commit task changes before producing reusable verification evidence"
                )
            verification = load_verification_config(repo, cwd=worktree)
            verification_base = _verification_base(task)
            levels = ("ready", "full") if args.level == "full" else (args.level,)
            full_scope = "complete" if args.complete else "integration"
            full_scopes = (
                ("integration", "complete")
                if args.level == "full" and args.complete
                else (("integration",) if args.level == "full" else None)
            )
            approval_scope = (
                "complete" if args.level == "full" and args.complete else args.level
            )
            require_approved_plan(
                repo,
                cwd=worktree,
                verification=verification,
                message="This machine has not approved the commands required by this verification.",
                scope=approval_scope,
                profile_ids=selected_profile_ids(
                    repo,
                    cwd=worktree,
                    base=verification_base,
                    verification=verification,
                    levels=levels,
                    full_scopes=full_scopes,
                ),
                approval_target={"task": str(task["id"])},
            )
            attempt_id = new_validation_attempt_id(args.level)
            attempts = [
                str(item)
                for item in task.get("validation_attempts", [])
                if isinstance(item, str) and item
            ]
            attempts.append(attempt_id)
            store.update_task(
                task["id"],
                validation_attempt=attempt_id,
                validation_attempts=attempts,
            )
            proof = validate(
                repo,
                cwd=worktree,
                base=verification_base,
                verification=verification,
                task_id=task["id"],
                level=args.level,
                full_scope="complete" if args.complete else "integration",
                force_task_scope=_is_in_place(task),
                expected_base_head=(
                    str(task["base_head"]) if _candidate_first(task) else None
                ),
                validation_base_ref=str(task["base_ref"]),
                attempt_id=attempt_id,
                attempt_owner={"kind": "task", "id": str(task["id"])},
            )
            return {
                "task_id": task["id"],
                "level": args.level,
                "full_scope": "complete" if args.complete else "integration",
                "proof": proof["fingerprint"],
                "reused": proof.get("reused", False),
                "kind": proof["kind"],
                "validation_attempt": attempt_id,
            }
    if args.command == "status":
        has_selector = any((args.task, args.root, args.batch))
        if args.detailed and (args.compact or args.history or has_selector):
            raise ActionableSoloAIError(
                "--detailed cannot be combined with compact status selectors",
                code="INVALID_STATUS_QUERY",
                next_action={"kind": "choose_detailed_or_compact"},
            )
        if args.json and args.history and not args.compact and not has_selector:
            raise ActionableSoloAIError(
                "--json status --history requires --compact",
                code="INVALID_STATUS_QUERY",
                next_action={"kind": "add_compact"},
            )
        if not args.json or args.compact or args.history or has_selector:
            return query_status_view(
                repo,
                task_id=args.task,
                root_id=args.root,
                batch_id=args.batch,
                include_history=args.history,
            )
        return _status(repo, detailed=args.detailed)
    if args.command == "recover":
        if args.repair_path and not args.repair_runtime_adapter:
            raise SoloAIError("recover --path requires --repair-runtime-adapter")
        if args.repair_runtime_adapter and not args.repair_path:
            raise SoloAIError(
                "recover --repair-runtime-adapter requires at least one exact --path"
            )
        return recover(
            repo,
            task_id=args.task,
            repair_runtime_adapter_paths=(
                args.repair_path if args.repair_runtime_adapter else None
            ),
            host_actor=_resolved_host_reference(args),
        )
    if args.command == "handoff":
        return handoff(
            repo,
            task_id=args.task,
            confirm=args.confirm,
            host_origin=_resolved_host_reference(args),
        )
    if args.command == "resume-in-place":
        return resume_in_place(
            repo,
            task_id=args.task,
            session_id=args.session,
            confirm=args.confirm,
        )
    if args.command == "abandon":
        return abandon(
            repo,
            task_id=args.task,
            lease=args.lease,
            confirm=args.confirm,
            reason=args.reason,
            source="cli",
            session_id=args.session,
            retain_worktree=args.retain_worktree,
        )
    if args.command == "reclaim-retained":
        return reclaim_retained_worktree(
            repo, task_id=args.task, confirm=args.confirm, dispose=args.dispose
        )
    if args.command == "warm-slot":
        return warm_slot(repo, slot_id=args.slot)
    if args.command == "dev" and args.dev_command == "start":
        return dev_start(repo, task_id=args.task, lease=args.lease)
    if args.command == "dev" and args.dev_command == "stop":
        return dev_stop(repo, task_id=args.task, lease=args.lease)
    if args.command == "prune-proofs":
        return _prune(repo, kind="proofs")
    if args.command == "prune-logs":
        return _prune(repo, kind="logs")
    if args.command == "prune-slot":
        return _prune(
            repo,
            kind="slot",
            slot=args.slot,
            plan_id=args.plan,
            confirm=args.confirm,
        )
    if args.command == "deinit":
        return deinit(repo, confirm=args.confirm, message=args.message)
    raise SoloAIError("Unsupported command")


def _repair_task_human(task: dict[str, Any]) -> str:
    """只在创建任务的本地终端交付该任务自己的 lease。"""

    return "\n".join(
        (
            f"Task: {task['id']}",
            f"Worktree: {task['worktree']}",
            f"Branch: {task['branch']}",
            f"Anchor: {task['anchor_path']}",
            f"Lease: {task['lease']}",
            *(("Request reused: yes",) if task.get("request_reused") else ()),
        )
    )


def _human_candidate_delivery(candidate: dict[str, Any]) -> str:
    """将候选的可核验交付事实转换为面向人的结论。"""

    if (
        candidate.get("delivered") is True
        or candidate.get("delivery_status") == "integrated"
    ):
        return "Change integrated into the current local base."
    status = str(candidate.get("status") or "")
    if status in {"withdrawn", "superseded"}:
        return "Saved change will not be delivered locally."
    if status == "withdrawing":
        return "Saved change is being withdrawn; local delivery is not established."
    if status == "held":
        return (
            "Saving the change is still being finalized; local delivery is not "
            "established."
        )
    waiting = candidate.get("waiting")
    if not isinstance(waiting, dict):
        return (
            "Change saved; waiting for local integration. "
            "The specific wait reason is not currently verified."
        )
    state = waiting.get("state")
    if state == "waiting_for_prior_batch":
        return (
            "Change saved; waiting for the prior local integration batch "
            f"({waiting.get('prior_batch_id')}) to finish before this candidate's "
            "batch can be composed and validated."
        )
    if state == "waiting_for_compatible_candidates":
        compatible = int(waiting.get("compatible_pending_count") or 0)
        batch_size = int(waiting.get("batch_size") or 0)
        needed = int(waiting.get("additional_candidates_needed") or 0)
        return (
            "Change saved; waiting for local integration. "
            f"{compatible}/{batch_size} saved changes share its frozen base and "
            f"activation policy; {needed} more compatible change(s) are needed "
            "for automatic integration."
        )
    if state == "waiting_for_recorded_delivery_cause":
        return (
            "Change saved; waiting for a recorded local-integration cause. "
            "Task counts and unrelated work are not used as the reason."
        )
    if state == "integration_process_confirmed":
        return (
            "Change saved; a recorded local integration batch is running "
            f"for it ({waiting.get('batch_id')})."
        )
    if state == "integration_process_unconfirmed":
        return (
            "Change saved; a recorded local integration batch owns it "
            f"({waiting.get('batch_id')}), but its process is not confirmed."
        )
    return (
        "Change saved; waiting for local integration. "
        "The specific wait reason is not currently verified."
    )


def _human_validation_progress(validation: object) -> str | None:
    """只陈述状态视图已经确认的验证事实。"""

    if not isinstance(validation, dict):
        return None
    state = validation.get("state")
    if state == "running":
        profile = next(
            (
                item
                for item in validation.get("profiles") or []
                if isinstance(item, dict) and item.get("state") == "running"
            ),
            None,
        )
        command = profile.get("current_command") if isinstance(profile, dict) else None
        if isinstance(command, dict):
            return (
                "Related validation has a confirmed running command "
                f"({command.get('index')}/{command.get('count')})."
            )
        return "Related validation command progress is unknown."
    if state == "waiting":
        return "Related validation has a confirmed queue record and is waiting."
    if state == "unknown":
        return (
            "Related validation state is unknown; its queue or process is not "
            "confirmed."
        )
    if state == "passed":
        return "Related validation completed successfully."
    if state in {"failed", "timed_out", "interrupted"}:
        return f"Related validation ended as {state}."
    return None


def _human_task_status(task: dict[str, Any]) -> str:
    delivery = task.get("candidate_delivery")
    if isinstance(delivery, dict):
        summary = _human_candidate_delivery(delivery)
    else:
        summary = {
            "active": "Work can continue in its isolated worktree.",
            "starting": "The task is recorded as preparing its isolated worktree.",
            "ready": "Work is ready for its recorded next lifecycle step.",
            "candidate-published": (
                "Change has been saved; local delivery state is unknown."
            ),
            "completed": "Work completed in the recorded local transaction.",
            "abandoned": "Work was ended without local delivery.",
        }.get(str(task.get("status") or ""), "Task state is not currently verified.")
    validation = _human_validation_progress(task.get("validation"))
    return " ".join(item for item in (summary, validation) if item)


def _human(
    command: str, result: dict[str, Any], args: argparse.Namespace | None = None
) -> str:
    if (
        command == "host-handoff"
        and getattr(args, "host_handoff_command", None) == "repair"
        and getattr(args, "host_handoff_repair_command", None) == "prepare"
    ):
        repair = result.get("repair")
        if isinstance(repair, dict) and isinstance(repair.get("lease"), str):
            return _repair_task_human(repair)
    if (
        command == "candidate"
        and getattr(args, "candidate_command", None) == "repair"
        and isinstance(result.get("lease"), str)
    ):
        return _repair_task_human(result)
    if command == "status":
        return _status_view_human(result)
    if command == "candidate" and getattr(args, "candidate_command", None) == "status":
        view = result.get("status_view") or {}
        summary = view.get("status_summary") or {}
        lines = [
            "Local delivery: "
            f"{summary.get('active', 0)} saved change(s) not yet delivered, "
            f"{summary.get('history', 0)} historical record(s), "
            f"{summary.get('active_batches', 0)} recorded integration batch(es)."
        ]
        if view.get("view") == "active" and summary.get("history"):
            lines.append("Historical records are hidden; use --history to show them.")
        for candidate in view.get("candidates") or []:
            lines.append(
                f"- {candidate.get('candidate_id')}: "
                f"{_human_candidate_delivery(candidate)}"
            )
        integrity = view.get("integrity") or {}
        if integrity.get("status") == "not-checked":
            lines.append(
                "Integrity: not checked; use --check to inspect DWW candidate refs."
            )
        else:
            issues = integrity.get("issues") or []
            in_progress = integrity.get("in_progress") or []
            observations = integrity.get("observations") or []
            lines.append(
                "Integrity: checked; "
                f"{len(issues)} issue(s), {len(in_progress)} recovery item(s), "
                f"{len(observations)} historical observation(s)."
            )
            for item in [*issues, *in_progress][:10]:
                identity = item.get("candidate_id") or item.get("ref") or "unknown"
                lines.append(f"- {item.get('kind')}: {identity}")
        return "\n".join(lines)
    if command == "choose":
        if result.get("decision") == "deferred":
            return (
                "检测到仓库已有成熟工作流；develop-with-worktrees 已静默让路，"
                "未更改任何 DWW 状态。"
            )
        choice = result.get("choice")
        if choice == "isolated":
            return "已选择独立目录开发；之后的普通修改会自动隔离。"
        if choice == "current-repository":
            return "已记住：此仓库在本机以后直接在当前目录修改。"
        if result.get("delegated"):
            return "已加入本次当前目录修改授权；不要启动 DWW 生命周期。"
        return "\n".join(
            (
                "本次已切换为当前目录直接修改；不要启动 DWW 生命周期。",
                "仅在委托子智能体修改时传递此一次性委托码：",
                str(result["delegation_code"]),
            )
        )
    if command == "start":
        mode = result.get("mode", "isolated")
        summary = "\n".join(
            (
                f"Task: {result['id']}",
                f"Mode: {mode}",
                f"Worktree: {result['worktree']}",
                f"Branch: {result['branch']}",
                f"Anchor: {result['anchor_path']}",
                f"Lease: {result['lease']}",
                *(("Request reused: yes",) if result.get("request_reused") else ()),
            )
        )
        root = result.get("root_anchor")
        root_content = root.get("content") if isinstance(root, dict) else None
        if isinstance(root_content, str) and root_content:
            return f"{summary}\n\nRoot anchor (complete plan):\n{root_content.rstrip()}"
        return summary
    if command == "finish":
        if result.get("outcome") == "batch_integrated":
            summary = (
                "Changes integrated into the current local base: "
                f"{result.get('batch_trigger', 'full')} batch {result['batch_id']} "
                f"at {result['integrated_head']} from {result['candidate_count']} "
                "saved change(s)."
            )
            handoff = result.get("repair_handoff")
            if isinstance(handoff, dict) and handoff.get("id"):
                return (
                    f"{summary}\nRepair handoff {handoff['id']} is already delivered "
                    "with this batch; no return message is required."
                )
            return summary
        if result.get("outcome") == "candidate_published":
            handoff = result.get("repair_handoff")
            next_step = (
                "This coding round is complete. The actual batch freezer owns follow-through; "
                "a smaller tail requires an explicit round completion."
                if result.get("seal_policy") == "auto_full"
                and result.get("tail_policy") == "explicit"
                else "This coding round is complete. The actual batch coordinator follows "
                "the retained compatibility policy."
                if result.get("seal_policy") == "auto_full"
                else "This coding round is complete. This legacy policy requires an explicit "
                "exact candidate batch."
            )
            if isinstance(handoff, dict) and handoff.get("id"):
                next_step = (
                    f"Repair return request: {handoff['id']}. Send its result-dispatch payload, "
                    "then record the actual result-delivery; the coordinator owns integration."
                )
            return (
                "Change saved; waiting for local integration.\n"
                f"Source ID: {result['candidate_id']} at {result['candidate_head']}.\n"
                f"The base branch did not move. {next_step}"
            )
        label = (
            "static checks only; no test command ran"
            if result.get("proof_kind") == "static-only"
            else "validation commands passed"
        )
        verb = (
            "Completed in place" if result.get("mode") == "in-place" else "Integrated"
        )
        return f"{verb} {result['task_id']} at {result['integrated_head']} ({label})."
    batch = result.get("batch") if command == "batch" else None
    if command == "batch" and not isinstance(batch, dict):
        batch = result
    if command == "batch" and batch.get("status") == "completed":
        return (
            f"Changes integrated into the current local base by batch {batch['id']} "
            f"at {batch['integrated_head']} from {len(batch['candidate_ids'])} "
            "saved change(s) "
            f"({batch.get('trigger', 'explicit_tail')})."
        )
    if command in {"recover", "handoff", "resume-in-place"}:
        if result.get("status") == "completed":
            return (
                f"Task: {result['id']}\nStatus: completed\n"
                f"Transaction: {result['transaction_id']}\n"
                f"Candidate: {result['candidate_head']}"
            )
        if result.get("status") == "candidate-published":
            return (
                f"Task: {result.get('task_id') or result['id']}\n"
                "Status: change saved; waiting for local integration\n"
                f"Source ID: {result['candidate_id']} at {result['candidate_head']}"
            )
        if result.get("status") == "abandoned":
            return (
                f"Task: {result.get('id') or result.get('task_id')}\n"
                f"Status: abandoned\nTransaction: {result['transaction_id']}"
            )
        if result.get("status") in {"integrated", "withdrawn", "superseded"}:
            state_summary = {
                "integrated": "change integrated into the current local base",
                "withdrawn": "saved change withdrawn; local delivery did not occur",
                "superseded": "saved change replaced; local delivery did not occur",
            }[result["status"]]
            lines = (
                f"Task: {result.get('id') or result.get('task_id')}",
                f"Status: {state_summary}",
                *(
                    (f"Source ID: {result['candidate_id']}",)
                    if result.get("candidate_id")
                    else ()
                ),
                *((f"Batch: {result['batch_id']}",) if result.get("batch_id") else ()),
                *(
                    (f"Local delivery record: {result['delivery_status']}",)
                    if result.get("delivery_status")
                    else ()
                ),
            )
            return "\n".join(lines)
        lease = result.get("lease")
        if isinstance(lease, str) and lease:
            return "\n".join((f"Task: {result['id']}", f"Lease: {lease}"))
        return json.dumps(
            _redact_leases(result), ensure_ascii=False, indent=2, sort_keys=True
        )
    return json.dumps(
        _redact_leases(result), ensure_ascii=False, indent=2, sort_keys=True
    )


def _status_view_human(result: dict[str, Any]) -> str:
    """普通 status 默认只展示当前可行动对象，不复制完整历史 JSON。"""

    scope = result.get("scope")
    if scope in {"current", "history"}:
        lines = [
            "Current local work: "
            f"{len(result.get('tasks') or [])} task(s), "
            f"{len(result.get('candidates') or [])} saved change(s), "
            f"{len(result.get('batches') or [])} recorded integration batch(es)."
        ]
        for task in result.get("tasks") or []:
            lines.append(f"- Task {task.get('id')}: {_human_task_status(task)}")
        for candidate in result.get("candidates") or []:
            lines.append(
                f"- Saved change {candidate.get('id')}: "
                f"{_human_candidate_delivery(candidate)}"
            )
        for batch in result.get("batches") or []:
            validation = _human_validation_progress(batch.get("validation"))
            batch_summary = (
                "Local integration completed."
                if batch.get("status") == "completed"
                else "Local integration has a recorded active batch."
            )
            lines.append(
                f"- Local integration {batch.get('id')}: "
                + " ".join(item for item in (batch_summary, validation) if item)
            )
        counts = result.get("history_counts") or {}
        if scope == "current" and any(counts.values()):
            lines.append(
                "Historical records hidden: "
                f"{counts.get('tasks', 0)} task(s), "
                f"{counts.get('candidates', 0)} saved change(s), "
                f"{counts.get('batches', 0)} batch(es). Use status --history."
            )
        return "\n".join(lines)
    if scope == "task":
        task = result["task"]
        lines = [f"Task {task.get('id')}: {_human_task_status(task)}"]
        retained = task.get("retained_worktree")
        if isinstance(retained, dict):
            lines.extend(
                (
                    f"Retained worktree: {retained.get('path')}",
                    f"Quarantined slot: {retained.get('slot_id')}",
                    f"Reason: {retained.get('reason')}",
                )
            )
        return "\n".join(lines)
    if scope == "batch":
        batch = result["batch"]
        validation = _human_validation_progress(batch.get("validation"))
        return f"Local integration {batch.get('id')}: " + " ".join(
            item
            for item in (
                "Local integration completed."
                if batch.get("status") == "completed"
                else "Local integration has a recorded active batch.",
                validation,
            )
            if item
        )
    if scope == "root":
        root = result["root"]
        return (
            f"Root objective {root.get('id')}: "
            f"acceptance is {root.get('overall_acceptance_status')}."
        )
    return json.dumps(
        _redact_leases(result), ensure_ascii=False, indent=2, sort_keys=True
    )


def _configure_noninteractive_text_output() -> None:
    """让被宿主捕获的 CLI 文本稳定为 UTF-8，交互终端保持原样。"""

    for stream in (sys.stdout, sys.stderr):
        if stream.isatty():
            continue
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError, ValueError):
            # 嵌入式宿主可替换标准流；无法重配时沿用其既有契约。
            continue


def _redact_leases(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _redact_leases(item)
            for key, item in value.items()
            if key
            not in {
                "lease",
                "lease_owner",
                "session_fingerprint",
                "delegation_code",
                "controller",
            }
        }
    if isinstance(value, list):
        return [_redact_leases(item) for item in value]
    return value


def main(argv: list[str] | None = None) -> int:
    _configure_noninteractive_text_output()
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        result = _dispatch(args)
    except (SoloAIError, DelegatedContractError, OSError) as caught:
        translated = git_metadata_access_error(
            caught,
            repository=Path(args.repo),
            operation=str(args.command),
        )
        if isinstance(caught, OSError) and translated is None:
            raise
        exc = translated or caught
        if args.json:
            payload: dict[str, Any] = {"ok": False, "error": str(exc)}
            if isinstance(exc, ActionableSoloAIError):
                payload.update(
                    {
                        "error_code": exc.code,
                        "context": exc.context,
                        "next_action": exc.next_action,
                    }
                )
            print(json.dumps(payload, ensure_ascii=True))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(
            json.dumps(
                {"ok": True, "result": _redact_leases(result)},
                ensure_ascii=True,
                sort_keys=True,
            )
        )
    else:
        print(_human(args.command, result, args))
    return 0
