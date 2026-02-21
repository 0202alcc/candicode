import unittest

from orchestrator.approvals import (
    CHECKPOINT_MERGE,
    CHECKPOINT_REQUIREMENTS,
    ApprovalManager,
    ApprovalRecord,
)


class ApprovalManagerTests(unittest.TestCase):
    def test_submit_rejects_invalid_merge_payload(self) -> None:
        manager = ApprovalManager(required_checkpoints={CHECKPOINT_MERGE})
        record = ApprovalRecord(
            checkpoint=CHECKPOINT_MERGE,
            approved_by="reviewer",
            approved_at=1.0,
            rationale="looks good",
            criteria_ack=["all gates pass"],
            risk_ack=True,
            metadata={},
        )
        result = manager.submit(record)
        self.assertFalse(result.ok)
        self.assertIn("merge_strategy", result.detail)

    def test_submit_accepts_structured_requirements_and_merge(self) -> None:
        manager = ApprovalManager(
            required_checkpoints={CHECKPOINT_REQUIREMENTS, CHECKPOINT_MERGE}
        )
        req = ApprovalRecord(
            checkpoint=CHECKPOINT_REQUIREMENTS,
            approved_by="pm",
            approved_at=1.0,
            rationale="requirements are clear",
            criteria_ack=["acceptance criteria reviewed"],
            risk_ack=True,
            metadata={"acceptance_criteria_confirmed": True},
        )
        merge = ApprovalRecord(
            checkpoint=CHECKPOINT_MERGE,
            approved_by="lead",
            approved_at=2.0,
            rationale="safe to merge",
            criteria_ack=["ci green", "review complete"],
            risk_ack=True,
            metadata={"merge_strategy": "squash"},
        )
        self.assertTrue(manager.submit(req).ok)
        self.assertTrue(manager.submit(merge).ok)
        self.assertEqual([], manager.missing_required())
        self.assertTrue(manager.ensure_required().ok)


if __name__ == "__main__":
    unittest.main()
