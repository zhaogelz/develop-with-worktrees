from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..config import load_repo_config
from ..delegated import DelegatedContractError, invoke_delegated
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
    """成熟仓库只有在精确契约获批后才暴露给通用编排器。"""

    name: str = "delegated"

    def assert_available(self, repo: GitRepo) -> None:
        route = repository_route(repo)
        if route["action"] != "delegated":
            raise SoloAIError(
                "The delegated lifecycle adapter requires a valid, locally approved repository contract"
            )
        if "status" not in route["adapter"]["capabilities"]:
            raise SoloAIError(
                "The delegated lifecycle adapter must declare status before orchestration can query capacity"
            )

    def available_slots(self, repo: GitRepo, *, batch_limit: int) -> int:
        route = repository_route(repo)
        if route["action"] != "delegated":
            self.assert_available(repo)
        try:
            response = invoke_delegated(
                repo.root,
                repo.common_dir,
                operation="status",
                request={},
                timeout_seconds=30,
            )
        except DelegatedContractError as exc:
            raise SoloAIError(f"Delegated lifecycle status failed: {exc}") from exc
        return min(
            batch_limit,
            int(route["adapter"]["max_parallel"]),
            int(response["result"]["available_slots"]),
        )


def adapter_for(name: str) -> LifecycleAdapter:
    if name == "dww":
        return DwwLifecycleAdapter()
    if name == "delegated":
        return DelegatedLifecycleAdapter()
    raise SoloAIError(f"Unknown lifecycle adapter: {name}")
