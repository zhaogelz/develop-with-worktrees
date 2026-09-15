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
  exact Commit → optional development proof → Finish
                    ↓
Integration policy
  candidate-first → held → Adapter release → immutable pending pool
                  → auto three-candidate batch / explicit exact tail
                  → affected combined proof → protected fast-forward
  legacy direct   → exact local fast-forward
```

The top seam is intentional: DWW does not compete with the host's task graph, worker dispatch, dependency management, or task UI. The legacy `solo-ai-orchestration` package remains only to drain already-created state. New work never creates a DWW controller identity or orchestration batch.

## Development priorities

DWW is built for fast AI-assisted development by individuals and small teams. After the user has authorized a task, the agent should keep moving through the in-scope investigation, implementation, checks, exact commits, locally permitted delivery, and deterministic recovery. Identity arguments such as `--confirm` verify an object; they are not a second request for user approval.

This does not remove the boundaries that make the workflow dependable. The agent still stops when the current request and durable project contract leave a material product, permission, migration, deletion, security, or external-side-effect decision unresolved. It keeps exact candidate identity, required validation, and protection for unknown working-tree content. New persistent services, state, abstractions, or human gates need an observed failure mode and a reason existing mechanisms cannot cover it.

## Approval and evidence

Machine-local approval describes the executable lifecycle policy: normalized repository and verification configuration, declared commands and environment names, profile coverage and closure, tool and lockfile identity, and Runtime Adapter inputs. Formatting-only policy edits such as comments or line endings therefore keep an existing approval; any semantic command, scope, permission, runtime, or configuration change produces a new plan and an explicit drift report. Older approval records remain readable but cannot authorize a newer plan.

Validation evidence has a separate, profile-scoped identity. Each check binds its own declared inputs, profile definition, tool facts, declared environment values, logs, and applicable frozen base. The aggregate receipt keeps the complete plan for audit, while an unrelated profile or formatting-only policy change does not rerun a check whose own inputs still match.

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

New tracked `.solo-ai/config.toml` defaults to candidate-first integration: source-only candidates, full batches of three, a pool capacity of ten, and an explicit tail. Start, candidate activation, Abandon, and reconciliation share the admission lock. Exactly the oldest three eligible candidates in one base-and-policy lane freeze once; a smaller exact snapshot freezes only after an explicit round-end or immediate-integration request. There is no age or quiet-period auto-seal. Legacy repositories without an integration table remain direct, and old batched policies retain Ready-gated candidate publication.

## Task anchors

Managed Start creates the anchor before returning the writable worktree. If configured, the optional project Runtime Adapter runs only after the exact isolated task, slot, branch, worktree identity, and candidate head are durable, and receives those facts plus the deterministic slot port block. The task becomes active only after a successful receipt and a second clean identity check; failure stays retryably `starting`, while contamination is quarantined and preserved. If the approved activate implementation itself is broken, an explicit repair recovery can convert only that same clean pre-activation task with an exact failed receipt into an Adapter repair: its activation is recorded as skipped, the caller freezes an exact subset of tracked approved Adapter input paths, Commit cannot escape that list, and the repaired release must clean any partial resources before publication. Ready verifies that the anchor is a regular local UTF-8 file with the exact task id; DWW imposes no content-size quota on task/root anchors, their plan inputs, retained plan versions, or exact closure state. The anchor is available through the Git common-dir rather than copied into every worktree. Direct completion and abandonment remove it. Candidate publication keeps it until explicit batch success or withdrawal.

## Direct transaction

Before changing a base ref, Finish freezes task, slot, worktree path and file identity, branch, base, candidate, and proof. The transaction advances `prepared → promoted → completed`. Git ancestry classifies interruption. Completion keeps the slot unavailable until a final identity/content check publishes it idle. The receipt is a validated projection, not the transaction authority.

## Candidate publication

Batched Finish records a publication transaction before Git cleanup. The default policy creates one exact `refs/dww/candidates/<id>` source identity without forcing a project test; legacy Ready-gated policies keep their existing proof. The optional project Runtime Adapter must release project-owned resources successfully without changing the task tree. DWW then detaches the worktree, deletes only the exact task branch, releases the slot with its directory identity, and activates the candidate as pending. Adapter failure or pool exhaustion leaves a recoverable publishing task and does not touch the base.

## Candidate batch transaction

Automatic full sealing snapshots the oldest configured pending candidate count. `batch reconcile --force --cause round-complete --reason <basis>` snapshots a smaller tail when the host has actually ended that round; `user`, `deploy`, and `dependency` record immediate-integration reasons. UI worker counts, raw worktree counts, Hook delivery, SessionEnd, and elapsed time never choose candidates. `batch seal` remains the exact-list compatibility and recovery interface. The ordered candidates, base, and policy epoch form an idempotent seal intent, so retries cannot duplicate a generation. A reviewed unchanged-candidate successor additionally names its exact failed predecessor; each explicit predecessor can therefore identify at most one successor generation.

The batch uses an isolated integration worktree. For each frozen candidate it applies the exact binary tree difference from that candidate's recorded base and commits the composed result. If the project configures the paired batch Runtime Adapter, DWW passes the exact generation identity, persisted positive `runtime_cycle`, and a dedicated non-slot port block to `batch_activate`, runs only the Ready and integration Full profiles selected by the composed changes after activation succeeds, then calls `batch_release` with the same cycle and persisted validation outcome. Pure profiles with complete declared inputs reuse their valid receipts from development or earlier candidates; mutable profiles run again. Activation and release receipts are content-addressed recovery facts; uncertainty within one command reuses that cycle, but a retry after successful release increments the cycle and cannot reuse a stale activation receipt. Uncertainty retains sole batch ownership and blocks promotion. DWW then rechecks the composed head, confirms the base still equals the sealed snapshot, and fast-forwards the one clean worktree that owns the target branch. Broad regression and pressure profiles are manual project diagnostics, not lifecycle gates.

Failure before promotion releases any configured batch runtime, records a failed generation, and preserves the base. Release uncertainty remains a nonfailed recoverable phase and cannot advance the base. Its candidates become retained and cannot be automatically selected again. A repair publishes a new candidate and the coordinating task may reuse unchanged exact candidates in a new generation. An interruption leaves a nonfailed recorded phase; `batch recover` resumes only that generation. A failed generation's exact clean detached worktree may be removed by an intent-first idempotent `batch retire` without deleting candidate refs or audit history. Read-only `batch metrics` derives full/tail rates, candidate wait, and executed Full cost from existing facts rather than adding policy state. Promotion is followed by idempotent worktree/ref cleanup, candidate completion, and anchor deletion.

Publication and delivery are separate projections. A candidate is delivered only when its completed batch is contained in the current base. An explicit `runtime verify --candidate` may then ask the project Adapter whether that source is effective in its runtime; DWW records the evidence but never interprets project ports, databases, browsers, authentication, or deployment semantics.

## Delegation and Hooks

A mature repository crosses the delegated seam only through a tracked declaration and machine-local approval of the exact contract and input hashes. Managed candidate batches are never imposed on a delegated workflow.

Hooks are adapters around this architecture. SessionStart may cache route context; PreToolUse may deny unsafe supported writes. The CLI and persisted Git facts remain authoritative when Hooks are absent. Hook code never seals a batch, marks a task complete, releases a slot, deletes an anchor, or repairs state.
