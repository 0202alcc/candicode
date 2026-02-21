import tempfile
import unittest
from pathlib import Path

from agents.model_client import StaticHostedModelClient
from integration.opencode_plugin import OpencodePipelinePlugin
from orchestrator.supervisor import Task
from orchestrator.tool_runner import ToolRequest


def _responses():
    return {
        "intent_agent": {
            "rewritten_prompt": "Update requested UI files and return full file_edits.",
            "target_files": [],
            "success_criteria": ["canvas only with black padding"],
            "proposed_new_files": [],
        },
        "triage_agent": {
            "task_type": "bug",
            "risk_level": "low",
            "scope_size": "small",
            "intensity": "normal",
        },
        "requirements_agent": {
            "acceptance_criteria": ["ac1"],
            "non_goals": [],
            "open_questions": [],
        },
        "planner_agent": {"steps": ["s1"], "test_plan": ["unit"], "risk_notes": []},
        "coder_agent": {
            "changes": ["chg"],
            "files_touched": [],
            "file_edits": [],
        },
        "test_agent": {"tests_added": ["tests/test_file.py"], "checks_to_run": ["unit"]},
        "reviewer_agent": {"findings": [], "risk_summary": "low risk", "required_fixes": []},
        "docs_agent": {"changelog": "updated", "runbook_delta": "none", "migration_notes": "none"},
    }


def _intent_only_response(target_files):
    return {
        "intent_agent": {
            "rewritten_prompt": "Simplify UI layout",
            "target_files": target_files,
            "success_criteria": ["canvas only"],
            "proposed_new_files": [],
        }
    }


class OpencodePluginTests(unittest.TestCase):
    def test_handle_prompt_returns_pipeline_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            # Mirror required schemas into temp repo view by pointing to project root instead.
            plugin = OpencodePipelinePlugin(
                repo_root=Path(__file__).resolve().parents[1],
                model_client=StaticHostedModelClient(responses=_responses()),
                workspace_root=repo,
            )
            result = plugin.handle_prompt("fix parser")
            self.assertEqual("completed", result.status)
            self.assertEqual("finalize", result.final_phase)

    def test_dry_run_mode_through_plugin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plugin = OpencodePipelinePlugin(
                repo_root=Path(__file__).resolve().parents[1],
                model_client=StaticHostedModelClient(responses=_responses()),
                workspace_root=Path(tmp),
            )
            result = plugin.handle_prompt("refactor", dry_run=True)
            self.assertEqual("completed", result.status)

    def test_repo_write_blocks_new_file_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            plugin = OpencodePipelinePlugin(
                repo_root=Path(__file__).resolve().parents[1],
                model_client=StaticHostedModelClient(responses=_responses()),
                workspace_root=repo,
            )
            result = plugin._handle_repo_write(
                ToolRequest(
                    tool="repo.write",
                    params={"path": "new.txt", "content": "x"},
                    trusted=True,
                )
            )
            self.assertFalse(result.ok)
            self.assertIn("blocked for new file", result.detail)

    def test_repo_write_resolves_unique_existing_basename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            target = repo / "web_client" / "index.html"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("before", encoding="utf-8")
            plugin = OpencodePipelinePlugin(
                repo_root=Path(__file__).resolve().parents[1],
                model_client=StaticHostedModelClient(responses=_responses()),
                workspace_root=repo,
            )
            result = plugin._handle_repo_write(
                ToolRequest(
                    tool="repo.write",
                    params={"path": "index.html", "content": "after"},
                    trusted=True,
                )
            )
            self.assertTrue(result.ok)
            self.assertIn("resolved from", result.detail)
            self.assertEqual("after", target.read_text(encoding="utf-8"))

    def test_repo_write_fails_when_basename_is_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            for rel in ["a/index.html", "b/index.html"]:
                path = repo / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("x", encoding="utf-8")
            plugin = OpencodePipelinePlugin(
                repo_root=Path(__file__).resolve().parents[1],
                model_client=StaticHostedModelClient(responses=_responses()),
                workspace_root=repo,
            )
            result = plugin._handle_repo_write(
                ToolRequest(
                    tool="repo.write",
                    params={"path": "index.html", "content": "after"},
                    trusted=True,
                )
            )
            self.assertFalse(result.ok)
            self.assertIn("ambiguous path", result.detail)

    def test_intent_phase_blocks_on_ambiguous_target_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            for rel in ["web/index.html", "web_client/index.html"]:
                path = repo / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("x", encoding="utf-8")

            plugin = OpencodePipelinePlugin(
                repo_root=Path(__file__).resolve().parents[1],
                model_client=StaticHostedModelClient(responses=_intent_only_response(["index.html"])),
                workspace_root=repo,
            )
            result = plugin.handle_prompt("simplify ui")
            self.assertEqual("blocked", result.status)
            self.assertEqual("intent", result.final_phase)
            self.assertIn("ambiguous", result.final_detail)

    def test_intent_phase_infers_targets_from_repo_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            for rel in ["web_client/index.html", "web_client/styles/main.css"]:
                path = repo / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("x", encoding="utf-8")

            plugin = OpencodePipelinePlugin(
                repo_root=Path(__file__).resolve().parents[1],
                model_client=StaticHostedModelClient(responses=_intent_only_response(["app.js", "canvas.js"])),
                workspace_root=repo,
            )
            task = Task(
                task_id="t1",
                prompt="simplify web interface to canvas with black padding",
                priority="interactive",
                created_at=1.0,
            )
            result = plugin.supervisor.phase_handlers["intent"](task)
            self.assertEqual("success", result.status)
            targets = task.agent_outputs["intent"]["target_files"]
            self.assertTrue(any(t.endswith("index.html") for t in targets))


if __name__ == "__main__":
    unittest.main()
