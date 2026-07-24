# Develop with Worktrees

`0.2.0-beta.2` 是一套“默认隔离、必要时受控直改”的本地 Git 工作流。生命周期内核不绑定 Codex；本插件提供 Codex 的技能和写入保护适配。

## 大多数时候：自动用隔离工作树

```text
doctor → start → 只在返回的工作树修改 → commit 精确路径
       → plan / verify → ready → finish
```

- 从你当前所在的本地分支派生，不把 `main` 写死。
- 每个普通任务独占一个可复用工作树和租约；Finish 只在本地快进合回记录的干净基线工作树。
- schema 3 验证会按改动路径选择 profile，并走本机 machine-global weighted FIFO 验证队列。预计慢时只提示拆分映射、减少重复准备，绝不偷偷少测。
- Finish 不删除依赖、缓存或测试数据；清理只能手工 `prune-slot`，并先看精确计划再确认。

## 真正需要当前环境时：一次受控直改

例如测试数据、数据库状态或缓存只在当前工作树可用，且用户明确说“只在当前工作树执行 / 直接在主分支改”时，才使用：

```text
start --in-place --session <本次 Codex 会话标识>
→ 当前工作树修改 → commit 精确路径 → ready → finish
```

通俗说：这不是关闭保护后随便改，而是给“这一次、这一个 Codex 会话、这一个当前分支”发一张临时通行证。

- 开始时 Git 必须干净；被 `.gitignore` 忽略的测试缓存可保留。
- 不新建槽位、不新建分支、不删缓存；验证永远相对开始瞬间的提交，所以提交后也不会漏掉本次改动。
- Finish 不合并、不切换分支、不删除任何文件。分支、HEAD 或会话变了就冻结并保留现场，绝不自动回滚、搬运或清理；前一 Codex 任务结束时，可精确确认 `resume-in-place` 接管未变化任务，若身份漂移仍须先人工恢复记录的分支和 HEAD。
- 直改任务进行时，隔离任务仍能开发；但不能把同一基线分支 Finish 合入，以免悄悄改变直改任务的身份。

Codex 中，用户在 `/hooks` 信任插件钩子后，`PreToolUse` 会在支持的本地工具路径上真正拒绝当前基线工作树的未授权写入（包括 `apply_patch` 和常见 Bash 写命令），不是只提醒。它不是操作系统沙箱：少数专用路径可能不经过钩子；只有后续又经过钩子的操作或执行 `doctor` 观察到逃逸脏改动时，才会保留并记录警报，不能承诺立即发现。每次安装或钩子升级后，都要在 `/hooks` 重新信任，再新开任务；未信任前不能把直改保护当作已启用。

## 安装

```text
codex plugin marketplace add zhaogelz/develop-with-worktrees --ref main
codex plugin add develop-with-worktrees@develop-with-worktrees
```

安装后新开 Codex 任务，在 `/hooks` 信任本插件，然后执行 `doctor`。插件不会自动升级，也绝不会 fetch、pull、push、创建 PR、rebase、squash、amend 或改写历史；团队仍按自己的节奏提交和创建 PR。
