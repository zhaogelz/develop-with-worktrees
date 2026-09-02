# Lifecycle reference

Mode precedence is detected mature workflow, local long-term current-directory choice, exact current-task authorization, managed policy, then the first-modification choice. A mature workflow always wins. A command whose route admission observes that workflow performs zero DWW lifecycle, anchor, candidate, or integration-batch writes unless its tracked delegated contract has been explicitly approved locally.

Routing assigns lifecycle ownership. The host's native task/subagent system owns decomposition, dependencies, worker scheduling, waiting, and task status. DWW owns Git execution identity and integration safety for each routed managed task. The old `orchestrate` store is drain-only compatibility and is not created for new work.

Hook-provided route context is an optional shortcut. Without it, the skill runs one read-only `dww route --json`; this fallback is a normal supported path. `PreToolUse` is optional hardening. Hook, idle, and SessionEnd events may wake a reconciliation check, but they never prove completion, choose candidates, or perform a lifecycle transition by themselves.

## First-modification choice

The adapter asks one plain-language question only for the first modifying intent in an unchosen repository. `choose --mode isolated` adopts the normal lifecycle and accepts internal static-only checks when no test command is discovered. `choose --mode current-repository` stores a local preference without touching tracked files. The session-bound `current-task` compatibility choice needs a trusted session identifier; when Hook context is unavailable, do not simulate it by weakening managed isolation.

Before applying a choice, `choose` routes again. If a mature workflow exists, it returns `deferred` and writes no policy, preference, task, anchor, candidate, or slot state.

## Isolated task (default)

`start` selects the least-recently-used idle slot and derives a task branch from the invocation worktree's current local branch. It records the branch, base commit, base worktree, slot generation, optional caller `request_id`, and optional repair `supersedes` candidate. A repeated non-empty request id bound to the same purpose and base returns the original task instead of consuming another slot.

Before Start returns, it creates `<git-common-dir>/solo-ai/task-anchors/<task-id>.md`. The anchor is a regular UTF-8 local file capped at 64 KiB. Ready requires its exact task identity. It is local state, never a tracked workspace file, and stays available from every worktree.

When `runtime_adapter.activate` is configured, isolated Start keeps both task and slot in `starting` after the exact branch, worktree, directory identity, candidate head, and anchor exist. DWW then invokes the approved project command with an immutable context containing those facts and the slot's deterministic 100-port block. Only a successful receipt and a second clean identity check atomically make the task and slot `active`. A command failure remains retryable by the same Start request or `recover`; a proven successful receipt is reused after interruption. Any tracked, ordinary-untracked, protected, or unknown-ignored contamination quarantines and preserves the worktree. Commit and Ready cannot begin while activation is incomplete.

A genuine pre-anchor task fails Ready until its execution contract is reviewed. `anchor adopt --task ... --objective ... --target ... --scope ... --acceptance ... --confirm <task-id>` reconstructs only that explicit local anchor. It refuses terminal tasks, blank fields, links, mismatched confirmation, and automatic inference from chat or stale plans.

`commit` requires an exact complete path manifest. `ready` safely synchronizes a forward base, validates the Ready closure, and records proof. Ready profiles contain only syntax/static checks, affected compilation, and light contracts; heavy profiles are valid only at Full. Each profile rechecks the expected base and candidate around validation admission and execution. Proof reuse requires the same candidate tree, normalized command, tool/platform facts, declared environment hashes, and tracked input closure. Identity drift fails closed. An unchanged deterministic failure is not blindly rerun when the profile declares complete inputs and no external state. If another integration advances the base, Ready resynchronizes and may reuse exact unchanged evidence. Five retries bound convergence; continued movement preserves the task. When the read-only merge prediction finds a real conflict, the task may explicitly merge the current recorded base in its own worktree and review the resolution. Exact-path Commit accepts that merge only while `MERGE_HEAD` still equals the current forward base head; arbitrary or stale merge identities fail closed. The separately recorded candidate-repair merge follows the same exact-path gate.

## Legacy direct integration

Direct is retained for an explicitly configured or pre-0.5 repository. `finish` freezes task, slot, worktree, branch, base, candidate, and proof identity in a persisted transaction, then fast-forwards the recorded clean base. The transaction moves `prepared → promoted → completed`. Completion keeps the slot unallocatable until exact cleanup and released-directory checks succeed. The completed receipt is a rebuildable projection, not the transaction log.

`recover` classifies an interruption from persisted identity and Git ancestry. An already promoted candidate proceeds only through exact cleanup. A forward base that no longer matches returns the unpromoted task for a fresh Ready; rewritten bases, dirty worktrees, moved refs, unknown ignored content, or ambiguous Git facts fail closed. Successful direct completion deletes the task anchor.

## Candidate-first integration

New repositories use `integration.mode = "batched"`, `seal_policy = "auto_full"`, and `tail_policy = "quiet_or_explicit"`. Finish still runs task-level safety and Ready validation, but instead of moving the base it:

1. freezes an immutable `refs/dww/candidates/<candidate-id>` ref and proof identity in `held` state;
2. runs the optional project runtime Adapter `release` command against an immutable JSON context;
3. verifies that the task worktree is still clean and uncontaminated;
4. detaches and deletes only the task branch, releases the slot, and activates the candidate as `pending`; and
5. keeps the task anchor until the candidate reaches a real terminal outcome.

Pool capacity defaults to 10 and counts held, pending, or sealed candidates. An Adapter failure leaves the durable candidate held and the task recoverable; it cannot enter a batch and the base stays unchanged. Full capacity makes Finish fail closed while preserving the ready/publishing task and its lease. A retained candidate from a deterministic failed generation remains available for exact repair or reuse but no longer consumes active capacity.

Start, candidate activation, Abandon, and tail reconciliation share the candidate-admission lock. Once one policy epoch and local base lane contains the configured five eligible candidates, DWW freezes the oldest five in publication order. The fifth candidate's Finish releases its task worktree before long integration, then obtains the single persisted integration turn. Only one generation for a base may execute at once; later work may accumulate without making Finish wait for an existing long batch. No resident DWW controller is required: an interrupted generation is resumed from recorded phases and Git facts.

`batch reconcile` freezes a smaller tail only from the exact pending candidates in one base-and-policy lane. Under `quiet_or_explicit`, the lane must have zero persisted modifying producers continuously for `tail_quiet_seconds`; a Start during that stability period cancels the opportunity. Activity affects timing only and can never add worktree contents or choose candidate identity. `reconcile` returns `next_reconcile_at` after the lane first becomes quiet, and the host's native heartbeat calls it at that time. A host without reliable scheduling cannot claim automatic quiet-tail support.

There is deliberately no maximum candidate age or longest-wait auto-seal. A producer remains part of the timing decision until it reaches a recorded terminal state. An explicit user, deployment, or downstream dependency request may call `batch reconcile --force --cause user|deploy|dependency`; it still freezes only the current exact pending snapshot. `batch seal --candidate ...` remains the exact-list compatibility and recovery interface. Seal intent excludes wake-up cause, so retries with the same ordered candidates, base, and policy epoch return the same generation instead of duplicating it.

Both full and tail batches apply each candidate's exact tree difference to a dedicated detached integration worktree and create deterministic local integration commits. When configured, the paired project Runtime Adapter `batch_activate` establishes project-owned validation resources from the exact frozen batch identity, positive persisted `runtime_cycle`, and dedicated port block before combined Full starts. DWW then runs Ready plus Full validation over the combined tree, records `passed`, `failed`, or `interrupted`, and invokes `batch_release` with the same cycle before promotion, failure finalization, or retry. A failed or uncertain activation/release receipt leaves the generation active, blocks `main`, and is retried by `batch recover`; successful receipts are reused only for identical command, input, context, and cycle identity. Once release succeeds, any later validation retry starts a new cycle and invokes activation again. DWW finally rechecks the composed worktree, base snapshot, and clean checked-out base worktree before fast-forwarding. An exact Ready proof may be reused in Full only while every proof input remains identical; databases, complete builds, authentication, and browser flows belong to Full and run once for the frozen composition.

Only an exact complete-batch count under `auto_full`, a proven stable quiet lane, or an authorized explicit cause may create a seal. UI task counts, raw worktree enumeration, SessionEnd, and Hook delivery can only wake the persisted-state check. A later revision cannot mutate a captured generation. `start --supersedes` publishes a repair candidate for a future generation.

Composition or final-validation failure releases any configured batch runtime, records the generation as failed, and preserves the base. Its candidates become retained rather than automatically pending again, so another publication cannot blindly recreate the same failed batch. A composition failure records the exact conflicting candidate and makes only that unsealed retained candidate eligible for `candidate repair --candidate <id>`. The command creates an idempotent managed task on the latest base, writes a complete repair anchor, and prepares the immutable candidate ref with `merge --no-commit --no-ff`; clean and conflicted preparations both remain inside the repair worktree. Exact-path Commit may complete only this recorded repair merge. Publishing the verified repair supersedes the old candidate. Other unchanged compatible retained candidates may be named again by exact identity in a reviewed new generation.

Automatic repair preparation is bounded to two generations in one supersession chain. The host resolves a prepared conflict without user interruption only when executable facts and durable contracts determine one answer. Product, permission, migration, deletion, security, or mutually valid test choices require human input. Final-validation and promotion failures are not eligible for automatic merge repair and retain their evidence for diagnosis. `batch recover` is reserved for an interrupted nonfailed transaction and resumes from the recorded phase and Git facts. After promotion it completes worktree/ref cleanup idempotently. Successful completion deletes every included task anchor. `candidate withdraw` deletes only an unsealed pending or retained candidate and its anchor.

Candidate publication is not delivery. A candidate reports delivered only after its completed batch is contained in the current clean base. When a user explicitly asks whether that source is effective in a running environment, `runtime verify --candidate <id>` invokes the project Adapter only after delivery and records fresh runtime evidence. DWW does not interpret ports, databases, browsers, authentication, deployment, or runtime-version semantics.

## Abandonment

`abandon` uses a persisted transaction under the integration and maintenance locks. It never discards tracked changes or uses blanket `git clean`. Ordinary untracked files are removed only through unchanged-object checks; protected, unknown, replaced, or late content blocks release. Task-ref deletion verifies every other active task ref. Successful abandonment deletes the task anchor.

A published candidate is no longer an active leased task and is not abandoned through task cleanup. Use explicit candidate withdrawal while it is pending.

## In-place compatibility

`start --in-place --session` is explicit compatibility, not the ordinary current-task bypass. It requires one clean attached current worktree and trusted session identity, creates no slot or branch, and binds:

```text
base_worktree + branch + start_head + expected_head + session fingerprint + lease
```

Commit, verify, Ready, Finish, and abandon recheck the same binding. In-place Finish writes a receipt and releases the task only; it does not merge, detach, reset, clean, or delete a branch. A mismatch quarantines and preserves files. `resume-in-place` transfers only an unchanged recorded identity with exact confirmation. In-place tasks also receive the standard DWW task anchor and remove it only on successful Finish or explicit clean abandonment.

## Local-only boundary

DWW Start, Ready, Finish, candidate publication, seal, recovery, and cleanup never fetch, pull, push, create a PR, deploy, rebase, squash, amend, or rewrite history. An explicit user-requested remote sync is a separate dry-run-first ordinary non-force push from the clean integrated base worktree.
