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


def test_javascript_boolean_password_attributes_do_not_hide_other_secrets(
    git_repo: Path,
) -> None:
    expressions = [
        "!0",
        "!!input.password",
        "'password'===input.type",
        '"password" === type',
        "input.type === 'password'",
        "input.type == 'password'",
    ]
    content = "\n".join(
        "const input = {" + "password" + ":" + value + "};" for value in expressions
    )
    path = git_repo / "input.js"
    path.write_text(content, encoding="utf-8")
    git(git_repo, "add", "input.js")
    repo = GitRepo(git_repo)
    assert scan(repo, cwd=git_repo, base=None, staged=True) == []

    # 同一压缩行的合法属性不能遮住其他赋值或内建密钥特征。
    path.write_text(
        content
        + "\nconst input={"
        + "password"
        + ":!0,"
        + "token"
        + ":'synthetic-test-value'};",
        encoding="utf-8",
    )
    git(git_repo, "add", "input.js")
    findings = scan(repo, cwd=git_repo, base=None, staged=True)
    assert [(item.line, item.rule) for item in findings] == [(7, "assigned-secret")]

    path.write_text(
        "const input={"
        + "password"
        + ":!0};const value='"
        + "sk-"
        + "proj-"
        + "abcdefghijklmnopqrstuvwxyz123456';",
        encoding="utf-8",
    )
    git(git_repo, "add", "input.js")
    assert [item.rule for item in scan(repo, cwd=git_repo, base=None, staged=True)] == [
        "openai-key"
    ]


def test_password_literals_and_unknown_expressions_remain_blocked(
    git_repo: Path,
) -> None:
    path = git_repo / "input.js"
    path.write_text(
        "\n".join(
            "const input={" + "password" + ":" + value + "};"
            for value in [
                "'synthetic-test-value'",
                "getValue()",
                "!value || getValue()",
            ]
        ),
        encoding="utf-8",
    )
    git(git_repo, "add", "input.js")
    findings = scan(GitRepo(git_repo), cwd=git_repo, base=None, staged=True)
    assert [(item.line, item.rule) for item in findings] == [
        (1, "assigned-secret"),
        (2, "assigned-secret"),
        (3, "assigned-secret"),
    ]

    path = git_repo / "settings.txt"
    path.write_text("password" + ":!0\n", encoding="utf-8")
    git(git_repo, "add", "settings.txt")
    assert any(
        item.path == "settings.txt"
        for item in scan(GitRepo(git_repo), cwd=git_repo, base=None, staged=True)
    )
