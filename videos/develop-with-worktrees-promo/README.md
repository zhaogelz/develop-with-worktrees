# develop-with-worktrees 推广视频工程

这是面向小红书与 B 站的 36 秒竖版推广视频源工程。

- 成片规格：1080×1920、30 FPS、无配音、约 36 秒。
- 画面风格：深蓝任务指挥台、层叠终端与工作流状态视觉。
- 分镜与时序：见 `STORYBOARD.md`。
- 发布文案：见 `PUBLISH.md`。
- 本地字体：`assets/fonts/NotoSerifSC-400.ttf`，遵循同目录的 OFL 1.1 许可证。
- 最终样片：`renders/develop-with-worktrees-promo-36s.mp4`。

## 预览与渲染

```text
npx hyperframes preview --port 3017
npx hyperframes render --quality high --output renders/develop-with-worktrees-promo-36s.mp4
```

首次渲染前，请先完成预览确认；渲染环境需要可用的 Chrome、FFmpeg 与 FFprobe。
