# Develop with Worktrees

## 多个 AI 同时开工，少一点互相收拾残局。

你让一个 AI 改页面，另一个修接口。它们都写好了，却碰了同一份文件。最后还得你来分清：谁改了什么，哪些能留，合起来还能不能跑。

Develop with Worktrees（DWW）是一个 Codex 插件：**给每个修改任务一份独立工作目录，做完后把改动放在一起检查，通过后再合回你的本地分支。**

[English](README.md) · [安装](#安装) · [36 秒看懂](#36-秒看懂) · [使用方式](#装好以后怎么用)

## 少操心这几件事

| 你可能遇到的麻烦 | DWW 怎么帮你 |
| --- | --- |
| 两个 AI 改到同一份文件，后写的盖掉先写的 | 每个任务在自己的目录里改，互不覆盖工作中的文件 |
| 任务都说做完了，合起来却跑不通 | 把这批改动合在一起，按项目配置检查，通过后才更新本地分支 |
| 聊天中断，回来又要解释一遍做到了哪 | 本地保存任务目标、范围和进度，续作有据可查 |
| 合并冲突或检查失败，担心成果丢了 | 保留改动和失败现场，修好后继续；成功前主线保持原样 |

一个人开几条 Codex 聊天、同时做功能和修 bug，或者小团队共用一个仓库，都可以用上这套流程。只做一个任务时也一样适用。

## 36 秒看懂

[![点击播放 DWW 宣传片：分开做，一起检查，再合回去](https://raw.githubusercontent.com/zhaogelz/develop-with-worktrees/c192f33d510e4dce713616497d93eae91753b47e/videos/develop-with-worktrees-promo/assets/overview.svg)](https://cdn.jsdelivr.net/gh/zhaogelz/develop-with-worktrees@2fbcd39b6dd9513bad7696303b4875954caad1b5/docs/media/develop-with-worktrees-promo-36s.mp4)

点击封面播放 36 秒成片（中文、无配音）。一个项目，三个任务：分开修改、一起检查，遇到问题停在合入前。[下载 MP4](https://github.com/zhaogelz/develop-with-worktrees/raw/2fbcd39b6dd9513bad7696303b4875954caad1b5/docs/media/develop-with-worktrees-promo-36s.mp4) · [视频工程](videos/develop-with-worktrees-promo/README.md)。

## 安装

需要可安装插件的 Codex、Git，以及可运行 Python 工具的 [uv](https://docs.astral.sh/uv/)。当前公开版本为 `0.5.0-beta.8`。

```text
codex plugin marketplace add zhaogelz/develop-with-worktrees --ref v0.5.0-beta.8
codex plugin add develop-with-worktrees@develop-with-worktrees
```

安装后，让 Codex 加载插件技能。若宿主提示新的或变化的 Hook 定义需要审查，按提示完成审查。

## 装好以后怎么用

1. 在 Codex 中打开你的 Git 项目，照常说要做什么，例如“帮我给登录页加上手机号登录”。
2. 首次使用时，按提示选择“每个任务使用独立目录”。已有工作流程的仓库会先沿用原流程。
3. AI 在独立目录中修改、检查，只提交这次审查过的文件；你可以在另一条聊天里继续安排别的任务。
4. AI 跟进本地合入：符合条件的一批改动会一起检查，通过后进入启动任务时记录的本地分支。

你负责说清目标，Codex 负责推进任务，DWW 负责管理这些改动怎么进出仓库。你不用每次手工创建、切换和回收工作目录。

## 先知道这几点就够了

- **“写完了”与“已合入”是两件事。** 改动可能在等同批集成；以实际合入本地分支的结果为准。具体条件见[交付流程](plugins/develop-with-worktrees/skills/develop-with-worktrees/references/native-delivery.md)。
- 隔离目录能避免工作中的文件互相覆盖，合并时仍可能出现冲突。DWW 会停下并保留现场，AI 再按任务范围处理；涉及产品取舍时由你决定。
- 检查运行什么，由你的项目配置决定。DWW 帮你执行和保留结果，检查能发现多少问题仍取决于项目本身。
- DWW 管本地 Git 流程；推送远端、创建 PR 和部署需要另外安排。任务怎么拆、AI 怎么协作，仍由 Codex 等宿主负责。

## 需要细节时再看

| 你想做什么 | 从这里开始 |
| --- | --- |
| 了解任务的完整操作 | [插件技能](plugins/develop-with-worktrees/skills/develop-with-worktrees/SKILL.md) |
| 调整检查、批次等设置 | [配置参考](plugins/develop-with-worktrees/skills/develop-with-worktrees/references/configuration.md) |
| 继续中断或失败的任务 | [恢复指南](plugins/develop-with-worktrees/skills/develop-with-worktrees/references/recovery.md) |
| 了解原理，或参与开发 | [架构说明](docs/architecture.md) / [开发指南](docs/development.md) |
| 查看版本变化 | [更新记录](CHANGELOG.md) |
