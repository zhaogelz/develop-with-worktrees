# Development and documentation maintenance

This repository ships the `develop-with-worktrees` plugin and its local Git
lifecycle runner. This guide is for maintainers changing the repository; it is
not a second user guide or a lifecycle contract.

## Work locally

Use [AGENTS.md](../AGENTS.md) and the installed `develop-with-worktrees` skill
for any repository change. Managed changes begin with `route` and `start`; edit
only in the returned worktree, commit reviewed paths, and follow the candidate
through local integration. Do not use this guide to bypass that lifecycle.

Install the development tools with:

```text
uv sync --dev
```

Use `uv` for Python commands. The tracked verification policy documents the
available development, Ready, Full, and Stress profiles in
[`.solo-ai/verification.toml`](../.solo-ai/verification.toml).

## Distribution and local installation boundaries

The public user path has one official GitHub marketplace identity, shown in the
README. Keep that public installation path independent from a developer checkout
and from any machine-specific migration directory.

A development checkout is source, not a daily runtime source. When two local
development paths have competed for one installed identity, a fixed committed
snapshot may be exported to a separate local marketplace to preserve a known
runtime while the machine is migrated. That local marketplace is a per-machine
transition aid, not a public distribution default and not proof that an official
release already contains a fix. Do not put its name, account path, or local
release directory in the public README.

After a plugin code, Hook, or packaged-document change, verify and commit the
fixed source first. A later formal release must use that fixed content and the
project's versioned release process. Different release content requires a new
version: never overwrite an existing formal version or deliver different content
under the same version to a daily-use cache. Keep development testing on an
isolated source. Do not push, tag, create a Release, replace a public
installation, or claim that the official distribution includes the change
without separate verification and authorization.

Treat installation, enablement, Hook trust, and runtime loading as separate
facts. For a local desktop check, use the same desktop executable and Windows
user that will run the plugin, then verify in this order:

1. The intended marketplace and plugin are installed from the expected fixed
   source.
2. Only the intended plugin identity is enabled; do not uninstall an older
   identity merely to switch it off.
3. The host reports the exact current Hook definition as trusted, when review
   is required.
4. A real host session loads the expected skill and enforces the expected Hook
   behavior.

An isolated test `CODEX_HOME` may exercise installation tests, but it cannot
stand in for the production user or bypass a production Hook, cache, config, or
trust decision. Never hand-edit those stores to manufacture a passing result.

## Checks

Run the smallest useful check while developing, then the checks selected by the
task lifecycle. These direct commands are useful for documentation and package
maintenance:

```text
uv run python scripts/verify_repository.py
uv run ruff check .
uv run ruff format --check .
uv run pytest -q tests/integration/test_cli.py
```

`scripts/verify_repository.py` parses tracked text configuration and verifies
repository-local Markdown and HTML links. It does not prove prose matches the
runtime contract or validate Markdown heading fragments. The document and
package tests therefore also check the authoritative routes and plugin-contained
references.

The isolated plugin install test is an explicit, higher-cost check:

```text
uv run pytest -q tests/integration/test_plugin_install.py::test_plugin_install_and_clean_uninstall_in_temporary_codex_home
```

It may be skipped when the Codex CLI is unavailable. A skip is not evidence of
a successful local installation. Broad Full and Stress verification remain
explicit diagnostic work; do not add them to every documentation edit.

When changing Hook parsing, matcher definitions, or the guard, run the focused
Hook tests first. After a formal plugin installation, use the same Codex
executable that users run to verify a harmless `apply_patch` in its owner task,
then verify that a different session and a protected base-worktree target are
denied before writing. Record the executable path and version with the source
commit and installed hook hash: a passing PATH CLI installation test does not
prove a different desktop-host executable loaded the updated Hook.

### 原生连续性验收

原生 App Server 验收由仓库外的控制器驱动；源码仓库只提供可重复的严格判定器：

```text
uv run python scripts/verify_native_patch_owner.py --events <证据目录>/appserver-events.json --turn-ids <证据目录>/turn-ids.json --owner-session-jsonl <owner 原始会话 JSONL> --repo <测试主工作树> --worktree <owner 隔离工作树> --result <证据目录>/verification.json
```

判定器只接受真实 `turn/completed`、`hook/completed`、`item/completed` 事件和
最终字节/Git 状态；它核对 owner、B、C 各自独立的 thread/turn 身份，并要求
owner 前两次写入、B 拒绝及其 turn 完成、C 拒绝及其 turn 完成、owner 的
`dww_test_barrier` 至少须有一个开始或请求事件，且出现的每个开始/请求都必须与同一
owner item 关联；其完成、owner 第三次写入和 owner turn 完成保持严格顺序。目标文件出现
任何非 owner 的成功 `fileChange` 也会使断言失败，即使另有三条合法 owner 写入。
最终现场要求 W 的 porcelain 仅允许 `probe.txt`，R 必须完全 clean；Git 查询非零
退出或文件读取失败均不能当作 clean。控制器必须传入 owner 的本地原始会话 JSONL；
判定器将其 `session_id` 和每次 `custom_tool_call` 的 `turn_id` 与 owner 身份绑定，并从
三次原始 `apply_patch` 输入中严格读取“相对、绝对、绝对”路径序列。它不接受控制器
写入的派生路径形式事件，也不从归一化后的 `fileChange` 目标路径推断通过。原始会话
JSONL 可能包含敏感上下文，应只在本机作为输入保留，不随证据包或提交发布。缺少或无法
绑定原始路径证据时状态为 `BLOCKED_HOST`；真实断言失败为 `FAIL_ASSERTION`，事件身份
或顺序不完整为 `INVALID_RUN`。验收控制器在
宿主退出后必须通过正式 DWW
`abandon --retain-worktree` 保留失败现场，不能只打印模型回复作为 PASS。

## Keep one source of truth

Use [architecture.md](architecture.md) for durable product and architecture
boundaries. The plugin's [skill](../plugins/develop-with-worktrees/skills/develop-with-worktrees/SKILL.md)
routes operating details to one reference per topic. Update the complete
reference for a changed contract and only the user-facing summary that the
change affects. Do not duplicate a whole protocol in a README, an anchor, and a
reference.

Plugin documentation must remain self-contained inside
`plugins/develop-with-worktrees`: installed skills cannot rely on `docs/` in
this source repository. Keep relative links valid after packaging.

The promotional video is a separate source project. Read its local
[`AGENTS.md`](../videos/develop-with-worktrees-promo/AGENTS.md) only when that
project is in scope. Markdown-only edits there do not require rendering; HTML,
assets, or timing changes use its existing `npm run check` workflow.
