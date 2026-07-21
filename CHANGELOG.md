# Changelog

## 0.1.0-beta.2 — 2026-07-21

- Republishes the public beta from its maintained GitHub namespace.
- Updates the CI badges and documented marketplace command to the new canonical repository.
- Fixes task-worktree CLI calls so `commit`, `ready`, and `finish` keep validating managed slot paths against the primary worktree.
- Recognizes the standard `.pytest_cache` and `.ruff_cache` directories as reusable local tool caches while continuing to block other unknown ignored files.

## 0.1.0-beta.1 — 2026-07-21

First public beta.

- Adds a local-first Codex workflow for 1–5 reusable, isolated Git worktree slots.
- Requires explicit repository adoption and reviewed validation before local integration.
- Preserves task state on conflicts or failures; integration is local-only, FIFO, and fast-forward only.
- Defers without writing anything when a repository already has Worktrunk, `scripts/worktree-flow.ps1`, or another mature workflow marker.
- Provides content-addressed redacted logs, exact-path commits, proof reuse guards, safe deinitialization, and Windows/Linux/macOS CI.

This is a beta release. Please report reproducible lifecycle, installation, or compatibility problems without including credentials, private paths, or unredacted logs.
