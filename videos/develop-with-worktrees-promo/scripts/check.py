"""从仓库验证入口运行视频工程检查，兼容 Windows 的 npm.cmd。"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def main() -> int:
    # Windows 的 CreateProcess 不会自动把 npm 解析为 npm.cmd。
    npm = shutil.which("npm")
    if npm is None:
        print("视频检查需要 Node.js 22+ 和 npm。", file=sys.stderr)
        return 1
    project = Path(__file__).resolve().parents[1]
    return subprocess.run(
        [npm, "run", "check", "--", *sys.argv[1:]],
        cwd=project,
        check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
