import unittest

from orchestrator.tool_runner import ToolRequest, ToolResult, ToolRunner


class ToolRunnerTests(unittest.TestCase):
    def test_safe_tool_runs_with_trusted_context(self) -> None:
        runner = ToolRunner(
            handlers={
                "repo.search": lambda req: ToolResult(
                    ok=True, detail="search complete", output=req.params.get("query")
                )
            }
        )
        result = runner.run(
            ToolRequest(tool="repo.search", params={"query": "bug"}, trusted=True)
        )
        self.assertTrue(result.ok)
        self.assertEqual("search complete", result.detail)

    def test_untrusted_context_is_limited_to_read_only_tools(self) -> None:
        runner = ToolRunner(
            handlers={
                "test.run": lambda req: ToolResult(ok=True, detail="tests ran")
            }
        )
        result = runner.run(ToolRequest(tool="test.run", params={}, trusted=False))
        self.assertFalse(result.ok)
        self.assertIn("untrusted context cannot run tool", result.detail)

    def test_risky_tool_requires_human_approval(self) -> None:
        runner = ToolRunner(
            handlers={
                "deploy.run": lambda req: ToolResult(ok=True, detail="deployed")
            }
        )
        blocked = runner.run(ToolRequest(tool="deploy.run", params={}, trusted=True))
        self.assertFalse(blocked.ok)
        self.assertIn("requires explicit human approval", blocked.detail)

        allowed = runner.run(
            ToolRequest(
                tool="deploy.run", params={}, trusted=True, human_approved=True
            )
        )
        self.assertTrue(allowed.ok)
        self.assertEqual("deployed", allowed.detail)

    def test_tool_must_be_allow_listed(self) -> None:
        runner = ToolRunner(handlers={"custom.tool": lambda req: ToolResult(ok=True, detail="x")})
        result = runner.run(ToolRequest(tool="custom.tool", params={}, trusted=True))
        self.assertFalse(result.ok)
        self.assertIn("not allow-listed", result.detail)


if __name__ == "__main__":
    unittest.main()
