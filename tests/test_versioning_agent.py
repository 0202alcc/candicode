import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, Tuple

from orchestrator.versioning import VersioningAgent


def _cp(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class FakeRunner:
    def __init__(self, mapping: Dict[Tuple[str, ...], subprocess.CompletedProcess]) -> None:
        self.mapping = mapping
        self.calls: List[Tuple[str, ...]] = []

    def __call__(self, cmd: List[str], cwd: Path) -> subprocess.CompletedProcess:
        key = tuple(cmd)
        self.calls.append(key)
        return self.mapping.get(key, _cp(1, "", "unexpected command"))


class VersioningAgentTests(unittest.TestCase):
    def test_create_branch_for_task_success(self) -> None:
        runner = FakeRunner(
            {
                ("git", "rev-parse", "--is-inside-work-tree"): _cp(0, "true\n"),
                (
                    "git",
                    "show-ref",
                    "--verify",
                    "--quiet",
                    "refs/heads/codex/task123-fix-ci",
                ): _cp(1),
                ("git", "show-ref", "--verify", "--quiet", "refs/heads/main"): _cp(0),
                ("git", "checkout", "main"): _cp(0),
                ("git", "checkout", "-b", "codex/task123-fix-ci"): _cp(0),
            }
        )
        agent = VersioningAgent("/tmp/repo", runner=runner)
        result = agent.create_branch_for_task("task123", "Fix CI")

        self.assertTrue(result.ok)
        self.assertEqual("codex/task123-fix-ci", result.branch)

    def test_create_branch_fails_when_not_git_repo(self) -> None:
        runner = FakeRunner(
            {
                ("git", "rev-parse", "--is-inside-work-tree"): _cp(1, "", "fatal"),
            }
        )
        agent = VersioningAgent("/tmp/repo", runner=runner)
        result = agent.create_branch_for_task("task123", "Fix CI")
        self.assertFalse(result.ok)
        self.assertIn("not a git repository", result.detail)

    def test_ensure_not_protected_branch_blocks_main(self) -> None:
        runner = FakeRunner(
            {
                ("git", "rev-parse", "--is-inside-work-tree"): _cp(0, "true\n"),
                ("git", "symbolic-ref", "--quiet", "--short", "HEAD"): _cp(0, "main\n"),
            }
        )
        agent = VersioningAgent("/tmp/repo", runner=runner, protected_branches=["main"])
        result = agent.ensure_not_protected_branch()
        self.assertFalse(result.ok)
        self.assertEqual("main", result.branch)
        self.assertIn("protected branch edit blocked", result.detail)

    def test_current_branch_falls_back_when_symbolic_ref_fails(self) -> None:
        runner = FakeRunner(
            {
                ("git", "rev-parse", "--is-inside-work-tree"): _cp(0, "true\n"),
                ("git", "symbolic-ref", "--quiet", "--short", "HEAD"): _cp(1, "", "detached"),
                ("git", "rev-parse", "--abbrev-ref", "HEAD"): _cp(0, "feature/x\n"),
            }
        )
        agent = VersioningAgent("/tmp/repo", runner=runner, protected_branches=["main"])
        result = agent.current_branch()
        self.assertTrue(result.ok)
        self.assertEqual("feature/x", result.branch)

    def test_current_branch_reads_git_head_ref_when_git_commands_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".git").mkdir(parents=True, exist_ok=True)
            (repo / ".git" / "HEAD").write_text("ref: refs/heads/codex/work\n", encoding="utf-8")
            runner = FakeRunner(
                {
                    ("git", "rev-parse", "--is-inside-work-tree"): _cp(0, "true\n"),
                    ("git", "symbolic-ref", "--quiet", "--short", "HEAD"): _cp(1, "", "unavailable"),
                    ("git", "rev-parse", "--abbrev-ref", "HEAD"): _cp(
                        128,
                        "",
                        "fatal: ambiguous argument 'HEAD': unknown revision",
                    ),
                }
            )
            agent = VersioningAgent(repo, runner=runner)
            result = agent.current_branch()
            self.assertTrue(result.ok)
            self.assertEqual("codex/work", result.branch)


if __name__ == "__main__":
    unittest.main()
