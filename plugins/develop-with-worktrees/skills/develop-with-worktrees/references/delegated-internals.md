# Delegated adapter internals

This reference is for maintainers changing DWW's delegated-adapter supervisor
or platform cleanup implementation. An adapter author normally needs only
[delegated adapters](delegated-adapters.md) and, when adopting a mature
workflow, [delegated migration](delegated-migration.md).

## Immutable execution input

Before invocation DWW reads the approved contract and every declared tracked
input, rechecks their fingerprint, and creates one private repository-external
closure with the same relative layout. The adapter opens code and configuration
from `DWW_VERIFIED_INPUT_ROOT`; `DWW_REPOSITORY_ROOT` is only the live Git/state
target and process working directory. This prevents a verified live checkout from
changing the bytes executed after approval.

The closure is removed only after its owned process boundary is confirmed empty.
Path replacement, unexpected closure content, or an unconfirmed process boundary
preserves the closure for diagnosis. The closure is a reliable boundary for
adapters that remain inside it, not an operating-system sandbox.

## POSIX process boundary

The caller owns a new-session supervisor process group before accepting an
adapter result. The supervisor uses bounded status, gate, payload, control, and
result channels; the parent accepts one verified GO transition before the
supervisor forks the adapter. It does not kill a bare numeric PID or PGID after
identity is uncertain or released.

Termination has explicit not-attempted, indeterminate, and delivered states.
The private control capability directs the still-owned supervisor to end its
current group, then the parent waits for the direct child and checks absence.
Missing status, timeout, output failure, or interruption never becomes guessed
identity. Any unconfirmed cleanup retains the verified closure.

## Windows process boundary

Before process creation DWW creates a `KILL_ON_JOB_CLOSE` Job Object. A private
`CreateProcessW` path supplies the exact inherited handles and Job list through
`STARTUPINFOEX` while the child is suspended, so membership begins with the
successful kernel creation. Process, thread, Job, stream, and attribute-list
handles have one owner; uncertain close, query, resume, or termination cannot
fall back to PID enumeration or reuse a numeric HANDLE.

DWW accepts a result only after the Job has no active processes. Incompatible
nested-Job policy and an unconfirmed boundary fail closed and preserve the
closure. `CREATE_BREAKAWAY_FROM_JOB` is not used.

## Limits and maintenance evidence

The transport bounds request, launch payload, output, and deadline; stdout is
strict UTF-8 JSON and errors are redacted and truncated. A mutating `start` with
uncertain native side effects may retry only with the same request ID and the
repository's native recovery path.

An adapter that deliberately creates a new POSIX session, uses an external
broker or privilege boundary, bypasses a Hook, or otherwise escapes the inherited
process boundary is outside this portable contract and must not be approved.
Relevant implementation lives in `scripts/solo_ai/delegated.py`; platform and
interruption regressions live with the delegated tests.
