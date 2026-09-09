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


def test_dependency_inventory_bounds_repeated_path_resolution(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真实目录清点的系统路径解析次数应与条目数线性且不重复倍增。"""
    repo = ignore(git_repo, "node_modules/")
    directory = git_repo / "node_modules" / "package" / "lib" / "nested"
    directory.mkdir(parents=True)
    files = [directory / f"generated-{index}.js" for index in range(60)]
    for file in files:
        file.write_bytes(b"generated")
    original = Path.resolve
    resolutions = 0

    def counted(path, *args, **kwargs):
        nonlocal resolutions
        resolutions += 1
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", counted)
    inventory = cleanup.inspect_untracked(repo, cwd=git_repo, expand_dependencies=True)
    assert inventory["retained"] == sorted(
        file.relative_to(git_repo).as_posix() for file in files
    )
    assert resolutions <= 4 * len(files) + 40, resolutions


def test_physical_dependency_cleanup_does_not_hash_file_contents(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """已知可重建依赖按对象/变更元数据删除，开放输出仍保留内容证明。"""
    repo = ignore(git_repo, "node_modules/\n.tmp/")
    dependency = git_repo / "node_modules" / "package" / "large.js"
    dependency.parent.mkdir(parents=True)
    dependency.write_bytes(b"generated" * 1024 * 256)
    output = git_repo / ".tmp" / "result.log"
    output.parent.mkdir()
    output.write_text("known output", encoding="utf-8")
    hashed = []
    original = util.sha256_file

    def reject_dependency_hash(path):
        assert not Path(path).is_relative_to(git_repo / "node_modules")
        hashed.append(Path(path))
        return original(path)

    monkeypatch.setattr(util, "sha256_file", reject_dependency_hash)
    cleanup.remove_recreatable_ignored(repo, cwd=git_repo)
    assert output in hashed
    assert not dependency.exists()
    assert not output.exists()


def test_physical_cleanup_expands_dependencies_once(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """身份冻结后按精确条目条件删除，不再重复全树清点。"""
    repo = ignore(git_repo, "node_modules/")
    root = git_repo / "node_modules" / "package"
    root.mkdir(parents=True)
    for index in range(40):
        (root / f"generated-{index}.js").write_text("generated", encoding="utf-8")
    original = cleanup.inspect_untracked
    expanded = 0

    def counted(*args, **kwargs):
        nonlocal expanded
        expanded += int(kwargs.get("expand_dependencies", False))
        return original(*args, **kwargs)

    monkeypatch.setattr(cleanup, "inspect_untracked", counted)
    cleanup.remove_recreatable_ignored(repo, cwd=git_repo)
    assert expanded == 1
    assert not root.exists()


def extended_test_path(path: Path) -> Path:
    # 测试准备独立使用 Windows API 路径，不能用待测实现生成反例。
    return Path("\\\\?\\" + str(path.absolute()))


@pytest.mark.skipif(os.name != "nt", reason="Windows MAX_PATH 真实回归")
def test_long_dependency_paths_are_fully_inspected_and_safely_removed(
    git_repo: Path,
) -> None:
    repo = ignore(git_repo, "node_modules/")
    directory = git_repo / "node_modules"
    while len(str(directory)) < 310:
        directory /= "nested-dependency-0123456789"
    native = extended_test_path(directory)
    native.mkdir(parents=True)
    file = directory / "generated.js"
    extended_test_path(file).write_bytes(b"generated")
    assert snapshot_plain_path(file)["kind"] == "file"
    inventory = cleanup.inspect_untracked(repo, cwd=git_repo, expand_dependencies=True)
    assert inventory["retained"] == [file.relative_to(git_repo).as_posix()]
    cleanup.remove_recreatable_ignored(repo, cwd=git_repo)
    assert not (git_repo / "node_modules").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows 深路径 junction 真实回归")
def test_long_dependency_junction_is_only_removed_as_a_link(
    git_repo: Path,
    tmp_path: Path,
    directory_link,
) -> None:
    repo = ignore(git_repo, "node_modules/")
    directory = git_repo / "node_modules"
    while len(str(directory)) < 310:
        directory /= "nested-dependency-0123456789"
    target = tmp_path / "preserved-source"
    target.mkdir()
    marker = target / "state.db"
    marker.write_bytes(b"preserve")
    link = directory / "package"
    directory_link(extended_test_path(link), target)
    assert is_link_or_junction(link)
    inventory = cleanup.inspect_untracked(repo, cwd=git_repo, expand_dependencies=True)
    assert inventory["retained"] == [link.relative_to(git_repo).as_posix()]
    cleanup.remove_recreatable_ignored(repo, cwd=git_repo)
    assert not (git_repo / "node_modules").exists()
    assert marker.read_bytes() == b"preserve"


@pytest.mark.skipif(os.name != "nt", reason="Windows 长路径保护边界")
def test_long_protected_output_is_not_deleted(git_repo: Path) -> None:
    repo = ignore(git_repo, ".tmp/")
    directory = git_repo / ".tmp"
    while len(str(directory)) < 310:
        directory /= "nested-output-0123456789"
    native = extended_test_path(directory)
    native.mkdir(parents=True)
    marker = native / ".env"
    marker.write_bytes(b"preserve")
    with pytest.raises(SoloAIError, match="Protected or unknown"):
        cleanup.remove_recreatable_ignored(repo, cwd=git_repo)
    assert marker.read_bytes() == b"preserve"


@pytest.mark.skipif(os.name != "nt", reason="Windows 长路径对象身份")
def test_long_file_replacement_keeps_both_objects(tmp_path: Path) -> None:
    directory = tmp_path
    while len(str(directory)) < 310:
        directory /= "nested-output-0123456789"
    native = extended_test_path(directory)
    native.mkdir(parents=True)
    file = directory / "generated.js"
    access = extended_test_path(file)
    access.write_bytes(b"original")
    expected = snapshot_plain_path(file)
    preserved = access.with_suffix(".original")
    access.rename(preserved)
    access.write_bytes(b"replacement")
    with pytest.raises(SoloAIError, match="changed before deletion"):
        util.delete_plain_path_if_unchanged(file, expected)
    assert access.read_bytes() == b"replacement"
    assert preserved.read_bytes() == b"original"


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
    original_snapshot = cleanup.snapshot_recreatable_file
    replaced = False

    def replace_after_snapshot(path):
        nonlocal replaced
        snapshot = original_snapshot(path)
        if path.name == "original.js" and not replaced:
            replaced = True
            root.rename(git_repo / "preserved-original-dependencies")
            directory_link(root, target)
        return snapshot

    monkeypatch.setattr(cleanup, "snapshot_recreatable_file", replace_after_snapshot)
    with pytest.raises(SoloAIError, match="changed before deletion|link or junction"):
        cleanup.remove_recreatable_ignored(repo, cwd=git_repo)
    assert marker.read_text(encoding="utf-8") == "preserved"
    assert (git_repo / "preserved-original-dependencies" / "original.js").is_file()


def test_plain_path_check_keeps_its_original_root_boundary(
    tmp_path: Path, directory_link, monkeypatch: pytest.MonkeyPatch
) -> None:
    """检查中根被换成链接时，不能再次解析根并接受新的外部边界。"""
    root = tmp_path / "managed"
    root.mkdir()
    file = root / "generated.js"
    file.write_bytes(b"original")
    target = tmp_path / "external"
    target.mkdir()
    marker = target / file.name
    marker.write_bytes(b"preserved")
    original_resolve = Path.resolve
    replaced = False

    def replace_before_resolve(path, *args, **kwargs):
        nonlocal replaced
        if path == file and not replaced:
            replaced = True
            root.rename(tmp_path / "original-root")
            directory_link(root, target)
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", replace_before_resolve)
    with pytest.raises(SoloAIError, match="outside managed root"):
        cleanup._require_plain_path(file, root)
    assert marker.read_bytes() == b"preserved"
    assert (tmp_path / "original-root" / file.name).read_bytes() == b"original"


def test_inventory_rejects_linked_ancestor_before_inspecting_leaf(
    git_repo: Path, tmp_path: Path, directory_link, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "external"
    target.mkdir()
    marker = target / "generated.js"
    marker.write_bytes(b"preserved")
    ancestor = git_repo / "node_modules" / "package"
    directory_link(ancestor, target)
    leaf = ancestor / marker.name
    original = cleanup.is_link_or_junction

    def reject_target_access(path):
        if path == leaf:
            raise AssertionError("祖先链接未拒绝前，不得读取目标后代的元数据")
        return original(path)

    monkeypatch.setattr(cleanup, "is_link_or_junction", reject_target_access)
    with pytest.raises(SoloAIError, match="link or junction"):
        cleanup._require_inventory_path(leaf, git_repo, ignored=True)
    assert marker.read_bytes() == b"preserved"


@pytest.mark.parametrize("change", ["same-size-write", "replacement"])
def test_metadata_cleanup_preserves_changed_dependency(
    tmp_path: Path, change: str
) -> None:
    """恢复原mtime或同大小也不能掩盖对象替换/写入；无需读取内容做SHA。"""
    file = tmp_path / "dependency.js"
    file.write_bytes(b"original")
    before = file.stat()
    expected = util.snapshot_recreatable_file(file)
    assert "sha256" not in expected
    if change == "replacement":
        file.rename(tmp_path / "original-preserved.js")
    file.write_bytes(b"replaced")
    os.utime(file, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(SoloAIError, match="changed before deletion"):
        util.delete_plain_path_if_unchanged(file, expected)
    assert file.read_bytes() == b"replaced"
    if change == "replacement":
        assert (tmp_path / "original-preserved.js").read_bytes() == b"original"


@pytest.mark.skipif(os.name != "nt", reason="Windows真实目录与文件占用保护")
def test_cleanup_holds_ancestors_and_file_against_rename(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = ignore(git_repo, "node_modules/")
    root = git_repo / "node_modules"
    file = root / "package" / "generated.js"
    file.parent.mkdir(parents=True)
    file.write_bytes(b"generated")
    mark = util._mark_windows_handle_for_deletion
    checked = False

    def check_then_mark(handle):
        nonlocal checked
        if not checked:
            checked = True
            for path in (git_repo, root, file.parent, file):
                with pytest.raises(PermissionError):
                    path.rename(path.with_name(path.name + "-moved"))
        return mark(handle)

    monkeypatch.setattr(util, "_mark_windows_handle_for_deletion", check_then_mark)
    cleanup.remove_recreatable_ignored(repo, cwd=git_repo)
    assert checked
    assert not root.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows已打开文件必须失败关闭")
def test_cleanup_preserves_occupied_dependency(git_repo: Path) -> None:
    repo = ignore(git_repo, "node_modules/")
    file = git_repo / "node_modules" / "generated.js"
    file.parent.mkdir()
    file.write_bytes(b"generated")
    with file.open("rb") as held:
        with pytest.raises(SoloAIError, match="busy|changed"):
            cleanup.remove_recreatable_ignored(repo, cwd=git_repo)
        assert held.read() == b"generated"
    assert file.read_bytes() == b"generated"
    cleanup.remove_recreatable_ignored(repo, cwd=git_repo)
    assert not file.exists()


def test_replacement_empty_directory_is_not_adopted_for_deletion(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = ignore(git_repo, "node_modules/")
    root = git_repo / "node_modules"
    root.mkdir()
    original_snapshot = cleanup.snapshot_plain_path
    replaced = False

    def replace_empty_directory(path):
        nonlocal replaced
        expected = original_snapshot(path)
        if path == root and not replaced:
            replaced = True
            root.rename(git_repo / "original-dependencies")
            root.mkdir()
        return expected

    monkeypatch.setattr(cleanup, "snapshot_plain_path", replace_empty_directory)
    with pytest.raises(SoloAIError, match="changed before deletion"):
        cleanup.remove_recreatable_ignored(repo, cwd=git_repo)
    assert root.exists()
    assert (git_repo / "original-dependencies").exists()


def test_recreatable_file_snapshot_and_delete_do_not_construct_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file = tmp_path / "dependency.js"
    file.write_bytes(b"generated data")

    def reject_hash(*args, **kwargs):
        pytest.fail("依赖对象快照和条件删除不得读取内容做SHA")

    monkeypatch.setattr(util.hashlib, "sha256", reject_hash)
    expected = util.snapshot_recreatable_file(file)
    util.delete_plain_path_if_unchanged(file, expected)
    assert not file.exists()
