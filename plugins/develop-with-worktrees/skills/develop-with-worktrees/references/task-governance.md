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

## Confirmed-objective root anchors

If the user explicitly confirms a complete implementation plan, asks to set that plan as the objective, or asks to proceed with it, the host creates exactly one root anchor before the first related child `Start`. It may do this even if the eventual work has only one child: root-anchor eligibility comes from the need to retain a confirmed objective, not from the eventual number of tasks. It belongs at the originating repository’s `<git-common-dir>/solo-ai/root-anchors/<root-id>.md`. The host supplies the complete final plan through a UTF-8 `--plan-file` below the repository or an explicit absolute external plain file, a short `--plan-source`, and its stable `--request-id`; a retry with the same request ID and plan is idempotent, while an attempt to reuse it for a changed plan fails closed.

The structured root stores the complete confirmed plan—not a lossy summary—plus immutable purpose and baseline, current target/scope/acceptance, a monotonically increasing plan version, a source-tagged record of explicit user amendments, current progress, and the overall acceptance result. The plan body may contain its own Markdown headings, lists, and code; DWW delimits it with reserved internal markers so it can be read back in full. The source input is not a second canonical document after creation. Do not create a root for discussion, read-only investigation, or an ordinary bounded change without a separately confirmed plan. The host, not DWW, decides whether the user confirmed it.

Only an explicit user change may replace the effective plan with `root-anchor amend --plan-file ... --source ... --summary ... --expected-sha256 ...`, or append its UTF-8 words verbatim with the mutually exclusive `--change-file ...`. Plan and change inputs follow the same explicit external plain-file rule as creation; acceptance evidence remains under the managed repository. Both operations increment the version, preserve the prior root version and amendment record, and reset the overall result to pending. The generic structured `root-anchor update` uses the same write path: a full-plan, target, scope, or acceptance-criteria change needs the next version and user-change record, resets acceptance, and cannot directly write `accepted` or `cancelled`. `root-anchor progress` changes only execution status. Technical implementation choices that do not alter purpose, scope, or acceptance stay local to the child task; neither the host nor DWW may silently rewrite the root plan. Existing legacy roots remain readable under their former rules; no agent may invent an unrecorded historic plan for them.

A related child in the origin repository uses `start --root-anchor <root-id>`. A related child in another repository uses `start --root-anchor <root-id> --root-anchor-file <absolute-root-anchor-path>` and still keeps its normal unique task anchor, worktree, candidate, and lifecycle. Candidate repair inherits the source task's root binding. DWW verifies that an external path is the exact non-linked root file for that ID, persists the fixed locator in the child state, and writes one exact child-state locator into the root’s machine-managed registry. It never discovers repositories globally or creates a second root copy. A missing, changed, linked, or identity-mismatched root reference fails closed before further lifecycle work.

Root anchors never define candidate membership, candidate groups, `scope_id`, task dependencies, worker scheduling, batch sealing, or cross-worktree atomicity. The host remains responsible for native orchestration. On continuation, handoff, model/context recovery, first work after binding, a root-version change, or candidate repair, it automatically reads `anchor show --task <task-id> --with-root --content --root-content` and records the current version/digest with `anchor acknowledge-root`; it does not ask the user and records no duplicate within a continuous same-version task. The record is not proof that an AI understood the full text. Only Commit, actual Ready, and Finish candidate publication require the recorded version to equal the current structured root version; a stale record gives an automatic read/acknowledge/retry recovery path without discarding work. `root-anchor close` rejects linked local or registered external nonterminal children and fails closed if a registered child state is unavailable or ambiguous. For every `candidate-published` child, it follows the exact persisted supersession lineage and permits closure only after the terminal candidate is integrated or explicitly withdrawn; publication alone is never delivery. A structured root also requires `root-anchor accept` to record an `accepted` or `cancelled` overall result with evidence for its current plan version before closing.

Use `anchor show --task <task-id> --content` to read the current UTF-8 content; the call without `--content` returns its compact identity, byte SHA-256, and origin-verification status. Save a reviewed revision through `anchor update --task <task-id> --lease <lease> --file <input-file> --expected-sha256 <sha256>`, invoked from the recorded task or base worktree with an input file under that same worktree. The command derives the destination from the task ID, checks the lease, state, physical worktree identity, and original purpose/baseline facts, then compares the byte digest under the maintenance lock and atomically replaces the anchor. If requested content is already current it succeeds as a no-op even when its otherwise-valid digest is old; otherwise a stale digest is rejected so one editor cannot overwrite another. A ready task may change only the complete `Current progress` block. Legacy anchors show as origin-unverified and must pass explicit `anchor adopt` before an update; anchors remain uncommitted and the input file is not itself an anchor.

`candidate repair` fills the managed child anchor before preparing the source merge and retains its source root binding. It records the immutable source candidate, latest repair base, bounded attempt, scope boundary, acceptance path, and the rule that semantic product or safety choices must be escalated rather than guessed.

Other routes must not create this DWW common-dir state. Use the repository-declared location, or a private repository-external temporary file if no safe ignored workspace location exists. Never stage or commit an anchor.

## Minimum content

The child anchor records only the current execution slice. A structured root additionally carries the full confirmed objective and its user-visible revisions. Record only the information needed for those roles:

- original user objective;
- implementation objects or repositories;
- reference baseline or candidate identity;
- scope boundaries and explicitly excluded work;
- acceptance criteria and validation route;
- current progress, decisions, and unresolved blockers.

Do not copy chat transcripts, hidden reasoning, credentials, leases, or unrelated project history. The anchor is a recovery aid, not a project diary.

## Precedence and recovery

Direct user instructions and repository hard rules remain authoritative. Within those boundaries, a structured root is the source for the confirmed objective, scope, and acceptance criteria; its bound child anchor is the source for the current execution slice. Executable facts in code, tests, schema, and configuration remain authoritative for implemented behavior; stale proposals and archived material do not override either.

After context compression, a model change, handoff, repair, later continuation, binding, or root-plan change, automatically re-read the child anchor and any bound root before the next modifying action, then acknowledge the exact structured version/digest once. A later user amendment makes the child require this automatic recovery before its next Commit, actual Ready, or candidate-publishing Finish. Update only progress while the task remains active. Continue without interruption when the current request, hard rules, and executable facts determine one compatible result. Stop only when they conflict or leave materially different product, permission, migration, deletion, security, or validation outcomes open; do not silently rewrite the anchor.

## Durable-document boundary

At acceptance, first decide whether the work changed a fact that future tasks must continue to obey. Durable documentation is warranted only for a lasting product rule, public interface or contract, data model, permission boundary, architecture boundary, stable project responsibility, or long-lived UI contract.

Bug fixes that restore an existing contract, implementation details, tests, builds, validation evidence, debugging steps, and ordinary engineering adjustments stay in code, tests, configuration, receipts, or the existing engineering reference. Do not create a generic `CONTEXT.md`, `requirements.md`, `plan.md`, task ledger, or ADR directory unless the repository explicitly designates it as the canonical home.

When a durable fact changed, update only the repository's existing canonical document for that topic. Do not duplicate the same fact across a root plan, feature plan, README, and task log.

For legacy direct integration, DWW removes the anchor after successful Finish. In candidate-first mode it remains after publication and is removed only when the candidate's automatic full batch, explicit exact tail, or a deliberately retained legacy `quiet_or_explicit` tail completes, the unsealed candidate is withdrawn, or the task is abandoned. Publication alone is not delivery. Failed or interrupted Adapter release and integration keep the anchor for recovery. A pre-anchor legacy task must use reviewed `anchor adopt` fields; neither DWW nor the host may invent its old execution contract.
