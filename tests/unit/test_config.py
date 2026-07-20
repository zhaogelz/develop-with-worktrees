from pathlib import Path

from solo_ai.config import discover_validation_commands, render_repo_config


def test_discovers_uv_pytest(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname="x"\n', encoding="utf-8")
    (tmp_path / "tests").mkdir()
    assert discover_validation_commands(tmp_path) == ["uv run pytest"]


def test_renders_port_placeholder() -> None:
    rendered = render_repo_config()
    assert "{port}" in rendered
    assert "slots = 3" in rendered
