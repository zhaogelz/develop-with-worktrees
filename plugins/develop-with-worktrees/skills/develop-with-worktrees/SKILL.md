---
name: develop-with-worktrees
description: "Use for any Git-repository task that may modify files. Route first, let mature workflows win, and use one plain-language plan confirmation for complex multi-AI work; otherwise use safe isolated local worktrees. Do not use for read-only analysis."
---

# Develop with Worktrees

Use this lifecycle for every modifying Git task unless the user chooses ordinary current-directory development. Explain in the user's language. The core CLI is host-neutral; the trusted Codex hook is a hard write guard for Codex-supported local tool paths, not an operating-system sandbox.

Set `DWW` to this skill's absolute `scripts/dww.py` and invoke it only with `uv`:

```text
uv run --script <DWW> --repo <repository-or-worktree> <subcommand>
```

## Hook trust without repeated user work

Codex persists trust against the exact hook definition. Treat `hooks/hooks.json` as a stable compatibility contract: ordinary plugin, skill, and guard-script updates must not change it and must not cause a repeated trust request. Missing `SessionStart` context alone is not evidence that trust is missing; use the read-only route fallback without asking.

Only act when Codex actually reports that a new or changed hook needs review. Explain the exact guard behavior change in plain language and ask for one confirmation. After explicit approval, if the current host exposes UI control, use it to open `/hooks` and complete the trust action instead of instructing the user to click through the interface. If host UI control is unavailable, state that product-surface limitation and do not claim the hard guard is active.

The plugin and hook cannot approve their own trust. Never edit Codex trust storage, use `--dangerously-bypass-hook-trust`, or convert this personal plugin to an enterprise managed hook to avoid review.

## Route before any repository choice

At the first modifying intent, use the compact route already injected by the trusted `SessionStart` hook. If no route context is available, run exactly one read-only fallback:

```text
uv run --script <DWW> --repo <repository-or-worktree> --json route
```

Do not use the full `doctor` report for routing.

- `defer`: the repository's mature workflow has absolute priority over choices 2/3 and DWW policy. Do not ask the DWW repository-choice question, initialize DWW, or change DWW state. Follow the repository's own instructions.
- `disabled` or `current-task`: do not ask again or run the DWW lifecycle; use normal current-directory development.
- `managed`: proactively start the normal isolated task.
- `ask`: and only `ask`, show the single question below.

## Complex multi-AI work: one central conversation

Treat a request as complex only when it has multiple independently verifiable outcomes, real dependencies, or an explicit shared contract. Keep a simple request as one normal DWW task: do not create a batch, extra worker, review worker, or status dashboard by default.

For a complex request, the current Codex conversation is the **only controller**. First give the user a short, plain-language plan made of vertical outcomes (or a contract-first task followed by its consumers), including the result each task will make visible. Ask for one confirmation. Do not dispatch a worker, create a DWW task, or write orchestration state before that confirmation.

If route is `ask`, fold the default isolated-directory choice into this same plan confirmation: say that confirmation will use separate directories and local-only integration. After the user confirms, run `choose --mode isolated`, then create and confirm the orchestration batch. Do not show the separate three-choice prompt as well. If the user instead explicitly asks for one AI or current-directory work, follow that explicit choice and do not create a batch.

After confirmation, create one opaque controller identifier locally, pass it to `dww orchestrate plan` and `dww orchestrate confirm`, and never give it to workers. The scheduler may dispatch at most `min(5, configured DWW slots, idle DWW slots)` development tasks. Validation remains governed by DWW's machine-global weighted queue.

- Only the controller may call `orchestrate claim`, start workers, add an internal task, or hand off the controller. A worker receives one task and has no authority to spawn another worker.
- Give each worker exactly one writer task. Same-file predictions do not serialize work. Only an explicit high-risk `exclusive_resources` value (for example a migration, lockfile, or shared contract) serializes tasks.
- Let each worker use its own ordinary DWW lifecycle. Git-clean merges with Ready/Finish evidence integrate locally; never guess text or semantic conflicts. Assign a new repair task from the latest base to the original owner when possible, otherwise make a dedicated integration repair task.
- A blocked task stops only its dependents. Continue unrelated frontier tasks. Record a repair attempt only when the diagnosis changed; two unchanged failures, business ambiguity, safety, data, permission, or scope changes require the central conversation to ask the user.
- Record existing proof/receipt references with `orchestrate complete`. At batch end, run only a targeted combination check that lacks evidence; do not add a default full repository test or review AI.
- `orchestrate pause`, `resume`, `cancel`, and `take-over` preserve code and local state. Cancellation never deletes a branch or file. A future central conversation can inspect status, take over with the exact batch id, and continue; no resident daemon is implied.

`dww` is the adapter for a `managed` repository. A mature repository may participate only through an explicit `delegated` adapter chosen by its own workflow; never parse arbitrary instructions or invent an external command. If it already has an external orchestrator, fully defer.

## First modifying intent in an unchosen repository

Read-only analysis never asks a question or claims a task. For a simple request, after repository instructions and the compact route are read, when the result is `ask`, show exactly this one question:

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

Do not add an initialization, test-discovery, or static-validation question. Do not explain internal terms unless the user asks.

- Choice 1: run `choose --mode isolated`. It sets up the managed lifecycle once. If no automated test is found, it silently uses its internal basic checks; it does not ask again.
- Choice 2: obtain the session identifier from trusted hook context and run `choose --mode current-task --session <id>`. For the rest of this session, work in the current directory exactly as if this skill were absent: do not create policy files, start a task, or run Commit/Ready/Finish. A new task asks again.
- Choice 3: run `choose --mode current-repository`. It locally disables this repository on this machine without changing tracked files; do not initialize or run this skill later unless the user changes the choice.

If a mature workflow appears before `choose`, the command returns `deferred` for every mode and changes no DWW state.

The choices are local to the current clone/common Git directory. A different clone or machine chooses independently. Natural-language changes such as “以后使用独立目录开发” and “以后直接在当前目录开发” are explicit new choices; apply them with `choose` without repeating the prompt. “在主分支完成 / 合到 main / 提交到 main” is not a bypass: ask whether current-directory execution is required if the intent is unclear.

When choice 2 needs a writing child agent, pass its returned one-time delegation code only in that child’s task instruction. The child registers its own hook session with `choose --mode current-task --session <child-id> --delegate <code>` before writing. Do not reuse the code for an unrelated task. Codex currently supplies no reliable parent-agent hook identifier, so this explicit delegation is the only safe way to extend the one-task choice without opening concurrent unrelated tasks.

## Default managed task

For an adopted repository, proactively run `start --name <purpose>` when a modifying intent is clear. It derives from the invocation worktree's current local branch.

1. Work only in the returned worktree. Keep its lease private.
2. Commit exactly the reviewed paths with repeated `--path`; never use unscoped staging.
3. Use `plan` or `verify --level development` as useful feedback. Slow estimates are advice only.
4. Run `ready`, then `finish` with the same task and lease.

Ready/Finish synchronize only the recorded base branch. A deleted, rewound, or rewritten base requires explicit `retarget`; Finish only fast-forwards the recorded clean base worktree. It never fetches, pulls, pushes, opens a PR, rebases, squashes, amends, or rewrites history.

## Explicit post-Finish publishing

DWW and the orchestration layer never publish automatically. When the user explicitly asks to sync a completed result to a remote after a successful Finish, treat publishing as a separate operation outside the DWW lifecycle:

1. Work only from the clean recorded base worktree and confirm its current branch and remote.
2. Run a normal push dry-run for that current branch first.
3. If the dry-run succeeds, use an ordinary non-force push of that branch. Set its upstream only when it has none.

Do not fetch, pull, force-push, delete a remote ref, push tags, create a PR, or deploy unless the user separately and explicitly asks for that action. A failed or non-fast-forward dry-run stops publishing; preserve local work and report the remote divergence.

## Advanced guarded current-worktree task

`start --in-place` remains a compatibility path only when the user explicitly asks to retain DWW's exact Commit/Ready/Finish safeguards while using the current clean worktree. It is not the meaning of choice 2. Follow [lifecycle.md](references/lifecycle.md) for its session, identity, and recovery requirements.

## Validation, cleanup, and limits

- `verification.toml` supports schema 3 only. Commands are registered argv arrays, never shell strings.
- Development, Ready, and Full evidence are separate. All changed candidate paths need Ready coverage unless internal static-only policy is active.
- Validation uses a machine-global weighted FIFO queue. `settings --validation-capacity auto|1..4` is local-only.
- Finish never removes dependencies or caches. `prune-slot` requires a reviewed exact plan and digest; `.env*`, symlinks, junctions, or changes stop the whole deletion.

Read [configuration.md](references/configuration.md), [lifecycle.md](references/lifecycle.md), and [safety.md](references/safety.md) before changing policy or handling an exception.
