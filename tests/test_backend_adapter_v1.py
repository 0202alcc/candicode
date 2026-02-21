import json
import time
import unittest
from pathlib import Path

from agents.backend_adapter_v1 import (
    BackendStructuredModelClient,
    HostedModelClientAdapterV1,
    ValidatedBackendClientV1,
    build_agent_request_v1,
)
from agents.basic_agents import BasicAgentSuite
from agents.model_client import StaticHostedModelClient
from config.validator import validate_config
from orchestrator.supervisor import Task


ROOT = Path(__file__).resolve().parents[1]
REQUEST_SCHEMA = ROOT / "contracts" / "schemas" / "backend_request_v1.json"
RESPONSE_SCHEMA = ROOT / "contracts" / "schemas" / "backend_response_v1.json"
TRACE_SCHEMA = ROOT / "contracts" / "schemas" / "trace_event_v1.json"


class FakeAdapter:
    def __init__(self, response_payload: dict) -> None:
        self.response_payload = response_payload

    def chat(self, request_payload: dict) -> dict:
        payload = dict(self.response_payload)
        payload["request_id"] = request_payload["request_id"]
        return payload


class BackendAdapterV1Tests(unittest.TestCase):
    def test_request_builder_matches_schema(self) -> None:
        req_schema = json.loads(REQUEST_SCHEMA.read_text(encoding="utf-8"))
        req = build_agent_request_v1(
            request_id="req-1",
            agent_name="triage_agent",
            model="gpt-x",
            prompt="classify issue",
        )
        result = validate_config(req, req_schema)
        self.assertTrue(result.valid, msg=str(result.errors))

    def test_validated_client_accepts_valid_response(self) -> None:
        response = {
            "version": "v1",
            "request_id": "placeholder",
            "model": "gpt-x",
            "output_text": "{\"task_type\":\"bug\"}",
            "finish_reason": "stop",
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            },
            "trace_events": [
                {
                    "event_type": "reasoning_summary",
                    "timestamp": 1.0,
                    "phase": "triage",
                    "detail": "classified as bug",
                    "level": "info",
                }
            ],
        }
        client = ValidatedBackendClientV1.from_schema_paths(
            adapter=FakeAdapter(response),
            request_schema_path=REQUEST_SCHEMA,
            response_schema_path=RESPONSE_SCHEMA,
        )
        req = build_agent_request_v1(
            request_id="req-2",
            agent_name="triage_agent",
            model="gpt-x",
            prompt="classify issue",
        )
        out = client.chat(req)
        self.assertEqual("v1", out["version"])
        self.assertEqual("req-2", out["request_id"])

    def test_validated_client_rejects_invalid_response(self) -> None:
        bad_response = {
            "version": "v1",
            "request_id": "placeholder",
            "model": "gpt-x",
            "output_text": "oops",
            "finish_reason": "unknown",
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            },
            "trace_events": [],
        }
        client = ValidatedBackendClientV1.from_schema_paths(
            adapter=FakeAdapter(bad_response),
            request_schema_path=REQUEST_SCHEMA,
            response_schema_path=RESPONSE_SCHEMA,
        )
        req = build_agent_request_v1(
            request_id="req-3",
            agent_name="triage_agent",
            model="gpt-x",
            prompt="classify issue",
        )
        with self.assertRaises(ValueError):
            client.chat(req)

    def test_trace_event_schema_validates_event_shape(self) -> None:
        trace_schema = json.loads(TRACE_SCHEMA.read_text(encoding="utf-8"))
        event = {
            "event_type": "tool_call",
            "timestamp": 1.23,
            "phase": "execute",
            "detail": "running repo.search",
            "level": "info",
        }
        result = validate_config(event, trace_schema)
        self.assertTrue(result.valid, msg=str(result.errors))

    def test_round_trip_through_v1_adapter_boundary(self) -> None:
        structured = StaticHostedModelClient(
            responses={
                "triage_agent": {
                    "task_type": "bug",
                    "risk_level": "low",
                    "scope_size": "small",
                    "intensity": "normal",
                }
            }
        )
        adapter = HostedModelClientAdapterV1(structured_client=structured)
        validated = ValidatedBackendClientV1.from_schema_paths(
            adapter=adapter,
            request_schema_path=REQUEST_SCHEMA,
            response_schema_path=RESPONSE_SCHEMA,
        )
        client = BackendStructuredModelClient(
            validated_client=validated,
            default_model="external-model",
        )
        payload = client.generate_structured(
            agent_name="triage_agent",
            prompt="triage this issue",
            context={"request_id": "r1"},
        )
        self.assertEqual("bug", payload["task_type"])

    def test_basic_agent_suite_can_use_validated_backend_v1(self) -> None:
        structured = StaticHostedModelClient(
            responses={
                "triage_agent": {
                    "task_type": "bug",
                    "risk_level": "low",
                    "scope_size": "small",
                    "intensity": "normal",
                }
            }
        )
        adapter = HostedModelClientAdapterV1(structured_client=structured)
        validated = ValidatedBackendClientV1.from_schema_paths(
            adapter=adapter,
            request_schema_path=REQUEST_SCHEMA,
            response_schema_path=RESPONSE_SCHEMA,
        )
        suite = BasicAgentSuite.from_validated_backend_v1(
            validated_client=validated,
            default_model="external-model",
        )
        handlers = suite.build_phase_handlers()
        task = Task(task_id="t1", prompt="bug", priority="interactive", created_at=time.time())
        result = handlers["triage"](task)
        self.assertEqual("success", result.status)
        self.assertEqual("bug", task.agent_outputs["triage"]["task_type"])


if __name__ == "__main__":
    unittest.main()
