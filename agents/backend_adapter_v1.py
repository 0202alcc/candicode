from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Protocol

from agents.model_client import HostedModelClient

from config.validator import validate_config


class BackendAdapterV1(Protocol):
    def chat(self, request_payload: Dict) -> Dict:
        """Execute a v1 backend chat request and return a v1 response payload."""


@dataclass
class ValidatedBackendClientV1:
    adapter: BackendAdapterV1
    request_schema: Dict
    response_schema: Dict

    @classmethod
    def from_schema_paths(
        cls,
        adapter: BackendAdapterV1,
        request_schema_path: str | Path,
        response_schema_path: str | Path,
    ) -> "ValidatedBackendClientV1":
        req = json.loads(Path(request_schema_path).read_text(encoding="utf-8"))
        res = json.loads(Path(response_schema_path).read_text(encoding="utf-8"))
        return cls(adapter=adapter, request_schema=req, response_schema=res)

    def chat(self, request_payload: Dict) -> Dict:
        request_validation = validate_config(request_payload, self.request_schema)
        if not request_validation.valid:
            details = "; ".join(
                f"{err.path}: {err.message}" for err in request_validation.errors
            )
            raise ValueError(f"backend request contract v1 invalid: {details}")

        response_payload = self.adapter.chat(request_payload)
        response_validation = validate_config(response_payload, self.response_schema)
        if not response_validation.valid:
            details = "; ".join(
                f"{err.path}: {err.message}" for err in response_validation.errors
            )
            raise ValueError(f"backend response contract v1 invalid: {details}")

        return response_payload


@dataclass
class HostedModelClientAdapterV1:
    """
    Adapter that maps an existing structured HostedModelClient into the frozen v1 backend contract.
    This lets current hosted model wiring pass through v1 validation today.
    """

    structured_client: HostedModelClient

    def chat(self, request_payload: Dict) -> Dict:
        agent_name = request_payload["agent_name"]
        model = request_payload["model"]
        request_id = request_payload["request_id"]

        prompt_parts = [
            msg["content"]
            for msg in request_payload.get("messages", [])
            if msg.get("role") == "user"
        ]
        prompt = "\n".join(prompt_parts).strip()

        payload = self.structured_client.generate_structured(
            agent_name=agent_name,
            prompt=prompt,
            context={
                "metadata": request_payload.get("metadata", {}),
                "trace_mode": request_payload.get("trace_mode", "summary"),
            },
        )
        output_text = json.dumps(payload, sort_keys=True)

        return {
            "version": "v1",
            "request_id": request_id,
            "model": model,
            "output_text": output_text,
            "finish_reason": "stop",
            "usage": {
                "prompt_tokens": max(1, len(prompt.split())),
                "completion_tokens": max(1, len(output_text.split())),
                "total_tokens": max(2, len(prompt.split()) + len(output_text.split())),
            },
            "trace_events": [
                {
                    "event_type": "reasoning_summary",
                    "timestamp": 0.0,
                    "phase": agent_name,
                    "detail": f"{agent_name} completed via hosted adapter",
                    "level": "info",
                }
            ],
        }


@dataclass
class BackendStructuredModelClient:
    """
    HostedModelClient implementation backed by the validated v1 backend client.
    """

    validated_client: ValidatedBackendClientV1
    default_model: str
    trace_mode: str = "summary"
    max_tokens: int = 512
    temperature: float = 0.2

    def generate_structured(self, agent_name: str, prompt: str, context: Dict) -> Dict:
        request_id = str(context.get("request_id") or f"{agent_name}-req")
        model = str(context.get("model") or self.default_model)
        trace_mode = str(context.get("trace_mode") or self.trace_mode)
        max_tokens = int(context.get("max_tokens") or self.max_tokens)
        temperature = float(context.get("temperature") or self.temperature)

        request = build_agent_request_v1(
            request_id=request_id,
            agent_name=agent_name,
            model=model,
            prompt=prompt,
            trace_mode=trace_mode,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        metadata = context.get("metadata")
        if isinstance(metadata, dict):
            request["metadata"] = metadata

        response = self.validated_client.chat(request)
        output_text = response.get("output_text", "")
        try:
            payload = json.loads(output_text)
        except Exception as exc:
            raise ValueError(f"backend response output_text is not valid JSON: {exc}") from exc

        if not isinstance(payload, dict):
            raise ValueError("backend response output_text JSON must decode to an object")
        return payload


def build_agent_request_v1(
    *,
    request_id: str,
    agent_name: str,
    model: str,
    prompt: str,
    trace_mode: str = "summary",
    max_tokens: int = 512,
    temperature: float = 0.2,
) -> Dict:
    return {
        "version": "v1",
        "request_id": request_id,
        "agent_name": agent_name,
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
        "trace_mode": trace_mode,
        "metadata": {},
    }
