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

Each evidence record binds the profile, declared inputs, tool/platform facts,
declared environment hashes, applicable frozen base, complete command receipts,
and logs. An unchanged Git head alone does not prove the working files or tools
are unchanged. Evidence never crosses repositories.

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
该阶段的命令执行历史估时；队列等待始终标为未知，除非已有当前尝试的队列事实。每个
profile 会显示实际匹配的改动文件，以及复用、执行或阻止的直接原因。预览只解释当前
事实，正式执行仍会重新核验输入。

`dww status --task <id>` 和 `--batch <id>` 只读取对象关联的最新尝试回执，不会清理
队列、重放恢复或扫描全部证明历史。状态中的 `waiting`、`running`、`reused`、
`failed`、`timed_out` 与 `interrupted` 都来自这个回执，不能从缺失日志推断为仍在运行。

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
