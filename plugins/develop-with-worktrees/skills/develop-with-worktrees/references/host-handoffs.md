# Host handoffs reference

DWW records Git and lifecycle facts; the host's native task system schedules
work and sends messages. In Codex Desktop, DWW records the exact injected host
task ID. It never derives a source or coordinator from a title, UI count,
worktree count, or session guess.

## Roles

| Role | Identity | Responsibility |
|---|---|---|
| Source | task that published the candidate | owns the original implementation record |
| Coordinator | host task that actually froze the batch | follows that batch through integration and recovery |
| Repair assignee | exact task that claims a prepared repair | prepares and publishes the replacement candidate |

Publishing a candidate may end the source task's coding round. It does not end
the coordinator's delivery responsibility.

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
