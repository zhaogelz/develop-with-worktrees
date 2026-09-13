# Configuration reference

Tracked policy is deliberately small. Local state, approvals, queue settings, logs, metrics, leases, and proofs are never committed.

```text
.solo-ai/config.toml          lifecycle and cleanup boundary
.solo-ai/verification.toml    schema 3 validation profiles
.solo-ai/stress-verification.toml  optional schema 3 explicit pressure profiles
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
integration = { mode = "batched", worktree_mode = "reusable", batch_size = 2, candidate_capacity = 10, seal_policy = "auto_full", tail_policy = "quiet_or_explicit", tail_quiet_seconds = 30 }

# Optional. DWW appends one JSON context-file path to each argv.
[runtime_adapter]
activate = ["uv", "run", "scripts/dww-runtime-adapter.py", "activate"]
release = ["uv", "run", "scripts/dww-runtime-adapter.py", "release"]
batch_activate = ["uv", "run", "scripts/dww-runtime-adapter.py", "batch-activate"]
batch_release = ["uv", "run", "scripts/dww-runtime-adapter.py", "batch-release"]
verify_effective = ["uv", "run", "scripts/dww-runtime-adapter.py", "verify-effective"]
input_paths = ["scripts/dww-runtime-adapter.py", "deploy/**"]
timeout_seconds = 300
```

`slots` is 1–32. Existing extra slots drain when the configured count is reduced and are never allocated until re-enabled. The worktree root is immutable after adoption. `cleanup.owned_paths` does not cause automatic deletion: it only names potential manual `prune-slot` targets. Entries must be unique under case-insensitive comparison so one Windows path cannot be declared twice with different casing.

Newly rendered policy uses batched candidate-first integration. Finish first creates a durable immutable `held` candidate. If a runtime Adapter is configured, `release` must succeed without changing or contaminating the task worktree. DWW then releases the slot and activates the candidate as `pending`; only pending candidates are eligible for a batch. `candidate_capacity` counts held, pending, and sealed nonterminal candidates.

`integration.worktree_mode` accepts `reusable` (new repository default, batched only) or `dedicated` (compatibility default when absent). Reusable batches share `<worktree_directory>/solo-ai-integration` serially and retain dependencies on return. Mode is frozen at Start and changes the candidate policy lane; enabling it does not rewrite old candidates or batch locations. The existing candidate-pool schema is 4 and remains able to read schemas 1–3; do not downgrade to an engine that cannot read the new state. No second workspace registry is created.

Reusable batch Adapter contexts add `worktree_binding`: `mode`, `owner` (batch ID), positive integer `generation`, `worktree`, `worktree_resolved`, `worktree_identity`, `managed_root_resolved`, and `managed_root_identity`. Identity objects carry integer `device`, `inode`, and `mode`; preserve integer precision. DWW validates the live directory before/after operations. An Adapter may read the existing `integration_workspace` entry in Git-common-dir `solo-ai/candidate-batches.json` to compare current owner/generation/location before side effects; it must not mutate that state or invent its own owner registry. `runtime_cycle` remains the distinct resource-activation cycle within that workspace generation. Legacy dedicated contexts do not carry the binding.

With `seal_policy = "auto_full"`, activating the second eligible candidate freezes the oldest configured full batch in one `base_ref + base_head + activation_epoch` lane, unless that base already has an active batch. `base_head` is the immutable task-start snapshot: automatic sealing never groups a historical candidate with a later-base candidate merely because both target the same branch. `batch_size` is 1–5 and defaults to 2. `candidate_capacity` must be at least the batch size and defaults to 10.

With `tail_policy = "quiet_or_explicit"`, `batch reconcile` freezes a 1-candidate tail only after DWW's persisted state shows zero modifying producers in the exact `base_ref + base_head + activation_epoch` lane for the complete `tail_quiet_seconds` period. The default is 30 seconds. Start, candidate activation, Abandon, and tail freeze share the candidate admission lock, so a new Start either blocks the freeze or begins after the immutable snapshot. Activity controls timing only: the candidate list still contains only already activated, unsealed, same-lane candidates. `SessionEnd`, Hook delivery, host idleness, and UI task counts may wake reconcile but never supply completion facts.

There is deliberately no maximum candidate age or longest-wait seal. While a producer remains nonterminal, the tail remains open until it is finished, recovered, or abandoned. Reconcile returns `next_reconcile_at` only after the lane becomes quiet; the host must provide a reliable heartbeat to claim automatic quiet-tail support. An explicit user, deployment, or dependency request may use `batch reconcile --force --cause user|deploy|dependency`. `batch seal --candidate ...` remains an exact-list compatibility interface. Seal intent is derived from the ordered candidate ids, base, and policy epoch rather than its trigger, so retries are idempotent. If a diagnosed external blocker changed without changing the retained candidates, `batch seal --after-failed-batch <id> --candidate ...` creates one reviewed successor whose ordered list must exactly match that failed predecessor.

`tail_policy = "explicit"` and `seal_policy = "explicit"` preserve manual-seal compatibility. `integration.mode = "direct"` preserves immediate local promotion. A pre-0.5 repository with no `integration` table is interpreted as direct; an existing batched table without the new fields remains explicit for both full and tail behavior. These compatibility defaults prevent a plugin update from changing active repository behavior. Each new task snapshots the resolved policy at Start; candidates from another policy epoch are never mixed into an automatic batch.

Candidate-pool records distinguish `composition_conflict`, `validation_failed`, and `promotion_blocked`. Only the exact candidate identified by a composition conflict may use `candidate repair`; the repair command is idempotent for the candidate and latest base and stops after two published repair generations. This bound is a fixed safety contract rather than a repository-tunable retry loop.

`remote_policy = "local-only"` governs DWW itself: Start, Ready, Finish, candidate publication, batch integration, and recovery never contact or mutate a remote. It does not prohibit a separate ordinary push after successful integration when the user explicitly requests publishing. That push must come from the clean base worktree, use a dry-run first, and must not force-update a remote ref.

## `verification.toml` schema 3

```toml
schema_version = 3
static_only = false

[[profiles]]
id = "unit"
level = "ready"                 # development, ready, full, or stress
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

All changed candidate paths must be covered by a Ready profile. Ready should contain syntax/static checks, affected compilation, and light contract tests. `resource_class = "heavy"` is accepted only with `level = "full"` or `"stress"`. A Ready or Full profile that selects work from the frozen diff must explicitly set `frozen_base = true`; only then its commands receive non-secret `DWW_VALIDATION_BASE_REF` and `DWW_VALIDATION_BASE_HEAD`, and those values join the proof identity. Ordinary static checks therefore retain their exact-input proof reuse when an unrelated base commit advances. A `frozen_base = true` Full profile additionally receives `DWW_VALIDATION_SCOPE`. Full validation always selects Ready first. A Full profile defaults to `full_scope = "integration"`, so it runs in the normal candidate batch; a `full_scope = "complete"` profile runs only when an operator explicitly calls `dww verify --level full --complete` (or in repository CI). Projects should reserve complete scope for broad repository regression, and select database, complete-build, authentication, and browser work narrowly in the integration scope only when the frozen changed paths require it.

New repositories place optional, low-frequency pressure checks in `.solo-ai/stress-verification.toml`. It uses the same schema, must set `static_only = false`, and may declare only `level = "stress"` profiles. DWW merges it with the primary policy, but only `dww verify --level stress` selects those profiles: Stress never substitutes for Ready or batch Full, and unlike Ready/Full it does not require every candidate path to match a Stress profile. Existing repositories may keep stress profiles in `verification.toml` for compatibility. Both files are approval- and proof-relevant policy inputs, so a change fails closed and conservatively invalidates old evidence.

A profile proof is reused only when its normalized commands, tool/platform facts, declared environment hashes, tracked input closure, and reuse scope are identical. A stored proof whose identity changed fails closed. A failed profile declared with `external_state = "none"` and `input_closure = "complete"` is not rerun unchanged; modify the candidate/policy or explicitly reclassify it.

`static_only = true` is valid only with no profiles. Commands are explicit argv arrays. Schema 2 is deliberately unsupported for tracked verification policy; migrate the repository policy before installing this release. Older local task state is read-upgraded to schema 6. Candidate-pool schemas 1–3 remain readable and migrate to schema 4; legacy candidates remain in their explicit policy epoch.

## Runtime Adapter contract

For fresh Full reuse, required outputs, and project onboarding, follow
[verification-reuse.md](verification-reuse.md). These rules use existing profile
fields; they do not add a second cache or orchestration contract.

`runtime_adapter` is optional and host-neutral. DWW appends one absolute JSON context path as the final argument; the project command owns every port, database, browser, authentication, deployment, and runtime-version decision. Both command argv and every tracked file matched by `input_paths` enter the machine approval fingerprint. Missing matches, approval drift, nonzero exit, timeout, or worktree changes fail closed.

`activate` applies only to isolated managed tasks. DWW invokes it after the exact task, branch, worktree, slot identity, and anchor exist, but before Start changes the task and slot from `starting` to `active`. Its immutable context includes the task and slot ids, absolute worktree, base ref/head, and a deterministic inclusive 100-port block derived from `port_base + (slot - 1) * 100`. `candidate_head` is deliberately absent because Start has not produced a candidate; DWW itself checks that the clean worktree still equals the frozen base immediately before and after the Adapter call. Candidate identity first enters Adapter context for release after Ready/Finish has fixed it. The project may use those facts to create ignored runtime metadata or start project resources; DWW does not interpret them. A nonzero result leaves the task and slot `starting`, so the same `request_id` or `recover` retries the exact activation. A successful content-addressed receipt is reused after an interruption. Tracked changes, ordinary untracked content, protected content, or unknown ignored content quarantine and preserve the worktree rather than activating it.

`release` runs after the immutable candidate ref exists but before candidate activation and slot release. Its successful receipt is content-addressed and reusable for interruption recovery.

`batch_activate` and `batch_release` are an optional pair around combined Full validation in the dedicated integration worktree. Their immutable contexts include the exact batch and ordered candidate ids, a positive persisted `runtime_cycle`, absolute worktree, frozen base ref/head, composed integration head, Adapter input hashes, and one inclusive 100-port block at `port_base + 3200`; this block is disjoint from all 32 task-slot blocks. `batch_release` receives the same cycle plus `validation_outcome = passed|failed|interrupted` and the recorded error when present. Full never starts before activation succeeds. Promotion never starts before release succeeds and the composed Git identity is rechecked. Activation or release uncertainty keeps the sealed generation active and recoverable; `batch recover` reuses only a successful receipt with identical command, inputs, context, and cycle. If validation was interrupted after resources were released, recovery increments `runtime_cycle` and invokes activation again before rerunning Full, so a stale successful activation receipt cannot stand in for released resources. Validation failure and interruption still run release before the generation is failed or retried. Projects may create ignored runtime metadata, databases, services, or browser state, but concrete resource semantics remain entirely project-owned.

When the current normalized validation or Adapter plan is not approved, DWW fails before execution and writes an exact field-level comparison with the nearest accepted plan under `<git-common-dir>/solo-ai/approval-mismatches/`. The report is diagnostic evidence only; it never broadens or renews approval automatically.

`verify_effective` never substitutes for Git delivery: `runtime verify --candidate <id>` is allowed only after that candidate's batch is contained in the current base, and each explicit check runs again because external runtime state may change. DWW records context, redacted log, digest, duration, and result under Git-common-dir state; it does not persist leases or environment values there.

## Machine-local validation capacity

No repository configuration is required. The default `auto` capacity is stable for a machine:

```text
clamp( min(floor(physical CPU cores / 4), floor(total RAM GiB / 8)), 1, 4 )
```

If hardware detection fails, capacity is one and `status` reports the warning. A user may run `settings --validation-capacity auto|1..4`; that setting is local and never changes a tracked repository policy.
