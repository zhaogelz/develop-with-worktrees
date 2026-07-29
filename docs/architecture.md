# Architecture

```text
Codex adapter
  Shared route: mature workflow → local/session choice → managed → ask
  Skill: one first-write route, default isolation, explicit child delegation
  Hook: one SessionStart route / supported-path PreToolUse deny / post-write preservation alert
        ↓
Host-neutral CLI lifecycle
  isolated task ── exact commit ── Ready/Finish ── local fast-forward
  in-place task ── exact commit ── Ready/Finish ── release only
        ↓
Git-common local state: preferences, session bypasses, leases, task identity, proofs, receipts, alerts
```

Normal tasks record `base_ref`, `base_head`, `base_worktree`, a generated branch, and a managed slot. They may synchronize a forward-moving base before Ready/Finish and merge only with fast-forward semantics.

An in-place task records the exact invoking worktree, attached branch, immutable `start_head`, mutable-only-by-DWW `expected_head`, and a hash of the Codex session identifier. It owns no slot or branch. All validation uses `start_head...HEAD`; profile proof inputs force `task:<id>` scope even if policy otherwise permits cross-task reuse. Finish writes an in-place receipt, releases the lease, and leaves Git checkout and ignored runtime data untouched.

The integration lock serializes an in-place Start with isolated merges. While an active in-place task binds one base worktree/branch, an isolated task may work normally but cannot Finish into that base. This avoids an invisible external HEAD advance that would invalidate current-worktree authorization.

Before a first write, a dependency-free shared router decides `defer`, `disabled`, `current-task`, `managed`, or `ask`. Mature repository workflows have absolute priority and receive zero DWW writes. The trusted `SessionStart` hook normally injects the compact route once; without hook context, the skill calls the read-only `dww route --json` fallback instead of loading `doctor`.

Only `ask` presents three choices: isolate all normal tasks, use the current directory for this task only, or remember current-directory development for this repository on this machine. The task-only choice is not an in-place task: it creates no task, policy, slot, or lifecycle proof. It is a hash-bound session bypass, so the hook also avoids recording ordinary writes as guard alerts. The host currently provides no parent-agent hook identifier; a writing child joins only with an explicit one-time delegation code, rather than an unsafe repository-wide or time-window bypass.

The Codex hook is a strong adapter layer, not the core authority. It reads local state and calls the same pure routing decision as the CLI before returning the standard `PreToolUse` deny output for supported Bash/editor actions. A mature external workflow gets one SessionStart deferral message, then Pre/Post hooks silently step aside. The hook cannot cover specialised execution paths that do not invoke it; a later hooked call or `doctor` can record a local alert and tell the agent to preserve the dirty worktree, but no immediate observation is promised. It never rolls back user files.

Validation profiles remain schema 3. The machine-global weighted FIFO queue and duration estimates are host-independent local services. Cleanup remains separately bounded: Finish deletes nothing; only an explicitly reviewed `prune-slot` can delete declared owned paths.
