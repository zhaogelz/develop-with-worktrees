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
    with pytest.raises(SoloAIError, match="inputs changed"):
        validate(repo)
    assert not list((repo.local_dir / "profile-proofs").glob("*.json"))


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


@pytest.mark.parametrize(
    "change", ["source", "command", "environment", "log", "missing-results"]
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

    assert (git_repo / ".tmp/count").read_text() == "2"


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
