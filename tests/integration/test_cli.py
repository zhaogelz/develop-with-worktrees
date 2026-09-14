from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tomllib
from pathlib import Path

from conftest import declare_delegated_adapter, git
from solo_ai import __version__
from solo_ai.cli import _human
from solo_ai.state import STATE_SCHEMA


def test_human_batch_output_accepts_direct_and_reconcile_results() -> None:
    batch = {
        "id": "batch-one",
        "status": "completed",
        "integrated_head": "a" * 40,
        "candidate_ids": ["candidate-one"],
        "trigger": "quiet_tail",
    }

    direct = _human("batch", batch)
    reconciled = _human(
        "batch",
        {"status": "completed", "batch": batch, "delivered": True},
    )

    assert direct == reconciled
    assert direct == (
        f"Integrated batch batch-one at {'a' * 40} from 1 candidate(s) (quiet_tail)."
    )


def test_human_recover_output_accepts_candidate_publication_without_a_lease() -> None:
    result = {
        "task_id": "task-published",
        "status": "candidate-published",
        "candidate_id": "candidate-published",
        "candidate_head": "a" * 40,
    }

    rendered = _human("recover", result)

    assert rendered == (
        "Task: task-published\n"
        "Status: candidate published; awaiting integration\n"
        f"Candidate: candidate-published at {'a' * 40}"
    )
    assert "Lease:" not in rendered


def test_human_recover_output_uses_id_for_idempotent_candidate_publication() -> None:
    result = {
        "id": "task-published",
        "status": "candidate-published",
        "candidate_id": "candidate-published",
        "candidate_head": "a" * 40,
    }

    rendered = _human("recover", result)

    assert rendered == (
        "Task: task-published\n"
        "Status: candidate published; awaiting integration\n"
        f"Candidate: candidate-published at {'a' * 40}"
    )
    assert "Lease:" not in rendered


def test_runtime_adapter_repair_cli_requires_an_exact_path(git_repo: Path) -> None:
    runner = (
        Path(__file__).parents[2]
        / "plugins"
        / "develop-with-worktrees"
        / "skills"
        / "develop-with-worktrees"
        / "scripts"
        / "dww.py"
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(runner),
            "--repo",
            str(git_repo),
            "recover",
            "--task",
            "task-missing",
            "--repair-runtime-adapter",
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "requires at least one exact --path" in completed.stderr


def test_release_version_contract_matches_manifest_metadata_and_cli(
    git_repo: Path,
) -> None:
    repository_root = Path(__file__).parents[2]
    runner = (
        repository_root
        / "plugins"
        / "develop-with-worktrees"
        / "skills"
        / "develop-with-worktrees"
        / "scripts"
        / "dww.py"
    )
    completed = subprocess.run(
        [sys.executable, str(runner), "--repo", str(git_repo), "--json", "version"],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)["result"]
    manifest = json.loads(
        (
            repository_root
            / "plugins"
            / "develop-with-worktrees"
            / ".codex-plugin"
            / "plugin.json"
        ).read_text(encoding="utf-8")
    )
    pyproject = tomllib.loads(
        (repository_root / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert payload["version"] == "0.5.0-beta.1"
    plugin_version = payload["plugin_version"]
    assert plugin_version == manifest["version"]
    if plugin_version != payload["version"]:
        cachebuster_prefix = f"{payload['version']}+codex."
        assert plugin_version.startswith(cachebuster_prefix)
        assert plugin_version.removeprefix(cachebuster_prefix)
    assert payload["version"] == pyproject["project"]["version"]
    assert payload["version"] == __version__
    assert f"## {payload['version']}" in (repository_root / "CHANGELOG.md").read_text(
        encoding="utf-8"
    )
    assert payload["verification_schema"] == 3
    assert payload["state_schema"] == STATE_SCHEMA
    assert "PreToolUse deny" in payload["codex_guard"]
    assert Path(payload["script"]).name == "dww.py"


def test_plugin_manifest_matches_current_codex_component_contract() -> None:
    repository_root = Path(__file__).parents[2]
    plugin_root = repository_root / "plugins" / "develop-with-worktrees"
    manifest = json.loads(
        (plugin_root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )

    skills_path = manifest["skills"]
    assert isinstance(skills_path, str)
    assert skills_path.startswith("./")
    assert (plugin_root / skills_path[2:]).is_dir()

    prompts = manifest["interface"]["defaultPrompt"]
    assert isinstance(prompts, list)
    assert 1 <= len(prompts) <= 3
    assert all(isinstance(prompt, str) and len(prompt) <= 128 for prompt in prompts)


def test_hook_definition_remains_the_stable_trust_contract() -> None:
    repository_root = Path(__file__).parents[2]
    hook_definition = (
        repository_root / "plugins" / "develop-with-worktrees" / "hooks" / "hooks.json"
    )

    assert (
        hashlib.sha256(hook_definition.read_bytes()).hexdigest()
        == "f05bfe3e90b2fbf93a7ae7003caaf840e38166a86df8bbbaf0b7d9b427d7e606"
    )


def test_user_facing_docs_describe_only_the_current_contract() -> None:
    repository_root = Path(__file__).parents[2]
    documents = [
        repository_root / "README.md",
        repository_root / "README.zh-CN.md",
        repository_root / "CHANGELOG.md",
        repository_root / "总体规划.md",
        repository_root
        / "plugins"
        / "develop-with-worktrees"
        / "skills"
        / "develop-with-worktrees"
        / "SKILL.md",
        repository_root
        / "plugins"
        / "develop-with-worktrees"
        / "skills"
        / "develop-with-worktrees"
        / "references"
        / "delegated-migration.md",
    ]
    text = "\n".join(path.read_text(encoding="utf-8") for path in documents)
    assert "0.1.0-beta.2" not in text
    assert "01..05" not in text
    assert "schema 3 only" in text
    assert "machine-global weighted FIFO" in text
    assert "--json route" in text
    assert "mature workflow" in text
    assert "native task/subagent" in text
    assert "drain-only" in text
    assert "optional hardening" in text
    assert "hooks/hooks.json" in text
    assert 'integration.mode = "batched"' in text
    assert 'seal_policy = "auto_full"' in text
    assert "Every three candidates" in text
    assert "每满 3 个候选" in text
    assert "batch seal" in text
    assert "candidate_capacity = 10" in text
    assert "task anchor" in text
    assert "任务锚点" in text
    assert "root-anchor" in text
    assert "complete plan" in text
    assert "root-anchor accept" in text
    assert "不再是多 AI 任务指挥中心" in text
    assert "Dual-run rules" in text
    assert "delegated revoke" in text
    assert not (repository_root / "需求.md").exists()
    assert not (repository_root / "方案.md").exists()
    assert "After every install or hook change" not in text
    assert "每次安装或钩子升级后" not in text


def test_cli_json_status_masks_uninitialized_state(git_repo: Path) -> None:
    runner = (
        Path(__file__).parents[2]
        / "plugins"
        / "develop-with-worktrees"
        / "skills"
        / "develop-with-worktrees"
        / "scripts"
        / "dww.py"
    )
    completed = subprocess.run(
        [sys.executable, str(runner), "--repo", str(git_repo), "--json", "status"],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["ok"] is True
    assert payload["result"]["mode"] == "uninitialized"
    assert "lease" not in json.dumps(payload["result"])


def test_cli_route_is_compact_and_read_only_for_mature_workflow(
    git_repo: Path,
) -> None:
    runner = (
        Path(__file__).parents[2]
        / "plugins"
        / "develop-with-worktrees"
        / "skills"
        / "develop-with-worktrees"
        / "scripts"
        / "dww.py"
    )
    marker = git_repo / "scripts" / "worktree-flow.ps1"
    marker.parent.mkdir()
    marker.write_text("# existing\n", encoding="utf-8")
    before = git(git_repo, "status", "--porcelain")

    completed = subprocess.run(
        [
            "uv",
            "run",
            "--script",
            str(runner),
            "--repo",
            str(git_repo),
            "--json",
            "route",
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=90,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload == {
        "ok": True,
        "result": {
            "action": "defer",
            "reason": "existing-workflow",
            "workflows": ["repository worktree-flow"],
        },
    }
    assert git(git_repo, "status", "--porcelain") == before
    assert not (git_repo / ".solo-ai").exists()


def test_cli_approves_and_invokes_only_the_exact_delegated_contract(
    git_repo: Path,
) -> None:
    runner = (
        Path(__file__).parents[2]
        / "plugins"
        / "develop-with-worktrees"
        / "skills"
        / "develop-with-worktrees"
        / "scripts"
        / "dww.py"
    )
    declare_delegated_adapter(
        git_repo,
        max_parallel=2,
        capabilities=("start", "status"),
        approve=False,
    )

    inspect = subprocess.run(
        [
            sys.executable,
            str(runner),
            "--repo",
            str(git_repo),
            "--json",
            "delegated",
            "inspect",
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    assert inspect.returncode == 0, inspect.stderr
    fingerprint = json.loads(inspect.stdout)["result"]["adapter"]["fingerprint"]
    before_approval = subprocess.run(
        [sys.executable, str(runner), "--repo", str(git_repo), "--json", "route"],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    assert json.loads(before_approval.stdout)["result"]["action"] == "defer"

    approval = subprocess.run(
        [
            sys.executable,
            str(runner),
            "--repo",
            str(git_repo),
            "--json",
            "delegated",
            "approve",
            "--fingerprint",
            fingerprint,
            "--accept",
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    assert approval.returncode == 0, approval.stderr
    routed = subprocess.run(
        [sys.executable, str(runner), "--repo", str(git_repo), "--json", "route"],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    assert json.loads(routed.stdout)["result"]["action"] == "delegated"

    invoked = subprocess.run(
        [
            sys.executable,
            str(runner),
            "--repo",
            str(git_repo),
            "--json",
            "delegated",
            "invoke",
            "--operation",
            "status",
            "--request",
            "{}",
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=90,
    )
    assert invoked.returncode == 0, invoked.stderr
    response = json.loads(invoked.stdout)["result"]
    assert response["ok"] is True
    assert response["result"] == {"available_slots": 2}

    revoked = subprocess.run(
        [
            sys.executable,
            str(runner),
            "--repo",
            str(git_repo),
            "--json",
            "delegated",
            "revoke",
            "--adapter-id",
            "example-worktree-flow",
            "--fingerprint",
            fingerprint,
            "--confirm",
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    assert revoked.returncode == 0, revoked.stderr
    assert json.loads(revoked.stdout)["result"]["revoked"] is True
    after_revoke = subprocess.run(
        [sys.executable, str(runner), "--repo", str(git_repo), "--json", "route"],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    assert json.loads(after_revoke.stdout)["result"]["action"] == "defer"


def test_cli_init_only_shows_plan_until_acceptance(git_repo: Path) -> None:
    runner = (
        Path(__file__).parents[2]
        / "plugins"
        / "develop-with-worktrees"
        / "skills"
        / "develop-with-worktrees"
        / "scripts"
        / "dww.py"
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(runner),
            "--repo",
            str(git_repo),
            "--json",
            "init",
            "--verify",
            '["git","status","--short"]',
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["result"]["decision"] == "needs-approval"
    assert {
        "profiles",
        "dependency_inputs",
        "platform_condition",
        "cross_task_policy",
    } <= set(payload["result"]["plan"])
    assert not (git_repo / ".solo-ai").exists()


def test_cli_init_accepts_a_reviewed_verification_file(
    git_repo: Path, tmp_path: Path
) -> None:
    runner = (
        Path(__file__).parents[2]
        / "plugins"
        / "develop-with-worktrees"
        / "skills"
        / "develop-with-worktrees"
        / "scripts"
        / "dww.py"
    )
    reviewed = tmp_path / "reviewed-verification.toml"
    reviewed.write_text(
        """schema_version = 3
static_only = false

[[profiles]]
id = "reviewed-full"
paths = ["**"]
input_paths = ["**"]
input_closure = "declared"
cross_task_reuse = false
external_state = "unknown"
environment = []
timeout_seconds = 120
resource_class = "normal"
level = "full"
full_scope = "integration"
commands = [["git", "diff", "--check", "main...HEAD"]]
""",
        encoding="utf-8",
    )

    preview = subprocess.run(
        [
            sys.executable,
            str(runner),
            "--repo",
            str(git_repo),
            "--json",
            "init",
            "--verification-file",
            str(reviewed),
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    assert preview.returncode == 0, preview.stderr
    plan = json.loads(preview.stdout)["result"]["plan"]
    assert plan["validation_source"].startswith("reviewed verification file:")
    assert plan["profiles"][0]["id"] == "reviewed-full"
    assert plan["profiles"][0]["level"] == "full"

    accepted = subprocess.run(
        [
            sys.executable,
            str(runner),
            "--repo",
            str(git_repo),
            "--json",
            "init",
            "--accept",
            "--verification-file",
            str(reviewed),
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    assert accepted.returncode == 0, accepted.stderr
    assert json.loads(accepted.stdout)["result"]["decision"] == "adopted"
    assert (git_repo / ".solo-ai" / "verification.toml").read_text(
        encoding="utf-8"
    ) == reviewed.read_text(encoding="utf-8")


def test_cli_choose_isolated_accepts_a_reviewed_verification_file(
    git_repo: Path, tmp_path: Path
) -> None:
    runner = (
        Path(__file__).parents[2]
        / "plugins"
        / "develop-with-worktrees"
        / "skills"
        / "develop-with-worktrees"
        / "scripts"
        / "dww.py"
    )
    reviewed = tmp_path / "reviewed-verification.toml"
    reviewed.write_text(
        """schema_version = 3
static_only = false

[[profiles]]
id = "reviewed-ready"
paths = ["**"]
input_paths = ["**"]
input_closure = "declared"
cross_task_reuse = false
external_state = "unknown"
environment = []
timeout_seconds = 120
resource_class = "normal"
level = "ready"
commands = [["git", "diff", "--check", "main...HEAD"]]
""",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(runner),
            "--repo",
            str(git_repo),
            "--json",
            "choose",
            "--mode",
            "isolated",
            "--verification-file",
            str(reviewed),
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["result"]["decision"] == "adopted"
    assert (git_repo / ".solo-ai" / "verification.toml").read_text(
        encoding="utf-8"
    ) == reviewed.read_text(encoding="utf-8")


def test_cli_static_only_first_shows_a_plan(git_repo: Path) -> None:
    runner = (
        Path(__file__).parents[2]
        / "plugins"
        / "develop-with-worktrees"
        / "skills"
        / "develop-with-worktrees"
        / "scripts"
        / "dww.py"
    )
    completed = subprocess.run(
        [sys.executable, str(runner), "--repo", str(git_repo), "--json", "init"],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["result"]["decision"] == "needs-approval"
    assert payload["result"]["plan"]["static_only"] is True


def test_cli_choose_current_task_redacts_session_and_delegation_code(
    git_repo: Path,
) -> None:
    runner = (
        Path(__file__).parents[2]
        / "plugins"
        / "develop-with-worktrees"
        / "skills"
        / "develop-with-worktrees"
        / "scripts"
        / "dww.py"
    )
    completed = subprocess.run(
        [
            "uv",
            "run",
            "--script",
            str(runner),
            "--repo",
            str(git_repo),
            "--json",
            "choose",
            "--mode",
            "current-task",
            "--session",
            "private-session",
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=90,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["result"] == {"choice": "current-task", "delegated": False}
    assert "private-session" not in completed.stdout
    assert "delegation_code" not in completed.stdout


def test_cli_plan_and_verify_cover_registered_development_ready_full_and_stress_levels(
    git_repo: Path,
) -> None:
    runner = (
        Path(__file__).parents[2]
        / "plugins"
        / "develop-with-worktrees"
        / "skills"
        / "develop-with-worktrees"
        / "scripts"
        / "dww.py"
    )

    def call(
        *arguments: str, repo_path: Path = git_repo
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "uv",
                "run",
                "--script",
                str(runner),
                "--repo",
                str(repo_path),
                *arguments,
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=90,
        )

    def call_json(*arguments: str, repo_path: Path = git_repo) -> dict:
        completed = call("--json", *arguments, repo_path=repo_path)
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout)["result"]

    call_json(
        "init", "--accept", "--verify", '["git", "diff", "--check", "main...HEAD"]'
    )
    policy = git_repo / ".solo-ai" / "verification.toml"
    policy.write_text(
        """schema_version = 3
static_only = false

[[profiles]]
id = "development"
level = "development"
paths = ["**"]
commands = [["git", "diff", "--check", "main...HEAD"]]

[[profiles]]
id = "ready"
level = "ready"
frozen_base = true
paths = ["**"]
commands = [
  ["git", "diff", "--check", "main...HEAD"],
  ["uv", "run", "python", "-c", "import os; print('DWW_TEST_BASE_REF=' + os.environ['DWW_VALIDATION_BASE_REF']); print('DWW_TEST_BASE_HEAD=' + os.environ['DWW_VALIDATION_BASE_HEAD'])"],
]

[[profiles]]
id = "full"
level = "full"
frozen_base = true
paths = ["**"]
commands = [
  ["git", "diff", "--check", "main...HEAD"],
  ["uv", "run", "python", "-c", "import os; print('DWW_TEST_SCOPE=' + os.environ['DWW_VALIDATION_SCOPE']); print('DWW_TEST_BASE_REF=' + os.environ['DWW_VALIDATION_BASE_REF']); print('DWW_TEST_BASE_HEAD=' + os.environ['DWW_VALIDATION_BASE_HEAD'])"],
]

[[profiles]]
id = "complete"
level = "full"
full_scope = "complete"
frozen_base = true
paths = ["**"]
commands = [
  ["git", "diff", "--check", "main...HEAD"],
  ["uv", "run", "python", "-c", "import os; print('DWW_TEST_SCOPE=' + os.environ['DWW_VALIDATION_SCOPE']); print('DWW_TEST_BASE_REF=' + os.environ['DWW_VALIDATION_BASE_REF']); print('DWW_TEST_BASE_HEAD=' + os.environ['DWW_VALIDATION_BASE_HEAD'])"],
]
""",
        encoding="utf-8",
    )
    (git_repo / ".solo-ai" / "stress-verification.toml").write_text(
        """schema_version = 3
static_only = false

[[profiles]]
id = "stress"
level = "stress"
resource_class = "heavy"
paths = ["levels.txt"]
commands = [["git", "diff", "--check", "main...HEAD"]]
""",
        encoding="utf-8",
    )
    git(git_repo, "add", ".solo-ai")
    git(git_repo, "commit", "-m", "test: configure validation levels")
    call_json("approve", "--accept")
    started = call("start", "--name", "verify levels")
    assert started.returncode == 0, started.stderr
    values = dict(line.split(": ", 1) for line in started.stdout.splitlines())
    task_id = values["Task"]
    lease = values["Lease"]
    worktree = Path(values["Worktree"])
    (worktree / "levels.txt").write_text("levels\n", encoding="utf-8")
    # Stress 仅覆盖其关联的运行时变更；同一候选中的文档不应被错误当成
    # Ready 全路径门禁，否则无法显式运行压力层。
    (worktree / "notes.md").write_text("documentation\n", encoding="utf-8")
    call_json(
        "commit",
        "--task",
        task_id,
        "--lease",
        lease,
        "--message",
        "test: commit validation level fixture",
        "--path",
        "levels.txt",
        "--path",
        "notes.md",
        repo_path=worktree,
    )
    plan = call_json("plan", "--task", task_id, repo_path=worktree)
    assert {profile["level"] for profile in plan["profiles"]} == {
        "development",
        "ready",
        "full",
        "stress",
    }
    development = call_json(
        "verify",
        "--task",
        task_id,
        "--lease",
        lease,
        "--level",
        "development",
        repo_path=worktree,
    )
    development_proof = json.loads(
        (
            git_repo / ".git" / "solo-ai" / "proofs" / f"{development['proof']}.json"
        ).read_text(encoding="utf-8")
    )
    assert [item["profile_id"] for item in development_proof["profile_proofs"]] == [
        "development"
    ]
    planned = {profile["id"]: profile for profile in plan["profiles"]}
    assert len(planned) == len(plan["profiles"])
    assert (
        planned["development"]["fingerprint"]
        == development_proof["profile_proofs"][0]["fingerprint"]
    )
    ready_gate = call_json(
        "ready",
        "--task",
        task_id,
        "--lease",
        lease,
        repo_path=worktree,
    )
    ready_proof = json.loads(
        (
            git_repo
            / ".git"
            / "solo-ai"
            / "proofs"
            / f"{ready_gate['ready_proof']}.json"
        ).read_text(encoding="utf-8")
    )
    environment_lines = {
        key: value
        for line in Path(ready_proof["runs"][-1]["log"])
        .read_text(encoding="utf-8")
        .splitlines()
        if line.startswith("DWW_TEST_")
        for key, value in (line.split("=", 1),)
    }
    assert environment_lines["DWW_TEST_BASE_REF"] == "main"
    assert environment_lines["DWW_TEST_BASE_HEAD"] == ready_proof["inputs"]["base_head"]
    assert (
        git(git_repo, "rev-parse", environment_lines["DWW_TEST_BASE_REF"])
        == environment_lines["DWW_TEST_BASE_HEAD"]
    )
    ready = call_json(
        "verify",
        "--task",
        task_id,
        "--lease",
        lease,
        "--level",
        "ready",
        repo_path=worktree,
    )
    assert ready["reused"] is True
    assert [item["profile_id"] for item in ready_proof["profile_proofs"]] == ["ready"]
    assert (
        planned["ready"]["fingerprint"]
        == ready_proof["profile_proofs"][0]["fingerprint"]
    )
    full = call_json(
        "verify",
        "--task",
        task_id,
        "--lease",
        lease,
        "--level",
        "full",
        repo_path=worktree,
    )
    full_proof = json.loads(
        (git_repo / ".git" / "solo-ai" / "proofs" / f"{full['proof']}.json").read_text(
            encoding="utf-8"
        )
    )
    assert [item["profile_id"] for item in full_proof["profile_proofs"]] == [
        "ready",
        "full",
    ]
    full_environment_lines = {
        key: value
        for line in Path(full_proof["runs"][-1]["log"])
        .read_text(encoding="utf-8")
        .splitlines()
        if line.startswith("DWW_TEST_")
        for key, value in (line.split("=", 1),)
    }
    assert full_environment_lines["DWW_TEST_SCOPE"] == "integration"
    assert full_environment_lines["DWW_TEST_BASE_REF"] == "main"
    assert (
        full_environment_lines["DWW_TEST_BASE_HEAD"]
        == full_proof["inputs"]["base_head"]
    )
    # 新Full仍须为外部状态未知的检查产生独立执行身份。
    assert (
        planned["full"]["fingerprint"] != full_proof["profile_proofs"][1]["fingerprint"]
    )
    complete = call_json(
        "verify",
        "--task",
        task_id,
        "--lease",
        lease,
        "--level",
        "full",
        "--complete",
        repo_path=worktree,
    )
    assert complete["full_scope"] == "complete"
    complete_proof = json.loads(
        (
            git_repo / ".git" / "solo-ai" / "proofs" / f"{complete['proof']}.json"
        ).read_text(encoding="utf-8")
    )
    assert [item["profile_id"] for item in complete_proof["profile_proofs"]] == [
        "ready",
        "full",
        "complete",
    ]
    complete_environment_lines = {
        key: value
        for line in Path(complete_proof["runs"][-1]["log"])
        .read_text(encoding="utf-8")
        .splitlines()
        if line.startswith("DWW_TEST_")
        for key, value in (line.split("=", 1),)
    }
    assert complete_environment_lines["DWW_TEST_SCOPE"] == "complete"
    assert complete_environment_lines["DWW_TEST_BASE_REF"] == "main"
    assert (
        complete_environment_lines["DWW_TEST_BASE_HEAD"]
        == complete_proof["inputs"]["base_head"]
    )
    stress = call_json(
        "verify",
        "--task",
        task_id,
        "--lease",
        lease,
        "--level",
        "stress",
        repo_path=worktree,
    )
    stress_proof = json.loads(
        (
            git_repo / ".git" / "solo-ai" / "proofs" / f"{stress['proof']}.json"
        ).read_text(encoding="utf-8")
    )
    assert [item["profile_id"] for item in stress_proof["profile_proofs"]] == ["stress"]
    assert (
        planned["stress"]["fingerprint"]
        != stress_proof["profile_proofs"][0]["fingerprint"]
    )
    call_json(
        "abandon",
        "--task",
        task_id,
        "--lease",
        lease,
        "--confirm",
        task_id,
        repo_path=worktree,
    )


def test_full_cli_lifecycle_runs_through_uv_script(git_repo: Path) -> None:
    runner = (
        Path(__file__).parents[2]
        / "plugins"
        / "develop-with-worktrees"
        / "skills"
        / "develop-with-worktrees"
        / "scripts"
        / "dww.py"
    )

    def call_json(*arguments: str, repo_path: Path = git_repo) -> dict:
        completed = subprocess.run(
            [
                "uv",
                "run",
                "--script",
                str(runner),
                "--repo",
                str(repo_path),
                "--json",
                *arguments,
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=90,
        )
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout)["result"]

    def call_start() -> dict[str, str]:
        completed = subprocess.run(
            [
                "uv",
                "run",
                "--script",
                str(runner),
                "--repo",
                str(git_repo),
                "start",
                "--name",
                "cli greeting",
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=90,
        )
        assert completed.returncode == 0, completed.stderr
        values = dict(line.split(": ", 1) for line in completed.stdout.splitlines())
        return {
            "id": values["Task"],
            "worktree": values["Worktree"],
            "lease": values["Lease"],
        }

    adopted = call_json(
        "init", "--accept", "--verify", '["git","diff","--check","main...HEAD"]'
    )
    assert adopted["decision"] == "adopted"
    task = call_start()
    worktree = Path(task["worktree"])
    (worktree / "cli.txt").write_text("hello\n", encoding="utf-8")
    call_json(
        "commit",
        "--task",
        task["id"],
        "--lease",
        task["lease"],
        "--message",
        "feat: cli greeting",
        "--path",
        "cli.txt",
        repo_path=worktree,
    )
    call_json(
        "ready", "--task", task["id"], "--lease", task["lease"], repo_path=worktree
    )
    published = call_json(
        "finish", "--task", task["id"], "--lease", task["lease"], repo_path=worktree
    )
    assert published["outcome"] == "candidate_published"
    tail = call_json("batch", "seal", "--candidate", published["candidate_id"])
    assert tail["status"] == "completed"
    assert (git_repo / "cli.txt").exists()

    started_direct = subprocess.run(
        [
            "uv",
            "run",
            "--script",
            str(runner),
            "--repo",
            str(git_repo),
            "start",
            "--name",
            "cli current worktree",
            "--in-place",
            "--session",
            "cli-session",
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=90,
    )
    assert started_direct.returncode == 0, started_direct.stderr
    direct_values = dict(
        line.split(": ", 1) for line in started_direct.stdout.splitlines()
    )
    assert direct_values["Mode"] == "in-place"
    assert direct_values["Worktree"] == str(git_repo)
    (git_repo / "cli-current.txt").write_text("current\n", encoding="utf-8")
    call_json(
        "commit",
        "--task",
        direct_values["Task"],
        "--lease",
        direct_values["Lease"],
        "--session",
        "cli-session",
        "--message",
        "test: cli current worktree",
        "--path",
        "cli-current.txt",
    )
    direct_plan = call_json(
        "plan",
        "--task",
        direct_values["Task"],
    )
    direct_ready = call_json(
        "ready",
        "--task",
        direct_values["Task"],
        "--lease",
        direct_values["Lease"],
        "--session",
        "cli-session",
    )
    direct_proof = json.loads(
        (
            git_repo
            / ".git"
            / "solo-ai"
            / "proofs"
            / f"{direct_ready['ready_proof']}.json"
        ).read_text(encoding="utf-8")
    )
    assert (
        direct_plan["profiles"][0]["fingerprint"]
        == direct_proof["profile_proofs"][0]["fingerprint"]
    )
    finished_direct = call_json(
        "finish",
        "--task",
        direct_values["Task"],
        "--lease",
        direct_values["Lease"],
        "--session",
        "cli-session",
    )
    assert finished_direct["mode"] == "in-place"
    assert (git_repo / "cli-current.txt").exists()


def test_anchor_cli_roundtrip_through_uv_script(git_repo: Path) -> None:
    runner = (
        Path(__file__).parents[2]
        / "plugins"
        / "develop-with-worktrees"
        / "skills"
        / "develop-with-worktrees"
        / "scripts"
        / "dww.py"
    )

    def call(
        *arguments: str, repo_path: Path = git_repo
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "uv",
                "run",
                "--script",
                str(runner),
                "--repo",
                str(repo_path),
                *arguments,
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=90,
        )

    def call_json(*arguments: str, repo_path: Path = git_repo) -> dict:
        completed = call("--json", *arguments, repo_path=repo_path)
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout)["result"]

    call_json("init", "--accept", "--verify", '["git","diff","--check","main...HEAD"]')
    started = call("start", "--name", "cli anchor")
    assert started.returncode == 0, started.stderr
    values = dict(line.split(": ", 1) for line in started.stdout.splitlines())
    task_id, lease = values["Task"], values["Lease"]
    worktree = Path(values["Worktree"])

    shown = call_json("anchor", "show", "--task", task_id)
    input_path = worktree / "anchor-input.md"
    content = shown["content"]
    content = content.replace(
        "- Implementation target: fill before editing",
        "- Implementation target: CLI anchor roundtrip",
    )
    content = content.replace(
        "- Scope boundary: fill before editing", "- Scope boundary: CLI test only"
    )
    content = content.replace(
        "- Acceptance criteria: fill before Ready",
        "- Acceptance criteria: show and update succeed",
    )
    content = content.replace(
        "- Current progress: task started", "- Current progress: CLI update verified"
    )
    input_path.write_text(content, encoding="utf-8", newline="\n")
    updated = call_json(
        "anchor",
        "update",
        "--task",
        task_id,
        "--lease",
        lease,
        "--file",
        str(input_path),
        "--expected-sha256",
        shown["sha256"],
    )
    assert updated["changed"] is True
    refreshed = call_json("anchor", "show", "--task", task_id)
    assert refreshed["content"] == content
    rejected = call(
        "--json",
        "anchor",
        "update",
        "--task",
        task_id,
        "--lease",
        "wrong",
        "--file",
        str(input_path),
        "--expected-sha256",
        refreshed["sha256"],
    )
    assert rejected.returncode == 2
    call_json(
        "abandon",
        "--task",
        task_id,
        "--lease",
        lease,
        "--confirm",
        task_id,
        repo_path=worktree,
    )
