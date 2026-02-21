#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.validator import validate_config
from agents.backend_adapter_v1 import build_agent_request_v1


def main() -> int:
    req_schema = json.loads(
        (ROOT / "contracts" / "schemas" / "backend_request_v1.json").read_text(
            encoding="utf-8"
        )
    )
    res_schema = json.loads(
        (ROOT / "contracts" / "schemas" / "backend_response_v1.json").read_text(
            encoding="utf-8"
        )
    )

    request = build_agent_request_v1(
        request_id="contract-check",
        agent_name="triage_agent",
        model="example-model",
        prompt="check contract",
    )
    request_valid = validate_config(request, req_schema)
    if not request_valid.valid:
        print("FAILED: request schema example invalid")
        for err in request_valid.errors:
            print(f"- {err.path}: {err.message}")
        return 1

    response = {
        "version": "v1",
        "request_id": "contract-check",
        "model": "example-model",
        "output_text": "ok",
        "finish_reason": "stop",
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        "trace_events": [],
    }
    response_valid = validate_config(response, res_schema)
    if not response_valid.valid:
        print("FAILED: response schema example invalid")
        for err in response_valid.errors:
            print(f"- {err.path}: {err.message}")
        return 1

    print("OK: backend adapter v1 contracts are valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
