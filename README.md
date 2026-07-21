# Develop with Worktrees

`develop-with-worktrees` is a local-first Codex plugin for safe, fast solo AI development with a small reusable Git worktree pool.

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

## Requirements

- Git
- [uv](https://docs.astral.sh/uv/)
- Codex with plugin and hook support

PowerShell 7 and a separately installed Python are not required. The runner and hook use PEP 723 scripts via `uv`.

## Development

```text
uv sync --dev
uv run pytest
uvx ruff check .
uvx ruff format --check .
uv run --with pyyaml <path-to-plugin-creator>/scripts/validate_plugin.py .
uv run --with pyyaml <path-to-skill-creator>/scripts/quick_validate.py skills/develop-with-worktrees
```

The test suite creates temporary repositories. It does not modify the current business project.
On Windows, the full lifecycle suite can take several minutes because it repeatedly creates,
releases, and restores temporary Git worktrees. Give its outer timeout at least ten minutes and,
after a timeout, check for the original `pytest` child before starting another run.

See the Chinese README for a fuller first-use and removal guide, and [`docs/architecture.md`](docs/architecture.md) for invariants and state layout.

## License

MIT
