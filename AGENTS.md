<!-- develop-with-worktrees:managed:start -->
## Isolated coding tasks

For every task that may modify repository files, use the installed `develop-with-worktrees` skill before editing. Run `start`, work only in the returned worktree, stage an exact reviewed path list with `commit`, then run `ready` and `finish`. Read-only analysis does not claim a slot. Do not bypass a failed gate. The DWW lifecycle is local-only and must not fetch, pull, push, create PRs, rebase, squash, amend, or rewrite history. After a successful Finish, an explicit user request may be fulfilled with an ordinary non-force push of the current branch from the clean base worktree; that publishing step is separate from DWW.
`Start` creates the local task anchor; keep it current and reread it after continuation or context loss. A configured project Adapter may establish project runtime identity only after the exact isolated task exists and before Start returns it as active. New repositories publish verified candidates, release project resources through the same Adapter, then release the task worktree. Each full batch freezes automatically; an exact smaller tail freezes only after its persisted lane is stably producer-free or after an explicit user, deployment, or dependency request. Host heartbeat only wakes `batch reconcile`; UI task counts, raw worktree counts, Hook delivery, and session end never prove completion or choose candidates. There is no candidate-age auto-seal. Use the host's native task/subagent system for task orchestration; legacy `dww orchestrate` state is drain-only. Candidate publication is not delivery; only integration into the current base is delivery. Explicit legacy direct policy remains upgrade compatibility only.
<!-- develop-with-worktrees:managed:end -->

## README audience

README 面向第一次接触 DWW 的普通使用者。首页优先通俗说明“解决什么问题、带来什么帮助、默认怎么工作”，不得以内部状态、事务、指纹或安全术语开篇。主流程不超过五步；详细配置、异常恢复和实现原理进入 `docs/` 或技能 references。修改用户可感知行为时必须同步检查中英文 README，避免重新变成架构说明书。
