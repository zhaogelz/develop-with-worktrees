from __future__ import annotations

from pathlib import Path

import pytest

from solo_ai.repo import GitRepo
from solo_ai.root_context import (
    amend_root_anchor,
    create_root_anchor,
    delete_root_anchor,
    record_root_acceptance,
    require_candidate_delivery_terminal,
    root_anchor_history_path,
    root_anchor_path,
    show_root_anchor,
    show_root_anchor_history,
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
        change_text=None,
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
    assert accepted["overall_acceptance_plan_version"] == 2


def test_incremental_amendment_appends_the_exact_change_and_resets_acceptance(
    git_repo: Path,
) -> None:
    repo = GitRepo(git_repo)
    created = create_root_anchor(
        repo,
        root_id="root-20260912000000-incremental",
        purpose="preserve incremental user words",
        target="append one confirmed correction",
        base_ref="main",
        base_head=repo.head(repo.root),
        scope="root amendment only",
        acceptance="every amendment is versioned and checked again",
        confirmed_plan="# V1\n\nKeep this text.",
        plan_source="user confirmed v1",
        request_id="incremental-root-test",
    )
    accepted = record_root_acceptance(
        repo,
        root_id=str(created["root_id"]),
        status="accepted",
        evidence="V1 was checked",
        expected_sha256=str(created["sha256"]),
    )
    exact_change = "用户原文第一行。\n\n```text\n  保留缩进和代码。\n```\n"
    amended = amend_root_anchor(
        repo,
        root_id=str(created["root_id"]),
        confirmed_plan=None,
        change_text=exact_change,
        source="user supplied a local correction",
        summary="append the exact correction",
        expected_sha256=str(accepted["sha256"]),
    )

    assert amended["plan_version"] == 2
    assert "# V1\n\nKeep this text." in str(amended["confirmed_plan"])
    assert exact_change.rstrip("\n") in str(amended["confirmed_plan"])
    assert amended["overall_acceptance_status"] == "pending"
    assert amended["overall_acceptance_plan_version"] is None
    assert "Version 2: append the exact correction" in str(amended["content"])


def test_generic_structured_update_resets_acceptance_and_cannot_grant_it(
    git_repo: Path,
) -> None:
    repo = GitRepo(git_repo)
    created = create_root_anchor(
        repo,
        root_id="root-20260912000000-generic-reset",
        purpose="reset acceptance on a generic plan update",
        target="protect the current plan version",
        base_ref="main",
        base_head=repo.head(repo.root),
        scope="structured root write path",
        acceptance="only accept records a checked outcome",
        confirmed_plan="# V1\n",
        plan_source="user confirmed v1",
        request_id="generic-reset-root-test",
    )
    accepted = record_root_acceptance(
        repo,
        root_id=str(created["root_id"]),
        status="accepted",
        evidence="V1 was checked",
        expected_sha256=str(created["sha256"]),
    )
    direct_outcome = str(accepted["content"]).replace(
        "- Status: accepted", "- Status: cancelled", 1
    )
    update = git_repo / "generic-root-update.md"
    update.write_text(direct_outcome, encoding="utf-8", newline="\n")
    with pytest.raises(SoloAIError, match="Only root-anchor accept"):
        update_root_anchor(
            repo,
            root_id=str(created["root_id"]),
            input_path=update,
            expected_sha256=str(accepted["sha256"]),
        )

    next_plan = direct_outcome.replace("- Plan version: 1", "- Plan version: 2", 1)
    next_plan = next_plan.replace("# V1", "# V2", 1)
    next_plan = next_plan.replace(
        "<!-- dww-user-changes:end -->",
        "- Version 2: generic update. Source: user confirmed it\n"
        "<!-- dww-user-changes:end -->",
        1,
    )
    update.write_text(next_plan, encoding="utf-8", newline="\n")
    updated = update_root_anchor(
        repo,
        root_id=str(created["root_id"]),
        input_path=update,
        expected_sha256=str(accepted["sha256"]),
    )
    assert updated["plan_version"] == 2
    assert updated["overall_acceptance_status"] == "pending"
    assert updated["overall_acceptance_plan_version"] is None


def test_large_structured_root_keeps_full_history_for_amend_and_generic_update(
    git_repo: Path,
) -> None:
    repo = GitRepo(git_repo)
    large_body = "内容" + "x" * (4 * 1024 * 1024 + 17)
    original_plan = f"# 完整方案 V1\n\n{large_body}\n"
    created = create_root_anchor(
        repo,
        root_id="root-20260912000000-unlimited",
        purpose="keep an unrestricted complete plan",
        target="exercise large anchor reads and updates",
        base_ref="main",
        base_head=repo.head(repo.root),
        scope="root size and full-history behavior",
        acceptance="every full prior plan remains readable",
        confirmed_plan=original_plan,
        plan_source="user confirmed a large plan",
        request_id="unlimited-root-test",
    )
    assert created["size_bytes"] > 4 * 1024 * 1024
    assert created["confirmed_plan"] == original_plan.strip()

    progressed = update_root_progress(
        repo,
        root_id=str(created["root_id"]),
        progress="large plan is being implemented",
        expected_sha256=str(created["sha256"]),
    )
    assert not root_anchor_history_path(
        repo, root_id=str(created["root_id"]), version=1
    ).exists()

    amended_plan = "# 完整方案 V2\n\n新的完整依据。\n"
    amended = amend_root_anchor(
        repo,
        root_id=str(created["root_id"]),
        confirmed_plan=amended_plan,
        change_text=None,
        source="user changed the confirmed plan",
        summary="replace the effective plan",
        expected_sha256=str(progressed["sha256"]),
    )
    history_v1 = show_root_anchor_history(
        repo, root_id=str(created["root_id"]), version=1
    )
    assert history_v1["content"] == progressed["content"]
    assert history_v1["size_bytes"] > 4 * 1024 * 1024

    generic = str(amended["content"])
    generic = generic.replace("- Plan version: 2", "- Plan version: 3", 1)
    generic = generic.replace("# 完整方案 V2", "# 完整方案 V3", 1)
    generic = generic.replace(
        "<!-- dww-user-changes:end -->",
        "- Version 3: update the effective plan through generic update. Source: user confirmed it\n"
        "<!-- dww-user-changes:end -->",
        1,
    )
    update = git_repo / "large-generic-update.md"
    update.write_text(generic, encoding="utf-8", newline="\n")
    updated = update_root_anchor(
        repo,
        root_id=str(created["root_id"]),
        input_path=update,
        expected_sha256=str(amended["sha256"]),
    )
    assert (
        show_root_anchor_history(repo, root_id=str(created["root_id"]), version=2)[
            "content"
        ]
        == amended["content"]
    )

    after_progress = update_root_progress(
        repo,
        root_id=str(created["root_id"]),
        progress="generic update checked",
        expected_sha256=str(updated["sha256"]),
    )
    assert after_progress["plan_version"] == 3
    assert not root_anchor_history_path(
        repo, root_id=str(created["root_id"]), version=3
    ).exists()


def test_deleting_a_root_removes_its_complete_history(git_repo: Path) -> None:
    repo = GitRepo(git_repo)
    created = create_root_anchor(
        repo,
        root_id="root-20260912000000-delete-history",
        purpose="remove terminal root history",
        target="exercise terminal cleanup",
        base_ref="main",
        base_head=repo.head(repo.root),
        scope="root and its own history only",
        acceptance="both local records are removed together",
        confirmed_plan="# V1\n",
        plan_source="user confirmed it",
        request_id="delete-history-test",
    )
    amended = amend_root_anchor(
        repo,
        root_id=str(created["root_id"]),
        confirmed_plan="# V2\n",
        change_text=None,
        source="user changed it",
        summary="advance the plan",
        expected_sha256=str(created["sha256"]),
    )
    history = root_anchor_history_path(repo, root_id=str(created["root_id"]), version=1)
    assert history.exists()
    delete_root_anchor(repo, root_id=str(amended["root_id"]))
    assert not root_anchor_path(repo, str(created["root_id"])).exists()
    assert not history.parent.exists()


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
    root_show = parser.parse_args(
        [
            "--repo",
            ".",
            "root-anchor",
            "show",
            "--root",
            "root-1",
            "--version",
            "2",
            "--content",
        ]
    )
    bind = parser.parse_args(
        [
            "--repo",
            ".",
            "anchor",
            "bind-root",
            "--task",
            "task-1",
            "--lease",
            "lease",
            "--root",
            "root-1",
        ]
    )
    amendment = parser.parse_args(
        [
            "--repo",
            ".",
            "root-anchor",
            "amend",
            "--root",
            "root-1",
            "--change-file",
            ".tmp\\user-change.md",
            "--source",
            "user confirmed the correction",
            "--summary",
            "append the exact correction",
            "--expected-sha256",
            "a" * 64,
        ]
    )

    assert create.root_anchor_command == "create"
    assert create.plan_source == "user confirmed it"
    assert create.request_id == "host-request-1"
    assert child.root_anchor == "root-1"
    assert str(child.root_anchor_file).endswith("root-1.md")
    assert root_show.version == 2
    assert root_show.content is True
    assert bind.anchor_command == "bind-root"
    assert amendment.plan_file is None
    assert str(amendment.change_file).endswith("user-change.md")
