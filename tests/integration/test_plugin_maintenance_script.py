from __future__ import annotations

import os
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).parents[2] / "plugins" / "develop-with-worktrees"
SCRIPT = PLUGIN_ROOT / "maintain-dww-plugin.ps1"


def _fake_uv_for_self_contained_helpers(tmp_path: Path) -> str:
    """模拟调用临时脚本，避免假辅助脚本触发真实包下载。"""
    runner = tmp_path / "fake-uv.py"
    runner.write_text(
        "import subprocess, sys\n"
        "args = sys.argv[1:]\n"
        "if not args or args[0] != 'run' or '--script' not in args:\n"
        "    raise SystemExit(2)\n"
        "index = args.index('--script')\n"
        "raise SystemExit(subprocess.call([sys.executable, args[index + 1], *args[index + 2:]]))\n",
        encoding="utf-8",
    )
    (tmp_path / "uv.cmd").write_text(
        f'@"{sys.executable}" "%~dp0fake-uv.py" %*\r\n', encoding="utf-8"
    )
    return f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}"


@pytest.mark.dww_fast
@pytest.mark.skipif(
    os.name != "nt", reason="PowerShell maintenance path is Windows-only"
)
def test_plugin_maintenance_script_has_a_single_stable_entrypoint() -> None:
    """正式入口随插件安装，且不能退回源码顶层脚本或缓存猜测。"""
    assert SCRIPT.is_file()
    assert SCRIPT.parent == PLUGIN_ROOT
    assert not (Path(__file__).parents[2] / "scripts" / SCRIPT.name).exists()
    source = SCRIPT.read_text(encoding="utf-8")
    assert "update_plugin_cachebuster.py" in source
    assert "create_basic_plugin.py" in source
    assert "validate_plugin.py" in source
    assert "git archive" in source
    assert "plugins\\cache" not in source.casefold()
    assert "'marketplace', 'remove'" in source
    assert "-MigrateMarketplace" in source
    assert "active-release.json" in source
    assert ".previous-release" in source
    assert "Join-Path $MarketplaceRoot 'releases'" not in source
    assert "未决的 .stage-*" in source
    assert "active-release 与本次精确源码不匹配" in source
    assert source.index("Write-Receipt $activeReceipt $newReceipt") < source.index(
        "Remove-Item -LiteralPath $stage"
    )
    assert source.index("installed-by-cli") < source.index(
        "Remove-Item -LiteralPath $previous"
    )
    assert "PluginCreatorRoot" not in source.split("param(", 1)[1].split(")", 1)[0]


@pytest.mark.dww_fast
@pytest.mark.skipif(
    os.name != "nt", reason="PowerShell maintenance path is Windows-only"
)
def test_plugin_maintenance_script_rechecks_unknown_switch_and_resumes(
    tmp_path: Path,
) -> None:
    """假 CLI 在切换输出丢失后仍可回读，重复执行不再反复切市场或安装。"""
    assert shutil.which("pwsh"), "Windows test environment must provide PowerShell"
    source = tmp_path / "source"
    source_plugin = source / "plugins" / "develop-with-worktrees"
    (source_plugin / ".codex-plugin").mkdir(parents=True)
    (source_plugin / ".codex-plugin" / "plugin.json").write_text(
        json.dumps({"name": "develop-with-worktrees", "version": "0.5.0-beta.7"}),
        encoding="utf-8",
    )

    def git(*args: str) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=source,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=True,
        )
        return completed.stdout.strip()

    git("init", "-b", "main")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "DWW test")
    git("add", ".")
    git("commit", "-m", "test: create local plugin source")
    commit = git("rev-parse", "HEAD")
    tree = git("rev-parse", f"{commit}:plugins/develop-with-worktrees")

    market = tmp_path / "market"
    active = market / "plugins" / "develop-with-worktrees"
    (active / ".codex-plugin").mkdir(parents=True)
    version = "0.5.0-beta.7+codex.fake"
    old_version = "0.5.0-beta.7+codex.old"
    (active / ".codex-plugin" / "plugin.json").write_text(
        json.dumps({"name": "develop-with-worktrees", "version": old_version}),
        encoding="utf-8",
    )
    (active / "old-release-marker.txt").write_text("old\n", encoding="utf-8")
    marketplace = market / ".agents" / "plugins"
    marketplace.mkdir(parents=True)
    (marketplace / "marketplace.json").write_text(
        json.dumps(
            {
                "name": "dww-stable-local",
                "plugins": [{"name": "develop-with-worktrees"}],
            }
        ),
        encoding="utf-8",
    )
    (market / "active-release.json").write_text(
        json.dumps(
            {
                "release_id": "old-release",
                "source_commit": "0" * 40,
                "source_tree": "1" * 40,
                "package_version": old_version,
            }
        ),
        encoding="utf-8",
    )
    codex_home = tmp_path / "codex-home"
    helpers = codex_home / "skills" / ".system" / "plugin-creator" / "scripts"
    helpers.mkdir(parents=True)
    helper_log = tmp_path / "helper.log"
    (helpers / "create_basic_plugin.py").write_text(
        """from pathlib import Path
import os
import sys

target = Path(sys.argv[sys.argv.index('--path') + 1]) / 'develop-with-worktrees'
target.mkdir(parents=True, exist_ok=True)
with Path(os.environ['DWW_FAKE_HELPER_LOG']).open('a', encoding='utf-8') as handle:
    handle.write('create\\n')
""",
        encoding="utf-8",
    )
    (helpers / "update_plugin_cachebuster.py").write_text(
        """import json
from pathlib import Path
import os
import sys

manifest = Path(sys.argv[1]) / '.codex-plugin' / 'plugin.json'
payload = json.loads(manifest.read_text(encoding='utf-8'))
payload['version'] = payload['version'].split('+', 1)[0] + '+codex.fake'
manifest.write_text(json.dumps(payload), encoding='utf-8')
with Path(os.environ['DWW_FAKE_HELPER_LOG']).open('a', encoding='utf-8') as handle:
    handle.write('cachebuster\\n')
""",
        encoding="utf-8",
    )
    (helpers / "validate_plugin.py").write_text(
        """from pathlib import Path
import os

with Path(os.environ['DWW_FAKE_HELPER_LOG']).open('a', encoding='utf-8') as handle:
    handle.write('validate\\n')
""",
        encoding="utf-8",
    )

    state = tmp_path / "fake-state.json"
    state.write_text(json.dumps({"marketplaces": [], "plugins": []}), encoding="utf-8")
    fake = tmp_path / "fake-codex.py"
    fake.write_text(
        """import json, os, pathlib, sys
state_path = pathlib.Path(os.environ['DWW_FAKE_CODEX_STATE'])
log_path = pathlib.Path(os.environ['DWW_FAKE_CODEX_LOG'])
state = json.loads(state_path.read_text(encoding='utf-8'))
args = sys.argv[1:]
log_path.write_text(log_path.read_text(encoding='utf-8') + ' '.join(args) + '\\n' if log_path.exists() else ' '.join(args) + '\\n', encoding='utf-8')
if args == ['--version']:
    print('codex fake 1.0')
elif args == ['plugin', 'marketplace', 'list', '--json']:
    print(json.dumps({'marketplaces': state['marketplaces']}))
elif args[:3] == ['plugin', 'marketplace', 'add']:
    if os.environ.get('DWW_FAKE_CODEX_FAIL_MARKET_ADD') == '1':
        print('market switch failed', file=sys.stderr)
        raise SystemExit(8)
    state['marketplaces'] = [{'name': 'dww-stable-local', 'root': args[3]}]
    state_path.write_text(json.dumps(state), encoding='utf-8')
    print('output lost after successful switch', file=sys.stderr)
    raise SystemExit(7)
elif args == ['plugin', 'list', '--marketplace', 'dww-stable-local', '--json']:
    print(json.dumps({'plugins': state['plugins']}))
elif args[:2] == ['plugin', 'add']:
    if os.environ.get('DWW_FAKE_CODEX_FAIL_PLUGIN_ADD') == '1':
        print('plugin install failed', file=sys.stderr)
        raise SystemExit(6)
    state['plugins'] = [{'name': 'develop-with-worktrees', 'version': '0.5.0-beta.7+codex.fake'}]
    state_path.write_text(json.dumps(state), encoding='utf-8')
    print(json.dumps({'installed': True}))
else:
    print('unsupported fake command: ' + repr(args), file=sys.stderr)
    raise SystemExit(9)
""",
        encoding="utf-8",
    )
    codex = tmp_path / "fake-codex.cmd"
    codex.write_text(
        f'@"{sys.executable}" "%~dp0fake-codex.py" %*\r\n', encoding="utf-8"
    )
    log = tmp_path / "fake-codex.log"
    env = {
        **os.environ,
        "PATH": _fake_uv_for_self_contained_helpers(tmp_path),
        "CODEX_HOME": str(codex_home),
        "DWW_FAKE_CODEX_STATE": str(state),
        "DWW_FAKE_CODEX_LOG": str(log),
        "DWW_FAKE_HELPER_LOG": str(helper_log),
    }
    command = [
        "pwsh",
        "-NoProfile",
        "-File",
        str(SCRIPT),
        "-Mode",
        "Install",
        "-SourceRepo",
        str(source),
        "-SourceCommit",
        commit,
        "-CodexPath",
        str(codex),
        "-MarketplaceRoot",
        str(market),
    ]
    failed_switch = subprocess.run(
        command,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        timeout=30,
        env={**env, "DWW_FAKE_CODEX_FAIL_MARKET_ADD": "1"},
    )
    assert failed_switch.returncode != 0
    assert json.loads(state.read_text(encoding="utf-8"))["marketplaces"] == []
    active_receipt = json.loads(
        (market / "active-release.json").read_text(encoding="utf-8")
    )
    assert active_receipt["source_commit"] == commit
    assert active_receipt["source_tree"] == tree
    assert (
        market
        / ".previous-release"
        / "plugins"
        / "develop-with-worktrees"
        / "old-release-marker.txt"
    ).is_file()
    assert not list(market.glob(".stage-*"))

    failed_install = subprocess.run(
        command,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        timeout=30,
        env={**env, "DWW_FAKE_CODEX_FAIL_PLUGIN_ADD": "1"},
    )
    assert failed_install.returncode != 0
    verification = json.loads(
        (market / "install-verification.json").read_text(encoding="utf-8")
    )
    assert verification["status"] == "plugin-install-failed"
    assert (market / ".previous-release").is_dir()
    assert not list(market.glob(".stage-*"))

    completed = subprocess.run(
        command,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        timeout=30,
        env=env,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(state.read_text(encoding="utf-8"))["plugins"] == [
        {"name": "develop-with-worktrees", "version": version},
    ]
    assert not (market / ".previous-release").exists()
    assert not list(market.glob(".stage-*"))
    assert helper_log.read_text(encoding="utf-8").splitlines() == [
        "create",
        "cachebuster",
        "validate",
    ]
    first_log = log.read_text(encoding="utf-8")
    assert first_log.count("plugin marketplace add") == 2
    assert first_log.count("plugin add develop-with-worktrees@dww-stable-local") == 2

    resumed = subprocess.run(
        command,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        timeout=30,
        env=env,
    )
    assert resumed.returncode == 0, resumed.stderr
    resumed_log = log.read_text(encoding="utf-8")
    assert resumed_log.count("plugin marketplace add") == 2
    assert resumed_log.count("plugin add develop-with-worktrees@dww-stable-local") == 2

    previous = market / ".previous-release"
    previous.mkdir()
    marker = previous / "preserve-me.txt"
    marker.write_text("do not overwrite\n", encoding="utf-8")
    (market / "active-release.json").write_text(
        json.dumps({"source_commit": "2" * 40, "source_tree": "3" * 40}),
        encoding="utf-8",
    )
    blocked_previous = subprocess.run(
        command,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        timeout=30,
        env=env,
    )
    assert blocked_previous.returncode != 0
    blocked_output = blocked_previous.stdout + blocked_previous.stderr
    assert "DWW_LOCAL_PUBLISH_ERROR" in blocked_output
    assert "active-release" in blocked_output
    assert marker.read_text(encoding="utf-8") == "do not overwrite\n"
    shutil.rmtree(previous)

    stage = market / ".stage-interrupted"
    stage.mkdir()
    marker = stage / "preserve-me.txt"
    marker.write_text("do not overwrite\n", encoding="utf-8")
    blocked_stage = subprocess.run(
        command,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        timeout=30,
        env=env,
    )
    assert blocked_stage.returncode != 0
    assert "未决的 .stage-*" in blocked_stage.stdout + blocked_stage.stderr
    assert marker.read_text(encoding="utf-8") == "do not overwrite\n"


@pytest.mark.dww_fast
@pytest.mark.skipif(
    os.name != "nt", reason="PowerShell maintenance path is Windows-only"
)
def test_maintenance_script_preserves_legacy_releases_and_check_is_read_only(
    tmp_path: Path,
) -> None:
    """唯一普通 legacy releases 可初始化；Check 只报告布局，绝不改写。"""
    assert shutil.which("pwsh"), "Windows test environment must provide PowerShell"
    source = tmp_path / "source"
    source_plugin = source / "plugins" / "develop-with-worktrees"
    (source_plugin / ".codex-plugin").mkdir(parents=True)
    (source_plugin / ".codex-plugin" / "plugin.json").write_text(
        json.dumps({"name": "develop-with-worktrees", "version": "0.5.0-beta.7"}),
        encoding="utf-8",
    )

    def git(*args: str) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=source,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=True,
        )
        return completed.stdout.strip()

    git("init", "-b", "main")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "DWW test")
    git("add", ".")
    git("commit", "-m", "test: create legacy release source")
    commit = git("rev-parse", "HEAD")

    market = tmp_path / "market"
    legacy = market / "releases"
    legacy.mkdir(parents=True)
    legacy_marker = legacy / "preserve-me.txt"
    legacy_marker.write_text("legacy\n", encoding="utf-8")
    codex_home = tmp_path / "codex-home"
    helpers = codex_home / "skills" / ".system" / "plugin-creator" / "scripts"
    helpers.mkdir(parents=True)
    (helpers / "create_basic_plugin.py").write_text(
        """import json
from pathlib import Path
import sys

arguments = sys.argv
target = Path(arguments[arguments.index('--path') + 1]) / 'develop-with-worktrees'
target.mkdir(parents=True, exist_ok=True)
if '--marketplace-path' in arguments:
    manifest = Path(arguments[arguments.index('--marketplace-path') + 1])
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({
        'name': 'dww-stable-local',
        'plugins': [{'name': 'develop-with-worktrees'}],
    }), encoding='utf-8')
""",
        encoding="utf-8",
    )
    (helpers / "update_plugin_cachebuster.py").write_text(
        """import json
from pathlib import Path
import sys

manifest = Path(sys.argv[1]) / '.codex-plugin' / 'plugin.json'
payload = json.loads(manifest.read_text(encoding='utf-8'))
payload['version'] = payload['version'].split('+', 1)[0] + '+codex.legacy'
manifest.write_text(json.dumps(payload), encoding='utf-8')
""",
        encoding="utf-8",
    )
    (helpers / "validate_plugin.py").write_text(
        "# valid test helper\n", encoding="utf-8"
    )

    state = tmp_path / "fake-state.json"
    state.write_text(json.dumps({"marketplaces": [], "plugins": []}), encoding="utf-8")
    fake = tmp_path / "fake-codex.py"
    fake.write_text(
        """import json, os, pathlib, sys
state_path = pathlib.Path(os.environ['DWW_FAKE_CODEX_STATE'])
state = json.loads(state_path.read_text(encoding='utf-8'))
args = sys.argv[1:]
if args == ['--version']:
    print('codex fake 1.0')
elif args == ['plugin', 'marketplace', 'list', '--json']:
    print(json.dumps({'marketplaces': state['marketplaces']}))
elif args[:3] == ['plugin', 'marketplace', 'add']:
    state['marketplaces'] = [{'name': 'dww-stable-local', 'root': args[3]}]
    state_path.write_text(json.dumps(state), encoding='utf-8')
    print(json.dumps({'added': True}))
elif args == ['plugin', 'list', '--marketplace', 'dww-stable-local', '--json']:
    print(json.dumps({'plugins': state['plugins']}))
elif args[:2] == ['plugin', 'add']:
    state['plugins'] = [{
        'name': 'develop-with-worktrees',
        'version': '0.5.0-beta.7+codex.legacy',
    }]
    state_path.write_text(json.dumps(state), encoding='utf-8')
    print(json.dumps({'installed': True}))
else:
    raise SystemExit(9)
""",
        encoding="utf-8",
    )
    codex = tmp_path / "fake-codex.cmd"
    codex.write_text(
        f'@"{sys.executable}" "%~dp0fake-codex.py" %*\r\n', encoding="utf-8"
    )
    env = {
        **os.environ,
        "PATH": _fake_uv_for_self_contained_helpers(tmp_path),
        "CODEX_HOME": str(codex_home),
        "DWW_FAKE_CODEX_STATE": str(state),
    }
    installed = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-File",
            str(SCRIPT),
            "-Mode",
            "Install",
            "-SourceRepo",
            str(source),
            "-SourceCommit",
            commit,
            "-CodexPath",
            str(codex),
            "-MarketplaceRoot",
            str(market),
        ],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        timeout=30,
        env=env,
    )
    assert installed.returncode == 0, installed.stderr
    assert legacy_marker.read_text(encoding="utf-8") == "legacy\n"
    assert (market / ".agents" / "plugins" / "marketplace.json").is_file()
    assert (market / "active-release.json").is_file()

    def snapshot(root: Path) -> list[tuple[str, bytes]]:
        return sorted(
            (str(path.relative_to(root)), path.read_bytes())
            for path in root.rglob("*")
            if path.is_file()
        )

    before_check = snapshot(market)
    checked = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-File",
            str(SCRIPT),
            "-Mode",
            "Check",
            "-MarketplaceRoot",
            str(market),
        ],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        timeout=30,
        env=env,
    )
    assert checked.returncode == 0, checked.stderr
    assert "布局可接受" in checked.stdout
    assert "未执行 CLI 或写入" in checked.stdout
    assert snapshot(market) == before_check

    unknown = tmp_path / "unknown-market"
    unknown.mkdir()
    (unknown / "unexpected.txt").write_text("unknown\n", encoding="utf-8")
    rejected = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-File",
            str(SCRIPT),
            "-Mode",
            "Check",
            "-MarketplaceRoot",
            str(unknown),
        ],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        timeout=30,
        env=env,
    )
    assert rejected.returncode != 0
    assert "未知、链接或混合内容" in rejected.stdout + rejected.stderr
