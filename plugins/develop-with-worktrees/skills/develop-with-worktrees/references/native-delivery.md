# Native task-head delivery

Newly adopted repositories with a clean target use state schema 12. A first
adoption with a dirty target retains schema 11 while its bootstrap commit is
pending. Existing schema-11 repositories keep their recorded candidate flow
until the guarded migration below succeeds.
Use `status --compact` to distinguish these modes before acting.

## Task and slot

`start` claims an idle fixed slot branch, records its generation and exact
target base, and returns that slot's worktree and lease. It checks prior delivery
or a completed ordinary abandonment release,
branch identity, cleanliness, and unknown files before reusing a slot. Start
does not install dependencies, build output, or launch project services.
For a registered idle slot left detached with no fixed branch, Start may recreate
that branch only after checking its recorded directory identity, any released
predecessor, clean worktree, unknown content, and detached HEAD ancestry in the
selected target. Any mismatch remains quarantined with its files preserved.

When a separately reviewed commit already exists in the same local Git object
database, `merge-source --task <id> --lease <lease> --source <full-sha>` records
the task HEAD, target base, and exact source before preparing a no-fast-forward
merge. A repeated call accepts only the same source and `MERGE_HEAD`. Review and
resolve any conflicts in the task worktree, then use ordinary `commit` with all
changed exact paths. An undeclared merge source, moved target, or changed task
HEAD remains blocked. This command performs no fetch or remote publication.

Edit only the returned worktree. `commit --path <exact path>` stages the
reviewed paths. `ready --task <id> --lease <lease>` freezes the branch, worktree,
generation, source head, and selected check evidence. A changed branch, dirty
tree, old lease, or moved slot requires a managed withdrawal and a new Ready
head. `finish --task <id> --lease <lease>` releases any configured task runtime,
then records that frozen head as waiting for integration; it does not release
the slot or claim delivery.

An exact full group freezes automatically. An explicit smaller tail needs a concrete
`round-complete`, `user`, `deploy`, or `dependency` cause and one-line reason.
Use `round-complete` only after the agreed round really ends and the exact
target lane has no active producer. Check the remaining agreed work and parallel
sources before closing a tail; ordinary task completion does not imply an
immediate tail. A blocked native Start may internally seal a capacity batch
only when no fixed slot is free and eligible finished sources are waiting;
this is not a host-selected tail cause or permission to finish active development.
When those round facts are verified, the host records and follows the eligible
tail without asking the user to repeat the task authorization.

## Integration and repair

One integration workspace owns a target branch at a time. It freezes the base
and ordered Ready heads, performs real Git merges, runs selected Full profiles
on the composed head, releases configured batch runtime after Full, then
promotes only to the exact unchanged base. The source commits and any integration
repair commit remain in the delivered ancestry.
Later batches wait for the prior batch, then use its delivered base.

`batch status --batch <id>` and `status --compact` expose waiting and failed
states. `batch reconcile` with `--force`, a supported `--cause`, `--reason`,
and `--base` closes an eligible tail. For a small, clearly shared integration
script fix, `batch repair` takes the exact batch, expected head, patch file,
paths, commit message, and reason. It commits the patch in the owned
integration workspace after the prior validation processes stop, starts a new
attempt, and keeps the failed attempt. Semantic
source changes return to the task owner. Do not edit the integration worktree
directly or retry an unchanged failed input in a loop.

After promotion, the integration worktree and task slots return under exact
identity checks; delivery receipts must succeed before a slot is idle. A release
failure retries only its unfinished tail. A cancelled or unknown slot is
preserved, not reset for another task.

Project runtime preparation is on demand; see [Runtime Adapter](runtime-adapter.md).
Pure proof reuse requires its declared inputs, tools, environment, command,
configuration, and logs to match. Side effects and required outputs need fresh
evidence.

## Existing repository migration

Run `migration preview --base <branch>` for a read-only blocker list. It
checks active legacy tasks, unsettled candidates and batches, owned resources,
the old integration workspace's saved result and directory identity, and each
slot's Git identity and unknown content. A completed, released idle integration
workspace is verified and carried into schema 12, including when its last batch
targeted another branch. An existing directory without an exact legacy record,
or a record whose saved ref, Git registration, identity, or contents have drifted,
blocks migration and remains untouched. An already enabled preview reports
whether the workspace is managed, available, or still needs legacy adoption.
Finish or explicitly resolve those legacy records under their original rules.
Historical candidates, refs,
proofs, and failed evidence are retained.
An old completed batch can prove a net-difference candidate's delivery through
its exact recorded candidate snapshot and applied identity, completion and
promotion receipts, proof, and composed head in the current target history,
even if its terminal candidate ref has been pruned or it predates explicit
validation and runtime-release fields. A ref that still exists must match the
recorded head. A failed batch remains as failure evidence but no longer
blocks migration once its workspace is recorded as released or retired, its
runner is absent, and every source candidate has reached a terminal state.
A clean idle slot may retain an attached superseded source branch when its exact
candidate ref leads through recorded supersession links to a delivered candidate
on the migration target. Missing receipts, live ownership, unfinished sources,
unknown content, or broken links still block migration.
A clean idle slot may also retain an attached withdrawn candidate branch when
the slot's recorded predecessor, branch, and HEAD match that candidate, its
completed withdrawal audit and preserved candidate ref match the HEAD, and the
commit is an ancestor of the selected target head. The candidate's original
target may differ; the current target's Git ancestry is checked directly.
A terminal task's explicitly retained, quarantined worktree may stay in place
through migration when its abandonment record, slot generation, directory
identity, branch, and HEAD still match exactly and tracked files are clean.
Its ignored audit content is left untouched; the slot stays quarantined and is
excluded from fixed-branch initialization. Any identity drift still blocks
migration.

When preview is ready, pass its exact branch and head to `migration enable`
with `--base <branch>` and `--confirm <branch>:<head>`. Enablement initializes
fixed branches only for safe idle slots and is idempotent. A previously
delivered legacy net-difference candidate is marked as legacy evidence; it is
never represented as an ancestor
of main. This changes only the selected repository's local DWW state. It does
not install a plugin, migrate another repository, or grant new Hook trust.

If a migrated repository retains an idle legacy `solo-ai-integration` worktree,
the first native batch may stop with `OWNERSHIP_DRIFT` because native state has
no binding for that existing directory. After reviewing the exact legacy pool
record and sealed native batch, use the one-time managed command
`migration adopt-legacy-integration-workspace --batch <id> --confirm <id>:<legacy-head>:<generation>`.
If no native batch exists, use
`migration adopt-legacy-integration-workspace --base main --confirm main:<migration-head>:<legacy-head>:<generation>`
after confirming that `main` still points to the recorded migration head.
It requires the completed legacy batch receipt, persistent head ref, unchanged
directory and Git identity, clean tracked files, retained-content boundary,
and unchanged native target base. The old result may belong to another release
branch, including while the first native batch is sealed. In that case its
source branch ancestry, passed Full proof, promotion, and release receipt must
match the recorded old delivery; the sealed native batch must still be waiting
on the unchanged target base and must not have claimed the worktree. An
unreadable ignored directory is accepted only when it is an existing plain
top-level opaque dependency root. The command
copies the idle record into native state unchanged and writes an adoption receipt
in the same atomic state update. It does not check out, move, or clean the
worktree. Continue the sealed batch with `batch recover --batch <id>` only after
the adoption result is verified; that recovery claims the next generation.
