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
