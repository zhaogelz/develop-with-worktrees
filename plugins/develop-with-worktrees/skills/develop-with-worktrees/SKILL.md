---
name: develop-with-worktrees
description: "Use for any Git-repository task that may modify files. Route lifecycle ownership, anchor and isolate the task, validate exact changes, publish an immutable candidate, auto-integrate full batches, and reconcile only exact proven tails. Do not use for read-only analysis."
---

# Develop with Worktrees

DWW is a host-neutral Git safety lifecycle. It owns repository routing, local task identity, task anchors, worktree isolation, exact-path commits, validation evidence, candidate publication, integration, cleanup, and recovery. It does not own user-facing task decomposition or worker scheduling; use the host's native task/subagent facilities for those concerns.

Set `DWW` to this skill's absolute `scripts/dww.py` and invoke it only with `uv`:

```text
uv run --script <DWW> --repo <repository-or-worktree> <subcommand>
```

## Hooks are optional hardening

The CLI lifecycle must remain correct with no Hook installed or trusted. A trusted `SessionStart` Hook may provide route context and a trusted `PreToolUse` Hook may hard-deny unsafe writes on supported Codex local-tool paths, but neither is a source of task completion, candidate identity, cleanup, or recovery truth. `SessionEnd` or host-idle events may only wake `batch reconcile`; they never prove completion or expand its persisted candidate snapshot.

Keep `hooks/hooks.json` stable during ordinary updates so existing trust is not needlessly invalidated. Only when Codex actually reports a new or changed Hook pending review should you explain the exact protection change and ask once. The plugin never edits trust storage, bypasses Hook trust, or claims an untrusted Hook is active.

## Route before modifying

Use route context already supplied by the host when present. Otherwise run exactly one read-only fallback:

```text
uv run --script <DWW> --repo <repository-or-worktree> --json route
```

The result selects the repository lifecycle owner:

- `defer`: follow the repository's mature lifecycle. Do not initialize or mutate DWW lifecycle, candidate, or batch state.
- `delegated`: use only the repository's exact locally approved adapter contract. Do not also start the managed lifecycle.
- `disabled` or `current-task`: use ordinary current-directory development for the recorded scope.
- `managed`: start the normal isolated DWW task.
- `ask`: and only `ask`, use the single choice below.

Read-only analysis never claims a slot or creates an anchor.

## Keep staged work focused

For a modifying task, read the hard rules, the route result, the active task anchor, the target script, directly related configuration, and the tests that exercise the changed behavior. Use `rg` to locate symbols and then read the small surrounding sections; expand only when a direct caller or current contract requires it. Do not repeatedly reread unrelated history or old test logs.

Use one Start-created task and worktree for all phases of the same change. Validate each behavior in a tight order: syntax or static checks, new unit tests, affected focused regressions, then the relevant test group and DWW Ready/Finish gates. Do not rerun an unchanged failing command without changing the input, environment, or diagnosis. Keep the anchor current when scope, acceptance, phase results, or a material blocker changes.

## Task orchestration belongs to the host

For multiple independently verifiable outcomes, use the host's native task, subagent, dependency, wait, and status facilities. Each writing worker still receives one routed DWW lifecycle task and its own worktree. DWW does not create a second DAG, controller identity, worker dashboard, or scheduling state.

The legacy `dww orchestrate` command family is drain-only compatibility. Existing batches may be inspected, completed, paused, resumed, handed over, or cancelled as supported, but do not create a new orchestration batch, add tasks, or create repairs there.

Do not confuse task orchestration with candidate integration batches:

- native orchestration answers “who works on which outcome and when?”;
- DWW candidate batches answer “which exact verified Git candidates are intentionally combined and promoted together?”.

## First modifying intent in an unchosen repository

When route is `ask`, show exactly this question:

```text
此仓库怎么修改？

1. 每个任务使用独立目录（推荐）
   任务互不影响，完成后自动合回。

2. 这一次直接改当前目录
   只跳过这一次，下次还会询问。

3. 以后都直接改当前目录
   记住此选择，这个仓库不再询问。

只影响本机，可随时修改。
```

- Choice 1: run `choose --mode isolated`. Initialization silently selects discovered checks or internal static checks.
- Choice 2: when a trusted session identifier is available, run `choose --mode current-task --session <id>` and otherwise explain that this optional session-bound bypass is unavailable without Hook context. Do not weaken managed safety to simulate it.
- Choice 3: run `choose --mode current-repository`; this writes only local preference state.

If a mature workflow appears before `choose`, the command returns `deferred` and writes no DWW state.

## Managed task lifecycle

When modifying intent is clear, start proactively:

```text
uv run --script <DWW> --repo <repo> start --name <purpose> [--request-id <stable-caller-id>]
```

`Start` returns the worktree, private lease, immutable base identity, and task-anchor path. When a project Runtime Adapter configures `activate`, DWW first creates the exact isolated task, branch, worktree, and anchor, then asks the project to establish its runtime identity before Start returns the task as active. A failed activation remains retryable through the same `request_id` or `recover`; it never silently reallocates the task. Never expose the lease to another worker.

If the approved `activate` implementation itself is defective, do not edit the still-`starting` worktree or bypass DWW. After inspecting the failure, run `recover --task <id> --repair-runtime-adapter --path <exact-adapter-input>`, repeating `--path` only for the exact files required. This explicit path requires the exact persisted failed activation receipt and is available only for a clean, exact pre-activation isolated task with both `activate` and `release` configured. Every named file must already be tracked and covered by the approved `runtime_adapter.input_paths`, except that `.solo-ai/config.toml` may repair the command declaration itself. The same task can commit only that frozen exact list. Ready and batch Full remain mandatory, and the repaired `release` must successfully clean any partial activation before the candidate can become eligible.

1. Work only in the returned worktree.
2. Read and update the generated anchor before editing. It lives at `<git-common-dir>/solo-ai/task-anchors/<task-id>.md`, is never committed, and records objective, target, baseline, scope, acceptance, and progress.
3. After context compression, model change, handoff, or continuation, reread the anchor before the next modification.
4. Commit exactly the reviewed paths with repeated `--path`; never use broad staging.
5. Use `plan` or `verify --level development` when useful.
6. Run `ready`, then `finish` with the same task and lease.
7. Use `anchor show --task` to reread the current context and `anchor update --task --lease --file --expected-sha256` to save a reviewed UTF-8 revision while the task is active. Run it from the recorded task or base worktree, with an input file below that same worktree.

Ready refuses a missing, linked, oversized, non-UTF-8, identity-mismatched, or origin-mismatched anchor. Start persists the original purpose and baseline; `show` reports whether a legacy anchor has verified origin. A repeated update whose content is already current is a successful no-op, while Ready permits changes only to the complete `Current progress` block. For a genuine pre-anchor task, review its objective, target, scope, and acceptance, then use `anchor adopt` with the exact task-id confirmation; never invent those fields automatically. Ready validates the exact clean candidate and synchronizes only the recorded local base. DWW never fetches, pulls, pushes, opens a PR, rebases, squashes, amends, or rewrites history.

## Candidate-first integration

New repositories use `integration.mode = "batched"`, `worktree_mode = "reusable"`, `batch_size = 5`, `candidate_capacity = 10`, `seal_policy = "auto_full"`, `tail_policy = "quiet_or_explicit"`, and `tail_quiet_seconds = 90`:

- An optional project Runtime Adapter may activate project-owned runtime identity after the isolated worktree is exact and before Start returns. DWW supplies the task, slot, base, worktree, and deterministic slot port block; the project still owns every concrete port, database, browser, and service decision.
- Finish validates and creates one immutable candidate ref. A configured project runtime Adapter must release project-owned resources before the candidate becomes eligible; only then does Finish release the worktree slot, leave the base unchanged, and keep the anchor.
- Publishing the configured fifth eligible candidate atomically freezes the oldest five candidates in that base-and-policy lane. That Finish then runs or waits for the persisted integration generation; no Hook or resident process is required.
- While one batch owns the integration turn, later Finish calls may keep publishing into the remaining bounded pool. A second batch does not freeze against the same stale base; `reconcile` resumes the existing batch first.
- DWW composes the exact frozen tree differences in a dedicated integration worktree. A configured paired batch Runtime Adapter establishes project-owned Full-validation resources from the exact batch identity, positive persisted `runtime_cycle`, and dedicated port block, then releases them with the same cycle and recorded validation outcome before DWW may fail, retry, or fast-forward. An interrupted Adapter command retries that exact cycle; after a successful release, a later validation retry increments the cycle and must activate resources again. Adapter uncertainty preserves the batch and base for `batch recover`.
- DWW runs combined Full validation only after batch activation, verifies successful release, the composed head, and the base snapshot again, then fast-forwards the clean base.
- `batch reconcile` may freeze a smaller tail only from pending candidates in one exact base-and-policy lane after that lane has zero persisted modifying producers for the full quiet period. A new Start and tail freeze share one admission lock; whichever wins defines the next immutable generation.
- The coordinating native task schedules a host heartbeat for `next_reconcile_at`. A host without reliable scheduling cannot claim automatic quiet-tail support. `Finish`, `Abandon`, and `SessionEnd` may wake the same check but do not prove completion.
- There is no candidate-age or maximum-wait auto-seal. An active producer keeps the tail open until it reaches a recorded terminal state. An explicit user, deployment, or downstream dependency request may run `batch reconcile --force --cause user|deploy|dependency` and freezes only the current exact pending snapshot.
- `batch seal --candidate <id> ...` remains the exact-list compatibility/recovery interface. Both seal intent and batch execution are idempotent; repeated calls never create a duplicate generation. After diagnosing and changing an external blocker, an unchanged retained candidate list may form exactly one reviewed successor with `--after-failed-batch <id>`; the predecessor must be failed and its ordered list must match exactly.
- A deterministic failed generation or complete deterministic profile failure is retained and never blindly rerun with unchanged inputs. When composition identifies one conflicting candidate, run `candidate repair --candidate <id>` to prepare an idempotent managed repair on the latest base; continue without user interruption only when code, contracts, and tests determine one result, and stop after two repair generations.
- Use `start --supersedes <candidate-id>` for other reviewed repair work. Unchanged compatible retained candidates may be explicitly reused in a new generation.
- `candidate withdraw` removes an unsealed pending or retained candidate. `batch recover` resumes only an interrupted nonfailed generation. `batch retire --batch <id>` may idempotently remove only the exact clean detached worktree of an already failed generation; it preserves candidate refs and audit facts. For explicitly approved maintenance of an old failed dedicated batch whose candidates are all `superseded`, `batch retire --fast --batch <id>` stages the exact directory and skips only per-file dependency content hashes after the same Git, identity, link, protected-content, and unknown-content preflight. It never changes normal retirement semantics or candidate facts. Use read-only `batch metrics` before changing batch size.

The task snapshots its integration policy at Start. Missing integration policy in a pre-upgrade repository remains legacy direct, and pre-upgrade explicit candidates never become eligible for automatic sealing merely because configuration changes. Explicit direct and explicit-seal modes are compatibility paths, not the recommended new-user flow.

Reusable batches share one detached `solo-ai-integration` workspace. Ordinary success or failure returns it after durable Git results and confirmed resource release, retaining dependencies without content scans or hashes. Unknown content, conflicts or uncertain ownership preserve the scene; old operations never touch a newer owner. Physical deletion is separate maintenance. Already-started tasks and configuration without `worktree_mode` retain dedicated compatibility; opt in with a compatible Adapter. See the [reusable-workspace lifecycle](references/lifecycle.md#reusable-integration-workspace) before recovery or migration.

## Task-anchor lifetime and durable facts

The anchor remains until the Git result reaches its real terminal boundary:

- direct integration succeeds;
- the candidate's full, quiet-tail, or explicit-tail batch succeeds;
- the pending candidate is explicitly withdrawn; or
- the task is explicitly abandoned.

Do not store chat transcripts, hidden reasoning, credentials, leases, or unrelated history in it. Only facts that future tasks must continue to obey belong in the repository's existing authoritative document: lasting product rules, public contracts, data models, permissions, architecture boundaries, stable responsibilities, or long-lived UI contracts. Ordinary fixes, implementation details, tests, builds, and validation receipts stay out of permanent task ledgers.

## Explicit remote publishing

DWW never publishes remotely. After a successful direct Finish or completed candidate batch, an explicit user request may be fulfilled as a separate operation from the clean base worktree: confirm branch and remote, run a normal push dry-run, then use an ordinary non-force push. Do not fetch, pull, force-push, delete remote refs, push tags, create a PR, or deploy without separate explicit authorization.

## Advanced current-worktree compatibility

`start --in-place` remains a compatibility path only when the user explicitly requests DWW's Commit/Ready/Finish safeguards in the current clean worktree and trusted session identity is available. It is not the ordinary meaning of choice 2. Follow [lifecycle.md](references/lifecycle.md) for binding and recovery requirements.

## Validation, cleanup, and references

- For project check onboarding or proof-cache migration, read [verification-reuse.md](references/verification-reuse.md). A fresh Full reuses only complete, pure checks; runtime effects and required artifacts cannot be replaced by old reports. Recovering an already-passed batch transaction is not a fresh Full.
- `verification.toml` schema 3 uses explicit argv arrays. All candidate paths require Ready coverage unless static-only policy is explicitly active.
- Development, Ready, and Full evidence are separate. Heavy profiles are valid only at Full. Eligible profiles with exact unchanged inputs reuse their content-addressed proof under the reuse contract above; a changed approval identity fails closed and writes a Git-common-dir field-level mismatch report for review. The machine-global weighted FIFO queue limits expensive validation. A validation child may reuse its active ancestor claim only when the explicit claim id and live OS ancestry both match; nested heavy under a normal claim fails closed.
- Optional `[runtime_adapter]` commands receive one final JSON-context path. Their exact argv and tracked `input_paths` are machine-approved. `release` gates candidate eligibility; paired `batch_activate`/`batch_release` gate combined Full and promotion using one dedicated batch port block; `runtime verify --candidate <id>` checks project-defined runtime effectiveness only after Git delivery. DWW never interprets project ports, databases, browsers, authentication, or deployment semantics.
- Finish never removes dependencies or caches. `prune-slot` requires a reviewed generation-bound plan; protected data, links, path drift, or unknown content stop deletion.
- Existing mature workflows cross the delegated seam only through the tracked, locally approved bounded adapter contract.

Read [configuration.md](references/configuration.md), [lifecycle.md](references/lifecycle.md), [task-governance.md](references/task-governance.md), and [safety.md](references/safety.md) before changing policy or handling an exception. For delegated adoption, also read [delegated-migration.md](references/delegated-migration.md).
