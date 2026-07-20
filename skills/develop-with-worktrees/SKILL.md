---
name: develop-with-worktrees
description: "Use for any Git-repository task that may modify files. Before editing, apply this local-first lifecycle: detect and defer to a repository's mature workflow, otherwise obtain the one-time adoption decision, then isolate work in a managed worktree, commit exact reviewed paths, validate, and safely integrate. Do not use for read-only analysis."
---

# Develop with Worktrees

Use this plugin's lifecycle for a modifying Git task. Explain in the user's language. Keep repository policy, commands, and commit messages consistent with the target repository's conventions.

The plugin hook is a Codex guardrail that reminds you before supported local write tools. It is not an OS-level lock and Codex's current `PreToolUse` protocol cannot portably hard-deny a tool call. The CLI below is the hard lifecycle gate. Never claim that manual edits, another IDE, or an untrusted/disabled hook are prevented.

## Locate and run the lifecycle

Set `DWW` to this skill's absolute `scripts/dww.py` path. Invoke it only through uv:

```text
uv run --script <DWW> --repo <path-inside-target-repository> <subcommand>
```

Do not install the runner globally or into the target repository. Do not use an undocumented bypass flag; none exists.

## Decide before a write

- Read-only inspection, planning, review, and diagnosis: stay in place and do not claim a slot.
- First inspect the target repository's own instructions.
- Run `doctor` or `status` when mode is uncertain.
- If a mature workflow is detected (including `scripts/worktree-flow.ps1` or Worktrunk), defer completely. Do not create `.solo-ai`, edit `AGENTS.md`, claim a slot, or try to migrate it automatically.
- If local mode is disabled, respect it. Only the user may ask to run `enable`.
- If uninitialized, show the one-time plan first:

```text
uv run --script <DWW> --repo <repo> init
```

This command is read-only until the user chooses. On acceptance, run `init --accept`; if no validation command exists, require the user's explicit `--accept-static-only`. If the user declines, run `init --decline`; this writes only a local disable preference and must not be bypassed by an agent.

`init --accept` creates exactly one bootstrap commit: `.solo-ai/config.toml`, `.solo-ai/verification.toml`, and the exact managed `AGENTS.md` block. A dirty primary worktree is never stashed, committed, discarded, or copied: the bootstrap stays pending in local Git metadata and is brought into the default branch by the first successful Finish after the primary becomes clean.

Every machine must approve the complete normalized plan once. After a clone, or when config/tool/platform/dependency/validation policy changes, run `approve --accept` from the checkout whose policy is being approved.

## Perform a modifying task

1. Run `start --name <short-purpose>`.
2. Record its task id, lease, branch, and returned worktree. The lease is secret-like operational state: do not put it in reports, files, or tickets.
3. Work only in that returned worktree. Re-read repository instructions there.
4. Run focused project checks while developing.
5. Inspect every changed path and commit only the exact reviewed list:

```text
uv run --script <DWW> --repo <worktree> commit --task <id> --lease <lease> --message <message> --path <path> [--path <path> ...]
```

The supplied paths must equal all task changes. Do not use `git add -A`, do not absorb unrelated files, and stop for user direction if the exact list is unclear.

6. Run `ready --task <id> --lease <lease>`.
7. Immediately run `finish --task <id> --lease <lease>` after Ready succeeds.

Ready predicts a merge before changing the task branch, then performs an ordinary merge of the latest local default branch when safe. It never rebases or auto-resolves a semantic conflict. Finish queues only integration (never Start), requires a clean primary worktree, uses one FIFO local lock, and fast-forwards only the local default branch. It never fetches, pulls, pushes, opens PRs, squashes, amends, or rewrites history.

Parallel slots never merge merely because they are active. A later task integrates only after it synchronizes with the then-current default branch and passes validation again.

## Validation and proofs

Policy commands are explicit argv arrays, not shell strings. Verification approval binds the Git-common identity, full normalized policy, profile paths and commands, declared input/environment closure, dependency locks, resolved executable paths and versions, and platform.

- Ready-to-Finish reuses only an exact candidate proof with persistent, content-addressed redacted logs.
- Cross-task profile reuse is off by default. It is allowed only when a profile explicitly declares `cross_task_reuse = true`, `external_state = "none"`, and a complete file/dependency/environment closure.
- Database, container, network, browser, time-sensitive, unknown, or otherwise external validation must not reuse across tasks.
- Static-only is explicit and must be described as static checks only; never say tests passed.

## Recover and clean up deliberately

- `status` masks leases. Add `--detailed` only for exact disk measurement.
- `recover --task <id>` is only for a user-confirmed stale operator. It refuses a live recorded operation and rotates the old lease.
- Fix a conflict or validation failure in the preserved task worktree, then commit and repeat Ready/Finish.
- `abandon --task <id> --lease <lease> --confirm <exact-id>` requires explicit user authorization to discard that task.
- `warm-slot --slot <01..05>` resets a clean detached idle slot to the latest local default branch, then serially runs only repository-declared dependency preparation. A maintenance lock prevents it from overlapping Start, pruning, or removal. Slots are lazy by default; their first checkout/dependency preparation can be slow.
- `prune-proofs`, `prune-logs`, and `prune-slot` are separate explicit `--confirm PRUNE` actions. They refuse active tasks/tickets; deleting logs invalidates proof reuse.
- `disable` / `enable` are per-machine preferences only. An agent may not self-disable to bypass a gate.

For a clean repository removal, first run `deinit --confirm DEINIT --message <repository-conventional-message>`. It refuses active tasks, queues, dirty primary state, changed managed markers, unknown untracked or ignored files, and protected `.env*` data. It prepares tracked policy removal, releases only exact managed worktrees, then fast-forwards the policy deletion and deletes exact local state. Only after a successful `deinit` may the user uninstall the Codex plugin.

Read [configuration.md](references/configuration.md), [lifecycle.md](references/lifecycle.md), and [safety.md](references/safety.md) before changing policy or handling an exceptional state.
