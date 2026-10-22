# Develop with Worktrees

## More AI tasks. Less cleaning up after them.

You ask one AI to build a page and another to fix an API. Both finish, but both touched the same files. Now you have to untangle what changed, what to keep, and whether it still works together.

Develop with Worktrees (DWW) is a Codex plugin that **gives each modifying task its own working directory, checks the combined changes, and merges them back into your local branch when they pass.**

[中文](README.zh-CN.md) · [Install](#install) · [See it in 36 seconds](#see-it-in-36-seconds) · [How to use it](#once-its-installed)

## A few things you can stop juggling

| The hassle | How DWW helps |
| --- | --- |
| Two AI tasks overwrite files while working | Each task edits in its own directory, away from the others' working files |
| Each task works alone, but the combined result breaks | A batch is merged and checked using your project's configuration before your local branch moves |
| A chat stops and you have to explain the task again | Local task notes keep the objective, scope, and progress available for continuation |
| A merge or check fails and you worry about losing work | Changes and failure details stay available for repair; the base stays unchanged until integration succeeds |

Use it when you run several Codex chats, build a feature alongside a bug fix, or work in a small team sharing a repository. It works for a single task, too.

## See it in 36 seconds

[![Watch the DWW promo: work separately, check together, then merge back](https://raw.githubusercontent.com/zhaogelz/develop-with-worktrees/c192f33d510e4dce713616497d93eae91753b47e/videos/develop-with-worktrees-promo/assets/overview.svg)](https://cdn.jsdelivr.net/gh/zhaogelz/develop-with-worktrees@2fbcd39b6dd9513bad7696303b4875954caad1b5/docs/media/develop-with-worktrees-promo-36s.mp4)

Click the cover to watch the 36-second video: one project, three tasks, working separately and getting checked together. Chinese on-screen text, no narration. [Download MP4](https://github.com/zhaogelz/develop-with-worktrees/raw/2fbcd39b6dd9513bad7696303b4875954caad1b5/docs/media/develop-with-worktrees-promo-36s.mp4) · [Video source](videos/develop-with-worktrees-promo/README.md).

## Install

You need Codex with plugin support, Git, and [uv](https://docs.astral.sh/uv/) to run the Python tools. The current public version is `0.5.0-beta.10`.

```text
codex plugin marketplace add zhaogelz/develop-with-worktrees --ref v0.5.0-beta.10
codex plugin add develop-with-worktrees@develop-with-worktrees
```

After installation, let Codex load the plugin skill. Review a new or changed Hook definition if the host prompts you to do so.

## Once it's installed

1. Open your Git project in Codex and ask for a change as usual, such as “Add phone-number login to the sign-in page.”
2. On first use, choose a separate directory for each task when prompted. Repositories with an existing workflow keep that workflow first.
3. The AI edits and checks its isolated copy, then commits only the reviewed files. You can assign another task in a separate chat.
4. The AI follows local integration: eligible changes are checked together, then merged into the local branch recorded when the task started. It continues with other eligible batches already in the round.

You describe the goal, Codex moves the task forward, and DWW manages how the changes enter and leave the repository. You don't have to create, switch, and reclaim working directories by hand each time.

## A few things to know

- **Finished editing and merged are separate states.** Changes may be waiting for batch integration. Use the actual local merge result to judge delivery. See the [delivery flow](plugins/develop-with-worktrees/skills/develop-with-worktrees/references/native-delivery.md) for the conditions.
- Separate directories prevent tasks from overwriting each other's working files. Merge conflicts can still happen. DWW stops and preserves the work so the AI can repair it within scope; product choices come back to you.
- Existing ignored data and dependencies can stay in place. DWW stops when incoming files would overwrite local content or traverse a local directory link.
- The optional edit guard directs native file patches to the task's directory. Reading and searching work even when files have uncommitted changes. Terminal commands and scripts remain subject to the host's filesystem permissions; DWW does not inspect their writes.
- Your project decides which checks run. DWW runs them and keeps their results; their coverage still depends on your project.
- DWW manages local Git work. Remote pushes, pull requests, and deployment are separate actions. Codex or another host still handles task planning and AI coordination.

## Find a detail when you need it

| You want to… | Start here |
| --- | --- |
| Follow the complete task workflow | [Plugin skill](plugins/develop-with-worktrees/skills/develop-with-worktrees/SKILL.md) |
| Configure checks and batches | [Configuration](plugins/develop-with-worktrees/skills/develop-with-worktrees/references/configuration.md) |
| Resume interrupted or failed work | [Recovery guide](plugins/develop-with-worktrees/skills/develop-with-worktrees/references/recovery.md) |
| Understand the design or contribute | [Architecture](docs/architecture.md) / [Development guide](docs/development.md) |
| See what changed between versions | [Changelog](CHANGELOG.md) |
