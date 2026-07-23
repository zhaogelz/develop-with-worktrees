---
name: develop-with-worktrees
description: "Use for any Git-repository task that may modify files. Use the local-first lifecycle: inspect or defer, adopt only with approval, start from the current local branch, commit exact paths, validate registered profiles, then Finish locally. Do not use for read-only analysis."
---

# Develop with Worktrees

Use the lifecycle for every modifying Git task. Explain in the user's language. The hook is only a Codex reminder; the CLI below is the lifecycle gate.

Set `DWW` to this skill's absolute `scripts/dww.py` and invoke it only with `uv`:

```text
uv run --script <DWW> --repo <repository-or-worktree> <subcommand>
```

## Before writing

- For read-only analysis, do not claim a slot.
- Read repository instructions, then run `doctor` or `status` when mode is uncertain.
- If a mature workflow is detected, defer completely: no policy, slot, or local lifecycle state is written.
- In an uninitialized repository, run `init` to show the plan. Only the user may choose `init --accept`, `init --accept-static-only`, or `init --decline`.
- Every machine approves the exact normalized validation plan. Use `approve --accept` for the tracked policy; use `approve --accept --task <id>` only when reviewing a committed candidate policy change before Ready.

## Modifying task

1. Run `start --name <purpose>`. It branches from the invocation worktree's current local branch; detached HEAD requires `--base`.
2. Work only in the returned worktree. Keep the returned lease private.
3. Commit exactly the reviewed paths with repeated `--path`; never use unscoped staging.
4. Use `plan --task <id>` for mappings, profile levels, queue class, and local duration estimate. Its slow-validation advisory is informational only.
5. Use `verify --level development` for registered development feedback. `verify --level full` is explicit; it runs ready plus full profiles.
6. Run `ready`, then `finish` with the same task and lease.

Ready/Finish synchronize only the recorded base branch. A deleted, rewound, or rewritten base requires explicit `retarget`; Finish only fast-forwards the recorded clean base worktree. It never fetches, pulls, pushes, opens a PR, rebases, squashes, amends, or rewrites history.

## Validation and queue

- `verification.toml` supports schema 3 only. Commands are registered argv arrays, never shell strings.
- Development, ready, and full evidence are separate. Only exact compatible Ready evidence can satisfy Finish.
- Every changed candidate path needs a Ready mapping unless the repository explicitly uses static-only mode.
- Validation uses a machine-global weighted FIFO queue: normal profiles use one unit; heavy profiles consume the computed capacity exclusively. Repository policy chooses only `normal` or `heavy` and cannot raise the machine limit.
- Default capacity is computed once from physical cores and total memory. `settings --validation-capacity auto|1..4` changes only the current machine. `status` and `version` show the capacity rationale and queue state.
- Successful local profile durations produce estimates. If the estimate exceeds ten minutes, show the advisory; never silently reduce coverage.

## Cleanup and recovery

- Finish never removes dependencies or caches.
- `prune-slot --slot <id>` first prints a plan. Execute only with its exact plan id and digest.
- Only exact top-level `cleanup.owned_paths` are candidates. A `.env*`, symlink, junction, or any content change inside a declared target stops the entire prune. Files outside declared targets are retained and do not expand deletion scope.
- `recover` only rotates a stale lease; it refuses live operations or live validation processes. `abandon` and `deinit` require explicit confirmation and preserve uncertain content.

Read [configuration.md](references/configuration.md), [lifecycle.md](references/lifecycle.md), and [safety.md](references/safety.md) before changing policy or handling an exception.
