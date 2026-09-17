# Configuration reference

This reference describes tracked policy, compatibility defaults, and local
approval. Use it when initializing a repository or changing a configuration
field. For check semantics read [verification reuse](verification-reuse.md); for
Runtime Adapter commands read [Runtime Adapter](runtime-adapter.md).

## What is tracked

| Location | Purpose |
|---|---|
| `.solo-ai/config.toml` | lifecycle, worktree, integration, and cleanup boundary |
| `.solo-ai/verification.toml` | schema-3 development, Ready, and Full profiles |
| `.solo-ai/stress-verification.toml` | optional schema-3 Stress-only profiles |
| `.solo-ai/delegated.toml` | optional mature-workflow adapter declaration |
| managed block in `AGENTS.md` | generated lifecycle reminder |
| Git common-dir `solo-ai/` | local tasks, anchors, candidates, receipts, and approval state |
| machine user state | validation queue, local settings, and metrics |

Preferences and current-task authorizations are local to the Git common
directory. They never enter version control and contain no raw session IDs or
delegation codes.

## Adoption and approval

A reviewed schema-3 verification policy can be adopted directly:

```text
dww init --verification-file <reviewed-verification.toml> --accept
dww choose --mode isolated --verification-file <reviewed-verification.toml>
```

Without a supplied file, DWW discovers conventional commands and creates one
conservative integration Full profile. The fallback does not claim cross-task
reuse; if no command is found, the resulting static-only limitation remains
explicit. `--verification-file` and `--verify` are mutually exclusive.

Machine-local approval covers the normalized execution policy: commands, relevant
configuration, tool and lockfile identity, declared environment names, profile
closure, and Runtime Adapter inputs. Comments and line-ending-only policy edits
do not change that normalized policy. A semantic command, scope, permission,
runtime, or configuration change produces an approval mismatch and must be
reviewed before execution.

Approval is not a profile proof. A profile's own reuse identity is narrower and
depends on its declared inputs and execution facts. A change to an unrelated
profile or formatting-only bytes does not by itself rerun a profile whose own
identity still matches; a changed command, relevant input, environment, tool
fact, frozen base, missing log, incomplete closure, or mutable external state
does. See [verification reuse](verification-reuse.md).

## `config.toml`

A new managed repository renders this policy shape:

```toml
schema_version = 2
mode = "managed"
slots = 3
branch_prefix = "codex/"
worktree_directory = ".worktrees"
port_base = 20000
remote_policy = "local-only"
sensitive_allowlist = []

cleanup = { owned_paths = [] }
integration = { mode = "batched", worktree_mode = "reusable", batch_size = 3, candidate_capacity = 10, seal_policy = "auto_full", candidate_validation = "batch", tail_policy = "explicit" }
```

| Field | New-repository default | Meaning and limits |
|---|---|---|
| `slots` | `3` | 1–32 managed worktree slots; reduced extra slots drain before reuse |
| `branch_prefix` | `codex/` | prefix for DWW-managed task branches |
| `worktree_directory` | `.worktrees` | immutable after adoption |
| `port_base` | `20000` | start of deterministic task port blocks |
| `remote_policy` | `local-only` | DWW lifecycle does not contact or mutate remotes |
| `cleanup.owned_paths` | `[]` | exact top-level paths eligible only for explicit `prune-slot` |
| `integration.mode` | `batched` | source candidates are locally integrated in batches |
| `worktree_mode` | `reusable` | one serial integration workspace; `dedicated` remains compatible |
| `batch_size` | `3` | exact automatic full-batch size, 1–5 |
| `candidate_capacity` | `10` | held, pending, and sealed nonterminal capacity |
| `seal_policy` | `auto_full` | freeze a complete exact lane automatically |
| `candidate_validation` | `batch` | Finish publishes source; batch performs project validation |
| `tail_policy` | `explicit` | small tail requires an explicit cause and reason |

`tail_quiet_seconds` may remain in an older policy, but new explicit tails never
use a timer. `tail_policy = "quiet_or_explicit"`, `candidate_validation = "ready"`,
`seal_policy = "explicit"`, and `integration.mode = "direct"` retain older
behavior when deliberately configured. Every task snapshots the resolved policy
at Start; do not assume a later policy edit changes an active candidate.

A reusable workspace returns safely without recursively deleting dependency
trees. Dedicated batches retain their original physical-retirement behavior. Both
modes use the same candidate and base identity gates; details are in
[safety](safety.md).

## Candidate and state compatibility

The tracked configuration schema is 2. Verification policy is schema 3; schema
2 verification files are intentionally unsupported. Current local task state is
read-upgraded to schema 6. The current candidate-pool schema is 6 and reads
schemas 1 through 5 before the next write upgrades them to 6. These numbers refer
to distinct objects and must not be substituted for one another.

Candidate records retain a policy epoch and frozen base lane. Older direct or
Ready-gated candidates remain under their recorded compatibility policy, rather
than being silently converted to the current candidate-first default.

## Verification policy fields

A schema-3 profile is explicit argv, selected paths, and an evidence contract:

```toml
schema_version = 3
static_only = false

[[profiles]]
id = "unit"
level = "ready"
paths = ["src/**", "tests/**"]
input_paths = ["src/**", "tests/**", "pyproject.toml", "uv.lock"]
input_closure = "declared"
external_state = "unknown"
environment = ["PYTHONUTF8"]
timeout_seconds = 1200
resource_class = "normal"
commands = [["uv", "run", "pytest"]]
```

`level` is `development`, `ready`, `full`, or `stress`. A heavy profile is Full
or Stress only. Ready and integration Full profiles must cover changed batch
paths. `frozen_base = true` adds the validated base to that profile's proof
identity. Full defaults to `full_scope = "integration"`; a
`full_scope = "complete"` profile is an explicit diagnostic, never an ordinary
candidate or release gate.

Stress profiles belong in `.solo-ai/stress-verification.toml` for new
repositories, set `static_only = false`, and contain only Stress profiles. They
run only with the explicit Stress command. Both verification files are policy
inputs; a semantic change can require new approval, while a proof is reused only
under its own exact conditions.

## Generated managed block

`dww doctor` classifies the managed `AGENTS.md` block as current, one exact known
legacy version, or unknown/user-edited. Plugin updates do not edit repositories.
When a user asks to synchronize rules, use an ordinary isolated task and replace
only a recognized complete legacy block. Missing markers, duplicates, or
user-edited text remain protected and require review. The generated block is not
a place to manually compress project-specific documentation.

## Local validation capacity

Validation capacity is local, not tracked. `settings --validation-capacity
auto|1..4` changes the machine setting. In `auto`, DWW derives a stable value from
physical CPU and RAM and clamps it to 1–4; detection failure uses one and reports
a warning. The queue controls expensive execution, not task scheduling.
