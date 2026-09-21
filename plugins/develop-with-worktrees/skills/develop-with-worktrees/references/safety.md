# Safety reference

Use this reference before trusting a Hook, releasing a worktree, retiring a batch,
or publishing remotely. DWW's CLI and persisted Git facts own lifecycle truth.
The Codex Hook is optional scoped hardening, not the only safety boundary.

## Hook trust and scope

A trusted plugin Hook may deny supported Codex local writes to a protected base
worktree before they happen. It recognizes a limited read-only command subset and
fails closed for unknown or compound supported writes. The Hook derives repository
scope from the current working directory and verified Git facts. For native
`apply_patch`, it also inspects declared targets before an outside-repository
session can leave routing: a managed target still requires its recorded owner.
An unrelated repository cannot supply authorization for that target. It does not claim
to sandbox deliberate cross-repository or specialized bypass paths.

`hooks/hooks.json` is the stable trust contract. Ordinary plugin, skill, and
guard-script changes do not alter it. A first install or intentional definition
change may require review. When the host reports pending review, explain the
changed behavior and use supported host control after approval; never edit trust
storage or bypass Hook trust. Without a trusted Hook, route fallback and the full
CLI lifecycle remain supported.

A mature workflow remains routing authority. Hook, idle, and session events may
wake reconciliation but cannot select candidates, complete a task, delete an
anchor, or release a slot.

For an isolated-task write, the Hook requires the event's exact Codex session
identifier to equal the task's recorded `host_origin.thread_id`. An absent,
malformed, or different owner fails closed before the supported write runs.
Hook `cwd` is the Codex session checkout, not a per-command exec workdir. For a
DWW runner command, the Hook therefore parses the literal `uv run --script`
shape and uses its `--repo` target only after verifying the installed runner and
the same Git common directory. A target task then goes through the existing
owner, root-refresh, branch, HEAD, and directory-identity checks; a foreign
repository, fake runner, shell control syntax, duplicate/missing repo option,
or malformed quoting remains denied.
For `apply_patch`, the current Codex Hook contract carries the patch in
`tool_input.command`. DWW also recognizes its older explicit `patch` field and
direct-string form for compatibility, but rejects missing, malformed, or
conflicting forms. It resolves every declared Add, Update, Delete, and Move
source/destination path from the event working directory first: all repository
targets must resolve to one active task in the same Git common directory. This
permits an owner's absolute-path patch from the base worktree and rejects a
cross-task or mixed-target patch as a whole. `Bash`, `Edit`, and `Write` receive
the same owner check for the current working directory, but DWW does not infer
arbitrary shell side effects or invent target fields the host did not provide. A
host that omits a reliable Hook event, session identifier, or complete patch
remains outside this protection and must be reported as such in native-host
verification.

For `apply_patch` only, a verified current-session delivery artifact under
`CODEX_HOME/visualizations/YYYY/MM/DD/<session-id>/` may be written even when
`CODEX_HOME` is itself a Git repository. The Hook derives CODEX_HOME from its own
process environment and the session identifier from the host event; it never
trusts a tool-input allowlist, environment, or session claim. Every patch target
must be in that one artifact root. Other sessions, CODEX_HOME configuration,
plugins, skills, sessions, Git metadata, tracked or staged files, nested
repositories, links, junctions, special paths, and mixed target sets remain
denied. This is a delivery-artifact exception, not a general external-write
allowlist or an operating-system sandbox.

An active DWW task worktree can itself be located beneath `CODEX_HOME`. That
does not make CODEX_HOME writable: every patch target must still be inside that
one registered task worktree, and the Hook rechecks its exact directory identity,
branch, candidate head, and owning session. This task-worktree rule is separate
from the delivery-artifact exception; a CODEX_HOME setting, plugin, skill, Git
metadata path, foreign worktree, or mixed patch never inherits task permission.

For an active isolated task with a bound root, the supported trusted
`SessionStart` path records only a small “refresh required” marker. Before a
supported write, the Hook requires `anchor refresh-root` to read the current task
and complete root once; ordinary read-only queries and the trusted DWW `anchor`
command remain available. This check is scoped to observed Hook events and tools,
not an operating-system sandbox. The CLI binding and version gates remain the
fallback when the Hook is not trusted or an event/tool is not covered.

### Restricted local plugin maintenance

The Hook keeps plugin maintenance separate from its ordinary read-only shell
parser. With exactly one active isolated task owned by the current Codex
session, it permits only an absolute, verified Windows Codex CLI to query
plugins or marketplaces, or to refresh `develop-with-worktrees` from the fixed
local `dww-stable-local` marketplace. A root refresh is still required before
the refresh write. It also permits exactly one installed-plugin maintenance
entry: the formal PowerShell 7 executable, `-NoProfile -File`, and the
`maintain-dww-plugin.ps1` stored beside the currently trusted Hook, followed by
the fixed `Install`, same-common-dir primary `main` source root, full-commit
and formal-Codex parameters. The script independently verifies that the source
commit is exactly `main`. It
rejects a different executable or script, plugin, marketplace, marketplace
add/remove, `-MigrateMarketplace`, added or reordered parameters, shell
composition, and configuration injection. The one-time move from an older
marketplace source remains a human-only system PowerShell maintenance action;
ordinary updates do not switch marketplace roots.

Any command that appears to invoke the maintenance script or a Codex plugin
subcommand but fails that exact contract is denied before the generic isolated
task write allowance. This keeps a fake executable, fake script, or composed
command from inheriting ordinary task permission.

The stable-market release directory is not an archive. During a package switch
the script keeps at most one `.previous-release` together with its one
`.stage-*` recovery scene. Once the new active receipt is written, it removes
the stage immediately. A later run rejects any remaining stage, and also
rejects a previous release unless the active receipt and active plugin exactly
match the requested source commit and tree. That one exact state may only
continue marketplace/plugin installation and read-back; a successful read-back
removes the previous release, while another install failure preserves it. The
resume path will not switch marketplace sources. The script never creates
`releases/` or accumulates old plugin copies.

`Check` is a read-only layout report: it does not call Codex or write files. A
missing market root, or an uninitialized root containing only one ordinary,
non-link legacy `releases/` directory, is an acceptable starting layout. The
legacy directory is preserved when `Install` initializes the new market;
unknown, linked, or mixed existing content remains rejected.

Ordinary source tasks Finish and return their worktrees. A coordinator starts a
separate short-lived maintenance task only when a plugin release is needed; an
ordinary development worktree is never retained merely to publish a package.

## Identity and content protection

Every managed Start, Commit, Ready, Finish, Recover, Abandon, and cleanup action
checks the recorded worktree, branch, base/head, ref, and platform directory
identity. A moved reference, dirty unknown content, replaced path, unreadable
directory, or ambiguous Git fact preserves the scene. DWW does not reset, clean,
rollback, move, or silently adopt it.

Logs redact common credentials. Proofs record command digests, hashes, redacted
displays, and results; they do not persist environment values, raw session IDs,
leases, or raw command lines. Runtime Adapter input closure is locally approved.
An Adapter failure or contamination preserves its task or candidate and leaves
the base unchanged. See [Runtime Adapter](runtime-adapter.md).

## Task return and abandonment

Finish never cleans dependency caches. Retained known dependency roots are
opaque, including normal package links; their ancestors must not be links.
Ordinary task release and Abandon require exact worktree identity. Abandon refuses
tracked edits and deletes ordinary untracked files only through unchanged-object
checks. It protects `.env*`, databases, uploads/storage, unknown ignored paths,
replacements, symlinks, and junctions.

`reclaim-retained` is the only path that returns an already terminal,
retain-worktree slot. Its read-only checklist confirmation binds the terminal
task, slot generation, path identity, branch/HEAD, base, and each ordinary-file
snapshot. A diagnostic preview may report multiple observable protected,
unknown, linked, unreadable, or unsnappable paths, but an incomplete or blocked
scan never issues a confirmation or deletion list. Confirmation reruns the
strict identity, content, and unchanged-object checks, resumes only its persisted phase,
and keeps the task record, branch, and receipts after the slot becomes reusable.
Known dependency caches remain in place; reuse is a safe return, not a physical
worktree deletion.

An active in-place task additionally binds its trusted session, branch, start
head, and expected head. A mismatch quarantines it. A post-session handoff
requires explicit exact confirmation and cannot recreate or clean an ambiguous
task.

## Runtime and validation boundaries

Adapter activation happens after an exact isolated task and anchor exist; release
happens after the immutable candidate ref exists. Batch activation and release
wrap combined Full when configured. An uncertain or nonzero Adapter result,
approval drift, timeout, or contamination blocks advancement. Successful release
is required before promotion. Runtime resources and effects remain project-owned;
DWW supplies identity and port facts but does not interpret services, databases,
authentication, or browsers.

Validation commands use explicit argv and bounded, observed processes. DWW never
adopts or kills an unverified process. See [verification reuse](verification-reuse.md)
and [recovery](recovery.md) for proof and interruption rules.

## Cleanup modes

| Operation | Use only when | What must remain protected |
|---|---|---|
| Ordinary task return | task/candidate reaches its normal terminal transition | dependency roots, unknown content, task identity |
| Reusable batch return | current owner/generation has exact clean Git state and confirmed runtime release | later generation's workspace, protected/unknown content |
| `batch retire` | a failed dedicated batch needs its exact detached workspace removed | candidate refs, history, original directory and Git identity |
| `batch retire --fast` | explicitly approved old failed dedicated batch whose candidates are superseded or withdrawn | the same identity/link/protected-content checks; only regenerable dependency hashes are skipped |
| `prune-slot` | reviewed one-time plan names exact top-level owned paths | task generation, staging manifest, changed/late/protected/link content |

A reusable workspace is returned, not physically cleaned. Dedicated retirement
uses a persisted intent and exact removal receipt. Fast retirement stages the
same directory on the same volume and never follows links; it is a maintenance
shortcut, not ordinary recovery. `prune-slot` is bounded to a reviewed plan,
recorded before the first move, and resumes only from that manifest.

On Windows, removal pins original ancestors and conditionally deletes the same
opened object. On all platforms, changed, recreated, protected, ordinary
untracked, unknown ignored, or reparse content stops cleanup. Non-force `git
worktree remove` is not a safety boundary for a populated tree.

## Local-only and remote boundary

DWW lifecycle operations never fetch, pull, push, create a pull request, deploy,
rebase, squash, amend, or rewrite history. If the user explicitly asks to publish
after integration, work from the clean base worktree, verify the branch and
remote, run a dry-run first, and use an ordinary non-force push. Remote
divergence stops publication; it does not authorize an automatic pull, rebase,
merge, deletion, tag, PR, or deployment.

## What DWW does not enforce

DWW provides lifecycle safety, not an operating-system sandbox. A specialized
path may bypass a Hook, and a project Adapter may deliberately escape the process
boundary described by its contract. Those limits must stay visible rather than
being presented as successful containment.
