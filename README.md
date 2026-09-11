# Develop with Worktrees

When several AI tasks modify one Git repository, they can overwrite each other, commit from the wrong directory, or reach the main branch without combined validation. DWW gives each modifying task an isolated worktree and local task anchor, accepts only exact reviewed paths, and promotes verified results safely.

## What it gives you

- One isolated worktree per modifying task.
- A local task anchor that survives continuation, handoff, and model changes; `anchor show/update` reads and saves checked context during an active task.
- Exact-path commits and immutable verified candidates.
- Automatic integration whenever five eligible candidates accumulate.
- A recorded recovery path that leaves the base unchanged on conflicts or failed validation.
- Completed checks are saved individually. A fresh Full reuses only checks with complete, pure inputs; mutable environments and required build artifacts are not replaced by old success reports.

The host's native task system still decides who does what and when. DWW owns only the Git safety lifecycle that carries completed work into the base branch.

## Default flow

1. `Start` creates the exact task anchor and isolated worktree, then lets an optional project Runtime Adapter establish project runtime identity before returning it as active.
2. The agent edits only there, commits exact paths, and runs `ready`.
3. `Finish` publishes a verified candidate and releases the task worktree without moving the base.
4. Every five candidates, DWW freezes the oldest eligible five and composes them in a dedicated integration worktree. An optional paired batch Adapter establishes and releases project-owned Full-validation resources before DWW may advance the base.
5. With fewer than five candidates, DWW freezes the exact pending tail only after the lane has no modifying producer for a stable 90 seconds, or after an explicit user, deployment, or downstream-dependency request.

Users do not need to copy candidate IDs. DWW selects only immutable candidates already activated in persisted state; the host heartbeat merely wakes reconciliation at `next_reconcile_at`. A host without reliable scheduling cannot claim automatic quiet-tail support.

Releasing a task keeps its dependency caches, including normal package links. New repositories also reuse one integration workspace: ordinary successful or failed batches return it without deleting or hashing the entire dependency tree. Each fresh Full still recreates required runtime effects. Physical disk cleanup is separate maintenance; see the [cleanup safety contract](plugins/develop-with-worktrees/skills/develop-with-worktrees/references/safety.md).

## Why five and ten

Five candidates amortise combined validation while keeping conflicts reviewable. The pool holds ten nonterminal candidates by default, so one batch can integrate while the next accumulates. A full pool preserves the publishing task and asks it to retry; it never drops work.

## What DWW never does

- It never guesses candidate identity from UI task counts, raw worktree counts, Hooks, or session end; those signals may only wake reconciliation.
- It has no candidate-age or longest-wait auto-seal. Active modifying work keeps a tail open unless an explicit user, deployment, or dependency request forces the current exact snapshot.
- It does not replace native task decomposition, dependencies, or worker scheduling.
- It never fetches, pulls, pushes, opens PRs, deploys, rebases, squashes, amends, or rewrites history.
- It does not absorb repository-specific ports, databases, browsers, test selection, or deployment rules.

Hooks are optional early hardening or wake-up sources. Routing, anchors, worktrees, validation, candidates, batching, and recovery must still work without them.

Projects may configure a Runtime Adapter. At Start it can establish ignored project runtime identity from DWW's exact task, slot, worktree, base, and deterministic port-block facts; no `candidate_head` is supplied until a candidate actually exists. DWW still verifies the clean worktree against the frozen base immediately before and after activation. After the immutable candidate ref is durable, the Adapter releases development resources before DWW frees the worktree and activates the candidate. Paired `batch_activate`/`batch_release` commands may wrap combined Full with a port block disjoint from all 32 task slots. Their context carries a persisted `runtime_cycle`: an interrupted command retries the same cycle and exact receipt, while recovery after resources were successfully released starts a new cycle and really activates them again. Uncertain activation or release preserves the batch and base for exact recovery. DWW never interprets the project's concrete port, database, browser, or service rules. Publication is not delivery: only a completed batch contained in the current base is delivered, and explicit runtime-effectiveness checks remain project-defined Adapter work.

A composition conflict tied to one candidate can prepare up to two managed repair generations on the latest base. The agent continues only when code, contracts, and tests determine one answer; product, permission, migration, deletion, and security choices still stop for a human. Final-validation failures are never disguised as merge conflicts and blindly retried. If a diagnosed external blocker changes while the candidates do not, `batch seal --after-failed-batch <id>` explicitly and idempotently creates one reviewed successor of that exact failed generation.

After a reusable generation fails, `batch retire --batch <id>` can finish its safe workspace return; it never deletes a later batch's workspace. Older dedicated batches retain exact, non-force physical retirement. For an explicitly approved cleanup of an old failed dedicated batch whose candidates are already superseded, `batch retire --fast --batch <id>` uses the same identity and protected-content preflight while avoiding dependency content hashes; it preserves candidate refs and audit facts and is retry-safe. `batch metrics` reads existing lifecycle and proof facts to report full/tail rates, candidate wait time, and executed Full cost before changing batch-size policy.

Legacy repositories may keep explicit direct or explicit-seal policies during migration. New repositories use the candidate-first flow.

## Installation

```text
codex plugin marketplace add zhaogelz/develop-with-worktrees --ref v0.5.0-beta.1
codex plugin add develop-with-worktrees@develop-with-worktrees
```

Start a new Codex session after installing or updating so the new skill text is loaded. Remote publishing is separate and requires an explicit user request for a dry-run-first ordinary non-force push from the clean integrated base.

See the [Chinese guide](README.zh-CN.md), [configuration](plugins/develop-with-worktrees/skills/develop-with-worktrees/references/configuration.md), [lifecycle](plugins/develop-with-worktrees/skills/develop-with-worktrees/references/lifecycle.md), [task governance](plugins/develop-with-worktrees/skills/develop-with-worktrees/references/task-governance.md), and [architecture](docs/architecture.md) for details.
