import unittest

from orchestrator.verification import FlakyPolicy, FlakyRegistry, VerificationRunner


class VerificationRunnerTests(unittest.TestCase):
    def test_checks_run_in_deterministic_order(self) -> None:
        seen = []

        def executor(name: str) -> bool:
            seen.append(name)
            return True

        runner = VerificationRunner(
            checks=["unit", "lint", "integration"],
            check_executor=executor,
            flaky_policy=FlakyPolicy(
                max_retries=0,
                quarantine_after_failures=2,
                require_owner=True,
                require_expiry=True,
            ),
        )
        result = runner.run()
        self.assertEqual("pass", result.status)
        self.assertEqual(["integration", "lint", "unit"], seen)
        self.assertEqual(["integration", "lint", "unit"], result.checks)

    def test_failure_without_quarantine_metadata_fails(self) -> None:
        def executor(_: str) -> bool:
            return False

        registry = FlakyRegistry()
        runner = VerificationRunner(
            checks=["unit"],
            check_executor=executor,
            flaky_policy=FlakyPolicy(
                max_retries=1,
                quarantine_after_failures=1,
                require_owner=True,
                require_expiry=True,
            ),
            flaky_registry=registry,
        )
        result = runner.run()
        self.assertEqual("fail", result.status)
        self.assertEqual(["unit"], result.failures)
        self.assertEqual([], result.flaky_quarantined)

    def test_flaky_check_is_quarantined_when_metadata_present(self) -> None:
        def executor(_: str) -> bool:
            return False

        registry = FlakyRegistry()
        registry.set_metadata("unit", owner="qa-owner", expiry="2026-03-01")
        runner = VerificationRunner(
            checks=["unit"],
            check_executor=executor,
            flaky_policy=FlakyPolicy(
                max_retries=0,
                quarantine_after_failures=1,
                require_owner=True,
                require_expiry=True,
            ),
            flaky_registry=registry,
        )
        result = runner.run()
        self.assertEqual("pass", result.status)
        self.assertEqual([], result.failures)
        self.assertEqual(["unit"], result.flaky_quarantined)


if __name__ == "__main__":
    unittest.main()
