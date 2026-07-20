# Configuration reference

`.solo-ai/config.toml` is tracked repository policy. Supported keys:

- `mode`: `managed`, `compatible`, or `disabled`.
- `slots`: fixed pool size from 1 through 5; default 3.
- `branch_prefix`: default `codex/`.
- `worktree_directory`: managed slot parent, default `.worktrees`.
- `port_base`: first slot's 100-port block; later slots advance by 100.
- `remote_policy`: version 1 supports `local-only`.
- `sensitive_allowlist`: exact glob exceptions reviewed by the repository owner.
- `lifecycle.dev_start`: optional shell command with `{port}` and `{slot}` placeholders.

`.solo-ai/verification.toml` contains ordered profiles. Each profile has an id, path globs, and commands. Changing the effective command set invalidates approval and requires:

```text
uv run --script <DWW> --repo <repo> approve --accept
```

Local runtime state lives under `.git/solo-ai` (or the shared Git common directory for linked worktrees). It must not be committed or copied between machines.

`disable` and `enable` write only `.git/solo-ai/preferences.json`. They provide a personal machine-local opt-out and never alter the tracked repository mode.
