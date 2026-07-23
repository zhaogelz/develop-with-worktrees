# Architecture

```text
Codex hook (reminder)
        ↓
Skill (decide read-only / defer / lifecycle)
        ↓
CLI (hard lifecycle gate)
        ├─ tracked repository policy
        ├─ Git-common task state, proofs, receipts
        └─ machine-user validation queue, settings, duration metrics
```

The repository policy is small: `config.toml` controls lifecycle and explicit cleanup ownership; `verification.toml` schema 3 maps paths to registered argv profiles. Local task state follows Git common metadata. The machine queue deliberately lives outside any repository so independent projects do not overcommit the same computer.

Task state records `base_ref`, `base_head`, and `base_worktree`. Ready and Finish operate only against that recorded base. Integration receipts make post-merge cleanup idempotent.

Validation profiles are classified only as normal or heavy. The machine computes capacity from physical CPU cores and total RAM, then admits profile execution in strict FIFO order. Waiters never hold repository state locks. Local duration medians are advisory data only.

Cleanup has a smaller deletion boundary than task state: Finish deletes nothing. Manual prune accepts only exact top-level ownership paths, stores an immutable review plan, and refuses any declared target containing `.env*`, a symlink/junction, or changed content.
