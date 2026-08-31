# Changelog

## 0.5.0-beta.1 — 2026-08-31

- 新初始化项目改为候选流水线默认：Finish 发布不可变候选并释放开发工作树，同一基线与策略代次每满 5 个候选时原子冻结最早 5 个，由第五个 Finish 直接触发可恢复的组合、Full 验证和主线推进；候选池默认容量 10。
- 不足 5 个的尾批只允许宿主当前协调任务在确认预定工作完成后显式列出精确候选收尾；单任务按一候选尾批处理。空闲、任务数、Hook 和 SessionEnd 均不能推断尾批，普通用户无需手工复制候选 ID。
- 任务在 Start 冻结集成策略；缺少 integration 表的旧项目保持 direct，旧 batched 配置保持 explicit，旧候选使用独立策略代次而不会被自动封批。状态升级到 schema 6，候选池升级到 schema 2，并保留旧 schema 的非破坏读取。
- 失败批次的候选改为 retained，不会因下一次发布而盲目重组；可由修复候选替代或在新显式代次中精确复用。批次长时间验证不再占用工作树维护锁，下一批候选仍可发布。
- 中英文 README 改为面向普通使用者说明“解决什么问题、带来什么帮助、默认怎么工作”，内部状态与兼容细节下沉到 references；DWW 项目级规则固定 README 主流程不超过五步。Hook 定义继续保持不变。
- 候选批次现在记录组合冲突、最终验证失败和推进阻断的精确失败类型；只有引发组合冲突的候选可自动进入受管返修，验证或安全取舍不再被当作可盲重试的合并问题。
- 新增 `candidate repair --candidate <id>`：在最新基线上幂等领取修复工作树、先补齐任务锚点，再以不可变候选 ref 准备可审阅的 merge 现场；精确路径 Commit 可安全完成该受管 merge。
- 自动返修链最多两代。代码、契约和测试能唯一决定结果时由代理继续 Commit/Ready/Finish 并显式封批；产品、权限、迁移、删除、安全或合法测试预期互斥时才升级人工。Hook 定义保持不变。
- 普通任务在基线推进后发生真实冲突时，可以在任务工作树中显式合并当前记录基线；精确路径 Commit 只接受该基线头或已登记候选返修的 merge 身份。

## 0.4.0-beta.1 — 2026-08-31

- 将 DWW 收敛为通用 Git 安全开发底座：新的复杂任务使用宿主原生任务/子智能体编排，DWW 旧 `orchestrate` 只保留既有批次的兼容收尾入口，不再创建新批次或追加任务。
- Managed `Start` 自动在 Git common-dir 建立不提交的任务锚点，并支持 `request_id` 幂等复用；Ready 校验锚点身份，直接合入、显式放弃、候选撤回或候选批次成功后再删除。
- 新增可选 `integration.mode = "batched"`：默认每批最多 5 个候选、候选池容量 10；Finish 发布不可变候选且立即释放工作树，只有显式列出候选的 `batch seal` 才冻结代次、组合验证并保护性推进基线。失败代次不移动基线且不自动重跑。
- Hook 降为可选强化层；核心路由、任务锚点、工作树、验证、候选池、显式封批和恢复均可无 Hook 运行，Hook 定义保持不变。

## 0.3.0-beta.18 — 2026-08-29

- 将 `route` 的职责收窄为生命周期与编排状态所有者选择；复杂任务的一次通俗计划确认、当前会话单一协调、临时任务锚点和长期文档边界属于非状态治理，在 `defer` 下仍适用，除非仓库明确覆盖同一规则。
- `defer` 明确禁止 DWW 初始化、生命周期命令和 orchestration state。已有外部编排时完全使用仓库编排器；只有原生生命周期时，当前会话通过原生任务身份、Status 与证据协调，不创建或修改 DWW batch。
- orchestration 全部写入口在落锁和持久化前复核当前 route/adapter；仓库从 managed/delegated 转为 `defer` 后，既有 batch 也冻结为只读。增加新建和既有 batch 的 common-dir 字节零漂移行为回归；Hook 定义保持不变。

## 0.3.0-beta.17 — 2026-08-28

- POSIX caller 在资源准备前预建唯一 process owner，launch 改为原地填充并与 GO、返回边界和监控共用同一个 `BaseException` guard；四条控制 channel 以自拥有 socket endpoint 对象整体落盘，不再把裸 FD tuple 跨越 `CALL → STORE_ATTR` 移交，端点关闭中断也可按同一对象安全重试。resource prepare、Popen、child-end close、status、GO 或 launch `RETURN_VALUE → STORE_FAST` 任一边界中断都会用同一 owner 幂等收束。
- control 命令交付改为 `not_attempted / indeterminate / delivered` 三态：只有原生 write 明确返回一个字节才是 delivered，不确定写保留唯一 writer 并由 stop 内部及 caller 外层安全重试。supervisor 每次只读一个字节，任一 `K` 都进入不可逆的自身进程组 `SIGKILL` 重试循环；重复 K 不再组成伪造帧，父方仍不向裸 PID/PGID 发送 destructive signal。
- status 无效、伪造或无法证明 direct-child 身份时仍优先消费私有 control capability 终止真实 supervisor，但不得据此确认进程组为空或删除 verified-input closure。adapter child 在 exec 前必须确认全部控制 FD 已关闭，任一关闭结果不确定都以 126 失败且绝不执行批准入口；固定 launcher `cwd=/`，即使绝对解释器位于仓库内也不会把仓库作为 GO 前启动目录。补充 pre/post-write 中断、连续不确定写、channel 落盘/关闭中断、launch/cleanup 返回边界、伪造 identity、self-kill 重试与无残留 launcher 回归；Windows Job 与 Hook 定义保持不变。

## 0.3.0-beta.16 — 2026-08-28

- POSIX gate launcher 改为不会自然退出的持久 supervisor：它在初始 status 前忽略 `SIGTERM`，GO 后才 fork/exec adapter，亲自 wait adapter 并通过单 writer、有界单帧 result pipe 返回退出码；adapter 在 exec 前恢复默认 `SIGTERM` 并关闭全部 status/gate/payload/result/control FD。adapter 完成、exec 失败、gate EOF 和协议错误都不会释放 supervisor 的进程组 leader 身份。
- 父方不再对可复用的裸 PID/PGID 发 destructive signal，也不在结果路径 `poll/reap` supervisor。确认 supervisor 仍是未 reap 直接子后，父方只向私有 control pipe 交付一次终止命令；仍活且实际拥有该组的 supervisor 自行 `SIGKILL` 当前组，父方随后只 wait/reap 和只读确认组消失。外部 SIGCHLD reaper、PID/PGID 复用或控制结果不确定时零裸 PGID 信号并保留 verified-input closure。
- POSIX 预 GO launcher 环境收窄为固定 locale，不再继承 `PATH`、`LIBPATH`、`SHLIB_PATH`、`LDR_*`、`GCONV_PATH` 或其他平台 loader/runtime 环境；完整 adapter 环境仍只在 GO 后从私有 payload 应用。Windows Job/process/thread/标准流/属性列表关闭路径先绑定原生 API，再单次 detach，并将 detach 后的异步中断明确报告为终止不确定；空 Job 的 `CreateJobObjectW` 返回到 Python 所有者落盘之间仍是无子进程、不可完全消除的极短资源泄漏边界。Hook 定义保持不变。

## 0.3.0-beta.15 — 2026-08-28

- Windows 委托启动不再经过“`Popen` 返回后再 assign Job”的窗口：调用方用 `STARTUPINFOEX` 同时传入精确标准流 HANDLE 列表与 `PROC_THREAD_ATTRIBUTE_JOB_LIST`，`CreateProcessW(CREATE_SUSPENDED)` 创建成功的第一刻即由预建 `KILL_ON_JOB_CLOSE` Job 原子拥有；预建轻量 process wrapper 直接持有 `PROCESS_INFORMATION` 的 process/thread HANDLE，系统创建成功但 Python 尚未返回时的任意 `BaseException` 也能收束且不会执行入口。
- POSIX 委托增加隔离的 gate/status launcher：父方用 CLOEXEC 管道钉住未 reap 的直接子和进程组身份，严格核对 `pid == pgid == process.pid` 后才发送唯一 GO；launcher 在 GO 前以 `-I -S`、非仓库 cwd 和剥离 Python/动态加载器变量的环境运行，原适配器 argv、cwd 与环境只在 GO 后从私有 FD 读取并 `execvpe`。`_fork_exec` 成功但 `Popen` 尚未返回、GO 写入边界中断、无效帧或超时都不会提前执行适配器，并按 TERM→KILL、`waitpid` 与组消失确认失败关闭。
- Job、process/thread、标准流副本和属性列表采用单次消费：原生关闭前先从唯一所有者移除数值，关闭成功后的异步中断不会再次查询或关闭已复用 HANDLE；关闭、查询、终止或身份确认不确定时保留 verified-input closure。补充真实 post-create/pre-return、迟到 marker、`sitecustomize`、GO 前后中断、PID/PGID 不匹配和 HANDLE 复用回归，Hook 定义保持不变。

## 0.3.0-beta.14 — 2026-08-28

- Windows 委托适配器改为预建 `KILL_ON_JOB_CLOSE` Job Object，并以 `CREATE_SUSPENDED` 启动根进程；调用方先用 Popen 原生 process HANDLE 直接加入 Job，再恢复执行，不再通过可复用 PID 或事后 PPID 树建立所有权，也不使用 `CREATE_BREAKAWAY_FROM_JOB`。
- Job handle 贯穿监控、等待和有界输出读取；success、nonzero、timeout、输出超限、读取异常及任意 `BaseException` 接受结果或清理输入闭包前，都必须确认 `ActiveProcesses == 0`，必要时终止整个 Job。配置、assign、resume、terminate、query 或关闭无法安全完成时失败关闭；仍可能使用批准输入时保留闭包。
- 补充短命 launcher 留下断链孙进程、正常 0/非零退出留下继承 stdio 后代、运行中动态派生、首次 PID 身份前复用、Job 配置/assign/resume/terminate/query 故障及输出读取异常回归。POSIX 的正常 0/非零退出也会检查并清空完整进程组；主动 `setsid` 或绕开继承边界仍明确不属于 OS sandbox。Hook 定义保持不变。

## 0.3.0-beta.13 — 2026-08-28

- Windows 异常委托终止先捕获根进程创建身份，冻结根与已发现后代，并重复递归枚举到身份集合稳定后再终止/强杀；首次快照后的非恶意动态派生不再逃出清理边界。
- 每个已拥有进程以 PID + 创建时间确认，PID 复用时拒绝触碰无关进程；任何暂停、枚举、终止、等待或身份确认不完整都会失败关闭并保留已验证输入闭包。
- 补充首次快照后派生孙进程、暂停失败保留现场和 PID 复用回归；POSIX 继续以新会话进程组覆盖动态后代，并明确 DWW 不是阻止受批准适配器主动逃逸的 OS sandbox。Hook 定义保持不变。

## 0.3.0-beta.12 — 2026-08-28

- 委托适配器启动后，轮询、等待和有界输出读取的任何 `BaseException` 都会先终止并确认调用方持有的完整进程组/树，再原样传播；不再只处理超时和输出溢出。
- POSIX 会在根进程响应 `SIGTERM` 后继续核验同组后代，必要时对整个进程组发送 `SIGKILL`；Windows 显式枚举、终止并确认已捕获的递归子树与根进程。
- 终止无法确认时失败关闭并保留已验证输入闭包及其诊断路径，不会在仍可能被子进程使用时删除；补充中断、异常读取、父子孙进程、拒绝终止后代和安全清理交互回归，Hook 定义保持不变。

## 0.3.0-beta.11 — 2026-08-28

- 委托调用将最终指纹核验实际读取的原始契约与全部 `tracked_inputs` 冻结为仓库外私有执行闭包；Python helper/PEP 723、PowerShell 控制器/模块/配置和 sh helper 不再回读活工作区。
- 适配器通过 `DWW_VERIFIED_INPUT_ROOT` 读取批准输入，通过 `DWW_REPOSITORY_ROOT` 操作原始仓库；cwd 仍为原仓库，执行闭包保持相对结构、禁止 Python 字节码污染，并在成功、失败和超时后安全清理。
- 清理逐项核验文件、目录、内容集合与对象身份；路径替换或新增内容时失败关闭并保留现场。补充最终核验后多输入漂移、跨运行时目录语义、契约原始字节、异常与超时回归；Hook 定义保持不变。

## 0.3.0-beta.10 — 2026-08-28

- 委托调用在最终指纹核验时冻结同一份入口字节，并只执行生命周期内清理的已验证快照；核验后、进程启动前修改活入口不再能借旧指纹执行，同时保留原仓库根和脚本同目录语义。

## 0.3.0-beta.9 — 2026-08-28

- 将委托 schema 1 收窄为经过验证的 `status` 与幂等 `start`，固定精确请求和结果字段；项目的 Ready、Finish、封板、恢复与清理继续由原生流程负责。
- 委托适配器的请求、stdout、stderr、UTF-8、严格 JSON 和错误摘要均有界；超时或输出溢出会终止调用持有的整棵进程树，项目返回的 `ok = false` 不再被外层包装成成功。
- 编排容量请求统一为 `status {}`，并在执行前要求契约显式声明 `status`；容量结果只接受 `0..max_parallel` 的整数，拒绝布尔值和额外原生字段。
- 通用迁移文档移除项目专属操作表，明确未知副作用时只能使用同一 request id 重试 `start`，其余项目规则留在各仓库的权威文档。
- 补充适配器漂移、严格结果、失败传播、输出上限、超时进程树和缺失能力回归测试；Hook 定义保持不变。

## 0.3.0-beta.8 — 2026-08-27

- 成熟仓库可提交版本化委托适配契约；只有精确契约与受管输入指纹在本机获批后才进入 `delegated`，漂移、歧义或无批准时继续失败关闭为 `defer`。
- 委托调用使用 DWW 构造的固定运行参数和单次 JSON 协议，编排容量来自适配器只读 `status`；仓库继续拥有任务、验证、候选、封板、恢复和清理实现。
- 委托批准支持按适配器 ID 与精确指纹本机撤销；迁移采用原生基线、未批准声明、双轨对比、显式批准、规则缩减和逐项提取状态机，回退不删除任何项目状态。
- 已确认方案和多步骤任务使用不提交的临时任务锚点恢复上下文；只把跨任务仍有效的规则或契约固化到唯一权威文档，并移除根目录按任务累积的需求/方案台账。
- 明确受委托仓库可以自有候选池和显式封板协议，DWW 只提供边界和迁移准则，不猜测或复制项目控制器。
- 完整 Ready 套件保持全部测试不变，将本仓库验证上限从 45 分钟调整为 60 分钟，以覆盖 Windows 上已观察到的完整生命周期测试耗时。
- Hook 定义保持不变，升级不会扩大已信任的命令入口。

## 0.3.0-beta.7 — 2026-08-21

- 修复插件清单使用旧式技能路径和超长单条默认提示，改为 Codex 当前要求的 `./skills/` 与最多三条短提示。
- 增加插件清单契约回归测试，发布前检查组件路径存在、默认提示类型、数量与长度；不改变 Hook 定义和既有信任摘要。

## 0.3.0-beta.6 — 2026-08-20

- 修复 Windows Hook 在 PowerShell 宿主中仍使用 cmd `%PLUGIN_ROOT%`，导致 SessionStart、PreToolUse 和 PostToolUse 无法定位守卫脚本的问题。
- 用真实 PowerShell 启动方式回归 Hook 定义；该修复有意改变一次信任定义，升级后只需重新审查一次。

## 0.3.0-beta.5 — 2026-08-13

- Finish 在改变基线分支前持久化精确候选事务；Recover 依据 Git 祖先事实幂等完成提升、detached、原子删引用和槽位释放。
- Abandon 与 Finish 共用集成串行边界，并使用可恢复事务、工作树身份核验和预期 SHA 原子删引用。
- 统一清理分类，大小写无关保护 `.env*`、数据库与上传/存储目录，拒绝未知 ignored 内容和链接/junction。
- PruneSlot 计划绑定槽位世代、一次性执行，在首次移动前持久化事务，并可从真实进程退出后的暂存或逐项删除阶段继续。
- Ready/Finish 的证明绑定精确候选 SHA，在敏感扫描、验证和进程停止前后拒绝候选漂移。
- Recover 发布同任务互斥操作，严格迁移旧回执，并可补齐已释放直改任务的最终回执；辅助操作日志失败不再制造假失败或遗留活动标记。
- Abandon 与 PruneSlot 绑定 Windows 文件对象身份并使用条件删除；tracked 改动、同路径替换、跨任务 ref 竞态和释放瞬间的晚到内容均保留现场或隔离槽位。

## 0.3.0-beta.4 — 2026-08-11

- Makes one Ready call converge when another task advances the recorded base during validation, instead of recording a stale Ready proof that Finish must validate again.
- Rechecks the expected base after machine validation admission and before every profile command; a post-validation check closes changes that occur during the final command. A failed command is discarded only when its base changed during execution, while a failure on the current base remains a hard failure.
- Reuses exact unchanged profile proofs across convergence attempts and bounds automatic retries at five while preserving the candidate on continued movement.

## 0.3.0-beta.3 — 2026-07-30

- Treats `hooks/hooks.json` as a stable trust-compatibility contract so ordinary plugin, skill, and guard-script updates require no repeated user action.
- Changes the AI flow to ask once only when Codex actually reports a first-install or changed-definition review, then use available host UI control after approval.
- Explicitly forbids editing Codex trust storage, using the broad hook-trust bypass flag, or misusing enterprise managed hooks.

## 0.3.0-beta.2 — 2026-07-30

- Clarifies that `remote_policy = "local-only"` constrains DWW and orchestration, rather than blocking an explicit user-requested publish after Finish.
- Adds one bounded post-Finish publish contract: clean base worktree, current branch, push dry-run first, ordinary non-force push only.
- Keeps fetch, pull, remote deletion, tags, PR creation, deployment, and history rewriting outside that permission unless separately requested.

## 0.3.0-beta.1 — 2026-07-30

- Adds a local multi-AI command-center layer with one-confirmation batches, dependency frontiers, a five-worker development ceiling, pause/resume/cancel/handoff, and compact completion receipts.
- Keeps orchestration state separate from DWW lifecycle state; records only minimal task, decision, and proof references, never chat transcripts, raw reasoning, leases, or secrets.
- Keeps same-file work optimistic by default, serializes only explicit high-risk resources, and escalates repeated unchanged failures rather than blindly retrying.
- Adds explicit `dww` and `delegated` lifecycle adapter boundaries; orchestration remains local-only and never pushes, opens PRs, deploys, or guesses semantic merges.

## 0.2.0-beta.5 — 2026-07-29

- Gives detected mature repository workflows absolute routing priority over DWW's long-term and current-task direct-development choices.
- Adds compact read-only `dww route --json` output and a dependency-free shared routing decision used by both CLI and Codex Hook.
- Makes `SessionStart` inject mature-workflow deferral once while later Pre/Post Tool hooks silently step aside, avoiding repeated context cost.
- Makes every `choose` mode return `deferred` without changing DWW state when a mature workflow exists.

## 0.2.0-beta.4 — 2026-07-24

- Replaces the multi-step repository adoption conversation with one plain-language three-choice prompt: isolate normal tasks, use the current directory for this task only, or remember current-directory development locally for this repository.
- Makes the one-task current-directory choice behave as if the plugin were absent: no policy files, DWW task, lifecycle gates, or guard alerts. The local authorization is bound to the exact worktree and hashed Codex session.
- Adds explicit one-time child-session delegation, rather than unsafe repository-wide or time-window bypasses, because current Codex hook payloads do not provide a reliable parent-agent identifier.
- Reuses existing disable safety checks for the long-term local choice and accepts internal static checks without a second user prompt when no test command is discovered.

## 0.2.0-beta.3 — 2026-07-24

- Fixes validation timeouts on macOS: a validation process launched by the current `run_logged` call is now terminated through its owned `Popen` process group, rather than being blocked by cross-call process-snapshot matching. Cross-call PID-reuse protection remains unchanged.

## 0.2.0-beta.2 — 2026-07-24

- Adds one explicit, session-bound `in-place` task mode for current-worktree-only work; it keeps an immutable validation start commit and never merges, switches, resets, cleans, or deletes that worktree on Finish or failure.
- Blocks same-base isolated Finish while an in-place task is active, so a parallel merge cannot silently invalidate its checked-out branch or HEAD.
- Upgrades local task state to schema 3 with read compatibility for existing schema 2 isolated tasks.
- Replaces the advisory Codex hook with supported `PreToolUse permissionDecision: deny` responses for protected base-worktree writes, plus preserved dirty-state alerts for escaped specialised paths.
- Requires explicit hook trust after install or hook changes and documents the boundary between Codex hard guardrails and operating-system enforcement.

## 0.2.0-beta.1 — 2026-07-23

- Clean breaking release: verification policy is schema 3 only.
- Starts and integrates against the recorded current local base branch.
- Adds machine-global weighted FIFO validation capacity, local capacity settings, duration estimates, and slow-validation advisory.
- Makes cache cleanup opt-in and plan-bound; links, junctions, `.env*`, and changed targets stop the whole prune.
- Adds CLI version contract, candidate-policy approval, and release consistency tests.
