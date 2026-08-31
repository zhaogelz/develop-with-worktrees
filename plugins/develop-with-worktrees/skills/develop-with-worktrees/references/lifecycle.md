# Lifecycle reference

Mode precedence is detected mature workflow, local long-term current-directory choice, exact current-task authorization, managed policy, then the first-modification choice. A mature workflow always wins. A command whose route admission observes that workflow performs zero DWW lifecycle, anchor, candidate, or integration-batch writes unless its tracked delegated contract has been explicitly approved locally.

Routing assigns lifecycle ownership. The host's native task/subagent system owns decomposition, dependencies, worker scheduling, waiting, and task status. DWW owns Git execution identity and integration safety for each routed managed task. The old `orchestrate` store is drain-only compatibility and is not created for new work.

Hook-provided route context is an optional shortcut. Without it, the skill runs one read-only `dww route --json`; this fallback is a normal supported path. `PreToolUse` is optional hardening. No lifecycle transition depends on SessionEnd, Hook delivery, idle time, or a resident process.

## First-modification choice

The adapter asks one plain-language question only for the first modifying intent in an unchosen repository. `choose --mode isolated` adopts the normal lifecycle and accepts internal static-only checks when no test command is discovered. `choose --mode current-repository` stores a local preference without touching tracked files. The session-bound `current-task` compatibility choice needs a trusted session identifier; when Hook context is unavailable, do not simulate it by weakening managed isolation.

Before applying a choice, `choose` routes again. If a mature workflow exists, it returns `deferred` and writes no policy, preference, task, anchor, candidate, or slot state.

## Isolated task (default)

`start` selects the least-recently-used idle slot and derives a task branch from the invocation worktree's current local branch. It records the branch, base commit, base worktree, slot generation, optional caller `request_id`, and optional repair `supersedes` candidate. A repeated non-empty request id bound to the same purpose and base returns the original task instead of consuming another slot.

Before Start returns, it creates `<git-common-dir>/solo-ai/task-anchors/<task-id>.md`. The anchor is a regular UTF-8 local file capped at 64 KiB. Ready requires its exact task identity. It is local state, never a tracked workspace file, and stays available from every worktree.

A genuine pre-anchor task fails Ready until its execution contract is reviewed. `anchor adopt --task ... --objective ... --target ... --scope ... --acceptance ... --confirm <task-id>` reconstructs only that explicit local anchor. It refuses terminal tasks, blank fields, links, mismatched confirmation, and automatic inference from chat or stale plans.

`commit` requires an exact complete path manifest. `ready` safely synchronizes a forward base, validates the Ready closure, and records proof. Each profile rechecks the expected base and candidate around validation admission and execution. If another integration advances the base, Ready resynchronizes and may reuse exact unchanged evidence. Five retries bound convergence; continued movement preserves the task. When the read-only merge prediction finds a real conflict, the task may explicitly merge the current recorded base in its own worktree and review the resolution. Exact-path Commit accepts that merge only while `MERGE_HEAD` still equals the current forward base head; arbitrary or stale merge identities fail closed. The separately recorded candidate-repair merge follows the same exact-path gate.

## Legacy direct integration

Direct is retained for an explicitly configured or pre-0.5 repository. `finish` freezes task, slot, worktree, branch, base, candidate, and proof identity in a persisted transaction, then fast-forwards the recorded clean base. The transaction moves `prepared → promoted → completed`. Completion keeps the slot unallocatable until exact cleanup and released-directory checks succeed. The completed receipt is a rebuildable projection, not the transaction log.

`recover` classifies an interruption from persisted identity and Git ancestry. An already promoted candidate proceeds only through exact cleanup. A forward base that no longer matches returns the unpromoted task for a fresh Ready; rewritten bases, dirty worktrees, moved refs, unknown ignored content, or ambiguous Git facts fail closed. Successful direct completion deletes the task anchor.

## Candidate-first integration

New repositories use `integration.mode = "batched"` with `seal_policy = "auto_full"`. Finish still runs task-level safety and Ready validation, but instead of moving the base it:

1. freezes an immutable `refs/dww/candidates/<candidate-id>` ref and proof identity;
2. records it in the bounded Git-common-dir candidate pool;
3. detaches and deletes only the task branch;
4. releases the clean slot; and
5. keeps the task anchor until the candidate reaches a real terminal outcome.

Pool capacity defaults to 10 and counts pending or sealed candidates. Full capacity makes Finish fail closed while preserving the ready/publishing task and its lease. A retained candidate from a deterministic failed generation remains available for exact repair or reuse but no longer consumes active capacity.

Candidate publication and full-batch selection share the candidate-pool lock. Once one policy epoch and local base lane contains the configured five eligible candidates, DWW freezes the oldest five in publication order. The fifth candidate's Finish releases its task worktree before long integration, then obtains the single persisted integration turn. A second full batch may freeze and wait while the first validates. No resident process is required: an interrupted generation is resumed from recorded phases and Git facts.

`batch seal --candidate ...` captures exactly 1 through the configured `batch_size` candidates for a smaller final tail. The coordinating native task calls it only after it knows all intended tasks have completed; workers publish and stop. The seal records one immutable generation and base snapshot. Both automatic and tail batches apply each candidate's exact tree difference to a dedicated detached integration worktree, create deterministic local integration commits, run combined Full validation, recheck the base snapshot and clean checked-out base worktree, then fast-forward.

Only an exact complete-batch count under `auto_full` may create an automatic seal. No timeout, idle state, active-task heuristic, SessionEnd event, or Hook event may infer a smaller tail. A later revision cannot mutate a captured generation. `start --supersedes` publishes a repair candidate for a future generation.

Composition or final-validation failure records the generation as failed and preserves the base. Its candidates become retained rather than automatically pending again, so another publication cannot blindly recreate the same failed batch. A composition failure records the exact conflicting candidate and makes only that unsealed retained candidate eligible for `candidate repair --candidate <id>`. The command creates an idempotent managed task on the latest base, writes a complete repair anchor, and prepares the immutable candidate ref with `merge --no-commit --no-ff`; clean and conflicted preparations both remain inside the repair worktree. Exact-path Commit may complete only this recorded repair merge. Publishing the verified repair supersedes the old candidate. Other unchanged compatible retained candidates may be named again by exact identity in a reviewed new generation.

Automatic repair preparation is bounded to two generations in one supersession chain. The host resolves a prepared conflict without user interruption only when executable facts and durable contracts determine one answer. Product, permission, migration, deletion, security, or mutually valid test choices require human input. Final-validation and promotion failures are not eligible for automatic merge repair and retain their evidence for diagnosis. `batch recover` is reserved for an interrupted nonfailed transaction and resumes from the recorded phase and Git facts. After promotion it completes worktree/ref cleanup idempotently. Successful completion deletes every included task anchor. `candidate withdraw` deletes only an unsealed pending or retained candidate and its anchor.

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
