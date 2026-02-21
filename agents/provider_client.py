from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Dict, Optional

from agents.model_client import HostedModelClient


@dataclass
class OpenAICompatibleHostedModelClient(HostedModelClient):
    """
    Provider-backed HostedModelClient using an OpenAI-compatible Chat Completions API.
    """

    api_key: str
    model: str
    base_url: str = "https://api.openai.com/v1"
    timeout_seconds: int = 60
    headers: Optional[Dict[str, str]] = None

    @classmethod
    def from_env(cls, require_api_key: bool = True) -> "OpenAICompatibleHostedModelClient":
        api_key = os.getenv("OPENCODE_LLM_API_KEY", "").strip()
        model = os.getenv("OPENCODE_LLM_MODEL", "").strip()
        base_url = os.getenv("OPENCODE_LLM_BASE_URL", "https://api.openai.com/v1").strip()
        timeout = int(os.getenv("OPENCODE_LLM_TIMEOUT_SECONDS", "60"))
        if require_api_key and not api_key:
            raise ValueError("Missing OPENCODE_LLM_API_KEY")
        if not model:
            raise ValueError("Missing OPENCODE_LLM_MODEL")
        return cls(api_key=api_key, model=model, base_url=base_url, timeout_seconds=timeout)

    def generate_structured(self, agent_name: str, prompt: str, context: Dict) -> Dict:
        user_prompt = self._build_user_prompt(agent_name=agent_name, prompt=prompt, context=context)
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a strict JSON generator for software-engineering agents. "
                        "Return only a valid JSON object and no extra text."
                    ),
                },
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
        }

        response = self._post_json(f"{self.base_url.rstrip('/')}/chat/completions", payload)
        content = self._extract_content(response)
        return self._parse_json_object(content)

    def _post_json(self, url: str, payload: Dict) -> Dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.headers:
            headers.update(self.headers)
        req = urllib.request.Request(
            url=url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise ValueError(f"Provider HTTP error {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise ValueError(f"Provider connection error: {exc}") from exc

    @staticmethod
    def _extract_content(response: Dict) -> str:
        choices = response.get("choices")
        if not choices or not isinstance(choices, list):
            raise ValueError("Provider response missing choices")
        first = choices[0]
        msg = first.get("message") if isinstance(first, dict) else None
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text" and isinstance(item.get("text"), str):
                    parts.append(item["text"])
            if parts:
                return "\n".join(parts).strip()
        raise ValueError("Provider response missing message.content")

    @staticmethod
    def _parse_json_object(text: str) -> Dict:
        text = _strip_markdown_json_fence(text)
        try:
            parsed = json.loads(text)
        except Exception as exc:
            raise ValueError(f"Provider returned non-JSON output: {text[:200]}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("Provider JSON output must be an object")
        return parsed

    @staticmethod
    def _build_user_prompt(agent_name: str, prompt: str, context: Dict) -> str:
        schema_hint = _schema_for_agent(agent_name)
        extra_rules = ""
        if agent_name == "intent_agent":
            extra_rules = (
                "Additional rules for intent_agent:\n"
                "- Rewrite the user request into a concrete engineering prompt.\n"
                "- Include exact relative file paths only when confidently resolved.\n"
                "- If the task likely needs new files, set proposed_new_files.\n"
                "- Keep rewritten_prompt actionable and implementation-oriented.\n\n"
            )
        if agent_name == "coder_agent":
            extra_rules = (
                "Additional rules for coder_agent:\n"
                "- If files_touched is non-empty, include file_edits.\n"
                "- file_edits must include every path from files_touched.\n"
                "- Each file_edits item must include full replacement content.\n\n"
            )
        return (
            f"Agent: {agent_name}\n"
            f"Task prompt:\n{prompt}\n\n"
            f"Context JSON:\n{json.dumps(context, sort_keys=True)}\n\n"
            f"{extra_rules}"
            "Return exactly one JSON object matching this schema hint:\n"
            f"{schema_hint}\n"
        )


def _schema_for_agent(agent_name: str) -> str:
    hints = {
        "intent_agent": '{"rewritten_prompt":"string","target_files":["string"],"success_criteria":["string"],"proposed_new_files":["string"]}',
        "triage_agent": '{"task_type":"string","risk_level":"low|medium|high","scope_size":"small|medium|large","intensity":"fast|normal|deep"}',
        "requirements_agent": '{"acceptance_criteria":["string"],"non_goals":["string"],"open_questions":["string"]}',
        "planner_agent": '{"steps":["string"],"test_plan":["string"],"risk_notes":["string"]}',
        "coder_agent": '{"changes":["string"],"files_touched":["string"],"file_edits":[{"path":"string","content":"string"}]}',
        "test_agent": '{"tests_added":["string"],"checks_to_run":["string"]}',
        "reviewer_agent": '{"findings":["string"],"risk_summary":"string","required_fixes":["string"]}',
        "docs_agent": '{"changelog":"string","runbook_delta":"string","migration_notes":"string"}',
    }
    return hints.get(agent_name, '{"result":"string"}')


def _strip_markdown_json_fence(text: str) -> str:
    stripped = text.strip()
    match = re.match(r"^```(?:json)?\s*\n(.*)\n```$", stripped, flags=re.DOTALL)
    if match:
        return match.group(1).strip()
    return stripped
