# 多平台发布文案

本视频为约 36 秒、1080×1920 的无配音竖版宣传片，适合小红书与 B 站竖屏投稿。发布时使用 `renders/develop-with-worktrees-promo.mp4`；若尚未渲染，先在 HyperFrames 预览中确认画面。

## 小红书

### 标题（任选其一）

1. 让多个 AI 同时写代码，项目不再被改乱
2. AI 同时改一个项目，最怕最后才发现乱了
3. 给每个 AI 一份独立副本，终于敢同时干活

### 正文

想同时让多个 Codex AI 干活？最容易出的问题，是它们动到同一份文件。

A 改登录，B 也改接口，测试又改了配置；等到要交付时，才发现项目已经被改乱。

`develop-with-worktrees` 做的事很简单：给每个 AI 一份独立副本，让它们各做各的；完成后再看清这次改了什么，确认没问题，再合回主项目。真的碰到冲突时，它会先停下来告诉你哪里冲突，不会偷偷覆盖原项目。

它解决的就是：一个人也能放心让多个 AI 同时写代码，而不用担心它们互相改乱项目。

公开 Beta 安装：

```text
codex plugin marketplace add zhaogelz/develop-with-worktrees --ref main
codex plugin add develop-with-worktrees@develop-with-worktrees
```

想看完整流程，可以从主页链接进入项目仓库。欢迎把你遇到的并行开发现场写在评论区。

### 话题

`#AI编程 #Codex #独立开发 #开发者工具 #Git #效率工具 #开源项目`

## B 站

### 标题

让多个 AI 同时写代码，项目不再被改乱

### 简介

多个 AI 同时改一个项目，真正麻烦的是：它们可能碰到同一份文件，而你通常在最后才发现项目已经乱了。

`develop-with-worktrees` 给每个 AI 一份独立副本，让它们各改各的；完成后检查改动，再把确认无误的内容合回主项目。发现冲突时先停下并指出位置，不会偷偷覆盖原项目。

公开 Beta：

```text
codex plugin marketplace add zhaogelz/develop-with-worktrees --ref main
codex plugin add develop-with-worktrees@develop-with-worktrees
```

项目仓库：<https://github.com/zhaogelz/develop-with-worktrees>

### 推荐标签

`AI编程` `Codex` `Git` `开发者工具` `效率工具` `独立开发`

### 置顶评论

你让多个 AI 同时写代码时，最怕的是改到同一份文件，还是最后合并才发现乱了？欢迎留言说说你的现场。

## 发布检查

- 小红书：首帧/封面优先使用“多个 AI 同时写代码，项目不再被改乱”；正文保留前两段痛点，不要直接贴过长安装说明。
- B 站：上传竖版视频，简介保留安装命令和仓库链接；置顶评论用于收集真实痛点。
- 两个平台都避免承诺未验证的性能数据或兼容性结论。
