# Develop with Worktrees

`0.2.0-beta.5` is a local-first workflow that keeps parallel AI changes from overwriting one another. Its lifecycle core is host-neutral; this plugin provides the Codex first-choice UX, skill, and write guard.

## Mature repository workflows win

Before any repository choice, a compact read-only route checks for a mature worktree or orchestration workflow. When one exists, DWW silently defers: it asks no three-choice question, writes no DWW state, and follows the repository's own instructions. This has absolute priority over previously stored one-task or long-term direct-directory choices; those local records remain untouched and can become active again only after the mature workflow marker is removed.

The trusted `SessionStart` hook normally injects this route once. If hook context is unavailable, the skill runs one lightweight `dww route --json` fallback; it does not load the full `doctor` report.

## One question on the first modification

When a user first intends to modify an unchosen repository, Codex asks one plain-language question:

```text
How should this repository be changed?

1. Use a separate directory for each task (recommended)
   Tasks do not affect one another and merge back automatically when finished.

2. Change the current directory this time
   Skip only this task; ask again next time.

3. Always change the current directory for this repository
   Remember this choice and do not ask again here.

This affects only this machine and can be changed later.
```

- Choice 1 sets up the normal isolated lifecycle from the current local branch, not an assumed `main`.
- Choice 2 makes this task behave as if the plugin were absent: no policy files, task, Commit/Ready/Finish, or write guard. A new Codex task asks again.
- Choice 3 records a local-only repository preference without changing tracked files. Other clones and machines choose independently.

If no automated test is found, choice 1 still isolates the task and uses internal basic checks without another question. A user can later say to use isolated directories or the current directory for this repository to change the local preference.

## After choosing isolated work

```text
route → start → edit only in returned directory → commit exact paths
       → plan / verify → ready → finish
```

- Each normal task receives its own reusable managed directory and lease. Finish fast-forwards only the recorded clean base worktree, locally.
- Validation uses schema 3 profiles and a machine-global weighted FIFO queue. Slow estimates advise splitting mappings or removing repeated preparation; they never weaken coverage.
- Finish preserves caches and dependencies. Destructive cleanup remains an explicit reviewed `prune-slot` action.

`start --in-place` remains an advanced compatibility path for a user who explicitly wants DWW's exact Commit/Ready/Finish safeguards in the current clean worktree; it is not choice 2.

In Codex, the trusted `PreToolUse` hook hard-denies protected-worktree writes on supported local tool paths. It is a strong guardrail, not operating-system enforcement: specialised paths can opt out. When a later hooked call or `doctor` observes escaped dirty state, it preserves and records an alert; it never promises immediate observation. After every install or hook change, open `/hooks`, trust this plugin's hook, then start a new task. Until then, do not rely on the guard.

## Installation

```text
codex plugin marketplace add zhaogelz/develop-with-worktrees --ref main
codex plugin add develop-with-worktrees@develop-with-worktrees
```

The plugin never updates itself, fetches, pulls, pushes, creates PRs, rebases, squashes, amends, or rewrites history.

See [Chinese documentation](README.zh-CN.md), [configuration](plugins/develop-with-worktrees/skills/develop-with-worktrees/references/configuration.md), and [architecture](docs/architecture.md).
