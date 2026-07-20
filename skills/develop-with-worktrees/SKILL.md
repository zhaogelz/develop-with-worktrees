---
name: develop-with-worktrees
description: Use for any Git repository coding task that may modify files. It initializes a local-first fixed worktree pool once, then isolates, commits, validates, and safely integrates each task. Do not claim a slot for read-only analysis. Defer when the repository already has a mature worktree or agent-orchestration workflow.
---

# Develop with Worktrees

Use the bundled lifecycle instead of editing the primary worktree. Keep explanations in the user's language; keep command arguments, configuration, and repository documentation in English unless the repository says otherwise.

## Locate the runner

Set `DWW` to this skill's `scripts/dww.py` absolute path. Invoke every command as:

```text
uv run --script <DWW> --repo <path-inside-repository> <command>
```

Do not install the runner into the target repository or global environment.

## Decide whether to claim a slot

- For read-only inspection, explanation, planning, review, or diagnosis: do not start a task.
- Before any file mutation: inspect repository instructions and check whether `.solo-ai/config.toml` exists.
- If an established repository workflow is detected, follow it. Never layer another managed pool over it.
- If `.solo-ai/config.toml` has `mode = "compatible"`, follow the repository's existing workflow.
- If tracked mode is disabled or local status reports `local_enabled = false`, do not use this lifecycle.

## Initialize once

When configuration is absent, inspect the repository and run `init`. Initialization creates one local bootstrap commit containing only:

- `.solo-ai/config.toml`
- `.solo-ai/verification.toml`
- the managed block in `AGENTS.md`

Review discovered commands with the user once for a new repository. Then pass `--accept`. If no meaningful command exists, explain the limitation and require explicit `--accept-static-only`. Never describe static-only completion as tests passing.

```text
uv run --script <DWW> --repo <repo> init --slots 3 --accept
```

Use `--verify <command>` repeatedly to replace discovery. Use `--compatible` only when the repository's own workflow should remain authoritative. Initialization may be slow because the three slots are lazy: source checkout happens on first use, and dependency environments remain local to each slot.

## Execute a modifying task

1. Run `start --name <short-purpose>` before editing.
2. Record the returned task id, lease, worktree, and branch. Work only in that worktree.
3. Read instructions again from the returned worktree.
4. Keep changes task-scoped. If unrelated pre-existing changes appear, stop and ask rather than absorbing them.
5. Run focused checks while developing.
6. Run `commit --task <id> --lease <lease> --message <message>`.
7. Run `ready --task <id> --lease <lease>`.
8. If Ready succeeds, immediately run `finish --task <id> --lease <lease>`.

Ready merges the latest local default branch into the task branch when necessary, scans the candidate, and validates it. Finish enters a local FIFO queue, rechecks the current inputs, reuses only an exact proof, and fast-forwards the local default branch. Multiple slots never merge automatically merely because they are active; every task is integrated serially, and a later task must resolve any real conflict against the newly advanced default branch.

Do not fetch, pull, push, open a PR, rebase, squash, amend, or rewrite history as part of this skill. Preserve valid task commits.

## Handle failures

- Treat any failed gate as preserved work, not permission to bypass it.
- Use `status`; add `--detailed` only when exact disk measurements are useful.
- Use `recover --task <id>` only when the prior operator is gone; it rotates the lease without discarding changes.
- Fix conflicts or validation failures in the same task worktree, commit, then repeat Ready and Finish.
- Use `abandon --task <id> --lease <lease> --confirm <exact-id>` only after the user explicitly agrees to discard the task.
- Use `prune --confirm PRUNE` only when the user accepts deletion of local logs and proofs; pruning invalidates proof reuse.
- Use `disable` or `enable` for a machine-local opt-out without changing repository policy.

## Respect local boundaries

Tracked policy lives in `.solo-ai/` and the managed `AGENTS.md` block. Slot state, leases, ports, dependency caches, process identities, validation logs, proofs, approvals, and personal disable choices belong under the repository's Git common directory and remain local.

Retain per-slot `.venv`, `node_modules`, `uv.toml`, `.env*`, `.cache`, and `.tmp` data. Never copy them from the primary worktree. Stop only a process whose PID, creation time, executable, working directory, and command line match the recorded identity. Unknown ignored files or unknown process identities block cleanup.

Read [lifecycle.md](references/lifecycle.md) for state transitions, [configuration.md](references/configuration.md) when changing policy, and [safety.md](references/safety.md) for sensitive-file and cleanup behavior.
