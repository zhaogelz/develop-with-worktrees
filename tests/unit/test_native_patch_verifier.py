from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2]))

import scripts.verify_native_patch_owner as verifier
from scripts.verify_native_patch_owner import main, verify_trace


def _hook(turn_id: str, status: str, text: str) -> dict[str, object]:
    thread_id = {
        "owner-turn": "owner-thread",
        "b-turn": "b-thread",
        "c-turn": "c-thread",
    }[turn_id]
    return {
        "method": "hook/completed",
        "params": {
            "threadId": thread_id,
            "turnId": turn_id,
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
            "threadId": "owner-thread",
            "turnId": "owner-turn",
            "item": {
                "type": "fileChange",
                "status": "completed",
                "changes": [{"path": f"{worktree}\\probe.txt", "diff": diff}],
            }
        },
    }


def _turn(turn_id: str) -> dict[str, object]:
    thread_id = {
        "owner-turn": "owner-thread",
        "b-turn": "b-thread",
        "c-turn": "c-thread",
    }[turn_id]
    return {
        "method": "turn/completed",
        "params": {
            "threadId": thread_id,
            "turn": {"id": turn_id, "status": "completed"},
        },
    }


def _valid_events(worktree: str) -> list[dict[str, object]]:
    return [
        _change(worktree, "relative"),
        _change(worktree, "absolute"),
        {
            "method": "item/started",
            "params": {
                "threadId": "owner-thread",
                "turnId": "owner-turn",
                "item": {
                    "type": "dynamicToolCall",
                    "id": "barrier-id",
                    "tool": "dww_test_barrier",
                    "status": "inProgress",
                },
            },
        },
        {
            "method": "item/tool/call",
            "params": {
                "threadId": "owner-thread",
                "turnId": "owner-turn",
                "callId": "barrier-id",
                "tool": "dww_test_barrier",
            },
        },
        _hook("b-turn", "blocked", "Codex session does not own this isolated task"),
        _turn("b-turn"),
        _hook("c-turn", "blocked", "target is not an active managed worktree"),
        _turn("c-turn"),
        {
            "method": "item/completed",
            "params": {
                "threadId": "owner-thread",
                "turnId": "owner-turn",
                "item": {
                    "type": "dynamicToolCall",
                    "id": "barrier-id",
                    "status": "completed",
                    "tool": "dww_test_barrier",
                    "success": True,
                },
            },
        },
        _change(worktree, "after-denials"),
        _hook("owner-turn", "completed", ""),
        _turn("owner-turn"),
    ]


def _owner_session_jsonl(
    tmp_path: Path,
    worktree: str,
    *,
    session_id: str = "owner-thread",
    turn_id: str = "owner-turn",
    targets: tuple[str, ...] | None = None,
) -> Path:
    targets = targets or (
        "probe.txt",
        f"{worktree}\\probe.txt",
        f"{worktree}\\probe.txt",
    )
    entries: list[dict[str, object]] = [
        {"type": "session_meta", "payload": {"session_id": session_id}}
    ]
    for target in targets:
        patch = f"*** Begin Patch\n*** Update File: {target}\n*** End Patch"
        entries.append(
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "input": f"await tools.apply_patch({json.dumps(patch)});",
                    "internal_chat_message_metadata_passthrough": {
                        "turn_id": turn_id
                    },
                },
            }
        )
    path = tmp_path / "owner-session.jsonl"
    path.write_text(
        "\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8"
    )
    return path


def test_verifier_rejects_owner_completion_without_b_and_c() -> None:
    result = verify_trace(
        [_turn("owner-turn")],
        {
            "owner_thread": "owner-thread",
            "owner_turn": "owner-turn",
            "b_thread": "b-thread",
            "b_turn": "b-turn",
            "c_thread": "c-thread",
            "c_turn": "c-turn",
        },
        expected_worktree=r"C:\repo\.worktrees\slot",
    )

    assert result.status == "INVALID_RUN"
    assert any(
        check.id == "N03" and check.status == "failed" for check in result.checks
    )


def test_verifier_rejects_derived_path_form_event_without_raw_jsonl() -> None:
    events = _valid_events(r"C:\repo\.worktrees\slot")
    events.append(
        {
            "method": "dww/nativePathForms",
            "params": {
                "source": "codex-session-jsonl",
                "threadId": "owner-thread",
                "turnId": "owner-turn",
                "forms": ["relative", "absolute"],
                "sequence": ["relative", "absolute", "absolute"],
            },
        }
    )

    result = verify_trace(
        events,
        {
            "owner_thread": "owner-thread",
            "owner_turn": "owner-turn",
            "b_thread": "b-thread",
            "b_turn": "b-turn",
            "c_thread": "c-thread",
            "c_turn": "c-turn",
        },
        expected_worktree=r"C:\repo\.worktrees\slot",
    )

    assert result.status == "BLOCKED_HOST"
    assert any(
        check.id == "PATH_FORMS" and check.status == "unverified"
        for check in result.checks
    )


def test_verifier_passes_only_with_bound_raw_owner_session(tmp_path: Path) -> None:
    worktree = r"C:\repo\.worktrees\slot"
    result = verify_trace(
        _valid_events(worktree),
        {
            "owner_thread": "owner-thread",
            "owner_turn": "owner-turn",
            "b_thread": "b-thread",
            "b_turn": "b-turn",
            "c_thread": "c-thread",
            "c_turn": "c-turn",
        },
        expected_worktree=worktree,
        owner_session_jsonl=_owner_session_jsonl(tmp_path, worktree),
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


def test_verifier_rejects_raw_owner_calls_from_another_turn(tmp_path: Path) -> None:
    worktree = r"C:\repo\.worktrees\slot"
    result = verify_trace(
        _valid_events(worktree),
        _turn_ids(),
        expected_worktree=worktree,
        owner_session_jsonl=_owner_session_jsonl(
            tmp_path, worktree, turn_id="other-owner-turn"
        ),
    )

    assert result.status == "BLOCKED_HOST"
    assert any(
        check.id == "PATH_FORMS" and check.status == "unverified"
        for check in result.checks
    )


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
        {
            "owner_thread": "owner-thread",
            "owner_turn": "owner-turn",
            "b_thread": "b-thread",
            "b_turn": "b-turn",
            "c_thread": "c-thread",
            "c_turn": "c-turn",
        },
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
    owner_session = _owner_session_jsonl(tmp_path, str(tmp_path / "worktree"))
    events.write_text(json.dumps([_turn("owner-turn")]), encoding="utf-8")
    turn_ids.write_text(
        json.dumps(
            {
                "owner_thread": "owner-thread",
                "owner_turn": "owner-turn",
                "b_thread": "b-thread",
                "b_turn": "b-turn",
                "c_thread": "c-thread",
                "c_turn": "c-turn",
            }
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
                "--owner-session-jsonl",
                str(owner_session),
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


def _turn_ids() -> dict[str, str]:
    return {
        "owner_thread": "owner-thread",
        "owner_turn": "owner-turn",
        "b_thread": "b-thread",
        "b_turn": "b-turn",
        "c_thread": "c-thread",
        "c_turn": "c-turn",
    }


def test_verifier_rejects_all_owner_changes_from_foreign_identity() -> None:
    events = _valid_events(r"C:\repo\.worktrees\slot")
    for event in events:
        item = ((event.get("params") or {}).get("item") or {})
        if item.get("type") == "fileChange":
            params = event["params"]
            assert isinstance(params, dict)
            params["threadId"] = "foreign-thread"
            params["turnId"] = "foreign-turn"

    result = verify_trace(
        events,
        _turn_ids(),
        expected_worktree=r"C:\repo\.worktrees\slot",
    )

    assert result.status == "FAIL_ASSERTION"
    assert any(
        check.id == "OWNER_CHANGES" and check.status == "failed"
        for check in result.checks
    )


def test_verifier_rejects_foreign_successful_change_to_target_file() -> None:
    events = _valid_events(r"C:\repo\.worktrees\slot")
    foreign_change = dict(events[0])
    foreign_change["params"] = dict(foreign_change["params"])
    foreign_change["params"]["threadId"] = "foreign-thread"
    foreign_change["params"]["turnId"] = "foreign-turn"
    events.insert(2, foreign_change)

    result = verify_trace(
        events,
        _turn_ids(),
        expected_worktree=r"C:\repo\.worktrees\slot",
    )

    assert result.status == "FAIL_ASSERTION"
    assert any(
        check.id == "OWNER_CHANGES" and check.status == "failed"
        for check in result.checks
    )


def test_verifier_rejects_foreign_multi_path_file_change() -> None:
    events = _valid_events(r"C:\repo\.worktrees\slot")
    extra = dict(events[0])
    extra_params = dict(extra["params"])
    extra_item = dict(extra_params["item"])
    extra_item["changes"] = [
        *extra_item["changes"],
        {"path": r"C:\repo\.worktrees\slot\other.txt", "diff": "foreign"},
    ]
    extra_params["item"] = extra_item
    extra_params["threadId"] = "foreign-thread"
    extra_params["turnId"] = "foreign-turn"
    extra["params"] = extra_params
    events.insert(2, extra)

    result = verify_trace(
        events,
        _turn_ids(),
        expected_worktree=r"C:\repo\.worktrees\slot",
    )

    assert result.status == "FAIL_ASSERTION"
    assert any(
        check.id == "OWNER_CHANGES" and check.status == "failed"
        for check in result.checks
    )


def test_verifier_requires_barrier_start_or_request() -> None:
    events = [
        event
        for event in _valid_events(r"C:\repo\.worktrees\slot")
        if event.get("method") not in {"item/started", "item/tool/call"}
    ]

    result = verify_trace(
        events,
        _turn_ids(),
        expected_worktree=r"C:\repo\.worktrees\slot",
    )

    assert result.status == "FAIL_ASSERTION"
    assert any(
        check.id == "BARRIER" and check.status == "failed" for check in result.checks
    )


def test_verifier_rejects_barrier_started_after_b_denial() -> None:
    events = _valid_events(r"C:\repo\.worktrees\slot")
    support_indexes = [
        index
        for index, event in enumerate(events)
        if event.get("method") in {"item/started", "item/tool/call"}
    ]
    support = [events[index] for index in support_indexes]
    for index in reversed(support_indexes):
        del events[index]
    b_turn_index = next(
        index
        for index, event in enumerate(events)
        if event.get("method") == "turn/completed"
        and (event.get("params") or {}).get("threadId") == "b-thread"
    )
    events[b_turn_index + 1 : b_turn_index + 1] = support

    result = verify_trace(
        events,
        _turn_ids(),
        expected_worktree=r"C:\repo\.worktrees\slot",
    )

    assert result.status == "FAIL_ASSERTION"
    assert any(
        check.id == "BARRIER_ORDER" and check.status == "failed"
        for check in result.checks
    )


def test_verifier_rejects_denials_moved_after_owner_third_change() -> None:
    events = _valid_events(r"C:\repo\.worktrees\slot")
    denial_indexes = [
        index
        for index, event in enumerate(events)
        if (event.get("params") or {}).get("threadId") in {"b-thread", "c-thread"}
    ]
    denials = [events[index] for index in denial_indexes]
    for index in reversed(denial_indexes):
        del events[index]
    third_change_index = next(
        index
        for index, event in enumerate(events)
        if ((event.get("params") or {}).get("item") or {})
        .get("changes", [{}])[0]
        .get("diff")
        == "after-denials"
    )
    events[third_change_index + 1 : third_change_index + 1] = denials

    result = verify_trace(
        events,
        _turn_ids(),
        expected_worktree=r"C:\repo\.worktrees\slot",
    )

    assert result.status == "FAIL_ASSERTION"
    assert any(
        check.id == "OWNER_CHANGES" and check.status == "failed"
        for check in result.checks
    )


def test_snapshot_rejects_unrelated_porcelain(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = repo / ".worktrees" / "slot"
    worktree.mkdir(parents=True)
    (repo / "probe.txt").write_text("baseline\n", encoding="utf-8")
    (worktree / "probe.txt").write_text(
        "owner-absolute\nowner-after-denials\n", encoding="utf-8"
    )

    def clean_git_status(path: Path) -> verifier.GitStatus:
        if path == worktree:
            return verifier.GitStatus((" M probe.txt",), True)
        return verifier.GitStatus((), True)

    monkeypatch.setattr(verifier, "_git_status", clean_git_status)
    clean_checks = verifier.verify_snapshot(repo, worktree)
    assert any(
        check.id == "NO_INTRUSION" and check.status == "passed"
        for check in clean_checks
    )

    for dirty_path in (repo, worktree):
        def fake_git_status(path: Path) -> verifier.GitStatus:
            if path == worktree:
                lines = [" M probe.txt"]
                if dirty_path == worktree:
                    lines.append("?? unrelated.txt")
                return verifier.GitStatus(tuple(lines), True)
            if dirty_path == repo:
                return verifier.GitStatus(("?? unrelated.txt",), True)
            return verifier.GitStatus((), True)

        monkeypatch.setattr(verifier, "_git_status", fake_git_status)
        checks = verifier.verify_snapshot(repo, worktree)

        assert any(
            check.id == "NO_INTRUSION" and check.status == "failed"
            for check in checks
        )


def test_snapshot_rejects_failed_git_query(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = repo / ".worktrees" / "slot"
    worktree.mkdir(parents=True)
    (repo / "probe.txt").write_text("baseline\n", encoding="utf-8")
    (worktree / "probe.txt").write_text(
        "owner-absolute\nowner-after-denials\n", encoding="utf-8"
    )

    monkeypatch.setattr(
        verifier,
        "_git_status",
        lambda _path: verifier.GitStatus((), False, "git status failed"),
    )
    checks = verifier.verify_snapshot(repo, worktree)

    assert any(
        check.id == "NO_INTRUSION" and check.status == "failed" for check in checks
    )


def test_git_status_command_error_is_not_clean(monkeypatch, tmp_path: Path) -> None:
    def raise_os_error(*_args: object, **_kwargs: object) -> None:
        raise OSError("git unavailable")

    monkeypatch.setattr(verifier.subprocess, "run", raise_os_error)

    result = verifier._git_status(tmp_path)

    assert result.succeeded is False
    assert result.lines == ()
    assert "git unavailable" in result.error


def test_git_status_nonzero_exit_is_not_clean(monkeypatch, tmp_path: Path) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> object:
        return type(
            "Completed",
            (),
            {"returncode": 1, "stdout": "", "stderr": "fatal: not a repo"},
        )()

    monkeypatch.setattr(verifier.subprocess, "run", fake_run)

    result = verifier._git_status(tmp_path)

    assert result.succeeded is False
    assert result.lines == ()
    assert result.error == "fatal: not a repo"
