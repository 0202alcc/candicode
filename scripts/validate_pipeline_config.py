#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.validator import validate_config


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate pipeline config against schema."
    )
    parser.add_argument(
        "--config",
        default="config/pipeline_config.json",
        help="Path to pipeline config JSON file.",
    )
    parser.add_argument(
        "--schema",
        default="config/pipeline_config.schema.json",
        help="Path to pipeline schema JSON file.",
    )
    args = parser.parse_args()

    config_path = Path(args.config)
    schema_path = Path(args.schema)

    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover
        print(f"FAILED: unable to read config {config_path}: {exc}")
        return 1

    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover
        print(f"FAILED: unable to read schema {schema_path}: {exc}")
        return 1

    result = validate_config(config, schema)
    if result.valid:
        print(f"OK: {config_path} is valid")
        return 0

    print(f"FAILED: {config_path} is invalid")
    for err in result.errors:
        print(f"- {err.path}: {err.message}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
