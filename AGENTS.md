<!-- develop-with-worktrees:managed:start -->
## Isolated coding tasks

For every task that may modify repository files, use the installed `develop-with-worktrees` skill before editing. Run `start`, work only in its returned worktree, review exact paths before `commit`, then `finish`; do not bypass failed gates. DWW is local-only: do not fetch, pull, push, rebase, squash, amend, or rewrite history through it.

Keep one task anchor per task. `Start` records the known purpose, scope, acceptance criteria, baseline, and progress; update it only when that execution contract or progress materially changes. When the user has confirmed a complete plan, create one root anchor before its child tasks: the root keeps the complete plan, full plan-changing history, amendments, and overall result without a content-size limit, while children keep only their execution slice. When the host supplies an exact task identity, create that root with the identity, a stable request ID, and an acceptance index; DWW stores only the exact host-to-root locator. Later `Start` calls from that host inherit the root, reject a conflicting explicit root, and record an explicit independent task only with its one-line reason. Protocol-root acceptance needs evidence for every indexed required item; legacy roots remain compatible.

Normal `Start` or `bind-root` prints the complete root context once without a separate acknowledgement step. After continuation, model/context recovery, a root-plan change, or candidate repair, use one `anchor refresh-root` operation to return the current task anchor and complete root context together; it records the current version without copying SHA or version parameters. This refresh is required before Commit, Ready, or Finish when the recorded root is stale, not on every edit. Close a structured root only after all children are terminal and accepted or cancelled evidence is recorded.

An exact full batch freezes automatically. A smaller tail freezes only on an explicit `round-complete`, `user`, `deploy`, or `dependency` cause with one short reason; heartbeat, idle time, and task counts never seal a batch. Ordinary completion does not request immediate integration. In native state, Ready freezes the exact task head and `Finish` leaves its fixed slot owned while the task waits for real merge, Full, promotion, and release. In legacy state, `Finish` publishes an immutable candidate and releases its worktree. Use `round-complete` only after the current round has ended and its lane has no active producer; use `user` only when the user explicitly asks to integrate now without waiting. Do not infer that exception from ordinary completion or review wording. Candidate publication is not delivery: the coordinator that freezes a full batch or tail follows integration, inspects failures, and repairs deterministically within the agreed scope.
<!-- develop-with-worktrees:managed:end -->

## 快速开发优先

本项目主要服务于个人和小团队使用 AI 快速开发。方案设计首先坚持“不要过度设计”：以当前证据、用户目标和验收标准为边界，优先选择易理解、易维护、短反馈且足以解决当前问题的方案；只有现有机制无法满足已经观察到的需求时，才新增持久层、抽象、流程或人工关卡，并说明理由和验证方式。

用户要求开始或继续任务后，范围内的调查、隔离修改、必要测试、精确路径提交、已有规则允许的本地合入，以及能证明身份的恢复默认由 AI 连续完成。已有授权持续有效；技术参数如 `--accept`、`--confirm` 或 `--force` 用于核对对象或执行既有授权，本身不构成再次询问用户的理由。

只有现有指令和项目契约无法决定产品或范围取舍、动作扩大权限或外部副作用、会丢弃未授权的成果，或现场身份无法核实时，才请求用户决定。验证按变更影响选择，保留精确提交、候选身份、必要集成验证和未知现场保护；输入变化时重跑受影响检查，不把所有变化都转成人工批准。

## README audience

README 面向第一次接触 DWW 的普通使用者。首页优先通俗说明“解决什么问题、带来什么帮助、默认怎么工作”，不得以内部状态、事务、指纹或安全术语开篇。主流程不超过五步；详细配置、异常恢复和实现原理进入 `docs/` 或技能 references。修改用户可感知行为时必须同步检查中英文 README，避免重新变成架构说明书。

## 文档权威路线

长期有效的产品定位、职责边界和架构取舍由 `docs/architecture.md` 统一维护；本仓库的开发、检查和文档维护入口见 `docs/development.md`。命令、配置和运行协议由插件技能的对应 reference 负责，README 只保留普通使用者需要的默认体验。`CHANGELOG.md` 是版本历史，视频目录只在视频工程任务中读取，不作为普通产品开发的默认上下文。
