---
name: develop-with-worktrees
description: "Use for any Git-repository task that may modify files, including preparing an implementation plan that will lead to repository changes. Route lifecycle ownership, anchor and isolate the task, save immutable source candidates, integrate affected three-candidate batches, and reconcile only explicitly requested tails. Pure read-only analysis does not claim a slot."
---

# Develop with Worktrees

DWW is a host-neutral Git safety lifecycle. It owns repository routing, local task identity, task anchors, worktree isolation, exact-path commits, validation evidence, candidate publication, integration, cleanup, and recovery. It does not own user-facing task decomposition or worker scheduling; use the host's native task/subagent facilities for those concerns.

## Plan without overdesign

Use this skill before writing a solution for a repository change, not only after the plan is complete. Do not overdesign: start from the observed need, existing contracts, and stated acceptance criteria, then choose the simplest implementation that can satisfy them without losing user work or bypassing required checks. A new persistent layer, abstraction, workflow, or human gate needs an observed requirement that the current mechanism cannot meet, plus a testable reason. Do not widen product scope merely because a more general system is possible.

Read-only investigation still runs `route` when ownership matters, but it creates no slot or anchor. Create a root anchor only after the user explicitly confirms a complete implementation plan or asks to proceed with it.

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

Use one Start-created task and worktree for all phases of the same change. Validate each behavior in a tight order: syntax or static checks, new unit tests, affected focused regressions, then the relevant test group. Development checks are chosen for the change; the default Finish saves the source candidate without turning that work into another project-test gate. Do not rerun an unchanged failing command without changing the input, environment, or diagnosis. Keep the anchor current when scope, acceptance, phase results, or a material blocker changes.

An explicit user request to start or continue work authorizes ordinary in-scope investigation, implementation, verification, exact lifecycle calls, and deterministic recovery until the work reaches its recorded boundary. Treat CLI values such as `--confirm`, `--accept`, and `--force` as identity or command safeguards, not as a reason to ask the user again. Ask only when the current request and repository contract cannot decide a material product, permission, migration, deletion, security, or external-side-effect choice.

## Task orchestration belongs to the host

For multiple independently verifiable outcomes, use the host's native task, subagent, dependency, wait, and status facilities. Each writing worker still receives one routed DWW lifecycle task and its own worktree. DWW does not create a second DAG, controller identity, worker dashboard, or scheduling state.

The legacy `dww orchestrate` command family is drain-only compatibility. Existing batches may be inspected, completed, paused, resumed, handed over, or cancelled as supported, but do not create a new orchestration batch, add tasks, or create repairs there.

Do not confuse task orchestration with candidate integration batches:

- native orchestration answers “who works on which outcome and when?”;
- DWW candidate batches answer “which exact verified Git candidates are intentionally combined and promoted together?”.

## Confirmed-objective root anchors

When the user explicitly confirms a complete implementation plan, says to set that plan as the objective, or asks to proceed with it, create one root anchor before the first related child `Start`. This applies even when the first child may later turn out to be the only task: the point is to preserve the confirmed objective, not to predict task count. Put the complete final plan in a UTF-8 plain file below the repository, or pass an explicit absolute plain file outside it, then create the root idempotently with the host's stable request identifier:

```text
uv run --script <DWW> --repo <repository-or-worktree> root-anchor create \
  --purpose <original-objective> --target <implementation-target> \
  --scope <explicit-boundary> --acceptance <acceptance-criteria> \
  --plan-file <complete-confirmed-plan.md> --plan-source <user-confirmation> \
  --request-id <stable-host-request-id>
```

The root keeps the full confirmed plan verbatim, including its own Markdown headings and code examples, together with immutable purpose/baseline facts, a plan version, explicit user-confirmed amendments, current progress, and the final overall outcome. DWW applies no byte, line, or character quota to root/task anchors, their input files, their full prior versions, or the exact cross-repository state needed to close a root; real filesystem, permission, and memory failures remain normal errors. Before a plan-changing root update overwrites the current file, DWW atomically saves the complete prior version under the origin common-dir's `solo-ai/root-anchor-history/<root-id>/`; a legacy guide root first upgraded by `root-anchor amend --plan-file` keeps its pre-structured text as a separate legacy snapshot, so the new confirmed plan can remain version 1. Progress, outcome, and child registration do not create history. The input file is only a safe handoff into DWW; after creation the root is the local source of truth. Do not create a root merely for discussion, read-only investigation, or a small request that has no separately confirmed plan. The host decides whether confirmation happened; DWW does not infer it from chat.

An explicit user amendment either replaces the effective plan with `root-anchor amend --plan-file ... --source ... --summary ... --expected-sha256 ...`, or appends its UTF-8 words verbatim with the mutually exclusive `--change-file ...`. These plan and change inputs may use the same explicit external plain-file path rule as creation; acceptance evidence stays under the managed repository. Both paths advance the plan version, retain the earlier root version, append a source-tagged amendment record, and reset overall acceptance to pending. `root-anchor progress` changes execution status only. A generic structured `root-anchor update` may change the full plan, target, scope, or acceptance criteria only with the next version and change record; its common write path also resets acceptance, and it cannot grant `accepted` or `cancelled`. Technical choices that do not change the user's purpose, boundaries, or acceptance criteria stay in the child task anchor and code; they are not a silent root-plan rewrite. Do not copy a full root plan into every child anchor.

The root is stored under the originating repository Git common-dir. It is not committed and never creates a `scope_id`, candidate group, task DAG, worker schedule, batch boundary, or cross-worktree atomicity. A child in that repository starts with `start --root-anchor <root-id>`. A child in another repository binds the same root with `start --root-anchor <root-id> --root-anchor-file <absolute-root-anchor-path>`. An already active or ready legacy child can instead use `anchor bind-root --task <task-id> --lease <lease> --root <root-id> [--root-anchor-file <absolute-root-anchor-path>]`; the same exact binding is idempotent and a different root is rejected. Candidate repair inherits the source task's root binding automatically. DWW accepts only the exact non-linked `solo-ai/root-anchors/<root-id>.md` file, persists its locator in the child state, and records that child’s exact DWW state locator in the root. It never searches repositories or creates a duplicate root.

When a structured root is supplied at Start or bound to an existing active task, the normal terminal result prints the complete root body once and records that read internally; it does not ask for a receipt or treat the record as proof that an AI understood every word. On continuation, model/context recovery, a root-version change, or candidate repair, the host invokes `anchor refresh-root` once; it returns the current task anchor and complete root body together, then records the current version without copying a SHA or version. Ordinary anchor and root-anchor queries return compact identity, path, digest, version, byte-size, and state summaries; `--content` returns exactly one full body, never a duplicate plan field. Root create, update, amend, progress, and accept are likewise compact unless `--content` is requested. Use `root-anchor show --version <n> --content` for one exact full historical version. A bound structured root with a stale recorded version rejects Commit, actual Ready, and Finish candidate publication only; the host refreshes and retries without losing work, rather than treating it as human approval or a gate on every edit. DWW alone maintains the external-child registry. root-anchor close rejects nonterminal children, ambiguous/missing child state, and candidate-published children whose exact lineage has not been integrated or explicitly withdrawn. For a confirmed-plan root it also rejects closure until root-anchor accept records the overall checked outcome for the current plan version, then removes the root and its history. Candidate publication, host idleness, and task counts never count as delivery.

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

- Choice 1: run `choose --mode isolated`. Supply `--verification-file <reviewed-schema-3-toml>` when the project already has a reviewed validation policy; otherwise initialization discovers commands and creates one conservative integration Full profile, or internal static checks when no command is found.
- Choice 2: when a trusted session identifier is available, run `choose --mode current-task --session <id>` and otherwise explain that this optional session-bound bypass is unavailable without Hook context. Do not weaken managed safety to simulate it.
- Choice 3: run `choose --mode current-repository`; this writes only local preference state.

If a mature workflow appears before `choose`, the command returns `deferred` and writes no DWW state.

## Managed task lifecycle

When modifying intent is clear, start proactively:

```text
uv run --script <DWW> --repo <repo> start --name <purpose> --target <implementation-target> --scope <boundary> --acceptance <criteria> [--request-id <stable-caller-id>]
```

`Start` writes the target, scope, and acceptance into its first task anchor before the task becomes active. Existing programmatic callers that omit these options receive narrow non-placeholder defaults for compatibility, but hosts should pass the reviewed facts. It returns the worktree, private lease, immutable base identity, and task-anchor path; when bound, its ordinary terminal output also prints the complete root plan once after those fixed headers. When a project Runtime Adapter configures `activate`, DWW first creates the exact isolated task, branch, worktree, and anchor, then asks the project to establish its runtime identity before Start returns the task as active. A failed activation remains retryable through the same `request_id` or `recover`; it never silently reallocates the task. Never expose the lease to another worker.

If the approved `activate` implementation itself is defective, do not edit the still-`starting` worktree or bypass DWW. After inspecting the failure, run `recover --task <id> --repair-runtime-adapter --path <exact-adapter-input>`, repeating `--path` only for the exact files required. This explicit path requires the exact persisted failed activation receipt and is available only for a clean, exact pre-activation isolated task with both `activate` and `release` configured. Every named file must already be tracked and covered by the approved `runtime_adapter.input_paths`, except that `.solo-ai/config.toml` may repair the command declaration itself. The same task can commit only that frozen exact list. Ready and batch Full remain mandatory, and the repaired `release` must successfully clean any partial activation before the candidate can become eligible.

For a batched isolated task whose immutable candidate is already `held` because its `release` failed, the same explicit recovery form is narrower still: each `--path` must be a changed, tracked Adapter input in a clean delivered base that has advanced from the task baseline. DWW keeps the candidate, task, context worktree, and candidate identity unchanged; it runs only the approved Adapter input from that delivered base, records the successful release receipt in the existing publication transaction, and resumes that exact publication. It never copies files into the candidate worktree, silently swaps an Adapter, or permits this path for a pending, sealed, integrated, or ambiguous candidate.

1. Work only in the returned worktree.
2. Start has already written the known task facts into `<git-common-dir>/solo-ai/task-anchors/<task-id>.md`; read it before work after a handoff or context loss, and update it only when the execution contract, progress, or a material blocker changes. It is never committed.
3. After context compression, model change, handoff, or continuation, reread the task anchor and use `anchor refresh-root` once when it is root-bound before the next modification.
4. Commit exactly the reviewed paths with repeated `--path`; never use broad staging.
5. Use `plan` or `verify --level development` when useful.
6. Run `ready` when it is useful development evidence or a legacy project requires it, then `finish` with the same task and lease. New default batched projects may finish directly from `active` after an exact commit.
7. Use `anchor show --task --content` to reread the current context and `anchor update --task --lease --file --expected-sha256` only when a reviewed revision is needed. Run it from the recorded task or base worktree, with an input file below that same worktree.

If a previous host session ended with an isolated task still holding uncommitted work, never extract its lease or recreate the task. After checking that no operation or validation is live, use the explicit `handoff --task <id> --confirm <task-id>:<branch>:<head>` path. It transfers only the lease after exact identity checks and preserves every retained file; see [lifecycle.md](references/lifecycle.md).

Ready refuses a missing, linked, non-UTF-8, identity-mismatched, or origin-mismatched anchor. Start persists the original purpose and baseline; `show` reports whether a legacy anchor has verified origin. A repeated update whose content is already current is a successful no-op, while Ready permits changes only to the complete `Current progress` block. For a genuine pre-anchor task, review its objective, target, scope, and acceptance, then use `anchor adopt` with the exact task-id confirmation; never invent those fields automatically. Ready validates the exact clean candidate and synchronizes only the recorded local base. DWW never fetches, pulls, pushes, opens a PR, rebases, squashes, amends, or rewrites history.

## Candidate-first integration

New repositories use `integration.mode = "batched"`, `worktree_mode = "reusable"`, `batch_size = 3`, `candidate_capacity = 10`, `seal_policy = "auto_full"`, `candidate_validation = "batch"`, and `tail_policy = "explicit"`:

- An optional project Runtime Adapter may activate project-owned runtime identity after the isolated worktree is exact and before Start returns. DWW supplies the task, slot, base, worktree, and deterministic slot port block; the project still owns every concrete port, database, browser, and service decision.
- Finish creates one immutable source candidate ref without a separate project test. A configured project runtime Adapter must release project-owned resources before the candidate becomes eligible; only then does Finish release the worktree slot, leave the base unchanged, and keep the anchor.
- Publishing the configured third eligible candidate atomically freezes the oldest three candidates in that exact frozen-base-and-policy lane. That Finish then runs or waits for the persisted integration generation; no Hook or resident process is required.
- While one batch owns the integration turn, later Finish calls may keep publishing into the remaining bounded pool. A second batch does not freeze against the same stale base; `reconcile` resumes the existing batch first.
- DWW composes the exact frozen tree differences in a dedicated integration worktree. A configured paired batch Runtime Adapter establishes project-owned Full-validation resources from the exact batch identity, positive persisted `runtime_cycle`, and dedicated port block, then releases them with the same cycle and recorded validation outcome before DWW may fail, retry, or fast-forward. An interrupted Adapter command retries that exact cycle; after a successful release, a later validation retry increments the cycle and must activate resources again. Adapter uncertainty preserves the batch and base for `batch recover`.
- DWW runs only the repository-declared Ready and integration Full profiles affected by the combined changes after batch activation, reusing complete pure checks whose declared inputs still match. It then verifies release, the composed head, and the base snapshot before fast-forwarding. Broad regression and stress remain manual diagnostics, not normal or release gates.
- `batch reconcile --force --cause round-complete --reason <one-line-basis>` freezes a smaller tail only from pending candidates in one exact frozen-base-and-policy lane after the coordinator has completed this round. `user`, `deploy`, and `dependency` are explicit immediate-integration reasons. A new Start and tail freeze share one admission lock; whichever wins defines the next immutable generation.
- There is no candidate-age, quiet-period, or host-idle auto-seal. `Finish`, `Abandon`, `SessionEnd`, Hook delivery, and task counts do not prove that a round is complete.
- `batch seal --candidate <id> ...` remains the exact-list compatibility/recovery interface. A smaller list records the same cause and one-line reason as reconcile, so it cannot bypass tail rules. Both seal intent and batch execution are idempotent; repeated calls never create a duplicate generation. After diagnosing and changing an external blocker, an unchanged retained candidate list may form exactly one reviewed successor with `--after-failed-batch <id>`; the predecessor must be failed and its ordered list must match exactly.
- A deterministic failed generation or complete deterministic profile failure is retained and never blindly rerun with unchanged inputs. When composition identifies one conflicting candidate, run `candidate repair --candidate <id>` to prepare an idempotent managed repair on the latest base; continue without user interruption only when code, contracts, and tests determine one result, and stop after two repair generations.
- A published candidate ends one developer task’s current coding round, but not the requested delivery. In Codex Desktop, `start`, `finish`, recovery, and batch actions automatically use the injected `CODEX_THREAD_ID`; `--host-kind <kind> --host-thread <id>` remains an explicit compatibility override for another host. The call that actually freezes the batch becomes its coordinator, never the source candidate by inference. For an attributed composition conflict, read `host-handoff status`; when it requests dispatch, run `host-handoff repair dispatch`, send its returned payload with the native task message tool, then record the actual result with `host-handoff repair delivery`. The assignee uses `repair claim` then `repair prepare`. A coordinator may use `repair attribute --evidence ...` only after reviewing a failed validation's exact test, log, and candidate evidence; DWW never attributes that business repair automatically. When the repair candidate is published, its task uses `result-dispatch` and `result-delivery` to notify the current coordinator. A request becomes resolved only after that candidate is actually integrated into the base. An unavailable coordinator or source is replaced only by the explicit revision-checked batch or reasoned repair `take-over` command. DWW returns message payloads and receipts; it never calls the host API itself.
- Use `start --supersedes <candidate-id>` for other reviewed repair work. Unchanged compatible retained candidates may be explicitly reused in a new generation.
- `candidate withdraw` removes an unsealed pending or retained candidate. `batch recover` resumes only an interrupted nonfailed generation. `batch retire --batch <id>` may idempotently remove only the exact clean detached worktree of an already failed generation; it preserves candidate refs and audit facts. For explicitly approved maintenance of an old failed dedicated batch whose candidates are all `superseded`, `batch retire --fast --batch <id>` stages the exact directory and skips only per-file dependency content hashes after the same Git, identity, link, protected-content, and unknown-content preflight. It never changes normal retirement semantics or candidate facts. Use read-only `batch metrics` before changing batch size.

The task snapshots its integration policy at Start. Missing integration policy in a pre-upgrade repository remains legacy direct, and pre-upgrade explicit candidates never become eligible for automatic sealing merely because configuration changes. Explicit direct and explicit-seal modes are compatibility paths, not the recommended new-user flow.

Reusable batches share one detached `solo-ai-integration` workspace. Ordinary success or failure returns it after durable Git results and confirmed resource release, retaining dependencies without content scans or hashes. Unknown content, conflicts or uncertain ownership preserve the scene; old operations never touch a newer owner. Physical deletion is separate maintenance. Already-started tasks and configuration without `worktree_mode` retain dedicated compatibility; opt in with a compatible Adapter. See the [reusable-workspace lifecycle](references/lifecycle.md#reusable-integration-workspace) before recovery or migration.

## Task-anchor lifetime and durable facts

The anchor remains until the Git result reaches its real terminal boundary:

- direct integration succeeds;
- the candidate's full or explicit-tail batch succeeds;
- the pending candidate is explicitly withdrawn; or
- the task is explicitly abandoned.

Do not store chat transcripts, hidden reasoning, credentials, leases, or unrelated history in it. Only facts that future tasks must continue to obey belong in the repository's existing authoritative document: lasting product rules, public contracts, data models, permissions, architecture boundaries, stable responsibilities, or long-lived UI contracts. Ordinary fixes, implementation details, tests, builds, and validation receipts stay out of permanent task ledgers.

## Explicit remote publishing

DWW never publishes remotely. After a successful direct Finish or completed candidate batch, an explicit user request may be fulfilled as a separate operation from the clean base worktree: confirm branch and remote, run a normal push dry-run, then use an ordinary non-force push. Do not fetch, pull, force-push, delete remote refs, push tags, create a PR, or deploy without separate explicit authorization.

## Advanced current-worktree compatibility

`start --in-place` remains a compatibility path only when the user explicitly requests DWW's Commit/Ready/Finish safeguards in the current clean worktree and trusted session identity is available. It is not the ordinary meaning of choice 2. Follow [lifecycle.md](references/lifecycle.md) for binding and recovery requirements.

## Validation, cleanup, and references

- For project check onboarding or proof-cache migration, read [verification-reuse.md](references/verification-reuse.md). A fresh Full reuses only complete, pure checks; runtime effects and required artifacts cannot be replaced by old reports. Recovering an already-passed batch transaction is not a fresh Full.
- `verification.toml` schema 3 uses explicit argv arrays. For onboarding, `init` and isolated `choose` accept a reviewed `--verification-file`; otherwise automatic discovery creates a conservative integration-Full profile with reuse disabled. All changed batch paths require Ready or integration-Full coverage unless static-only policy is explicitly active. An optional `stress-verification.toml` may contain only explicit Stress profiles; it is never a candidate Finish gate.
- Development, Ready, Full, and explicit Stress evidence are separate. Heavy profiles are valid only at Full or Stress. Full profiles default to `full_scope = "integration"`, which is the normal batch gate; `full_scope = "complete"` is an explicit diagnostic command. `dww verify --level stress` selects only the pressure profiles; it does not replace batch Full. Eligible complete pure profiles with unchanged declared inputs reuse their content-addressed proof across tasks and batches in one repository; a changed approval identity fails closed and writes a Git-common-dir field-level mismatch report for review. The machine-global weighted FIFO queue limits expensive validation. A validation child may reuse its active ancestor claim only when the explicit claim id and live OS ancestry both match; nested heavy under a normal claim fails closed.
- Optional `[runtime_adapter]` commands receive one final JSON-context path. Their exact argv and tracked `input_paths` are machine-approved. `release` gates candidate eligibility; paired `batch_activate`/`batch_release` gate combined Full and promotion using one dedicated batch port block; `runtime verify --candidate <id>` checks project-defined runtime effectiveness only after Git delivery. DWW never interprets project ports, databases, browsers, authentication, or deployment semantics.
- Finish never removes dependencies or caches. `prune-slot` requires a reviewed generation-bound plan; protected data, links, path drift, or unknown content stop deletion.
- Existing mature workflows cross the delegated seam only through the tracked, locally approved bounded adapter contract.

Read [configuration.md](references/configuration.md), [lifecycle.md](references/lifecycle.md), [task-governance.md](references/task-governance.md), and [safety.md](references/safety.md) before changing policy or handling an exception. For delegated adoption, also read [delegated-migration.md](references/delegated-migration.md).
