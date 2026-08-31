# Architecture

```text
Host-native task / subagent orchestration
  goals, dependencies, workers, waits, user-facing status
                    ↓ one Git-writing task per worker
DWW lifecycle router
  defer → mature repository lifecycle
  delegated → exact approved adapter
  managed → task identity + anchor + isolated worktree
                    ↓
Git safety and evidence
  exact Commit → Ready proof → Finish
                    ↓
Integration policy
  direct  → exact local fast-forward
  batched → immutable candidate pool → explicit seal → combined Full proof → fast-forward
```

The top seam is intentional: DWW does not compete with the host's task graph, worker dispatch, dependency management, or task UI. The legacy `solo-ai-orchestration` package remains only to drain already-created state. New work never creates a DWW controller identity or orchestration batch.

## Local state

All lifecycle state stays under the repository's Git common directory:

```text
solo-ai/state.json                 slots, tasks, leases, direct transactions
solo-ai/task-anchors/              active execution contracts
solo-ai/proofs/                    exact validation evidence
solo-ai/candidate-batches.json     immutable candidates and sealed generations
solo-ai/*-receipts/                rebuildable completion projections
```

Tracked `.solo-ai/config.toml` selects `integration.mode = direct|batched`. Direct remains the generic default. Batched mode defaults to five candidates per explicit seal and ten pending candidates in the pool. Capacity is backpressure only and never a seal trigger.

## Task anchors

Managed Start creates the anchor before returning the writable worktree. Ready verifies that it is a regular local UTF-8 file, at most 64 KiB, with the exact task id. The anchor is available through the Git common-dir rather than copied into every worktree. Direct completion and abandonment remove it. Candidate publication keeps it until explicit batch success or withdrawal.

## Direct transaction

Before changing a base ref, Finish freezes task, slot, worktree path and file identity, branch, base, candidate, and proof. The transaction advances `prepared → promoted → completed`. Git ancestry classifies interruption. Completion keeps the slot unavailable until a final identity/content check publishes it idle. The receipt is a validated projection, not the transaction authority.

## Candidate publication

Batched Finish records a publication transaction before Git cleanup. It creates one exact `refs/dww/candidates/<id>` ref, persists candidate identity and proof under a lock, detaches the worktree, deletes only the exact task branch, and releases the slot with its directory identity. Pool exhaustion leaves the task in a recoverable publishing state and does not touch the base.

## Explicit batch transaction

`batch seal` accepts an explicit ordered list of one through the configured batch size. It snapshots every candidate ref and the current base head. No worker count, queue count, timer, idle heuristic, Hook, or SessionEnd event can create this record.

The batch uses a dedicated detached worktree. For each frozen candidate it applies the exact binary tree difference from that candidate's recorded base and commits the composed result. After composition it runs the repository's Ready plus Full profiles over the combined tree, checks the base still equals the sealed snapshot, and fast-forwards the one clean worktree that owns the target branch.

Failure before promotion records a failed generation and preserves the base. It is deliberately not auto-retried. A repair publishes a new candidate and the caller names the intended candidates in a new seal. An interruption leaves a nonfailed recorded phase; `batch recover` resumes only that generation. Promotion is followed by idempotent worktree/ref cleanup, candidate completion, and anchor deletion.

## Delegation and Hooks

A mature repository crosses the delegated seam only through a tracked declaration and machine-local approval of the exact contract and input hashes. Managed candidate batches are never imposed on a delegated workflow.

Hooks are adapters around this architecture. SessionStart may cache route context; PreToolUse may deny unsafe supported writes. The CLI and persisted Git facts remain authoritative when Hooks are absent. Hook code never seals a batch, marks a task complete, releases a slot, deletes an anchor, or repairs state.
