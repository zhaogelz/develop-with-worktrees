# Host handoffs reference

DWW records Git and lifecycle facts; the host's native task system schedules
work and sends messages. In Codex Desktop, DWW records the exact injected host
task ID. It never derives a source or coordinator from a title, UI count,
worktree count, or session guess.

A scheduled subagent task ID is not its Hook session ID: Codex subagent Hooks
use the parent session ID ([Codex Hooks, Common input fields](https://learn.chatgpt.com/docs/hooks)).
For delegated coding, the verified lifecycle owner performs managed writes while
the subagent may prepare patches and tests for that owner. An independent host
task needs its own verified Hook identity; `CODEX_THREAD_ID` alone is not proof
of write ownership.

## Roles

| Role | Identity | Responsibility |
|---|---|---|
| Source | native task whose Ready head waits for integration, or legacy task that published a candidate | owns the original implementation record |
| Coordinator | host task that actually froze the batch | follows that batch through integration and recovery |
| Repair assignee (legacy) | exact task that claims a prepared candidate repair | prepares and publishes the replacement candidate |

In native state, Finish ends the source's coding slice but leaves its fixed slot
owned while the Ready head waits for integration. In legacy state, candidate
publication releases the source worktree. Neither transition ends the
coordinator's delivery responsibility. Native shared integration repairs use
the managed `batch repair` entry; the candidate notification sequence below
applies to legacy candidate repair.

After a source finishes its agreed slice, the host coordinating the round checks
the remaining agreed work and active producers in the exact target lane. An
exact full batch freezes automatically. Only a genuinely ended smaller round
with no active producer supports `round-complete`; an ordinary Finish, quiet
interval, or task count does not. Follow only the returned exact task, target
branch, and batch through validation, scoped repair, promotion, and release or
a recorded failure. Do not claim delivery from Finish or take over an unrelated
batch.

For authorization carried in a host handoff, follow
[Task governance](task-governance.md): preserve the original instruction and
the exact recipient, purpose, information scope, and allowed action in the
existing context. This records prior authority; it grants no new permission.
Keep leases, credentials, and raw conversation dumps out of messages. Proceed
within clear prior authorization; ask only for an essential missing fact.

## Repair handoff sequence

1. A composition conflict, or an evidence-based validation attribution, creates
   one durable repair request.
2. The coordinator calls `host-handoff repair dispatch --request <id>` to obtain
   a stable redacted payload. This prepares a message; it does not send one.
3. The host sends that payload through its own task API and records the actual
   attempt with `host-handoff repair delivery` as `sent`, `uncertain`, or
   `failed`.
4. The assignee claims the request and runs `host-handoff repair prepare` to
   create or return its idempotent managed repair task.
5. After publication, the repair source prepares and records a return
   notification. The coordinator integrates the returned candidate.
6. The request is `resolved` only when the terminal replacement candidate is
   delivered into the current base.

Each attempt binds sender, recipient, coordinator revision, and—when
applicable—the candidate. A late receipt cannot replace a newer attempt. A
prepared payload is never reported as sent, and DWW never sends a host message
itself.

## Attribution and take-over

Only an attributed composition conflict is eligible for automatic candidate
repair. Full validation failures, promotion blocks, and unattributed conflicts
need exact evidence before the coordinator uses `repair attribute`. A host may
replace an unavailable coordinator with `batch take-over` at the stored
revision, or replace an unavailable repair source with `repair take-over` and a
reason. These actions preserve the original source identity.

Automatic repair is limited to two published generations. It cannot choose an
unresolved product, permission, migration, deletion, security, or test outcome.
See [recovery](recovery.md) for that decision boundary and
[lifecycle](lifecycle.md) for the ordinary candidate flow.
