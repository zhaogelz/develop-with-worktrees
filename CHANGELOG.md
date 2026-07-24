# Changelog

## 0.2.0-beta.4 — 2026-07-24

- Replaces the multi-step repository adoption conversation with one plain-language three-choice prompt: isolate normal tasks, use the current directory for this task only, or remember current-directory development locally for this repository.
- Makes the one-task current-directory choice behave as if the plugin were absent: no policy files, DWW task, lifecycle gates, or guard alerts. The local authorization is bound to the exact worktree and hashed Codex session.
- Adds explicit one-time child-session delegation, rather than unsafe repository-wide or time-window bypasses, because current Codex hook payloads do not provide a reliable parent-agent identifier.
- Reuses existing disable safety checks for the long-term local choice and accepts internal static checks without a second user prompt when no test command is discovered.

## 0.2.0-beta.3 — 2026-07-24

- Fixes validation timeouts on macOS: a validation process launched by the current `run_logged` call is now terminated through its owned `Popen` process group, rather than being blocked by cross-call process-snapshot matching. Cross-call PID-reuse protection remains unchanged.

## 0.2.0-beta.2 — 2026-07-24

- Adds one explicit, session-bound `in-place` task mode for current-worktree-only work; it keeps an immutable validation start commit and never merges, switches, resets, cleans, or deletes that worktree on Finish or failure.
- Blocks same-base isolated Finish while an in-place task is active, so a parallel merge cannot silently invalidate its checked-out branch or HEAD.
- Upgrades local task state to schema 3 with read compatibility for existing schema 2 isolated tasks.
- Replaces the advisory Codex hook with supported `PreToolUse permissionDecision: deny` responses for protected base-worktree writes, plus preserved dirty-state alerts for escaped specialised paths.
- Requires explicit hook trust after install or hook changes and documents the boundary between Codex hard guardrails and operating-system enforcement.

## 0.2.0-beta.1 — 2026-07-23

- Clean breaking release: verification policy is schema 3 only.
- Starts and integrates against the recorded current local base branch.
- Adds machine-global weighted FIFO validation capacity, local capacity settings, duration estimates, and slow-validation advisory.
- Makes cache cleanup opt-in and plan-bound; links, junctions, `.env*`, and changed targets stop the whole prune.
- Adds CLI version contract, candidate-policy approval, and release consistency tests.
