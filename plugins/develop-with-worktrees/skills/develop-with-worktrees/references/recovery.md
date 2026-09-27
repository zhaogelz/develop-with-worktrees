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
| Candidate is `held` | publication and release receipt | repair or recover the exact release path | candidate ref, task anchor, and base |
| Older published candidate has no ordinary branch | completed publication, original branch name, exact candidate ref and head | preview `candidate restore-branch --candidate <id>`, then use `--apply` for the exact missing branch | candidate ref and any current worktree; a conflicting branch is never overwritten |
| Batch is interrupted but not failed | batch phase and live operation | `batch recover` for that recorded generation | batch, base, and validation receipt |
| Composition conflict | failed batch and attributed candidate | prepare the exact candidate repair | retained candidates and conflict worktree |
| Stale prepared task merge | task ID, `MERGE_HEAD`, recorded base, and current branch head | preserve the resolved files and inspect the recorded candidate repair path after the active batch | task worktree and conflict resolution; never complete the stale merge |
| Validation failed | selected profile, log, and inputs | change inputs or make an evidence-based attribution | failed generation and base |
| Promotion blocked | sealed base/head and worktree identity | remove the external blocker, then recover | integration worktree and base |
| Unknown/protected/replaced content | path identity and inventory | stop and preserve the scene | all affected content |

`status --compact --task <task-id>` and `status --compact --batch <batch-id>`
are read-only snapshots. Add history only when investigating terminal records.

Resuming a clean pre-activation Start verifies the owned slot generation and the
same released predecessor as ordinary Start before changing Git. A missing branch
is created only from a detached HEAD already in the recorded base history; an
interrupted native branch creation advances only by fast-forward to that base.
Unique commits, conflicting release records, unknown files, and replaced
directories remain preserved. This clean recovery does not use `reset --hard`.

## Root and task continuity

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

An active validation process is observed, not duplicated because its output is
quiet. An incomplete Full receives a fresh execution identity for non-pure or
incomplete checks. A complete pure profile can reuse an exact matching proof;
an unchanged deterministic failure is not blindly rerun.

If Full already passed, recovery of runtime release, promotion, or cleanup
resumes the recorded batch transaction. It does not rerun Full merely to finish
that later work. A runtime resource cycle that was successfully released before
a new validation attempt must be activated again; see [Runtime Adapter](runtime-adapter.md).

`batch recover` is only for an interrupted nonfailed transaction. A failed
generation preserves the base and exact evidence. A reviewed successor with the
same candidates requires the explicit failed predecessor and an actually changed
external blocker; it cannot be created to repeat an unchanged deterministic
failure.

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
verify the actual host before declaring runtime success. A recovery package
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
