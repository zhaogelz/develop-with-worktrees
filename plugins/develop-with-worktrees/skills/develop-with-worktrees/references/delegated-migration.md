# Delegated adapter migration

Migrate a mature repository by wrapping its current lifecycle before extracting any implementation. The native workflow remains the authority until every declared operation has parity evidence and the exact adapter fingerprint is locally approved.

## State machine

1. **Native baseline (`defer`)** — record the current workflow marker, supported operations, stable outputs, validation entrypoints, recovery behavior, and Hook definition hash. Do not create or approve a delegated contract yet.
2. **Declared but inactive (`defer`)** — add the tracked entrypoint and `.solo-ai/delegated.toml`, initially with only the read-only `status` capability. `delegated inspect` must report a valid but unapproved fingerprint; normal project instructions still invoke the native lifecycle directly.
3. **Shadow comparison (`defer`)** — compare native and adapter results using the matrix below. Add idempotent `start` only after its parity and interruption tests pass. Every tracked-input change creates a new fingerprint and keeps the adapter inactive.
4. **Locally approved (`delegated`)** — a user reviews `delegated inspect` and approves that exact fingerprint. The project changes only its invocation route: the adapter forwards to the existing native implementation. Do not delete or port the native implementation in this step.
5. **Rule reduction** — after the approved path handles the project's required operations, remove only project prose now supplied by DWW. Retain business or architecture redlines, canonical document routes, project-specific validation commands, lifecycle arguments, and one concise native rollback instruction.
6. **Incremental extraction** — move one proven generic behavior at a time behind the same adapter interface. Re-run parity and approve the new fingerprint after each extraction. Do not copy a mature repository's entire controller into DWW as one migration.

## Version and result contract

Transport schema version 1 is fixed by DWW. The repository adapter must echo `schema_version`, `adapter_id`, `fingerprint`, and `operation`, then return `ok` plus exactly one result object or error. Project tests should normalize native output into these exact operation results:

| Operation | Minimum stable result |
|---|---|
| `status` | integer `available_slots`, bounded by `max_parallel` |
| `start` | stable `request_id`, `task_id`, absolute `worktree`, `slot_id`, `branch`, hexadecimal `base_head`, and boolean `request_reused` |

DWW enforces these request and result shapes. The repository owns stricter validation of the native controller facts before returning the minimal normalized result. Native lifecycle operations not listed here are not schema-1 capabilities and must stay behind the project's own reviewed commands.

The adapter must separate approved executable input from the live operation target. Load every adapter helper, controller, module, and configuration file from `DWW_VERIFIED_INPUT_ROOT` (or an entrypoint-relative path inside it). Pass `DWW_REPOSITORY_ROOT` to the native controller only as its Git/state repository, and expect the process working directory to equal that same live root. An adapter that reopens tracked code or configuration under the live repository recreates a time-of-check/time-of-use gap and must stay inactive at `defer`.

## Dual-run rules

Read-only operations may run sequentially against the same repository snapshot. Normalize both outputs and compare stable fields. A comparison tool must report field-level differences and exit nonzero; it must never silently prefer the adapter result.

Do not execute a mutating operation twice against one live state merely to compare it. Use one of these safe routes:

- two disposable local repositories created from the same commit and configuration, running native and adapter paths separately;
- the native workflow's documented idempotency key, only when the second call is guaranteed to return the same task rather than repeat a side effect;
- a repository-provided dry-run or plan mode that has its own contract tests.

For `start`, compare both the normalized output and the resulting Git/local-control facts. Inject interruption before and after the native task is persisted, retry only with the same stable request id, and confirm the repository returns the same task rather than allocating twice. A parity pass requires the same task/base identities and recovery result—not merely matching exit codes.

## Activation and rollback gates

Activate only when all are true:

- `delegated inspect` is valid and lists exactly the intended workflow markers and inputs;
- the adapter entrypoint delegates to the current native implementation rather than duplicating it;
- every declared helper, controller, module, and configuration input is loaded from `DWW_VERIFIED_INPUT_ROOT`, while `DWW_REPOSITORY_ROOT` is used only as the native Git/state target;
- `status` parity and, when declared, idempotent `start` parity and interruption tests pass;
- Hook definition content is unchanged unless the user separately approved a reviewed Hook change;
- the native command remains usable and the rollback command has been exercised.

Rollback is local and immediate:

```text
dww --repo <path> --json delegated revoke --adapter-id <id> --fingerprint <sha256> --confirm
```

After revocation, verify `route.action == "defer"`, then use the repository's native workflow. Contract or tracked-input drift also fails closed to `defer`; do not auto-approve the replacement fingerprint. Rollback must not delete tasks, candidates, worktrees, proofs, or native state.
