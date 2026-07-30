# Architecture

```text
Codex central conversation
  simple goal ──────────────────────────────→ one normal DWW task
  complex goal → plain plan → one confirmation
                    ↓
Host-neutral orchestration layer
  batch state → dependency frontier → controller-only worker dispatch
  pause / cancel / handoff → minimal evidence ledger → compact receipt
                    ↓
Lifecycle adapter boundary
  managed repository → DWW adapter → Start / exact Commit / Ready / Finish
  mature repository → explicit delegated adapter → repository-owned lifecycle
                    ↓
Git common-dir local state
  solo-ai/                 DWW slots, leases, proofs, lifecycle receipts
  solo-ai-orchestration/   batch graph, decisions, task/proof references, receipts
```

The orchestration layer is deliberately separate from `StateStore`. A batch has a goal, a maximum development concurrency of at most five, a lifecycle adapter, and minimal task summaries. It starts in `awaiting-confirmation`; no worker is schedulable before the central conversation calls `confirm`. A new central conversation can make a confirmed handoff using the exact batch id, while branches and worker files remain untouched.

The scheduler is pure: it returns planned tasks whose dependencies are completed, subject to the batch limit and the currently available DWW slots. `write_scope` is advisory and never serializes a task by itself. `exclusive_resources` is the narrow opt-in gate for migrations, lockfiles, shared contracts, or another explicitly high-risk artifact. A blocked task prevents only its descendants; unrelated ready work remains in the frontier.

Only the central Codex conversation is a supported dispatcher. It claims one writer per task, starts the worker’s ordinary lifecycle, and records its lifecycle task id. A worker may edit only its own task and may not create another worker. This is a host-workflow rule, not an operating-system security boundary.

DWW remains the Git authority. The orchestration layer neither copies slots, leases, integration locks, validation scheduling, nor semantic merge logic. Its `dww` adapter is available only when the shared route is `managed`; it references DWW task ids and Ready/Finish proof or receipt ids. Its `delegated` adapter does not guess commands, parse arbitrary instructions, or write DWW state; an existing mature repository must explicitly choose it. Repositories with an external orchestrator still fully defer.

Each completion requires existing acceptance evidence with a `kind` and `ref`. When every task is complete and evidenced, the batch writes a small receipt. The controller runs only missing targeted combination validation; there is no default full test, reviewer AI, resident service, push, PR, deployment, or release. Repeated unchanged failures stop after two reports; progressive repairs are governed by the batch’s configurable effective-change and elapsed-time budget.

The Codex hook remains a strong adapter, not the core authority. It recognizes the `dww orchestrate` command family as a trusted DWW command, while the existing route still gives mature workflows absolute priority. It cannot cover specialized execution paths that do not invoke it and never rolls back user files.
