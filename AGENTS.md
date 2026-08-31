<!-- develop-with-worktrees:managed:start -->
## Isolated coding tasks

For every task that may modify repository files, use the installed `develop-with-worktrees` skill before editing. Run `start`, work only in the returned worktree, stage an exact reviewed path list with `commit`, then run `ready` and `finish`. Read-only analysis does not claim a slot. Do not bypass a failed gate. `Start` creates the local task anchor; keep it current and reread it after continuation or context loss. New tasks publish verified candidates and release their worktrees; each full configured batch is frozen automatically, while the coordinating native task explicitly seals an exact smaller tail only after it knows the intended work is complete. Never infer a tail from idle time, task counts, Hook delivery, or session end. Explicit legacy direct policy is upgrade compatibility only. The DWW lifecycle is local-only and must not fetch, pull, push, create PRs, rebase, squash, amend, or rewrite history. After successful integration, an explicit user request may be fulfilled with an ordinary non-force push from the clean base worktree; that publishing step is separate from DWW.
<!-- develop-with-worktrees:managed:end -->

## README audience

README 面向第一次接触 DWW 的普通使用者。首页优先通俗说明“解决什么问题、带来什么帮助、默认怎么工作”，不得以内部状态、事务、指纹或安全术语开篇。主流程不超过五步；详细配置、异常恢复和实现原理进入 `docs/` 或技能 references。修改用户可感知行为时必须同步检查中英文 README，避免重新变成架构说明书。
