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

## 安装

```text
codex plugin marketplace add zhaogelz/develop-with-worktrees --ref v0.5.0-beta.7
codex plugin add develop-with-worktrees@develop-with-worktrees
```

安装或更新后，当前宿主需要先加载已安装的技能，正在运行的任务才会看到新内容。只有当前宿主仍显示旧版行为、且没有可用的重新加载方式时，才新开一个 Codex 会话；不要仅因版本号变化、一次拒绝或解析/路径/owner/lease 错误而重开会话或重跑验收。加载技能与 Hook 信任是两件事：版本更新本身不等于需要重新信任。只有宿主明确报告首次安装或 Hook 定义变化待审查时，才通过宿主支持的方式审查一次准确的定义；DWW 不读取或修改宿主信任存储。源码版本是 `0.5.0-beta.7`；插件缓存构建可能额外带有 `+codex.<build>` 后缀。

## 默认怎么工作

刚创建、尚无提交的空 Git 仓库也能直接使用：确认隔离模式后，DWW 会沿用你选定的分支建立首个空提交，不会自动收录已有文件或暂存内容。

1. 宿主先路由仓库。受管修改在独立工作树开始；已有成熟流程的仓库继续由原流程负责。
2. AI 只在返回的工作树修改，并提交明确检查过的路径。
3. `finish` 固化源码候选，表示本轮编码结束，并不等于已经进入主线。
4. 每满 3 个兼容候选，DWW 会在集成工作树中组合改动，并运行组合改动影响到的仓库检查。
5. 不足 3 个的收尾候选必须记录 `round-complete`、`user`、`deploy` 或 `dependency` 等明确原因；空闲时间和任务数量都不能当作本轮结束的依据。

宿主会继续跟进候选的集成、恢复或需要决策的失败。普通流程不需要手工抄候选 ID。

## 保护边界

DWW 会保护未知或受保护的工作树内容，也不会根据界面状态、Hook 或等待时间猜候选。冲突或组合检查失败时主线保持不动，并保留恢复依据。只有声明的输入、环境、工具事实和日志都一致时，纯检查结果才可复用；必需构建产物和可变运行状态仍会重新执行。

DWW 自身只做本地操作：不会 fetch、pull、push、创建 PR、部署、rebase、squash、amend 或改写历史。用户明确要求的远程发布是从干净、已集成基线执行的独立操作。

## 深入阅读

- 实施任务时使用已安装的[技能](plugins/develop-with-worktrees/skills/develop-with-worktrees/SKILL.md)，它会按场景路由到一份参考文档。
- 了解职责边界看[架构说明](docs/architecture.md)；维护本仓库看[开发与文档维护](docs/development.md)。
- 配置字段看[配置参考](plugins/develop-with-worktrees/skills/develop-with-worktrees/references/configuration.md)，中断或失败看[恢复参考](plugins/develop-with-worktrees/skills/develop-with-worktrees/references/recovery.md)。
