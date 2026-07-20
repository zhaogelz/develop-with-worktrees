# Architecture

The repository ships one self-contained Codex skill. Its PEP 723 runner imports adjacent modules and stores all mutable runtime data in the target repository's Git common directory.

Tracked policy and local state are intentionally separate:

```text
target repository
├── .solo-ai/config.toml          tracked policy
├── .solo-ai/verification.toml    tracked validation map
├── AGENTS.md                     tracked invocation rule
├── .worktrees/                   reusable linked worktrees, locally excluded
└── .git/solo-ai/                 local state
    ├── state.json
    ├── approvals.json
    ├── queue/
    ├── locks/
    ├── logs/
    └── proofs/
```

State mutations use atomic file replacement under an atomic-directory lock. Integration uses a timestamped FIFO ticket plus one shared integration lock. A task lease prevents a stale agent window from mutating a recovered task.

Validation proofs are content-addressed. Candidate proofs include the candidate commit/tree and current default-branch commit, so Ready-to-Finish reuse is exact. Per-profile proofs hash every tracked file in the profile's declared path closure plus policy, commands, lockfiles, tools, and platform, allowing conservative reuse across tasks when those inputs are identical. Referenced logs must still exist for either form of reuse.

The process manager is deliberately narrow. It owns only processes launched through the configured lifecycle command and records enough identity to reject PID reuse or an unrelated process.
