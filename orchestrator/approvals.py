from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Set


CHECKPOINT_REQUIREMENTS = "requirements"
CHECKPOINT_WAIVER = "waiver"
CHECKPOINT_MERGE = "merge"
CHECKPOINT_ROLLOUT = "rollout"


@dataclass(frozen=True)
class ApprovalRecord:
    checkpoint: str
    approved_by: str
    approved_at: float
    rationale: str
    criteria_ack: List[str]
    risk_ack: bool
    metadata: Dict[str, object]


@dataclass(frozen=True)
class ApprovalResult:
    ok: bool
    detail: str


class ApprovalManager:
    def __init__(self, required_checkpoints: Optional[Set[str]] = None) -> None:
        self.required_checkpoints: Set[str] = set(required_checkpoints or set())
        self.records: Dict[str, ApprovalRecord] = {}

    def submit(self, record: ApprovalRecord) -> ApprovalResult:
        validation = self._validate_record(record)
        if not validation.ok:
            return validation
        self.records[record.checkpoint] = record
        return ApprovalResult(ok=True, detail=f"approval recorded: {record.checkpoint}")

    def missing_required(self) -> List[str]:
        return sorted([c for c in self.required_checkpoints if c not in self.records])

    def is_approved(self, checkpoint: str) -> bool:
        return checkpoint in self.records

    def ensure_required(self) -> ApprovalResult:
        missing = self.missing_required()
        if missing:
            return ApprovalResult(
                ok=False,
                detail=f"missing required approvals: {', '.join(missing)}",
            )
        return ApprovalResult(ok=True, detail="all required approvals present")

    def _validate_record(self, record: ApprovalRecord) -> ApprovalResult:
        if not record.approved_by.strip():
            return ApprovalResult(ok=False, detail="approved_by is required")
        if not record.rationale.strip():
            return ApprovalResult(ok=False, detail="rationale is required")
        if not record.criteria_ack:
            return ApprovalResult(ok=False, detail="criteria_ack must be non-empty")
        if not record.risk_ack:
            return ApprovalResult(ok=False, detail="risk_ack must be true")

        if record.checkpoint == CHECKPOINT_REQUIREMENTS:
            if "acceptance_criteria_confirmed" not in record.metadata:
                return ApprovalResult(
                    ok=False,
                    detail="requirements approval missing acceptance_criteria_confirmed",
                )
        elif record.checkpoint == CHECKPOINT_WAIVER:
            for key in ("policy_id", "owner", "expiry"):
                if key not in record.metadata:
                    return ApprovalResult(
                        ok=False,
                        detail=f"waiver approval missing {key}",
                    )
        elif record.checkpoint == CHECKPOINT_MERGE:
            if "merge_strategy" not in record.metadata:
                return ApprovalResult(
                    ok=False,
                    detail="merge approval missing merge_strategy",
                )
        elif record.checkpoint == CHECKPOINT_ROLLOUT:
            for key in ("stage", "rollback_plan_ack"):
                if key not in record.metadata:
                    return ApprovalResult(
                        ok=False,
                        detail=f"rollout approval missing {key}",
                    )
        else:
            return ApprovalResult(ok=False, detail=f"unknown checkpoint: {record.checkpoint}")

        return ApprovalResult(ok=True, detail="approval payload valid")


def make_approval(
    checkpoint: str,
    approved_by: str,
    rationale: str,
    criteria_ack: List[str],
    metadata: Dict[str, object],
) -> ApprovalRecord:
    return ApprovalRecord(
        checkpoint=checkpoint,
        approved_by=approved_by,
        approved_at=time.time(),
        rationale=rationale,
        criteria_ack=criteria_ack,
        risk_ack=True,
        metadata=metadata,
    )
