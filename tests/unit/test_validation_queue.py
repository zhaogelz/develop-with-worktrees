from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import solo_ai.validation_queue as queue


def test_auto_capacity_uses_stable_physical_machine_dimensions(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(queue.psutil, "cpu_count", lambda logical=False: 8)
    monkeypatch.setattr(
        queue.psutil, "virtual_memory", lambda: SimpleNamespace(total=16 * 2**30)
    )

    details = queue.capacity_details()

    assert details["mode"] == "auto"
    assert details["capacity"] == 2
    assert details["physical_cores"] == 8


def test_fixed_machine_capacity_is_local_and_heavy_claim_is_exclusive(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    queue.set_capacity("2")
    started = threading.Event()

    def claim_normal() -> None:
        with queue.claim_validation_slot("normal"):
            started.set()

    with queue.claim_validation_slot("heavy"):
        status = queue.queue_status()
        assert status["capacity"]["mode"] == "fixed"
        assert status["active_units"] == 2
        worker = threading.Thread(target=claim_normal)
        worker.start()
        time.sleep(0.35)
        assert not started.is_set()

    worker.join(timeout=3)
    assert not worker.is_alive()
    assert started.is_set()
    assert queue.queue_status()["active_units"] == 0


def test_local_duration_median_produces_only_an_advisory(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    digests = ["command-a"]
    for value in (650.0, 700.0, 750.0):
        queue.record_profile_duration(
            profile_id="slow", command_digests=digests, duration_seconds=value
        )

    estimate = queue.estimate_validation([("slow", digests)])

    assert estimate["estimated_seconds"] == 700.0
    assert estimate["advisory"]
