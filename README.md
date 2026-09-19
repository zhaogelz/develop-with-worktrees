# Develop with Worktrees

When several AI tasks change one Git repository, they can overwrite each
other, commit from the wrong directory, or leave a result unverified. Develop
with Worktrees (DWW) gives each modifying task its own worktree, records the
task contract locally, and carries reviewed changes through local integration.

## What you get

- An isolated worktree for every managed modifying task.
- A local task anchor that preserves the objective, scope, baseline, and
  acceptance criteria across continuation or handoff.
- One confirmed plan kept at its root, so related work can resume from the same
  objective instead of asking you to copy it into every task.
- Exact-path commits and immutable source candidates.
- Combined validation and protected local promotion for compatible work.
- One local approval can cover the commands of the next unchanged step, without repeating approval for unrelated checks.
- A recoverable record when composition or validation fails; the base branch
  stays unchanged until a batch succeeds.

DWW is the Git lifecycle layer. Your host's task system still decides how work
is split, scheduled, and discussed. Your project still owns its tests, runtime
resources, and product decisions.

Plainly: an integrated change has reached the local base; a retained worktree is
kept for review and is not reusable; a reusable slot has been safely returned.

## Install

```text
codex plugin marketplace add zhaogelz/develop-with-worktrees --ref v0.5.0-beta.7
codex plugin add develop-with-worktrees@develop-with-worktrees
```

After installing or updating, let the current host load the installed skill
before using it. Review a Hook only when that host explicitly reports a new or
changed definition awaiting review. Handle an error by its reported cause; a
version change or one denial is not a reason to repeatedly restart or trust.
DWW cannot inspect or change host trust storage. The source version is
`0.5.0-beta.7`; a plugin cache build may append a `+codex.<build>` suffix.

## Default flow

A brand-new empty Git repository is supported: after you accept isolated mode,
DWW creates an empty first commit on your chosen branch. It will not include
existing files or staged changes automatically.

1. The host routes the repository. A managed change starts in an isolated
   worktree; an existing mature workflow keeps ownership of its repository.
2. The agent edits only there and commits the exact reviewed paths.
3. `finish` preserves an immutable source candidate for local delivery. It ends
   the coding round; it does not by itself move the base branch.
4. Every three compatible candidates, DWW combines the changes in an
   integration worktree and runs the affected repository checks.
5. If a smaller final group must be delivered now, the host records one
   supported delivery cause and its reason. DWW never uses idle time, a task
   count, or a UI state as that cause.

The host follows a published candidate through integration, recovery, or a
recorded decision. The host still owns task splitting, waiting, and messages;
you do not need to copy candidate IDs for the normal flow.

## Only when needed

- If the repository already has a mature workflow, let it keep ownership. An
  exact locally approved [adapter](plugins/develop-with-worktrees/skills/develop-with-worktrees/references/delegated-adapters.md)
  is a maintainer option, not a default setup step.
- If an interrupted task, conflict, or combined check needs attention, follow
  the recorded [recovery](plugins/develop-with-worktrees/skills/develop-with-worktrees/references/recovery.md)
  path instead of guessing or cleaning the worktree.
- If a reviewed test worktree was deliberately retained, use the returned
  checklist in `reclaim-retained`; it never treats “task ended” as permission to
  erase unknown work.
- Projects with their own external runtime resources can configure a Runtime
  [Adapter](plugins/develop-with-worktrees/skills/develop-with-worktrees/references/runtime-adapter.md).
  Most personal and small-team repositories do not need one.

## Boundaries that keep work safe

When the host loads and trusts the Hook, native patches started outside Git also
check ownership of the target worktree; another session gains no write permission
by starting elsewhere.

DWW preserves unknown or protected working-tree content and does not guess a
candidate from UI state, hooks, or elapsed time. A conflict or failed combined
check leaves the base branch unchanged and provides a recorded recovery route.
Completed pure checks may be reused only when their declared inputs,
environment, tool facts, and logs still match; required build output and mutable
runtime effects are run again.

DWW itself is local-only: it does not fetch, pull, push, open pull requests,
deploy, rebase, squash, amend, or rewrite history. An explicitly requested
remote publish is a separate operation from a clean integrated base.

## Find the right detail

- Use the installed [skill](plugins/develop-with-worktrees/skills/develop-with-worktrees/SKILL.md)
  for a managed task and its scenario-specific references.
- Read the [Chinese guide](README.zh-CN.md) for the same user flow in Chinese.
- Read [architecture](docs/architecture.md) for responsibility and design
  boundaries, or [development maintenance](docs/development.md) when changing
  this repository.
- See [configuration](plugins/develop-with-worktrees/skills/develop-with-worktrees/references/configuration.md)
  for policy fields, and [recovery](plugins/develop-with-worktrees/skills/develop-with-worktrees/references/recovery.md)
  for an interrupted or failed task.
