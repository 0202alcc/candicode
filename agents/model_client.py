from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Dict, Protocol


class HostedModelClient(Protocol):
    def generate_structured(self, agent_name: str, prompt: str, context: Dict) -> Dict:
        """Return a JSON-like dict for the agent output."""


@dataclass
class StaticHostedModelClient:
    """Deterministic local stand-in for a hosted model API."""

    responses: Dict[str, Dict]

    def generate_structured(self, agent_name: str, prompt: str, context: Dict) -> Dict:
        payload = self.responses.get(agent_name)
        if payload is None:
            raise ValueError(f"no static response configured for agent '{agent_name}'")
        # Defensive copy to prevent caller mutation.
        return json.loads(json.dumps(payload))
