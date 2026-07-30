<!-- develop-with-worktrees:managed:start -->
## Isolated coding tasks

For every task that may modify repository files, use the installed `develop-with-worktrees` skill before editing. Run `start`, work only in the returned worktree, stage an exact reviewed path list with `commit`, then run `ready` and `finish`. Read-only analysis does not claim a slot. Do not bypass a failed gate. The DWW lifecycle is local-only and must not fetch, pull, push, create PRs, rebase, squash, amend, or rewrite history. After a successful Finish, an explicit user request may be fulfilled with an ordinary non-force push of the current branch from the clean base worktree; that publishing step is separate from DWW.
<!-- develop-with-worktrees:managed:end -->
