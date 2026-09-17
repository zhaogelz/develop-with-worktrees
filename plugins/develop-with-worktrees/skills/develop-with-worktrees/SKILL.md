---
name: develop-with-worktrees
description: "Use for a Git-repository change that needs safe local routing, isolated worktrees, exact commits, candidates, and local integration. It does not replace the host's task or subagent orchestration."
---

# Develop with Worktrees

DWW owns local Git routing, task identity, worktrees, anchors, exact commits,
candidates, integration, recovery, and cleanup. The host owns task scheduling
and messages. Run this skill's absolute `scripts/dww.py` path:

```text
uv run --script <DWW> --repo <repository-or-worktree> <subcommand>
```

## Before modifying

Use host route context or this read-only fallback:

```text
uv run --script <DWW> --repo <repository-or-worktree> --json route
```

- `managed`: DWW owns the local lifecycle.
- `defer`: the repository's mature workflow owns it; write no DWW state.
- `delegated`: use only the tracked, locally approved adapter.
- `disabled` or `current-task`: follow the recorded local mode.
- `ask`: ask the one first-modification question in [lifecycle](references/lifecycle.md).

For a managed task, run `start` before editing, use only its returned worktree,
keep its one task anchor current when the contract or material progress changes,
and commit only reviewed exact paths. Do not bypass failed gates, discard unknown
content, or use DWW to fetch, pull, push, deploy, rebase, squash, amend, or
rewrite history.

A confirmed complete plan needs one root anchor before its first child. The root
retains the full plan; each child retains its execution slice. Start or
`bind-root` returns it once. After continuation, context recovery, root change,
or candidate repair, refresh it before a gate when stale.

## Finish and follow delivery

Finish publishes an immutable source candidate and ends the coding round; it
does not deliver the change. Compatible candidates freeze automatically in groups
of three. A smaller tail needs a recorded `round-complete`, `user`, `deploy`, or
`dependency` cause and one-line reason. Idle time, Hooks, and task counts never
prove a round ended.

Follow each batch through integration and repair recorded, deterministic failures
within scope. Use `status --compact` for a read-only view; candidate publication,
current-base delivery, and runtime effectiveness are separate facts.

## Read one reference for the active scenario

| Scenario | Read first |
| --- | --- |
| Start, Commit, Ready, Finish, status, candidate, or tail | [Lifecycle](references/lifecycle.md) |
| Complete plan, root/child anchor, continuation, handoff, or close | [Task governance](references/task-governance.md) |
| Policy fields, defaults, compatibility, approval, or capacity | [Configuration](references/configuration.md) |
| Check selection, proof reuse, artifacts, or runtime effectiveness | [Verification reuse](references/verification-reuse.md) |
| `starting`, `held`, interrupted, failed, repair, or promotion recovery | [Recovery](references/recovery.md) |
| Project Runtime Adapter fields, cycles, or receipts | [Runtime Adapter](references/runtime-adapter.md) |
| Host task source, coordinator, or repair notifications | [Host handoffs](references/host-handoffs.md) |
| Hooks, unknown content, identity checks, cleanup, or remote boundary | [Safety](references/safety.md) |
| Mature-workflow adapter declaration and approval | [Delegated adapters](references/delegated-adapters.md) |
| Mature-workflow adoption and rollback | [Delegated migration](references/delegated-migration.md) |
| Delegated supervisor or platform process boundary | [Delegated internals](references/delegated-internals.md) |

Read only the active reference and a directly linked prerequisite. Do not replace
a confirmed root plan with a summary or create a second task scheduler.
