# Develop with Worktrees

[![CI](https://github.com/zhaogelz/develop-with-worktrees/actions/workflows/ci.yml/badge.svg)](https://github.com/zhaogelz/develop-with-worktrees/actions/workflows/ci.yml)

**Safe parallel Codex work, for one developer.**

`develop-with-worktrees` turns several independent Codex tasks into a controlled delivery loop: isolated worktrees, explicit validation, conflict prediction, FIFO local integration, and clean removal. It is not another generic `git worktree` command wrapper.

```text
one developer + several Codex tasks
          │
          ▼
isolated reusable slots (code / dependencies / ports)
          │
          ▼
exact-path commit → Ready proof → FIFO local Finish
          │
          ▼
main advances safely; failures keep their task scene intact
```

## Install the public beta

Requirements: Git, [uv](https://docs.astral.sh/uv/), and Codex with plugin and hook support.

```text
codex plugin marketplace add zhaogelz/develop-with-worktrees --ref v0.1.0-beta.2
codex plugin add develop-with-worktrees@develop-with-worktrees
```

The first modifying task in a new repository shows an adoption plan; it does not change tracked files until you accept. Start a new Codex task after installation so Codex can load the plugin.

To remove it, first run the repository `deinit` flow described in the Chinese guide, then run:

```text
codex plugin remove develop-with-worktrees@develop-with-worktrees
```

## Why this exists

- **More useful solo parallelism.** Run 1–5 independent AI tasks without sharing a branch, dependency environment, port range, or local task state.
- **Fast without a merge gamble.** A candidate is explicitly validated and checked against the latest local default branch before serialized `ff-only` integration.
- **Does not take over established repositories.** Worktrunk, `scripts/worktree-flow.ps1`, and other mature workflow markers win automatically; the plugin makes zero policy or slot writes.
- **Failures stay recoverable.** Unknown files, protected data, active processes, conflicts, and failed validation preserve the task scene instead of being automatically deleted or retried.

This is intentionally a narrow Codex workflow layer. For broad interactive worktree management, branch switching, PR flows, or shell-centric automation, use a general worktree tool such as Worktrunk. A repository should use one lifecycle owner; this plugin detects an existing mature owner and defers.

It combines three layers:

1. A lightweight Codex hook that detects likely local writes and injects a policy reminder.
2. A skill that chooses read-only, defer, first-adoption, or managed lifecycle behavior.
3. A `uv run --script` CLI that owns slots, leases, exact path staging, validation proofs, process cleanup, FIFO integration, and deinitialization.

The hook is intentionally a guardrail, not an OS-level lock: Codex's current PreToolUse protocol can inject context but has no portable hard-deny result. The CLI is the hard gate for this plugin lifecycle.

## Safety model

- Existing Worktrunk, `scripts/worktree-flow.ps1`, and other mature workflow markers always win. The plugin defers with zero policy/slot writes.
- New repositories get one plan before any tracked adoption. Accept creates only two `.solo-ai` policy files plus an exact `AGENTS.md` block; decline is machine-local.
- Dirty primary worktrees are never stashed or modified. Their policy bootstrap remains local until the first clean Finish.
- Tasks use fixed lazy slots (default 3, configurable 1–5), isolated branches, exact reviewed `--path` commits, and no remote Git operations.
- Ready predicts merge conflicts before changing a task. Finish serializes only local `ff-only` integration and preserves failures.
- Default proof reuse is Ready-to-Finish exact candidate reuse. Cross-task reuse requires an explicit closed, external-state-free profile declaration.
- Logs are redacted and content-addressed; raw environment values and raw process command lines are not persisted.
- Deinit is required before plugin uninstall and removes only exact owned policy/state/worktrees.

PowerShell 7 and a separately installed Python are not required. The runner and hook use PEP 723 scripts via `uv`.

## Development

```text
uv sync --dev
uv run pytest
uvx ruff check .
uvx ruff format --check .
uv run --with pyyaml <path-to-plugin-creator>/scripts/validate_plugin.py plugins/develop-with-worktrees
uv run --with pyyaml <path-to-skill-creator>/scripts/quick_validate.py plugins/develop-with-worktrees/skills/develop-with-worktrees
```

The test suite creates temporary repositories. It does not modify the current business project.
On Windows, the full lifecycle suite can take several minutes because it repeatedly creates,
releases, and restores temporary Git worktrees. Give its outer timeout at least ten minutes and,
after a timeout, check for the original `pytest` child before starting another run.

See the [Chinese README](README.zh-CN.md) for a fuller first-use and removal guide, and [`docs/architecture.md`](docs/architecture.md) for invariants and state layout.

## Beta feedback

Please open an issue with your OS, Git and `uv` versions, repository shape, command, expected result, and redacted error output. Do not include credentials, private repository URLs, private paths, or unredacted logs. Security reports follow [SECURITY.md](SECURITY.md).

## License

MIT
