from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional


@dataclass(frozen=True)
class VersioningResult:
    ok: bool
    detail: str
    branch: Optional[str] = None


class VersioningAgent:
    def __init__(
        self,
        repo_path: str | Path,
        protected_branches: Optional[List[str]] = None,
        runner: Optional[Callable[[List[str], Path], subprocess.CompletedProcess]] = None,
    ) -> None:
        self.repo_path = Path(repo_path)
        self.protected_branches = protected_branches or ["main"]
        self.runner = runner or self._default_runner

    def create_branch_for_task(
        self, task_id: str, prompt: str, base_branch: str = "main"
    ) -> VersioningResult:
        if not self._is_git_repo():
            return VersioningResult(ok=False, detail="not a git repository")

        slug = self._slugify(prompt)
        branch_name = f"codex/{task_id}-{slug}"

        branch_exists = self._run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"]
        ).returncode == 0
        if branch_exists:
            checkout = self._run(["git", "checkout", branch_name])
            if checkout.returncode != 0:
                return VersioningResult(
                    ok=False,
                    detail=f"failed to checkout existing branch {branch_name}: {checkout.stderr.strip()}",
                )
            return VersioningResult(
                ok=True, detail=f"using existing branch {branch_name}", branch=branch_name
            )

        base_exists = self._run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{base_branch}"]
        ).returncode == 0
        if base_exists:
            checkout_base = self._run(["git", "checkout", base_branch])
            if checkout_base.returncode != 0:
                return VersioningResult(
                    ok=False,
                    detail=f"failed to checkout base branch {base_branch}: {checkout_base.stderr.strip()}",
                )

        create = self._run(["git", "checkout", "-b", branch_name])
        if create.returncode != 0:
            return VersioningResult(
                ok=False,
                detail=f"failed to create branch {branch_name}: {create.stderr.strip()}",
            )
        return VersioningResult(ok=True, detail="branch created", branch=branch_name)

    def ensure_not_protected_branch(self) -> VersioningResult:
        current = self.current_branch()
        if not current.ok:
            return current
        assert current.branch is not None
        if current.branch in self.protected_branches:
            return VersioningResult(
                ok=False,
                detail=f"protected branch edit blocked: {current.branch}",
                branch=current.branch,
            )
        return VersioningResult(
            ok=True, detail=f"active branch allowed: {current.branch}", branch=current.branch
        )

    def current_branch(self) -> VersioningResult:
        if not self._is_git_repo():
            return VersioningResult(ok=False, detail="not a git repository")

        # symbolic-ref works even on unborn branches where rev-parse HEAD can fail.
        symbolic = self._run(["git", "symbolic-ref", "--quiet", "--short", "HEAD"])
        if symbolic.returncode == 0 and symbolic.stdout.strip():
            branch = symbolic.stdout.strip()
            return VersioningResult(ok=True, detail="current branch resolved", branch=branch)

        rev_parse = self._run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
        if rev_parse.returncode != 0:
            fallback_branch = self._read_head_ref()
            if fallback_branch:
                return VersioningResult(
                    ok=True,
                    detail="current branch resolved from HEAD ref",
                    branch=fallback_branch,
                )
            stderr = rev_parse.stderr.strip() or symbolic.stderr.strip()
            return VersioningResult(
                ok=False, detail=f"failed to get current branch: {stderr}"
            )
        branch = rev_parse.stdout.strip()
        return VersioningResult(ok=True, detail="current branch resolved", branch=branch)

    def _is_git_repo(self) -> bool:
        result = self._run(["git", "rev-parse", "--is-inside-work-tree"])
        return result.returncode == 0 and result.stdout.strip() == "true"

    def _run(self, cmd: List[str]) -> subprocess.CompletedProcess:
        return self.runner(cmd, self.repo_path)

    def _read_head_ref(self) -> Optional[str]:
        head = self.repo_path / ".git" / "HEAD"
        if not head.exists():
            return None
        raw = head.read_text(encoding="utf-8").strip()
        prefix = "ref: refs/heads/"
        if raw.startswith(prefix):
            value = raw[len(prefix) :].strip()
            return value or None
        return None

    @staticmethod
    def _default_runner(cmd: List[str], cwd: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            cmd,
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
        )

    @staticmethod
    def _slugify(prompt: str) -> str:
        cleaned = re.sub(r"[^a-z0-9]+", "-", prompt.lower()).strip("-")
        if not cleaned:
            return "task"
        return cleaned[:40]
