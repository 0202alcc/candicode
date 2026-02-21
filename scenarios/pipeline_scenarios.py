from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

from agents.basic_agents import BasicAgentSuite
from agents.backend_adapter_v1 import (
    HostedModelClientAdapterV1,
    ValidatedBackendClientV1,
)
from agents.model_client import StaticHostedModelClient
from contracts.registry import ContractRegistry
from orchestrator.approvals import (
    CHECKPOINT_MERGE,
    CHECKPOINT_WAIVER,
    ApprovalManager,
)
from orchestrator.audit import AuditLogger, ProvenanceBundler
from orchestrator.supervisor import StateStore, Supervisor, TaskQueue
from policy.engine import PolicyContext, evaluate_auto_rollback


@dataclass(frozen=True)
class ScenarioResult:
    name: str
    expected: str
    actual: str
    passed: bool
    detail: str


def _schemas_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "contracts" / "schemas"


def _backend_request_schema() -> Path:
    return _schemas_dir() / "backend_request_v1.json"


def _backend_response_schema() -> Path:
    return _schemas_dir() / "backend_response_v1.json"


def _basic_responses() -> Dict[str, Dict]:
    return {
        "triage_agent": {
            "task_type": "bug",
            "risk_level": "low",
            "scope_size": "small",
            "intensity": "normal",
        },
        "requirements_agent": {
            "acceptance_criteria": ["ac1"],
            "non_goals": [],
            "open_questions": [],
        },
        "planner_agent": {
            "steps": ["step1"],
            "test_plan": ["unit"],
            "risk_notes": [],
        },
        "coder_agent": {"changes": ["chg"], "files_touched": ["file.py"]},
        "test_agent": {
            "tests_added": ["tests/test_file.py"],
            "checks_to_run": ["unit"],
        },
        "reviewer_agent": {
            "findings": [],
            "risk_summary": "low risk",
            "required_fixes": [],
        },
        "docs_agent": {
            "changelog": "updated",
            "runbook_delta": "none",
            "migration_notes": "none",
        },
    }


def scenario_bugfix_happy_path() -> ScenarioResult:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        hosted = StaticHostedModelClient(responses=_basic_responses())
        v1_adapter = HostedModelClientAdapterV1(structured_client=hosted)
        validated = ValidatedBackendClientV1.from_schema_paths(
            adapter=v1_adapter,
            request_schema_path=_backend_request_schema(),
            response_schema_path=_backend_response_schema(),
        )
        supervisor = Supervisor(
            queue=TaskQueue(),
            state_store=StateStore(root / "state.json"),
            contract_registry=ContractRegistry.from_directory(_schemas_dir()),
            phase_handlers=BasicAgentSuite.from_validated_backend_v1(
                validated_client=validated,
                default_model="hosted-model-v1",
            ).build_phase_handlers(),
            audit_logger=AuditLogger(root / "audit.jsonl"),
            provenance_bundler=ProvenanceBundler(root / "bundles"),
        )
        task = supervisor.run_prompt("fix null pointer in parser")
        ok = task.status == "completed"
        return ScenarioResult(
            name="bugfix_happy_path",
            expected="completed",
            actual=task.status,
            passed=ok,
            detail="completed with audit/provenance" if ok else "unexpected status",
        )


def scenario_high_risk_refactor_requires_merge_approval() -> ScenarioResult:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        approvals = ApprovalManager(required_checkpoints={CHECKPOINT_MERGE})
        supervisor = Supervisor(
            queue=TaskQueue(),
            state_store=StateStore(root / "state.json"),
            approval_manager=approvals,
        )
        task = supervisor.run_prompt("refactor authentication flow deeply")
        ok = task.status == "blocked" and task.phase_history[-1].phase == "human_checkpoints"
        return ScenarioResult(
            name="high_risk_refactor_requires_merge_approval",
            expected="blocked@human_checkpoints",
            actual=f"{task.status}@{task.phase_history[-1].phase}",
            passed=ok,
            detail=task.phase_history[-1].detail,
        )


def scenario_migration_requires_waiver() -> ScenarioResult:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        approvals = ApprovalManager(required_checkpoints={CHECKPOINT_WAIVER})
        supervisor = Supervisor(
            queue=TaskQueue(),
            state_store=StateStore(root / "state.json"),
            approval_manager=approvals,
        )
        task = supervisor.run_prompt("drop legacy column and migrate data")
        ok = task.status == "blocked" and task.phase_history[-1].phase == "human_checkpoints"
        return ScenarioResult(
            name="migration_requires_waiver",
            expected="blocked@human_checkpoints",
            actual=f"{task.status}@{task.phase_history[-1].phase}",
            passed=ok,
            detail=task.phase_history[-1].detail,
        )


def scenario_security_triggered_block() -> ScenarioResult:
    def failing_security(_: object) -> PolicyContext:
        return PolicyContext(
            requirements_clear=True,
            architecture_required=False,
            architecture_approved=True,
            ci_full_passed=True,
            security_passed=False,
            qa_passed=True,
            perf_passed=True,
            review_passed=True,
            docs_passed=True,
            human_merge_approval=True,
            branch_up_to_date=True,
            post_deploy_smoke_passed=True,
            slo_healthy=True,
            alerts_healthy=True,
        )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        supervisor = Supervisor(
            queue=TaskQueue(),
            state_store=StateStore(root / "state.json"),
            policy_context_resolver=failing_security,
        )
        task = supervisor.run_prompt("patch auth")
        ok = task.status == "blocked" and task.phase_history[-1].phase == "gate_pre_merge"
        return ScenarioResult(
            name="security_triggered_block",
            expected="blocked@gate_pre_merge",
            actual=f"{task.status}@{task.phase_history[-1].phase}",
            passed=ok,
            detail=task.phase_history[-1].detail,
        )


def scenario_failed_rollout_triggers_rollback() -> ScenarioResult:
    ctx = PolicyContext(
        requirements_clear=True,
        architecture_required=False,
        architecture_approved=True,
        ci_full_passed=True,
        security_passed=True,
        qa_passed=True,
        perf_passed=True,
        review_passed=True,
        docs_passed=True,
        human_merge_approval=True,
        branch_up_to_date=True,
        post_deploy_smoke_passed=False,
        slo_healthy=True,
        alerts_healthy=True,
    )
    rollback = evaluate_auto_rollback(ctx)
    ok = rollback.get("rollback_required") is True
    return ScenarioResult(
        name="failed_rollout_triggers_rollback",
        expected="rollback_required=True",
        actual=f"rollback_required={rollback.get('rollback_required')}",
        passed=ok,
        detail="post-deploy smoke failure triggers rollback",
    )


def run_all_scenarios() -> List[ScenarioResult]:
    return [
        scenario_bugfix_happy_path(),
        scenario_high_risk_refactor_requires_merge_approval(),
        scenario_migration_requires_waiver(),
        scenario_security_triggered_block(),
        scenario_failed_rollout_triggers_rollback(),
    ]
