---
name: develop-with-worktrees
description: "Use for a Git-repository change that needs safe local routing, isolated worktrees, exact commits, candidates, and local integration. It does not replace the host's task or subagent orchestration."
---

# Develop with Worktrees

DWW owns the local Git lifecycle: route, task identity, isolated worktree, task/root anchors, exact-path commits, source candidates, local integration, recovery, and cleanup. The host owns native task/subagent decomposition, dependencies, and scheduling. Do not create a second task DAG or a persistent coordinator. The legacy `orchestrate` interface is drain-only compatibility and must not create new orchestration state.

Use DWW to make an already-defined change safer and easier to continue, not to add process. Keep the simplest mechanism that preserves user work, identity, and required verification.

Set `DWW` to this skill's absolute `scripts/dww.py`, and use it only through `uv`:

```text
uv run --script <DWW> --repo <repository-or-worktree> <subcommand>
```

## Route and scope

Before a modifying task, use host-provided route context or run one read-only fallback:

```text
uv run --script <DWW> --repo <repository-or-worktree> --json route
```

- `managed`: use the lifecycle below.
- `defer`: follow the repository's mature workflow; do not mutate DWW state.
- `delegated`: use only its tracked, locally approved adapter.
- `disabled` or `current-task`: follow that recorded local mode.
- `ask`: ask the skill's single first-modification question from [lifecycle.md](references/lifecycle.md).

Read-only analysis never claims a slot. Hooks are optional hardening: `hooks/hooks.json` and Hook/idle events never establish task truth or autonomous delivery.

## Normal managed task

When the user has confirmed a complete plan, create one root anchor before its first child task. It keeps the complete plan and overall acceptance; child anchors keep only the execution slice. Start the task with reviewed purpose, target, scope, acceptance, and the root id when present:

```text
uv run --script <DWW> --repo <repo> start \
  --name <purpose> --target <implementation-target> \
  --scope <boundary> --acceptance <criteria> \
  --root-anchor <root-id>
```

Work only in the returned worktree and keep its one local task anchor current when the execution contract, progress, or a material blocker changes. After continuation, model/context recovery, candidate repair, or a root-plan change, run `anchor refresh-root` once before the next modification. A stale bound root blocks Commit, real Ready, and Finish publication until refreshed; it is not a human acknowledgement gate.

1. Inspect only the anchor, changed behavior, its direct callers, configuration, and focused tests.
2. Commit reviewed paths one by one with `commit --path`; never broad-stage.
3. Run a useful development check early. Development, Ready, Full, and explicit Stress evidence are distinct; do not turn Finish into a duplicate project-test gate.
4. Use Ready only when useful or required by legacy policy, then Finish with the same task and private lease.
5. Follow the actual candidate/batch result through integration. Candidate publication is not delivery.

Use `root-anchor accept` only after current-plan acceptance evidence is checked. `root-anchor close` requires every child terminal and every candidate lineage integrated or explicitly withdrawn. Never copy a full root plan into child anchors.

## Candidate-first delivery

New managed repositories use `integration.mode = "batched"`, `seal_policy = "auto_full"`, `candidate_capacity = 10`, and a three-candidate full batch. Finish publishes one immutable source candidate after its task-level safety checks and releases its worktree; the base does not move until an integration batch succeeds.

- The third compatible pending candidate freezes the exact full lane automatically.
- A smaller tail requires an explicit cause and one-line reason. No age, idle signal, Hook event, or task count means a round is complete.
- `batch reconcile --force --cause round-complete|user|deploy|dependency --reason <basis>` is the ordinary coordinator/recovery interface; `batch seal` remains the exact-list compatibility interface.
- `finish --cause <user|deploy|dependency|round-complete> --reason <basis>` is a convenience for a batched source task. Its first intent is persisted with publication, and it freezes only that candidate's exact pending lane after publication/recovery. With no cause and reason, Finish keeps its existing source-publication behavior. It never starts a second scheduler, bypasses Full, or freezes an unrelated active lane.

An integration or release failure preserves the exact candidate and base for deterministic recovery. Do not retry an unchanged deterministic failure blindly. Do not fetch, pull, push, rebase, squash, amend, rewrite history, deploy, or remove unknown content.

## Fast, directed status

Use ordinary `status` for a short human summary. Legacy `--json status` remains compatibility output. For AI-oriented current facts without historical payload or reconciliation side effects, use:

```text
status --compact
status --compact --task <task-id>
status --compact --root <root-id>
status --compact --batch <batch-id>
status --compact --history
candidate status --compact
```

Compact views are read-only request snapshots: they do not reconcile operation receipts or write state. Ask for history only when investigating it. Error JSON may add `error_code`, `context`, and `next_action`; callers must continue accepting the legacy fields.

`batch metrics` is read-only. Its publication-to-delivery duration is only observed for integrated candidates; coverage counts state which timestamps or Full proofs are absent, so missing data is not mistaken for a performance result.

## Read a reference only when needed

- Policy schema, approval, proof reuse, or verification: [verification-reuse.md](references/verification-reuse.md) and [configuration.md](references/configuration.md).
- Tail reconciliation, recovery, runtime adapters, in-place compatibility, or candidate repair: [lifecycle.md](references/lifecycle.md).
- Root plans, task handoff, or host coordination boundary: [task-governance.md](references/task-governance.md).
- Cleanup, path identity, hooks, or protected content: [safety.md](references/safety.md).
- Mature-workflow migration: [delegated-migration.md](references/delegated-migration.md).

The machine-global weighted FIFO validates expensive work. Complete pure proofs may be reused only when their declared inputs, tool/environment facts, and policy identity still match; mutable runtime effects and required artifacts never reuse an old success.
