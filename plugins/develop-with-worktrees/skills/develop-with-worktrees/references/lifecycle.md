# Lifecycle reference

Mode precedence is local disable, detected mature workflow, managed policy, then uninitialized confirmation. A mature workflow always wins and receives zero writes.

## Isolated task (default)

`start` selects the least-recently-used idle slot and derives a task branch from the invocation worktree's current local branch. It records the branch, base commit, and base worktree. `commit` requires an exact complete path manifest. `ready` safely synchronizes a forward base, validates the Ready closure, and records proof. `finish` takes the local FIFO integration turn, verifies the clean recorded base, validates again after synchronization, and fast-forwards only that base. Durable integration receipts make detach, branch cleanup, and release retryable after interruption.

## In-place task (explicit only)

`start --in-place --session` is a separate task type, not a flag that disables the workflow. It runs only in the current clean attached worktree, requires a Codex session identifier, creates no slot or branch, and records:

```text
base_worktree + branch + start_head + expected_head + session fingerprint + lease
```

Ignored data is allowed at start; Git tracked or nonignored changes are not. `commit`, `verify`, `ready`, `finish`, and `abandon` recheck the identical worktree, branch, expected HEAD, and session. Only exact-path `dww commit` advances `expected_head`. The verification base is always immutable `start_head`, never the moving branch ref. In-place validation forces task-scoped proofs.

In-place Finish writes a completion receipt and releases the task only. It does not merge, detach, switch, reset, clean, remove branches, or delete caches. A mismatch quarantines the task and preserves every file. Ordinary `recover` refuses it. After a prior Codex task ended, explicit `resume-in-place --confirm TASK:BRANCH:EXPECTED_HEAD` may transfer an unchanged active, ready, or quarantined task: it rechecks the recorded branch and expected HEAD, rejects live operations or validation, rotates the lease/session, clears Ready evidence, and changes no project files. A mismatch still requires manual restoration first.

An active in-place task blocks isolated Finish into its same recorded base worktree and branch. Isolated tasks may still Start from currently committed base content and work in parallel.

`abandon`, `prune-slot`, and `deinit` require explicit confirmation. In-place abandon never cleans or resets: it releases only a clean, still-bound task; dirty work remains preserved.
