# Verification reuse and project onboarding

## Ownership and granularity

DWW owns command execution, durable profile proofs, input matching, and recovery.
The project owns check selection, commands, and runtime resources through its
Adapter. Use the existing verification profiles, not a second cache controller,
task DAG, candidate group, or artifact store.

A profile saves its proof before the next profile starts. A multi-command profile
saves success only after all its commands pass. Expose independently recoverable
checks as separate profiles where worthwhile; do not split every test case or
break a project's strongly coupled execution pipeline just to increase caching.
Keep useful project-internal parallelism inside the relevant command.

Declared inputs are checked before and after commands and before aggregate
success. An unchanged HEAD alone does not prove unchanged working files. Logs
must exist, match their digests, and contain successful execution receipts.
Evidence stays in each repository's Git common directory; identical fingerprints
in two repositories do not make their proofs interchangeable.

## Reuse is a conservative declaration

A batch or explicit verification may reuse a check when `external_state = "none"`
and `input_closure = "complete"`, and its declared inputs, environment, tool
facts, frozen base (when used), and logs still match. Such a proof is reusable
between tasks and batches in the same repository by default; it never enables
cross-repository reuse. `cross_task_reuse` remains accepted for older policy
files but is no longer needed to opt into this safe case.

Complete inputs include all relevant source, tests, fixtures, invoked scripts,
configuration, lockfiles, tools, and declared environment variables. DWW cannot
infer purity or discover hidden dependencies. Ignored/untracked files, network
responses, clocks, or mutable services are not covered merely by writing
`input_paths = ["**"]`. If those affect the result and cannot be represented by
the current contract, leave the check non-pure or its closure incomplete.

Commands that must create an artifact or change runtime state cannot be replaced
by a previous success report. Mark these `external_state = "unknown"`, including
build output consumed by later checks, database preparation, authentication and
browser flows. Each fresh Full executes them again. Project build caches may
accelerate the command, but DWW does not materialize missing outputs. A pure
compiler check may reuse only when no later step needs its generated files.

The aggregate receipt keeps the whole execution plan for audit. A profile proof
does not include unrelated profiles, unrelated lockfiles, or formatting-only
policy bytes, so those changes do not repeat an unaffected check. Its own
declared inputs and tool facts remain exact. Approval is a separate normalized
execution-policy contract: comments and line-ending-only edits do not require a
new approval, while any semantic policy change still does.

## Recovery is not a fresh Full

- If the original validation process is still running, observe it; never start
  a duplicate because logs are quiet or the caller was interrupted.
- Retrying an incomplete Full gives non-pure or incomplete checks a fresh
  execution identity, including after runtime resources were released/recreated.
  Completed pure checks with matching inputs can still be reused.
- Once Full passed, recovery of resource release, promotion, or cleanup resumes
  the recorded batch transaction. It does not restart Full merely to finish
  cleanup. Existing base, composition, and receipt-integrity gates still apply.
- Ready/Finish exact task receipts remain compatible. Ready must not be used to
  produce runtime resources or build outputs that Full needs. Runtime
  effectiveness after Git delivery requires the project's fresh Adapter check.

The Full execution identity is proof metadata, not another task scope or
orchestration state. The reuse-contract revision invalidates old content proofs
that lacked input-stability checks; it does not discard completed batch receipts.
Do not hot-update an engine while its tasks or batches are active.

## Minimal project examples

The same schema supports Python, Node, and other command-line toolchains. These
examples assume genuinely pure checks with complete declared inputs; copy the
shape, not an unverified purity assertion.

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

A Node project can replace the command with `["npm", "run", "check"]`, declare
its environment, and audit its own scripts and dependency closure. A required
build uses a separate Full profile, for example:

```toml
[[profiles]]
id = "required-build"
level = "full"
paths = ["**"]
input_paths = ["**"]
input_closure = "declared"
external_state = "unknown"
resource_class = "heavy"
environment = []
commands = [["npm", "run", "build"]]
```

## Safe migration

Exercise real disposable repositories before adoption: interrupted validation,
runtime-cycle replacement, missing outputs, changed inputs/commands/environment,
corrupt logs, repository isolation, and already-passed Full cleanup recovery.
Compare command counts and duration as well as final pass/fail results.

Drain the project's active lifecycle before switching its validation entry point
or installed engine. Confirm equivalent checks and resource cleanup, then remove
duplicate proof/recovery implementations. Keep project check selection and
runtime adapters. Never run two lifecycle owners at once; rollback is a reviewed
return to the prior implementation, not a second controller left running.
