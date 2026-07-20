# Lifecycle reference

## Mode precedence

```text
local disable -> defer to detected mature workflow -> managed adoption -> uninitialized confirmation
```

Detection is checked on every managed action. Existing Worktrunk, repository `scripts/worktree-flow.ps1`, and other configured markers always win. In defer mode the plugin writes no tracked policy, slot, process, validation, or compatibility state.

## First adoption

`init` with no acceptance flag only returns a plan. The user chooses one of:

- `init --accept`: commit the three tracked policy artifacts.
- `init --accept-static-only`: only after reviewing that no test command will run.
- `init --decline`: save a local disable preference, with no tracked edit.

The bootstrap is made in a temporary worktree from committed local default HEAD. If the primary worktree is clean it fast-forwards immediately. If it is dirty, the bootstrap reference and temporary policy checkout remain under Git common metadata; Start branches from that reference, and the first Finish after the primary is clean fast-forwards the bootstrap before integrating the task. Primary dirt is never stashed, copied, committed, or discarded.

## Task state and lease

```text
inactive / idle -> starting -> active -> ready -> finished
                              |              |
                              +-> quarantined +-> active (after a new commit)
```

Excess reduced slots transition `active -> draining -> inactive`. Each task gets a random lease returned only by Start or explicit Recover. `status`, `doctor`, and JSON status redact it. Each mutable action atomically records `active_operation` plus process identity first, then clears it on exit. Recover rotates a lease only if that recorded operation is no longer live; it never repairs a quarantined task by guessing.

Start chooses the least-recently-used idle configured slot. It is lazy: it checks out source on first use and does not install dependencies. `warm-slot` is the only serial prewarm action. All slot caches and environments remain local to that slot.

## Commit, Ready, Finish

Commit requires `--path` for every changed tracked or untracked path. The exact manifest must equal the task change set; the lifecycle uses `git add -- <paths>`, never `git add -A`. It runs a repository-declared scanner when configured and then the built-in sensitive-content gate.

Ready requires a clean committed branch. It predicts the merge of the current local default branch without writing, then performs a normal merge only when prediction succeeds. A conflict remains in the current task worktree. Ready scans and validates the candidate and records its proof.

Finish creates a local FIFO ticket, takes one integration lock, checks both worktrees, synchronizes again, validates or reuses an exact proof, and performs only `git merge --ff-only` into the local default branch. A conflicting later task leaves the queue and keeps its worktree; it does not hold the queue hostage. No remote Git operation is part of this lifecycle.

## Recover, abandon, deinit

`abandon` needs an exact task id and current lease. It preserves unknown/protected ignored files by quarantining instead of cleaning. `deinit` is a separate two-stage removal: first it commits the exact tracked policy deletion, then it removes exact registered managed worktrees, and only then removes the exact local state directory. It never scans disks for repositories and never removes another tool's worktree.
