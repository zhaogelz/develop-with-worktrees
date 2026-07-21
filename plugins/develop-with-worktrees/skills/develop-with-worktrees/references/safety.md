# Safety reference

## What is enforced where

The lifecycle CLI is the hard gate for work started through this plugin. The bundled Codex hook is intentionally lightweight and stdlib-only. It can inject a pre-write policy reminder for supported Codex local tools, but the current Codex `PreToolUse` interface does not provide a portable hard-deny result. Therefore it is a strong guardrail, not OS, IDE, or manual-edit enforcement. Hook failures emit a conservative model-visible warning; an agent must not disable the hook or local preference to bypass policy.

## Candidate content gate

The built-in gate reports only a path, line, and rule name. It blocks newly added `.env*`, private-key/certificate files, credential files, recognizable private-key headers, common provider tokens, and assignments to secret-like names unless an exact repository allowlist applies. It does not print a secret value.

A repository may declare a dedicated scanner before the built-in gate. This supplements rather than replaces the built-in gate. Never add a scanner command without repository approval.

## Logs, proofs, and privacy

Command execution uses explicit argv and `shell=False`. Logs redact common credential patterns before persistence and are content-addressed under local Git common metadata. Proofs store command digests and redacted displays, never raw environment values. Declared environment values are SHA-256 hashes or `absent`. A missing or changed log invalidates its proof.

Exact candidate reuse binds candidate commit/tree and default head. Profile reuse across different task branches is disabled unless the profile explicitly says `cross_task_reuse = true` and `external_state = "none"`; its full declared tracked/dependency/environment closure, policy, tools, platform, and logs must still match. Time is audit data and cannot make a proof valid by itself.

## Process and cleanup ownership

Development processes are spawned without a shell in an isolated process group/session. The root process identity contains PID, creation time, executable, cwd, and a command-line digest rather than a raw command line. Stop validates the root before terminating its descendant tree. Port retries are confined to the owning slot's 100-port range.

Ignored `.env*` files are protected and block release or deinit. Unknown ignored files and process mismatches also block cleanup. Pruning is never automatic. `prune-proofs`, `prune-logs`, `prune-slot`, abandon, and deinit all need explicit confirmation and exact managed-path checks.
