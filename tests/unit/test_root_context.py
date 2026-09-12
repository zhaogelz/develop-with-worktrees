from __future__ import annotations

from pathlib import Path

import pytest

from solo_ai.repo import GitRepo
from solo_ai.root_context import (
    create_root_anchor,
    root_anchor_path,
    show_root_anchor,
    update_root_anchor,
)
from solo_ai.util import SoloAIError


def _create(repo: GitRepo) -> dict[str, object]:
    return create_root_anchor(
        repo,
        root_id="root-20260912000000-test",
        purpose="durable objective",
        target="root anchor tests",
        base_ref="main",
        base_head=repo.head(repo.root),
        scope="local root facts only",
        acceptance="unit tests pass",
    )


def test_root_anchor_round_trip_and_immutable_contract(git_repo: Path) -> None:
    repo = GitRepo(git_repo)
    created = _create(repo)
    assert created["root_id"] == "root-20260912000000-test"
    changed = str(created["content"]).replace(
        "- Current progress: root anchor created",
        "- Current progress: coordinator resumed",
    )
    update = git_repo / "root-update.md"
    update.write_text(changed, encoding="utf-8", newline="\n")
    result = update_root_anchor(
        repo,
        root_id="root-20260912000000-test",
        input_path=update,
        expected_sha256=str(created["sha256"]),
    )
    assert result["changed"] is True
    assert (
        "coordinator resumed"
        in show_root_anchor(repo, root_id="root-20260912000000-test")["content"]
    )

    invalid = changed.replace(
        "- Original purpose: durable objective", "- Original purpose: forged"
    )
    update.write_text(invalid, encoding="utf-8", newline="\n")
    with pytest.raises(SoloAIError, match="cannot be changed"):
        update_root_anchor(
            repo,
            root_id="root-20260912000000-test",
            input_path=update,
            expected_sha256=str(result["sha256"]),
        )


def test_root_anchor_rejects_path_like_identity(git_repo: Path) -> None:
    repo = GitRepo(git_repo)
    with pytest.raises(SoloAIError, match="not safe"):
        root_anchor_path(repo, "../root")


def test_root_anchor_update_rejects_manual_child_registry(git_repo: Path) -> None:
    repo = GitRepo(git_repo)
    created = _create(repo)
    update = git_repo / "root-update.md"
    update.write_text(
        str(created["content"])
        + "\n<!-- dww-root-children:start -->\n"
        + "<!-- dww-root-children:end -->\n",
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(SoloAIError, match="managed only by DWW Start"):
        update_root_anchor(
            repo,
            root_id="root-20260912000000-test",
            input_path=update,
            expected_sha256=str(created["sha256"]),
        )


def test_cli_exposes_root_anchor_and_child_binding() -> None:
    from solo_ai.cli import _parser

    parser = _parser()
    create = parser.parse_args(
        [
            "--repo",
            ".",
            "root-anchor",
            "create",
            "--purpose",
            "objective",
            "--target",
            "target",
            "--scope",
            "scope",
            "--acceptance",
            "acceptance",
        ]
    )
    child = parser.parse_args(
        [
            "--repo",
            ".",
            "start",
            "--name",
            "child",
            "--root-anchor",
            "root-1",
            "--root-anchor-file",
            "C:\\example\\.git\\solo-ai\\root-anchors\\root-1.md",
        ]
    )

    assert create.root_anchor_command == "create"
    assert child.root_anchor == "root-1"
    assert str(child.root_anchor_file).endswith("root-1.md")
