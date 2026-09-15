from __future__ import annotations

import pytest

from solo_ai import cli
from solo_ai.cli import _parser
from solo_ai.host_context import (
    host_reference,
    normalize_host_reference,
    resolve_host_reference,
)
from solo_ai.util import SoloAIError


def test_host_reference_requires_a_complete_verified_pair() -> None:
    assert host_reference("codex", "thread-123") == {
        "kind": "codex",
        "thread_id": "thread-123",
    }
    assert host_reference(None, None) is None

    with pytest.raises(SoloAIError, match="provided together"):
        host_reference("codex", None)
    with pytest.raises(SoloAIError, match="lowercase"):
        host_reference("Codex", "thread-123")
    with pytest.raises(SoloAIError, match="single-line"):
        host_reference("codex", "thread\n123")


def test_normalize_host_reference_refuses_extra_or_mutable_fields() -> None:
    original = {"kind": "codex", "thread_id": "thread-123"}
    normalized = normalize_host_reference(original)

    assert normalized == original
    assert normalized is not original
    with pytest.raises(SoloAIError, match="exactly kind and thread_id"):
        normalize_host_reference(
            {"kind": "codex", "thread_id": "thread-123", "title": "guess"}
        )


def test_resolve_host_reference_uses_exact_codex_thread_only_as_a_fallback() -> None:
    assert resolve_host_reference(
        None, None, environment={"CODEX_THREAD_ID": "desktop-task"}
    ) == {"kind": "codex", "thread_id": "desktop-task"}
    assert resolve_host_reference(
        "codex", "explicit-task", environment={"CODEX_THREAD_ID": "desktop-task"}
    ) == {"kind": "codex", "thread_id": "explicit-task"}
    assert (
        resolve_host_reference(None, None, environment={"CODEX_SESSION_ID": "hook"})
        is None
    )


def test_host_handoff_commands_require_exact_host_references() -> None:
    parser = _parser()
    dispatch = parser.parse_args(
        [
            "host-handoff",
            "repair",
            "dispatch",
            "--request",
            "repair-123",
            "--host-kind",
            "codex",
            "--host-thread",
            "coordinator",
        ]
    )
    takeover = parser.parse_args(
        [
            "host-handoff",
            "batch",
            "take-over",
            "--batch",
            "batch-123",
            "--expected-revision",
            "2",
            "--reason",
            "coordinator unavailable",
            "--host-kind",
            "codex",
            "--host-thread",
            "replacement",
        ]
    )

    assert dispatch.host_handoff_command == "repair"
    assert dispatch.host_handoff_repair_command == "dispatch"
    assert dispatch.retry is False
    assert takeover.host_handoff_batch_command == "take-over"
    assert takeover.expected_revision == 2

    automatic = parser.parse_args(
        ["host-handoff", "repair", "dispatch", "--request", "repair-123"]
    )
    assert automatic.host_kind is None
    assert automatic.host_thread is None


def test_cli_start_uses_the_injected_codex_thread(
    git_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def capture_start(_repo, **kwargs):
        captured.update(kwargs)
        return {"id": "task-test"}

    monkeypatch.setenv("CODEX_THREAD_ID", "desktop-task")
    monkeypatch.setattr(cli, "start", capture_start)
    args = _parser().parse_args(
        ["--repo", str(git_repo), "start", "--name", "automatic host"]
    )

    assert cli._dispatch(args) == {"id": "task-test"}
    assert captured["host_origin"] == {"kind": "codex", "thread_id": "desktop-task"}


def test_cli_finish_uses_the_injected_codex_thread(
    git_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def capture_finish(_repo, **kwargs):
        captured.update(kwargs)
        return {"id": "task-test"}

    monkeypatch.setenv("CODEX_THREAD_ID", "desktop-task")
    monkeypatch.setattr(cli, "finish", capture_finish)
    args = _parser().parse_args(
        [
            "--repo",
            str(git_repo),
            "finish",
            "--task",
            "task-test",
            "--lease",
            "private-lease",
        ]
    )

    assert cli._dispatch(args) == {"id": "task-test"}
    assert captured["host_actor"] == {"kind": "codex", "thread_id": "desktop-task"}
