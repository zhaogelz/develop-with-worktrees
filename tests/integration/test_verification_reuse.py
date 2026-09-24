from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest
from conftest import git
from solo_ai import proof, validation_queue
from solo_ai.config import load_verification_config
from solo_ai.repo import GitRepo
from solo_ai.util import SoloAIError


def configure(
    root: Path,
    script: str,
    *,
    external: str = "none",
    closure: str = "complete",
    frozen_base: bool = False,
) -> GitRepo:
    """只搭建验证接口所需的真实Git事实，不创建任务和集成队列。"""
    policy = root / ".solo-ai"
    policy.mkdir()
    (policy / "config.toml").write_text(
        'schema_version = 2\nmode = "managed"\n', encoding="utf-8"
    )
    (root / ".gitignore").write_text(".tmp/\n", encoding="utf-8")
    (policy / "verification.toml").write_text(
        f'''
schema_version = 3
static_only = false
[[profiles]]
id = "check"
level = "full"
paths = ["**"]
input_paths = ["**"]
input_closure = "{closure}"
external_state = "{external}"
frozen_base = {str(frozen_base).lower()}
commands = [{json.dumps([sys.executable, "-c", script])}]
''',
        encoding="utf-8",
    )
    git(root, "add", ".solo-ai", ".gitignore")
    git(root, "commit", "-m", "test: declare validation interface")
    return GitRepo(root)


def validate(repo: GitRepo):
    return proof.validate(
        repo,
        cwd=repo.root,
        base="main",
        task_id="same-batch",
        verification=load_verification_config(repo),
        level="full",
        expected_candidate_head=repo.head(repo.root),
    )


def configure_collecting_profiles(root: Path, profiles: list[dict]) -> GitRepo:
    repo = configure(root, "pass")
    lines = ["schema_version = 3", "static_only = false"]
    for item in profiles:
        lines.extend(
            [
                "[[profiles]]",
                f"id = {json.dumps(item['id'])}",
                'level = "full"',
                'paths = ["**"]',
                f"input_paths = {json.dumps(item.get('inputs', ['README.md']))}",
                'input_closure = "complete"',
                f"external_state = {json.dumps(item.get('external', 'none'))}",
                f"continue_on_failure = {str(item.get('collect', False)).lower()}",
                f"depends_on = {json.dumps(item.get('depends', []))}",
                f"resource_class = {json.dumps(item.get('resource', 'normal'))}",
                f"commands = [{json.dumps([sys.executable, '-c', item['script']])}]",
            ]
        )
    (root / ".solo-ai/verification.toml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    git(root, "add", ".solo-ai/verification.toml")
    git(root, "commit", "-m", "test: declare independent profiles")
    return repo


@pytest.fixture(autouse=True)
def isolated_machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(validation_queue, "_machine_root", lambda: tmp_path / "machine")


def test_new_full_recreates_required_output(git_repo: Path):
    repo = configure(
        git_repo,
        "from pathlib import Path; p=Path('.tmp/build.bin'); "
        "p.parent.mkdir(exist_ok=True); p.write_text('artifact')",
        external="unknown",
    )
    first = validate(repo)
    artifact = git_repo / ".tmp/build.bin"
    artifact.unlink()
    second = validate(repo)
    assert artifact.is_file(), "上次构建成功证明不能替代已丢失的产物"
    assert first["fingerprint"] != second["fingerprint"]


def test_static_proof_reuses_real_builtin_receipt_and_rejects_empty_runs(
    git_repo: Path,
):
    repo = configure(git_repo, "pass")
    (git_repo / ".solo-ai/verification.toml").write_text(
        "schema_version = 3\nstatic_only = true\n", encoding="utf-8"
    )
    git(git_repo, "add", ".solo-ai/verification.toml")
    git(git_repo, "commit", "-m", "test: use explicit static-only gate")
    first = validate(repo)
    second = validate(repo)
    assert first["kind"] == "static-only"
    assert first["inputs"]["command_manifest"] == []
    assert first["profile_proofs"] == []
    assert len(first["runs"]) == 1
    assert first["runs"][0]["command_digest"] is None
    assert second["reused"] is True
    assert first["fingerprint"] == second["fingerprint"]
    identity = {
        "fingerprint": first["fingerprint"],
        "candidate_head": first["inputs"]["candidate_head"],
        "base_head": first["inputs"]["base_head"],
    }
    proof.require_exact_passed_proof(first, **identity)
    with pytest.raises(SoloAIError, match="logs are missing or changed"):
        proof.require_exact_passed_proof({**first, "runs": []}, **identity)


def test_input_changed_without_commit_cannot_receive_success_proof(git_repo: Path):
    repo = configure(
        git_repo, "from pathlib import Path; Path('README.md').write_text('changed')"
    )
    with pytest.raises(SoloAIError, match="declared paths: README.md"):
        validate(repo)
    assert not list((repo.local_dir / "profile-proofs").glob("*.json"))
    attempts = list((repo.local_dir / "validation-attempts").glob("*.json"))
    assert len(attempts) == 1
    attempt = json.loads(attempts[0].read_text(encoding="utf-8"))
    assert attempt["profiles"][0]["runs"][0]["duration_seconds"] >= 0
    assert attempt["profiles"][0]["runs"][0]["receipt"]


def test_failed_validation_keeps_an_attempt_receipt_with_executed_cost(
    git_repo: Path,
) -> None:
    repo = configure(git_repo, "raise SystemExit(3)", external="unknown")
    attempt_id = "full-attempt-failure"

    with pytest.raises(SoloAIError, match="Validation failed"):
        proof.validate(
            repo,
            cwd=repo.root,
            base="main",
            task_id="same-batch",
            verification=load_verification_config(repo),
            level="full",
            expected_candidate_head=repo.head(repo.root),
            attempt_id=attempt_id,
        )

    attempt = proof.read_validation_attempt(repo, attempt_id)
    assert attempt["state"] == "completed"
    assert attempt["result"] == "failed"
    assert attempt["profiles"][0]["state"] == "failed"
    assert attempt["profiles"][0]["runs"][0]["duration_seconds"] >= 0


def test_collects_two_independent_failures_and_keeps_success(git_repo: Path) -> None:
    repo = configure_collecting_profiles(
        git_repo,
        [
            {"id": "first", "script": "raise SystemExit(2)", "collect": True},
            {"id": "second", "script": "raise SystemExit(3)", "collect": True},
            {"id": "success", "script": "pass"},
        ],
    )
    attempt_id = "collect-two-failures"
    with pytest.raises(SoloAIError, match="first, second"):
        proof.validate(
            repo,
            cwd=repo.root,
            base="main",
            verification=load_verification_config(repo),
            level="full",
            expected_candidate_head=repo.head(repo.root),
            attempt_id=attempt_id,
        )
    attempt = proof.read_validation_attempt(repo, attempt_id)
    assert attempt["summary"]["failed"] == 2
    assert attempt["summary"]["passed"] == 1
    assert [item["profile_id"] for item in attempt["failures"]] == ["first", "second"]
    success = next(item for item in attempt["profiles"] if item["id"] == "success")
    assert success["proof"]
    assert success["runs"][0]["exit_code"] == 0


def test_failed_prerequisite_blocks_heavy_check_but_keeps_independent_check(
    git_repo: Path,
) -> None:
    repo = configure_collecting_profiles(
        git_repo,
        [
            {"id": "preflight", "script": "raise SystemExit(2)", "collect": True},
            {
                "id": "browser",
                "script": "from pathlib import Path; p=Path('.tmp/heavy'); p.parent.mkdir(exist_ok=True); p.write_text('ran')",
                "resource": "heavy",
                "depends": ["preflight"],
            },
            {"id": "independent", "script": "pass"},
        ],
    )
    attempt_id = "blocked-heavy"
    with pytest.raises(SoloAIError, match="preflight"):
        proof.validate(
            repo,
            cwd=repo.root,
            base="main",
            verification=load_verification_config(repo),
            level="full",
            expected_candidate_head=repo.head(repo.root),
            attempt_id=attempt_id,
        )
    attempt = proof.read_validation_attempt(repo, attempt_id)
    assert attempt["summary"]["blocked"] == 1
    assert attempt["summary"]["passed"] == 1
    assert attempt["profiles"][1]["error_reason"] == "dependency_not_passed:preflight"
    assert not (git_repo / ".tmp/heavy").exists()


def test_input_drift_stops_collection_before_next_profile(git_repo: Path) -> None:
    repo = configure_collecting_profiles(
        git_repo,
        [
            {
                "id": "drift",
                "script": "from pathlib import Path; Path('README.md').write_text('changed'); raise SystemExit(2)",
                "collect": True,
            },
            {
                "id": "later",
                "script": "from pathlib import Path; Path('.tmp/later').write_text('ran')",
            },
        ],
    )
    with pytest.raises(SoloAIError, match="declared paths: README.md"):
        validate(repo)
    assert not (git_repo / ".tmp/later").exists()


def test_repair_reuses_unaffected_pure_check_and_rebuilds_output(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (git_repo / "feature.txt").write_text("bad", encoding="utf-8")
    git(git_repo, "add", "feature.txt")
    git(git_repo, "commit", "-m", "test: add failing feature")
    repo = configure_collecting_profiles(
        git_repo,
        [
            {"id": "pure", "script": "pass", "inputs": ["README.md"]},
            {
                "id": "feature",
                "script": "from pathlib import Path; raise SystemExit(0 if Path('feature.txt').read_text() == 'good' else 2)",
                "inputs": ["feature.txt"],
                "collect": True,
            },
            {
                "id": "build",
                "script": "from pathlib import Path; p=Path('.tmp/build.bin'); p.parent.mkdir(exist_ok=True); p.write_text('fresh')",
                "external": "unknown",
            },
        ],
    )
    original_run = proof.run_logged
    counts = {"pure": 0, "feature": 0, "build": 0}

    def count_run(*args, **kwargs):
        counts[kwargs["receipt_metadata"]["profile_id"]] += 1
        return original_run(*args, **kwargs)

    monkeypatch.setattr(proof, "run_logged", count_run)
    with pytest.raises(SoloAIError, match="feature"):
        validate(repo)
    artifact = git_repo / ".tmp/build.bin"
    assert artifact.is_file()
    artifact.unlink()
    (git_repo / "feature.txt").write_text("good", encoding="utf-8")
    git(git_repo, "add", "feature.txt")
    git(git_repo, "commit", "-m", "test: repair feature")
    result = validate(repo)
    assert result["result"] == "passed"
    assert counts == {"pure": 1, "feature": 2, "build": 2}
    assert artifact.read_text(encoding="utf-8") == "fresh"


def test_interrupted_second_command_keeps_all_prior_attempt_costs(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = configure(git_repo, "print('first')", external="unknown")
    policy = git_repo / ".solo-ai/verification.toml"
    first_command = json.dumps([sys.executable, "-c", "print('first')"])
    second_command = json.dumps([sys.executable, "-c", "print('second')"])
    policy.write_text(
        policy.read_text(encoding="utf-8").replace(
            f"commands = [{first_command}]",
            f"commands = [{first_command}, {second_command}]",
        ),
        encoding="utf-8",
    )
    original_run = proof.run_logged
    calls = 0

    def interrupt_after_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = original_run(*args, **kwargs)
        if calls == 2:
            raise KeyboardInterrupt("synthetic interruption after second command")
        return result

    monkeypatch.setattr(proof, "run_logged", interrupt_after_second)
    attempt_id = "full-attempt-second-command-interrupted"

    with pytest.raises(KeyboardInterrupt, match="second command"):
        proof.validate(
            repo,
            cwd=repo.root,
            base="main",
            task_id="same-batch",
            verification=load_verification_config(repo),
            level="full",
            expected_candidate_head=repo.head(repo.root),
            attempt_id=attempt_id,
        )

    attempt = proof.read_validation_attempt(repo, attempt_id)
    runs = attempt["profiles"][0]["runs"]
    assert attempt["result"] == "interrupted"
    assert len(runs) == 2
    assert all(isinstance(run["duration_seconds"], (int, float)) for run in runs)


def test_execution_decision_explains_a_verified_previous_command_change(
    git_repo: Path,
) -> None:
    repo = configure(git_repo, "print('first')")
    policy = git_repo / ".solo-ai/verification.toml"
    policy.write_text(
        policy.read_text(encoding="utf-8").replace('level = "full"', 'level = "ready"'),
        encoding="utf-8",
    )
    verification = load_verification_config(repo)
    proof.validate(
        repo,
        cwd=repo.root,
        base="main",
        task_id="same-task",
        verification=verification,
        level="ready",
        expected_candidate_head=repo.head(repo.root),
    )
    first_command = json.dumps([sys.executable, "-c", "print('first')"])
    second_command = json.dumps([sys.executable, "-c", "print('second')"])
    policy.write_text(
        policy.read_text(encoding="utf-8").replace(first_command, second_command),
        encoding="utf-8",
    )
    _, records = proof.proof_inputs(
        repo,
        cwd=repo.root,
        base="main",
        verification=load_verification_config(repo),
        task_id="same-task",
        levels=("ready",),
    )
    profile, inputs, fingerprint = records[0]

    decision = proof.profile_execution_decision(
        repo, profile=profile, inputs=inputs, fingerprint=fingerprint
    )

    assert decision["action"] == "execute"
    assert "command_changed" in decision["previous_input_changes"]


def test_input_drift_during_cached_log_check_cannot_receive_success(
    git_repo: Path, monkeypatch
):
    repo = configure(git_repo, "pass")
    validate(repo)
    original = proof._logs_exist

    def change_after_check(receipt):
        result = original(receipt)
        (git_repo / "README.md").write_text("changed during cache lookup")
        return result

    monkeypatch.setattr(proof, "_logs_exist", change_after_check)
    with pytest.raises(SoloAIError, match="inputs changed"):
        validate(repo)


def test_profile_proof_does_not_cross_frozen_validation_bases(git_repo: Path):
    """项目命令可读取 DWW 基线，因此不同基线绝不能复用同一 profile proof。"""
    repo = configure(git_repo, COUNT, frozen_base=True)
    git(git_repo, "branch", "base-a")
    (git_repo / "notes.md").write_text("base-b\n", encoding="utf-8")
    git(git_repo, "add", "notes.md")
    git(git_repo, "commit", "-m", "test: advance alternate base")
    git(git_repo, "branch", "base-b")
    (git_repo / "candidate.txt").write_text("candidate\n", encoding="utf-8")
    git(git_repo, "add", "candidate.txt")
    git(git_repo, "commit", "-m", "test: add candidate")
    candidate_head = repo.head(repo.root)

    def validate_against(base: str):
        return proof.validate(
            repo,
            cwd=repo.root,
            base=base,
            validation_base_ref=base,
            task_id="same-candidate",
            verification=load_verification_config(repo),
            level="full",
            expected_base_head=repo.git(
                ["rev-parse", base], cwd=repo.root
            ).stdout.strip(),
            expected_candidate_head=candidate_head,
        )

    first = validate_against("base-a")
    second = validate_against("base-b")

    assert (git_repo / ".tmp" / "count").read_text(encoding="utf-8") == "2"
    assert (
        first["profile_proofs"][0]["fingerprint"]
        != second["profile_proofs"][0]["fingerprint"]
    )
    assert (
        first["inputs"]["validation_environment"]["DWW_VALIDATION_BASE_HEAD"]
        != second["inputs"]["validation_environment"]["DWW_VALIDATION_BASE_HEAD"]
    )


COUNT = (
    "from pathlib import Path; p=Path('.tmp/count'); p.parent.mkdir(exist_ok=True); "
    "n=int(p.read_text()) if p.exists() else 0; p.write_text(str(n+1))"
)


@pytest.mark.parametrize(
    "external,closure,expected",
    [
        ("none", "complete", 1),
        ("none", "declared", 2),
        ("unknown", "complete", 2),
    ],
)
def test_only_complete_pure_checks_survive_new_full(
    git_repo: Path, external, closure, expected
):
    repo = configure(git_repo, COUNT, external=external, closure=closure)
    validate(repo)
    validate(repo)
    assert (git_repo / ".tmp/count").read_text() == str(expected)


def test_complete_pure_profile_reuses_between_tasks_when_its_declared_inputs_match(
    git_repo: Path,
):
    """批次重跑时，未受后续候选影响的检查应直接复用已有收据。"""
    repo = configure(git_repo, COUNT)
    (git_repo / "src").mkdir()
    (git_repo / "src/checked.py").write_text("VALUE = 1\n", encoding="utf-8")
    policy = git_repo / ".solo-ai/verification.toml"
    policy.write_text(
        policy.read_text(encoding="utf-8").replace(
            'input_paths = ["**"]', 'input_paths = ["src/**"]'
        ),
        encoding="utf-8",
    )
    git(git_repo, "add", "src/checked.py", ".solo-ai/verification.toml")
    git(git_repo, "commit", "-m", "test: declare the check input boundary")
    first = validate(repo)

    (git_repo / "README.md").write_text("unrelated candidate\n", encoding="utf-8")
    git(git_repo, "add", "README.md")
    git(git_repo, "commit", "-m", "test: add unrelated candidate")
    second = proof.validate(
        repo,
        cwd=repo.root,
        base="main",
        task_id="next-batch",
        verification=load_verification_config(repo),
        level="full",
        expected_candidate_head=repo.head(repo.root),
    )

    assert (
        first["profile_proofs"][0]["fingerprint"]
        == second["profile_proofs"][0]["fingerprint"]
    )
    assert second["profile_proofs"][0]["reused"] is True
    assert (git_repo / ".tmp/count").read_text() == "1"


def test_complete_pure_proof_ignores_selection_and_queue_metadata(
    git_repo: Path,
) -> None:
    """纯检查的执行事实不应因调度元数据而失效。"""
    repo = configure(git_repo, COUNT)
    (git_repo / "src").mkdir()
    (git_repo / "src" / "checked.py").write_text("VALUE = 1\n", encoding="utf-8")
    policy = git_repo / ".solo-ai" / "verification.toml"
    policy.write_text(
        policy.read_text(encoding="utf-8").replace(
            'input_paths = ["**"]', 'input_paths = ["src/**"]'
        ),
        encoding="utf-8",
    )
    git(git_repo, "add", "src/checked.py", ".solo-ai/verification.toml")
    git(git_repo, "commit", "-m", "test: narrow pure proof inputs")
    first = validate(repo)

    policy.write_text(
        policy.read_text(encoding="utf-8")
        .replace('paths = ["**"]', 'paths = ["README.md"]')
        .replace(
            "frozen_base = false",
            'frozen_base = false\ncross_task_reuse = true\nresource_class = "heavy"',
        ),
        encoding="utf-8",
    )
    (git_repo / "README.md").write_text("selection-only change\n", encoding="utf-8")
    second = validate(repo)

    assert (
        first["profile_proofs"][0]["fingerprint"]
        == second["profile_proofs"][0]["fingerprint"]
    )
    assert second["profile_proofs"][0]["reused"] is True
    assert (git_repo / ".tmp/count").read_text(encoding="utf-8") == "1"


@pytest.mark.parametrize(
    "change",
    ["source", "command", "policy-comment", "environment", "log", "missing-results"],
)
def test_changed_or_damaged_evidence_cannot_skip_execution(
    git_repo: Path, monkeypatch, change
):
    repo = configure(git_repo, COUNT)
    policy = git_repo / ".solo-ai/verification.toml"
    policy.write_text(policy.read_text() + '\nenvironment = ["DWW_FIXTURE_ENV"]\n')
    first = validate(repo)
    if change == "source":
        (git_repo / "README.md").write_text("new source")
    elif change == "command":
        policy.write_text(
            policy.read_text().replace("from pathlib", "import os; from pathlib")
        )
    elif change == "policy-comment":
        policy.write_text(
            "# The proof identity keeps byte-level configuration evidence.\n"
            + policy.read_text(),
            encoding="utf-8",
        )
    elif change == "environment":
        monkeypatch.setenv("DWW_FIXTURE_ENV", "changed")
    elif change == "log":
        Path(first["runs"][0]["log"]).write_text("corrupted")
    else:
        for directory, fingerprint in (
            ("proofs", first["fingerprint"]),
            ("profile-proofs", first["profile_proofs"][0]["fingerprint"]),
        ):
            path = repo.local_dir / directory / f"{fingerprint}.json"
            value = json.loads(path.read_text())
            value["runs"] = []
            path.write_text(json.dumps(value))
    second = validate(repo)
    assert (git_repo / ".tmp/count").read_text() == "2"
    assert proof._logs_exist(second), "重验后必须得到完整、可再次核验的执行日志"


def test_stress_supplement_change_cannot_reuse_full_evidence(git_repo: Path):
    repo = configure(git_repo, COUNT)
    validate(repo)
    (git_repo / ".solo-ai/stress-verification.toml").write_text(
        """schema_version = 3
static_only = false

[[profiles]]
id = "stress"
level = "stress"
resource_class = "heavy"
paths = ["**"]
commands = [["git", "status"]]
""",
        encoding="utf-8",
    )

    validate(repo)

    assert (git_repo / ".tmp/count").read_text() == "1"


def test_partial_success_receipts_cannot_skip_unrecorded_commands(git_repo: Path):
    repo = configure(git_repo, COUNT)
    policy = git_repo / ".solo-ai/verification.toml"
    policy.write_text(
        policy.read_text().replace(
            json.dumps([sys.executable, "-c", COUNT]),
            json.dumps([sys.executable, "-c", COUNT])
            + ", "
            + json.dumps([sys.executable, "-c", "print('second')"]),
        )
    )
    first = validate(repo)
    for directory, fingerprint in (
        ("proofs", first["fingerprint"]),
        ("profile-proofs", first["profile_proofs"][0]["fingerprint"]),
    ):
        path = repo.local_dir / directory / f"{fingerprint}.json"
        value = json.loads(path.read_text())
        value["runs"] = value["runs"][:1]
        path.write_text(json.dumps(value))
    second = validate(repo)
    assert (git_repo / ".tmp/count").read_text() == "2"
    assert len(second["runs"]) == 2
    assert proof._logs_exist(second)


def test_identical_profiles_in_different_repositories_do_not_share_results(
    git_repo: Path,
):
    other = git_repo.parent / "other-project"
    shutil.copytree(git_repo, other)
    first_repo = configure(git_repo, COUNT)
    second_repo = configure(other, COUNT)
    first = validate(first_repo)
    second = validate(second_repo)
    assert (
        first["profile_proofs"][0]["fingerprint"]
        == second["profile_proofs"][0]["fingerprint"]
    )
    assert second["profile_proofs"][0]["reused"] is False
    assert (other / ".tmp/count").read_text() == "1"


def test_node_project_uses_the_same_declarative_interface(git_repo: Path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is not installed")
    repo = configure(git_repo, "pass")
    policy = git_repo / ".solo-ai/verification.toml"
    node_check = "const fs=require('node:fs'); fs.mkdirSync('.tmp',{recursive:true}); fs.writeFileSync('.tmp/node-ok','ok')"
    policy.write_text(
        policy.read_text().replace(
            json.dumps([sys.executable, "-c", "pass"]),
            json.dumps([node, "-e", node_check]),
        )
    )
    result = validate(repo)
    assert result["result"] == "passed"
    assert (git_repo / ".tmp/node-ok").read_text() == "ok"
