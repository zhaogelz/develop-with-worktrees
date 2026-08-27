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
capabilities = ["start", "status", "ready", "finish", "recover", "abandon"]
max_parallel = 5
```

Every path is an exact forward-slash repository-relative path. The contract, entrypoint, workflow markers, and declared inputs must be tracked regular files inside the checkout, not links. `workflow_markers` must exactly equal the mature markers DWW detects. The entrypoint and every marker must also appear in `tracked_inputs`.

The runtime fixes the argv shape; the repository cannot inject shell arguments:

- `python`: `uv run --script <entrypoint>`
- `powershell`: `pwsh -NoProfile -File <entrypoint>`
- `sh`: `sh <entrypoint>`

DWW limits contract and input count/size so the Codex hook can re-fingerprint them on every route without unbounded work. It hashes the raw contract and every declared input. A semantic edit, comment edit, script edit, marker addition, or marker removal therefore invalidates the local approval.

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

The response schema, adapter id, fingerprint, and operation must match. A successful `status` result must include integer `available_slots` between zero and the declared `max_parallel`; the orchestration layer uses this live value rather than assuming all declared capacity is idle. Other result fields are owned by the repository adapter. Failed responses use `ok = false` and a non-empty `error`.

This interface does not make the repository lifecycle generic. The repository still owns its task identities, leases, candidate pool, explicit seal, validation evidence, recovery, and cleanup. DWW owns only routing, approval, the bounded JSON call, and generic orchestration bookkeeping.

For staged adoption, parity testing, rollout, and rollback, follow [Delegated adapter migration](delegated-migration.md).
