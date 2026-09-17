# develop-with-worktrees 推广视频工程

这是面向小红书与 B 站的 36 秒竖版推广视频源工程：1080×1920、30 FPS、无配音。最终样片输出为 `renders/develop-with-worktrees-promo-36s.mp4`。

| 资料 | 用途 |
| --- | --- |
| `[BRIEF.md](BRIEF.md)` | 目标、受众与限制 |
| `[frame.md](frame.md)` | 视觉方向 |
| `[STORYBOARD.md](STORYBOARD.md)` | 分镜与时序 |
| `[PUBLISH.md](PUBLISH.md)` | 小红书与 B 站文案 |
| `[assets/fonts/NotoSerifSC-400.ttf](assets/fonts/NotoSerifSC-400.ttf)` | 本地字体；同目录保留 OFL 1.1 许可证 |

## 预览与渲染

`npm run dev -- --port 3017` 启动预览服务器。确认画面后，使用：

`npm run render -- --quality high --output renders/develop-with-worktrees-promo-36s.mp4`

本机需要可用的 Chrome、FFmpeg 与 FFprobe。改动 `index.html` 后先运行 `npm run check`。
