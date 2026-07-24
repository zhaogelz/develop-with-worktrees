---
name: develop-with-worktrees
description: "Use for any Git-repository task that may modify files. Ask one plain-language repository choice on the first write, then default to isolated local worktrees. Do not use for read-only analysis."
---

# Develop with Worktrees

Use this lifecycle for every modifying Git task unless the user chooses ordinary current-directory development. Explain in the user's language. The core CLI is host-neutral; the trusted Codex hook is a hard write guard for Codex-supported local tool paths, not an operating-system sandbox.

Set `DWW` to this skill's absolute `scripts/dww.py` and invoke it only with `uv`:

```text
uv run --script <DWW> --repo <repository-or-worktree> <subcommand>
```

## First modifying intent in an unchosen repository

Read-only analysis never asks a question or claims a task. After repository instructions are read, when the user first intends to modify an unchosen Git repository, ask exactly this one question:

```text
此仓库怎么修改？

1. 每个任务使用独立目录（推荐）
   任务互不影响，完成后自动合回。

2. 这一次直接改当前目录
   只跳过这一次，下次还会询问。

3. 以后都直接改当前目录
   记住此选择，这个仓库不再询问。

只影响本机，可随时修改。
```

Do not add an initialization, test-discovery, or static-validation question. Do not explain internal terms unless the user asks.

- Choice 1: run `choose --mode isolated`. It sets up the managed lifecycle once. If no automated test is found, it silently uses its internal basic checks; it does not ask again.
- Choice 2: obtain the session identifier from trusted hook context and run `choose --mode current-task --session <id>`. For the rest of this session, work in the current directory exactly as if this skill were absent: do not create policy files, start a task, or run Commit/Ready/Finish. A new task asks again.
- Choice 3: run `choose --mode current-repository`. It locally disables this repository on this machine without changing tracked files; do not initialize or run this skill later unless the user changes the choice.

The choices are local to the current clone/common Git directory. A different clone or machine chooses independently. Natural-language changes such as “以后使用独立目录开发” and “以后直接在当前目录开发” are explicit new choices; apply them with `choose` without repeating the prompt. “在主分支完成 / 合到 main / 提交到 main” is not a bypass: ask whether current-directory execution is required if the intent is unclear.

When choice 2 needs a writing child agent, pass its returned one-time delegation code only in that child’s task instruction. The child registers its own hook session with `choose --mode current-task --session <child-id> --delegate <code>` before writing. Do not reuse the code for an unrelated task. Codex currently supplies no reliable parent-agent hook identifier, so this explicit delegation is the only safe way to extend the one-task choice without opening concurrent unrelated tasks.

## Default managed task

For an adopted repository, proactively run `start --name <purpose>` when a modifying intent is clear. It derives from the invocation worktree's current local branch.

1. Work only in the returned worktree. Keep its lease private.
2. Commit exactly the reviewed paths with repeated `--path`; never use unscoped staging.
3. Use `plan` or `verify --level development` as useful feedback. Slow estimates are advice only.
4. Run `ready`, then `finish` with the same task and lease.

Ready/Finish synchronize only the recorded base branch. A deleted, rewound, or rewritten base requires explicit `retarget`; Finish only fast-forwards the recorded clean base worktree. It never fetches, pulls, pushes, opens a PR, rebases, squashes, amends, or rewrites history.

## Advanced guarded current-worktree task

`start --in-place` remains a compatibility path only when the user explicitly asks to retain DWW's exact Commit/Ready/Finish safeguards while using the current clean worktree. It is not the meaning of choice 2. Follow [lifecycle.md](references/lifecycle.md) for its session, identity, and recovery requirements.

## Validation, cleanup, and limits

- `verification.toml` supports schema 3 only. Commands are registered argv arrays, never shell strings.
- Development, Ready, and Full evidence are separate. All changed candidate paths need Ready coverage unless internal static-only policy is active.
- Validation uses a machine-global weighted FIFO queue. `settings --validation-capacity auto|1..4` is local-only.
- Finish never removes dependencies or caches. `prune-slot` requires a reviewed exact plan and digest; `.env*`, symlinks, junctions, or changes stop the whole deletion.

Read [configuration.md](references/configuration.md), [lifecycle.md](references/lifecycle.md), and [safety.md](references/safety.md) before changing policy or handling an exception.
