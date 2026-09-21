# Lifecycle reference

Use this reference for normal route, Start, Commit, Ready, Finish, candidate,
status, and tail-batch work. Use [recovery](recovery.md) when an exact operation
is interrupted or fails, and [task governance](task-governance.md) for root and
child anchor rules.

## Route first

Routing chooses the lifecycle owner in this order: a detected mature workflow,
a local long-term current-directory choice, exact current-task authorization,
managed policy, then the first-modification choice. A mature workflow wins. A
command admitted as `defer` writes no DWW task, anchor, candidate, or batch
state unless its tracked delegated contract is valid and locally approved.

The host owns task/subagent decomposition, scheduling, waiting, and status. DWW
owns Git execution identity and local integration safety. Hooks may provide route
context or wake a check, but a Hook, idle state, or session end never proves
completion or performs a lifecycle transition by itself.

### First modifying intent

In an unchosen repository, DWW asks one plain-language question. `choose --mode
isolated` adopts the ordinary managed flow. `choose --mode current-repository`
stores a local preference without changing tracked files. The session-bound
`current-task` compatibility path requires a trusted session identity; do not
simulate it by weakening managed isolation. Before choosing, DWW routes again,
so a detected mature workflow remains deferred.

## Managed task

Initialization uses the attached local branch of the worktree passed through
`--repo`; a linked worktree is therefore a valid policy and delivery target.
It never substitutes `origin/HEAD` or a legacy `solo-ai.default-branch` value
for that explicit calling context. A detached invocation is rejected until a
checked-out local branch is selected.

### Windows Codex sandbox and the shared `.git` directory

When Codex opens a worktree under `CODEX_HOME/worktrees`, that worktree is the
place to edit project files. Its `.git` file points to the repository's Git
common directory, usually the `.git` directory beside the original checkout.
This is normal Git worktree layout: the worktree keeps its own `HEAD` and index,
while the common directory owns shared objects, refs, and DWW's local state.
Do not move ordinary file edits to the common directory.

The common directory is still where `start`, `commit`, `ready`, `finish`, and
other DWW lifecycle commands update Git and DWW metadata. Under Codex's
`workspace-write` sandbox, that metadata may be protected even when the
worktree itself is writable. A Windows sandbox identity can also make Git reject
the shared repository as "dubious ownership". If DWW returns
`GIT_METADATA_ACCESS_REQUIRES_HOST_APPROVAL`, rerun the exact same DWW command
through the host's reviewed escalation (`auto_review`). That is a narrow,
per-command approval for the lifecycle action; it does not grant a general
write exception.

Do not fix this condition by adding a global `safe.directory` exception,
changing `.git` ownership, or loosening filesystem ACLs. Those changes weaken
the ownership or sandbox boundary for more than the one reviewed operation.
Using Codex's Local environment can remove the extra outer worktree, but it does
not make protected Git metadata writable from an unapproved sandbox command.

### A repository with no commits

Accepted initialization (including `choose --mode isolated`) can adopt a clean
unborn repository. Preview reports `initial_empty_baseline` without creating a
commit. After acceptance, DWW preserves the symbolic HEAD branch name and uses
a temporary index to create one empty parentless baseline before its ordinary
bootstrap commit. Existing history does not receive an extra baseline.

Staged, untracked, or ignored files, unreadable refs, and missing Git author
configuration block automatic adoption without enrolling user content. Fix the
reported cause and retry: a baseline already created before a later bootstrap
failure is reused. A recorded pending bootstrap still uses the normal recovery
path; it is not overwritten.

For a dirty target, bootstrap records the target branch, worktree identity, and
starting commit before keeping user content out of its policy commit. Finish
rechecks those recorded facts and cleanliness before merging, so it cannot send
the policy or later task delivery to another worktree. Schema-1 bootstrap records
remain readable only when their branch and bootstrap parent identify one local
target; incomplete or ambiguous records stop with a preservation error.

`start` selects an idle slot, derives a task branch from the local base branch,
and records the branch, frozen base, worktree identity, and optional stable
request ID. Repeating the same non-empty request ID with the same purpose and
base returns the original task instead of allocating another slot.

Before Start returns a writable worktree it creates one local task anchor. If a
project configures `runtime_adapter.activate`, the task stays `starting` until
the Adapter records success and DWW repeats the clean identity check. At this
point the Adapter receives task, slot, worktree, base, and port-block facts; it
does not receive `candidate_head`, because no candidate exists. Activation
failure is retryable through the recorded task, while contamination is preserved
and quarantined.

Work only in the returned worktree. Update the anchor when its target, scope,
acceptance, current progress, or material blocker changes. Do not stage a broad
set of files: `commit` requires a reviewed exact path manifest and preserves any
unreviewed content. See [task governance](task-governance.md) for anchor update
and continuation details.

When a command needs a temporary input file, create that one exact file inside
the active authorized task or base worktree, run the command, confirm the result
was persisted, and delete the same file before Finish while authorization is
still active. Do not rely on a terminal-time deletion exception or recursively
remove a temporary directory; preserved historical inputs remain preserved.

## Ready and Finish

Ready remains available for development evidence and legacy policies. It checks
the exact task identity and selected Ready profiles. The default candidate-first
policy does not make Ready a mandatory project-test gate: run useful development
checks, then Finish preserves the exact source candidate and the combined batch
runs the required integration checks.

Direct integration remains compatibility for a repository explicitly configured
for it. Its Finish records a persisted transaction and fast-forwards only the
exact clean base; it never resets, cleans, or adopts ambiguous content.

## Candidate-first delivery

New repositories default to batched candidate-first integration:

1. Finish freezes `refs/dww/candidates/<id>` and records the exact source in
   `held` state.
2. If configured, the Runtime Adapter releases project resources without
   changing the task tree.
3. DWW rechecks cleanliness, releases the task worktree and slot, then marks the
   candidate `pending`.
4. The task anchor stays until the candidate is delivered, withdrawn, or the
   task is otherwise terminal.

The source candidate is immutable. Publishing it ends the developer's coding
round, but it is delivered only after a completed batch is contained in the
current base. Runtime effectiveness is a separate explicit project check.

Human-readable Finish and status output says “Change saved; waiting for local
integration” until Git facts prove it reached the current local base. An
`auto_full` wait may report only the compatible saved-change count for that
candidate's exact frozen base and activation policy. An explicit tail instead
waits for a recorded delivery cause; it must not infer a reason from task counts,
other bases, other repositories, UI state, or elapsed time. If the relevant
recorded fact cannot be confirmed, report the wait reason as unknown.

The pool counts held, pending, and sealed nonterminal candidates. Its default
capacity is 10. A full pool preserves the publishing task rather than dropping
work. A release failure leaves a candidate held and recoverable; it cannot enter
a batch.

### Automatic batches and explicit tails

With `seal_policy = "auto_full"`, the third eligible pending candidate in the
same frozen-base and policy lane automatically freezes the oldest exact three.
The lane includes base ref, base head, and activation epoch, so candidates from
different bases or policies are never mixed.

A smaller tail requires:

```text
batch reconcile --force --cause round-complete --reason <one-line-basis>
```

`user`, `deploy`, and `dependency` are explicit immediate-integration causes.
Ordinary “complete”, “finish”, or “review” wording describes the requested
outcome; it does not select `user`. Use that cause only when the user explicitly
asks to integrate now without waiting for compatible work. `round-complete`
refuses a lane with active producers. A full compatible batch does not wait for
unrelated unfinished work. Start, candidate activation, Abandon, and tail
reconciliation share admission locking, so the chosen candidates are an exact
snapshot. UI counts, raw worktree enumeration, Hook delivery, quiet time, and
session end cannot choose a batch.

A batched source task can carry the same intent at publication with
`finish --cause <cause> --reason <basis>`. DWW persists the first intent and
reconciles only that candidate's exact pending lane. Without both options,
Finish retains ordinary source-publication behavior.

### Combined verification and promotion

A frozen batch applies each candidate's exact tree difference in an isolated
integration worktree, runs selected Ready and integration Full profiles, and
then rechecks the composed head and sealed base before protected promotion. Pure
proofs may be reused only under their matching contract; mutable profiles run
again. [Verification reuse](verification-reuse.md) explains the selection and
proof rules, while [Runtime Adapter](runtime-adapter.md) covers optional batch
resources.

Failure before promotion records the generation and preserves the base. An
interrupted nonfailed generation resumes with `batch recover`; a deterministic
failure is handled through [recovery](recovery.md), not blindly resealed.

## Candidate inspection, withdrawal, and abandonment

Use ordinary `status` for a human summary. `status --compact` and `candidate
status --compact` provide read-only current views without receipt reconciliation;
use exact task, root, or batch selectors to narrow the projection and request
history only when needed. `candidate status --check` explicitly compares the
registered candidate records with the DWW ref namespace.

`candidate withdraw --candidate <id> --reason <one-line>` removes an eligible
pending or retained candidate from future integration after exact SHA checking.
It keeps the immutable candidate ref and frozen withdrawal audit facts. It is not
task abandonment. The normal `abandon --reason <one-line>` requires a releasable
worktree, then performs its recorded cleanup and releases the slot; it refuses a
candidate already held by an active batch. When a reviewed isolated worktree must
remain exactly in place, use `abandon --retain-worktree --reason <one-line>` with
the exact task confirmation. It requires no registered development process or
runtime-adapter activation, rechecks the branch, HEAD, and full worktree status,
then records the task as terminal while retaining every file and quarantining the
slot with the supplied reason. It never cleans, resets, detaches, deletes the
branch, or releases that worktree. Neither operation uses blanket cleanup.

When a terminal retained worktree is later known to contain only disposable
test residue, run `reclaim-retained --task <id>` first. A complete safe scan
returns the exact identity, slot generation, branch/HEAD, deletion checklist,
and one-use confirmation. If a read-only scan observes protected, unknown,
linked, unreadable, or unsnappable content, it returns a blocker report instead:
no confirmation or deletion list is issued. Re-run only a complete checklist
with its confirmation: that call runs the strict scan again rather than consuming
the diagnostic report, then removes unchanged ordinary files, detaches to the
recorded base, and returns the slot. It refuses active tasks, tracked work,
identity changes, and changed checklists; it keeps the terminal task record,
task branch, and abandonment receipt for audit.

If the retained worktree is a verified, explicitly disposable residue but its
contents cannot pass that ordinary-file checklist, use the separate two-step
`reclaim-retained --task <id> --dispose` preview and its exact `--confirm` value.
This is not a broad force option: it accepts only the original completed
retain-worktree task in its exact quarantined slot, refuses tracked changes or
branch/HEAD/base drift, records a durable disposal receipt, removes only that
root without following symlinks or junctions, preserves its task branch and
abandonment audit, then recreates the same slot as a clean detached worktree at
the frozen current default-branch head.

## Compatibility modes

An explicit in-place task binds one clean registered worktree, branch, start
head, expected head, and trusted session. It never merges, detaches, resets,
cleans, or deletes that worktree at Finish. A trusted detached linked worktree
may attach exactly one task-prefixed branch through the recorded bind path.

Legacy direct and explicit-seal policies remain readable compatibility modes.
Each task snapshots its resolved policy at Start; a later configuration change
does not rewrite active task or candidate behavior.

## Local-only boundary

Start, Ready, Finish, candidate publication, sealing, recovery, and cleanup do
not fetch, pull, push, create a PR, deploy, rebase, squash, amend, or rewrite
history. An explicitly requested remote publish is separate: use the clean base
worktree, verify its branch and remote, run a dry-run, and never force, delete,
tag, create a PR, or deploy unless separately authorized.
