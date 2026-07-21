from pathlib import Path

from solo_ai.repo import GitRepo
from solo_ai.safety import scan

from conftest import git


def test_reports_secret_location_without_value(git_repo: Path) -> None:
    secret = "sk-proj-abcdefghijklmnopqrstuvwxyz123456"
    (git_repo / "config.txt").write_text(f"token={secret}\n", encoding="utf-8")
    git(git_repo, "add", "config.txt")
    findings = scan(GitRepo(git_repo), cwd=git_repo, base=None, staged=True)
    assert [(item.path, item.line) for item in findings] == [
        ("config.txt", 1),
        ("config.txt", 1),
    ]
    assert secret not in repr(findings)


def test_allows_only_an_exact_declared_path(git_repo: Path) -> None:
    (git_repo / "config.txt").write_text("token=not-for-production\n", encoding="utf-8")
    git(git_repo, "add", "config.txt")
    repo = GitRepo(git_repo)

    assert (
        scan(repo, cwd=git_repo, base=None, staged=True, allowlist=("config.txt",))
        == []
    )
    assert scan(repo, cwd=git_repo, base=None, staged=True, allowlist=("*.txt",))
