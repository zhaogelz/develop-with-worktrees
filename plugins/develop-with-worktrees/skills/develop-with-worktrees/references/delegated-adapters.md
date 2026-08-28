# Delegated adapter contract

Use this seam only when a repository already owns a mature local lifecycle. An ordinary workflow marker still makes DWW defer. A repository becomes `delegated` only after it commits a valid contract and the user locally approves the exact contract-plus-input fingerprint.

## Tracked declaration

Create `.solo-ai/delegated.toml`:

```toml
schema_version = 1
id = "example-worktree-flow"
runtime = "python" # python, powershell, or sh
entrypoint = "scripts/dww_adapter.py"
workflow_markers = ["scripts/worktree-flow.ps1"]
tracked_inputs = [
  "scripts/dww_adapter.py",
  "scripts/worktree-flow.ps1",
]
capabilities = ["start", "status"]
max_parallel = 5
```

Every path is an exact forward-slash repository-relative path. The contract, entrypoint, workflow markers, and declared inputs must be tracked regular files inside the checkout, not links. `workflow_markers` must exactly equal the mature markers DWW detects. The entrypoint and every marker must also appear in `tracked_inputs`.

The runtime fixes the argv shape; the repository cannot inject shell arguments:

- `python`: `uv run --script <entrypoint>`
- `powershell`: `pwsh -NoProfile -File <entrypoint>`
- `sh`: `sh <entrypoint>`

DWW limits contract and input count/size so the Codex hook can re-fingerprint them on every route without unbounded work. It hashes the raw contract and every declared input. A semantic edit, comment edit, script edit, marker addition, or marker removal therefore invalidates the local approval.

Immediately before invocation, DWW captures the entrypoint bytes used by the final fingerprint check and executes a private verified snapshot rather than reopening the live entrypoint. The snapshot stays in the entrypoint's directory only for the bounded process lifetime, so Python sibling imports, inline script metadata, PowerShell `$PSScriptRoot`, shell directory lookup, and the original repository working directory keep their existing meaning; success and failure both remove it. A concurrent edit to the live entrypoint can invalidate the next route, but cannot run under the already constructed approved envelope.

Schema 1 deliberately exposes only two proven capabilities: read-only `status` and idempotent `start`. Ready, Finish, integration, recovery, abandonment, and cleanup remain native project commands. Adding names to the generic allowlist before their request, result, and interruption semantics are standardized would grant authority without a portable contract.

## Inspection and local approval

Inspection is read-only and never executes the entrypoint:

```text
dww --repo <path> --json delegated inspect
```

Review the returned adapter fields and fingerprint, then approve that exact value:

```text
dww --repo <path> --json delegated approve --fingerprint <sha256> --accept
```

Approval is stored only under the Git common directory. It is not committed. A missing, unreadable, mismatched, or stale approval makes `route` return `defer`; it never falls through to managed initialization or executes an adapter.

Revoke an exact approval without changing tracked files:

```text
dww --repo <path> --json delegated revoke --adapter-id <id> --fingerprint <sha256> --confirm
```

The adapter id and fingerprint must match the current local approval. After revocation, routing immediately returns to `defer` and the repository's native mature workflow remains authoritative.

## Invocation protocol

Invoke only a declared capability:

```text
dww --repo <path> --json delegated invoke --operation status --request '{}'
```

DWW sends one JSON object on stdin and passes no project-controlled argv:

```json
{
  "schema_version": 1,
  "adapter_id": "example-worktree-flow",
  "fingerprint": "<sha256>",
  "operation": "status",
  "request": {}
}
```

Requests are exact:

- `status` accepts only `{}`.
- `start` accepts exactly `name` and a stable `request_id`; the request id uses letters, digits, `.`, `_`, `:`, or `-` and is at most 128 characters.

The entrypoint must emit exactly one JSON object on stdout:

```json
{
  "schema_version": 1,
  "adapter_id": "example-worktree-flow",
  "fingerprint": "<same-sha256>",
  "operation": "status",
  "ok": true,
  "result": {"available_slots": 2}
}
```

The response schema, adapter id, fingerprint, and operation must match. Outcome fields are mutually exclusive and exact: success has only `result`; failure has only a non-empty `error` and is surfaced as a failed invocation, never wrapped as success.

Successful operation results are also exact:

| Operation | Result fields |
|---|---|
| `status` | `available_slots`: integer from zero through the declared `max_parallel` |
| `start` | `request_id`, `task_id`, absolute `worktree`, `slot_id`, `branch`, hexadecimal `base_head`, and boolean `request_reused` |

The orchestration layer uses the live status count rather than assuming all declared capacity is idle. Project adapters may validate richer native output internally, but must not leak native fields through this minimal boundary.

Transport is bounded: the JSON request, stdout, and stderr each have fixed byte limits; output must be strict UTF-8 and strict JSON; the caller enforces a positive deadline and terminates the owned process tree on timeout or output overflow. Adapter stderr and structured errors are redacted and truncated before they reach the caller. A timeout has unknown native side effects, so retry a mutating `start` only with the same `request_id` and use the repository's native recovery/status path if its outcome remains uncertain.

This interface does not make the repository lifecycle generic. The repository still owns its task identities, leases, candidate pool, explicit seal, validation evidence, recovery, and cleanup. DWW owns only routing, approval, the bounded JSON call, and generic orchestration bookkeeping.

For staged adoption, parity testing, rollout, and rollback, follow [Delegated adapter migration](delegated-migration.md).
