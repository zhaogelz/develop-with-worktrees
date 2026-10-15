# Recovery reference

Use this reference after a lifecycle command reports an interruption, a held
candidate, a failed batch, an identity mismatch, or a repair request. Start with
the exact task, candidate, batch, and persisted error; do not invent identity
from a title, a worktree count, or a quiet log.

## Choose the recovery path

| Symptom | First inspect | Safe next action | Preserve |
|---|---|---|---|
| Bound root is stale | child anchor and current root | `anchor refresh-root`, then retry the blocked command | task worktree and current plan |
| Start remains `starting` | activation receipt, worktree identity, contamination | retry the same request ID or `recover` after the cause is known | task, slot, anchor, and unexpected files |
| Start was quarantined before activation because an old idle slot was dirty | task has no branch, anchor, activation, candidate, or ordinary untracked content; each tracked dirty blob equals the current descendant base or an exact blob in its post-baseline first-parent history | `recover` records and resumes the bounded pre-activation release | original scene, exact content fingerprints, accepted commit, slot generation, and ignored/protected content |
| Start was quarantined before activation because a clean fixed slot was detached | exact task and slot identity, no branch, anchor, activation, Ready, candidate, or delivery; detached HEAD belongs to current target history | `recover --task <id> --abandon-unactivated --confirm <id> --reason <one-line>` ends an unneeded task with an audit reason, then the next Start can restore the idle branch | task remains distinct from any replacement delivery; unknown content and audit slots |
| Start was quarantined by an old volume identity after a verified clone | completed volume recovery receipt and before-state, same task/slot directory numbers, no branch, anchor, activation, candidate, or delivery | the same exact `recover --abandon-unactivated` entry returns the empty task and records its reason | old state backup, empty task identity, protected content |
| A frozen native source is already in its current local base, but remains waiting without a batch | exact source HEAD and branch, no delivery claim, live process or validation; review the independent delivery | `handoff` to the current host, then ordinary `abandon` with an audit reason; close dependent sources before their ancestors | original commits, task histories, and abandonment receipts |
| Candidate is `held` | publication and release receipt | repair or recover the exact release path | candidate ref, task anchor, and base |
| Older published candidate has no ordinary branch | completed publication, original branch name, exact candidate ref and head | preview `candidate restore-branch --candidate <id>`, then use `--apply` for the exact missing branch | candidate ref and any current worktree; a conflicting branch is never overwritten |
| Batch is interrupted but not failed | batch phase and live operation | `batch recover` for that recorded generation | batch, base, and validation receipt |
| Composition conflict | failed batch and attributed candidate | prepare the exact candidate repair | retained candidates and conflict worktree |
| Stale prepared task merge | task ID, `MERGE_HEAD`, recorded base, and current branch head | preserve the resolved files and inspect the recorded candidate repair path after the active batch | task worktree and conflict resolution; never complete the stale merge |
| Validation failed | selected profile, log, and inputs | change inputs or make an evidence-based attribution | failed generation and base |
| Promotion blocked | sealed base/head and worktree identity | remove the external blocker, then recover | integration worktree and base |
| Unknown/protected/replaced content | path identity and inventory | stop and preserve the scene | all affected content |

Use `status --compact` for the current-work summary. Native task-head batches
use `batch status --batch <batch-id>` for their exact persisted phase; legacy
candidate snapshots use `status --compact --task <task-id>` or
`status --compact --batch <batch-id>`. Add history only when investigating
terminal records.

After native promotion, `NATIVE_RELEASE_PENDING` identifies the worktree and
each blocking relative path. Human output explains the reason and next step;
JSON adds `context.blockers` with `path` and `reason`, and instructions in
`next_action`. Reasons distinguish branch/HEAD drift, tracked changes,
configuration, databases, uploads/storage, unknown content, links, and failed
inspection. It never prints file contents or follows a blocked link target.
Preserve the scene, reconcile identity through DWW, and retain only verified
task-owned artifacts through the project's existing artifact policy. Then retry
that task's `finish`; recovery keeps the promoted result and passed Full.
This diagnostic grants no deletion or cleanup permission.

For example, `context.blockers` may contain:

```json
[
  {"path": ".tmp/regression/current", "reason": "link"},
  {"path": ".tmp/regression/uploads/sample.pdf", "reason": "uploads_or_storage"},
  {"path": ".tmp/regression/test.db", "reason": "database"}
]
```

Inspect the recorded batch and the reported paths. Use the project's existing
artifact lifecycle to retain verified task-owned artifacts outside the slot and
verify the retained result. Reconcile branch or HEAD drift through DWW before cleanup.
Retry `finish --task <task-id> --lease <current-lease>` after the blockers are
resolved. Completion requires the exact task delivery and slot-return receipt;
the target commit alone does not establish that the slot was returned.

Resuming a clean pre-activation Start verifies the owned slot generation and the
same released predecessor as ordinary Start before changing Git. A missing branch
is created only from a detached HEAD already in the recorded base history; an
interrupted native branch creation advances only by fast-forward to that base.
Unique commits, conflicting release records, unknown files, and replaced
directories remain preserved. This clean recovery does not use `reset --hard`.

## Root and task continuity

### Volume clone with unchanged directories

For native schema 12, a confirmed disk clone can leave every recorded path and
directory file number unchanged while the volume identifier changes. Stop host
work and validation first, then preview:

```text
dww migration recover-volume
dww migration recover-volume --confirm <preview-sha256> --confirm-no-live-work
```

Use the installed runner for these commands. The confirmation binds the full
state, current directory identities, and Git HEAD/branch facts. Recovery accepts
only one old-to-new volume transition. Paths, file numbers, modes, registration,
the common Git directory, clean worktrees, slot generations, and frozen task
heads must still match. Active operations, queued validation, unfinished batches,
owned integration workspaces, and ambiguous pre-activation tasks block it.
This narrow entry requires existing fixed-slot release identities; it does not
guess missing records or recover moved/recreated directories.

An absent, unregistered idle slot with no owner or disposal in progress and
cleared release identities is preserved as never created or formally disposed.
A missing slot that still has recorded directory identities remains blocked.
A clean pre-activation native task may retain its exact fixed branch only when
its released predecessor was delivered, its branch and HEAD still match that
source, and both source and delivery belong to the recorded base history.
After rebinding, ordinary `recover --task <id>` resumes that same task.

Only live slot records, their current/released task identities, and the idle
integration workspace are rebound. Task statuses, ownership, leases, commits,
proofs, historical batches, and ignored files are preserved. The original state
and an interruption-safe receipt are saved under `solo-ai/volume-recovery/` in
the Git common directory. Retry the same confirmation after a write interruption;
changed evidence requires a fresh preview. Recovery is not delivery or cleanup:
continue the exact existing task under its lifecycle owner afterward.
After a completed rebind, a root-identity pre-activation failure can use the
existing empty-task release only when its before-state and receipt identify the
same task and the current directory still passes ordinary release checks.

Start and `bind-root` already return the current complete root. After a real
continuation, context recovery, root-plan change, or candidate repair, read the
child anchor and run `anchor refresh-root` once. A stale review record blocks
Commit, actual Ready, and Finish publication until it is refreshed; it is an
automatic refresh-and-retry path, not a request for the user to reconfirm an
unchanged plan.

For an ended host session with an unchanged active task, use the exact handoff
path after verifying that no operation, process, or validation remains alive.
The receiving host must be resolved from trusted context or supplied exactly
with `--host-kind` and `--host-thread`; a successful handoff atomically rotates
the lease and replaces the task's `host_origin`. Never reuse an old lease,
abandon real uncommitted work, or create a replacement task merely to avoid
identity checks. See [task governance](task-governance.md).

## Validation and batch recovery

If the machine validation queue's `state.json` is entirely zeroed and normal
queue reads fail, first confirm that no validation command is still running.
The narrowly scoped `settings --recover-zeroed-queue <sha256> --confirm <sha256>
--confirm-no-live-validation` command accepts only the exact damaged bytes. It
holds the machine queue lock, rejects live or unknown tickets and visible
validation processes, saves the original bytes and an audit receipt, then
atomically restores an empty queue. The operator's no-live confirmation covers
processes whose environment cannot be read by the host. It does not accept a
truncated or otherwise malformed nonzero JSON file. Run the interrupted Full
again through its original task or batch after recovery; the recovery receipt is
not validation proof.

An active validation process is observed, not duplicated because its output is
quiet. An incomplete Full receives a fresh execution identity for non-pure or
incomplete checks. A complete pure profile can reuse an exact matching proof;
an unchanged deterministic failure is not blindly rerun.

If a native `batch recover` finds an incomplete Full attempt after the runner
was interrupted, inspect its recorded process and queue ticket, then use
`batch recover --batch <id> --confirm-no-live-validation` only when the
original validation has stopped. Recovery checks the exact recorded process
creation time, every recorded queue ticket, and the unchanged batch workspace
before closing the attempt. It preserves completed profile results and logs;
an already failed profile leaves the batch failed for managed repair, while a
pure interruption releases runtime and leaves the batch composed for a fresh
Full on the next `batch recover`. This recovery is not a passing proof.

If Full already passed, recovery of runtime release, promotion, or cleanup
resumes the recorded batch transaction. It does not rerun Full merely to finish
that later work. A runtime resource cycle that was successfully released before
a new validation attempt must be activated again; see [Runtime Adapter](runtime-adapter.md).

Without explicit external recovery evidence, `batch recover` only resumes an
interrupted nonfailed transaction. A failed
generation preserves the base and exact evidence. A reviewed successor with the
same candidates requires the explicit failed predecessor and an actually changed
external blocker; it cannot be created to repeat an unchanged deterministic
failure.

A stopped native batch whose only failures are non-ordinary execution exits in
profiles with external state may resume after the external fault has actually
been repaired. Review the failure log and the real recovery check, then use:

```text
dww batch recover --batch <id> --confirm-no-live-validation --external-failure-evidence <local-json> --confirm-recovered <id>:<failed-attempt>:<integration-head>
```

The UTF-8 JSON file must stay inside the invoking repository and contain
`schema_version: 1`, `batch_id`, `validation_attempt`, `integration_head`,
`base_before`, and nonempty `failure_observation` and `recovery_observation`
strings. Record the observed fault and completed recovery without credentials or
private payloads. This is a reviewed attribution, not automatic proof of the
fault's cause. The entry checks the stopped attempt, exact unchanged source and
base, clean owned workspace, absent queue tickets, and passed runtime release.
Ordinary assertion failures, pure checks, timeouts, unfinished commands and
identity drift remain blocked. It snapshots the evidence, preserves the original
failed attempt and starts a new Full; it never marks failure as success. Repeating
the same request resumes that transaction and does not duplicate a completed Full.

## Repair decisions

### Recovering DWW itself

An installed DWW bug can block promotion of its own fix. The installed
`maintain-dww-plugin.ps1` supports `-Mode RecoveryInstall` with the same exact
`-SourceRepo`, `-SourceCommit`, and `-CodexPath` arguments as ordinary Install.
`batch recovery-source --commit <full-sha>` is a read-only preflight. It accepts
the existing promotion-blocked legacy batch after passed Full, or one frozen
native DWW maintenance task with a clean, exact source commit and passed
task-level Full proof. The native source can be checked before an integration
batch exists; its main base, task and slot identity, anchor, proof inputs, logs,
and runtime state must still match. Missing, changed, superseded, or unverified
sources are rejected.

RecoveryInstall archives the immutable commit and uses the normal marketplace
and CLI installer. It does not execute an arbitrary source runner, overwrite an
installed cache, disable protection, or change Git/DWW state by hand. It keeps
the previous release and includes the validation identity in the release
receipt. After recovery, complete normal integration and Install from main;
when main contains the recovery commit with an identical plugin tree, Install
verifies the recovery receipt and installed version before promoting its source
identity and removing the retained previous release. If main has a newer plugin
tree, Install first verifies that recovery version and the old backup, then
switches to the new tree through its normal staged release path.
Verify the actual host before declaring runtime success. A recovery package
does not mean that its source has been delivered. Host review is still required
when the host reports a changed Hook definition.

The installed maintenance entry and its installed recovery-source verifier must
both support the native source before RecoveryInstall can use it. A fix present
only in an uninstalled task worktree cannot authorize its own installation;
preserve the verified source and use an explicitly supported host maintenance
channel if the installed verifier still understands only legacy batches.

For `RETAINED_DISPOSAL_ACCESS_DENIED`, inspect the reported filesystem object
and execution identity. Sandbox approval and a Windows administrator token are
different. Preserve the staged transaction and resume the same confirmation
after access is legitimately available; do not loosen ACLs or take ownership.

A composition conflict may identify one retained candidate for managed repair.
The repair uses the latest base and is bounded to two published generations in a
supersession chain. Resolve automatically only when code, durable contracts,
tests, and the user's request determine one compatible result.
For a normal source candidate, do not merge `main` while waiting for another
batch. If an earlier manual merge is already prepared against an obsolete head,
Commit reports the task, prepared head, and current head and preserves the
worktree. Review and preserve any resolved files before changing that scene;
the old merge is not made valid by a broader parent allowance. A replacement
candidate follows the recorded repair path and receives combined validation on
the batch's actual execution base.

Final-validation failures and promotion blocks are not merge conflicts. Before
creating a validation repair request, the coordinator reviews the exact
candidate, test, and log evidence. Ask the user only if the evidence leaves
materially different product, permission, migration, deletion, security, or
test outcomes open. The [host handoffs](host-handoffs.md) reference covers the
separate task-notification protocol.

## Runtime Adapter bootstrap exception

When the approved `activate` implementation itself prevents Start, the narrow
`recover --repair-runtime-adapter --path <exact-input>` path may repair only the
persisted failed activation. It needs the original clean pre-activation task,
an exact failed receipt, both activate and release commands, and each named path
inside the approved Adapter input closure (plus the allowed configuration path).
Commit remains limited to the recorded exact paths; normal validation and
release rules still apply. This is not a general escape hatch for a dirty task.

## Cleanup and uncertainty

Never treat a missing, moved, dirty, protected, linked, or unreadable path as a
successful cleanup. Ordinary task return, reusable integration-workspace return,
dedicated-batch retirement, fast retirement, and `prune-slot` have different
preconditions. Follow [safety](safety.md) before any cleanup operation.

When a process boundary, runtime release, directory identity, or Git fact is
uncertain, the safe result is a recoverable blocked state with the base unchanged.

## Dirty pre-activation slot release

`recover` may return a quarantined isolated task only when Start failed before
creating its branch or anchor because an already detached idle slot was dirty.
It accepts no content merely because a backup exists: there must be no ordinary
untracked or protected ignored files. Every tracked dirty file must either have
the same Git blob as the current base ref, or have that exact blob at the same
path in the current base's first-parent history after the task baseline. The
recorded transaction pins the worktree and managed-root identities, slot
generation, old detached HEAD, base HEAD, porcelain status, each blob, and the
accepting commit. A matching blob only in an older baseline, another ref, or
similar text is not enough.

The release uses `reset --hard` only after those checks prove the tracked content
is already in the accepted base. It never runs `clean`, never removes ignored
content, and rejects a changed base, directory, slot generation, HEAD, status,
or file blob. A crash after the reset resumes from the recorded transaction and
does not invent the missing task branch or anchor. The slot becomes reusable only
after the post-reset identity and content checks pass. Any other dirty Start
continues to use ordinary Start recovery and remains preserved on uncertainty.

An explicitly abandoned clean fixed-slot Start uses the same durable release
transaction. It requires the exact task ID and one-line reason and accepts only
the recorded fixed-branch identity failure before any task branch, anchor, Ready,
candidate, or delivery exists. The detached worktree must be clean and its HEAD
must be an ancestor of the current descendant base. It records an `abandoned`
terminal result; a separate replacement delivery does not become this task's
delivery. The next Start performs the guarded fixed-branch restoration.
The completed empty release becomes the slot's immediate predecessor, even when
an older task previously occupied that slot. If an older completed release left
the slot idle but still pointing to that older predecessor, run
`recover --task <empty-task-id>` again: it repairs only the same slot generation
after verifying the original release, worktree identity and clean detached HEAD.
The base may have advanced along the same history; a changed slot generation,
diverged base or replaced worktree remains blocked.
