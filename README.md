# Develop with Worktrees

`0.2.0-beta.2` is a local-first workflow for safe parallel AI Git work. Its core lifecycle is host-neutral; this plugin supplies the Codex adapter and guard.

## Default: isolated work

```text
doctor → start → edit only in returned worktree → commit exact paths
       → plan / verify → ready → finish
```

- `start` derives from the invoking worktree's current local branch, never an assumed `main`.
- Each normal task receives its own reusable managed worktree and lease. Finish fast-forwards only the recorded clean base worktree, locally.
- Validation uses schema 3 profiles and a machine-global weighted FIFO queue. Slow estimates advise splitting profiles or removing repeated preparation; they never weaken coverage.
- Finish preserves caches and dependencies. Destructive cleanup remains an explicit reviewed `prune-slot` action.

## Explicit current-worktree work

When the user clearly requires the current environment—for example, data or a test cache that only exists there—use one session-bound task instead:

```text
start --in-place --session <Codex-session-id>
→ edit current worktree → commit exact paths → ready → finish
```

It starts only from a clean Git worktree (ignored test data may stay), creates no slot or branch, and validates every change against its immutable start commit. Finish neither merges, switches branches, deletes files, nor clears ignored test data. If a prior Codex task ended, an exact-confirmation resume may transfer an unchanged active, ready, or quarantined task without changing files; a branch/HEAD mismatch must still be manually restored first. While it is active, isolated work may continue, but another task cannot Finish into the same base branch.

In Codex, the trusted `PreToolUse` hook hard-denies protected base-worktree writes on supported local tool paths. It is a strong guardrail, not operating-system enforcement: specialised paths can opt out. When a later hooked call or `doctor` observes escaped dirty state, it preserves and records an alert; it never promises to see an opt-out path immediately. After every install or hook change, open `/hooks`, trust this plugin's hook, then start a new task. Until then, do not rely on in-place protection.

## Installation

```text
codex plugin marketplace add zhaogelz/develop-with-worktrees --ref main
codex plugin add develop-with-worktrees@develop-with-worktrees
```

Start a new Codex task, trust the hook in `/hooks`, and run `doctor` before modifying a repository. The plugin never updates itself, fetches, pulls, pushes, creates PRs, rebases, squashes, amends, or rewrites history.

See [Chinese documentation](README.zh-CN.md), [configuration](plugins/develop-with-worktrees/skills/develop-with-worktrees/references/configuration.md), and [architecture](docs/architecture.md).
