# Develop with Worktrees

多个 AI 同时修改一个 Git 仓库时，容易互相覆盖、在错误目录提交，或在没有组合验证的情况下进入主线。Develop with Worktrees（DWW）为每个修改任务准备独立工作树，在本地记录任务约束，并把已检查的改动带入本地集成。

## 它能带来什么

- 每个受管修改任务使用独立工作树。
- 本地任务锚点保存目标、范围、基线和验收，续作或交接不必重新猜上下文。
- 已确认的完整方案只保留一份主锚点，相关任务续作时回到同一依据，不需要你反复复制方案。
- 只提交已经检查过的精确路径，并固化为不可变源码候选。
- 兼容候选组合后再按影响运行仓库检查，并保护性推进本地主线。
- 本机批准只核对下一步实际会运行的命令；无关检查变化不会让日常步骤重复卡住。
- 出现冲突或验证失败时保留可恢复事实，主线在批次成功前不移动。

DWW 只负责 Git 生命周期。任务怎么拆、谁来做、何时等待由 Codex 等宿主负责；项目自己的测试、运行资源和产品决策仍由项目负责。

用大白话说：“已合入”是改动已经进入本地主线；“保留工作树”是留给复核，还不能再用；“可复用”才是安全归还的槽位。

## 安装

```text
codex plugin marketplace add zhaogelz/develop-with-worktrees --ref v0.5.0-beta.7
codex plugin add develop-with-worktrees@develop-with-worktrees
```

安装或更新后，先让当前宿主加载已安装的技能再使用。只有宿主明确显示新的或变化的 Hook 定义待审查时才审查；出现错误先按报告的原因处理，不能因为版本变化或一次拒绝反复重开会话或信任。DWW 不读取或修改宿主信任存储。源码版本是 `0.5.0-beta.7`；插件缓存构建可能额外带有 `+codex.<build>` 后缀。

## 默认怎么工作

刚创建、尚无提交的空 Git 仓库也能直接使用：确认隔离模式后，DWW 会沿用你选定的分支建立首个空提交，不会自动收录已有文件或暂存内容。

1. 宿主先路由仓库。受管修改在独立工作树开始；已有成熟流程的仓库继续由原流程负责。
2. AI 只在返回的工作树修改，并提交明确检查过的路径。
3. `finish` 固化用于本地交付的不可变源码候选，并自动归还任务工作树；它不会自行推进主线。
4. 每满 3 个兼容候选，DWW 会在集成工作树中组合改动，并运行受组合改动影响的仓库检查。
5. 实际一轮结束且不足 3 个的收尾候选需要交付时，AI 会记录一个受支持的交付原因并继续跟进尾批；空闲时间、任务数量和界面状态都不能当作该原因。

宿主会继续跟进候选的集成、恢复或需要决策的失败。任务怎么拆、何时等待和如何通知仍由宿主负责；普通流程不需要手工抄候选 ID。

## 仅在需要时深入

- 仓库已有成熟流程时，让原流程继续负责。精确、经本机批准的[适配器](plugins/develop-with-worktrees/skills/develop-with-worktrees/references/delegated-adapters.md)是维护选项，不是默认配置步骤。
- 任务中断、冲突或组合检查需要处理时，按已记录的[恢复路径](plugins/develop-with-worktrees/skills/develop-with-worktrees/references/recovery.md)操作，不猜测也不清理工作树。
- 如果复核用的测试工作树被特意保留，使用 `reclaim-retained` 返回的清单再回收；任务已结束不等于可以清空未知内容。
- 只有项目自身存在外部运行资源时才配置 [Runtime Adapter](plugins/develop-with-worktrees/skills/develop-with-worktrees/references/runtime-adapter.md)；多数个人和小团队仓库并不需要它。

## 保护边界

在宿主已加载并信任 Hook 的原生补丁调用中，从仓库外发起的修改也会检查目标工作树的归属；其他会话不能因此获得写入权限。

DWW 会保护未知或受保护的工作树内容，也不会根据界面状态、Hook 或等待时间猜候选。冲突或组合检查失败时主线保持不动，并保留恢复依据。只有声明的输入、环境、工具事实和日志都一致时，纯检查结果才可复用；必需构建产物和可变运行状态仍会重新执行。

DWW 自身只做本地操作：不会 fetch、pull、push、创建 PR、部署、rebase、squash、amend 或改写历史。用户明确要求的远程发布是从干净、已集成基线执行的独立操作。

## 深入阅读

- 实施任务时使用已安装的[技能](plugins/develop-with-worktrees/skills/develop-with-worktrees/SKILL.md)，它会按场景路由到一份参考文档。
- 了解职责边界看[架构说明](docs/architecture.md)；维护本仓库看[开发与文档维护](docs/development.md)。
- 配置字段看[配置参考](plugins/develop-with-worktrees/skills/develop-with-worktrees/references/configuration.md)，中断或失败看[恢复参考](plugins/develop-with-worktrees/skills/develop-with-worktrees/references/recovery.md)。
