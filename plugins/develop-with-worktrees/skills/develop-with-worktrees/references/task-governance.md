# Task context and durable documentation

Use a short-lived task anchor to preserve the active execution contract without
turning every change into a permanent plan document. This reference explains when
anchors exist, what they hold, and how a confirmed objective survives a
continuation.

## Lifecycle ownership

Route first. A DWW-managed task gets its anchor from DWW. Under `defer`, create
no DWW task, anchor, candidate, or batch state; follow the repository's own
context rule after it authorizes a writable workspace. Read-only investigation
does not create an anchor.

A managed anchor lives at
`<git-common-dir>/solo-ai/task-anchors/<task-id>.md`. It is a regular local UTF-8
file, available from the base checkout and task worktree, and is never staged or
committed.

## Child anchors

Managed Start creates one child anchor before returning the writable worktree.
It records only the current execution slice:

- original user objective;
- implementation target and repositories;
- reference baseline or candidate identity;
- scope boundary and explicit exclusions;
- acceptance criteria and validation route;
- current progress, decisions, and unresolved blockers.

Update it when those facts materially change. A ready task may change only its
complete `Current progress` block. Use the recorded lease and an input file under
the recorded task or base worktree for `anchor update`; the digest prevents a
stale editor from overwriting newer content. Do not copy chat transcripts, hidden
reasoning, credentials, leases, or unrelated history into an anchor.

## Confirmed-objective root anchors

Create exactly one root anchor before the first related child when the user has
explicitly confirmed a complete implementation plan, asks to set it as the
objective, or asks to proceed with it. Do not create a root for a discussion,
read-only investigation, or an ordinary small change without a confirmed plan.

The root lives at `<git-common-dir>/solo-ai/root-anchors/<root-id>.md`. Its
creation receives the complete UTF-8 plan through `--plan-file`, a concise source,
and a stable request ID. It retains the complete plan, immutable purpose and
baseline, target/scope/acceptance, monotonically versioned explicit user
amendments, current progress, and overall acceptance result. The source input is
not another canonical document after creation.

A child starts with `--root-anchor <root-id>`. A cross-repository child must also
supply the exact external root file; DWW records that one non-linked locator and
does not discover repositories or copy the root. Root anchors do not define
candidate membership, task dependencies, scheduler ownership, or batch scope.

Only an explicit user plan change may replace the effective plan through
`root-anchor amend` or append the user's words as a change. Technical choices that
do not alter purpose, scope, or acceptance stay with the child. A plan change
preserves the prior version and resets overall acceptance.

## Continuation and close

Start or `anchor bind-root` returns the complete current root once. On actual
continuation, handoff, model/context recovery, root-plan change, or candidate
repair, read the child anchor and run `anchor refresh-root` once. That command
returns the current child and complete root, then records the reviewed version.

The record is not proof of understanding and does not ask the user again. Commit,
actual Ready, and candidate-publishing Finish require the recorded root version
to equal the current root version. If it is stale, refresh and retry without
discarding work. Do not add a redundant refresh immediately after Start or a
successful bind.

`root-anchor close` requires every linked child to be terminal. For a
candidate-published child, its exact supersession lineage must be integrated or
explicitly withdrawn; publication alone is not delivery. A structured root also
requires `root-anchor accept` to record accepted or cancelled evidence for its
current plan version before closure.

## Precedence and durable documents

Direct user instructions and repository hard rules are authoritative. Within
them, the root is authoritative for confirmed objective, scope, and acceptance;
the child anchor is authoritative for current execution facts. Code, tests,
schema, and configuration remain authoritative evidence of implemented behavior.

At acceptance, update long-lived documentation only when the change alters a
lasting product rule, public contract, data model, permission boundary,
architecture boundary, stable responsibility, or UI contract. Update the one
existing canonical document for that topic. Ordinary fixes, debugging notes,
tests, receipts, and temporary plans stay in their natural execution evidence.
Do not create generic `CONTEXT.md`, `requirements.md`, `plan.md`, task ledgers, or
an ADR directory unless the repository has named one as canonical.

In candidate-first mode, a child anchor remains after source publication until
the candidate is delivered or withdrawn. Failed integration, interrupted
release, and uncertain state preserve it for recovery. See [recovery](recovery.md)
for the actual repair decision tree.
