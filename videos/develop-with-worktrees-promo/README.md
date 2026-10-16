# DWW 宣传视频工程

“多个 AI 同时开工，少一点互相收拾残局。”

36 秒、1080×1920、30 FPS、中文屏幕文字、无配音。用一个项目和三个任务讲清：分开做，一起检查，再合回去。画面是流程示意。

![宣传片概览](assets/overview.svg)

## 本地预览

在本目录运行：

```text
uv run python scripts/check.py
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

导出后检查成片，以下命令都在本目录执行：

```text
ffprobe -v error -show_entries stream=codec_type,codec_name,width,height,avg_frame_rate,nb_frames,duration -of json renders/develop-with-worktrees-promo-36s.mp4
ffmpeg -v error -xerror -i renders/develop-with-worktrees-promo-36s.mp4 -f null -
```

预期只有一条 H.264 视频流：1080×1920、`30/1` FPS、1080 帧、36 秒，无音轨；第二条命令应完整解码通过。再播放一遍，检查六幕文字、转场和片尾安装入口。

工程文件、检查截图和 MP4 是不同产物。MP4 保存在本地导出目录，仓库首页封面链接到本工程；有可公开播放的成片地址后，再更新首页的视频入口。

## 维护检查

HTML、素材或时间线变化后运行 `uv run python scripts/check.py`：先检查本地字体是否覆盖全部画面文字，再运行 `npm run check`。仓库也登记了仅匹配视频路径的 development 检查，可通过 DWW 的受管验证入口运行。

带截图与指定取帧的检查：

```text
uv run python scripts/check.py --snapshots --at 3.8,9.8,15.8,22.8,29.8,34.8
```

不要跳过实际画面核对。具体工作规则见 [AGENTS.md](AGENTS.md)。
