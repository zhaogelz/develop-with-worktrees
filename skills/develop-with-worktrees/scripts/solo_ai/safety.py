from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path

from .repo import GitRepo
from .util import SoloAIError


@dataclass(frozen=True)
class Finding:
    path: str
    line: int | None
    rule: str


_SENSITIVE_NAMES = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*credentials*.json",
    "*service-account*.json",
)
_CONTENT_RULES = (
    ("private-key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    (
        "github-token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    ),
    ("openai-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "assigned-secret",
        re.compile(
            r"(?i)\b(password|passwd|token|secret|api[_-]?key)\b\s*[:=]\s*[^\s$<{][^\s]*"
        ),
    ),
)


def _allowed(path: str, allowlist: tuple[str, ...]) -> bool:
    normalized = path.replace("\\", "/")
    return normalized in allowlist


def scan(
    repo: GitRepo,
    *,
    cwd: Path,
    base: str | None,
    staged: bool = False,
    allowlist: tuple[str, ...] = (),
) -> list[Finding]:
    name_args = ["diff", "--name-status", "--no-renames"]
    diff_args = ["diff", "--no-ext-diff", "--unified=0"]
    if staged:
        name_args.append("--cached")
        diff_args.append("--cached")
    elif base:
        name_args.append(f"{base}...HEAD")
        diff_args.append(f"{base}...HEAD")
    findings: list[Finding] = []
    for row in repo.git(name_args, cwd=cwd).stdout.splitlines():
        status, _, path = row.partition("\t")
        if status == "A" and not _allowed(path, allowlist):
            leaf = Path(path).name
            if any(fnmatch.fnmatchcase(leaf, pattern) for pattern in _SENSITIVE_NAMES):
                findings.append(Finding(path, None, "sensitive-file"))

    current_path = "unknown"
    current_line = 0
    for line in repo.git(diff_args, cwd=cwd).stdout.splitlines():
        if line.startswith("+++ b/"):
            current_path = line[6:]
            continue
        if line.startswith("@@"):
            match = re.search(r"\+(\d+)", line)
            current_line = int(match.group(1)) if match else 0
            continue
        if line.startswith("+") and not line.startswith("+++"):
            if not _allowed(current_path, allowlist):
                for rule, pattern in _CONTENT_RULES:
                    if pattern.search(line[1:]):
                        findings.append(
                            Finding(current_path, current_line or None, rule)
                        )
            current_line += 1
        elif not line.startswith("-"):
            current_line += 1
    return findings


def require_safe(
    repo: GitRepo,
    *,
    cwd: Path,
    base: str | None,
    staged: bool = False,
    allowlist: tuple[str, ...] = (),
) -> None:
    findings = scan(repo, cwd=cwd, base=base, staged=staged, allowlist=allowlist)
    if findings:
        summary = "\n".join(
            f"- {item.path}{':' + str(item.line) if item.line else ''}: {item.rule}"
            for item in findings
        )
        raise SoloAIError(
            f"Sensitive-content gate blocked the task:\n{summary}\nNo secret values were written to the report."
        )
