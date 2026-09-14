from __future__ import annotations

from pathlib import Path

import pytest

from solo_ai.repo import GitRepo
from solo_ai.root_context import (
    amend_root_anchor,
    create_root_anchor,
    record_root_acceptance,
    require_candidate_delivery_terminal,
    root_anchor_path,
    show_root_anchor,
    update_root_progress,
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


def test_confirmed_plan_root_requires_a_stable_request_id(git_repo: Path) -> None:
    repo = GitRepo(git_repo)
    with pytest.raises(SoloAIError, match="stable request id"):
        create_root_anchor(
            repo,
            root_id="root-20260912000000-needs-request",
            purpose="persist a confirmed plan",
            target="require idempotent creation",
            base_ref="main",
            base_head=repo.head(repo.root),
            scope="root creation only",
            acceptance="a retry cannot create a duplicate root",
            confirmed_plan="# confirmed plan",
            plan_source="user asked to proceed",
        )


def test_legacy_root_with_an_incidental_plan_heading_remains_readable(
    git_repo: Path,
) -> None:
    repo = GitRepo(git_repo)
    created = _create(repo)
    update = git_repo / "legacy-heading.md"
    update.write_text(
        str(created["content"])
        + "\n## Confirmed plan\n\nThis is only a historic note in a legacy root.\n",
        encoding="utf-8",
        newline="\n",
    )
    update_root_anchor(
        repo,
        root_id=str(created["root_id"]),
        input_path=update,
        expected_sha256=str(created["sha256"]),
    )
    shown = show_root_anchor(repo, root_id=str(created["root_id"]))
    assert shown["plan_version"] is None
    assert shown["confirmed_plan"] is None


def test_structured_root_keeps_complete_plan_and_versions_user_amendments(
    git_repo: Path,
) -> None:
    repo = GitRepo(git_repo)
    original_plan = """# 已确认方案

- 需求：保留完整正文。

## 实施细节

- 用户方案可以自由使用二级标题，不能被锚点格式截断。
- Root ID: 这行属于方案正文，不能被当成锚点元数据。
```text
- Acceptance criteria: 这行也属于示例。
```
"""
    created = create_root_anchor(
        repo,
        root_id="root-20260912000000-structured",
        purpose="persist the confirmed plan",
        target="keep the complete plan as the root source",
        base_ref="main",
        base_head=repo.head(repo.root),
        scope="goal anchor content only",
        acceptance="plan changes are versioned and total acceptance is recorded",
        confirmed_plan=original_plan,
        plan_source="user confirmed plan",
        request_id="structured-root-test",
    )
    assert created["plan_version"] == 1
    assert created["confirmed_plan"] == original_plan.strip()
    assert created["overall_acceptance_status"] == "pending"

    progressed = update_root_progress(
        repo,
        root_id=str(created["root_id"]),
        progress="child task is implementing the first phase",
        expected_sha256=str(created["sha256"]),
    )
    assert progressed["plan_version"] == 1

    amended_plan = "# 已确认方案\n\n- 需求：保留完整正文与明确修订。\n"
    amended = amend_root_anchor(
        repo,
        root_id=str(created["root_id"]),
        confirmed_plan=amended_plan,
        source="user explicitly changed the requirement",
        summary="add the amendment history requirement",
        expected_sha256=str(progressed["sha256"]),
    )
    assert amended["plan_version"] == 2
    assert amended["confirmed_plan"] == amended_plan.strip()
    assert "Version 2: add the amendment history requirement" in amended["content"]

    invalid = str(amended["content"]).replace("完整正文与明确修订", "静默改写", 1)
    update = git_repo / "invalid-plan-update.md"
    update.write_text(invalid, encoding="utf-8", newline="\n")
    with pytest.raises(SoloAIError, match="increment plan version"):
        update_root_anchor(
            repo,
            root_id=str(created["root_id"]),
            input_path=update,
            expected_sha256=str(amended["sha256"]),
        )

    accepted = record_root_acceptance(
        repo,
        root_id=str(created["root_id"]),
        status="accepted",
        evidence="all child tasks reached delivery and the effective plan was checked",
        expected_sha256=str(amended["sha256"]),
    )
    assert accepted["overall_acceptance_status"] == "accepted"


@pytest.mark.parametrize("status", ["held", "pending", "sealed", "retained"])
def test_root_candidate_delivery_rejects_nonterminal_candidate_states(
    status: str,
) -> None:
    candidates = {
        "candidate-one": {
            "candidate_id": "candidate-one",
            "task_id": "task-one",
            "status": status,
        }
    }

    with pytest.raises(SoloAIError, match="not delivered or withdrawn"):
        require_candidate_delivery_terminal(
            candidates, task_id="task-one", label="Root child task-one"
        )


def test_root_candidate_delivery_follows_supersession_to_terminal_outcome() -> None:
    candidates = {
        "candidate-original": {
            "candidate_id": "candidate-original",
            "task_id": "task-one",
            "status": "superseded",
            "superseded_by": "candidate-repair",
        },
        "candidate-repair": {
            "candidate_id": "candidate-repair",
            "task_id": "task-two",
            "status": "integrated",
        },
    }

    require_candidate_delivery_terminal(
        candidates, task_id="task-one", label="Root child task-one"
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
            "--plan-file",
            ".tmp\\confirmed-plan.md",
            "--plan-source",
            "user confirmed it",
            "--request-id",
            "host-request-1",
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
    assert create.plan_source == "user confirmed it"
    assert create.request_id == "host-request-1"
    assert child.root_anchor == "root-1"
    assert str(child.root_anchor_file).endswith("root-1.md")
