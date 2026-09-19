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

## Install

```text
codex plugin marketplace add zhaogelz/develop-with-worktrees --ref v0.5.0-beta.7
codex plugin add develop-with-worktrees@develop-with-worktrees
```

After installing or updating, the current host must load the installed skill
before a running task can use it. If the host still shows the old skill
behavior, use its supported reload path; start a new Codex session only when
that host has no reload path. This is separate from Hook trust: a version
change, a single denial, or a parse/path/owner/lease error is not evidence that
Hook review is needed. Ask for review only when the host explicitly reports a
first-install or changed-definition review, and use the host's supported
control for that exact definition once. DWW cannot inspect or change host trust
storage. The source version is `0.5.0-beta.7`; a plugin cache build may append
a `+codex.<build>` suffix.

## Default flow

A brand-new empty Git repository is supported: after you accept isolated mode,
DWW creates an empty first commit on your chosen branch. It will not include
existing files or staged changes automatically.

1. The host routes the repository. A managed change starts in an isolated
   worktree; an existing mature workflow keeps ownership of its repository.
2. The agent edits only there and commits the exact reviewed paths.
3. `finish` publishes an immutable source candidate. It ends that coding round,
   but does not by itself deliver the result to the base branch.
4. Every three compatible candidates, DWW combines them in an integration
   worktree and runs the repository checks affected by the combined changes.
5. A smaller final group needs an explicit recorded reason such as
   `round-complete`, `user`, `deploy`, or `dependency`; DWW never treats idle
   time or a task count as proof that the round ended.

The host follows a published candidate through integration, recovery, or a
recorded decision. You do not need to copy candidate IDs for the normal flow.

## Boundaries that keep work safe

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
