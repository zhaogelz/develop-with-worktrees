# Develop with Worktrees

`0.5.0-beta.1` is a host-neutral local Git safety lifecycle. It routes modifying work, creates first-class task anchors, isolates worktrees, commits exact paths, validates candidates, integrates directly or through explicit candidate batches, and recovers from persisted Git facts. The host's native task/subagent system owns decomposition, dependencies, workers, waits, and task status.

## Responsibility boundary

- Native task orchestration answers who works on which outcome and when.
- DWW answers where each Git change is made, what exact candidate was verified, and which explicit candidates may move the base.
- Codex Hooks are optional hardening, not lifecycle truth.

The old `dww orchestrate` family is drain-only compatibility. It can finish or inspect existing legacy batches, but it no longer creates batches, appends tasks, or creates repair tasks.

## Routing and managed work

A compact read-only route lets mature repository workflows win. A deferred repository receives no DWW task, anchor, candidate, or batch state. An approved delegated adapter remains exact and locally fingerprinted. A managed repository receives an isolated task; an unchosen repository gets one plain-language choice.

Hook route context is optional. Without it, the skill runs one `dww route --json` fallback.

```text
route → start → update generated task anchor → edit returned worktree only
      → commit exact paths → ready → finish
```

Start creates `<git-common-dir>/solo-ai/task-anchors/<task-id>.md` before returning. It records purpose, target, baseline, boundaries, acceptance, and progress. Reread it after context loss, model changes, handoff, or continuation. A repeated caller `request_id` returns the same managed task rather than consuming another slot.

Direct integration is the default. Finish validates, fast-forwards the recorded clean local base, releases the worktree, and deletes the anchor. DWW never fetches, pulls, pushes, opens PRs, rebases, squashes, amends, deploys, or rewrites history.

## Optional explicit candidate batches

Repositories that need one combined acceptance boundary may configure:

```toml
integration = { mode = "batched", batch_size = 5, candidate_capacity = 10 }
```

In batched mode, Finish publishes one immutable verified candidate and immediately releases its slot without moving the base. The anchor remains. The pool defaults to ten candidates and never seals itself.

Only `batch seal --candidate <id> ...` freezes a generation, with at most five candidates by default. DWW composes their exact tree differences in a dedicated integration worktree, runs combined Full validation, rechecks the frozen base, and then fast-forwards the clean checked-out base. Conflict or final-validation failure preserves the base and is never blindly rerun. A recorded composition conflict can use `candidate repair --candidate <id>` to prepare one bounded managed repair on the latest base; deterministic validation, product, permission, migration, destructive, and security choices still stop for review.

Candidate count, timers, apparent idleness, task completion, and SessionEnd never trigger a seal. Candidate withdrawal, successful batch integration, or explicit abandonment removes the associated anchor.

## Optional Hook hardening

A trusted `PreToolUse` Hook can deny unauthorised writes on supported Codex local-tool paths. It is valuable defence in depth, not an operating-system sandbox and not required for route, anchors, worktrees, validation, candidates, sealing, or recovery. Ordinary releases keep `hooks/hooks.json` stable to avoid needless re-trust.

## Installation

```text
codex plugin marketplace add zhaogelz/develop-with-worktrees --ref v0.5.0-beta.1
codex plugin add develop-with-worktrees@develop-with-worktrees
```

An explicit user request may separately push an already integrated clean base branch with a dry-run-first ordinary non-force push.

See [Chinese documentation](README.zh-CN.md), [configuration](plugins/develop-with-worktrees/skills/develop-with-worktrees/references/configuration.md), [lifecycle](plugins/develop-with-worktrees/skills/develop-with-worktrees/references/lifecycle.md), [task governance](plugins/develop-with-worktrees/skills/develop-with-worktrees/references/task-governance.md), and [architecture](docs/architecture.md).
