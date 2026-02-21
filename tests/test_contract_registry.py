import unittest
from pathlib import Path

from contracts.registry import ContractRegistry


ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "contracts" / "schemas"


class ContractRegistryTests(unittest.TestCase):
    def test_validate_known_phase_payload_success(self) -> None:
        registry = ContractRegistry.from_directory(SCHEMAS)
        payload = {
            "task_type": "bug",
            "risk_level": "low",
            "scope_size": "small",
            "intensity": "normal",
        }
        result = registry.validate("triage", payload)
        self.assertTrue(result.valid)
        self.assertEqual([], result.errors)

    def test_validate_known_phase_payload_failure(self) -> None:
        registry = ContractRegistry.from_directory(SCHEMAS)
        payload = {
            "task_type": "bug",
            "risk_level": "low",
            "scope_size": "small",
        }
        result = registry.validate("triage", payload)
        self.assertFalse(result.valid)
        self.assertTrue(any("missing required key" in err for err in result.errors))

    def test_missing_phase_schema_fails_closed(self) -> None:
        registry = ContractRegistry.from_directory(SCHEMAS)
        result = registry.validate("nonexistent_phase", {})
        self.assertFalse(result.valid)
        self.assertIn("missing handoff schema", result.errors[0])


if __name__ == "__main__":
    unittest.main()
