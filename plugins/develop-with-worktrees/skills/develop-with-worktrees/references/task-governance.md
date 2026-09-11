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

## Cross-phase coordinator anchors

When the user has confirmed one implementation objective that spans several independently managed writing tasks, the host coordinator may create exactly one local root anchor before the first child `Start`. It belongs at `<git-common-dir>/solo-ai/root-anchors/<root-id>.md` and records the original purpose, implementation target, immutable base reference, scope boundary, acceptance criteria, and current progress. It is not a second task database or a permanent plan document.

The host, not DWW, decides whether a request is confirmed and cross-phase. Do not create a root anchor for discussion, read-only investigation, or an ordinary bounded one-candidate change. A related child uses `start --root-anchor <root-id>` and keeps its normal unique task anchor, worktree, candidate, and lifecycle. The task state and task-anchor text bind the same root ID; a missing or changed root reference fails closed before further lifecycle work.

Root anchors never define candidate membership, candidate groups, `scope_id`, task dependencies, worker scheduling, batch sealing, or cross-worktree atomicity. The host remains responsible for native orchestration. On continuation it must read the root anchor and the active child anchor before modifying files. `root-anchor close` rejects linked nonterminal children. The coordinator records the final acceptance outcome before closing and independently verifies required candidate delivery or explicit withdrawal; a candidate publication alone is never delivery.

Use `anchor show --task <task-id>` to read the current UTF-8 content, byte SHA-256, and origin-verification status. Save a reviewed revision through `anchor update --task <task-id> --lease <lease> --file <input-file> --expected-sha256 <sha256>`, invoked from the recorded task or base worktree with an input file under that same worktree. The command derives the destination from the task ID, checks the lease, state, physical worktree identity, and original purpose/baseline facts, then compares the byte digest under the maintenance lock and atomically replaces the anchor. If requested content is already current it succeeds as a no-op even when its otherwise-valid digest is old; otherwise a stale digest is rejected so one editor cannot overwrite another. A ready task may change only the complete `Current progress` block. Legacy anchors show as origin-unverified and must pass explicit `anchor adopt` before an update; anchors remain uncommitted and the input file is not itself an anchor.

`candidate repair` fills the managed anchor before preparing the source merge. It records the immutable source candidate, latest repair base, bounded attempt, scope boundary, acceptance path, and the rule that semantic product or safety choices must be escalated rather than guessed.

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

For legacy direct integration, DWW removes the anchor after successful Finish. In candidate-first mode it remains after publication and is removed only when the candidate's automatic full batch, proven quiet tail, or explicit exact tail completes, the unsealed candidate is withdrawn, or the task is abandoned. Publication alone is not delivery. Failed or interrupted Adapter release and integration keep the anchor for recovery. A pre-anchor legacy task must use reviewed `anchor adopt` fields; neither DWW nor the host may invent its old execution contract.
