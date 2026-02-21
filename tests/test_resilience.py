import unittest

from orchestrator.resilience import (
    ResilienceConfig,
    ResilienceManager,
    TaskBudget,
)


class ResilienceManagerTests(unittest.TestCase):
    def test_budget_exceeded_flags_expected_dimensions(self) -> None:
        manager = ResilienceManager(
            budget=TaskBudget(
                max_wall_time_seconds=1.0,
                max_phase_attempts=2,
                max_tool_calls=1,
            ),
            config=ResilienceConfig(
                max_retries_per_phase=1,
                fallback_phases=set(),
                safe_mode_failure_threshold=3,
            ),
        )
        manager.record_phase_attempt("t1", "triage")
        manager.record_phase_attempt("t1", "requirements")
        manager.record_phase_attempt("t1", "plan")

        failures = manager.budget_exceeded(
            task_id="t1",
            elapsed_seconds=2.0,
            tool_calls=2,
        )
        self.assertEqual(
            {"max_wall_time_seconds", "max_phase_attempts", "max_tool_calls"},
            set(failures),
        )

    def test_retry_allowed_respects_config_limit(self) -> None:
        manager = ResilienceManager(
            budget=TaskBudget(
                max_wall_time_seconds=60.0,
                max_phase_attempts=100,
                max_tool_calls=100,
            ),
            config=ResilienceConfig(
                max_retries_per_phase=1,
                fallback_phases=set(),
                safe_mode_failure_threshold=5,
            ),
        )
        manager.record_phase_attempt("t1", "triage")
        self.assertTrue(manager.retry_allowed("t1", "triage"))
        manager.record_phase_attempt("t1", "triage")
        self.assertFalse(manager.retry_allowed("t1", "triage"))


if __name__ == "__main__":
    unittest.main()
