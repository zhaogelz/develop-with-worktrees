from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

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


def test_verified_descendant_reuses_parent_claim_without_self_deadlock(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    queue.set_capacity("2")
    module_root = str(Path(queue.__file__).resolve().parents[1])
    code = "\n".join(
        (
            "import json, sys",
            f"sys.path.insert(0, {module_root!r})",
            "from solo_ai.validation_queue import claim_validation_slot, queue_status",
            "with claim_validation_slot('normal') as claim:",
            "    print(json.dumps({'claim': claim, 'status': queue_status()}))",
        )
    )

    with queue.claim_validation_slot("heavy") as parent_claim:
        environment = os.environ.copy()
        environment.update(queue.inherited_claim_environment(parent_claim))
        completed = subprocess.run(
            [sys.executable, "-c", code],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
            env=environment,
        )
        observed = json.loads(completed.stdout)
        assert observed["claim"]["inherited"] is True
        assert observed["claim"]["inherited_from"] == parent_claim["id"]
        assert observed["claim"]["units"] == 0
        assert observed["status"]["active_units"] == 2

    assert queue.queue_status()["active_units"] == 0


def test_nested_heavy_validation_rejects_a_normal_parent_claim(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    queue.set_capacity("2")

    with queue.claim_validation_slot("normal") as parent_claim:
        monkeypatch.setenv(
            queue.INHERITED_CLAIM_ENV,
            queue.inherited_claim_environment(parent_claim)[queue.INHERITED_CLAIM_ENV],
        )
        with pytest.raises(
            queue.SoloAIError, match="declare the outer validation heavy"
        ):
            with queue.claim_validation_slot("heavy"):
                raise AssertionError("nested heavy claim must not be granted")


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
