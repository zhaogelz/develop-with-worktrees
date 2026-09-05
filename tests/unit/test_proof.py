from __future__ import annotations

from types import SimpleNamespace

from solo_ai import proof


def test_execution_environment_keeps_required_windows_data_paths(
    monkeypatch,
) -> None:
    monkeypatch.setenv("APPDATA", r"C:\Users\example\AppData\Roaming")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\example\AppData\Local")
    monkeypatch.setenv("PROGRAMDATA", r"C:\ProgramData")
    monkeypatch.setenv("UNDECLARED_SECRET", "must-not-leak")

    environment = proof._execution_environment(SimpleNamespace(environment=()))

    assert environment["APPDATA"] == r"C:\Users\example\AppData\Roaming"
    assert environment["LOCALAPPDATA"] == r"C:\Users\example\AppData\Local"
    assert environment["PROGRAMDATA"] == r"C:\ProgramData"
    assert "UNDECLARED_SECRET" not in environment
