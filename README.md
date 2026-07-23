# Develop with Worktrees

`0.2.0-beta.1` is a local-first Codex workflow for safe parallel Git tasks: isolated reusable worktrees, exact-path commits, registered validation, and local-only integration back to the branch you started from.

## What it does

- Starts from the current checked-out local branch, not a fixed `main` assumption.
- Keeps tasks isolated with leases, deterministic slots, conflict prediction, recovery receipts, and fast-forward-only local integration.
- Uses schema 3 validation profiles with development, ready, and explicit full levels.
- Shares a machine-global weighted FIFO validation queue. Capacity is automatic by default and adjustable locally with `dww settings --validation-capacity auto|1..4`.
- Keeps cleanup manual: Finish never deletes caches. `prune-slot` deletes only exact declared paths after a reviewed plan and digest confirmation.
- Defers with zero writes when the repository already has a mature worktree workflow.

It never fetches, pulls, pushes, creates PRs, rebases, squashes, amends, or rewrites history.

## Installation

Install from the public GitHub marketplace:

```text
codex plugin marketplace add zhaogelz/develop-with-worktrees --ref main
codex plugin add develop-with-worktrees@develop-with-worktrees
```

Start a new Codex task after installation so its skill is loaded. To install from a local checkout instead, add that checkout as a local Codex marketplace and install the same plugin identifier. The plugin does not update itself automatically; refresh the marketplace and reinstall only when you choose to adopt a newer release.

The first modifying task in a repository shows an adoption plan. `init --accept` is required before tracked policy is written. Existing repositories upgrading from schema 2 must migrate their own verification policy to schema 3 before using this release.

## Everyday flow

```text
doctor → start → edit in returned worktree → commit exact paths
       → plan / verify (optional development feedback) → ready → finish
```

`plan` includes mapped profiles, resource class, local duration estimates, and any unmapped paths. A slow-validation notice is advice to split mappings or remove repeated preparation; it never weakens coverage.

See [README.zh-CN.md](README.zh-CN.md), [configuration](plugins/develop-with-worktrees/skills/develop-with-worktrees/references/configuration.md), and [architecture](docs/architecture.md).
