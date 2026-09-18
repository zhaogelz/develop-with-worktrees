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
