from __future__ import annotations

"""DWW 对外顶层命令的唯一契约。

该模块由 CLI 在受限环境中导入，保持无第三方依赖。
"""

TOP_LEVEL_COMMANDS = frozenset(
    {
        "version",
        "init",
        "choose",
        "approve",
        "disable",
        "enable",
        "settings",
        "doctor",
        "route",
        "migration",
        "delegated",
        "orchestrate",
        "candidate",
        "batch",
        "host-handoff",
        "start",
        "commit",
        "merge-source",
        "ready",
        "ready-withdraw",
        "finish",
        "retarget",
        "plan",
        "verify",
        "status",
        "recover",
        "abandon",
        "reclaim-retained",
        "resume-in-place",
        "warm-slot",
        "dev",
        "prune-proofs",
        "prune-logs",
        "prune-slot",
        "deinit",
        "anchor",
        "root-anchor",
        "handoff",
        "runtime",
    }
)
