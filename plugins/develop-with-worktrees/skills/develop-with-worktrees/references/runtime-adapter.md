# Runtime Adapter reference

A Runtime Adapter is optional project code that prepares and releases runtime
resources around a DWW task or combined Full. It is different from a
[delegated adapter](delegated-adapters.md): a delegated adapter exposes an
existing project lifecycle, while a Runtime Adapter supplies project-owned
runtime effects to DWW's managed lifecycle.

## Configuration

Declare command arrays and their tracked input closure in `.solo-ai/config.toml`:

```toml
[runtime_adapter]
activate = ["uv", "run", "scripts/dww-runtime-adapter.py", "activate"]
release = ["uv", "run", "scripts/dww-runtime-adapter.py", "release"]
batch_activate = ["uv", "run", "scripts/dww-runtime-adapter.py", "batch-activate"]
batch_release = ["uv", "run", "scripts/dww-runtime-adapter.py", "batch-release"]
verify_effective = ["uv", "run", "scripts/dww-runtime-adapter.py", "verify-effective"]
input_paths = ["scripts/dww-runtime-adapter.py", "deploy/**"]
environment = ["NODE_ENV", "TARGET_ARCH"]
required_outputs = ["dist"]
timeout_seconds = 300
```

DWW appends one absolute JSON context path to each command. It approves the
normalized command and the bytes selected by `input_paths`; missing matches,
approval drift, timeout, nonzero exit, or task-tree contamination fail closed.
The Adapter may create project-owned ignored runtime data, but must not mutate
the tracked task tree or DWW state.

For native fixed-slot tasks, include the project's lock files and preparation
script in `input_paths`. The context contains their hashes, the selected
environment digest, the slot generation, the exact source HEAD, and
`rebuildable_outputs` from `cleanup.owned_paths`. The project Adapter decides
whether to reuse dependency caches or rebuild those declared outputs. DWW does
not delete caches, configuration, databases, uploads, or unknown files.
Projects that read source directly can leave `activate` unset.
`required_outputs` names optional top-level paths that must exist for a
successful preparation receipt to be reused. Missing paths rerun the Adapter;
the Adapter itself decides what to rebuild and must produce them before success.

## Task operations

| Operation | When it runs | Key facts | Required outcome |
|---|---|---|---|
| `activate` | legacy isolated Start; for native tasks, first `runtime prepare --task <id> --lease <lease>` or `dev start` | task/slot IDs and generation, source HEAD, worktree, base, task port block, input and environment digests | successful receipt and a second clean identity check |
| `release` | after immutable candidate ref exists; before candidate activation and slot release | task, candidate identity, worktree, base, input hashes | resources released without contaminating the task tree |
| `batch_activate` | before combined Full | batch/candidate IDs, frozen base, integration head, batch port block, runtime cycle | resources ready for this exact cycle |
| `batch_release` | after Full attempt and before final batch transition | same cycle plus validation outcome/error | resources released for that exact outcome |
| `verify_effective` | only after source is delivered into current base | delivered candidate and project runtime facts | fresh project-defined effectiveness evidence |

Native Start performs only Git and identity preparation. It records whether
activation is configured without running the Adapter. `runtime prepare` is an
explicit first-use entry point; `dev start` calls it automatically. Repeating
prepare with unchanged command, tracked inputs, environment digest, source HEAD,
and task generation reuses its successful receipt. A new generation or changed
input runs it again. The Adapter must check any project outputs it needs before
reporting success and may replace only declared rebuildable outputs. When no
native prepare ran, Finish has no task runtime to release.

At legacy Start, `candidate_head` is deliberately absent: no candidate exists
yet. `base_head` is the frozen task baseline. A real candidate first enters the
context at release after Ready or Finish fixes it. Do not infer candidate
identity from a task's internal bookkeeping field.

## Port blocks and workspace identity

An isolated task receives an inclusive 100-port block beginning at
`port_base + (slot - 1) * 100`. Batch work receives a separate block beginning
at `port_base + 3200`, disjoint from up to 32 task slots. DWW provides these
facts but does not decide how a project uses ports, databases, browsers,
authentication, or services.

Reusable batch contexts also include `worktree_binding`: mode, owner batch,
generation, resolved path, and directory identities. `runtime_cycle` identifies
one resource activation within that generation. The two values are distinct: a
later workspace owner has a new generation; a validation retry after successful
release starts a new runtime cycle in the same generation.

## Receipts, retries, and delivery

Successful content-addressed receipts are reusable only for the same operation,
approved command/input closure, context, and relevant cycle. An interrupted
command retries that recorded cycle. If resources were successfully released
before a later validation retry, DWW increments the cycle and activates them
again; it never treats an old activation receipt as live resources.

Uncertain activation or release preserves the task or batch and leaves the base
unchanged. `batch recover` resumes a nonfailed batch; see [recovery](recovery.md)
for the failure decision tree. Release success is required before promotion.

Candidate publication and Git delivery do not prove a running service uses the
new source. `runtime verify --candidate <id>` is an explicit fresh check after
delivery. DWW records its result without interpreting the project's runtime
semantics.
