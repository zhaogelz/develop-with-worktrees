# Lifecycle reference

Mode precedence is detected mature workflow, local long-term current-directory choice, exact current-task authorization, managed policy, then the first-modification choice. A mature workflow always wins, including over choices 2/3, and receives zero writes.

The trusted `SessionStart` hook normally injects one compact route. When that context is unavailable, the skill runs one read-only `dww route --json`; it never uses the full `doctor` report for first-write routing. The actions are `defer`, `disabled`, `current-task`, `managed`, and `ask`.

## First-modification choice

The Codex adapter asks one plain-language question only when the user first intends to write an unchosen repository. `choose --mode isolated` adopts the normal lifecycle and accepts internal static-only checks when no test command is discovered. `choose --mode current-task --session` creates no tracked file, task, slot, or policy and makes that session behave as if DWW were absent; it refuses if this exact directory already owns an active DWW task, which must first Finish or be abandoned. `choose --mode current-repository` uses the same no-active-task, no-queue, and no-lock checks as `disable`, then stores the local preference without touching tracked files.

Before applying any of those modes, `choose` routes again. If a mature workflow exists, it returns `deferred` and does not write policy, preference, session, task, or slot state.

The host hook exposes no reliable parent-agent or task identifier. A writing child session therefore joins a current-task choice only through an explicit one-time delegation code. This is deliberately narrower than a time-window or repository-wide bypass, which would allow unrelated concurrent tasks. A new session without a registered delegation returns to the normal first-choice behavior.

## Isolated task (default)

`start` selects the least-recently-used idle slot and derives a task branch from the invocation worktree's current local branch. It records the branch, base commit, and base worktree. `commit` requires an exact complete path manifest. `ready` safely synchronizes a forward base, validates the Ready closure, and records proof. Each profile rechecks the expected base after entering the machine validation queue and before each command; Ready checks it again after validation. If another Finish advanced the base, the same Ready call resynchronizes and reuses exact unchanged profile proofs. Five automatic retries bound convergence; continued movement preserves the task and fails explicitly instead of recording a stale Ready proof. `finish` takes the local FIFO integration turn, verifies the clean recorded base, validates again after synchronization, and fast-forwards only that base. Durable integration receipts make detach, branch cleanup, and release retryable after interruption.

Finish is the terminal DWW operation and remains local-only. An explicit user-requested remote sync occurs only after Finish as a separate ordinary Git operation from the clean base worktree. It starts with a push dry-run and permits only a non-force push of the current branch; DWW state, leases, and lifecycle locks are not extended to cover publishing.

## In-place task (explicit only)

`start --in-place --session` is a separate task type, not a flag that disables the workflow. It runs only in the current clean attached worktree, requires a Codex session identifier, creates no slot or branch, and records:

```text
base_worktree + branch + start_head + expected_head + session fingerprint + lease
```

Ignored data is allowed at start; Git tracked or nonignored changes are not. `commit`, `verify`, `ready`, `finish`, and `abandon` recheck the identical worktree, branch, expected HEAD, and session. Only exact-path `dww commit` advances `expected_head`. The verification base is always immutable `start_head`, never the moving branch ref. In-place validation forces task-scoped proofs.

In-place Finish writes a completion receipt and releases the task only. It does not merge, detach, switch, reset, clean, remove branches, or delete caches. A mismatch quarantines the task and preserves every file. Ordinary `recover` refuses it. After a prior Codex task ended, explicit `resume-in-place --confirm TASK:BRANCH:EXPECTED_HEAD` may transfer an unchanged active, ready, or quarantined task: it rechecks the recorded branch and expected HEAD, rejects live operations or validation, rotates the lease/session, clears Ready evidence, and changes no project files. A mismatch still requires manual restoration first.

An active in-place task blocks isolated Finish into its same recorded base worktree and branch. Isolated tasks may still Start from currently committed base content and work in parallel.

`abandon`, `prune-slot`, and `deinit` require explicit confirmation. In-place abandon never cleans or resets: it releases only a clean, still-bound task; dirty work remains preserved.
