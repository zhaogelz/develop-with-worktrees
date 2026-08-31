---
name: develop-with-worktrees
description: "Use for any Git-repository task that may modify files. Route lifecycle ownership first, then use first-class task anchors, safe isolated worktrees, exact validation, and either direct or explicit candidate-batch integration. Let the host's native task/subagent system own task orchestration. Do not use for read-only analysis."
---

# Develop with Worktrees

DWW is a host-neutral Git safety lifecycle. It owns repository routing, local task identity, task anchors, worktree isolation, exact-path commits, validation evidence, candidate publication, integration, cleanup, and recovery. It does not own user-facing task decomposition or worker scheduling; use the host's native task/subagent facilities for those concerns.

Set `DWW` to this skill's absolute `scripts/dww.py` and invoke it only with `uv`:

```text
uv run --script <DWW> --repo <repository-or-worktree> <subcommand>
```

## Hooks are optional hardening

The CLI lifecycle must remain correct with no Hook installed or trusted. A trusted `SessionStart` Hook may provide route context and a trusted `PreToolUse` Hook may hard-deny unsafe writes on supported Codex local-tool paths, but neither is a source of task completion, candidate sealing, cleanup, or recovery truth. Never use `SessionEnd`, idle time, or Hook delivery as a correctness condition.

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

`Start` returns the worktree, private lease, immutable base identity, and task-anchor path. A repeated `request_id` returns the same managed task instead of consuming another slot. Never expose the lease to another worker.

1. Work only in the returned worktree.
2. Read and update the generated anchor before editing. It lives at `<git-common-dir>/solo-ai/task-anchors/<task-id>.md`, is never committed, and records objective, target, baseline, scope, acceptance, and progress.
3. After context compression, model change, handoff, or continuation, reread the anchor before the next modification.
4. Commit exactly the reviewed paths with repeated `--path`; never use broad staging.
5. Use `plan` or `verify --level development` when useful.
6. Run `ready`, then `finish` with the same task and lease.

Ready refuses a missing, linked, oversized, non-UTF-8, or identity-mismatched anchor. It validates the exact clean candidate and synchronizes only the recorded local base. DWW never fetches, pulls, pushes, opens a PR, rebases, squashes, amends, or rewrites history.

## Direct and batched integration

`integration.mode = "direct"` is the generic default. A successful Finish fast-forwards the recorded clean base worktree, releases the slot, and deletes the anchor.

A repository may explicitly configure `integration.mode = "batched"`. The defaults are `batch_size = 5` and `candidate_capacity = 10`:

- Finish validates and publishes one immutable candidate ref, releases its worktree slot, leaves the base unchanged, and keeps its anchor.
- `candidate status` shows the bounded pool.
- Only `batch seal --candidate <id> ...` freezes a generation. List every intended candidate explicitly; one seal accepts at most the configured batch size.
- DWW composes the exact frozen tree differences in a dedicated integration worktree, runs combined Full validation, verifies the base snapshot again, then fast-forwards the clean base.
- A later revision never changes a sealed generation. Use `start --supersedes <candidate-id>` for a repair task and seal a new explicit generation.
- `candidate withdraw --candidate <id>` removes only a pending candidate not captured by an active batch.
- `batch recover --batch <id>` resumes only an interrupted recorded generation. A deterministically failed generation is not automatically rerun.

Never seal because the pool reached five, because no worker appears active, because a timer elapsed, or because a session ended. The caller decides the exact intended set. On conflict or final-validation failure, the base remains unchanged; diagnose, publish a repair candidate, and explicitly seal a new generation. Unchanged compatible candidates may be reused only by naming their exact identities again.

## Task-anchor lifetime and durable facts

The anchor remains until the Git result reaches its real terminal boundary:

- direct integration succeeds;
- the candidate's explicit batch succeeds;
- the pending candidate is explicitly withdrawn; or
- the task is explicitly abandoned.

Do not store chat transcripts, hidden reasoning, credentials, leases, or unrelated history in it. Only facts that future tasks must continue to obey belong in the repository's existing authoritative document: lasting product rules, public contracts, data models, permissions, architecture boundaries, stable responsibilities, or long-lived UI contracts. Ordinary fixes, implementation details, tests, builds, and validation receipts stay out of permanent task ledgers.

## Explicit remote publishing

DWW never publishes remotely. After a successful direct Finish or completed candidate batch, an explicit user request may be fulfilled as a separate operation from the clean base worktree: confirm branch and remote, run a normal push dry-run, then use an ordinary non-force push. Do not fetch, pull, force-push, delete remote refs, push tags, create a PR, or deploy without separate explicit authorization.

## Advanced current-worktree compatibility

`start --in-place` remains a compatibility path only when the user explicitly requests DWW's Commit/Ready/Finish safeguards in the current clean worktree and trusted session identity is available. It is not the ordinary meaning of choice 2. Follow [lifecycle.md](references/lifecycle.md) for binding and recovery requirements.

## Validation, cleanup, and references

- `verification.toml` schema 3 uses explicit argv arrays. All candidate paths require Ready coverage unless static-only policy is explicitly active.
- Development, Ready, and Full evidence are separate. The machine-global weighted FIFO queue limits expensive validation.
- Finish never removes dependencies or caches. `prune-slot` requires a reviewed generation-bound plan; protected data, links, path drift, or unknown content stop deletion.
- Existing mature workflows cross the delegated seam only through the tracked, locally approved bounded adapter contract.

Read [configuration.md](references/configuration.md), [lifecycle.md](references/lifecycle.md), [task-governance.md](references/task-governance.md), and [safety.md](references/safety.md) before changing policy or handling an exception. For delegated adoption, also read [delegated-migration.md](references/delegated-migration.md).
