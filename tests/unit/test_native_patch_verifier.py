from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2]))

from scripts.verify_native_patch_owner import main, verify_trace


def _hook(thread_id: str, status: str, text: str) -> dict[str, object]:
    return {
        "method": "hook/completed",
        "params": {
            "threadId": thread_id,
            "turnId": thread_id,
            "run": {
                "eventName": "preToolUse",
                "status": status,
                "entries": [{"kind": "feedback", "text": text}],
            },
        },
    }


def _change(worktree: str, diff: str) -> dict[str, object]:
    return {
        "method": "item/completed",
        "params": {
            "item": {
                "type": "fileChange",
                "status": "completed",
                "changes": [{"path": f"{worktree}\\probe.txt", "diff": diff}],
            }
        },
    }


def _turn(turn_id: str) -> dict[str, object]:
    return {
        "method": "turn/completed",
        "params": {"turn": {"id": turn_id, "status": "completed"}},
    }


def _valid_events(worktree: str) -> list[dict[str, object]]:
    return [
        _change(worktree, "relative"),
        _change(worktree, "absolute"),
        {
            "method": "item/completed",
            "params": {"item": {"type": "dynamicToolCall", "status": "completed"}},
        },
        _hook("b-turn", "blocked", "Codex session does not own this isolated task"),
        _turn("b-turn"),
        _hook("c-turn", "blocked", "target is not an active managed worktree"),
        _turn("c-turn"),
        _change(worktree, "after-denials"),
        _hook("owner-turn", "completed", ""),
        _turn("owner-turn"),
        {
            "method": "dww/nativePathForms",
            "params": {
                "source": "codex-session-jsonl",
                "turnId": "owner-turn",
                "forms": ["relative", "absolute"],
                "sequence": ["relative", "absolute", "absolute"],
            },
        },
    ]


def test_verifier_rejects_owner_completion_without_b_and_c() -> None:
    result = verify_trace(
        [_turn("owner-turn")],
        {"owner_turn": "owner-turn", "b_turn": "b-turn", "c_turn": "c-turn"},
        expected_worktree=r"C:\repo\.worktrees\slot",
    )

    assert result.status == "INVALID_RUN"
    assert any(
        check.id == "N03" and check.status == "failed" for check in result.checks
    )


def test_verifier_requires_explicit_path_form_evidence() -> None:
    events = _valid_events(r"C:\repo\.worktrees\slot")
    events = [event for event in events if event.get("method") != "dww/nativePathForms"]

    result = verify_trace(
        events,
        {"owner_turn": "owner-turn", "b_turn": "b-turn", "c_turn": "c-turn"},
        expected_worktree=r"C:\repo\.worktrees\slot",
    )

    assert result.status == "BLOCKED_HOST"
    assert any(
        check.id == "PATH_FORMS" and check.status == "unverified"
        for check in result.checks
    )


def test_verifier_passes_only_with_complete_trace() -> None:
    result = verify_trace(
        _valid_events(r"C:\repo\.worktrees\slot"),
        {"owner_turn": "owner-turn", "b_turn": "b-turn", "c_turn": "c-turn"},
        expected_worktree=r"C:\repo\.worktrees\slot",
    )

    assert result.status == "PASS"
    assert {check.id for check in result.checks if check.status == "passed"} >= {
        "TURNS",
        "BARRIER",
        "N03",
        "N04",
        "OWNER_CHANGES",
        "PATH_FORMS",
    }


def test_verifier_rejects_file_change_sequence_without_barrier_boundary() -> None:
    events = _valid_events(r"C:\repo\.worktrees\slot")
    barrier_index = next(
        index
        for index, event in enumerate(events)
        if event.get("method") == "item/completed"
        and ((event.get("params") or {}).get("item") or {}).get("type")
        == "dynamicToolCall"
    )
    third_change_index = next(
        index
        for index, event in enumerate(events)
        if event.get("method") == "item/completed"
        and ((event.get("params") or {}).get("item") or {})
        .get("changes", [{}])[0]
        .get("diff")
        == "after-denials"
    )
    events.insert(barrier_index, events.pop(third_change_index))

    result = verify_trace(
        events,
        {"owner_turn": "owner-turn", "b_turn": "b-turn", "c_turn": "c-turn"},
        expected_worktree=r"C:\repo\.worktrees\slot",
    )

    assert result.status == "FAIL_ASSERTION"
    assert any(
        check.id == "OWNER_CHANGES" and check.status == "failed"
        for check in result.checks
    )


def test_cli_preserves_invalid_run_when_snapshot_also_fails(tmp_path: Path) -> None:
    events = tmp_path / "events.json"
    turn_ids = tmp_path / "turn-ids.json"
    result_path = tmp_path / "result.json"
    events.write_text(json.dumps([_turn("owner-turn")]), encoding="utf-8")
    turn_ids.write_text(
        json.dumps(
            {"owner_turn": "owner-turn", "b_turn": "b-turn", "c_turn": "c-turn"}
        ),
        encoding="utf-8",
    )

    assert (
        main(
            [
                "--events",
                str(events),
                "--turn-ids",
                str(turn_ids),
                "--repo",
                str(tmp_path / "repo"),
                "--worktree",
                str(tmp_path / "worktree"),
                "--result",
                str(result_path),
            ]
        )
        == 1
    )
    assert (
        json.loads(result_path.read_text(encoding="utf-8"))["status"] == "INVALID_RUN"
    )
