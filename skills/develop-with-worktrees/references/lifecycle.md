# Lifecycle reference

The managed lifecycle is `idle -> active -> ready -> finished`. A failure preserves the active worktree. Unsafe cleanup moves a slot to `quarantined`.

`start` allocates the least-recently-used idle slot and branches from the committed local default branch. Uncommitted primary-worktree files are excluded. No remote operation occurs.

`ready` requires a clean, committed candidate. It merges the latest local default branch into the task branch without rebasing, scans the resulting diff, and records a validation proof.

`finish` uses a FIFO ticket and one integration lock in the Git common directory. After taking the lock it requires a clean primary worktree, synchronizes again if the default branch advanced, validates or exactly reuses a proof, then fast-forwards the default branch. It detaches the reusable slot and deletes only the integrated short-lived branch.

Slot dependencies are intentionally lazy and retained. The first task in each slot can therefore be slower; later tasks reuse local dependency state when the project's own tools allow it.

Use `status` for normal inspection. `status --detailed` recursively measures slot disks and is intentionally more expensive. `recover` rotates a stale lease. `abandon` requires exact task-id confirmation and preserves known dependency/config caches. It quarantines rather than guessing when ignored data is unknown.
