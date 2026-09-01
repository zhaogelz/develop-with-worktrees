# Architecture

```text
Host-native task / subagent orchestration
  goals, dependencies, workers, waits, user-facing status
                    ↓ one Git-writing task per worker
DWW lifecycle router
  defer → mature repository lifecycle
  delegated → exact approved adapter
  managed → task identity + anchor + isolated worktree → Adapter activate
                    ↓
Git safety and evidence
  exact Commit → Ready proof → Finish
                    ↓
Integration policy
  candidate-first → held → Adapter release → immutable pending pool
                  → auto full batch / quiet-or-explicit exact tail
                  → combined Full proof → protected fast-forward
  legacy direct   → exact local fast-forward
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
solo-ai/runtime-adapter/           content-addressed Adapter evidence
```

New tracked `.solo-ai/config.toml` defaults to candidate-first integration: full batches of five, a pool capacity of ten, and a 90-second `quiet_or_explicit` tail. Start, candidate activation, Abandon, and reconciliation share the admission lock. Exactly the oldest five eligible candidates in one base-and-policy lane freeze once; a smaller exact snapshot freezes only after the persisted lane is stably producer-free or an authorized explicit cause is supplied. There is no maximum-age auto-seal. Legacy repositories without an integration table remain direct; old batched policy without the new fields remains explicit.

## Task anchors

Managed Start creates the anchor before returning the writable worktree. If configured, the optional project Runtime Adapter runs only after the exact isolated task, slot, branch, worktree identity, and candidate head are durable, and receives those facts plus the deterministic slot port block. The task becomes active only after a successful receipt and a second clean identity check; failure stays retryably `starting`, while contamination is quarantined and preserved. Ready verifies that the anchor is a regular local UTF-8 file, at most 64 KiB, with the exact task id. The anchor is available through the Git common-dir rather than copied into every worktree. Direct completion and abandonment remove it. Candidate publication keeps it until explicit batch success or withdrawal.

## Direct transaction

Before changing a base ref, Finish freezes task, slot, worktree path and file identity, branch, base, candidate, and proof. The transaction advances `prepared → promoted → completed`. Git ancestry classifies interruption. Completion keeps the slot unavailable until a final identity/content check publishes it idle. The receipt is a validated projection, not the transaction authority.

## Candidate publication

Batched Finish records a publication transaction before Git cleanup. It creates one exact `refs/dww/candidates/<id>` ref and persists a `held` candidate identity and proof. The optional project Runtime Adapter must release project-owned resources successfully without changing the task tree. DWW then detaches the worktree, deletes only the exact task branch, releases the slot with its directory identity, and activates the candidate as pending. Adapter failure or pool exhaustion leaves a recoverable publishing task and does not touch the base.

## Candidate batch transaction

Automatic full sealing snapshots the oldest configured pending candidate count. `batch reconcile` may snapshot a smaller tail only after the exact base-and-policy lane has zero persisted modifying producers continuously for its quiet period, or after `--force --cause user|deploy|dependency`. `next_reconcile_at` is a host heartbeat contract, not a completion fact. UI worker counts, raw worktree counts, Hook delivery, or SessionEnd never choose candidates. `batch seal` remains the exact-list compatibility and recovery interface. The ordered candidates, base, and policy epoch form an idempotent seal intent, so wake-up cause and retries cannot duplicate a generation.

The batch uses a dedicated detached worktree. For each frozen candidate it applies the exact binary tree difference from that candidate's recorded base and commits the composed result. After composition it runs the repository's Ready plus Full profiles over the combined tree, reusing only exact unchanged proofs, checks the base still equals the sealed snapshot, and fast-forwards the one clean worktree that owns the target branch. Heavy database, complete-build, authentication, and browser profiles are Full-only.

Failure before promotion records a failed generation and preserves the base. Its candidates become retained and cannot be automatically selected again. A repair publishes a new candidate and the coordinating task may reuse unchanged exact candidates in a new generation. An interruption leaves a nonfailed recorded phase; `batch recover` resumes only that generation. Promotion is followed by idempotent worktree/ref cleanup, candidate completion, and anchor deletion.

Publication and delivery are separate projections. A candidate is delivered only when its completed batch is contained in the current base. An explicit `runtime verify --candidate` may then ask the project Adapter whether that source is effective in its runtime; DWW records the evidence but never interprets project ports, databases, browsers, authentication, or deployment semantics.

## Delegation and Hooks

A mature repository crosses the delegated seam only through a tracked declaration and machine-local approval of the exact contract and input hashes. Managed candidate batches are never imposed on a delegated workflow.

Hooks are adapters around this architecture. SessionStart may cache route context; PreToolUse may deny unsafe supported writes. The CLI and persisted Git facts remain authoritative when Hooks are absent. Hook code never seals a batch, marks a task complete, releases a slot, deletes an anchor, or repairs state.
