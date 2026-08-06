---
version: 2
name: Mission Control — Frame
description: 高密度但克制的开发任务指挥台视觉，用真实工作流状态替代抽象 AI 装饰。
unit: 1080x1920 portrait frame
principle: 每帧都有可读的工作状态、清晰的主叙事和至少两处视觉焦点。

colors:
  canvas: "#071321"
  panel: "#0C2038"
  panel-2: "#112B49"
  ink: "#EAF2FF"
  ink-muted: "#9BB6D8"
  cobalt: "#4D7CFF"
  cyan: "#71D7FF"
  risk: "#FF8B7A"
  verified: "#86E6B1"
  line: "rgba(137, 177, 230, 0.28)"

typography:
  display: { fontFamily: "Noto Serif SC", weight: 400 }
  ui: { fontFamily: "Noto Serif SC", weight: 400 }
  mono: { fontFamily: "ui-monospace, Consolas, monospace", weight: 400 }

spacing:
  edge: "5.2cqw"
  safeTop: "8cqh"
  safeBottom: "8cqh"

components:
  mission-grid: "细密蓝黑网格 + 低透明径向辉光；不可纯色留白。"
  terminal-panel: "深色方角面板、2px 边线、真实任务/路径/状态字段；可叠放形成深度。"
  status-chip: "只用于风险、校验、就绪等工作流状态；颜色必须有语义。"
  route-line: "任务节点间的细线或方形进度条，动效跟随实际步骤。"
  chrome: "顶部任务序号、底部阶段名称与时间刻度。"
---

# Mission Control — Frame

## 视觉目标

- 不是海报，也不是泛化 AI HUD；每帧都应像一个可运行的开发任务指挥台。
- 通过终端行、工作区路径、状态芯片、任务节点与验证结果建立“真实可控”的产品感。
- 第一帧先给出结果；第二帧短暂压迫；第三、四帧从混乱转为清晰；第五帧证明冲突不会被偷偷覆盖；第六帧收束成发布行动。

## 画面层级

1. 背景：深蓝网格、极轻的径向辉光、1–2 处低透明装饰线。
2. 中景：一个主终端/工作区面板，加 2–4 个有明确身份的支持面板。
3. 前景：中文主张、任务状态或 CTA；不使用空泛图标和随机数字。

## 构图规则

- 每帧至少有两个视觉焦点，并以左上—右下或右上—左下的阅读路径组织。
- 主文案大而短；面板内使用真实能力词：隔离、精确提交、验证预检、顺序本地合入。
- 深度用面板尺度、边线和叠放关系表达，不使用玻璃拟态、厚重阴影或霓虹粒子。
- 风险只用 `risk`，校验成功只用 `verified`，其他高亮统一用 `cobalt` / `cyan`。

## 动效规则

- 每帧遵循 build → breathe → resolve：前 30% 组装，之后保持一处轻微的可寻址环境运动，末段完成收束。
- 面板从不同方向或以不同尺度进入；不要所有元素同时淡入。
- 场间转场保持原有语义：混乱时快速切换，释放与闭环时平稳收束。
