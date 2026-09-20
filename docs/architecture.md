# Architecture

DWW is a local Git delivery layer for AI-assisted repository changes. It gives
the host a durable execution identity without taking over the host's task graph
or the project's product and runtime responsibilities.

```text
Host task system
  decomposition, dependencies, workers, user-facing status
                         |
                         v
DWW lifecycle router
  defer -> mature project workflow
  delegated -> exact locally approved adapter
  managed -> task identity + anchor + isolated worktree
                         |
                         v
Git lifecycle
  exact commit -> source candidate -> combined verification -> local promotion
                         |
                         v
Project contract
  test selection, runtime resources, product decisions, optional effectiveness check
```

## Responsibility boundary

The host owns task decomposition, scheduling, waiting, and messages. DWW owns
route admission, local task identity, worktrees, anchors, exact commits,
candidate publication, local integration, and recovery receipts. The project
owns validation commands, ports, databases, browsers, deployment semantics, and
product decisions.

The former generic orchestration store is drain-only compatibility. New work
must not create a second DWW scheduler, task DAG, candidate group, or persistent
coordinator.

## Default, optional, and legacy surfaces

For an individual or small team, the default surface is deliberately narrow:
route the repository, work in the returned isolated worktree, commit reviewed
paths, publish a candidate, and follow its local delivery or recovery. The
invariants below make that short path safe; they are not optional complexity.

Some capabilities remain available only for a specific boundary. A delegated
adapter is for a mature repository that has an exact tracked contract and local
approval. A Runtime Adapter is for project-owned external runtime resources.
Host handoff records support an interrupted repair when the host needs a durable
receipt. These capabilities do not replace host scheduling and are not part of
ordinary setup.

The generic `orchestrate` surface is legacy drain-only compatibility: it may
inspect, cancel, or finish records that already exist, but it must not create
new work. Removing it requires evidence that retained local state no longer
needs safe recovery; it is not a shortcut for simplifying the default flow.

## Core invariants

- A detected mature workflow has routing priority. DWW writes no managed state
  for `defer`; `delegated` requires an exact tracked contract and local approval.
- A managed task receives one isolated worktree and one local task anchor before
  it becomes writable. Identity uncertainty, dirty unexpected content, or a
  moved reference preserves the scene instead of adopting or deleting it.
- Initialization adopts the attached local branch of the invoked worktree,
  including a linked worktree. Its policy commit, managed slot root, pending
  bootstrap, task base, and local promotion remain bound to that recorded branch
  and worktree identity; remote defaults and legacy default-branch preferences
  are only compatibility queries, never an override for a recorded target.
- The optional Hook may allow an `apply_patch` target in either a current host
  session's verified `CODEX_HOME/visualizations/YYYY/MM/DD/<session>` artifact
  root or one registered active task worktree. A task worktree beneath
  `CODEX_HOME` still needs its exact directory identity, branch, candidate head,
  and owner session to match. Neither narrow exception grants access to other
  Codex data, Git-tracked content, nested repositories, links, or mixed targets.
- Native patch checks resolve managed target repositories even when the session
  starts outside Git; an unrelated checkout cannot bypass the target's protection.
  Other Hook events use the session checkout as the repository domain, while a DWW
  runner's literal `--repo` selects the lifecycle target. The Hook accepts that
  target only when the installed runner is exact and both worktrees share the
  same Git common directory; the target task still passes the existing owner,
  root-refresh, branch, HEAD, and directory-identity checks. It never treats an
  arbitrary command argument or a foreign repository as an execution context.
- A confirmed objective has one root anchor. It retains the complete plan and
  explicit amendments; its child anchors retain only their execution slices.
  Exact host identity may map to that root as a locator only; it never stores a
  second plan or becomes a scheduler.
- An exact-path commit creates the only eligible task change. Candidate
  publication is immutable source preservation, not delivery.
- New repositories publish source candidates, automatically freeze an exact
  compatible group of three, and run affected combined checks before protected
  local promotion. A smaller tail requires a recorded cause and reason.
- Local approval covers only the commands a lifecycle step will execute; an
  unchanged complete approval can cover a smaller step. Approval does not include
  proof-only facts such as lockfiles or tool versions.
- A passed pure proof is reusable only under matching declared inputs,
  environment, tools, and logs. Runtime effects and required artifacts are not
  replaced by a prior success report.
- A composition conflict, validation failure, and promotion block remain
  distinct. Deterministic recovery uses recorded identities; it never changes
  the base by guessing a merge or retrying an unchanged failure blindly.

## State and evidence

Lifecycle state lives under the repository's Git common directory, not in the
tracked checkout. The important groups are:

| Location | Authority |
|---|---|
| `solo-ai/state.json` | slots, task identity, leases, direct transactions |
| `solo-ai/task-anchors/` and `root-anchors/` | active execution contracts and confirmed objectives |
| `solo-ai/state.json` host-root fields and `root-close-receipts/` | exact host locator, expected task binding, and minimal cross-repository close recovery |
| `solo-ai/proofs/` | validation evidence and logs |
| `solo-ai/candidate-batches.json` | immutable candidates, batches, and reusable workspace ownership |
| `solo-ai/runtime-adapter/` | content-addressed Adapter receipts |

These are recovery evidence, not a public API for direct editing. The CLI and
persisted Git facts decide lifecycle truth; status views are projections.

A retained terminal worktree is deliberately not reusable merely because its
old task is terminal. The explicit `reclaim-retained` maintenance path first
binds a reviewable deletion checklist to its recorded identity, then returns
only an unchanged, clean, detached test worktree. It preserves the terminal
task, branch, and receipts, so “integrated”, “retained for review”, and
“reusable” remain distinct states.
A diagnostic blocker report authorizes no deletion.

## Delivery and runtime are separate

A source candidate is published after its task finishes. It becomes delivered
only when its completed batch is contained in the current base. An optional
project runtime check may later determine whether that delivered source is
effective in a running environment. DWW records the result but never interprets
project-specific ports, databases, authentication, or deployment state.

## Design references

The [skill](../plugins/develop-with-worktrees/skills/develop-with-worktrees/SKILL.md)
routes operating procedures to a focused reference. Detailed policy lives in
[configuration](../plugins/develop-with-worktrees/skills/develop-with-worktrees/references/configuration.md),
task continuity in [task governance](../plugins/develop-with-worktrees/skills/develop-with-worktrees/references/task-governance.md),
and protection rules in [safety](../plugins/develop-with-worktrees/skills/develop-with-worktrees/references/safety.md).

The changelog records version-specific validation samples and historical policy
changes. They are evidence about those releases, not the source of the current
operating contract.
