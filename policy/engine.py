from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List


@dataclass(frozen=True)
class PolicyContext:
    requirements_clear: bool
    architecture_required: bool
    architecture_approved: bool
    ci_full_passed: bool
    security_passed: bool
    qa_passed: bool
    perf_passed: bool
    review_passed: bool
    docs_passed: bool
    human_merge_approval: bool
    branch_up_to_date: bool
    post_deploy_smoke_passed: bool
    slo_healthy: bool
    alerts_healthy: bool


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    failures: List[str]


def evaluate_pre_merge(ctx: PolicyContext) -> GateResult:
    failures: List[str] = []

    if not ctx.requirements_clear:
        failures.append("requirements_clear")
    if ctx.architecture_required and not ctx.architecture_approved:
        failures.append("architecture_approval")
    if not ctx.ci_full_passed:
        failures.append("ci_full")
    if not ctx.security_passed:
        failures.append("security")
    if not ctx.qa_passed:
        failures.append("qa_matrix")
    if not ctx.perf_passed:
        failures.append("perf_budget")
    if not ctx.review_passed:
        failures.append("review")
    if not ctx.docs_passed:
        failures.append("docs")
    if not ctx.human_merge_approval:
        failures.append("human_merge_approval")
    if not ctx.branch_up_to_date:
        failures.append("branch_up_to_date")

    return GateResult(allowed=not failures, failures=failures)


def evaluate_pre_rollout(ctx: PolicyContext) -> GateResult:
    failures: List[str] = []

    if not ctx.post_deploy_smoke_passed:
        failures.append("post_deploy_smoke")
    if not ctx.slo_healthy:
        failures.append("slo_health")
    if not ctx.alerts_healthy:
        failures.append("alert_health")

    return GateResult(allowed=not failures, failures=failures)


def evaluate_auto_rollback(ctx: PolicyContext) -> Dict[str, bool]:
    return {
        "rollback_required": (
            not ctx.post_deploy_smoke_passed
            or not ctx.slo_healthy
            or not ctx.alerts_healthy
        )
    }
