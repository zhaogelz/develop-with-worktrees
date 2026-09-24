from __future__ import annotations

from pathlib import Path

from solo_ai.config import render_repo_config
from solo_ai.native_migration import preview_native_migration
from solo_ai.repo import GitRepo
from solo_ai.state import StateStore
from solo_ai.util import atomic_write_json


def _legacy_repo(root: Path) -> tuple[GitRepo, StateStore]:
    config = root / ".solo-ai"
    config.mkdir()
    (config / "config.toml").write_text(render_repo_config(), encoding="utf-8")
    repo = GitRepo(root)
    return repo, StateStore(repo)


def test_native_migration_preview_reports_active_legacy_task(git_repo: Path) -> None:
    repo, store = _legacy_repo(git_repo)
    state = store._empty()
    state["tasks"]["active-one"] = {"id": "active-one", "status": "active"}
    atomic_write_json(store.path, state)

    preview = preview_native_migration(repo, base_ref="main")

    assert preview["status"] == "blocked"
    assert {item["kind"] for item in preview["blockers"]} == {"active-legacy-task"}
    assert store.read()["schema_version"] == state["schema_version"]


def test_native_migration_preview_allows_empty_legacy_state(git_repo: Path) -> None:
    repo, store = _legacy_repo(git_repo)
    atomic_write_json(store.path, store._empty())

    preview = preview_native_migration(repo, base_ref="main")

    assert preview["status"] == "ready"
    assert preview["base_head"] == repo.head()
    assert preview["blockers"] == []
