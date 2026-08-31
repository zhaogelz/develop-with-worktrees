# Task context and durable documentation

Use a short-lived task anchor to preserve the active implementation contract without turning every task into a permanent plan document.

Routing decides which lifecycle owns task state. DWW-managed task context is now part of that lifecycle; task decomposition and worker scheduling remain owned by the host's native task/subagent system. Under `defer`, create no DWW task, anchor, candidate, or batch state. Apply the repository's own context rule after its lifecycle authorizes the writable workspace.

## When to create an anchor

Managed `Start` always creates one before returning the writable worktree. For a deferred or disabled workflow, follow its own rule and create a temporary anchor when any of these is true:

- the user confirmed a plan and asked to start or continue it;
- the task has multiple implementation steps, repositories, modules, or acceptance checks;
- completion is likely to span context compression, a model change, or a later continuation.

A clearly bounded single small edit may omit it only outside the managed lifecycle. Read-only analysis never creates one.

A managed task always uses `<git-common-dir>/solo-ai/task-anchors/<task-id>.md`. This makes the anchor available from the base checkout, task worktree, and recovery commands without placing it in the repository or requiring `.gitignore` changes. `Start` returns the exact path, Ready validates its regular-file, size, UTF-8, and task-id identity, and `status` lists it.

Other routes must not create this DWW common-dir state. Use the repository-declared location, or a private repository-external temporary file if no safe ignored workspace location exists. Never stage or commit an anchor.

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

When a durable fact changed, update only the repository's existing canonical document for that topic. Do not duplicate the same fact across a root plan, feature plan, README, and task log.

For direct integration, DWW removes the anchor after successful Finish. In batched mode it remains after candidate publication and is removed only when that candidate's explicit batch completes, the pending candidate is withdrawn, or the task is abandoned. Failed or interrupted integration keeps the anchor for recovery.
