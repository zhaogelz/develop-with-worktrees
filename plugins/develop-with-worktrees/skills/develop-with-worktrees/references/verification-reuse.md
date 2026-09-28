# Verification reuse and project onboarding

DWW executes declared commands, saves profile evidence, checks declared inputs,
and recovers interrupted verification. The project selects checks and owns
runtime resources. Reuse the existing verification profiles; do not add a second
proof cache, task DAG, candidate group, or artifact controller.

## Make profiles independently useful

A profile saves evidence before the next profile starts. A multi-command profile
succeeds only after every command passes. Split independently recoverable checks
when that improves recovery, but do not split every test case or break a
project's coupled command pipeline merely to increase caching. Keep useful
project-internal parallelism inside the command.

Independent profiles collect ordinary assertion failures by default. The
default ordinary exit code is `1`; a profile may declare additional codes with
`ordinary_failure_exit_codes`, or use `continue_on_failure = false` to stop at
its first ordinary failure. Reserve different codes for runtime and environment
errors in project commands. A profile may set
`depends_on = ["preflight"]` for an earlier profile in the same verification
file. DWW includes that prerequisite when selecting the dependent profile and
marks the dependent `blocked` if the prerequisite fails. The dependent command
does not start. Commands inside one profile still stop on the first failure.
Input or identity drift, command execution error, unexpected exit code,
interruption, and timeout stop the attempt. Failure collection does not turn a
failed or blocked required profile into a passing Full proof.

Each evidence record binds the profile, declared inputs, tool/platform facts,
declared environment hashes, applicable frozen base, complete command receipts,
and logs. An unchanged Git head alone does not prove the working files or tools
are unchanged. Evidence never crosses repositories.
The attempt receipt lists passed, reused, failed, blocked, interrupted, and
timed-out profiles with the failed profile IDs. Any failed or blocked required
profile keeps the combined validation from passing.

For a normal batched task, explicit Ready and its plan use the task's frozen
source base, even if the target branch advances. Profiles that compare against a
base must use `DWW_VALIDATION_BASE_HEAD` and set `frozen_base = true`; using a
moving branch name inside the command is not a frozen input. Batch Full uses the
actual composed head and the execution base frozen when that batch gets its turn.
Source Ready proof cannot stand in for a changed combination or runtime effect.

## Reuse conditions

A profile can be reused across tasks and batches only when all of these hold:

| Requirement | Why |
|---|---|
| `external_state = "none"` | the command has no unrepresented mutable effect |
| `input_closure = "complete"` | every relevant source, test, script, configuration, lockfile, tool, and declared environment is covered |
| matching declared inputs, commands, tools, environment, frozen base when used, and log receipts | the same contract actually ran successfully |
| no required generated artifact for a later step | a report cannot materialize missing output |

`cross_task_reuse` remains accepted for older policies, but matching pure complete
profiles no longer need it as an extra opt-in. Ignored or untracked files,
network responses, clocks, and mutable services are not made pure by declaring
`input_paths = ["**"]`. If they affect the command and cannot be represented,
leave external state non-pure or closure incomplete.

Build output consumed later, database preparation, authentication, browser flows,
and other runtime effects use `external_state = "unknown"`. A fresh Full runs
them again. A build cache may speed execution, but DWW never substitutes an old
report for a missing required artifact.

## Approval and proof are different

Approval is a normalized execution-policy decision. Formatting-only comments and
line endings do not change it; a semantic command, scope, permission, runtime,
or configuration edit does. Profile proof identity is narrower: a formatting-only
policy edit or unrelated profile does not automatically invalidate a matching
profile proof. A profile changes when its own declared command, inputs,
environment, tool facts, base requirement, logs, or reuse conditions change.

This distinction avoids two false rules: neither “every policy byte edit reruns
everything” nor “all unrelated policy changes are always reusable” is correct.
Read [configuration](configuration.md) for field definitions and approval
admission.

## Recovery is not a fresh Full

- If the original validation process is alive, observe it; do not launch a
  duplicate because output is quiet or the caller was interrupted.
- Retrying incomplete Full gives non-pure or incomplete profiles a fresh
  execution identity. Completed matching pure profiles may still be reused.
- After Full passes, resource release, promotion, and cleanup recovery resumes
  the recorded transaction; it does not rerun Full just to complete later work.
- Ready must not create runtime resources or required output for Full. A fresh
  runtime effectiveness check is an explicit project Adapter operation after Git
  delivery.

## 看懂等待、重跑和成本

每次验证会留下一个精确的 `validation-attempts/<id>.json` 回执。它不是第二套
任务数据库或证明缓存：证明仍是唯一的复用依据，尝试回执只把本次计划、队列等待、
已执行命令和终态联系起来。失败、超时和中断的已执行命令同样进入批次指标；旧的
`executed_full_validation_seconds` 保持原有“已完成批次的总证明”口径，新的
`full_validation_attempt_seconds` 才是包含失败尝试的明确口径。

`dww plan --task <id>` 默认比较 development、Ready、Full 和 Stress，因而不会
把它们相加冒充“本次还要等多久”。使用 `--level` 选择一个实际阶段后，才会给出
该阶段尚需执行命令的历史估时；已经复用的 profile 不再计入这个时间。队列等待始终
标为未知，除非已有当前尝试的队列事实。每个 profile 会显示实际匹配的改动文件，以及
复用、执行或阻止的直接原因。若保留着同一 profile 最近一次可核验的成功证明，执行
原因还会列出改变的输入类别，例如源码、依赖锁文件、命令、环境、输入范围或冻结快照。
预览只解释当前事实，正式执行仍会重新核验输入。

`dww status --task <id>` 和 `--batch <id>` 只读取对象关联的最新尝试回执，不会清理
队列、重放恢复或扫描全部证明历史。回执写有 `waiting` 或 `running` 时，状态视图还会
只读取对应队列票据或命令进程快照来确认它仍活着；确认不了便显示 `unknown`，而不把旧
回执误报成仍在排队或执行。`reused`、`failed`、`timed_out` 与 `interrupted` 继续来自
该回执，不能从缺失日志推断为仍在运行。

面向人的进度说明同样只陈述这些已观察到的回执、退出码、队列票据或进程快照；没有
可核验输出时，不能声称正在构建、正在执行或运行健康。

## Minimal policy examples

Use these only when their stated purity is true.

```toml
[[profiles]]
id = "pure-unit"
level = "ready"
paths = ["**"]
input_paths = ["**"]
input_closure = "complete"
external_state = "none"
environment = ["PYTHONUTF8"]
commands = [["uv", "run", "pytest", "tests/unit"]]
```

A required build is a separate integration Full profile:

```toml
[[profiles]]
id = "required-build"
level = "full"
full_scope = "integration"
paths = ["**"]
input_paths = ["**"]
input_closure = "declared"
external_state = "unknown"
resource_class = "heavy"
environment = []
commands = [["npm", "run", "build"]]
```

## Safe adoption

Before adopting a lifecycle or verification change, use disposable repositories
to exercise interrupted validation, changed inputs/commands/environment, missing
outputs, corrupt logs, runtime-cycle replacement, repository isolation, and
post-Full cleanup recovery. Compare command counts and duration as well as final
pass/fail results.

Drain active lifecycle work before changing the validation entry point or engine.
Keep project check selection and Runtime Adapters; remove duplicate proof or
recovery implementations only after equivalent behavior is demonstrated. A
rollback returns to the prior reviewed implementation rather than leaving two
lifecycle owners active.
