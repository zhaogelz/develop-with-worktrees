# Architecture

```text
Codex adapter
  Skill: chooses default isolation or explicit in-place task
  Hook: supported-path PreToolUse deny / post-write preservation alert
        ↓
Host-neutral CLI lifecycle
  isolated task ── exact commit ── Ready/Finish ── local fast-forward
  in-place task ── exact commit ── Ready/Finish ── release only
        ↓
Git-common local state: leases, task identity, proofs, receipts, alerts
```

Normal tasks record `base_ref`, `base_head`, `base_worktree`, a generated branch, and a managed slot. They may synchronize a forward-moving base before Ready/Finish and merge only with fast-forward semantics.

An in-place task records the exact invoking worktree, attached branch, immutable `start_head`, mutable-only-by-DWW `expected_head`, and a hash of the Codex session identifier. It owns no slot or branch. All validation uses `start_head...HEAD`; profile proof inputs force `task:<id>` scope even if policy otherwise permits cross-task reuse. Finish writes an in-place receipt, releases the lease, and leaves Git checkout and ignored runtime data untouched.

The integration lock serializes an in-place Start with isolated merges. While an active in-place task binds one base worktree/branch, an isolated task may work normally but cannot Finish into that base. This avoids an invisible external HEAD advance that would invalidate current-worktree authorization.

The Codex hook is a strong adapter layer, not the core authority. It reads local state and returns the standard `PreToolUse` deny output before supported Bash/editor actions run. A mature external workflow still causes complete deferral. The hook cannot cover specialised execution paths that do not invoke it; a later hooked call or `doctor` can record a local alert and tell the agent to preserve the dirty worktree, but no immediate observation is promised. It never rolls back user files.

Validation profiles remain schema 3. The machine-global weighted FIFO queue and duration estimates are host-independent local services. Cleanup remains separately bounded: Finish deletes nothing; only an explicitly reviewed `prune-slot` can delete declared owned paths.
