from pathlib import Path

from conftest import git
from solo_ai.repo import GitRepo
from solo_ai.safety import scan


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


def test_sensitive_file_names_are_case_insensitive(git_repo: Path) -> None:
    (git_repo / ".ENV.production").write_text("placeholder\n", encoding="utf-8")
    (git_repo / "SECRET.PEM").write_text("placeholder\n", encoding="utf-8")
    git(git_repo, "add", ".ENV.production", "SECRET.PEM")
    findings = scan(GitRepo(git_repo), cwd=git_repo, base=None, staged=True)
    assert {(item.path, item.rule) for item in findings} == {
        (".ENV.production", "sensitive-file"),
        ("SECRET.PEM", "sensitive-file"),
    }
