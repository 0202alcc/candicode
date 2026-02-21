import json
import unittest
from pathlib import Path

from policy.engine import PolicyContext, evaluate_auto_rollback, evaluate_pre_merge, evaluate_pre_rollout


ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "policy" / "policy_spec.json"


def make_context(**overrides: bool) -> PolicyContext:
    baseline = {
        "requirements_clear": True,
        "architecture_required": False,
        "architecture_approved": True,
        "ci_full_passed": True,
        "security_passed": True,
        "qa_passed": True,
        "perf_passed": True,
        "review_passed": True,
        "docs_passed": True,
        "human_merge_approval": True,
        "branch_up_to_date": True,
        "post_deploy_smoke_passed": True,
        "slo_healthy": True,
        "alerts_healthy": True,
    }
    baseline.update(overrides)
    return PolicyContext(**baseline)


class PolicySpecTests(unittest.TestCase):
    def test_policy_spec_is_loadable(self) -> None:
        spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        self.assertIn("gates", spec)
        self.assertIn("pre_merge_required", spec["gates"])
        self.assertIn("pre_rollout_required", spec["gates"])


class PreMergeGateTests(unittest.TestCase):
    def test_pre_merge_allows_when_everything_passes(self) -> None:
        result = evaluate_pre_merge(make_context())
        self.assertTrue(result.allowed)
        self.assertEqual([], result.failures)

    def test_pre_merge_blocks_without_merge_approval(self) -> None:
        result = evaluate_pre_merge(make_context(human_merge_approval=False))
        self.assertFalse(result.allowed)
        self.assertIn("human_merge_approval", result.failures)

    def test_pre_merge_blocks_when_architecture_required_not_approved(self) -> None:
        result = evaluate_pre_merge(
            make_context(architecture_required=True, architecture_approved=False)
        )
        self.assertFalse(result.allowed)
        self.assertIn("architecture_approval", result.failures)

    def test_pre_merge_collects_multiple_failures(self) -> None:
        result = evaluate_pre_merge(
            make_context(
                ci_full_passed=False,
                security_passed=False,
                review_passed=False,
                branch_up_to_date=False,
            )
        )
        self.assertFalse(result.allowed)
        self.assertEqual(
            {"ci_full", "security", "review", "branch_up_to_date"},
            set(result.failures),
        )


class PreRolloutGateTests(unittest.TestCase):
    def test_pre_rollout_allows_when_health_checks_pass(self) -> None:
        result = evaluate_pre_rollout(make_context())
        self.assertTrue(result.allowed)
        self.assertEqual([], result.failures)

    def test_pre_rollout_blocks_when_slo_unhealthy(self) -> None:
        result = evaluate_pre_rollout(make_context(slo_healthy=False))
        self.assertFalse(result.allowed)
        self.assertIn("slo_health", result.failures)

    def test_auto_rollback_triggers_on_post_deploy_failure(self) -> None:
        result = evaluate_auto_rollback(make_context(post_deploy_smoke_passed=False))
        self.assertTrue(result["rollback_required"])

    def test_auto_rollback_not_required_when_everything_is_healthy(self) -> None:
        result = evaluate_auto_rollback(make_context())
        self.assertFalse(result["rollback_required"])


if __name__ == "__main__":
    unittest.main()
