# Delegated adapter migration

Migrate a mature repository by wrapping its current lifecycle before extracting any implementation. The native workflow remains the authority until every declared operation has parity evidence and the exact adapter fingerprint is locally approved.

## State machine

1. **Native baseline (`defer`)** — record the current workflow marker, supported operations, stable outputs, validation entrypoints, recovery behavior, and Hook definition hash. Do not create or approve a delegated contract yet.
2. **Declared but inactive (`defer`)** — add the tracked entrypoint and `.solo-ai/delegated.toml`, initially with only the read-only `status` capability. `delegated inspect` must report a valid but unapproved fingerprint; normal project instructions still invoke the native lifecycle directly.
3. **Shadow comparison (`defer`)** — compare native and adapter results using the matrix below. Expand capabilities only after their parity tests pass. Every tracked-input change creates a new fingerprint and keeps the adapter inactive.
4. **Locally approved (`delegated`)** — a user reviews `delegated inspect` and approves that exact fingerprint. The project changes only its invocation route: the adapter forwards to the existing native implementation. Do not delete or port the native implementation in this step.
5. **Rule reduction** — after the approved path handles the project's required operations, remove only project prose now supplied by DWW. Retain business or architecture redlines, canonical document routes, project-specific validation commands, lifecycle arguments, and one concise native rollback instruction.
6. **Incremental extraction** — move one proven generic behavior at a time behind the same adapter interface. Re-run parity and approve the new fingerprint after each extraction. Do not copy a mature repository's entire controller into DWW as one migration.

## Version and result contract

Transport schema version 1 is fixed by DWW. The repository adapter must echo `schema_version`, `adapter_id`, `fingerprint`, and `operation`, then return `ok` plus a result object or error. Project tests should normalize native `KEY=value` output into these minimum operation results:

| Operation | Minimum stable result |
|---|---|
| `status` | `available_slots`, slot/task/candidate state, integration-pool state; exclude volatile timestamps, process ids, and cached disk size from parity |
| `start` | stable request id, task id, worktree, slot, branch, base head, and whether the request was reused |
| `ready` | task id, candidate head/ref/revision, verification outcome, and next action |
| `finish` | task id, candidate head/ref, finish mode, source-slot release, frozen-batch count, and next action |
| `seal` / `drain` | immutable batch id and generation, candidate identities, final proof or failure, base before/after, and recovery state |
| `recover` | task id or batch generation, recovered state, whether promotion occurred, and next action |
| `abandon` / `withdraw` | exact task/candidate identity, preserved-or-removed result, and slot/candidate state |

DWW itself enforces the envelope and the `status.available_slots` bound. The repository owns the stricter per-operation schema and must test it before declaring each capability.

## Dual-run rules

Read-only operations may run sequentially against the same repository snapshot. Normalize both outputs and compare stable fields. A comparison tool must report field-level differences and exit nonzero; it must never silently prefer the adapter result.

Do not execute a mutating operation twice against one live state merely to compare it. Use one of these safe routes:

- two disposable local repositories created from the same commit and configuration, running native and adapter paths separately;
- the native workflow's documented idempotency key, only when the second call is guaranteed to return the same task rather than repeat a side effect;
- a repository-provided dry-run or plan mode that has its own contract tests.

For each mutation, compare both the normalized output and the resulting Git/local-control facts. Inject interruption at the repository's persisted phases for Ready, Finish/seal, Recover, Abandon, and cleanup. A parity pass requires the same candidate/base identities, preservation behavior, and recovery result—not merely matching exit codes.

## Activation and rollback gates

Activate only when all are true:

- `delegated inspect` is valid and lists exactly the intended workflow markers and inputs;
- the adapter entrypoint delegates to the current native implementation rather than duplicating it;
- `status` parity and every declared mutating-capability parity test pass;
- Hook definition content is unchanged unless the user separately approved a reviewed Hook change;
- the native command remains usable and the rollback command has been exercised.

Rollback is local and immediate:

```text
dww --repo <path> --json delegated revoke --adapter-id <id> --fingerprint <sha256> --confirm
```

After revocation, verify `route.action == "defer"`, then use the repository's native workflow. Contract or tracked-input drift also fails closed to `defer`; do not auto-approve the replacement fingerprint. Rollback must not delete tasks, candidates, worktrees, proofs, or native state.

## E-Farmer X rollout worksheet

Use the existing `scripts/worktree-flow.ps1` and `scripts/worktree-flow/WorktreePool.psm1` as the native authority. The first adapter should be a small Python entrypoint run by `uv`; it translates JSON to the current PowerShell actions and normalizes their `KEY=value` output. Keep the integration-batch store and candidate-pool implementation in the project.

Adopt capabilities in this order:

1. `status`: count idle slots for `available_slots`; preserve slot, request, task, candidate, active-batch, queue, and validation summaries.
2. `start`: require the existing stable `RequestId`; compare `TASK_ID`, `WORKTREE_PATH`, `SLOT_ID`, `BRANCH`, `BASE_HEAD`, and `REQUEST_REUSED`.
3. `ready` and `finish`: target the worktree from the current main controller; preserve candidate head/ref/revision, proof state, source release, batch capture, and next action.
4. `seal`, `drain`, and `recover`: preserve explicit/manual seal, immutable generation, one final verification boundary, proof reuse, protected fast-forward, and resume-only-the-captured-generation behavior.
5. `abandon` and `withdraw`: preserve exact task/candidate identity and all fail-closed content checks.

Only after those parity gates pass should E-Farmer X shorten its project instructions. Keep its identity/RMAP/tenant/module/deployment/browser/UI redlines and exact document routes; remove repeated generic worktree, anchor, long-term-document, and validation-governance prose only where the installed DWW skill now provides equivalent behavior.
