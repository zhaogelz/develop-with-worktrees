# Architecture

`develop-with-worktrees` is a Codex marketplace repository containing one plugin, not merely a prompt file:

```text
marketplace root
├── .agents/plugins/marketplace.json
├── plugins/develop-with-worktrees/
│   ├── .codex-plugin/plugin.json   plugin metadata
│   ├── hooks/                      lightweight Codex guardrail
│   └── skills/develop-with-worktrees/  task-facing policy and runner
└── tests/                          CLI, lifecycle, hook, and fault tests
```

The plugin deliberately splits responsibilities:

| Layer | Responsibility | Boundary |
| --- | --- | --- |
| Hook | Detect likely supported Codex writes; inject a mode-aware reminder | Guardrail only; no OS/IDE enforcement claim |
| Skill | Decide read-only/defer/adopt/managed workflow and guide the agent | Must not self-bypass a user decision |
| CLI | State transition, path staging, proof, process, integration, cleanup | Hard gate for the plugin lifecycle |

## Repository policy versus local state

```text
target repository
├── .solo-ai/config.toml            tracked schema-2 policy
├── .solo-ai/verification.toml      tracked schema-2 validation map
├── AGENTS.md                       exact tracked managed block
├── .worktrees/solo-ai-slot-01..05  local managed worktrees
└── <git-common-dir>/solo-ai/        untracked, machine-local state
    ├── bootstrap.json               dirty-primary bootstrap only
    ├── preferences.json             local disable choice
    ├── approvals.json               per-machine policy approval
    ├── state.json                   slots, masked leases, operations
    ├── queue/ and locks/            FIFO integration coordination
    ├── logs/content/                redacted content-addressed logs
    └── proofs/ profile-proofs/      reusable evidence metadata
```

Policy is shared only after an explicit adoption commit. State follows the Git common directory so linked worktrees share it, but it is never copied, committed, or disk-scanned from a plugin registry.

## Core invariants

1. Mode precedence is local disable, existing mature workflow defer, managed adoption, then uninitialized confirmation.
2. Initializing an existing workflow writes nothing. A dirty primary gets a pending bootstrap; no stash or primary mutation is used.
3. Every mutation records a live operation before work starts. Recover cannot steal a live lease.
4. Commit stages an exact task change manifest, never `git add -A`.
5. Integration is local-only FIFO plus `ff-only`; conflicts preserve the task.
6. Cross-task proof reuse is disabled unless a profile states closed inputs and no external state.
7. Cleanup uses exact owned paths and stops on unknown/protected data.

## Versioning and removal

Tracked and local schemas are versioned. A future update must inspect active state first: active tasks freeze new adoption and migration, while the update must retain a compatible Finish/Recover path for those tasks. An empty state may receive only an additive local migration. Security or tracked validation policy changes always require a user-reviewed bootstrap commit and per-machine approval.

Removal is intentionally two-stage: repository `deinit` first, plugin uninstall second. This keeps a user from losing the lifecycle runner while a repository still has owned state.
