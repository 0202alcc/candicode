import unittest

from scenarios.pipeline_scenarios import run_all_scenarios


class PipelineScenarioTests(unittest.TestCase):
    def test_all_pipeline_scenarios_pass(self) -> None:
        results = run_all_scenarios()
        failed = [r for r in results if not r.passed]
        self.assertEqual(
            [],
            failed,
            msg="; ".join(
                [f"{r.name} expected={r.expected} actual={r.actual}" for r in failed]
            ),
        )

    def test_expected_scenario_names_present(self) -> None:
        results = run_all_scenarios()
        names = {r.name for r in results}
        self.assertEqual(
            {
                "bugfix_happy_path",
                "high_risk_refactor_requires_merge_approval",
                "migration_requires_waiver",
                "security_triggered_block",
                "failed_rollout_triggers_rollback",
            },
            names,
        )


if __name__ == "__main__":
    unittest.main()
