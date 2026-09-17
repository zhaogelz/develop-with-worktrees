# Delegated adapter migration

Migrate a mature repository by wrapping its existing lifecycle before extracting
generic behavior. The native workflow remains authoritative until every declared
operation has parity evidence and the exact adapter fingerprint is locally
approved.

## Staged migration

1. **Native baseline (`defer`).** Record current workflow markers, supported
   operations, stable outputs, validation entry points, recovery behavior, and
   Hook definition hash. Do not declare or approve a DWW contract yet.
2. **Declared but inactive (`defer`).** Commit the entrypoint and
   `.solo-ai/delegated.toml` with only read-only `status`. Inspection must report
   a valid but unapproved fingerprint; normal project instructions stay native.
3. **Shadow comparison (`defer`).** Compare normalized native and adapter results.
   Add idempotent `start` only after parity and interruption tests pass. Any
   tracked-input change creates a new inactive fingerprint.
4. **Locally approved (`delegated`).** A user approves the exact fingerprint.
   Change only the invocation route: the adapter forwards to the existing native
   implementation. Do not delete or port the native lifecycle yet.
5. **Rule reduction.** Remove only project prose now supplied by DWW. Retain
   business and architecture redlines, canonical document routes, project
   validation commands, lifecycle arguments, and one concise native rollback.
6. **Incremental extraction.** Move one proven generic behavior at a time behind
   the same interface. Re-run parity and approve the new fingerprint after every
   extraction; never copy a mature controller wholesale into DWW.

## Compare safely

Read-only operations may run sequentially against the same repository snapshot.
Normalize both outputs and report field-level differences; a comparison must fail
rather than silently prefer the adapter result.

Do not execute a mutating operation twice against one live state merely to
compare it. Use two disposable repositories from the same commit, the native
workflow's documented idempotency key when it returns the same task, or a
repository-provided dry-run/plan mode with its own contract tests.

For `start`, compare the exact normalized result described in
[delegated adapters](delegated-adapters.md), plus resulting Git and local-control
facts. Inject interruption before and after native task persistence, retry with
the same stable request ID, and prove it returns the same task/base identity.
Matching exit codes alone are not parity evidence.

## Activate and roll back

Activate only when all of these hold:

- `delegated inspect` lists exactly the intended markers and inputs;
- the adapter delegates to the current native implementation;
- code and configuration load from `DWW_VERIFIED_INPUT_ROOT` while
  `DWW_REPOSITORY_ROOT` is only the native Git/state target;
- `status` parity and declared idempotent `start` parity/interruption tests pass;
- the Hook definition is unchanged unless separately reviewed;
- the native command and rollback route both work.

Rollback is local and immediate:

```text
dww --repo <path> --json delegated revoke --adapter-id <id> --fingerprint <sha256> --confirm
```

After revocation verify `route.action == "defer"` and use the repository's native
workflow. Contract or input drift also fails closed to `defer`. Rollback must not
delete tasks, candidates, worktrees, proofs, or native state.

The delegated contract is intentionally narrow. Preserve the native lifecycle's
ownership of candidate pooling, sealing, validation, recovery, and cleanup
unless those operations gain their own proven portable contract.
