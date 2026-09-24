---
name: develop-with-worktrees
description: "Use for a Git-repository change that needs safe local routing, isolated worktrees, exact commits, and local integration. It does not replace the host's task or subagent orchestration."
---

# Develop with Worktrees

DWW owns local Git routing, task identity, worktrees, anchors, exact commits,
task heads, integration, recovery, and cleanup. The host owns task scheduling
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
retains the full plan; each child retains its execution slice. With an exact host
task identity, create the root with its stable request ID and derived acceptance
index; later Start inherits that one root automatically. Start or `bind-root`
returns it once. After continuation, context recovery, root change, or candidate
repair, refresh it before a gate when stale. Read [Task governance](references/task-governance.md)
before using root, host association, reindex, or close commands.

When a Codex workspace sandbox reports Git metadata access or Git's "dubious
ownership" error, keep the ownership check and sandbox protection. Re-run the
same DWW lifecycle command through the host's reviewed escalation; do not add a
global `safe.directory` exception or loosen filesystem ACLs. See the Windows
and Codex sandbox note in [Lifecycle](references/lifecycle.md).

## Finish and follow delivery

For state schema 12, Ready freezes the exact task branch, slot generation,
worktree, and source commit. Finish records it as waiting for integration. The
slot stays owned until its task commit reaches the target branch and runtime
release succeeds. The ordinary Git branch stays attached throughout.

A full compatible group freezes automatically. A smaller tail needs a recorded
`round-complete`, `user`, `deploy`, or `dependency` cause and one-line
reason. Idle time, Hooks, and task counts are not causes. Use
`round-complete` only when that lane has no active producer, and `user` only
for an explicit request to integrate now.

One batch per target branch merges the original task commits, runs the selected
combined checks on the exact merged head, and promotes only if the target
still matches its frozen base. Follow the batch through recovery and release;
Finish alone is not delivery. Shared integration repairs use the owned
`batch repair` entry and a new validation attempt. Do not edit a frozen task
or integration worktree outside its managed entry.

For a new managed repository, Start performs light Git and identity checks.
Project Runtime Adapter preparation runs at first use, such as
`runtime prepare` or `dev start`. See [native delivery](references/native-delivery.md)
for commands, migration, and recovery boundaries.

Schema-11 repositories retain their candidate-first policy and legacy
recovery until `migration preview` is clear and `migration enable` succeeds.
The old flow is documented in [Lifecycle](references/lifecycle.md); do not
reinterpret a legacy candidate as a native task head.

When a focused formatting check fails, ask the project's formatter for a diff
against the changed paths, apply it, and rerun that focused check.

Use `status --compact` for an exact read-only view. The host owns task
splitting, waiting, and messages, and follows every frozen batch through local
delivery or a recorded failure.

## User-facing status

Report in the user's language. State whether work is active, Ready, waiting for
local integration, delivered to the local base, installed, or verified by the
current host. Finish and a frozen source head are not delivery. If installation
or host verification is unconfirmed, say so.

Ask for Hook trust only when the host explicitly reports a first-install or
changed-definition review. A version change, one denial, or an identity error
does not itself justify repeated trust or a new session. Do not expose leases,
raw state records, or logs unless the user asks for diagnostic detail.

Describe waiting or a failed batch from its recorded base, owner, attempt, and
next action. Do not infer a tail cause from counts, UI state, or quiet time.

## Read one reference for the active scenario

| Scenario | Read first |
| --- | --- |
| New fixed-slot Start, Ready, Finish, batch repair, or migration | [Native delivery](references/native-delivery.md) |
| Legacy candidate, status, or tail recovery | [Lifecycle](references/lifecycle.md) |
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
