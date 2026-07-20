from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .util import CommandResult, SoloAIError, run


@dataclass(frozen=True)
class WorktreeInfo:
    path: Path
    head: str | None
    branch: str | None
    bare: bool = False
    detached: bool = False


class GitRepo:
    def __init__(self, path: Path):
        probe = run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            cwd=path,
            check=False,
        )
        if probe.returncode != 0:
            raise SoloAIError(f"Not a Git working tree: {path}")
        self.root = Path(probe.stdout.strip()).resolve()
        common = self.git(["rev-parse", "--git-common-dir"]).stdout.strip()
        common_path = Path(common)
        if not common_path.is_absolute():
            common_path = self.root / common_path
        self.common_dir = common_path.resolve()
        self.local_dir = self.common_dir / "solo-ai"

    def local_json(self, name: str, default: Any) -> Any:
        from .util import read_json

        return read_json(self.local_dir / name, default)

    def policy_path(self) -> Path:
        """Return the tracked policy checkout, including a dirty-primary bootstrap."""
        config = self.root / ".solo-ai" / "config.toml"
        if config.exists():
            return self.root
        bootstrap = self.local_json("bootstrap.json", {})
        path = bootstrap.get("worktree")
        if (
            path
            and (candidate := Path(str(path))).is_dir()
            and (candidate / ".solo-ai" / "config.toml").exists()
        ):
            return candidate
        return self.root

    def git(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
        timeout: float | None = None,
    ) -> CommandResult:
        actual = cwd or self.root
        return run(
            ["git", "-C", str(actual), *args], cwd=actual, check=check, timeout=timeout
        )

    def worktrees(self) -> list[WorktreeInfo]:
        output = self.git(["worktree", "list", "--porcelain"]).stdout
        records: list[WorktreeInfo] = []
        current: dict[str, str | bool] = {}
        for line in [*output.splitlines(), ""]:
            if not line:
                if current:
                    records.append(
                        WorktreeInfo(
                            path=Path(str(current["worktree"])).resolve(),
                            head=str(current.get("HEAD"))
                            if current.get("HEAD")
                            else None,
                            branch=str(current.get("branch"))
                            if current.get("branch")
                            else None,
                            bare=bool(current.get("bare")),
                            detached=bool(current.get("detached")),
                        )
                    )
                    current = {}
                continue
            key, _, value = line.partition(" ")
            current[key] = value if value else True
        return records

    @property
    def primary_path(self) -> Path:
        trees = self.worktrees()
        if not trees:
            raise SoloAIError("Git reports no working trees")
        return trees[0].path

    def default_branch(self) -> str:
        override = self.git(
            ["config", "--local", "--get", "solo-ai.default-branch"], check=False
        )
        if override.returncode == 0 and override.stdout.strip():
            return override.stdout.strip()

        remote = self.git(
            ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
            check=False,
        )
        if remote.returncode == 0 and "/" in remote.stdout.strip():
            return remote.stdout.strip().split("/", 1)[1]

        symbolic = self.git(["symbolic-ref", "--quiet", "--short", "HEAD"], check=False)
        if symbolic.returncode == 0 and symbolic.stdout.strip():
            candidate = symbolic.stdout.strip()
            if candidate in {"main", "master", "develop", "trunk"}:
                return candidate

        branches = self.git(
            ["for-each-ref", "--format=%(refname:short)", "refs/heads"]
        ).stdout.splitlines()
        for candidate in ("main", "master", "develop", "trunk"):
            if candidate in branches:
                return candidate
        if len(branches) == 1:
            return branches[0]
        raise SoloAIError(
            "Cannot determine the local default branch; set git config solo-ai.default-branch NAME"
        )

    def head(self, cwd: Path | None = None) -> str:
        return self.git(["rev-parse", "HEAD"], cwd=cwd).stdout.strip()

    def tree(self, ref: str = "HEAD", cwd: Path | None = None) -> str:
        return self.git(["rev-parse", f"{ref}^{{tree}}"], cwd=cwd).stdout.strip()

    def branch(self, cwd: Path | None = None) -> str | None:
        result = self.git(
            ["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=cwd, check=False
        )
        return result.stdout.strip() if result.returncode == 0 else None

    def is_clean(
        self, cwd: Path | None = None, *, include_untracked: bool = True
    ) -> bool:
        args = ["status", "--porcelain=v1"]
        if not include_untracked:
            args.append("--untracked-files=no")
        return not self.git(args, cwd=cwd).stdout.strip()

    def status_lines(self, cwd: Path | None = None) -> list[str]:
        return [
            line
            for line in self.git(
                ["status", "--porcelain=v1"], cwd=cwd
            ).stdout.splitlines()
            if line
        ]

    def ensure_primary_default(self) -> tuple[Path, str]:
        primary = self.primary_path
        default = self.default_branch()
        if self.branch(primary) != default:
            raise SoloAIError(f"Primary worktree must have {default!r} checked out")
        return primary, default

    def ensure_default_primary_clean(self) -> tuple[Path, str]:
        primary, default = self.ensure_primary_default()
        if not self.is_clean(primary):
            raise SoloAIError("Primary worktree must be clean before integration")
        return primary, default

    def merge_base(self, left: str, right: str, cwd: Path | None = None) -> str:
        return self.git(["merge-base", left, right], cwd=cwd).stdout.strip()

    def is_ancestor(
        self, ancestor: str, descendant: str, cwd: Path | None = None
    ) -> bool:
        return (
            self.git(
                ["merge-base", "--is-ancestor", ancestor, descendant],
                cwd=cwd,
                check=False,
            ).returncode
            == 0
        )

    def tracked(self, relative: str, cwd: Path | None = None) -> bool:
        return (
            self.git(
                ["ls-files", "--error-unmatch", "--", relative], cwd=cwd, check=False
            ).returncode
            == 0
        )

    def add_local_exclude(self, pattern: str) -> None:
        exclude = self.common_dir / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = (
            exclude.read_text(encoding="utf-8", errors="replace")
            if exclude.exists()
            else ""
        )
        lines = existing.splitlines()
        if pattern not in lines:
            with exclude.open("a", encoding="utf-8", newline="\n") as handle:
                if existing and not existing.endswith("\n"):
                    handle.write("\n")
                handle.write(pattern + "\n")

    def ignored_untracked(self, cwd: Path) -> list[str]:
        output = self.git(
            ["ls-files", "--others", "--ignored", "--exclude-standard", "-z"], cwd=cwd
        ).stdout
        return sorted(item for item in output.split("\0") if item)

    def untracked(self, cwd: Path) -> list[str]:
        output = self.git(
            ["ls-files", "--others", "--exclude-standard", "-z"], cwd=cwd
        ).stdout
        return sorted(item for item in output.split("\0") if item)

    def changed_paths(self, cwd: Path) -> list[str]:
        """All tracked and untracked paths that would be included in a task commit."""
        changed = self.git(["diff", "--name-only", "-z"], cwd=cwd).stdout.split("\0")
        staged = self.git(
            ["diff", "--cached", "--name-only", "-z"], cwd=cwd
        ).stdout.split("\0")
        return sorted(
            {item for item in [*changed, *staged, *self.untracked(cwd)] if item}
        )

    def default_head(self) -> str:
        return self.git(["rev-parse", self.default_branch()]).stdout.strip()
