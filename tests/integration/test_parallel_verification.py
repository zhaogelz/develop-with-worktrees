from __future__ import annotations

import threading
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from conftest import git
from solo_ai import proof, validation_queue
from solo_ai.config import load_verification_config
from solo_ai.repo import GitRepo
from solo_ai.util import SoloAIError
from test_verification_reuse import configure


@pytest.fixture(autouse=True)
def isolated_machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(validation_queue, "_machine_root", lambda: tmp_path / "machine")


@pytest.mark.parametrize("fails", [False, True])
@pytest.mark.parametrize("capacity", [1, 2])
@pytest.mark.parametrize("external", ["none", "unknown"])
def test_parallel_branches_execute_matching_pure_check_once(
    git_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capacity: int,
    external: str,
    fails: bool,
) -> None:
    """两个真实分支在同一检查开始前汇合，纯检查只应执行一次。"""
    configure(
        git_repo,
        "raise SystemExit(1)" if fails else "print('checked')",
        external=external,
    )
    policy = git_repo / ".solo-ai/verification.toml"
    policy.write_text(
        policy.read_text(encoding="utf-8").replace(
            'input_paths = ["**"]', 'input_paths = ["README.md"]'
        ),
        encoding="utf-8",
    )
    git(git_repo, "add", ".solo-ai/verification.toml")
    git(git_repo, "commit", "-m", "test: independent declared input")
    branches = []
    for index in range(2):
        branch = tmp_path / f"branch-{index}"
        git(git_repo, "worktree", "add", "-b", f"feature-{index}", str(branch))
        (branch / f"feature-{index}.txt").write_text(str(index), encoding="utf-8")
        git(branch, "add", f"feature-{index}.txt")
        git(branch, "commit", "-m", "test: independent source")
        branches.append(GitRepo(branch))
    validation_queue.set_capacity(str(capacity))
    rendezvous = threading.Barrier(2)
    both_decided = threading.Event()
    seen = set()
    mutex = threading.Lock()
    original_update = proof._update_validation_attempt_profile
    original_run = proof.run_logged
    executions = []

    def observed_update(*args, **kwargs):
        # 先确认两次独立验证都到达执行/等证明阶段，再放行命令。
        if kwargs.get("state") == "waiting":
            with mutex:
                seen.add(args[1])
                if len(seen) == 2:
                    both_decided.set()
        return original_update(*args, **kwargs)

    def observed_run(*args, **kwargs):
        assert both_decided.wait(15), "第二个分支必须真实到达验证等待阶段"
        with mutex:
            executions.append(str(kwargs["cwd"]))
        return original_run(*args, **kwargs)

    monkeypatch.setattr(proof, "_update_validation_attempt_profile", observed_update)
    monkeypatch.setattr(proof, "run_logged", observed_run)

    def validate_branch(branch):
        rendezvous.wait(timeout=15)
        try:
            return proof.validate(
                branch,
                cwd=branch.root,
                base="main",
                task_id=branch.root.name,
                verification=load_verification_config(branch),
                level="full",
                expected_candidate_head=branch.head(),
            )
        except SoloAIError as error:
            if not fails:
                raise
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(validate_branch, branches))
    assert len(executions) == (1 if external == "none" else 2)
    if fails:
        assert all(isinstance(result, SoloAIError) for result in results)
        if external == "none":
            assert any("already failed" in str(result) for result in results)
        return
    assert all(result["result"] == "passed" for result in results)
    if external == "none":
        assert sorted(result["profile_proofs"][0]["reused"] for result in results) == [
            False,
            True,
        ]


def test_inherited_validation_does_not_deadlock_on_independent_proof_waiter(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    """父容量、独立等待者、继承容量子进程形成真实三进程锁顺序场景。"""
    repo = configure(git_repo, "print('checked once')")
    validation_queue.set_capacity("1")
    worker = tmp_path / "validate-worker.py"
    worker.write_text(
        """import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from solo_ai import proof, validation_queue
from solo_ai.config import load_verification_config
from solo_ai.repo import GitRepo
validation_queue._machine_root = lambda: Path(sys.argv[3])
repo = GitRepo(Path(sys.argv[2]))
result = proof.validate(repo, cwd=repo.root, base='main', task_id=sys.argv[4],
    verification=load_verification_config(repo), level='full', expected_candidate_head=repo.head())
print(json.dumps({'result': result['result'], 'reused': result['profile_proofs'][0]['reused']}))
""",
        encoding="utf-8",
    )
    command = [
        sys.executable,
        str(worker),
        str(Path(proof.__file__).parents[1]),
        str(git_repo),
        str(tmp_path / "machine"),
    ]
    environment = dict(os.environ)
    environment.pop(validation_queue.INHERITED_CLAIM_ENV, None)
    independent = None
    try:
        with validation_queue.claim_validation_slot("normal") as parent:
            independent = subprocess.Popen(
                [*command, "independent"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                env=environment,
            )
            deadline = time.monotonic() + 20
            while not list((repo.local_dir / "locks").glob("profile-*.lock")):
                assert independent.poll() is None
                assert time.monotonic() < deadline
                time.sleep(0.05)
            nested = subprocess.run(
                [*command, "nested"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=20,
                env={
                    **environment,
                    **validation_queue.inherited_claim_environment(parent),
                },
            )
            assert nested.returncode == 0, nested.stderr
            assert json.loads(nested.stdout)["reused"] is False
        output, errors = independent.communicate(timeout=20)
        assert independent.returncode == 0, errors
        assert json.loads(output)["reused"] is True
        assert len(list((repo.local_dir / "validation-runs").glob("*/*.json"))) == 1
    finally:
        if independent is not None and independent.poll() is None:
            independent.kill()
            independent.communicate(timeout=10)


def test_waiting_profile_rechecks_changed_input_before_reusing(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """等待既有证明期间源码发生变化，拿到锁后仍必须拒绝旧输入。"""
    repo = configure(git_repo, "print('checked')")
    original = proof._profile_execution_lock

    from contextlib import contextmanager

    @contextmanager
    def changed_while_waiting(*args, **kwargs):
        with original(*args, **kwargs):
            (git_repo / "README.md").write_text(
                "changed while waiting", encoding="utf-8"
            )
            yield

    monkeypatch.setattr(proof, "_profile_execution_lock", changed_while_waiting)
    with pytest.raises(SoloAIError, match="declared paths: README.md"):
        proof.validate(
            repo,
            cwd=repo.root,
            base="main",
            task_id="changed",
            verification=load_verification_config(repo),
            level="full",
            expected_candidate_head=repo.head(),
        )
    assert not list((repo.local_dir / "validation-runs").glob("*/*.json"))


def test_profile_wait_status_requires_live_owner_and_waiter(git_repo: Path) -> None:
    from solo_ai.status_views import _validation_profile_projection
    from solo_ai.util import DirectoryLock, process_snapshot

    repo = configure(git_repo, "pass")
    fingerprint = "a" * 64
    lock = repo.local_dir / "locks" / f"profile-{fingerprint}.lock"
    profile = {
        "id": "check",
        "state": "waiting",
        "proof_wait": {"fingerprint": fingerprint, "waiter": process_snapshot()},
    }
    with DirectoryLock(lock):
        assert _validation_profile_projection(profile, repo=repo)["state"] == "waiting"
        profile["proof_wait"]["waiter"] = {"pid": 0}
        assert _validation_profile_projection(profile, repo=repo)["state"] == "unknown"
        (lock / "owner.json").write_text("[]", encoding="utf-8")
        assert _validation_profile_projection(profile, repo=repo)["state"] == "unknown"
        (lock / "owner.json").write_text("broken", encoding="utf-8")
        assert _validation_profile_projection(profile, repo=repo)["state"] == "unknown"


def test_cached_heavy_profile_reuses_under_normal_parent_capacity(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """已有纯证明无需申请 heavy 容量，不能被父容量等级误拒绝。"""
    repo = configure(git_repo, "print('once')")
    policy = git_repo / ".solo-ai/verification.toml"
    policy.write_text(
        policy.read_text(encoding="utf-8").replace(
            'input_paths = ["**"]',
            'input_paths = ["README.md"]\nresource_class = "heavy"',
        ),
        encoding="utf-8",
    )
    git(git_repo, "add", ".solo-ai/verification.toml")
    git(git_repo, "commit", "-m", "test: heavy declared input")
    first = proof.validate(
        repo,
        cwd=repo.root,
        base="main",
        task_id="first",
        verification=load_verification_config(repo),
        level="full",
        expected_candidate_head=repo.head(),
    )
    assert first["profile_proofs"][0]["reused"] is False
    (git_repo / "feature.txt").write_text("new candidate", encoding="utf-8")
    git(git_repo, "add", "feature.txt")
    git(git_repo, "commit", "-m", "test: unrelated candidate input")
    with validation_queue.claim_validation_slot("normal") as parent:
        monkeypatch.setenv(validation_queue.INHERITED_CLAIM_ENV, parent["id"])
        second = proof.validate(
            repo,
            cwd=repo.root,
            base="main",
            task_id="second",
            verification=load_verification_config(repo),
            level="full",
            expected_candidate_head=repo.head(),
        )
    assert second["profile_proofs"][0]["reused"] is True
    assert len(list((repo.local_dir / "validation-runs").glob("*/*.json"))) == 1
