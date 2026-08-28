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

Immediately before invocation, DWW retains the raw contract and every tracked-input byte read by the final fingerprint check, then creates one private repository-external execution closure with the same repository-relative layout. The runtime opens the entrypoint from that closure, so Python sibling imports and PEP 723 metadata, PowerShell `$PSScriptRoot` modules, shell directory helpers, workflow controllers, and declared configuration all resolve to the approved bytes instead of reopening the live checkout. The process working directory remains the original repository.

DWW sets two authoritative environment variables for the bounded process lifetime:

- `DWW_VERIFIED_INPUT_ROOT` is the immutable approved-input root. Adapter code, controllers, modules, and configuration must load executable or policy input only from this root (or from entrypoint-relative paths that stay inside it).
- `DWW_REPOSITORY_ROOT` is the original live repository root. Use it only as the native workflow's Git/state target, not as a code or configuration source. It is also the process working directory.

The closure contains `.solo-ai/delegated.toml` plus exactly the declared tracked inputs, disables Python bytecode writes, never appears in repository Git status, and is removed after success, failure, or confirmed process-boundary termination. If any closure file, directory, or path identity is replaced or unexpected content appears, cleanup fails closed and preserves the changed path for diagnosis. If the caller cannot confirm that the owned POSIX process group or Windows Job Object is empty, it preserves the entire closure and reports its path because a surviving descendant may still be using those approved bytes. A concurrent edit to any live contract or tracked input can invalidate the next route, but cannot alter the already constructed approved closure.

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

Transport is bounded: the JSON request, launch payload, stdout, and stderr each have fixed byte limits; output must be strict UTF-8 and strict JSON; the caller enforces a positive deadline. Success, nonzero adapter failure, timeout, output overflow, bounded-output read failure, and every other `BaseException` must all leave the owned process boundary empty before a result is accepted or the verified closure is cleaned. On POSIX that boundary is the whole new process group, including descendants that outlive a root which returned normally or accepted `SIGTERM`; a remaining group is terminated and confirmed even after root exit.

Before POSIX process creation the caller opens CLOEXEC status, gate, termination-control, and result pipes plus a private bounded launch-payload descriptor. Only the exact launcher child ends are passed with `pass_fds`. The fixed launcher starts as a new-session process-group leader under an absolute Python executable with `-I -S`, outside the repository cwd and with only fixed locale variables; it does not inherit `PATH`, Python, dynamic-loader, locale-module, or other platform runtime configuration. It installs `SIGTERM` ignore before its bounded status frame, closes that endpoint, and waits without spawning. The caller requires `pid == pgid == process.pid > 0` and one unique GO byte. Only after GO does the supervisor read the original argv/cwd/environment payload, fork an adapter child, restore the child's default `SIGTERM`, close every control/result descriptor in that child, and `execvpe` the adapter. The supervisor alone waits the adapter and reports its return code through one bounded, single-writer result frame, then remains alive; adapter success, nonzero exit, exec failure, gate EOF, and protocol failure never release the group-leader identity.

The parent never sends a destructive signal to a numeric POSIX PID or PGID and never polls or reaps the supervisor before the final boundary close. After proving that the supervisor is still its unreaped direct child, it writes one strict termination command to the parent-only control pipe. The still-live supervisor then signals its own current process group with `SIGKILL`, so a recycled PGID cannot redirect the signal. The parent only waits/reaps that direct child and performs a read-only absence check afterward; it never signals by PGID after identity release. If `_fork_exec` created the supervisor but `Popen` did not return it, status plus the private control writer retains the same safe protocol. Missing, truncated, mismatched, timed-out, externally reaped, or unexpectedly exited supervisor state is never converted into a guessed identity: no destructive parent signal is sent and the verified closure is preserved.

On Windows the caller creates a Job Object with `KILL_ON_JOB_CLOSE` before process creation. A private `CreateProcessW` wrapper passes both the exact inherited standard-stream HANDLE list and `PROC_THREAD_ATTRIBUTE_JOB_LIST` in one `STARTUPINFOEX`, with `CREATE_SUSPENDED`; the root therefore belongs to the Job from the first successful kernel return rather than through a post-create assignment window. A prebuilt process wrapper uniquely owns the `PROCESS_INFORMATION` process/thread HANDLE fields before the call, so a `BaseException` delivered after system creation but before Python returns still has both the atomic Job boundary and native handles available for cleanup. Only a confirmed Job member is resumed. `CREATE_BREAKAWAY_FROM_JOB` is never used. Incompatible nested-Job policy fails closed while the root is still suspended, and ownership never depends on a post-launch PID lookup or parent-chain snapshot. The Job handle stays owned through monitoring, root wait, and bounded output reading. Before any result is accepted, `ActiveProcesses` must reach zero; ordinary descendants, short-lived launchers, broken parent chains, and inherited-stdio descendants are terminated as one Job when necessary. Resume, termination, query, or close failures never fall back to PID enumeration; an unconfirmed Job preserves the verified closure. Every Job, process/thread, inherited-stream, and attribute-list close first binds the native API, then consumes the value from its single owner before entering the native call; an asynchronous exception after detach or close is explicitly indeterminate and cannot retry a reused numeric HANDLE. `CreateJobObjectW` itself returns an empty Job handle rather than writing into a pre-owned field, so an asynchronous interruption between that return and Python ownership assignment can at worst leak an empty Job handle; no child exists at that boundary, and retrying the unknown numeric handle would be less safe. Adapter stderr and structured errors are redacted and truncated before they reach the caller. A timeout has unknown native side effects, so retry a mutating `start` only with the same `request_id` and use the repository's native recovery/status path if its outcome remains uncertain.

This is a reliable cleanup boundary for approved adapters that stay inside the inherited process boundary; it is not an operating-system sandbox. On POSIX an adapter that deliberately creates a new session with `setsid`, or on any platform uses an external broker, privilege boundary, Hook bypass, or another deliberate escape, is outside the portable contract and must not be approved.

This interface does not make the repository lifecycle generic. The repository still owns its task identities, leases, candidate pool, explicit seal, validation evidence, recovery, and cleanup. DWW owns only routing, approval, the bounded JSON call, and generic orchestration bookkeeping.

For staged adoption, parity testing, rollout, and rollback, follow [Delegated adapter migration](delegated-migration.md).
