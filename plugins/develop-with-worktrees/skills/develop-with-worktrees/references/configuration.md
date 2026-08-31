# Configuration reference

Tracked policy is deliberately small. Local state, approvals, queue settings, logs, metrics, leases, and proofs are never committed.

```text
.solo-ai/config.toml          lifecycle and cleanup boundary
.solo-ai/verification.toml    schema 3 validation profiles
.solo-ai/delegated.toml       optional mature-repository adapter contract
AGENTS.md managed block        Codex lifecycle reminder
<git-common-dir>/solo-ai/     repository-local state and receipts
<machine-user-state>/develop-with-worktrees/  validation queue, settings, metrics
```

`preferences.json` in the Git common directory is the machine-local long-term choice for this repository. `enabled = false` means normal current-directory development and never changes tracked files. `session-overrides.json` contains only hashed current-task session authorizations and delegated capability hashes; it contains neither raw session identifiers nor delegation codes and never enters version control.

`dww route --json` is a compact read-only lifecycle-owner query. It returns one action: `defer`, `delegated`, `disabled`, `current-task`, `managed`, or `ask`. A detected mature workflow normally returns `defer`; existing preference and session files are left untouched but inactive while that workflow marker remains, and DWW creates no lifecycle, anchor, candidate, or integration-batch state. It returns `delegated` only when `.solo-ai/delegated.toml` is valid and its exact contract-plus-input fingerprint has been approved in local Git-common-dir state. See [Delegated adapter contract](delegated-adapters.md).

State-changing lifecycle commands linearize at route admission; DWW is not a background watcher and cannot lock files owned by another workflow. A command whose admission observes `defer` performs no DWW state write. New task orchestration uses the host's native task/subagent system. The old `orchestrate` state remains only for draining existing batches and is not created for new work.

## `config.toml`

```toml
schema_version = 2
mode = "managed"
slots = 3
branch_prefix = "codex/"
worktree_directory = ".worktrees"
port_base = 20000
remote_policy = "local-only"
sensitive_allowlist = []

# Empty by default. Each item is one exact top-level directory or file name.
cleanup = { owned_paths = [] }
integration = { mode = "direct", batch_size = 5, candidate_capacity = 10 }
```

`slots` is 1–32. Existing extra slots drain when the configured count is reduced and are never allocated until re-enabled. The worktree root is immutable after adoption. `cleanup.owned_paths` does not cause automatic deletion: it only names potential manual `prune-slot` targets. Entries must be unique under case-insensitive comparison so one Windows path cannot be declared twice with different casing.

`integration.mode` is `direct` or `batched`. Direct is the generic default and makes Finish integrate immediately. Batched mode makes Finish publish an immutable verified candidate and release the slot; only an explicit `batch seal` promotes the exact listed generation. `batch_size` is 1–5 and defaults to 5. `candidate_capacity` must be at least the batch size, defaults to 10, and bounds pending candidate pressure rather than triggering an automatic seal.

Candidate-pool records distinguish `composition_conflict`, `validation_failed`, and `promotion_blocked`. Only the exact candidate identified by a composition conflict may use `candidate repair`; the repair command is idempotent for the candidate and latest base and stops after two published repair generations. This bound is a fixed safety contract rather than a repository-tunable retry loop.

`remote_policy = "local-only"` governs DWW itself: Start, Ready, Finish, candidate publication, batch integration, and recovery never contact or mutate a remote. It does not prohibit a separate ordinary push after successful integration when the user explicitly requests publishing. That push must come from the clean base worktree, use a dry-run first, and must not force-update a remote ref.

## `verification.toml` schema 3

```toml
schema_version = 3
static_only = false

[[profiles]]
id = "unit"
level = "ready"                 # development, ready, or full
paths = ["src/**", "tests/**"]
input_paths = ["src/**", "tests/**", "pyproject.toml", "uv.lock"]
input_closure = "declared"      # complete is required for cross-task reuse
cross_task_reuse = false
external_state = "unknown"      # only none may opt into cross-task reuse
environment = ["PYTHONUTF8"]
timeout_seconds = 1200
resource_class = "normal"       # normal or heavy
commands = [["uv", "run", "pytest"]]
```

All changed candidate paths must be covered by a Ready profile. `static_only = true` is valid only with no profiles. Commands are explicit argv arrays. Schema 2 is deliberately unsupported for tracked verification policy; migrate the repository policy before installing this release. Older local task state is read-upgraded to schema 5 with existing tasks treated as isolated and direct integration preserved.

## Machine-local validation capacity

No repository configuration is required. The default `auto` capacity is stable for a machine:

```text
clamp( min(floor(physical CPU cores / 4), floor(total RAM GiB / 8)), 1, 4 )
```

If hardware detection fails, capacity is one and `status` reports the warning. A user may run `settings --validation-capacity auto|1..4`; that setting is local and never changes a tracked repository policy.
