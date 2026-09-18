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

## Core invariants

- A detected mature workflow has routing priority. DWW writes no managed state
  for `defer`; `delegated` requires an exact tracked contract and local approval.
- A managed task receives one isolated worktree and one local task anchor before
  it becomes writable. Identity uncertainty, dirty unexpected content, or a
  moved reference preserves the scene instead of adopting or deleting it.
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
