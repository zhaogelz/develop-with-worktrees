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
| Candidate is `held` | publication and release receipt | repair or recover the exact release path | candidate ref, task anchor, and base |
| Batch is interrupted but not failed | batch phase and live operation | `batch recover` for that recorded generation | batch, base, and validation receipt |
| Composition conflict | failed batch and attributed candidate | prepare the exact candidate repair | retained candidates and conflict worktree |
| Validation failed | selected profile, log, and inputs | change inputs or make an evidence-based attribution | failed generation and base |
| Promotion blocked | sealed base/head and worktree identity | remove the external blocker, then recover | integration worktree and base |
| Unknown/protected/replaced content | path identity and inventory | stop and preserve the scene | all affected content |

`status --compact --task <task-id>` and `status --compact --batch <batch-id>`
are read-only snapshots. Add history only when investigating terminal records.

## Root and task continuity

Start and `bind-root` already return the current complete root. After a real
continuation, context recovery, root-plan change, or candidate repair, read the
child anchor and run `anchor refresh-root` once. A stale review record blocks
Commit, actual Ready, and Finish publication until it is refreshed; it is an
automatic refresh-and-retry path, not a request for the user to reconfirm an
unchanged plan.

For an ended host session with an unchanged active task, use the exact handoff
path after verifying that no operation, process, or validation remains alive.
Never reuse an old lease, abandon real uncommitted work, or create a replacement
task merely to avoid identity checks. See [task governance](task-governance.md).

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

A composition conflict may identify one retained candidate for managed repair.
The repair uses the latest base and is bounded to two published generations in a
supersession chain. Resolve automatically only when code, durable contracts,
tests, and the user's request determine one compatible result.

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
