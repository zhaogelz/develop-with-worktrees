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

## Onboarding validation policy

Use a project-reviewed schema-3 policy when one already exists:

```text
dww init --verification-file <reviewed-verification.toml> --accept
dww choose --mode isolated --verification-file <reviewed-verification.toml>
```

`--verification-file` and `--verify` are mutually exclusive. DWW parses the supplied file with the same schema as tracked `.solo-ai/verification.toml`, shows its redacted profile plan before ordinary acceptance, and copies that exact reviewed text into the adoption commit. It does not infer a second cache or reuse policy from the file: cross-task reuse remains available only for profiles that explicitly declare `external_state = "none"`, `input_closure = "complete"`, and matching inputs, environment, and tools.

Without either option, DWW discovers conventional commands and renders one conservative `level = "full"`, `full_scope = "integration"` profile that covers all paths, keeps `cross_task_reuse = false`, and declares external state as unknown. This fallback provides a real combined validation gate without pretending that it understands the project. Explicit `--verify` continues to create the compatible Ready-level profile. When no command is found, static-only remains an explicit limitation. The initialization preview records which of these sources produced the policy.

## Managed rule-block upgrades

`dww doctor` reports the managed `AGENTS.md` block as `current`, one exact known legacy version, or `unknown-or-user-edited`. A plugin update never writes a user repository by itself. When the user asks to synchronize rules, perform the work in a normal isolated DWW task and replace only the recognized complete legacy block with the current complete block, then commit and deliver it through the usual lifecycle. Missing markers, duplicate markers, or any user-edited/unknown block stay protected and require review; deinitialization follows the same rule and never deletes ambiguous policy text.

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
integration = { mode = "batched", worktree_mode = "reusable", batch_size = 3, candidate_capacity = 10, seal_policy = "auto_full", candidate_validation = "batch", tail_policy = "explicit", tail_quiet_seconds = 30 }

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

Newly rendered policy uses batched candidate-first integration. `candidate_validation = "batch"` means Finish first creates a durable immutable `held` source candidate without requiring a separate project test. Development checks remain available when useful. If a runtime Adapter is configured, `release` must succeed without changing or contaminating the task worktree. DWW then releases the slot and activates the candidate as `pending`; only pending candidates are eligible for a batch. `candidate_capacity` counts held, pending, and sealed nonterminal candidates. Set `candidate_validation = "ready"` only when a project deliberately needs the legacy candidate-level gate.

`integration.worktree_mode` accepts `reusable` (new repository default, batched only) or `dedicated` (compatibility default when absent). Reusable batches share `<worktree_directory>/solo-ai-integration` serially and retain dependencies on return. Mode is frozen at Start and changes the candidate policy lane; enabling it does not rewrite old candidates or batch locations. The existing candidate-pool schema is 6 and remains able to read schemas 1–5; do not downgrade to an engine that cannot read the new state. No second workspace registry is created.

Reusable batch Adapter contexts add `worktree_binding`: `mode`, `owner` (batch ID), positive integer `generation`, `worktree`, `worktree_resolved`, `worktree_identity`, `managed_root_resolved`, and `managed_root_identity`. Identity objects carry integer `device`, `inode`, and `mode`; preserve integer precision. DWW validates the live directory before/after operations. An Adapter may read the existing `integration_workspace` entry in Git-common-dir `solo-ai/candidate-batches.json` to compare current owner/generation/location before side effects; it must not mutate that state or invent its own owner registry. `runtime_cycle` remains the distinct resource-activation cycle within that workspace generation. Legacy dedicated contexts do not carry the binding.

With `seal_policy = "auto_full"`, activating the third eligible candidate freezes the oldest configured full batch in one `base_ref + base_head + activation_epoch` lane, unless that base already has an active batch. `base_head` is the immutable task-start snapshot: automatic sealing never groups a historical candidate with a later-base candidate merely because both target the same branch. `batch_size` is 1–5 and defaults to 3. `candidate_capacity` must be at least the batch size and defaults to 10.

The new `tail_policy = "explicit"` never infers that a short tail is ready from inactivity or a timer. The coordinator ends the current round with `batch reconcile --force --cause round-complete --reason <one-line-basis>`; `user`, `deploy`, and `dependency` record explicit immediate-integration reasons. DWW freezes the exact pending candidates from one `base_ref + base_head + activation_epoch` lane, and rejects round completion while that lane has an active producer. Start, candidate activation, Abandon, and tail freeze share the candidate admission lock, so a new Start either blocks the freeze or begins after the immutable snapshot. `SessionEnd`, Hook delivery, host idleness, and UI task counts never supply completion facts. `batch seal --candidate ...` remains an exact-list compatibility interface, and a smaller list must record the same cause and reason. Seal intent is derived from the ordered candidate ids, base, and policy epoch rather than its explanation, so retries are idempotent and retain the first recorded basis. If a diagnosed external blocker changed without changing the retained candidates, `batch seal --after-failed-batch <id> --candidate ...` creates one reviewed successor whose ordered list must exactly match that failed predecessor.

`tail_policy = "quiet_or_explicit"`, `tail_policy = "explicit"`, `candidate_validation = "ready"`, and `seal_policy = "explicit"` preserve older policies when deliberately configured. `integration.mode = "direct"` preserves immediate local promotion. A pre-0.5 repository with no `integration` table is interpreted as direct; an existing batched table without `candidate_validation` remains Ready-gated. These compatibility defaults prevent a plugin update from changing active repository behavior. Each new task snapshots the resolved policy at Start; candidates from another policy epoch are never mixed into an automatic batch.

Candidate-pool records distinguish `composition_conflict`, `validation_failed`, and `promotion_blocked`. Only the exact candidate identified by a composition conflict may use `candidate repair`; the repair command is idempotent for the candidate and latest base and stops after two published repair generations. This bound is a fixed safety contract rather than a repository-tunable retry loop.

## Host handoff receipts

DWW records Git and lifecycle facts; the host's native task system still owns task scheduling and actual messages. In Codex Desktop, CLI calls use the exact injected `CODEX_THREAD_ID` when `--host-kind/--host-thread` are omitted. The explicit pair remains a compatibility override for another host. DWW never derives an identity from a title, UI count, worktree count, session fingerprint, or Hook session id. It preserves the Start caller as the immutable candidate source, while the caller that actually freezes a batch becomes its coordinator.

After a candidate is published, the originating development task may finish its current round. The host that froze the full batch or explicit/quiet tail is the recorded coordinator and follows the batch through integration. A composition conflict attributed to one candidate creates exactly one local repair receipt in Git-common-dir state. Full validation failures, promotion blocks, and unattributed composition failures do not create a repair request automatically. The current coordinator may create one validation repair receipt only with `host-handoff repair attribute --batch ... --candidate ... --evidence ...`, after it has reviewed the exact test, log, and candidate evidence.

Use `host-handoff status` to read pending requests and returned actions. The recorded coordinator calls `host-handoff repair dispatch --request <id>` to prepare a stable, redacted message payload, sends it through the host's native messaging API, then records the observed result with `host-handoff repair delivery --request <id> --outcome sent|uncertain|failed`. Preparing a payload never records it as delivered. The assignee acknowledges it with `host-handoff repair claim` and calls `host-handoff repair prepare` to create or return one idempotent managed repair task. When that task publishes a repair candidate, its source prepares and records the return notification with `result-dispatch` and `result-delivery`; it is still only a candidate. The handoff projects `resolved` only after the replacement candidate is actually delivered into the current base. If the coordinator or original developer is unavailable, use the explicit `host-handoff batch take-over --expected-revision <n> ...` or `host-handoff repair take-over --reason <text> ...` path. Coordinator revisions reject delayed actions from an older coordinator; source identity remains immutable.

`host-handoff` does not send a message, create a scheduler, or decide product conflicts. A host without an exact source identifier still receives a durable receipt, but must explicitly assign a repair owner before dispatch.

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

All changed batch paths must be covered by a Ready or integration Full profile. Ready should contain syntax/static checks, affected compilation, and light contract tests; it is no longer a mandatory Finish gate for a default source candidate. `resource_class = "heavy"` is accepted only with `level = "full"` or `"stress"`. A Ready or Full profile that selects work from the frozen diff must explicitly set `frozen_base = true`; only then its commands receive non-secret `DWW_VALIDATION_BASE_REF` and `DWW_VALIDATION_BASE_HEAD`, and those values join the proof identity. Ordinary static checks therefore retain their exact-input proof reuse when an unrelated base commit advances. A `frozen_base = true` Full profile additionally receives `DWW_VALIDATION_SCOPE`. Full validation selects affected Ready checks first. A Full profile defaults to `full_scope = "integration"`, so it runs in the normal candidate batch; a `full_scope = "complete"` profile is an explicit diagnostic command only. Projects should select database, complete-build, authentication, and browser work narrowly in the integration scope only when the frozen changed paths require it.

New repositories place optional, low-frequency pressure checks in `.solo-ai/stress-verification.toml`. It uses the same schema, must set `static_only = false`, and may declare only `level = "stress"` profiles. DWW merges it with the primary policy, but only `dww verify --level stress` selects those profiles: Stress never substitutes for Ready or batch Full, and unlike Ready/Full it does not require every candidate path to match a Stress profile. Existing repositories may keep stress profiles in `verification.toml` for compatibility. Both files are approval- and proof-relevant policy inputs, so a change fails closed and conservatively invalidates old evidence.

A profile proof is reused only when its normalized commands, tool/platform facts, declared environment hashes, tracked input closure, and reuse scope are identical. A stored proof whose identity changed fails closed. A failed profile declared with `external_state = "none"` and `input_closure = "complete"` is not rerun unchanged; modify the candidate/policy or explicitly reclassify it.

`static_only = true` is valid only with no profiles. Commands are explicit argv arrays. Schema 2 is deliberately unsupported for tracked verification policy; migrate the repository policy before installing this release. Older local task state is read-upgraded to schema 6. Candidate-pool schemas 1–4 remain readable and migrate to schema 5; legacy candidates remain in their explicit policy epoch.

## Runtime Adapter contract

For fresh Full reuse, required outputs, and project onboarding, follow
[verification-reuse.md](verification-reuse.md). These rules use existing profile
fields; they do not add a second cache or orchestration contract.

`runtime_adapter` is optional and host-neutral. DWW appends one absolute JSON context path as the final argument; the project command owns every port, database, browser, authentication, deployment, and runtime-version decision. Both command argv and every tracked file matched by `input_paths` enter the machine approval fingerprint. Missing matches, approval drift, nonzero exit, timeout, or worktree changes fail closed.

`activate` applies only to isolated managed tasks. DWW invokes it after the exact task, branch, worktree, slot identity, and anchor exist, but before Start changes the task and slot from `starting` to `active`. Its immutable context includes the task and slot ids, absolute worktree, base ref/head, and a deterministic inclusive 100-port block derived from `port_base + (slot - 1) * 100`. `candidate_head` is deliberately absent because Start has not produced a candidate; DWW itself checks that the clean worktree still equals the frozen base immediately before and after the Adapter call. Candidate identity first enters Adapter context for release after Ready/Finish has fixed it. The project may use those facts to create ignored runtime metadata or start project resources; DWW does not interpret them. A nonzero result leaves the task and slot `starting`, so the same `request_id` or `recover` retries the exact activation. A successful content-addressed receipt is reused after an interruption. Tracked changes, ordinary untracked content, protected content, or unknown ignored content quarantine and preserve the worktree rather than activating it.

`release` runs after the immutable candidate ref exists but before candidate activation and slot release. Its successful receipt is content-addressed and reusable for interruption recovery.

`batch_activate` and `batch_release` are an optional pair around combined Full validation in the dedicated integration worktree. Their immutable contexts include the exact batch and ordered candidate ids, a positive persisted `runtime_cycle`, absolute worktree, frozen base ref/head, composed integration head, Adapter input hashes, and one inclusive 100-port block at `port_base + 3200`; this block is disjoint from all 32 task-slot blocks. `batch_release` receives the same cycle plus `validation_outcome = passed|failed|interrupted` and the recorded error when present. Full never starts before activation succeeds. Promotion never starts before release succeeds and the composed Git identity is rechecked. Activation or release uncertainty keeps the sealed generation active and recoverable; `batch recover` reuses only a successful receipt with identical command, inputs, context, and cycle. If validation was interrupted after resources were released, recovery increments `runtime_cycle` and invokes activation again before rerunning Full, so a stale successful activation receipt cannot stand in for released resources. Validation failure and interruption still run release before the generation is failed or retried. Projects may create ignored runtime metadata, databases, services, or browser state, but concrete resource semantics remain entirely project-owned.

When the current normalized validation or Adapter plan is not approved, DWW fails before executing validation, an Adapter, `dev_start`, or a configured `warm` command and writes an exact field-level comparison with the nearest accepted plan under `<git-common-dir>/solo-ai/approval-mismatches/`. Pure `show`/`list` calls, root/task-anchor reads, and a warm slot without configured commands do not execute project commands and do not require that approval. The report is diagnostic evidence only; it never broadens or renews approval automatically.

`verify_effective` never substitutes for Git delivery: `runtime verify --candidate <id>` is allowed only after that candidate's batch is contained in the current base, and each explicit check runs again because external runtime state may change. DWW records context, redacted log, digest, duration, and result under Git-common-dir state; it does not persist leases or environment values there.

## Machine-local validation capacity

No repository configuration is required. The default `auto` capacity is stable for a machine:

```text
clamp( min(floor(physical CPU cores / 4), floor(total RAM GiB / 8)), 1, 4 )
```

If hardware detection fails, capacity is one and `status` reports the warning. A user may run `settings --validation-capacity auto|1..4`; that setting is local and never changes a tracked repository policy.
