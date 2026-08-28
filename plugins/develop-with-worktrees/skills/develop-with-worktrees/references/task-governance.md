# Task context and durable documentation

Use a short-lived task anchor to preserve the active implementation contract without turning every task into a permanent plan document.

These are non-state governance rules. Routing decides which lifecycle or orchestrator owns task state; it does not disable plain-language plan confirmation, single-conversation coordination, temporary task context, or the durable-document boundary. Under `defer`, create no DWW task, lifecycle, or orchestration state. Apply these rules only after the routed owner has authorized the exact writable workspace: DWW `start` for `managed`, the repository workflow for `defer` or `delegated`, and the recorded user choice for `disabled` or `current-task`. Use an explicit repository rule instead when it covers the same concern.

## When to create an anchor

Create one immediately after entering the authorized writable worktree when any of these is true:

- the user confirmed a plan and asked to start or continue it;
- the task has multiple implementation steps, repositories, modules, or acceptance checks;
- completion is likely to span context compression, a model change, or a later continuation.

A clearly bounded single small edit may omit it. Read-only analysis never creates one.

Use the repository-declared temporary anchor location when present. Otherwise prefer `.tmp/task-anchors/<task-id-or-purpose>.md` after confirming that Git ignores it and the routed workflow authorized that workspace. A `managed` task may fall back to `<git-common-dir>/solo-ai/task-anchors/<task-id>.md`. Other routes must not create that DWW common-dir fallback; if no safe workspace location exists, use a private repository-external temporary file for the current task. Never stage or commit an anchor, and do not modify `.gitignore` merely to store one.

## Minimum content

Record only the current execution contract:

- original user objective;
- implementation objects or repositories;
- reference baseline or candidate identity;
- scope boundaries and explicitly excluded work;
- acceptance criteria and validation route;
- current progress, decisions, and unresolved blockers.

Do not copy chat transcripts, hidden reasoning, credentials, leases, or unrelated project history. The anchor is a recovery aid, not a project diary.

## Precedence and recovery

Direct user instructions and repository hard rules remain authoritative. Within those boundaries, the current task anchor is the source for the active goal, scope, and acceptance criteria. Executable facts in code, tests, schema, and configuration remain authoritative for implemented behavior; stale proposals and archived material do not override either.

After context compression, a model change, handoff, or later continuation, re-read the anchor before the next modifying action. Update its progress while the task remains active. If it conflicts with a current hard rule or the user's latest direction, stop and resolve that conflict rather than silently rewriting the anchor.

## Durable-document boundary

At acceptance, first decide whether the work changed a fact that future tasks must continue to obey. Durable documentation is warranted only for a lasting product rule, public interface or contract, data model, permission boundary, architecture boundary, stable project responsibility, or long-lived UI contract.

Bug fixes that restore an existing contract, implementation details, tests, builds, validation evidence, debugging steps, and ordinary engineering adjustments stay in code, tests, configuration, receipts, or the existing engineering reference. Do not create a generic `CONTEXT.md`, `requirements.md`, `plan.md`, task ledger, or ADR directory unless the repository explicitly designates it as the canonical home.

When a durable fact changed, update only the repository's existing canonical document for that topic. Do not duplicate the same fact across a root plan, feature plan, README, and task log. Once validation and any required durable update are complete, remove the task anchor.
