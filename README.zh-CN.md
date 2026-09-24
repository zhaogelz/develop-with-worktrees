# Develop with Worktrees

多个 AI 同时修改一个 Git 仓库时，容易互相覆盖、在错误目录提交，或在没有组合验证的情况下进入主线。Develop with Worktrees（DWW）为每个修改任务准备独立工作树，在本地记录任务约束，并把已检查的改动带入本地集成。

## 它能带来什么

- 每个受管修改任务使用独立工作树。
- 本地任务锚点保存目标、范围、基线和验收，续作或交接不必重新猜上下文。
- 已确认的完整方案只保留一份主锚点，相关任务续作时回到同一依据，不需要你反复复制方案。
- 只提交已经检查过的精确路径；固定工位分支保留到实际交付。
- 使用真实 Git 合并，让原任务提交留在本地主线的祖先中。
- 同一目标分支一次只集成一个批次，组合验证通过后才推进主线。
- Start 保持轻量；项目运行环境在首次实际使用时准备。
- 本机批准只核对下一步实际会运行的命令；无关检查变化不会让日常步骤重复卡住。
- 出现冲突或验证失败时保留可恢复事实，主线在批次成功前不移动。

DWW 只负责 Git 生命周期。任务怎么拆、谁来做、何时等待由 Codex 等宿主负责；项目自己的测试、运行资源和产品决策仍由项目负责。

用大白话说：Finish 可以表示“等待集成”。任务提交进入本地主线、资源释放后，才算实际交付。

## 安装

```text
codex plugin marketplace add zhaogelz/develop-with-worktrees --ref v0.5.0-beta.7
codex plugin add develop-with-worktrees@develop-with-worktrees
```

安装或更新后，先让当前宿主加载已安装的技能再使用。只有宿主明确显示新的或变化的 Hook 定义待审查时才审查；出现错误先按报告的原因处理，不能因为版本变化或一次拒绝反复重开会话或信任。DWW 不读取或修改宿主信任存储。源码版本是 `0.5.0-beta.7`；插件缓存构建可能额外带有 `+codex.<build>` 后缀。

已确认的本地 DWW 市场在日常维护时只刷新这个插件，不反复切换市场来源。从旧本地来源首次迁移是单独、可核验的维护动作，不属于普通任务步骤。
若 DWW 自身故障阻断升级，可按[恢复路径](plugins/develop-with-worktrees/skills/develop-with-worktrees/references/recovery.md)安装已通过完整验证的恢复版，不必先完成正常合入。
正常源码任务完成后仍应 `finish` 并归还工作树；需要发布插件时，由协调者启动一个独立、短生命周期的维护任务，不把普通开发工作树留作发布用途。

## 默认怎么工作

刚创建、尚无提交的空 Git 仓库也能直接使用：确认隔离模式后，DWW 会沿用你选定的分支建立首个空提交，不会自动收录已有文件或暂存内容。

对于已有仓库，DWW 会从你路由的工作树当前附着分支开始（关联工作树同样适用），并交付回这个已记录目标；旧的远端默认分支或本地默认分支偏好不会改写该选择。

1. 宿主路由仓库，在固定独立工位启动受管任务。
2. AI 只在返回的工作树修改、运行针对性检查，并提交精确路径。
3. Ready 冻结任务提交；Finish 将它列入本地集成等待，此时工位和分支仍归该任务。
4. 满批自动集成；小批须有明确的一轮结束、用户立即集成、部署或依赖原因。DWW 真实合并任务提交、验证组合、推进本地主线，最后释放工位。

宿主继续跟进批次集成和恢复，负责拆任务、等待与消息。Finish 本身不表示已交付，也不表示当前安装的插件已经验证。

如果格式检查失败，先让格式化工具对已审查的改动路径输出差异，按差异修复后再重跑检查；不要因为一次格式错误就提前启动组合验证。

## 仅在需要时深入

- 仓库已有成熟流程时，让原流程继续负责。精确、经本机批准的[适配器](plugins/develop-with-worktrees/skills/develop-with-worktrees/references/delegated-adapters.md)是维护选项，不是默认配置步骤。
- 已有 DWW 仓库保持记录中的旧流程，先用 `migration preview --base <分支>` 查看阻塞，再在排空后执行 `migration enable --base <分支> --confirm <分支>:<提交>`。该操作不会安装插件或修改别的仓库。
- 任务中断、冲突或组合检查需要处理时，按已记录的[恢复路径](plugins/develop-with-worktrees/skills/develop-with-worktrees/references/recovery.md)操作，不猜测也不清理工作树。
- 如果复核用的测试工作树被特意保留，使用 `reclaim-retained` 返回的清单再回收；任务已结束不等于可以清空未知内容。若返回阻塞，先查看汇总报告；已合入或已确认有副本的成果无需重复备份，未知内容继续保留。
- 只有项目自身存在外部运行资源时才配置 [Runtime Adapter](plugins/develop-with-worktrees/skills/develop-with-worktrees/references/runtime-adapter.md)；多数个人和小团队仓库并不需要它。

## 保护边界

在宿主已加载并信任 Hook 的原生补丁调用中，从仓库外发起的修改也会检查目标工作树的归属；其他会话不能因此获得写入权限。

DWW 会保护未知或受保护的工作树内容，也不会根据界面状态、Hook 或等待时间猜测封批原因。冲突或组合检查失败时主线保持不动，并保留恢复依据。只有声明的输入、环境、工具事实和日志都一致时，纯检查结果才可复用；必需构建产物和可变运行状态仍会重新执行。

DWW 自身只做本地操作：不会 fetch、pull、push、创建 PR、部署、rebase、squash、amend 或改写历史。用户明确要求的远程发布是从干净、已集成基线执行的独立操作。

## 深入阅读

- 实施任务时使用已安装的[技能](plugins/develop-with-worktrees/skills/develop-with-worktrees/SKILL.md)，它会按场景路由到一份参考文档。
- 了解职责边界看[架构说明](docs/architecture.md)；维护本仓库看[开发与文档维护](docs/development.md)。
- 配置字段看[配置参考](plugins/develop-with-worktrees/skills/develop-with-worktrees/references/configuration.md)，中断或失败看[恢复参考](plugins/develop-with-worktrees/skills/develop-with-worktrees/references/recovery.md)。
