# DWW 宣传视频工程

“多个 AI 同时开工，少一点互相收拾残局。”

36 秒、1080×1920、30 FPS、中文屏幕文字、无配音。用一个项目和三个任务讲清：分开做，一起检查，再合回去。画面是流程示意。

![宣传片概览](assets/overview.svg)

## 本地预览

在本目录运行：

```text
npm run check
npm run dev -- --port 3017
```

检查通过后，在终端返回的本地 Studio 地址打开工程并播放。建议先看完整 36 秒，再拖到 22.8 秒和 29.8 秒，确认“检查通过后合回”与“冲突时暂停”两种情况。

本机需要 Node.js 22+、Chrome、FFmpeg 与 FFprobe。CLI 已从 0.7.94 更新到 0.8.163，并在 `package.json` 中固定版本，方便复现。

## 文件怎么找

| 文件 | 内容 |
| --- | --- |
| [BRIEF.md](BRIEF.md) | 受众、主张和承诺边界 |
| [frame.md](frame.md) | 全片唯一视觉母版 |
| [STORYBOARD.md](STORYBOARD.md) | 六幕时序与取帧位置 |
| [index.html](index.html) | 全部可编辑画面、文字和时间线 |
| [index.motion.json](index.motion.json) | 关键先后顺序与出现时间检查 |
| [scripts/check.py](scripts/check.py) | 仓库验证入口，兼容 Windows npm.cmd |
| [PUBLISH.md](PUBLISH.md) | 小红书与 B 站发布文案 |
| [字体说明](assets/fonts/README.md) | 字体来源与许可 |

## 导出

确认预览后再执行：

```text
npm run render -- --quality high --fps 30 --output renders/develop-with-worktrees-promo-36s.mp4
```

导出后用 FFprobe 核对时长、分辨率和帧率。工程文件、检查截图和 MP4 是不同产物；仓库首页封面链接到本工程，尚未导出的 MP4 不作为可播放视频展示。

## 维护检查

HTML、素材或时间线变化后运行 `npm run check`。仓库也登记了仅匹配视频路径的 development 检查，可通过 DWW 的受管验证入口运行。

带截图与指定取帧的检查：

```text
npm run check -- --snapshots --at 3.8,9.8,15.8,22.8,29.8,34.8
```

不要跳过实际画面核对。具体工作规则见 [AGENTS.md](AGENTS.md)。
