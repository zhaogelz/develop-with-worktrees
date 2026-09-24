from __future__ import annotations

"""DWW 对外顶层命令的唯一契约。

该模块不能引入第三方依赖：CLI 和工作树 Hook 都会在受限环境中导入它。
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
        "delegated",
        "orchestrate",
        "candidate",
        "batch",
        "host-handoff",
        "start",
        "commit",
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
