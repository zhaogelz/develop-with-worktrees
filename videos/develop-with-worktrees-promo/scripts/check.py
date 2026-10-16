"""检查视频字体覆盖，并兼容 Windows 的 npm.cmd 启动工程检查。"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path


class VisibleText(HTMLParser):
    """收集屏幕文字，跳过 CSS 和 JavaScript 内容。"""

    def __init__(self) -> None:
        super().__init__()
        self.hidden = 0
        self.characters: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"style", "script"}:
            self.hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"style", "script"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.characters.update(char for char in data if not char.isspace())


def check_font_coverage(project: Path) -> int:
    fonts = project / "assets" / "fonts"
    manifest = json.loads((fonts / "font-subset.json").read_text(encoding="utf-8"))
    font = (fonts / "NotoSerifSC-400.ttf").read_bytes()
    if hashlib.sha256(font).hexdigest() != manifest["sha256"]:
        raise ValueError("字体文件与字形覆盖清单不匹配，请重新核对字体子集。")
    parser = VisibleText()
    parser.feed((project / "index.html").read_text(encoding="utf-8"))
    missing = sorted(parser.characters - set(manifest["characters"]))
    if missing:
        raise ValueError("视频新增字符缺少已核验的字形：" + "".join(missing))
    return len(parser.characters)


def main() -> int:
    project = Path(__file__).resolve().parents[1]
    try:
        count = check_font_coverage(project)
    except (OSError, ValueError, KeyError) as error:
        print(f"字体检查失败：{error}", file=sys.stderr)
        return 1
    print(f"字体覆盖检查通过：{count} 个画面字符。", flush=True)
    # Windows 的 CreateProcess 不会自动把 npm 解析为 npm.cmd。
    npm = shutil.which("npm")
    if npm is None:
        print("视频检查需要 Node.js 22+ 和 npm。", file=sys.stderr)
        return 1
    return subprocess.run(
        [npm, "run", "check", "--", *sys.argv[1:]],
        cwd=project,
        check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
