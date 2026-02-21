import time
import unittest

from agents.basic_agents import BasicAgentSuite
from agents.model_client import StaticHostedModelClient
from orchestrator.supervisor import Task


class RecordingModelClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def generate_structured(self, agent_name, prompt, context):
        self.calls.append((agent_name, prompt))
        return self.responses[agent_name]


class BasicAgentSuiteTests(unittest.TestCase):
    def test_handlers_store_agent_outputs(self) -> None:
        suite = BasicAgentSuite(
            StaticHostedModelClient(
                responses={
                    "triage_agent": {
                        "task_type": "bug",
                        "risk_level": "low",
                        "scope_size": "small",
                        "intensity": "normal",
                    }
                }
            )
        )
        handlers = suite.build_phase_handlers()
        task = Task(task_id="t1", prompt="p", priority="interactive", created_at=time.time())

        result = handlers["triage"](task)
        self.assertEqual("success", result.status)
        self.assertIn("triage", task.agent_outputs)
        self.assertEqual("bug", task.agent_outputs["triage"]["task_type"])

    def test_handler_fails_when_model_missing_response(self) -> None:
        suite = BasicAgentSuite(StaticHostedModelClient(responses={}))
        handlers = suite.build_phase_handlers()
        task = Task(task_id="t1", prompt="p", priority="interactive", created_at=time.time())

        result = handlers["triage"](task)
        self.assertEqual("failed", result.status)
        self.assertIn("error", result.detail)

    def test_strict_code_requires_file_edits(self) -> None:
        suite = BasicAgentSuite(
            StaticHostedModelClient(
                responses={
                    "coder_agent": {
                        "changes": ["edit"],
                        "files_touched": ["src/a.py"],
                    }
                }
            ),
            strict_code_file_edits=True,
        )
        handlers = suite.build_phase_handlers()
        task = Task(task_id="t1", prompt="p", priority="interactive", created_at=time.time())

        result = handlers["code"](task)
        self.assertEqual("failed", result.status)
        self.assertIn("file_edits", result.detail)

    def test_strict_code_accepts_matching_file_edits(self) -> None:
        suite = BasicAgentSuite(
            StaticHostedModelClient(
                responses={
                    "coder_agent": {
                        "changes": ["edit"],
                        "files_touched": ["src/a.py"],
                        "file_edits": [{"path": "src/a.py", "content": "print('ok')\n"}],
                    }
                }
            ),
            strict_code_file_edits=True,
        )
        handlers = suite.build_phase_handlers()
        task = Task(task_id="t1", prompt="p", priority="interactive", created_at=time.time())

        result = handlers["code"](task)
        self.assertEqual("success", result.status)

    def test_strict_code_rejects_paths_outside_intent_targets(self) -> None:
        suite = BasicAgentSuite(
            StaticHostedModelClient(
                responses={
                    "coder_agent": {
                        "changes": ["edit"],
                        "files_touched": ["src/other.py"],
                        "file_edits": [{"path": "src/other.py", "content": "print('ok')\n"}],
                    }
                }
            ),
            strict_code_file_edits=True,
        )
        handlers = suite.build_phase_handlers()
        task = Task(task_id="t1", prompt="p", priority="interactive", created_at=time.time())
        task.agent_outputs["intent"] = {
            "rewritten_prompt": "Edit src/app.py",
            "target_files": ["src/app.py"],
            "success_criteria": [],
        }

        result = handlers["code"](task)
        self.assertEqual("failed", result.status)
        self.assertIn("outside intent target_files", result.detail)

    def test_non_intent_phases_use_rewritten_prompt(self) -> None:
        client = RecordingModelClient(
            responses={
                "intent_agent": {
                    "rewritten_prompt": "Edit src/app.tsx to simplify layout; return file_edits.",
                    "target_files": ["src/app.tsx"],
                    "success_criteria": ["canvas only"],
                },
                "requirements_agent": {
                    "acceptance_criteria": ["ac"],
                    "non_goals": [],
                    "open_questions": [],
                },
            }
        )
        suite = BasicAgentSuite(client)
        handlers = suite.build_phase_handlers()
        task = Task(task_id="t1", prompt="simplify web ui", priority="interactive", created_at=time.time())

        handlers["intent"](task)
        handlers["requirements"](task)

        self.assertEqual("simplify web ui", client.calls[0][1])
        self.assertIn("Edit src/app.tsx", client.calls[1][1])


if __name__ == "__main__":
    unittest.main()
