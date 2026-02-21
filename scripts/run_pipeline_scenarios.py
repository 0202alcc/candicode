#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scenarios.pipeline_scenarios import run_all_scenarios


def main() -> int:
    results = run_all_scenarios()
    failed = [r for r in results if not r.passed]

    print("Pipeline Scenario Results")
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        print(f"- [{status}] {r.name}: expected={r.expected} actual={r.actual}")

    summary = {
        "total": len(results),
        "passed": len(results) - len(failed),
        "failed": len(failed),
        "results": [r.__dict__ for r in results],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
