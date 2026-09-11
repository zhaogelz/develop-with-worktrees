"""验证仓库中的文本配置与本地文档、网页资源引用。"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).parents[1].resolve()
TEXT_SUFFIXES = {".css", ".html", ".js", ".json", ".md", ".toml", ".yaml", ".yml"}
MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")
HTML_LINK = re.compile(r"\b(?:href|src)=[\"']([^\"']+)[\"']", re.IGNORECASE)
URL_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)


def tracked_paths() -> list[Path]:
    completed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [
        ROOT / value.decode("utf-8") for value in completed.stdout.split(b"\0") if value
    ]


def local_target(document: Path, raw: str) -> Path | None:
    value = raw.strip().strip("<>")
    if not value or value.startswith("#") or URL_SCHEME.match(value):
        return None
    target = unquote(value.split("#", maxsplit=1)[0].split(maxsplit=1)[0])
    if not target:
        return None
    resolved = (document.parent / target).resolve()
    try:
        resolved.relative_to(ROOT)
    except ValueError as error:
        raise ValueError(f"local reference leaves repository: {raw}") from error
    return resolved


def check_references(document: Path, pattern: re.Pattern[str]) -> list[str]:
    failures: list[str] = []
    content = document.read_text(encoding="utf-8")
    for raw in pattern.findall(content):
        try:
            target = local_target(document, raw)
        except ValueError as error:
            failures.append(f"{document.relative_to(ROOT)}: {error}")
            continue
        if target is not None and not target.exists():
            failures.append(
                f"{document.relative_to(ROOT)}: missing local reference {raw}"
            )
    return failures


def main() -> int:
    failures: list[str] = []
    for path in tracked_paths():
        if path.suffix not in TEXT_SUFFIXES:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            failures.append(f"{path.relative_to(ROOT)}: expected UTF-8 text")
            continue
        if path.suffix == ".json":
            try:
                json.loads(content)
            except json.JSONDecodeError as error:
                failures.append(f"{path.relative_to(ROOT)}: invalid JSON: {error.msg}")
        elif path.suffix == ".toml":
            try:
                tomllib.loads(content)
            except tomllib.TOMLDecodeError as error:
                failures.append(f"{path.relative_to(ROOT)}: invalid TOML: {error}")
        elif path.suffix == ".md":
            failures.extend(check_references(path, MARKDOWN_LINK))
        elif path.suffix == ".html":
            failures.extend(check_references(path, HTML_LINK))
    if failures:
        print("Repository static validation failed:", file=sys.stderr)
        print("\n".join(f"- {failure}" for failure in failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
