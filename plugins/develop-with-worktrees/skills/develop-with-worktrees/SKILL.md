---
name: develop-with-worktrees
description: "Use for any Git-repository task that may modify files. Default to an isolated local worktree lifecycle; only use the session-bound current-worktree mode when the user clearly requires it. Do not use for read-only analysis."
---

# Develop with Worktrees

Use this lifecycle for every modifying Git task. Explain in the user's language. The core CLI is host-neutral; the trusted Codex hook is a hard write guard for Codex-supported local tool paths, not an operating-system sandbox.

Set `DWW` to this skill's absolute `scripts/dww.py` and invoke it only with `uv`:

```text
uv run --script <DWW> --repo <repository-or-worktree> <subcommand>
```

## Before writing

- For read-only analysis, do not claim a task.
- Read repository instructions, then run `doctor` or `status` when mode is uncertain.
- If a mature workflow is detected, defer completely: no policy, slot, or lifecycle state is written.
- In an uninitialized repository, run `init` to show the plan. Only the user may choose `init --accept`, `init --accept-static-only`, or `init --decline`.
- In Codex, after plugin install or a hook update, tell the user to open `/hooks`, trust this plugin, and start a new task. Until trusted, do not claim current-worktree writes are protected.

## Default modifying task

1. Proactively run `start --name <purpose>` when a modifying intent is clear. It derives from the invocation worktree's current local branch.
2. Work only in the returned worktree. Keep its lease private.
3. Commit exactly the reviewed paths with repeated `--path`; never use unscoped staging.
4. Use `plan` or `verify --level development` as useful feedback. Slow estimates are advice only.
5. Run `ready`, then `finish` with the same task and lease.

Ready/Finish synchronize only the recorded base branch. A deleted, rewound, or rewritten base requires explicit `retarget`; Finish only fast-forwards the recorded clean base worktree. It never fetches, pulls, pushes, opens a PR, rebases, squashes, amends, or rewrites history.

## Explicit current-worktree task

Use this only if the user clearly says that execution must stay in the current worktree (for example, “直接在主分支改”, “只在当前工作树执行”). “在主分支完成” is ambiguous: ask whether current-worktree execution is required or isolation is acceptable.

1. Obtain the current Codex session identifier from the trusted SessionStart hook context, then run `start --in-place --name <purpose> --session <id>` in that current worktree.
2. It requires a clean Git worktree; ignored caches/data may remain. It creates no branch and no slot.
3. Pass the same `--session <id>` to `commit`, `verify`, `ready`, `finish`, and `abandon`. Commit still requires the exact changed path list.
4. Never use ordinary `recover` for it. On a session/branch/HEAD mismatch, preserve everything. If the prior Codex task ended, explicitly confirm `resume-in-place` after checking the recorded branch and expected HEAD; it rotates the lease/session, clears Ready evidence, and changes no project files. A mismatch still needs manual restoration first.
5. Finish never merges, changes checkout, resets, cleans, deletes a branch, or deletes ignored runtime data. It validates relative to the start commit and its proof is task-scoped.

Other isolated tasks may Start while an in-place task is active. Do not promise their Finish will proceed: the CLI blocks a Finish into the same base until current-worktree work ends, then synchronization and affected validation occur normally.

## Validation, cleanup, and limits

- `verification.toml` supports schema 3 only. Commands are registered argv arrays, never shell strings.
- Development, Ready, and Full evidence are separate. All changed candidate paths need Ready coverage unless static-only is explicit.
- Validation uses a machine-global weighted FIFO queue. `settings --validation-capacity auto|1..4` is local-only.
- Finish never removes dependencies or caches. `prune-slot` requires a reviewed exact plan and digest; `.env*`, symlinks, junctions, or changes stop the whole deletion.

Read [configuration.md](references/configuration.md), [lifecycle.md](references/lifecycle.md), and [safety.md](references/safety.md) before changing policy or handling an exception.
