from __future__ import annotations

from types import SimpleNamespace

from solo_ai import proof
from solo_ai.config import CommandSpec


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


def test_tool_probe_cache_is_local_and_rechecks_executable_identity(
    tmp_path, monkeypatch
) -> None:
    policy = tmp_path / ".solo-ai"
    policy.mkdir()
    for name in ("config.toml", "verification.toml"):
        (policy / name).write_text("schema_version = 3\n", encoding="utf-8")
    binary = tmp_path / "test-tool"
    binary.write_bytes(b"first")
    probes = []

    def tool(command, cwd):
        probes.append(command.argv[0])
        return {"version": binary.read_text()}

    monkeypatch.setattr(proof, "_tracked", lambda *args: [])
    monkeypatch.setattr(proof.shutil, "which", lambda *args: str(binary))
    monkeypatch.setattr(proof, "_tool", tool)
    config = SimpleNamespace(normalized=lambda: {})
    commands = [CommandSpec(("test-tool",))]
    cache = {}
    first = proof._shared_inputs(None, tmp_path, commands, config, cache)
    second = proof._shared_inputs(None, tmp_path, commands, config, cache)
    assert first == second
    assert probes == ["git", "uv", "test-tool"]

    binary.write_bytes(b"changed executable")
    changed = proof._shared_inputs(None, tmp_path, commands, config, cache)
    assert changed != first
    assert len(probes) == 6
    proof._shared_inputs(None, tmp_path, commands, config, {})
    assert len(probes) == 9, "新一轮必须重新探测，不能跨轮缓存工具版本"


def test_unknown_tool_identity_never_caches_probe(tmp_path, monkeypatch) -> None:
    policy = tmp_path / ".solo-ai"
    policy.mkdir()
    for name in ("config.toml", "verification.toml"):
        (policy / name).write_text("schema_version = 3\n", encoding="utf-8")
    probes = []
    monkeypatch.setattr(proof, "_tracked", lambda *args: [])
    monkeypatch.setattr(
        proof.shutil, "which", lambda *args: str(tmp_path / "missing-tool")
    )

    def tool(*args):
        probes.append(True)
        return {"version": None}

    monkeypatch.setattr(proof, "_tool", tool)
    config = SimpleNamespace(normalized=lambda: {})
    cache = {}
    proof._shared_inputs(None, tmp_path, [], config, cache)
    proof._shared_inputs(None, tmp_path, [], config, cache)
    assert len(probes) == 4
    assert not cache
