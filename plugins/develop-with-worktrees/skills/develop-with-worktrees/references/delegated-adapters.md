# Delegated adapter contract

Use delegation only when a repository already owns a mature local lifecycle. A
normal workflow marker still makes DWW defer. The repository becomes
§delegated§ only after it commits a valid contract and the user locally approves
the exact contract-plus-input fingerprint.

## Tracked declaration

Create §.solo-ai/delegated.toml§:

§§§toml
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
§§§

Every path is an exact forward-slash repository-relative tracked regular file,
not a link. §workflow_markers§ exactly match the mature markers DWW detects, and
the entrypoint and every marker also appear in §tracked_inputs§.

The runtime fixes the command shape; project configuration cannot inject shell
arguments:

| Runtime | Invoked form |
|---|---|
| §python§ | §uv run --script <entrypoint>§ |
| §powershell§ | §pwsh -NoProfile -File <entrypoint>§ |
| §sh§ | §sh <entrypoint>§ |

DWW bounds declaration and input count/size so routing can fingerprint them
without unbounded work. Any contract, comment, input, or marker change produces
a new fingerprint and returns routing to §defer§ until approved again.

## Verified input and live target

Immediately before invocation DWW freezes the approved contract and declared
input bytes into a private repository-external closure. Adapter code, helpers,
controllers, and configuration load from §DWW_VERIFIED_INPUT_ROOT§. The process
working directory and §DWW_REPOSITORY_ROOT§ remain the original live repository,
used only as the Git/state target.

The closure includes the declaration and exact tracked inputs, preserves relative
layout, disables Python bytecode writes, and never appears in repository status.
A later edit to the live checkout can invalidate the next route but cannot change
the bytes already executing. Full process-boundary mechanics are in
[delegated internals](delegated-internals.md).

## Capabilities and transport

Schema 1 deliberately exposes only read-only §status§ and idempotent §start§.
Ready, Finish, integration, recovery, abandonment, and cleanup remain native
project commands. Do not expand this allowlist without standardized request,
result, and interruption semantics.

Invoke only a declared operation:

§§§text
dww --repo <path> --json delegated invoke --operation status --request '{}'
§§§

DWW sends one JSON object on stdin:

§§§json
{
  "schema_version": 1,
  "adapter_id": "example-worktree-flow",
  "fingerprint": "<sha256>",
  "operation": "status",
  "request": {}
}
§§§

Requests are exact: §status§ accepts only §{}§; §start§ accepts exactly §name§ and
a stable §request_id§ of at most 128 permitted characters. The adapter returns
one strict UTF-8 JSON object with matching schema, adapter ID, fingerprint, and
operation. Success has only §result§; failure has only a non-empty §error§.

| Operation | Exact successful result |
|---|---|
| §status§ | §available_slots§ integer from 0 through §max_parallel§ |
| §start§ | §request_id§, §task_id§, absolute §worktree§, §slot_id§, §branch§, hexadecimal §base_head§, and boolean §request_reused§ |

Output, request, payload, timeout, and errors are bounded. A timeout has unknown
native effects: retry a mutating start only with the same request ID and use the
repository's native recovery/status path if the outcome remains uncertain.

## Inspect, approve, revoke

Inspection is read-only:

§§§text
dww --repo <path> --json delegated inspect
§§§

Review the returned fields and exact fingerprint, then approve it locally:

§§§text
dww --repo <path> --json delegated approve --fingerprint <sha256> --accept
§§§

Approval is stored under the Git common directory and is not committed. Missing,
unreadable, mismatched, or stale approval routes to §defer§; it never falls
through to managed initialization or executes the adapter. Revoke an exact
current approval with:

§§§text
dww --repo <path> --json delegated revoke --adapter-id <id> --fingerprint <sha256> --confirm
§§§

Revocation returns routing to the mature repository workflow and does not delete
its tasks, candidates, worktrees, proofs, or state.

## Boundary

Delegation wraps, rather than replaces, the repository's lifecycle. DWW's managed
candidate batches are never imposed through delegation. Use
[delegated migration](delegated-migration.md) for staged adoption and rollback.
