# Develop with Worktrees

`0.4.0-beta.1` 是一个通用的本地 Git 安全开发底座。它负责为修改任务选择正确的仓库流程、建立任务锚点、隔离工作树、精确提交、验证、候选发布、合入和恢复；任务怎么拆、由几个 AI 做、依赖谁，则交给 Codex 等宿主自带的任务/子智能体能力。

## 一句话理解

- 宿主任務系统管“谁做什么、什么时候做”。
- DWW 管“每个修改在哪个安全目录做、改了什么、验证过没有、哪些确定候选可以合回”。
- Hook 只是一层可选的提前拦截，不再承担正确性。

旧 `dww orchestrate` 不再创建新任务批次，也不再追加或生成修复任务；它只保留已有旧批次的查看、收尾、暂停、恢复、移交和取消兼容入口。

## 成熟项目优先

第一次修改前，DWW 只读判断仓库由谁管理：

- 已有成熟流程：DWW 静默让路，不写自己的任务、锚点、候选或批次状态。
- 已批准的项目适配器：严格走该适配器，不混用 DWW managed 生命周期。
- 已启用 DWW：自动领取独立工作树。
- 尚未选择：只问一次“独立目录、仅本次当前目录、以后当前目录”。

`SessionStart` Hook 可以提前提供这个判断；没有 Hook 时，技能运行一次只读 `dww route --json`，效果相同。

## 普通开发流程

```text
route → start → 更新自动生成的任务锚点 → 只在返回目录修改
      → commit 精确路径 → ready → finish
```

`Start` 自动在 `<git-common-dir>/solo-ai/task-anchors/<task-id>.md` 建立不提交的任务锚点，记录目标、对象、基线、边界、验收和进度。上下文压缩、换模型、交接或续作后，修改前先重读。重复传入同一个 `request_id` 会返回原任务，不会多占一个工作树。

通用默认是直接模式：Finish 验证后本地快进目标分支，释放工作树并删除锚点。DWW 不会 fetch、pull、push、创建 PR、rebase、squash、amend 或改写历史。

## 可选候选批次

需要“一批改动一起最终验收”的项目可以显式配置：

```toml
integration = { mode = "batched", batch_size = 5, candidate_capacity = 10 }
```

这时：

1. 每个任务 Finish 后只发布一个已验证、不可变的候选并立即释放工作树，主分支不动，锚点保留。
2. 候选池默认最多 10 个；满了只会停止继续发布，不会自动封批。
3. 只有明确执行 `batch seal --candidate <id> ...` 才冻结本次候选，默认一批最多 5 个。
4. DWW 在独立集成工作树组合这些确定候选，执行最终 Full 验证，再核对主分支仍是原基线，最后才快进。
5. 冲突或最终验证失败时主分支不动；诊断后用 `start --supersedes <candidate-id>` 发布修复候选，再显式封一个新批次。失败代次不会被自动重跑。

不会因为“刚好有 5 个”“现在没有活跃任务”“等了一段时间”或“会话结束”自动封批。候选撤回、批次成功或任务放弃后才删除对应锚点。

## Hook 的位置

受信任的 `PreToolUse` Hook 仍能在 Codex 支持的本地工具路径上提前拒绝未授权写入，这是有价值的强化保护，但不是操作系统沙箱。即使 Hook 未安装、未信任或宿主没有 Hook，DWW 的路由、锚点、工作树、验证、候选池、显式封批和恢复仍可正常工作。`hooks/hooks.json` 在普通更新中保持不变，避免重复信任。

## 安装

```text
codex plugin marketplace add zhaogelz/develop-with-worktrees --ref v0.4.0-beta.1
codex plugin add develop-with-worktrees@develop-with-worktrees
```

只有用户明确要求时，成功合入后的干净基线工作树才可另行执行一次 dry-run 优先、非强制的普通推送。

详细边界见[配置参考](plugins/develop-with-worktrees/skills/develop-with-worktrees/references/configuration.md)、[生命周期参考](plugins/develop-with-worktrees/skills/develop-with-worktrees/references/lifecycle.md)、[任务治理参考](plugins/develop-with-worktrees/skills/develop-with-worktrees/references/task-governance.md)和[安全参考](plugins/develop-with-worktrees/skills/develop-with-worktrees/references/safety.md)。
