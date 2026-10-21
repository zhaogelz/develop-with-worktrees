from __future__ import annotations

from pathlib import Path

import pytest
from conftest import git
from solo_ai import lifecycle
from solo_ai.config import CommandSpec
from solo_ai.lifecycle import commit_task, finish, initialize, start
from solo_ai.repo import GitRepo
from solo_ai.state import LEGACY_STATE_SCHEMA, StateStore
from solo_ai.util import SoloAIError, atomic_write_json

VERIFY = CommandSpec(("git", "diff", "--check", "main...HEAD"))


def _initialize(path: Path) -> tuple[GitRepo, dict]:
    repo = GitRepo(path)
    result = initialize(
        repo, slots=1, commands=[VERIFY], accept=True, accept_static_only=False
    )
    return repo, result


def _commit_source(repo: GitRepo, name: str = "feature.txt") -> dict:
    task = start(repo, name="preserve existing local content")
    destination = Path(task["worktree"]) / name
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("reviewed source\n", encoding="utf-8")
    commit_task(
        repo,
        task_id=task["id"],
        lease=task["lease"],
        message="test: reviewed source",
        paths=[name],
    )
    return task


def _finish(repo: GitRepo, task: dict) -> dict:
    return finish(
        repo,
        task_id=task["id"],
        lease=task["lease"],
        cause="user",
        reason="deliver the requested local change",
    )


@pytest.mark.parametrize("bootstrap_schema", [1, 2])
def test_pending_bootstrap_finishes_with_unrelated_ignored_data(
    git_repo: Path, bootstrap_schema: int
) -> None:
    # 先留下真实的待合入初始化记录，再恢复源码；运行数据始终在原处。
    (git_repo / "README.md").write_text("unfinished work\n", encoding="utf-8")
    repo = GitRepo(git_repo)
    repo.add_local_exclude("/application-state/")
    data = git_repo / "application-state" / "database.bin"
    data.parent.mkdir()
    data.write_bytes(b"existing application data\x00\xff")
    repo, result = _initialize(git_repo)
    assert result["decision"] == "pending-primary-clean"
    if bootstrap_schema == 1:
        pending = lifecycle._bootstrap(repo)
        for key in (
            "target_ref",
            "target_head",
            "target_worktree",
            "target_worktree_resolved",
            "target_worktree_identity",
        ):
            pending.pop(key)
        pending["schema_version"] = 1
        atomic_write_json(repo.local_dir / "bootstrap.json", pending)
    task = _commit_source(repo)
    git(git_repo, "restore", "README.md")

    result = _finish(repo, task)

    assert (git_repo / "feature.txt").read_text(encoding="utf-8") == "reviewed source\n"
    assert data.read_bytes() == b"existing application data\x00\xff"
    assert not (repo.local_dir / "bootstrap.json").exists()
    assert result["delivered"] is True


@pytest.mark.parametrize(
    "local_path", ["AGENTS.md", ".solo-ai", ".solo-ai/verification.toml"]
)
def test_initial_bootstrap_collision_preserves_data_and_can_continue(
    git_repo: Path, local_path: str
) -> None:
    repo = GitRepo(git_repo)
    base = repo.head()
    repo.add_local_exclude("/AGENTS.md")
    repo.add_local_exclude("/.solo-ai")
    existing = git_repo / local_path
    existing.parent.mkdir(exist_ok=True)
    existing.write_bytes(b"original local content\x00\xff")

    with pytest.raises(SoloAIError, match="Bootstrap promotion failed"):
        _initialize(git_repo)

    assert repo.head() == base
    assert existing.read_bytes() == b"original local content\x00\xff"
    assert not (git_repo / ".solo-ai/config.toml").exists()
    pending = lifecycle._bootstrap(repo)
    assert pending["target_head"] == base
    assert (Path(pending["worktree"]) / ".solo-ai/config.toml").is_file()
    assert StateStore(repo).read()["schema_version"] == LEGACY_STATE_SCHEMA

    # 模拟内容所有者将冲突文件迁到自己选择的位置；DWW 本身不搬移文件。
    preserved = git_repo / "preserved-local.bin"
    repo.add_local_exclude("/preserved-local.bin")
    existing.rename(preserved)
    task = _commit_source(repo)
    assert _finish(repo, task)["delivered"] is True
    assert preserved.read_bytes() == b"original local content\x00\xff"


def test_pending_bootstrap_rechecks_new_ignored_collision(git_repo: Path) -> None:
    (git_repo / "README.md").write_text("unfinished work\n", encoding="utf-8")
    repo, _ = _initialize(git_repo)
    task = _commit_source(repo)
    git(git_repo, "restore", "README.md")
    base = repo.head()
    repo.add_local_exclude("/AGENTS.md")
    local = git_repo / "AGENTS.md"
    local.write_text("local instructions\n", encoding="utf-8")
    pending = (repo.local_dir / "bootstrap.json").read_bytes()

    with pytest.raises(SoloAIError, match="AGENTS.md"):
        _finish(repo, task)

    assert repo.head() == base
    assert local.read_text(encoding="utf-8") == "local instructions\n"
    assert (repo.local_dir / "bootstrap.json").read_bytes() == pending


@pytest.mark.parametrize("drift", ["head", "branch", "identity"])
def test_pending_bootstrap_ignored_data_does_not_relax_identity(
    git_repo: Path, drift: str
) -> None:
    (git_repo / "README.md").write_text("unfinished work\n", encoding="utf-8")
    repo, _ = _initialize(git_repo)
    task = _commit_source(repo)
    git(git_repo, "restore", "README.md")
    repo.add_local_exclude("/local.bin")
    local = git_repo / "local.bin"
    local.write_bytes(b"keep")
    if drift == "head":
        git(git_repo, "commit", "--allow-empty", "-m", "external target change")
    elif drift == "branch":
        git(git_repo, "switch", "-c", "other-target")
    else:
        pending = lifecycle._bootstrap(repo)
        pending["target_worktree_identity"]["inode"] += 1
        atomic_write_json(repo.local_dir / "bootstrap.json", pending)
    head = repo.head()

    with pytest.raises(SoloAIError, match="Recorded target"):
        _finish(repo, task)

    assert repo.head() == head
    assert local.read_bytes() == b"keep"
    assert (repo.local_dir / "bootstrap.json").is_file()
    assert not (git_repo / "feature.txt").exists()


@pytest.mark.parametrize("conflict", ["file", "identical", "parent-file"])
def test_native_promotion_preserves_an_ignored_destination(
    git_repo: Path, conflict: str
) -> None:
    repo, result = _initialize(git_repo)
    assert result["decision"] == "adopted"
    destination = (
        "feature.txt/child.txt" if conflict == "parent-file" else "feature.txt"
    )
    task = _commit_source(repo, destination)
    base = repo.head(git_repo)
    repo.add_local_exclude("/feature.txt")
    existing = git_repo / "feature.txt"
    content = (
        "reviewed source\n" if conflict == "identical" else "local application state\n"
    )
    existing.write_text(content, encoding="utf-8")

    with pytest.raises(SoloAIError, match="feature.txt"):
        _finish(repo, task)

    assert repo.head(git_repo) == base
    assert existing.read_text(encoding="utf-8") == content
    store = StateStore(repo)
    stored = store.task(task["id"])
    assert stored["native_delivery"]["delivery"] is None
    batch = store.native_batch(stored["native_delivery"]["batch_id"])
    attempt = batch["validation_attempt"]
    preserved = git_repo / "preserved-local.txt"
    repo.add_local_exclude("/preserved-local.txt")
    existing.rename(preserved)
    _finish(repo, task)
    assert (git_repo / destination).read_text(encoding="utf-8") == "reviewed source\n"
    assert preserved.read_text(encoding="utf-8") == content
    assert store.native_batch(batch["id"])["validation_attempt"] == attempt


@pytest.mark.parametrize("collision", [False, True])
def test_bootstrap_preserves_ignored_directory_links(
    git_repo: Path, tmp_path: Path, directory_link, collision: bool
) -> None:
    repo = GitRepo(git_repo)
    outside = tmp_path / "external-data"
    outside.mkdir()
    data = outside / "data.bin"
    data.write_bytes(b"external application data")
    name = ".solo-ai" if collision else "application-state"
    # 不加末尾斜杠，确保 POSIX symlink 和 Windows junction 都命中规则。
    repo.add_local_exclude(f"/{name}")
    directory_link(git_repo / name, outside)
    assert git(git_repo, "check-ignore", "--", name) == name
    base = repo.head()

    if collision:
        with pytest.raises(SoloAIError, match="Bootstrap promotion failed"):
            _initialize(git_repo)
        assert repo.head() == base
    else:
        _, result = _initialize(git_repo)
        assert result["decision"] == "adopted"
    assert sorted(item.name for item in outside.iterdir()) == ["data.bin"]
    assert data.read_bytes() == b"external application data"


def test_bootstrap_keeps_unrelated_files_inside_ignored_policy_directory(
    git_repo: Path,
) -> None:
    repo = GitRepo(git_repo)
    repo.add_local_exclude("/.solo-ai/")
    data = git_repo / ".solo-ai" / "local-notes.bin"
    data.parent.mkdir()
    data.write_bytes(b"local notes")

    _, result = _initialize(git_repo)

    assert result["decision"] == "adopted"
    assert data.read_bytes() == b"local notes"
    assert (git_repo / ".solo-ai/config.toml").is_file()


@pytest.mark.parametrize("mode", ["direct", "batched"])
def test_legacy_promotion_preserves_ignored_destination(
    git_repo: Path, mode: str
) -> None:
    from solo_ai.config import load_verification_config
    from solo_ai.lifecycle import approve, ready

    repo, _ = _initialize(git_repo)
    StateStore(repo).mutate(
        lambda state: state.update(schema_version=LEGACY_STATE_SCHEMA)
    )
    config = git_repo / ".solo-ai/config.toml"
    text = config.read_text(encoding="utf-8")
    if mode == "direct":
        line = next(
            line for line in text.splitlines() if line.startswith("integration = ")
        )
        text = text.replace(
            line,
            'integration = { mode = "direct", worktree_mode = "dedicated", batch_size = 5, candidate_capacity = 10, seal_policy = "explicit", tail_policy = "explicit", tail_quiet_seconds = 90 }',
        )
    config.write_text(text, encoding="utf-8")
    git(git_repo, "add", ".solo-ai/config.toml")
    git(git_repo, "commit", "--allow-empty", "-m", "test: legacy integration mode")
    approve(repo, load_verification_config(repo))
    task = _commit_source(repo)
    if mode == "direct":
        ready(repo, task_id=task["id"], lease=task["lease"])
    repo.add_local_exclude("/feature.txt")
    existing = git_repo / "feature.txt"
    existing.write_bytes(b"local data")
    base = repo.head()

    with pytest.raises(SoloAIError, match="feature.txt"):
        if mode == "direct":
            finish(repo, task_id=task["id"], lease=task["lease"])
        else:
            _finish(repo, task)

    assert repo.head() == base
    assert existing.read_bytes() == b"local data"


def test_fast_forward_allows_tracked_file_to_become_directory(git_repo: Path) -> None:
    repo = GitRepo(git_repo)
    base = repo.head()
    git(git_repo, "switch", "-c", "incoming")
    (git_repo / "README.md").unlink()
    (git_repo / "README.md").mkdir()
    (git_repo / "README.md" / "child.txt").write_text("new content", encoding="utf-8")
    git(git_repo, "add", "--all")
    git(git_repo, "commit", "-m", "test: replace tracked file with directory")
    incoming = repo.head()
    git(git_repo, "switch", "main")

    repo.fast_forward(incoming, cwd=git_repo)

    assert repo.head() != base
    assert (git_repo / "README.md" / "child.txt").read_text(
        encoding="utf-8"
    ) == "new content"


def test_fast_forward_preserves_ignored_child_when_replacing_directory(
    git_repo: Path,
) -> None:
    repo = GitRepo(git_repo)
    directory = git_repo / "folder"
    directory.mkdir()
    (directory / "tracked.txt").write_text("old source", encoding="utf-8")
    git(git_repo, "add", "folder/tracked.txt")
    git(git_repo, "commit", "-m", "test: tracked directory")
    base = repo.head()
    git(git_repo, "switch", "-c", "incoming")
    (directory / "tracked.txt").unlink()
    directory.rmdir()
    directory.write_text("new source", encoding="utf-8")
    git(git_repo, "add", "--all")
    git(git_repo, "commit", "-m", "test: replace directory")
    incoming = repo.head()
    git(git_repo, "switch", "main")
    repo.add_local_exclude("/folder/local.bin")
    data = directory / "local.bin"
    data.write_bytes(b"original")

    with pytest.raises(SoloAIError, match="folder"):
        repo.fast_forward(incoming, cwd=git_repo)

    assert repo.head() == base
    assert data.read_bytes() == b"original"
    assert (directory / "tracked.txt").read_text(encoding="utf-8") == "old source"
