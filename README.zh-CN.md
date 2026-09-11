# Develop with Worktrees

多个 AI 或多个任务同时修改一个 Git 项目时，很容易互相覆盖、在错误目录提交，或者没有完整验证就进入主线。DWW 会给每个修改任务分配独立目录，记录任务目标，只提交明确检查过的文件，并在验证通过后安全合入。

## 它能带来什么

- 每个任务在自己的工作树施工，互不覆盖。
- 任务目标和验收条件写在本地任务锚点里，换模型或续作也不容易跑偏；任务进行中可以用 `anchor show/update` 读取和保存经过校验的上下文。
- 每个任务只提交明确检查过的路径，完成后成为不可修改的候选。
- 多任务默认每满 5 个候选集中组合、完整验证并合入。
- 冲突、测试失败或中途退出时主线不动，可以从已记录状态恢复。
- 独立检查通过后及时保存结果；新一轮完整验证只复用输入完整且不依赖可变环境的检查。数据库、浏览器和需要重新生成的构建产物不会因为旧报告成功而被跳过。

任务怎么拆、谁先做、谁依赖谁，由 Codex 等宿主自带的任务系统负责；DWW 只负责这些成果怎样安全进入 Git 主线。

## 默认怎么工作

1. `Start` 建立精确任务锚点和专用工作树；项目配置了 Runtime Adapter 时，先由项目建立运行身份，再把任务作为可修改状态返回。
2. AI 只在该工作树修改，用 `commit` 提交精确路径，再执行 `ready`。
3. `Finish` 发布已验证候选并释放开发工作树，主线暂时不动。
4. 每满 5 个候选，DWW 自动冻结最早 5 个，在独立集成工作树完成组合；若项目配置批次 Adapter，则先建立该批次专属的端口、数据库或浏览器验证环境，再执行一次 Full，可靠释放后才推进主线。
5. 不足 5 个时，只有当同一通道没有修改任务并稳定 90 秒，或用户、部署、下游依赖明确要求时，才冻结当时精确的等待候选。

普通用户不用手工抄候选 ID。DWW 从持久化状态选择已经 `Finish` 且已释放项目运行资源的不可变候选；宿主 heartbeat 只负责在 `next_reconcile_at` 唤醒检查。没有可靠定时唤醒能力的宿主，不能声称支持自动静默尾批。

释放任务时保留依赖缓存，正常的包链接不会阻塞收尾。新仓库的批次也串行复用一个验收目录：普通成功或失败只归还使用权，不递归删除、哈希整套依赖；每轮 Full 所需的数据库、认证等效果仍重新验证。物理磁盘清理另作维护，不放在普通收尾中；具体保护见[清理安全约定](plugins/develop-with-worktrees/skills/develop-with-worktrees/references/safety.md)。

## 为什么默认是 5 和 10

一批 5 个可以减少频繁完整验证，同时把冲突范围控制在容易排查的大小。候选池容量 10，通常可让一批正在集成时，下一批继续积累。容量满时 DWW 会保留当前任务并要求稍后重试，不会丢弃成果。

## DWW 不会做什么

- 不根据界面任务数、原始工作树数量、Hook 或会话结束猜测候选；这些信号最多唤醒检查。
- 不设置候选最长等待自动封尾；仍有修改任务时，尾批会继续等待，除非出现明确的用户、部署或下游依赖要求。
- 不接管 Codex 的任务拆分、子代理调度和依赖关系。
- 不自动 fetch、pull、push、创建 PR、部署、rebase、squash、amend 或改写历史。
- 不把端口、数据库、浏览器、业务测试和部署规则从项目里搬走。

Hook 只是可选的提前拦截或唤醒来源。即使没有安装或信任 Hook，路由、任务锚点、工作树、验证、候选、批次和恢复仍须完整可用。

项目可以配置 Runtime Adapter：`Start` 时，DWW 只提供精确任务、槽位、工作树、基线和确定性的端口段事实；候选尚未产生，因此不传 `candidate_head`，但 DWW 仍会在调用 Adapter 前后自行确认干净工作树等于冻结基线。候选固化后，再由项目释放开发资源，成功后 DWW 才释放工作树并让候选进入批次。组合完成后，可选的 `batch_activate`/`batch_release` 用独立于 32 个任务槽位的固定端口段包住一次 Full。上下文携带持久化的 `runtime_cycle`：命令中断只重试同一周期和精确收据，资源已成功释放后再恢复则进入新周期并真实重新激活。激活或释放不确定时主线不动，通过同一批次恢复。DWW 不解释端口、数据库、浏览器等具体规则。候选发布不等于交付；只有批次进入当前主线才算交付。用户明确要求核对运行版本时，再由项目 Adapter 做运行态核验。

组合阶段若能明确定位到某个冲突候选，DWW 可在最新主线上准备最多两代受管返修；只有代码、契约和测试能唯一决定结果时才自动继续，产品、权限、迁移、删除或安全取舍仍交给人决定。最终验证失败不会冒充合并冲突盲目重跑。若已诊断的外部阻塞发生变化而候选未变，`batch seal --after-failed-batch <id>` 会显式且幂等地创建该失败代次唯一的已审查后继。

复用模式的批次失败后，可用 `batch retire --batch <id>` 完成安全归还；旧批次迟到退役不能删除后来者的工作区。旧专用目录批次保留精确物理退役。对于已明确批准清理、且候选全部已被替代的历史失败专用批次，可用 `batch retire --fast --batch <id>`：仍核对 Git、目录身份、链接、受保护和未知内容，只跳过可再生依赖的逐文件内容哈希，并通过暂存目录和收据支持幂等重试。候选 ref 和审计事实始终保留。调整批量前先用只读 `batch metrics` 查看满批率、尾批率、候选等待和实际 Full 成本，不凭感觉改默认值。

旧项目可以继续显式使用 direct 或手工封批策略；它们只用于平稳升级，新项目默认使用候选流水线。

## 安装

```text
codex plugin marketplace add zhaogelz/develop-with-worktrees --ref v0.5.0-beta.1
codex plugin add develop-with-worktrees@develop-with-worktrees
```

安装或更新后新开一个 Codex 会话，使新版技能文案稳定加载。DWW 的本地生命周期不包含远程发布；只有用户另行明确要求时，才可从已合入且干净的基线工作树执行 dry-run 优先的普通非强制推送。

详细配置、升级兼容、异常恢复和安全原理见[配置参考](plugins/develop-with-worktrees/skills/develop-with-worktrees/references/configuration.md)、[生命周期参考](plugins/develop-with-worktrees/skills/develop-with-worktrees/references/lifecycle.md)、[任务治理参考](plugins/develop-with-worktrees/skills/develop-with-worktrees/references/task-governance.md)和[安全参考](plugins/develop-with-worktrees/skills/develop-with-worktrees/references/safety.md)。
