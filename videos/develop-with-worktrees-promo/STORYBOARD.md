---
format: 1080x1920
duration: 36s
message: "让多个 AI 同时写代码，也不把项目改乱。"
arc: "结果先行 → 看见混乱 → 分开修改 → 检查合回 → 冲突先停 → 行动号召"
audience: "想让多个 AI 同时帮忙写代码、但担心项目被改乱的开发者"
mode: autonomous
music: none
---

## Video direction

- **一句话先讲明白**：首屏直接说“多个 AI 同时写代码，也不会把项目改乱”；机制只为这句话提供证据，不让观众先猜产品是什么。
- **小白文案**：所有主文案优先使用“各改各的副本”“检查后再合回去”“冲突先停下”“原项目保持不动”。`worktree`、`commit`、`validation` 只能作为面板小字，不能承担理解任务。
- **视觉系统**：沿用深蓝 `Mission Control` 任务指挥台；用一个主项目、多个 AI 任务副本、检查结果和暂停挡板表现能力。风险只用 `risk`，安全结果只用 `verified`。
- **运动语法**：每幕按说话节拍逐项出现，使用长尾减速，不弹跳；后半程只在新信息出现时运动，结论出现后静止停读。第五幕是全片安全证据，状态从“发现冲突”明确变为“已暂停”。
- **节奏**：第一幕先给结果；第二幕加压；第三幕释放；第四幕解释；第五幕建立信任；第六幕收束。第四、第五幕允许更长停读，避免 36 秒样片变成信息轰炸。
- **禁止项**：不使用“工作区”“交付闭环”“精确提交”“验证预检”等术语作为大标题；不使用虚构数据、霓虹粒子、人物、浏览器外框、懒惰呼吸或所有元素同时漂浮。
- **全片**：静音，36 秒竖版；内容保持在画面上方约 83%，为平台字幕与按钮留出安全区域。

## Frame 1 — 一眼看懂它解决什么

- scene: 先直接给出“多个 AI 同时写代码，也不会把项目改乱”的结果，再用三个同时运行的 AI 任务卡说明它解决的是哪个现场。
- voiceover: "让多个 AI 同时写代码，也不会把项目改乱。"
- duration: 5s
- poster: 3.8s
- transition_in: cut
- status: animated
- src: compositions/frames/01-not-by-luck.html
- type: hook
- persuasion: Outcome-first promise
- beat: recognition + relief
- blueprint: kinetic-type-beats (Adapt)
- asset_candidates:

focal: “多个 AI 同时写代码，也不会把项目改乱”两行主张与三个 AI 任务卡。

roles: 深蓝网格为 background；主任务面板和两张风险任务卡为 supporting；结果主张与“各改各的副本”提示为 cutout。

Adapt: 保留任务卡从两侧压入的签名动作，但第一拍先给出产品结果，不让观众等到结尾才知道它解决什么。

Scene 1 (0.0–1.1s): 上方先出现“让多个 AI 同时写代码”，主标题随即补全“也不会把项目改乱”；上方主张占画面 70% 宽度，中央主任务面板开始建立。

Scene 2 (1.1–3.2s): 两张 AI 任务卡从左右压入，依次出现“AI B 也在改登录”“AI C 正在跑测试”，风险标签亮起“改到同一处”；三层深度让问题一眼可见。

Scene 3 (3.2–5.0s): 底部结论落为“办法：每个 AI 先各改各的副本”，风险主标题从警示色转为安全色，停读后进入下一幕。

narrativeRole: 在第一秒就让普通观众知道产品的用途，再用熟悉的并行改代码现场承接兴趣。

keyMessage: 这是一个避免多个 AI 把同一项目改乱的工具。

## Frame 2 — 为什么容易乱

- scene: 三个 AI 任务同时碰到同一份项目内容，问题通常拖到最后才暴露。
- voiceover: "一个改登录，一个改接口，还有一个在跑测试；它们可能都碰到同一处。"
- duration: 6s
- poster: 4.8s
- transition_in: zoom-through
- status: animated
- src: compositions/frames/02-the-mess.html
- type: pain_point
- persuasion: Pain agitation
- beat: overwhelm
- blueprint: overwhelm-surround (Adapt)
- asset_candidates:

focal: 中央“同一个项目”面板，被三个 AI 任务卡和风险连线包围。

roles: 深蓝网格为 background；同一项目面板为 midground；三个 AI 任务卡、冲突连线和最终结论为 foreground。

Adapt: 保留中心对象不动、风险从四面靠近的结构；所有标签都写成“谁在改什么”，不用 Git 术语。

Scene 1 (0.0–1.3s): 标题“同一个项目，被三件事同时改”进入上半区；中央项目面板先建立。

Scene 2 (1.3–3.7s): 三张卡依次出现：“AI A 改登录”“AI B 改接口”“AI C 跑测试”；连线逐条收向“登录页面”和“项目配置”。

Scene 3 (3.7–4.8s): 风险标签依次落为“同一文件”“互相不知道”“最后才发现”，画面密度达到最高点。

Scene 4 (4.8–6.0s): 底部落“等要交付，才发现东西乱了”，所有风险卡停止运动并停读。

narrativeRole: 把“项目被改乱”从一句警告变成看得见的因果关系。

keyMessage: 多个 AI 如果直接改同一份项目，冲突往往最后才被发现。

## Frame 3 — 给每个 AI 一份独立副本

- scene: 同一项目拆成四个互不干扰的 AI 任务副本，画面从拥挤转为清晰。
- voiceover: "解决办法很简单：每个 AI 各改各的副本。"
- duration: 6s
- poster: 4.8s
- transition_in: squeeze
- status: animated
- src: compositions/frames/03-own-workspace.html
- type: product_intro
- persuasion: Friction reduction
- beat: relief + control
- blueprint: logo-assemble-lockup (Adapt)
- asset_candidates:

focal: “每个 AI 各改各的副本”主张与四张独立 AI 任务卡。

roles: 网格为 background；四个任务副本为 supporting；中间的产品名与一句话解释为 cutout。

Adapt: 保留四张卡围绕中心锁定的动作；卡片直接说明 AI 在做什么，并把独立性解释成“只改自己的副本”。

Scene 1 (0.0–1.2s): 拥挤画面被压缩离开，标题“每个 AI，各改各的”进入上方。

Scene 2 (1.2–4.1s): 登录、接口、测试、文档四张卡逐张落位；每张卡都出现“只改自己的副本”，路线在卡片之间保持断开。

Scene 3 (4.1–5.0s): 中央出现 `develop-with-worktrees` 和解释“让它们不再互相碰文件”。

Scene 4 (5.0–6.0s): 结果卡亮起“不会互相改乱”，其余元素静止停读。

narrativeRole: 用一句简单规则给出立即可理解的解法。

keyMessage: 每个 AI 在独立副本里工作，就不会互相把项目改乱。

## Frame 4 — 完成后，检查再合回去

- scene: 用四步普通话流程说明安全合入，而不是展示术语清单。
- voiceover: "先分开干活，只交这次改的内容，检查没问题，再合回主项目。"
- duration: 7s
- poster: 5.8s
- transition_in: push-slide LEFT
- status: animated
- src: compositions/frames/04-the-loop.html
- type: feature_showcase
- persuasion: Show-don't-tell proof
- beat: confidence
- blueprint: grid-card-assemble (Adapt)
- asset_candidates:

focal: 左侧四步流程和中央“检查通过后再合回去”的确认面板。

roles: 网格为 background；四步流程为 supporting；检查面板与最终“不用一乱再重来”结果为 cutout。

Adapt: 保留流程逐项组装、路线最终闭合的动作；四步全部写成普通话，技术命令只作为辅助小字。

Scene 1 (0.0–1.4s): 标题“完成后，检查再合回去”出现，第一步“先分开干活”亮起。

Scene 2 (1.4–3.0s): 第二步“只交这次改的内容”进入，中央面板同步显示“这次改了什么”。

Scene 3 (3.0–4.8s): 第三步“检查有没有问题”进入，状态逐行显示“改动独立 / 内容清楚 / 检查通过”。

Scene 4 (4.8–6.0s): 第四步“确认后再合回主项目”进入，流程线闭合。

Scene 5 (6.0–7.0s): 结果卡出现“不用一乱再重来”，停读后进入安全证据。

narrativeRole: 证明产品不是把任务分开就结束，而是把确认无误的结果安全带回主项目。

keyMessage: 分开干活、检查清楚，再把确认无误的改动合回来。

## Frame 5 — 真有冲突，就先停下

- scene: 主项目和 AI 修改都碰到登录页面时，安全挡板落下；任务暂停并指出冲突位置，主项目保持原样。
- voiceover: "真有冲突怎么办？先停下来，告诉你哪里冲突，不会偷偷覆盖原项目。"
- duration: 7s
- poster: 5.8s
- transition_in: crossfade
- status: animated
- src: compositions/frames/05-conflict-guard.html
- type: feature_showcase
- persuasion: Risk reversal
- beat: trust + peace of mind
- blueprint: agent-progress-theater (Adapt)
- asset_candidates:

focal: “发现冲突，已暂停”状态挡板与“主项目保持原样”的确认卡。

roles: 深蓝网格和扫描线为 background；主项目、AI 修改双卡为 supporting；暂停挡板、冲突位置和安全结果为 cutout。

Adapt: 保留“工作状态 → 回执状态变化”的签名动作；把自动检查清单改成冲突挡板，状态从“正在检查”变为“已暂停”，再落下安全回执。

Scene 1 (0.0–1.3s): 标题“真有冲突怎么办？”进入上方；中央出现“正在检查两边的修改”，扫描线开始移动。

Scene 2 (1.3–3.3s): 左右两张卡依次出现“主项目也改了登录页面”和“AI B 也改了登录页面”；两条路线同时指向中间的“登录页面”。

Scene 3 (3.3–5.0s): 风险挡板从中间展开，状态切换为“发现冲突 / 已暂停”，扫描线停止；下面明确显示“不会自动覆盖”。

Scene 4 (5.0–7.0s): 三条回执依次变为“先停下”“告诉你冲突在哪”“修好再继续”；最后高亮“原项目保持原样”并静止停读。

narrativeRole: 回答观众对自动合入最直接的担心，补上可信的失败处理证据。

keyMessage: 遇到冲突会先停下，不会猜着合、更不会偷偷覆盖原项目。

## Frame 6 — 这就是它解决的问题

- scene: 把全片压缩成一句可复述的产品定义，再落到公开 Beta 行动。
- voiceover: "develop-with-worktrees：让多个 AI 同时写代码，也不会互相改乱。"
- duration: 5s
- poster: 4s
- transition_in: crossfade
- status: animated
- src: compositions/frames/05-public-beta.html
- type: cta
- persuasion: Risk reversal
- beat: motivation + urgency-to-act
- blueprint: cta-morph-press (Adapt)
- asset_candidates:

focal: “让多个 AI 同时写代码，也不会互相改乱”的结论与公开 Beta 按钮。

roles: 网格为 background；三张 AI 任务与检查卡为 supporting；产品面板、主张和 CTA 为 cutout。

Adapt: 保留产品面板、按钮和点击动作；第六幕只重复普通人需要记住的一句话，不再引入新术语。

Scene 1 (0.0–1.4s): 顶部出现“多个 AI，也不会改乱”，产品面板与三个独立 AI 任务建立。

Scene 2 (1.4–3.4s): 产品面板落一句解释“每个任务各自独立，完成后检查再合回去”，CTA 放大进入。

Scene 3 (3.4–5.0s): 光标点击“查看公开 Beta”，确认卡亮起“可以放心并行”，最后停在按钮与产品名。

narrativeRole: 把前五幕压缩为一句可复述的产品定义，并给出下一步。

keyMessage: 这是一个避免多个 AI 把同一项目改乱的工具。
