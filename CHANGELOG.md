# Changelog

## 0.3.0-beta.5 — 2026-08-13

- Finish 在改变基线分支前持久化精确候选事务；Recover 依据 Git 祖先事实幂等完成提升、detached、原子删引用和槽位释放。
- Abandon 与 Finish 共用集成串行边界，并使用可恢复事务、工作树身份核验和预期 SHA 原子删引用。
- 统一清理分类，大小写无关保护 `.env*`、数据库与上传/存储目录，拒绝未知 ignored 内容和链接/junction。
- PruneSlot 计划绑定槽位世代、一次性执行，在首次移动前持久化事务，并可从真实进程退出后的暂存或逐项删除阶段继续。
- Ready/Finish 的证明绑定精确候选 SHA，在敏感扫描、验证和进程停止前后拒绝候选漂移。
- Recover 发布同任务互斥操作，严格迁移旧回执，并可补齐已释放直改任务的最终回执；辅助操作日志失败不再制造假失败或遗留活动标记。
- Abandon 与 PruneSlot 绑定 Windows 文件对象身份并使用条件删除；tracked 改动、同路径替换、跨任务 ref 竞态和释放瞬间的晚到内容均保留现场或隔离槽位。

## 0.3.0-beta.4 — 2026-08-11

- Makes one Ready call converge when another task advances the recorded base during validation, instead of recording a stale Ready proof that Finish must validate again.
- Rechecks the expected base after machine validation admission and before every profile command; a post-validation check closes changes that occur during the final command. A failed command is discarded only when its base changed during execution, while a failure on the current base remains a hard failure.
- Reuses exact unchanged profile proofs across convergence attempts and bounds automatic retries at five while preserving the candidate on continued movement.

## 0.3.0-beta.3 — 2026-07-30

- Treats `hooks/hooks.json` as a stable trust-compatibility contract so ordinary plugin, skill, and guard-script updates require no repeated user action.
- Changes the AI flow to ask once only when Codex actually reports a first-install or changed-definition review, then use available host UI control after approval.
- Explicitly forbids editing Codex trust storage, using the broad hook-trust bypass flag, or misusing enterprise managed hooks.

## 0.3.0-beta.2 — 2026-07-30

- Clarifies that `remote_policy = "local-only"` constrains DWW and orchestration, rather than blocking an explicit user-requested publish after Finish.
- Adds one bounded post-Finish publish contract: clean base worktree, current branch, push dry-run first, ordinary non-force push only.
- Keeps fetch, pull, remote deletion, tags, PR creation, deployment, and history rewriting outside that permission unless separately requested.

## 0.3.0-beta.1 — 2026-07-30

- Adds a local multi-AI command-center layer with one-confirmation batches, dependency frontiers, a five-worker development ceiling, pause/resume/cancel/handoff, and compact completion receipts.
- Keeps orchestration state separate from DWW lifecycle state; records only minimal task, decision, and proof references, never chat transcripts, raw reasoning, leases, or secrets.
- Keeps same-file work optimistic by default, serializes only explicit high-risk resources, and escalates repeated unchanged failures rather than blindly retrying.
- Adds explicit `dww` and `delegated` lifecycle adapter boundaries; orchestration remains local-only and never pushes, opens PRs, deploys, or guesses semantic merges.

## 0.2.0-beta.5 — 2026-07-29

- Gives detected mature repository workflows absolute routing priority over DWW's long-term and current-task direct-development choices.
- Adds compact read-only `dww route --json` output and a dependency-free shared routing decision used by both CLI and Codex Hook.
- Makes `SessionStart` inject mature-workflow deferral once while later Pre/Post Tool hooks silently step aside, avoiding repeated context cost.
- Makes every `choose` mode return `deferred` without changing DWW state when a mature workflow exists.

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
