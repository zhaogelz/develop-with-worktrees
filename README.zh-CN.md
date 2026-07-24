# Develop with Worktrees

`0.2.0-beta.4` 是一套默认不让并行 AI 改动互相覆盖的本地 Git 工作流。生命周期内核不绑定 Codex；本插件提供 Codex 的首次选择、技能和写入保护适配。

## 第一次修改时，只问一个问题

```text
此仓库怎么修改？

1. 每个任务使用独立目录（推荐）
   任务互不影响，完成后自动合回。

2. 这一次直接改当前目录
   只跳过这一次，下次还会询问。

3. 以后都直接改当前目录
   记住此选择，这个仓库不再询问。

只影响本机，可随时修改。
```

- 选 1 后，普通修改自动在独立目录完成，结束时只在本地合回当前派生分支；不会把 `main` 写死。
- 选 2 后，这次任务就在当前目录按普通方式修改，插件不创建配置、不启动任务，也不要求 Commit/Ready/Finish。下次新开任务再问。
- 选 3 后，只在这台机器的此仓库记住“直接改当前目录”；不提交任何设置，换电脑或换克隆目录仍由各自选择。

如果没有发现自动测试，选 1 仍会隔离任务并在内部执行基础检查，不会再弹出一个确认。用户以后说“这个仓库以后使用独立目录开发”或“以后直接在当前目录开发”即可改变本机选择。

## 选独立目录后会发生什么

```text
doctor → start → 只在返回的目录修改 → commit 精确路径
       → plan / verify → ready → finish
```

- 每个普通任务独占一个可复用目录和租约；Finish 只在本地快进合回记录的干净基线工作树。
- schema 3 验证会按改动路径选择 profile，并走本机 machine-global weighted FIFO 验证队列。预计慢时只提示拆分映射、减少重复准备，绝不偷偷少测。
- Finish 不删除依赖、缓存或测试数据；清理只能手工 `prune-slot`，并先看精确计划再确认。

`start --in-place` 仍保留给明确要求“在当前目录但仍要 DWW 的精确提交、验证和 Finish 保护”的高级兼容场景；它不是选 2 的含义。

Codex 中，用户在 `/hooks` 信任插件钩子后，`PreToolUse` 会在支持的本地工具路径上真正拒绝受保护工作目录的未授权写入（包括 `apply_patch` 和常见 Bash 写命令），不是只提醒。它不是操作系统沙箱：少数专用路径可能不经过钩子；只有后续又经过钩子的操作或执行 `doctor` 观察到逃逸脏改动时，才会保留并记录警报，不能承诺立即发现。每次安装或钩子升级后，都要在 `/hooks` 重新信任，再新开任务；未信任前不能把保护当作已启用。

## 安装

```text
codex plugin marketplace add zhaogelz/develop-with-worktrees --ref main
codex plugin add develop-with-worktrees@develop-with-worktrees
```

插件不会自动升级，也绝不会 fetch、pull、push、创建 PR、rebase、squash、amend 或改写历史；团队仍按自己的节奏提交和创建 PR。
