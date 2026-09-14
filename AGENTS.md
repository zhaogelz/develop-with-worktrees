<!-- develop-with-worktrees:managed:start -->
## Isolated coding tasks

For every task that may modify repository files, use the installed `develop-with-worktrees` skill before editing. Run `start`, work only in the returned worktree, and stage an exact reviewed path list with `commit`. Run `ready` when useful development evidence or a legacy task requires it, then `finish`; new default batched tasks may finish directly from `active` after the exact commit. Read-only analysis does not claim a slot. Do not bypass a failed gate. The DWW lifecycle is local-only and must not fetch, pull, push, create PRs, rebase, squash, amend, or rewrite history. After a successful Finish, an explicit user request may be fulfilled with an ordinary non-force push of the current branch from the clean base worktree; that publishing step is separate from DWW.
`Start` creates the local task anchor; keep it current and reread it after continuation or context loss. When the user has explicitly confirmed a complete plan or asked to set it as the objective, create one DWW `root-anchor` before the first child Start with the full plan file, its source, and a stable request id. The root is the single durable objective: it keeps the complete final plan, explicit user amendments, progress, and the checked overall result; children bind it but do not duplicate it. On continuation or candidate repair, read `anchor show --with-root` and acknowledge the reviewed plan version before editing. A structured root closes only after every child is terminal and `root-anchor accept` records accepted or cancelled evidence. A child in another repository uses the same root only through explicit `--root-anchor-file <absolute-path>`, never a duplicated root. DWW verifies and records that exact non-linked root locator and its child-state locator, but it never searches repositories or becomes a `scope_id`, candidate group, DAG, scheduler, or batch boundary. A configured project Adapter may establish project runtime identity only after the exact isolated task exists and before Start returns it as active. New repositories publish exact source candidates, release project resources through the same Adapter, then release the task worktree. Each configured full batch freezes automatically; an exact smaller tail freezes only after the host explicitly ends the round or requests immediate integration. Host heartbeat may wake `batch reconcile` but cannot choose candidates; UI task counts, raw worktree counts, Hook delivery, and session end never prove completion. There is no candidate-age or quiet-period auto-seal. Use the host's native task/subagent system for task orchestration; legacy `dww orchestrate` state is drain-only. Candidate publication is not delivery; only integration into the current base is delivery. Explicit legacy direct policy remains upgrade compatibility only.
<!-- develop-with-worktrees:managed:end -->

## 快速开发优先

本项目主要服务于个人和小团队使用 AI 快速开发。方案设计优先考虑短反馈周期、少人工打断、容易理解和容易维护；在不丢失用户工作、不越过已授权范围、不绕过必要验证的前提下，优先选择解决当前问题的最小方案。

用户要求开始或继续任务后，范围内的调查、隔离修改、必要测试、精确路径提交、已有规则允许的本地合入，以及能证明身份的恢复默认由 AI 连续完成。已有授权持续有效；技术参数如 `--accept`、`--confirm` 或 `--force` 用于核对对象或执行既有授权，本身不构成再次询问用户的理由。

只有现有指令和项目契约无法决定产品或范围取舍、动作扩大权限或外部副作用、会丢弃未授权的成果，或现场身份无法核实时，才请求用户决定。验证按变更影响选择，保留精确提交、候选身份、必要集成验证和未知现场保护；输入变化时重跑受影响检查，不把所有变化都转成人工批准。

## README audience

README 面向第一次接触 DWW 的普通使用者。首页优先通俗说明“解决什么问题、带来什么帮助、默认怎么工作”，不得以内部状态、事务、指纹或安全术语开篇。主流程不超过五步；详细配置、异常恢复和实现原理进入 `docs/` 或技能 references。修改用户可感知行为时必须同步检查中英文 README，避免重新变成架构说明书。
