import io
import json
import os
import unittest
import urllib.error
from unittest import mock

from agents.provider_client import OpenAICompatibleHostedModelClient


class _FakeHTTPResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class ProviderClientTests(unittest.TestCase):
    def test_generate_structured_calls_provider_and_parses_json(self) -> None:
        response = {
            "choices": [
                {
                    "message": {
                        "content": '{"task_type":"bug","risk_level":"low","scope_size":"small","intensity":"normal"}'
                    }
                }
            ]
        }
        captured = {}

        def _urlopen(req, timeout=None, context=None):
            captured["url"] = req.full_url
            captured["auth"] = req.get_header("Authorization")
            headers = {k.lower(): v for k, v in req.header_items()}
            captured["x_test"] = headers.get("x-test")
            captured["timeout"] = timeout
            captured["payload"] = json.loads(req.data.decode("utf-8"))
            return _FakeHTTPResponse(response)

        with mock.patch("urllib.request.urlopen", side_effect=_urlopen):
            client = OpenAICompatibleHostedModelClient(
                api_key="test-key",
                model="test-model",
                base_url="https://api.provider.example/v1",
                timeout_seconds=5,
                headers={"X-Test": "abc"},
            )
            out = client.generate_structured("triage_agent", "triage this", {"request_id": "r1"})

        self.assertEqual("bug", out["task_type"])
        self.assertEqual("https://api.provider.example/v1/chat/completions", captured["url"])
        self.assertEqual("Bearer test-key", captured["auth"])
        self.assertEqual("abc", captured["x_test"])
        self.assertEqual("test-model", captured["payload"]["model"])
        self.assertEqual(5, captured["timeout"])

    def test_generate_structured_parses_text_parts_and_json_fence(self) -> None:
        response = {
            "choices": [
                {
                    "message": {
                        "content": [
                            {
                                "type": "text",
                                "text": '```json\n{"changes":["x"],"files_touched":["a.py"]}\n```',
                            }
                        ]
                    }
                }
            ]
        }
        with mock.patch("urllib.request.urlopen", return_value=_FakeHTTPResponse(response)):
            client = OpenAICompatibleHostedModelClient(
                api_key="test-key",
                model="test-model",
                base_url="https://api.provider.example/v1",
                timeout_seconds=5,
            )
            out = client.generate_structured("coder_agent", "code this", {})

        self.assertEqual(["x"], out["changes"])

    def test_generate_structured_surfaces_http_errors(self) -> None:
        error = urllib.error.HTTPError(
            url="https://api.provider.example/v1/chat/completions",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=io.BytesIO(b'{"error":"bad key"}'),
        )
        with mock.patch("urllib.request.urlopen", side_effect=error):
            client = OpenAICompatibleHostedModelClient(
                api_key="bad-key",
                model="test-model",
                base_url="https://api.provider.example/v1",
                timeout_seconds=5,
            )
            with self.assertRaisesRegex(ValueError, "HTTP error 401"):
                client.generate_structured("triage_agent", "triage this", {})

    def test_generate_structured_rejects_empty_content_with_clear_error(self) -> None:
        response = {"choices": [{"message": {"content": "   "}}]}
        with mock.patch("urllib.request.urlopen", return_value=_FakeHTTPResponse(response)):
            client = OpenAICompatibleHostedModelClient(
                api_key="test-key",
                model="test-model",
                base_url="https://api.provider.example/v1",
                timeout_seconds=5,
            )
            with self.assertRaisesRegex(ValueError, "message.content is empty"):
                client.generate_structured("triage_agent", "triage this", {})

    def test_from_env_requires_api_key_and_model(self) -> None:
        prev = dict(os.environ)
        try:
            os.environ.pop("OPENCODE_LLM_API_KEY", None)
            os.environ.pop("OPENCODE_LLM_MODEL", None)
            with self.assertRaisesRegex(ValueError, "Missing OPENCODE_LLM_API_KEY"):
                OpenAICompatibleHostedModelClient.from_env()

            os.environ["OPENCODE_LLM_API_KEY"] = "x"
            with self.assertRaisesRegex(ValueError, "Missing OPENCODE_LLM_MODEL"):
                OpenAICompatibleHostedModelClient.from_env()

            os.environ["OPENCODE_LLM_MODEL"] = "m"
            client = OpenAICompatibleHostedModelClient.from_env(require_api_key=False)
            self.assertEqual("x", client.api_key)
        finally:
            os.environ.clear()
            os.environ.update(prev)

    def test_post_json_skips_auth_header_when_api_key_empty(self) -> None:
        captured = {}

        def _urlopen(req, timeout=None, context=None):
            captured["auth"] = req.get_header("Authorization")
            response = {
                "choices": [{"message": {"content": '{"result":"ok"}'}}],
            }
            return _FakeHTTPResponse(response)

        with mock.patch("urllib.request.urlopen", side_effect=_urlopen):
            client = OpenAICompatibleHostedModelClient(
                api_key="",
                model="test-model",
                base_url="https://api.provider.example/v1",
                timeout_seconds=5,
            )
            out = client.generate_structured("unknown_agent", "noop", {})

        self.assertEqual("ok", out["result"])
        self.assertIsNone(captured["auth"])


if __name__ == "__main__":
    unittest.main()
