from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from solo_ai.repo import GitRepo
from solo_ai.root_context import create_root_anchor
from solo_ai.task_context import (
    anchor_path,
    read_anchor,
    read_anchor_update,
    require_anchor,
    update_anchor,
)
from solo_ai.util import SoloAIError


def _task(repo: Path) -> tuple[GitRepo, dict[str, object]]:
    git_repo = GitRepo(repo)
    task_id = "task-20260911034605-test"
    task: dict[str, object] = {
        "id": task_id,
        "status": "active",
        "anchor_origin": {
            "schema_version": "1",
            "task_id": task_id,
            "original_purpose": "test purpose",
            "reference_baseline": "main at abc",
        },
    }
    path = anchor_path(git_repo, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Task anchor: test\n\n"
        f"- Task ID: {task_id}\n"
        "- Original purpose: test purpose\n"
        "- Implementation target: target\n"
        "- Reference baseline: main at abc\n"
        "- Scope boundary: tests only\n"
        "- Acceptance criteria: unit tests pass\n"
        "- Current progress: started\n"
        "\nDetails stay here.\n",
        encoding="utf-8",
        newline="\n",
    )
    return git_repo, task


def test_read_and_update_anchor_returns_byte_sha_and_preserves_extra_text(
    git_repo: Path,
) -> None:
    repo, task = _task(git_repo)
    shown = read_anchor(repo, task)
    changed = shown["content"].replace(
        "- Current progress: started", "- Current progress: 阶段 A 完成"
    )
    result = update_anchor(repo, task, content=changed, expected_sha256=shown["sha256"])
    assert result["changed"] is True
    reread = read_anchor(repo, task)
    assert reread["content"] == changed
    assert "Details stay here." in reread["content"]
    assert reread["sha256"] == hashlib.sha256(changed.encode("utf-8")).hexdigest()


def test_update_anchor_accepts_identical_retry_before_stale_digest_check(
    git_repo: Path,
) -> None:
    repo, task = _task(git_repo)
    shown = read_anchor(repo, task)
    stale = shown["content"].replace(
        "- Current progress: started", "- Current progress: other"
    )
    update_anchor(repo, task, content=stale, expected_sha256=shown["sha256"])
    retry = update_anchor(repo, task, content=stale, expected_sha256=shown["sha256"])
    assert retry["changed"] is False
    current = read_anchor(repo, task)
    invalid = current["content"].replace(
        "- Scope boundary: tests only", "- Scope boundary: fill before editing"
    )
    with pytest.raises(SoloAIError, match="template placeholder"):
        update_anchor(repo, task, content=invalid, expected_sha256=current["sha256"])


def test_ready_anchor_update_allows_only_progress(git_repo: Path) -> None:
    repo, task = _task(git_repo)
    task["status"] = "ready"
    shown = read_anchor(repo, task)
    progress = shown["content"].replace(
        "- Current progress: started", "- Current progress: ready proof complete"
    )
    result = update_anchor(
        repo,
        task,
        content=progress,
        expected_sha256=shown["sha256"],
        progress_only=True,
    )
    assert result["changed"] is True
    current = read_anchor(repo, task)
    invalid = current["content"].replace(
        "- Scope boundary: tests only", "- Scope boundary: broader"
    )
    with pytest.raises(SoloAIError, match="only Current progress"):
        update_anchor(
            repo,
            task,
            content=invalid,
            expected_sha256=current["sha256"],
            progress_only=True,
        )
    changed_body = current["content"].replace("Details stay here.", "Details changed.")
    with pytest.raises(SoloAIError, match="full Current progress block"):
        update_anchor(
            repo,
            task,
            content=changed_body.replace(
                "- Current progress: ready proof complete",
                "- Current progress:\n  ready proof recorded",
            ),
            expected_sha256=current["sha256"],
            progress_only=True,
        )


def test_anchor_cli_parser_exposes_show_and_update() -> None:
    from solo_ai.cli import _parser

    parser = _parser()
    show = parser.parse_args(["--repo", ".", "anchor", "show", "--task", "task-1"])
    update = parser.parse_args(
        [
            "--repo",
            ".",
            "anchor",
            "update",
            "--task",
            "task-1",
            "--lease",
            "lease",
            "--file",
            "anchor.md",
            "--expected-sha256",
            "a" * 64,
        ]
    )
    assert show.anchor_command == "show"
    assert update.anchor_command == "update"
    assert update.expected_sha256 == "a" * 64


def test_anchor_fields_inside_code_fence_do_not_satisfy_identity(
    git_repo: Path,
) -> None:
    repo, task = _task(git_repo)
    shown = read_anchor(repo, task)
    fenced = shown["content"].replace(
        f"- Task ID: {task['id']}",
        f"```\n- Task ID: {task['id']}\n```",
    )
    with pytest.raises(SoloAIError, match="exactly one 'Task ID'"):
        update_anchor(
            repo,
            task,
            content=fenced,
            expected_sha256=shown["sha256"],
        )


def test_anchor_rejects_indented_fake_field_and_origin_mismatch(git_repo: Path) -> None:
    repo, task = _task(git_repo)
    shown = read_anchor(repo, task)
    indented = shown["content"] + "\n  - Original purpose: forged\n"
    with pytest.raises(SoloAIError, match="indented duplicate"):
        update_anchor(
            repo,
            task,
            content=indented,
            expected_sha256=shown["sha256"],
        )
    path = anchor_path(repo, str(task["id"]))
    path.write_text(
        shown["content"].replace(
            "- Original purpose: test purpose", "- Original purpose: forged"
        ),
        encoding="utf-8",
        newline="\n",
    )
    with pytest.raises(SoloAIError, match="original purpose does not match"):
        read_anchor(repo, task)


def test_root_bound_anchor_update_rejects_changed_root_reference(
    git_repo: Path,
) -> None:
    repo, task = _task(git_repo)
    root = create_root_anchor(
        repo,
        root_id="root-20260912000100-test",
        purpose="durable objective",
        target="bind a child anchor",
        base_ref="main",
        base_head=repo.head(repo.root),
        scope="anchor reference only",
        acceptance="child reference cannot drift",
    )
    task["root_anchor_id"] = root["root_id"]
    path = anchor_path(repo, str(task["id"]))
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "- Original purpose: test purpose\n",
            f"- Original purpose: test purpose\n- Root anchor: `{root['root_id']}`\n",
        ),
        encoding="utf-8",
        newline="\n",
    )
    shown = read_anchor(repo, task)
    drifted = str(shown["content"]).replace(f"- Root anchor: `{root['root_id']}`\n", "")

    with pytest.raises(SoloAIError, match="root reference does not match"):
        update_anchor(
            repo,
            task,
            content=drifted,
            expected_sha256=str(shown["sha256"]),
        )


def test_legacy_anchor_can_be_shown_but_not_ready_or_updated(git_repo: Path) -> None:
    repo, task = _task(git_repo)
    task.pop("anchor_origin")
    shown = read_anchor(repo, task)
    assert shown["origin_verified"] is False
    with pytest.raises(SoloAIError, match="before Ready"):
        require_anchor(repo, task, require_verified_origin=True)
    with pytest.raises(SoloAIError, match="before updating"):
        update_anchor(
            repo,
            task,
            content=shown["content"],
            expected_sha256=shown["sha256"],
        )


def test_anchor_path_rejects_path_like_task_id(git_repo: Path) -> None:
    repo = GitRepo(git_repo)
    with pytest.raises(SoloAIError, match="safe anchor name"):
        anchor_path(repo, "../outside")
    with pytest.raises(SoloAIError, match="safe anchor name"):
        anchor_path(repo, "not-a-task-id")


def test_anchor_update_input_must_stay_in_the_calling_worktree(git_repo: Path) -> None:
    repo, _ = _task(git_repo)
    outside = git_repo.parent / "outside-anchor-update.md"
    outside.write_text("outside", encoding="utf-8")
    with pytest.raises(SoloAIError, match="outside the allowed"):
        read_anchor_update(repo, outside)
