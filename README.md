# Develop with Worktrees

A local-first Codex skill for fast, safe solo AI development with a small fixed Git worktree pool.

Once initialized, every file-changing coding task gets an exclusive short-lived branch and reusable worktree slot. The skill commits the task, synchronizes it with the latest local default branch, runs repository-approved validation, then integrates it through a FIFO gate. Read-only work never occupies a slot.

## Why it exists

Git worktrees make parallel AI tasks practical, but the raw command is only the beginning. A reliable solo workflow also needs ownership, collision-free integration, retained dependency caches, validation evidence, safe failure recovery, and conservative cleanup. This project packages those decisions as one reusable Codex skill.

Key defaults:

- Three lazy reusable slots, configurable from one to five.
- Local-only operation: no fetch, pull, push, PR creation, or telemetry.
- Start from the committed local default branch even when the primary worktree is dirty; Finish waits for a clean primary worktree.
- One branch per task; no squash, rebase, amend, or history rewriting.
- Ready validation plus FIFO Finish integration.
- Exact candidate-proof reuse plus conservative cross-task profile reuse tied to code, policy, commands, dependencies, tools, platform, and persistent logs.
- Sensitive-content gates and redacted local logs.
- Failures preserve the task worktree for recovery.

## Requirements

- Git
- [uv](https://docs.astral.sh/uv/)
- Codex with skill support

PowerShell 7 and a preinstalled Python are not required. The runner is a PEP 723 script executed by uv.

## Install

Copy or install only `skills/develop-with-worktrees` into your Codex skills directory, or install it from this repository with the Codex skill installer. Restart/reload skill discovery as required by your Codex surface.

The repository itself is also the distributable source; no package installation is needed.

## First use in a repository

The skill inspects the repository once. If it finds validation commands, it presents them for approval before creating a local bootstrap commit. That commit contains only `.solo-ai/config.toml`, `.solo-ai/verification.toml`, and a managed block in `AGENTS.md`.

If no meaningful validation command is found, static-only mode requires explicit acceptance and is always reported as “static checks only,” never “tests passed.” If a mature worktree/orchestration system already exists, compatible mode defers to it.

After initialization, ask Codex for normal coding work. The skill is implicitly eligible for any task that may modify Git-tracked files; you do not need to request worktrees each time.

## Lifecycle

```text
read-only request -> inspect in place
modifying request -> Start -> edit/check -> Commit -> Ready -> FIFO Finish
                                      failure -> preserve -> Recover -> retry
```

Multiple slots do not merge themselves. Each completed task passes through the one-at-a-time Finish gate. If another task advanced the default branch, the later candidate merges that new local default into its own branch and resolves any real conflict before integration.

The first task in each slot can be slower because checkout and project dependencies are lazy. Each slot keeps its own `.venv`, `node_modules`, `.env*`, and caches for later tasks; these are never copied from the primary worktree.

## Direct runner use

Codex normally drives the bundled runner. For diagnostics:

```text
uv run --script skills/develop-with-worktrees/scripts/dww.py --repo <repo> status
uv run --script skills/develop-with-worktrees/scripts/dww.py --repo <repo> status --detailed
```

Use `--json` before the subcommand for automation. See the skill's `references/` directory for configuration, lifecycle, and safety details.

Use `disable` and `enable` for a machine-local personal opt-out. This preference stays under `.git/solo-ai` and does not change repository policy for anyone else.

## Comparison with Worktrunk

[Worktrunk](https://github.com/max-sixty/worktrunk) is an excellent general-purpose worktree CLI with polished hooks, path templates, shell integration, and fast interactive ergonomics. This project serves a narrower purpose: it is an opinionated Codex execution policy for autonomous solo AI coding. Its distinguishing pieces are fixed reusable slots, task leases, conservative local-only defaults, proof-bound validation reuse, FIFO integration, sensitive-content checks, and failure-preserving cleanup.

The projects are complementary. We borrow the idea that worktree tooling should feel simple and scriptable, while deliberately avoiding automatic remote operations, default squash/rebase, unknown-file cleanup, and gate bypasses.

## Development

```text
uv sync --dev
uv run pytest
uv run <path-to-skill-creator>/scripts/quick_validate.py skills/develop-with-worktrees
```

Tests create temporary Git repositories and never use your current project as a test bed. CI runs on Windows, Linux, and macOS.

## Status

Version 0.1 is intentionally conservative. It is suitable for local trials, but the secret scanner is not a dedicated security product and compatibility mode does not wrap an existing orchestrator.

## License

MIT
