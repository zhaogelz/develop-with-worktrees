---
name: develop-with-worktrees
description: "Use for a Git-repository change that needs safe local routing, isolated worktrees, exact commits, and local integration. It does not replace the host's task or subagent orchestration."
---

# Develop with Worktrees

DWW owns local Git routing, task identity, worktrees, anchors, exact commits,
integration, recovery, and cleanup. The host owns scheduling and messages;
the project owns check selection and runtime resources. Run the installed
skill's absolute `scripts/dww.py` path:

```text
uv run --script <DWW> --repo <repository-or-worktree> <subcommand>
```

## Ordinary work

1. Route before editing. Use host route context or `--json route`.
   For `managed`, run `start` and edit only its returned worktree.
   `defer` uses the project's workflow without DWW state; `delegated` uses its
   approved adapter. Other modes and first-use choices follow
   [Lifecycle](references/lifecycle.md).
2. Keep the task's one anchor current when scope or material progress changes.
   Run focused project checks and `commit` only reviewed exact paths.
3. For native tasks (state schema 12), use `finish` directly after committing.
   It performs Ready, releases task runtime, and follows an eligible batch
   through combined Full, promotion, and slot return. Explicit `ready` is only
   needed when a separately frozen head is useful. See
   [Native delivery](references/native-delivery.md) for exact arguments.

A full compatible group freezes automatically. A smaller tail needs a recorded
`round-complete`, `user`, `deploy`, or `dependency` cause and one-line reason.
Verify that the agreed round and its target lane have no active producer before
using `round-complete`. Ordinary completion, idle time, task counts, and Hooks
do not establish that cause. Waiting is a valid result, not delivery.

Repeat native `finish` to continue its already assigned batch. A completed task
returns its verified historical receipt without touching a reused slot.
If a check failed, inspect and repair first; repeating unchanged failed input
does not run another long validation. Schema-11 repositories keep their recorded
legacy flow until guarded migration; read [Lifecycle](references/lifecycle.md).

## Continue within the authorization

Within the user's authorized scope, update context, inspect related code and old
assertions, collect independent failures, repair them together, and continue
focused checks and eligible delivery. Do not ask again for those ordinary steps.
Technical `--accept`, `--confirm`, and `--force` arguments verify an exact
operation; they are not a new user decision.

A failed prerequisite blocks its dependents. Runtime, timeout, cleanup, identity,
or input errors stop the attempt. A repaired candidate needs a new attempt;
matching pure profile evidence may be reused under
[Verification reuse](references/verification-reuse.md). A failed gate must be
fixed, not waived. Required outputs and runtime effects need fresh evidence.

Preserve unknown content and other tasks' work. Continue unrelated read-only or
isolated work while resolving a blocked operation. Use the managed `batch repair`
entry for a frozen integration repair; never edit its workspace directly.
DWW performs no fetch, pull, push, deploy, rebase, squash, amend, or history rewrite.

## Context and host boundaries

A confirmed complete plan has one root anchor before its first child; each child
keeps only its execution slice. Read
[Task governance](references/task-governance.md) for creation, host association,
continuation, acceptance, and close. Refresh stale root context after actual
continuation, recovery, root change, or candidate repair; do not add a duplicate
refresh after Start.

When creating a saved-project task, honor the user's Local or Worktree choice
before the host call, then route that checkout. Start stays light; project runtime
preparation runs on demand. The skill does not create a second task scheduler.

Use `status --compact` for a read-only view. Report active work, waiting, failed
checks, local delivery, installation, and host loading as separate observed facts.
Only local promotion and resource return establish delivery. Ask for Hook trust
only when the host reports a first-install or changed-definition review.
For sandbox Git metadata or dubious-ownership errors, retry the same command
through reviewed escalation; keep the ownership and filesystem protections.

## Read details only for the current case

| Case | Reference |
| --- | --- |
| Native Finish, tail, batch repair, or migration | [Native delivery](references/native-delivery.md) |
| Held, interrupted, failed, or release recovery | [Recovery](references/recovery.md) |
| Policy and capacity | [Configuration](references/configuration.md) |
| Hooks, unknown content, cleanup, or remote boundaries | [Safety](references/safety.md) |
| Runtime preparation and receipts | [Runtime Adapter](references/runtime-adapter.md) |
| Host association and repair messages | [Host handoffs](references/host-handoffs.md) |
| Mature workflow adoption | [Delegated adapters](references/delegated-adapters.md), then its directly needed migration or internals reference |
