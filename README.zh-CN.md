# Develop with Worktrees

`0.2.0-beta.1` 是给 Codex 使用的本地优先多工作树流程：多个任务分别在隔离工作树中开发，提交时只允许精确路径，验证有证据，最后只在本地合回你开始任务时所在的分支。

它不是固定把所有任务合到 `main`。例如你在 `feature/order` 启动任务，成功 Finish 后只推进 `feature/order`；团队仍可自行推送并创建 PR。

## 日常怎么用

```text
doctor → start → 在返回的工作树里修改 → commit 精确路径
       → plan / verify（可选开发期反馈）→ ready → finish
```

- `verification.toml` 只支持 schema 3；profile 分为 `development`、`ready`、`full`。`full` 必须显式执行，日常 Ready 不会偷偷跑它。
- `plan` 会告诉你哪些改动被哪些验证覆盖、预计耗时和是否有未映射路径。超过十分钟只会提醒你拆分映射或减少重复准备，不会自动减少验证。
- 所有仓库共用本机验证队列：普通验证占一个容量，重型验证独占当前容量。默认按物理 CPU 核数和内存自动计算；可运行 `dww settings --validation-capacity auto|1..4` 只调整本机，不修改仓库文件。
- Finish 不会删除 `.venv`、依赖或缓存。只有手动 `prune-slot`，并且仓库在 `cleanup.owned_paths` 精确声明过顶层路径，才会先给你路径、原因、大小和内容摘要计划。目标内部有 `.env*`、链接/junction 或在确认后发生变化时，整次删除都会停止。

## 安装与升级

把本仓库作为本地 Codex marketplace 添加后安装插件，再新开一个 Codex 任务加载技能。插件不会自动发布、自动更新，也不会扫描或迁移其他项目。

旧 schema 2 项目只迁移自己的 `.solo-ai/verification.toml` 到 schema 3 后再使用本版本。首次采用仓库时先查看 `init` 计划，只有用户明确 `init --accept` 才会写入受管策略。

它从不 fetch、pull、push、创建 PR、rebase、squash、amend 或改写历史。发现仓库已有成熟工作树流程时，会完全让位、不写任何状态。
