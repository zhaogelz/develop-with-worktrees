# Configuration reference

Tracked policy is deliberately small and versioned. Runtime state is never tracked.

```text
.solo-ai/config.toml          repository lifecycle policy
.solo-ai/verification.toml    repository verification policy
AGENTS.md managed block        Codex invocation instruction
<git-common-dir>/solo-ai/      local state, leases, logs, proofs, caches
```

## `config.toml` schema 2

```toml
schema_version = 2
mode = "managed"
slots = 3                       # 1 through 5
branch_prefix = "codex/"
worktree_directory = ".worktrees"
port_base = 20000               # each slot owns a separate 100-port range
remote_policy = "local-only"
sensitive_allowlist = []

# Optional, repository-declared argv. It runs before the built-in low-false-
# positive secret gate; a non-zero exit preserves the task.
# secret_scanner = ["gitleaks", "protect", "--staged"]

# Optional serial preparation commands. They run only through WarmSlot in an
# idle slot and do not copy a primary .venv, node_modules, cache, or .env file.
# warm = [["uv", "sync"]]

[lifecycle]
# dev_start = ["npm", "run", "dev", "--", "--port", "{port}"]

# [lifecycle.readiness]
# kind = "http"                 # tcp or http
# target = "http://127.0.0.1:{port}/health"
# timeout_seconds = 30
```

There is no `compatible` tracked mode. A detected mature workflow wins before managed policy and causes complete defer. A locally disabled preference wins over both. If a marker is later added, managed actions stop; if it is later removed, adoption is not silently recreated or migrated.

`lifecycle.dev_start` is an argv array and is started with `shell=False`. It must declare a TCP or HTTP readiness probe. The process manager records a redacted-safe identity digest, uses a slot-only port range, and stops only a matching owned process tree.

Reducing `slots` does not delete directories: active excess slots become `draining`, then `inactive` after release. Increasing up to five reactivates an inactive slot. Start never queues or expands the pool.

## `verification.toml` schema 2

```toml
schema_version = 2
static_only = false

[[profiles]]
id = "backend"
paths = ["backend/**"]
commands = [["uv", "run", "pytest", "backend/tests"]]

# Default: candidate-only reuse. Set both fields exactly as below only when the
# command has a complete declared closure and no database/container/network/
# browser/time/unknown external input.
cross_task_reuse = false
external_state = "unknown"     # only "none" permits explicit cross-task reuse
input_paths = ["backend/**", "pyproject.toml", "uv.lock"]
environment = ["PYTHONUTF8"]   # raw values are hashed, never persisted
```

Commands must be argv arrays. Do not place a shell pipeline, inline secret, or an implicit shell interpreter in them. The discovery helper chooses `pwsh` or `powershell.exe` only when an existing PowerShell verification script can actually be interpreted; it never assumes PowerShell 7.

Every machine must run `approve --accept` for the exact normalized plan. The approval fingerprint covers both policy files, all profile semantics, resolved executable paths and versions, lockfile inputs, platform, declared environment names and Git-common identity. A config or tool/platform change therefore requires approval again. Approvals are local and are not committed.

`static_only = true` is valid only when there are no profiles. An uncovered candidate change is an error unless static-only is explicitly selected.
