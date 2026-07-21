<!-- develop-with-worktrees:managed:start -->
## Isolated coding tasks

For every task that may modify repository files, use the installed `develop-with-worktrees` skill before editing. Run `start`, work only in the returned worktree, stage an exact reviewed path list with `commit`, then run `ready` and `finish`. Read-only analysis does not claim a slot. Do not bypass a failed gate. This repository's policy is local-only: do not fetch, pull, push, create PRs, rebase, squash, amend, or rewrite history through this lifecycle.
<!-- develop-with-worktrees:managed:end -->
