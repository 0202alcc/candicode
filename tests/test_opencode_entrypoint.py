import json
import os
from pathlib import Path
import subprocess
import unittest
from argparse import Namespace

from scripts.opencode_entrypoint import (
    _provider_error_text,
    _resolve_client_mode,
    _resolve_provider_settings,
)


class OpencodeEntrypointTests(unittest.TestCase):
    def test_resolve_client_mode_honors_explicit_setting(self) -> None:
        self.assertEqual("static", _resolve_client_mode("static", None, None))
        self.assertEqual("provider", _resolve_client_mode("provider", None, None))

    def test_resolve_client_mode_auto_uses_provider_when_env_is_set(self) -> None:
        prev = dict(os.environ)
        try:
            os.environ["OPENCODE_LLM_API_KEY"] = "k"
            os.environ["OPENCODE_LLM_MODEL"] = "m"
            self.assertEqual("provider", _resolve_client_mode("auto", None, None))

            os.environ.pop("OPENCODE_LLM_API_KEY", None)
            self.assertEqual("static", _resolve_client_mode("auto", None, None))
        finally:
            os.environ.clear()
            os.environ.update(prev)

    def test_resolve_client_mode_auto_uses_provider_when_model_flag_and_key(self) -> None:
        prev = dict(os.environ)
        try:
            os.environ["OPENCODE_PROVIDER_OPENAI_API_KEY"] = "k"
            self.assertEqual("provider", _resolve_client_mode("auto", "openai", "gpt-4.1-mini"))
        finally:
            os.environ.clear()
            os.environ.update(prev)

    def test_resolve_client_mode_auto_falls_back_for_cloud_without_key(self) -> None:
        prev = dict(os.environ)
        try:
            os.environ.pop("OPENCODE_LLM_API_KEY", None)
            os.environ.pop("OPENCODE_PROVIDER_OPENAI_API_KEY", None)
            self.assertEqual("static", _resolve_client_mode("auto", "openai", "gpt-4.1-mini"))
        finally:
            os.environ.clear()
            os.environ.update(prev)

    def test_resolve_client_mode_auto_uses_provider_for_local_provider(self) -> None:
        self.assertEqual("provider", _resolve_client_mode("auto", "localpc", "qwen2.5-coder"))

    def test_provider_error_text_flags_retriable_output_shape_errors(self) -> None:
        self.assertTrue(_provider_error_text("intent_agent error: Provider response message.content is empty"))
        self.assertTrue(_provider_error_text("triage_agent error: Provider returned non-JSON output: <empty>"))

    def test_resolve_provider_settings_supports_provider_specific_env(self) -> None:
        prev = dict(os.environ)
        try:
            os.environ["OPENCODE_PROVIDER_OPENAI_API_KEY"] = "provider-key"
            os.environ["OPENCODE_PROVIDER_OPENAI_BASE_URL"] = "https://api.openai.example/v1"
            os.environ["OPENCODE_PROVIDER_OPENAI_TIMEOUT_SECONDS"] = "30"
            os.environ["OPENCODE_PROVIDER_OPENAI_HEADERS_JSON"] = '{"X-Test":"1"}'
            args = Namespace(provider_id="openai", model_id="gpt-x")
            settings = _resolve_provider_settings(args)
            self.assertEqual("provider-key", settings["api_key"])
            self.assertEqual("https://api.openai.example/v1", settings["base_url"])
            self.assertEqual(30, settings["timeout_seconds"])
            self.assertEqual("gpt-x", settings["model"])
            self.assertEqual("1", settings["headers"]["X-Test"])
        finally:
            os.environ.clear()
            os.environ.update(prev)

    def test_resolve_provider_settings_allows_local_without_api_key(self) -> None:
        prev = dict(os.environ)
        try:
            os.environ.pop("OPENCODE_LLM_API_KEY", None)
            args = Namespace(provider_id="localpc", model_id="qwen2.5-coder")
            settings = _resolve_provider_settings(args)
            self.assertEqual("", settings["api_key"])
            self.assertEqual("http://127.0.0.1:11434/v1", settings["base_url"])
        finally:
            os.environ.clear()
            os.environ.update(prev)

    def test_resolve_provider_settings_requires_api_key_for_cloud(self) -> None:
        prev = dict(os.environ)
        try:
            os.environ.pop("OPENCODE_LLM_API_KEY", None)
            args = Namespace(provider_id="openai", model_id="gpt-4.1-mini")
            with self.assertRaisesRegex(ValueError, "Missing API key"):
                _resolve_provider_settings(args)
        finally:
            os.environ.clear()
            os.environ.update(prev)

    def test_entrypoint_returns_json_error_payload_on_provider_misconfig(self) -> None:
        prev = dict(os.environ)
        try:
            os.environ.pop("OPENCODE_LLM_API_KEY", None)
            os.environ.pop("OPENCODE_PROVIDER_OPENAI_API_KEY", None)
            proc = subprocess.run(
                [
                    "python3",
                    str((Path(__file__).resolve().parents[1] / "scripts" / "opencode_entrypoint.py")),
                    "test",
                    "--model-client",
                    "provider",
                    "--provider-id",
                    "openai",
                    "--model-id",
                    "gpt-4.1-mini",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, proc.returncode)
            payload = json.loads(proc.stdout)
            self.assertEqual("failed", payload["status"])
            self.assertEqual("entrypoint", payload["final_phase"])
        finally:
            os.environ.clear()
            os.environ.update(prev)


if __name__ == "__main__":
    unittest.main()
