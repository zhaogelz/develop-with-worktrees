# 视频工程规则

本目录只保存 36 秒（1080×1920、30 FPS、无配音）推广视频的源文件和可复现的发布资料。

## 开始前

修改视频、动效、渲染或媒体时，先阅读适用的 HyperFrames 技能；它规定构图、时间线和渲染的实现方式。以下资料按职责读取：

| 需要确定的内容 | 权威资料 |
| --- | --- |
| 目标、受众和限制 | `[BRIEF.md](BRIEF.md)` |
| 视觉语言 | `[frame.md](frame.md)` |
| 镜头顺序与时长 | `[STORYBOARD.md](STORYBOARD.md)` |
| 平台发布文案 | `[PUBLISH.md](PUBLISH.md)` |
| 工程导航与命令 | `[README.md](README.md)` |

## 工作约束

- 时间线、字幕和画面结构写在 `[index.html](index.html)`；不要用截图代替可编辑文字或布局。
- 改动 HTML 后必须运行 `npm run check`。修复错误后才可交付。
- 先预览确认，再渲染；渲染命令是 `npm run render -- --quality high --output renders/develop-with-worktrees-promo-36s.mp4`。
- 渲染和发布会产生外部副作用，除非明确要求，不自动执行。
- 字体与素材许可保留在 `[assets/](assets/)`；不要删除来源与许可记录。
