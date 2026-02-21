import copy
import json
import unittest
from pathlib import Path

from config.validator import validate_config


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "pipeline_config.json"
SCHEMA_PATH = ROOT / "config" / "pipeline_config.schema.json"


class PipelineConfigValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        self.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    def test_valid_pipeline_config_passes(self) -> None:
        result = validate_config(self.config, self.schema)
        self.assertTrue(result.valid)
        self.assertEqual([], result.errors)

    def test_missing_required_key_fails(self) -> None:
        broken = copy.deepcopy(self.config)
        del broken["branching"]["base_branch"]

        result = validate_config(broken, self.schema)
        self.assertFalse(result.valid)
        messages = {(err.path, err.message) for err in result.errors}
        self.assertIn(("branching.base_branch", "missing required key"), messages)

    def test_invalid_enum_fails(self) -> None:
        broken = copy.deepcopy(self.config)
        broken["global_policy"]["default_mode"] = "turbo"

        result = validate_config(broken, self.schema)
        self.assertFalse(result.valid)
        self.assertTrue(
            any(err.path == "global_policy.default_mode" for err in result.errors)
        )

    def test_additional_property_fails(self) -> None:
        broken = copy.deepcopy(self.config)
        broken["branching"]["forbidden"] = "x"

        result = validate_config(broken, self.schema)
        self.assertFalse(result.valid)
        self.assertTrue(
            any(err.path == "branching.forbidden" for err in result.errors)
        )


if __name__ == "__main__":
    unittest.main()
