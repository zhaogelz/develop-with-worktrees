# Safety reference

Use this reference before trusting a Hook, releasing a worktree, retiring a batch,
or publishing remotely. DWW's CLI and persisted Git facts own lifecycle truth.
The Codex Hook is optional scoped hardening, not the only safety boundary.

## Hook trust and scope

A trusted plugin Hook may deny supported Codex local writes to a protected base
worktree before they happen. It recognizes a limited read-only command subset and
fails closed for unknown or compound supported writes. The Hook derives repository
scope from the current working directory and verified Git facts; it does not claim
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
For `apply_patch`, it resolves every declared Add, Update, Delete, and Move
source/destination path first: all repository targets must resolve to one active
task in the same Git common directory. This permits an owner's absolute-path
patch from the base worktree and rejects a cross-task or mixed-target patch as a
whole. `Bash`, `Edit`, and `Write` receive the same owner check for the current
working directory, but DWW does not infer arbitrary shell side effects or invent
target fields the host did not provide. A host that omits a reliable Hook event,
session identifier, or target path remains outside this protection and must be
reported as such in native-host verification.

For an active isolated task with a bound root, the supported trusted
`SessionStart` path records only a small “refresh required” marker. Before a
supported write, the Hook requires `anchor refresh-root` to read the current task
and complete root once; ordinary read-only queries and the trusted DWW `anchor`
command remain available. This check is scoped to observed Hook events and tools,
not an operating-system sandbox. The CLI binding and version gates remain the
fallback when the Hook is not trusted or an event/tool is not covered.

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
