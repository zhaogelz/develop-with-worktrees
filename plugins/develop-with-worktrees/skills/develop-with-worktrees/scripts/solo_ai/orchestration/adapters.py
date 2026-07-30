from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..config import load_repo_config
from ..lifecycle import repository_route
from ..repo import GitRepo
from ..state import StateStore
from ..util import SoloAIError


class LifecycleAdapter(Protocol):
    """编排器只依赖这个最小边界，不复制仓库的生命周期实现。"""

    name: str

    def assert_available(self, repo: GitRepo) -> None: ...

    def available_slots(self, repo: GitRepo, *, batch_limit: int) -> int: ...


@dataclass(frozen=True)
class DwwLifecycleAdapter:
    name: str = "dww"

    def assert_available(self, repo: GitRepo) -> None:
        if repository_route(repo)["action"] != "managed":
            raise SoloAIError(
                "The DWW lifecycle adapter requires a managed repository; use an explicit delegated adapter for a mature repository"
            )

    def available_slots(self, repo: GitRepo, *, batch_limit: int) -> int:
        config = load_repo_config(repo, cwd=repo.policy_path())
        idle = sum(
            1
            for slot in StateStore(repo).read().get("slots", {}).values()
            if slot.get("status") == "idle" and int(slot.get("id", "0")) <= config.slots
        )
        return min(batch_limit, idle)


@dataclass(frozen=True)
class DelegatedLifecycleAdapter:
    """成熟仓库必须显式选用；它绝不尝试猜测或执行外部工作流。"""

    name: str = "delegated"

    def assert_available(self, repo: GitRepo) -> None:
        del repo

    def available_slots(self, repo: GitRepo, *, batch_limit: int) -> int:
        del repo
        return batch_limit


def adapter_for(name: str) -> LifecycleAdapter:
    if name == "dww":
        return DwwLifecycleAdapter()
    if name == "delegated":
        return DelegatedLifecycleAdapter()
    raise SoloAIError(f"Unknown lifecycle adapter: {name}")
