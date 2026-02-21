from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List


@dataclass(frozen=True)
class ValidationErrorItem:
    path: str
    message: str


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    errors: List[ValidationErrorItem]


def _path_join(base: str, key: str) -> str:
    if not base:
        return key
    return f"{base}.{key}"


def _validate_type(value: Any, expected_type: str) -> bool:
    mapping = {
        "object": dict,
        "array": list,
        "string": str,
        "boolean": bool,
        "number": (int, float),
        "integer": int,
    }
    expected = mapping.get(expected_type)
    if expected is None:
        return True
    # bool is also int in Python; keep integer strict.
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, expected)


def _resolve_ref(schema: Dict[str, Any], ref: str) -> Dict[str, Any]:
    if not ref.startswith("#/$defs/"):
        return {}
    key = ref.split("/")[-1]
    defs = schema.get("$defs", {})
    return defs.get(key, {})


def _validate_node(
    data: Any, node_schema: Dict[str, Any], root_schema: Dict[str, Any], path: str
) -> List[ValidationErrorItem]:
    errors: List[ValidationErrorItem] = []

    if "$ref" in node_schema:
        resolved = _resolve_ref(root_schema, node_schema["$ref"])
        if not resolved:
            errors.append(
                ValidationErrorItem(path or "$", f"unresolved schema ref {node_schema['$ref']}")
            )
            return errors
        return _validate_node(data, resolved, root_schema, path)

    expected_type = node_schema.get("type")
    if expected_type and not _validate_type(data, expected_type):
        actual = type(data).__name__
        errors.append(
            ValidationErrorItem(path or "$", f"expected {expected_type}, got {actual}")
        )
        return errors

    enum_values = node_schema.get("enum")
    if enum_values is not None and data not in enum_values:
        errors.append(
            ValidationErrorItem(
                path or "$", f"expected one of {enum_values}, got {data!r}"
            )
        )

    if expected_type == "object":
        assert isinstance(data, dict)
        required_keys = node_schema.get("required", [])
        for key in required_keys:
            if key not in data:
                errors.append(
                    ValidationErrorItem(_path_join(path, key), "missing required key")
                )

        props = node_schema.get("properties", {})
        allow_extra = node_schema.get("additionalProperties", True)

        for key, value in data.items():
            if key not in props:
                if not allow_extra:
                    errors.append(
                        ValidationErrorItem(
                            _path_join(path, key), "additional property is not allowed"
                        )
                    )
                continue
            errors.extend(
                _validate_node(
                    value, props[key], root_schema, _path_join(path, key)
                )
            )

    if expected_type == "array":
        assert isinstance(data, list)
        min_items = node_schema.get("minItems")
        if min_items is not None and len(data) < min_items:
            errors.append(
                ValidationErrorItem(
                    path or "$", f"expected at least {min_items} items, got {len(data)}"
                )
            )
        item_schema = node_schema.get("items")
        if item_schema:
            for idx, value in enumerate(data):
                errors.extend(
                    _validate_node(value, item_schema, root_schema, f"{path}[{idx}]")
                )

    return errors


def validate_config(data: Dict[str, Any], schema: Dict[str, Any]) -> ValidationResult:
    errors = _validate_node(data, schema, schema, "")
    return ValidationResult(valid=not errors, errors=errors)
