from __future__ import annotations

import pytest

from solo_ai.cli import _parser
from solo_ai.host_context import host_reference, normalize_host_reference
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
