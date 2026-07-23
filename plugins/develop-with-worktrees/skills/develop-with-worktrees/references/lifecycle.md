# Lifecycle reference

Mode precedence is local disable, detected mature workflow, managed policy, then uninitialized confirmation. A mature workflow always wins and receives zero writes.

`init` shows a plan first. `init --accept` commits the policy and managed AGENTS block; `init --decline` stores only a local preference. Dirty primary worktrees are never stashed, copied, committed, or discarded.

`start` chooses the least-recently-used idle slot and uses the current local branch of the invocation worktree as its base. It records the base branch, base commit, and base worktree. Detached HEAD requires `--base`; an active managed task cannot be a base. `retarget` changes the base only after exact confirmation and invalidates Ready evidence.

`commit` requires an exact complete path manifest. `ready` requires a clean committed branch, verifies a safe synchronization with the recorded base, validates the Ready closure, and records a proof. `finish` takes a local FIFO integration ticket, rechecks the recorded base worktree, reuses only exact compatible evidence, then fast-forwards that base branch. A durable integration receipt makes detach, branch cleanup, and release retryable after interruption.

`recover` rotates a lease only after recorded operations and validation children are no longer live. `abandon`, `prune-slot`, and `deinit` require explicit confirmation. `deinit` removes only registered, ownership-matching managed worktrees after preflight and never scans disks for repositories.
