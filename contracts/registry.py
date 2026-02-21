from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

from config.validator import validate_config


@dataclass(frozen=True)
class HandoffValidation:
    valid: bool
    errors: List[str]


class ContractRegistry:
    def __init__(self, schemas: Dict[str, Dict]) -> None:
        self.schemas = schemas

    @classmethod
    def from_directory(cls, schema_dir: str | Path) -> "ContractRegistry":
        schema_path = Path(schema_dir)
        schemas: Dict[str, Dict] = {}
        for file_path in sorted(schema_path.glob("*.json")):
            phase = file_path.stem
            schemas[phase] = json.loads(file_path.read_text(encoding="utf-8"))
        return cls(schemas=schemas)

    def validate(self, phase: str, payload: Dict) -> HandoffValidation:
        schema = self.schemas.get(phase)
        if schema is None:
            return HandoffValidation(
                valid=False,
                errors=[f"missing handoff schema for phase '{phase}'"],
            )

        result = validate_config(payload, schema)
        if result.valid:
            return HandoffValidation(valid=True, errors=[])

        errors = [f"{err.path}: {err.message}" for err in result.errors]
        return HandoffValidation(valid=False, errors=errors)
