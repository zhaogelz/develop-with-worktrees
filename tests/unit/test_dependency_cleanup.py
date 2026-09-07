from pathlib import Path

import os
import pytest
from conftest import git
from solo_ai import cleanup, util
from solo_ai.repo import GitRepo
from solo_ai.util import (
    SoloAIError,
    delete_link_path_if_unchanged,
    is_link_or_junction,
    snapshot_link_path,
    snapshot_plain_path,
)


def ignore(root: Path, pattern: str) -> GitRepo:
    (root / ".gitignore").write_text(pattern + "\n", encoding="utf-8")
    git(root, "add", ".gitignore")
    git(root, "commit", "-m", "test: declare ignored outputs")
    return GitRepo(root)


def test_retaining_dependency_root_does_not_walk_it(
    git_repo: Path, directory_link, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = ignore(git_repo, "node_modules/")
    directory_link(git_repo / "node_modules" / "loop", git_repo)
    original = Path.iterdir

    def guarded(path):
        if "node_modules" in path.parts:
            raise AssertionError("保留依赖时不应进入依赖目录或链接目标")
        return original(path)

    monkeypatch.setattr(Path, "iterdir", guarded)
    inventory = cleanup.inspect_untracked(repo, cwd=git_repo)
    assert inventory["retained"] == ["node_modules"]


def test_dependency_inventory_and_delete_never_walk_link_target(
    git_repo: Path, tmp_path: Path, directory_link, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = ignore(git_repo, "node_modules/")
    target = tmp_path / "source"
    target.mkdir()
    marker = target / "state.db"
    marker.write_bytes(b"preserve")
    link = git_repo / "node_modules" / "package"
    directory_link(link, target)
    original = Path.iterdir

    def guarded(path):
        if (
            path == link
            or path == target
            or link in path.parents
            or target in path.parents
        ):
            raise AssertionError("不得枚举链接目标")
        return original(path)

    monkeypatch.setattr(Path, "iterdir", guarded)
    inventory = cleanup.inspect_untracked(repo, cwd=git_repo, expand_dependencies=True)
    assert inventory["retained"] == ["node_modules/package"]
    cleanup.remove_recreatable_ignored(repo, cwd=git_repo)
    assert not link.exists()
    assert not (git_repo / "node_modules").exists()
    assert marker.read_bytes() == b"preserve"


@pytest.mark.parametrize("relative", ["node_modules", ".tmp/link", "unknown/link"])
def test_link_at_dependency_root_or_outside_dependencies_remains_blocked(
    git_repo: Path, tmp_path: Path, directory_link, relative: str
) -> None:
    repo = ignore(git_repo, relative.split("/")[0] + "/")
    target = tmp_path / "preserved"
    target.mkdir()
    marker = target / "marker"
    marker.write_text("safe", encoding="utf-8")
    link = git_repo / relative
    directory_link(link, target)
    for expanded in (False, True):
        with pytest.raises(SoloAIError, match="link or junction"):
            cleanup.inspect_untracked(repo, cwd=git_repo, expand_dependencies=expanded)
    assert is_link_or_junction(link)
    assert marker.read_text(encoding="utf-8") == "safe"


def test_protected_parent_cannot_be_hidden_by_nested_dependency_name(
    git_repo: Path,
) -> None:
    repo = ignore(git_repo, "storage/")
    nested = git_repo / "storage" / "node_modules" / "record"
    nested.parent.mkdir(parents=True)
    nested.write_text("preserved", encoding="utf-8")
    with pytest.raises(SoloAIError, match="Protected or unknown"):
        cleanup.remove_recreatable_ignored(repo, cwd=git_repo)
    assert nested.read_text(encoding="utf-8") == "preserved"


def test_link_object_replacement_is_not_deleted(tmp_path: Path, directory_link) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    link = tmp_path / "link"
    directory_link(link, first)
    expected = snapshot_link_path(link)
    # 只删除这个临时链接对象，用不同对象模拟并发替换。
    assert is_link_or_junction(link)
    if os.name == "nt":
        link.rmdir()
    else:
        link.unlink()
    directory_link(link, second)
    with pytest.raises(SoloAIError, match="changed"):
        delete_link_path_if_unchanged(link, expected)
    assert is_link_or_junction(link)
    assert first.is_dir() and second.is_dir()


def test_plain_snapshot_still_rejects_junction(tmp_path: Path, directory_link) -> None:
    target = tmp_path / "source"
    target.mkdir()
    link = tmp_path / "link"
    directory_link(link, target)
    with pytest.raises(SoloAIError, match="Refusing to snapshot a link"):
        snapshot_plain_path(link)


@pytest.mark.skipif(os.name != "nt", reason="Windows 同对象句柄保护")
def test_open_link_cannot_be_replaced_before_handle_deletion(
    tmp_path: Path, directory_link, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "source"
    target.mkdir()
    marker = target / "marker"
    marker.write_text("preserved", encoding="utf-8")
    link = tmp_path / "link"
    directory_link(link, target)
    expected = snapshot_link_path(link)
    original = util._mark_windows_handle_for_deletion

    def attempt_replacement(handle):
        with pytest.raises(PermissionError):
            link.rename(tmp_path / "moved-link")
        return original(handle)

    monkeypatch.setattr(util, "_mark_windows_handle_for_deletion", attempt_replacement)
    delete_link_path_if_unchanged(link, expected)
    assert not link.exists()
    assert marker.read_text(encoding="utf-8") == "preserved"


@pytest.mark.skipif(os.name != "nt", reason="Windows 未知 reparse 类型")
def test_unknown_reparse_tag_is_rejected_before_deletion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ctypes

    def return_unknown_tag(
        handle, code, source, source_length, output, capacity, length, pending
    ):
        data = (0xA000001D).to_bytes(4, "little") + b"\0" * 4
        ctypes.memmove(output, data, len(data))
        length._obj.value = len(data)
        return 1

    monkeypatch.setattr(ctypes.windll.kernel32, "DeviceIoControl", return_unknown_tag)
    with pytest.raises(SoloAIError, match="Unknown dependency reparse type"):
        util._windows_link_snapshot(0, mode=0)


def test_late_link_is_not_added_to_the_frozen_deletion_inventory(
    git_repo: Path, tmp_path: Path, directory_link, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = ignore(git_repo, "node_modules/")
    dependency = git_repo / "node_modules" / "initial.js"
    dependency.parent.mkdir()
    dependency.write_text("generated", encoding="utf-8")
    target = tmp_path / "source"
    target.mkdir()
    marker = target / "marker"
    marker.write_text("preserved", encoding="utf-8")
    link = dependency.parent / "late-link"
    original_delete = cleanup.delete_plain_path_if_unchanged

    def insert_then_delete(path, expected):
        if path == dependency:
            directory_link(link, target)
        return original_delete(path, expected)

    monkeypatch.setattr(cleanup, "delete_plain_path_if_unchanged", insert_then_delete)
    with pytest.raises(SoloAIError, match="changed during"):
        cleanup.remove_recreatable_ignored(repo, cwd=git_repo)
    assert is_link_or_junction(link)
    assert marker.read_text(encoding="utf-8") == "preserved"


def test_dependency_root_replacement_stops_deletion(
    git_repo: Path, tmp_path: Path, directory_link, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = ignore(git_repo, "node_modules/")
    root = git_repo / "node_modules"
    root.mkdir()
    (root / "original.js").write_text("generated", encoding="utf-8")
    target = tmp_path / "external"
    target.mkdir()
    marker = target / "original.js"
    marker.write_text("preserved", encoding="utf-8")
    original_snapshot = cleanup.snapshot_plain_path
    replaced = False

    def replace_after_snapshot(path):
        nonlocal replaced
        snapshot = original_snapshot(path)
        if path.name == "original.js" and not replaced:
            replaced = True
            root.rename(git_repo / "preserved-original-dependencies")
            directory_link(root, target)
        return snapshot

    monkeypatch.setattr(cleanup, "snapshot_plain_path", replace_after_snapshot)
    with pytest.raises(SoloAIError, match="link or junction"):
        cleanup.remove_recreatable_ignored(repo, cwd=git_repo)
    assert marker.read_text(encoding="utf-8") == "preserved"
    assert (git_repo / "preserved-original-dependencies" / "original.js").is_file()
