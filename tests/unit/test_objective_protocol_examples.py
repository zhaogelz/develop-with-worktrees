from __future__ import annotations

import re
from pathlib import Path

import pytest

from solo_ai.repo import GitRepo
from solo_ai.root_context import (
    amend_root_anchor,
    create_root_anchor,
    record_root_acceptance,
    reindex_root_acceptance,
    show_root_anchor,
    update_root_progress,
)
from solo_ai.util import SoloAIError


def _index(plan: str, *ids: str) -> list[dict[str, object]]:
    """按演练已确认的验收项建立索引，故意不从自然语言自动猜测。"""

    rows: list[dict[str, object]] = []
    for item_id in ids:
        match = re.search(rf"^- {re.escape(item_id)}: (?P<quote>.+)$", plan, re.M)
        assert match is not None
        rows.append(
            {
                "id": item_id,
                "locator": f"confirmed plan: {item_id}",
                "quote": str(match.group("quote")),
                "required": True,
                "plan_version": None,
            }
        )
    return rows


def _create_protocol_root(
    git_repo: Path, *, root_id: str, plan: str, index: list[dict[str, object]]
) -> dict[str, object]:
    repo = GitRepo(git_repo)
    return create_root_anchor(
        repo,
        root_id=root_id,
        purpose="exercise confirmed-objective semantics",
        target="preserve the effective user plan",
        base_ref="main",
        base_head=repo.head(repo.root),
        scope="root objective protocol examples only",
        acceptance="the reviewed acceptance index covers the confirmed plan",
        confirmed_plan=plan,
        plan_source="user confirmed final plan",
        request_id=f"semantic-example-{root_id}",
        acceptance_index=index,
        objective_protocol_version=1,
    )


def test_t01_final_confirmation_replaces_an_unconfirmed_draft(git_repo: Path) -> None:
    """T01：主锚点保存最终确认稿，不能把讨论草案当成永久方案。"""

    unconfirmed_draft = "- A01: 先把全部讨论草案永久冻结。"
    final_plan = "- A01: 仅以最终确认方案作为主锚点。"
    created = _create_protocol_root(
        git_repo,
        root_id="root-20260917000000-t01",
        plan=final_plan,
        index=_index(final_plan, "A01"),
    )

    shown = show_root_anchor(GitRepo(git_repo), root_id=str(created["root_id"]))
    assert shown["confirmed_plan"] == final_plan
    assert unconfirmed_draft not in str(shown["content"])


def test_t02_explicit_user_amendment_is_versioned_without_rewriting_v1(
    git_repo: Path,
) -> None:
    """T02：用户明确修正形成新版本，原确认方案和原文修正都可追溯。"""

    plan = "- A01: 已确认的完整方案必须保留。"
    created = _create_protocol_root(
        git_repo,
        root_id="root-20260917000000-t02",
        plan=plan,
        index=_index(plan, "A01"),
    )
    user_words = "- A02: 后续明确修正必须作为版本化原文追加。"
    effective_plan = (
        f"{plan}\n\n---\n\n## User-confirmed amendment (version 2)\n\n{user_words}"
    )
    amended = amend_root_anchor(
        GitRepo(git_repo),
        root_id=str(created["root_id"]),
        confirmed_plan=None,
        change_text=user_words,
        source="user explicitly corrected the plan",
        summary="append the exact correction",
        expected_sha256=str(created["sha256"]),
        acceptance_index=_index(effective_plan, "A01", "A02"),
    )

    assert amended["plan_version"] == 2
    assert str(amended["confirmed_plan"]).startswith(plan)
    assert user_words in str(amended["confirmed_plan"])


def test_t19_technical_implementation_choice_does_not_amend_user_objective(
    git_repo: Path,
) -> None:
    """T19：技术实现调整只能记入进度，不能悄悄扩大或改写用户目标。"""

    plan = "- A01: 主锚点保留目的、范围边界和验收标准。"
    created = _create_protocol_root(
        git_repo,
        root_id="root-20260917000000-t19",
        plan=plan,
        index=_index(plan, "A01"),
    )
    progressed = update_root_progress(
        GitRepo(git_repo),
        root_id=str(created["root_id"]),
        progress="technical choice: store child execution state in the Git common-dir",
        expected_sha256=str(created["sha256"]),
    )

    assert progressed["plan_version"] == 1
    assert progressed["confirmed_plan"] == plan
    assert "technical choice" in str(progressed["content"])


def test_t21_reviewer_corrects_an_omitted_acceptance_index_item(git_repo: Path) -> None:
    """T21：索引漏项须由核对发现、重建索引；之后不能以不完整证据验收。"""

    plan = "\n".join(
        [
            "- A01: 保留原始目的。",
            "- A02: 保留完整最终方案。",
            "- A03: 对每项验收标准记录可核查证据。",
        ]
    )
    initially_omitted = _index(plan, "A01", "A02")
    created = _create_protocol_root(
        git_repo,
        root_id="root-20260917000000-t21",
        plan=plan,
        index=initially_omitted,
    )

    # 此处的比较代表宿主在自然语言语义核对时的明确发现；DWW 不声称能从任意
    # 自然语言方案自动推断所有验收项。发现后必须用受控 reindex 修复。
    expected_ids = {"A01", "A02", "A03"}
    indexed_ids = {str(row["id"]) for row in initially_omitted}
    assert expected_ids - indexed_ids == {"A03"}

    reindexed = reindex_root_acceptance(
        GitRepo(git_repo),
        root_id=str(created["root_id"]),
        acceptance_index=_index(plan, "A01", "A02", "A03"),
        expected_sha256=str(created["sha256"]),
    )
    incomplete_evidence = (
        '{"items": ['
        '{"id":"A01","status":"passed","observation":"ok","evidence":"proof/a"},'
        '{"id":"A02","status":"passed","observation":"ok","evidence":"proof/b"}'
        "]}"
    )
    with pytest.raises(SoloAIError, match="missing indexed items"):
        record_root_acceptance(
            GitRepo(git_repo),
            root_id=str(created["root_id"]),
            status="accepted",
            evidence=incomplete_evidence,
            expected_sha256=str(reindexed["sha256"]),
        )
