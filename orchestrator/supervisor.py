from __future__ import annotations

import hashlib
import heapq
import json
import re
import time
import uuid
from datetime import datetime, timezone
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from contracts.registry import ContractRegistry
from orchestrator.approvals import ApprovalManager, CHECKPOINT_WAIVER
from orchestrator.audit import AuditLogger, ProvenanceBundler
from orchestrator.resilience import ResilienceManager
from policy.engine import PolicyContext, evaluate_pre_merge
from orchestrator.tool_runner import ToolRequest, ToolRunner
from orchestrator.verification import VerificationRunner
from orchestrator.versioning import VersioningAgent, VersioningResult


PRIORITY_ORDER = {
    "interactive": 0,
    "release_blocking": 1,
    "background": 2,
}


@dataclass
class PhaseResult:
    phase: str
    status: str
    detail: str
    timestamp: float


@dataclass
class Task:
    task_id: str
    prompt: str
    priority: str
    created_at: float
    work_branch: Optional[str] = None
    trusted_context: bool = True
    agent_outputs: Dict[str, Dict] = field(default_factory=dict)
    handoff_artifacts: Dict[str, Dict] = field(default_factory=dict)
    provenance_bundle_path: Optional[str] = None
    execution_mode: str = "normal"
    replay_source: Optional[str] = None
    degraded_mode: bool = False
    safe_mode: bool = False
    failure_count: int = 0
    tool_calls: int = 0
    token_estimate: int = 0
    budget_token_limit: Optional[int] = None
    budget_tool_limit: Optional[int] = None
    budget_time_limit_seconds: Optional[float] = None
    budget_envelope_id: Optional[str] = None
    state_revision: int = 0
    invalidated: bool = False
    invalidation_reason: Optional[str] = None
    status: str = "queued"
    phase_history: List[PhaseResult] = field(default_factory=list)


class TaskQueue:
    def __init__(self) -> None:
        self._heap: List[tuple] = []
        self._seq = 0

    def enqueue(self, task: Task) -> None:
        score = PRIORITY_ORDER.get(task.priority, PRIORITY_ORDER["background"])
        heapq.heappush(self._heap, (score, task.created_at, self._seq, task))
        self._seq += 1

    def dequeue(self) -> Optional[Task]:
        if not self._heap:
            return None
        return heapq.heappop(self._heap)[-1]

    def __len__(self) -> int:
        return len(self._heap)


class StateStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.snapshot_dir = self.path.parent / "snapshots"

    def load(self) -> Dict[str, Dict]:
        if not self.path.exists():
            return {"tasks": {}}
        raw = self.path.read_text(encoding="utf-8")
        if not raw.strip():
            return {"tasks": {}}
        data = json.loads(raw)
        if "tasks" not in data or not isinstance(data["tasks"], dict):
            data["tasks"] = {}
        return data

    def save(self, data: Dict[str, Dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")

    def save_task(self, task: Task) -> None:
        data = self.load()
        previous = data["tasks"].get(task.task_id)
        previous_revision = int(previous.get("state_revision", 0)) if isinstance(previous, dict) else 0
        task.state_revision = previous_revision + 1
        payload = self._serialize_task(task)
        data["tasks"][task.task_id] = payload
        self.save(data)
        self._write_snapshot(task.task_id, payload, task.state_revision)

    def load_task(self, task_id: str) -> Optional[Dict]:
        data = self.load()
        return data["tasks"].get(task_id)

    @staticmethod
    def _serialize_task(task: Task) -> Dict:
        payload = asdict(task)
        payload["phase_history"] = [asdict(item) for item in task.phase_history]
        return payload

    def list_snapshots(self, task_id: str) -> List[Path]:
        root = self.snapshot_dir / task_id
        if not root.exists():
            return []
        return sorted([path for path in root.glob("r*.json") if path.is_file()])

    def _write_snapshot(self, task_id: str, payload: Dict, revision: int) -> None:
        root = self.snapshot_dir / task_id
        root.mkdir(parents=True, exist_ok=True)
        target = root / f"r{revision:04d}.json"
        target.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


class Supervisor:
    def __init__(
        self,
        queue: TaskQueue,
        state_store: StateStore,
        phase_handlers: Optional[Dict[str, Callable[[Task], PhaseResult]]] = None,
        phase_order: Optional[List[str]] = None,
        policy_context_resolver: Optional[Callable[[Task], PolicyContext]] = None,
        versioning_agent: Optional[VersioningAgent] = None,
        base_branch: str = "main",
        tool_runner: Optional[ToolRunner] = None,
        execute_tool_name: str = "repo.search",
        contract_registry: Optional[ContractRegistry] = None,
        phase_payload_builders: Optional[Dict[str, Callable[[Task], Dict]]] = None,
        verification_runner: Optional[VerificationRunner] = None,
        approval_manager: Optional[ApprovalManager] = None,
        audit_logger: Optional[AuditLogger] = None,
        provenance_bundler: Optional[ProvenanceBundler] = None,
        resilience_manager: Optional[ResilienceManager] = None,
        phase_event_callback: Optional[Callable[[Task, Dict[str, Any]], None]] = None,
    ) -> None:
        self.queue = queue
        self.state_store = state_store
        self.phase_handlers = phase_handlers or {}
        self.policy_context_resolver = policy_context_resolver or self._default_policy_context
        self.versioning_agent = versioning_agent
        self.base_branch = base_branch
        self.tool_runner = tool_runner
        self.execute_tool_name = execute_tool_name
        self.contract_registry = contract_registry
        self.phase_payload_builders = phase_payload_builders or {}
        self.verification_runner = verification_runner
        self.approval_manager = approval_manager
        self.audit_logger = audit_logger
        self.provenance_bundler = provenance_bundler
        self.resilience_manager = resilience_manager
        self.phase_event_callback = phase_event_callback
        self.phase_order = phase_order or [
            "ingress_guard",
            "budget_envelope",
            "intent",
            "triage",
            "router_gate",
            "requirements",
            "requirements_gate",
            "versioning",
            "plan",
            "code",
            "test",
            "ci_gate",
            "security_gate",
            "qa_perf_gate",
            "review",
            "reviewer_gate",
            "docs",
            "execute",
            "verify",
            "human_checkpoints",
            "waiver_gate",
            "gate_pre_merge",
            "release_rollout",
            "deploy_runner",
            "health_gate",
            "platform_health_gate",
            "dr_gate",
            "access_gate",
            "finalize",
        ]
        self._ensure_phase_contract_coverage()

    def _ensure_phase_contract_coverage(self) -> None:
        if self.contract_registry is None:
            return
        missing = self.contract_registry.missing_schemas(self.phase_order)
        if missing:
            raise ValueError(
                "missing handoff schema(s) for configured phase_order: "
                + ", ".join(missing)
            )

    def create_task(
        self,
        prompt: str,
        priority: str = "interactive",
        execution_mode: str = "normal",
    ) -> Task:
        task_id = uuid.uuid4().hex[:12]
        task = Task(
            task_id=task_id,
            prompt=prompt,
            priority=priority,
            created_at=time.time(),
            execution_mode=execution_mode,
        )
        self.queue.enqueue(task)
        self.state_store.save_task(task)
        self._emit_audit(
            "task_created",
            task,
            {
                "priority": task.priority,
                "trusted_context": task.trusted_context,
                "execution_mode": task.execution_mode,
            },
        )
        return task

    def process_next(self) -> Optional[Task]:
        task = self.queue.dequeue()
        if task is None:
            return None

        task.status = "in_progress"
        self.state_store.save_task(task)
        self._emit_audit("task_started", task, {"phase_count": len(self.phase_order)})

        start_time = time.time()
        phase_started_at: Dict[str, float] = {}
        for phase in self.phase_order:
            phase_started_at[phase] = time.time()
            self._emit_phase_event(
                task,
                {
                    "phase": phase,
                    "status": "running",
                    "detail": f"{phase} started",
                    "timestamp": time.time(),
                },
            )
            result = self._run_phase_with_resilience(task, phase)
            task.phase_history.append(result)
            self._emit_phase_event(
                task,
                {
                    "phase": result.phase,
                    "status": result.status,
                    "detail": result.detail,
                    "timestamp": result.timestamp,
                    "duration_ms": int(max(0.0, (result.timestamp - phase_started_at.get(result.phase, result.timestamp)) * 1000)),
                },
            )
            self._emit_phase_event(
                task,
                {
                    "phase": result.phase,
                    "status": "output",
                    "detail": f"{result.phase} output",
                    "timestamp": result.timestamp,
                    "output": self._build_phase_payload(result.phase, task),
                },
            )
            self.state_store.save_task(task)
            self._emit_audit(
                "phase_result",
                task,
                {
                    "phase": result.phase,
                    "status": result.status,
                    "detail": result.detail,
                },
            )
            if result.status != "success":
                task.status = "blocked"
                self._write_provenance_bundle(task, outcome="blocked")
                self.state_store.save_task(task)
                self._emit_audit(
                    "task_blocked",
                    task,
                    {"blocked_phase": result.phase, "detail": result.detail},
                )
                return task

            budget_failures = self._check_budget(task, start_time)
            if budget_failures:
                task.status = "blocked"
                detail = f"budget exceeded: {', '.join(budget_failures)}"
                budget_result = PhaseResult(
                    phase="budget",
                    status="failed",
                    detail=detail,
                    timestamp=time.time(),
                )
                task.phase_history.append(budget_result)
                self._emit_audit("budget_block", task, {"failures": budget_failures})
                self._write_provenance_bundle(task, outcome="blocked")
                self.state_store.save_task(task)
                return task

        task.status = "completed"
        self._write_provenance_bundle(task, outcome="completed")
        self.state_store.save_task(task)
        self._emit_audit("task_completed", task, {"phase_count": len(task.phase_history)})
        return task

    def run_prompt(
        self, prompt: str, priority: str = "interactive", dry_run: bool = False
    ) -> Task:
        mode = "dry_run" if dry_run else "normal"
        self.create_task(prompt=prompt, priority=priority, execution_mode=mode)
        task = self.process_next()
        assert task is not None
        return task

    def replay_from_bundle(self, bundle_path: str | Path) -> Task:
        path = Path(bundle_path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        material = raw.get("material", {})
        replay_issues = self._replay_invalidation_reasons(raw)
        task_payload = material.get("task", {})
        phase_history = [
            PhaseResult(
                phase=item["phase"],
                status=item["status"],
                detail=item["detail"],
                timestamp=item["timestamp"],
            )
            for item in task_payload.get("phase_history", [])
        ]
        task = Task(
            task_id=task_payload.get("task_id", "replay"),
            prompt=task_payload.get("prompt", ""),
            priority=task_payload.get("priority", "interactive"),
            created_at=task_payload.get("created_at", time.time()),
            work_branch=task_payload.get("work_branch"),
            trusted_context=task_payload.get("trusted_context", True),
            agent_outputs=task_payload.get("agent_outputs", {}),
            handoff_artifacts=task_payload.get("handoff_artifacts", {}),
            provenance_bundle_path=str(path),
            execution_mode="replay",
            replay_source=str(path),
            degraded_mode=task_payload.get("degraded_mode", False),
            safe_mode=task_payload.get("safe_mode", False),
            failure_count=task_payload.get("failure_count", 0),
            tool_calls=task_payload.get("tool_calls", 0),
            token_estimate=task_payload.get("token_estimate", 0),
            budget_token_limit=task_payload.get("budget_token_limit"),
            budget_tool_limit=task_payload.get("budget_tool_limit"),
            budget_time_limit_seconds=task_payload.get("budget_time_limit_seconds"),
            budget_envelope_id=task_payload.get("budget_envelope_id"),
            state_revision=task_payload.get("state_revision", 0),
            invalidated=bool(replay_issues),
            invalidation_reason="; ".join(replay_issues) if replay_issues else None,
            status="replay_invalidated" if replay_issues else "replayed",
            phase_history=phase_history,
        )
        self.state_store.save_task(task)
        self._emit_audit(
            "task_replayed",
            task,
            {
                "source": str(path),
                "phase_count": len(task.phase_history),
                "invalidated": bool(replay_issues),
                "issues": replay_issues,
            },
        )
        return task

    def _run_phase_with_resilience(self, task: Task, phase: str) -> PhaseResult:
        while True:
            if self.resilience_manager is not None:
                attempt_no = self.resilience_manager.record_phase_attempt(task.task_id, phase)
                self._emit_audit(
                    "phase_attempt",
                    task,
                    {"phase": phase, "attempt": attempt_no},
                )

            result = self._run_phase(phase, task)
            if result.status == "success":
                contract_result = self._validate_phase_handoff(phase, task)
                if contract_result is None:
                    return result
                result = contract_result

            if self.resilience_manager is None:
                return result

            task.failure_count = self.resilience_manager.record_failure(task.task_id)
            self._emit_audit(
                "phase_failure",
                task,
                {
                    "phase": phase,
                    "detail": result.detail,
                    "failure_count": task.failure_count,
                },
            )

            if self.resilience_manager.should_enter_safe_mode(task.task_id):
                task.safe_mode = True
                return PhaseResult(
                    phase=phase,
                    status="failed",
                    detail=f"safe mode engaged after repeated failures: {result.detail}",
                    timestamp=time.time(),
                )

            if self.resilience_manager.retry_allowed(task.task_id, phase):
                continue

            if self.resilience_manager.fallback_allowed(phase, task.degraded_mode):
                task.degraded_mode = True
                self._emit_audit(
                    "phase_fallback",
                    task,
                    {"phase": phase, "reason": result.detail},
                )
                return PhaseResult(
                    phase=phase,
                    status="success",
                    detail=f"fallback path used for {phase}",
                    timestamp=time.time(),
                )

            return result

    def _run_phase(self, phase: str, task: Task) -> PhaseResult:
        if phase == "ingress_guard":
            return self._run_ingress_guard_phase(task)
        if phase == "budget_envelope":
            return self._run_budget_envelope_phase(task)
        if phase == "router_gate":
            return self._run_router_gate_phase(task)
        if phase == "requirements_gate":
            return self._run_requirements_gate_phase(task)
        if phase == "ci_gate":
            return self._run_ci_gate_phase(task)
        if phase == "security_gate":
            return self._run_security_gate_phase(task)
        if phase == "qa_perf_gate":
            return self._run_qa_perf_gate_phase(task)
        if phase == "reviewer_gate":
            return self._run_reviewer_gate_phase(task)
        if phase == "versioning":
            return self._run_versioning_phase(task)
        if phase == "execute":
            guard = self._guard_execute_branch(task)
            if not guard.ok:
                return PhaseResult(
                    phase="execute",
                    status="failed",
                    detail=guard.detail,
                    timestamp=time.time(),
                )
            return self._run_execute_phase(task)
        if phase == "verify":
            return self._run_verify_phase(task)
        if phase == "human_checkpoints":
            return self._run_human_checkpoints_phase(task)
        if phase == "waiver_gate":
            return self._run_waiver_gate_phase(task)
        if phase == "gate_pre_merge":
            return self._run_pre_merge_gate(task)
        if phase == "release_rollout":
            return self._run_release_rollout_phase(task)
        if phase == "deploy_runner":
            return self._run_deploy_runner_phase(task)
        if phase == "health_gate":
            return self._run_health_gate_phase(task)
        if phase == "platform_health_gate":
            return self._run_platform_health_gate_phase(task)
        if phase == "dr_gate":
            return self._run_dr_gate_phase(task)
        if phase == "access_gate":
            return self._run_access_gate_phase(task)

        handler = self.phase_handlers.get(phase)
        if handler:
            return handler(task)
        return PhaseResult(
            phase=phase,
            status="success",
            detail=f"stub phase {phase} completed",
            timestamp=time.time(),
        )

    def _run_ingress_guard_phase(self, task: Task) -> PhaseResult:
        original_prompt = task.prompt
        redacted_prompt, redactions = self._redact_prompt(original_prompt)
        tainted = self._is_tainted_prompt(original_prompt)
        task.prompt = redacted_prompt
        task.trusted_context = task.trusted_context and not tainted
        notes: List[str] = []
        if redactions:
            notes.append("secret redaction applied")
        if tainted:
            notes.append("untrusted context markers detected")
        task.agent_outputs["ingress_guard"] = {
            "sanitized": redacted_prompt != original_prompt,
            "tainted": tainted,
            "redactions": redactions,
            "notes": notes,
        }
        return PhaseResult(
            phase="ingress_guard",
            status="success",
            detail=(
                "ingress guard passed"
                if not notes
                else f"ingress guard passed ({'; '.join(notes)})"
            ),
            timestamp=time.time(),
        )

    def _run_budget_envelope_phase(self, task: Task) -> PhaseResult:
        profiles = {
            "interactive": {"token_budget": 12000, "tool_budget": 20, "time_budget_seconds": 300.0},
            "release_blocking": {"token_budget": 30000, "tool_budget": 40, "time_budget_seconds": 900.0},
            "background": {"token_budget": 6000, "tool_budget": 10, "time_budget_seconds": 180.0},
        }
        profile = profiles.get(task.priority, profiles["background"])
        budget_id = f"{task.task_id}:{task.priority}:v1"
        task.budget_token_limit = int(profile["token_budget"])
        task.budget_tool_limit = int(profile["tool_budget"])
        task.budget_time_limit_seconds = float(profile["time_budget_seconds"])
        task.budget_envelope_id = budget_id
        task.agent_outputs["budget_envelope"] = {
            "token_budget": task.budget_token_limit,
            "tool_budget": task.budget_tool_limit,
            "time_budget_seconds": task.budget_time_limit_seconds,
            "budget_id": budget_id,
            "policy": "priority_profile_v1",
        }
        return PhaseResult(
            phase="budget_envelope",
            status="success",
            detail=(
                "budget envelope set "
                f"(tokens={task.budget_token_limit}, tools={task.budget_tool_limit}, time={task.budget_time_limit_seconds}s)"
            ),
            timestamp=time.time(),
        )

    def _run_router_gate_phase(self, task: Task) -> PhaseResult:
        triage_payload = task.agent_outputs.get("triage", {})
        confidence = self._coerce_confidence_score(triage_payload)
        threshold = 0.70
        human_threshold = 0.35
        allowed = confidence >= threshold
        route = "normal"
        failures: List[str] = []
        if not allowed:
            if confidence >= human_threshold:
                route = "deeper_pipeline"
                task.degraded_mode = True
                failures.append("router_confidence_below_threshold")
            else:
                route = "human_triage"
                task.degraded_mode = True
                failures.append("router_confidence_requires_human_triage")

        task.agent_outputs["router_gate"] = {
            "allowed": allowed,
            "confidence": confidence,
            "threshold": threshold,
            "route": route,
            "failures": failures,
        }
        if route == "human_triage":
            return PhaseResult(
                phase="router_gate",
                status="failed",
                detail=(
                    "router confidence below human-triage threshold; "
                    f"confidence={confidence:.2f}, threshold={human_threshold:.2f}"
                ),
                timestamp=time.time(),
            )
        if route == "deeper_pipeline":
            return PhaseResult(
                phase="router_gate",
                status="success",
                detail=(
                    "router confidence below threshold; using deeper pipeline fallback "
                    f"(confidence={confidence:.2f}, threshold={threshold:.2f})"
                ),
                timestamp=time.time(),
            )
        return PhaseResult(
            phase="router_gate",
            status="success",
            detail=f"router confidence passed threshold (confidence={confidence:.2f}, threshold={threshold:.2f})",
            timestamp=time.time(),
        )

    def _run_requirements_gate_phase(self, task: Task) -> PhaseResult:
        requirements = self._build_phase_payload("requirements", task)
        if not isinstance(requirements, dict):
            requirements = {}
        missing_items: List[str] = []
        failures: List[str] = []

        acceptance: List[str] = []
        if isinstance(requirements, dict):
            raw_acceptance = requirements.get("acceptance_criteria")
            if isinstance(raw_acceptance, list):
                acceptance = [item.strip() for item in raw_acceptance if isinstance(item, str) and item.strip()]
        if not acceptance:
            missing_items.append("acceptance_criteria")
            failures.append("missing_acceptance_criteria")

        open_questions: List[str] = []
        if isinstance(requirements, dict):
            raw_questions = requirements.get("open_questions")
            if isinstance(raw_questions, list):
                open_questions = [item.strip() for item in raw_questions if isinstance(item, str) and item.strip()]
        if open_questions:
            missing_items.append("open_questions_resolved")
            failures.append("open_questions_pending")

        clear = not failures
        next_action = "proceed" if clear else "request_clarification"
        task.agent_outputs["requirements_gate"] = {
            "clear": clear,
            "missing_items": missing_items,
            "next_action": next_action,
            "failures": failures,
        }

        if not clear:
            return PhaseResult(
                phase="requirements_gate",
                status="failed",
                detail=(
                    "requirements not clear: "
                    + ", ".join(missing_items)
                    + ". Request structured clarification."
                ),
                timestamp=time.time(),
            )
        return PhaseResult(
            phase="requirements_gate",
            status="success",
            detail="requirements clear; proceeding",
            timestamp=time.time(),
        )

    def _run_ci_gate_phase(self, task: Task) -> PhaseResult:
        checks = self._extract_checks_to_run(task.agent_outputs.get("test", {}))
        if self.verification_runner is None:
            selected = sorted(checks or [])
            task.agent_outputs["ci_gate"] = {
                "pass": True,
                "checks": selected,
                "failed_checks": [],
                "flaky_quarantined": [],
            }
            return PhaseResult(
                phase="ci_gate",
                status="success",
                detail="ci gate passed (no verification runner configured)",
                timestamp=time.time(),
            )

        result = self.verification_runner.run(checks=checks)
        task.agent_outputs["ci_gate"] = {
            "pass": result.status == "pass",
            "checks": result.checks,
            "failed_checks": result.failures,
            "flaky_quarantined": result.flaky_quarantined,
        }
        if result.status == "pass":
            return PhaseResult(
                phase="ci_gate",
                status="success",
                detail=(
                    "ci gate passed"
                    if not result.flaky_quarantined
                    else f"ci gate passed with quarantined checks: {', '.join(result.flaky_quarantined)}"
                ),
                timestamp=time.time(),
            )
        return PhaseResult(
            phase="ci_gate",
            status="failed",
            detail=f"ci gate failed checks: {', '.join(result.failures)}",
            timestamp=time.time(),
        )

    def _run_security_gate_phase(self, task: Task) -> PhaseResult:
        payload = task.agent_outputs.get("security", {})
        if not isinstance(payload, dict):
            payload = {}
        critical_findings = payload.get("critical_findings")
        high_findings = payload.get("high_findings")
        critical = (
            [item.strip() for item in critical_findings if isinstance(item, str) and item.strip()]
            if isinstance(critical_findings, list)
            else []
        )
        high = (
            [item.strip() for item in high_findings if isinstance(item, str) and item.strip()]
            if isinstance(high_findings, list)
            else []
        )
        waiver_raw = payload.get("waiver_candidates")
        waiver_candidates = (
            [item.strip() for item in waiver_raw if isinstance(item, str) and item.strip()]
            if isinstance(waiver_raw, list)
            else []
        )
        passed = not critical and not high
        task.agent_outputs["security_gate"] = {
            "pass": passed,
            "critical_findings": critical,
            "high_findings": high,
            "waiver_candidates": waiver_candidates,
        }
        if not passed:
            summary = []
            if critical:
                summary.append(f"critical={len(critical)}")
            if high:
                summary.append(f"high={len(high)}")
            return PhaseResult(
                phase="security_gate",
                status="failed",
                detail=f"security gate failed ({', '.join(summary)})",
                timestamp=time.time(),
            )
        return PhaseResult(
            phase="security_gate",
            status="success",
            detail="security gate passed",
            timestamp=time.time(),
        )

    def _run_qa_perf_gate_phase(self, task: Task) -> PhaseResult:
        payload = task.agent_outputs.get("qa_perf", {})
        if not isinstance(payload, dict):
            payload = {}
        raw_matrix = payload.get("qa_matrix")
        matrix: Dict[str, str] = {}
        if isinstance(raw_matrix, dict):
            for key, value in raw_matrix.items():
                if not isinstance(key, str) or not key.strip():
                    continue
                if isinstance(value, str) and value.strip() in {"pass", "fail", "skip"}:
                    matrix[key.strip()] = value.strip()
        raw_violations = payload.get("budget_violations")
        violations = (
            [item.strip() for item in raw_violations if isinstance(item, str) and item.strip()]
            if isinstance(raw_violations, list)
            else []
        )
        matrix_failures = sorted([name for name, status in matrix.items() if status == "fail"])
        passed = not matrix_failures and not violations
        task.agent_outputs["qa_perf_gate"] = {
            "pass": passed,
            "qa_matrix": matrix,
            "budget_violations": violations,
        }
        if not passed:
            parts: List[str] = []
            if matrix_failures:
                parts.append(f"qa failures: {', '.join(matrix_failures)}")
            if violations:
                parts.append(f"budget violations: {', '.join(violations)}")
            return PhaseResult(
                phase="qa_perf_gate",
                status="failed",
                detail="qa/perf gate failed (" + "; ".join(parts) + ")",
                timestamp=time.time(),
            )
        return PhaseResult(
            phase="qa_perf_gate",
            status="success",
            detail="qa/perf gate passed",
            timestamp=time.time(),
        )

    def _run_reviewer_gate_phase(self, task: Task) -> PhaseResult:
        payload = task.agent_outputs.get("review", {})
        if not isinstance(payload, dict):
            payload = {}
        raw_required = payload.get("required_fixes")
        required_fixes = (
            [item.strip() for item in raw_required if isinstance(item, str) and item.strip()]
            if isinstance(raw_required, list)
            else []
        )
        raw_severity = payload.get("max_severity")
        if isinstance(raw_severity, str) and raw_severity.strip():
            max_severity = raw_severity.strip().lower()
        else:
            # Infer severity from risk summary when explicit severity is unavailable.
            risk_summary = payload.get("risk_summary")
            max_severity = "none"
            if isinstance(risk_summary, str):
                lower = risk_summary.lower()
                if "critical" in lower:
                    max_severity = "critical"
                elif "high" in lower:
                    max_severity = "high"
                elif "medium" in lower:
                    max_severity = "medium"
                elif "low" in lower:
                    max_severity = "low"
        blocked_severities = {"high", "critical"}
        passed = (not required_fixes) and (max_severity not in blocked_severities)
        task.agent_outputs["reviewer_gate"] = {
            "pass": passed,
            "required_fixes": required_fixes,
            "max_severity": max_severity,
        }
        if not passed:
            return PhaseResult(
                phase="reviewer_gate",
                status="failed",
                detail=(
                    "reviewer gate failed ("
                    f"required_fixes={len(required_fixes)}, max_severity={max_severity})"
                ),
                timestamp=time.time(),
            )
        return PhaseResult(
            phase="reviewer_gate",
            status="success",
            detail="reviewer gate passed",
            timestamp=time.time(),
        )

    def _run_execute_phase(self, task: Task) -> PhaseResult:
        if task.execution_mode == "dry_run":
            return PhaseResult(
                phase="execute",
                status="success",
                detail="dry-run execute simulation (no tool call)",
                timestamp=time.time(),
            )
        if task.safe_mode:
            return PhaseResult(
                phase="execute",
                status="failed",
                detail="safe mode blocks execute phase",
                timestamp=time.time(),
            )
        if self.tool_runner is None:
            return PhaseResult(
                phase="execute",
                status="success",
                detail="execute stub completed (no tool runner configured)",
                timestamp=time.time(),
            )

        # Prefer concrete file edits emitted by the coder agent.
        code_payload = task.agent_outputs.get("code", {})
        file_edits = self._extract_file_edits(code_payload)
        if file_edits:
            applied = 0
            for edit in file_edits:
                request = ToolRequest(
                    tool="repo.write",
                    params={
                        "path": edit["path"],
                        "content": edit["content"],
                    },
                    trusted=task.trusted_context,
                    human_approved=False,
                )
                result = self.tool_runner.run(request)
                task.tool_calls += 1
                if not result.ok:
                    return PhaseResult(
                        phase="execute",
                        status="failed",
                        detail=result.detail,
                        timestamp=time.time(),
                    )
                applied += 1
            return PhaseResult(
                phase="execute",
                status="success",
                detail=f"applied {applied} file edit(s)",
                timestamp=time.time(),
            )

        request = ToolRequest(
            tool=self.execute_tool_name,
            params={"query": task.prompt},
            trusted=task.trusted_context,
            human_approved=False,
        )
        result = self.tool_runner.run(request)
        task.tool_calls += 1
        if not result.ok:
            return PhaseResult(
                phase="execute",
                status="failed",
                detail=result.detail,
                timestamp=time.time(),
            )
        return PhaseResult(
            phase="execute",
            status="success",
            detail=result.detail,
            timestamp=time.time(),
        )

    def _run_verify_phase(self, task: Task) -> PhaseResult:
        if self.verification_runner is None:
            return PhaseResult(
                phase="verify",
                status="success",
                detail="verify stub completed (no verification runner configured)",
                timestamp=time.time(),
            )

        checks = self._extract_checks_to_run(task.agent_outputs.get("test", {}))
        result = self.verification_runner.run(checks=checks)
        task.agent_outputs["verify"] = {
            "checks": result.checks,
            "status": result.status,
            "flaky_quarantined": result.flaky_quarantined,
            "retry_counts": result.retry_counts,
        }
        if result.status == "pass":
            return PhaseResult(
                phase="verify",
                status="success",
                detail=(
                    "verification passed"
                    if not result.flaky_quarantined
                    else f"verification passed with quarantined checks: {', '.join(result.flaky_quarantined)}"
                ),
                timestamp=time.time(),
            )
        return PhaseResult(
            phase="verify",
            status="failed",
            detail=f"verification failed checks: {', '.join(result.failures)}",
            timestamp=time.time(),
        )

    @staticmethod
    def _extract_file_edits(code_payload: Dict) -> List[Dict[str, str]]:
        raw = code_payload.get("file_edits")
        if not isinstance(raw, list):
            return []
        edits: List[Dict[str, str]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            path_value = item.get("path")
            content_value = item.get("content")
            if not isinstance(path_value, str) or not path_value.strip():
                continue
            if not isinstance(content_value, str):
                continue
            edits.append({"path": path_value.strip(), "content": content_value})
        return edits

    @staticmethod
    def _extract_checks_to_run(test_payload: Dict) -> Optional[List[str]]:
        raw = test_payload.get("checks_to_run")
        if not isinstance(raw, list):
            return None
        checks = [item.strip() for item in raw if isinstance(item, str) and item.strip()]
        return checks or None

    def _run_versioning_phase(self, task: Task) -> PhaseResult:
        if task.execution_mode == "dry_run":
            task.work_branch = f"codex/{task.task_id}-dry-run"
            return PhaseResult(
                phase="versioning",
                status="success",
                detail=f"dry-run versioning simulation {task.work_branch}",
                timestamp=time.time(),
            )
        if self.versioning_agent is None:
            task.work_branch = f"codex/{task.task_id}-task"
            return PhaseResult(
                phase="versioning",
                status="success",
                detail=f"versioning stub assigned {task.work_branch}",
                timestamp=time.time(),
            )

        result = self.versioning_agent.create_branch_for_task(
            task_id=task.task_id,
            prompt=task.prompt,
            base_branch=self.base_branch,
        )
        if not result.ok:
            return PhaseResult(
                phase="versioning",
                status="failed",
                detail=result.detail,
                timestamp=time.time(),
            )
        task.work_branch = result.branch
        return PhaseResult(
            phase="versioning",
            status="success",
            detail=result.detail,
            timestamp=time.time(),
        )

    def _guard_execute_branch(self, task: Task) -> VersioningResult:
        if task.execution_mode == "dry_run":
            return VersioningResult(
                ok=True,
                detail="dry-run mode skips protected-branch guard",
                branch=task.work_branch,
            )
        if self.versioning_agent is None:
            return VersioningResult(
                ok=True, detail="no versioning agent configured", branch=None
            )
        return self.versioning_agent.ensure_not_protected_branch()

    def _run_pre_merge_gate(self, task: Task) -> PhaseResult:
        if self.approval_manager is not None:
            ensure = self.approval_manager.ensure_required()
            if not ensure.ok:
                return PhaseResult(
                    phase="gate_pre_merge",
                    status="failed",
                    detail=ensure.detail,
                    timestamp=time.time(),
                )

        ctx = self.policy_context_resolver(task)
        result = evaluate_pre_merge(ctx)
        if result.allowed:
            return PhaseResult(
                phase="gate_pre_merge",
                status="success",
                detail="pre-merge gate passed",
                timestamp=time.time(),
            )
        waiver = task.agent_outputs.get("waiver_gate", {})
        if isinstance(waiver, dict):
            waiver_needed = bool(waiver.get("waiver_needed", False))
            requires_human = bool(waiver.get("requires_human", True))
            if waiver_needed and not requires_human:
                return PhaseResult(
                    phase="gate_pre_merge",
                    status="success",
                    detail=f"pre-merge gate waived: {', '.join(result.failures)}",
                    timestamp=time.time(),
                )
        return PhaseResult(
            phase="gate_pre_merge",
            status="failed",
            detail=f"pre-merge gate failed: {', '.join(result.failures)}",
            timestamp=time.time(),
        )

    def _run_waiver_gate_phase(self, task: Task) -> PhaseResult:
        ctx = self.policy_context_resolver(task)
        result = evaluate_pre_merge(ctx)
        waiver_needed = not result.allowed
        reasons = list(result.failures) if not result.allowed else []
        requires_human = False

        if waiver_needed:
            record = None
            if self.approval_manager is not None and self.approval_manager.is_approved(CHECKPOINT_WAIVER):
                record = self.approval_manager.records.get(CHECKPOINT_WAIVER)
            if record is None:
                requires_human = True
            elif self._waiver_expired(record.metadata.get("expiry")):
                requires_human = True
                reasons.append("waiver_expired")
            else:
                self._emit_audit(
                    "waiver_logged",
                    task,
                    {
                        "policy_failures": result.failures,
                        "approved_by": record.approved_by,
                        "reason": record.rationale,
                        "owner": record.metadata.get("owner"),
                        "expiry": record.metadata.get("expiry"),
                        "policy_id": record.metadata.get("policy_id"),
                    },
                )
                follow_up_id = f"waiver-follow-up-{task.task_id}"
                self._emit_audit(
                    "waiver_follow_up_task",
                    task,
                    {
                        "task_id": follow_up_id,
                        "owner": record.metadata.get("owner"),
                        "expiry": record.metadata.get("expiry"),
                    },
                )

        task.agent_outputs["waiver_gate"] = {
            "waiver_needed": waiver_needed,
            "reasons": reasons,
            "requires_human": requires_human,
        }
        if waiver_needed and requires_human:
            return PhaseResult(
                phase="waiver_gate",
                status="failed",
                detail=f"waiver required before merge: {', '.join(reasons)}",
                timestamp=time.time(),
            )
        if waiver_needed:
            return PhaseResult(
                phase="waiver_gate",
                status="success",
                detail="waiver approved and logged",
                timestamp=time.time(),
            )
        return PhaseResult(
            phase="waiver_gate",
            status="success",
            detail="waiver not required",
            timestamp=time.time(),
        )

    def _run_release_rollout_phase(self, task: Task) -> PhaseResult:
        stages = ["dev", "staging", "prod"]
        task.agent_outputs["release_rollout"] = {
            "staged": True,
            "stages": stages,
            "canary": True,
            "mode": "environment_staged",
        }
        return PhaseResult(
            phase="release_rollout",
            status="success",
            detail="release rollout planned (dev -> staging -> prod with canary)",
            timestamp=time.time(),
        )

    def _run_deploy_runner_phase(self, task: Task) -> PhaseResult:
        rollout = task.agent_outputs.get("release_rollout", {})
        stages = rollout.get("stages") if isinstance(rollout, dict) else None
        if not isinstance(stages, list) or not stages:
            stages = ["dev", "staging", "prod"]
        deployed_stage = str(stages[-1])
        task.agent_outputs["deploy_runner"] = {
            "deployed_stage": deployed_stage,
            "rollout_complete": True,
            "strategy": "canary" if isinstance(rollout, dict) and rollout.get("canary") else "direct",
        }
        return PhaseResult(
            phase="deploy_runner",
            status="success",
            detail=f"deploy runner completed staged rollout through {deployed_stage}",
            timestamp=time.time(),
        )

    def _run_health_gate_phase(self, task: Task) -> PhaseResult:
        observability = task.agent_outputs.get("observability", {})
        if not isinstance(observability, dict):
            observability = {}
        raw_failed = observability.get("failed_signals")
        failed_signals = (
            [item.strip() for item in raw_failed if isinstance(item, str) and item.strip()]
            if isinstance(raw_failed, list)
            else []
        )
        healthy_flag = observability.get("healthy")
        healthy = bool(healthy_flag) if isinstance(healthy_flag, bool) else not failed_signals
        slo = observability.get("slo_status")
        slo_status = slo if isinstance(slo, str) and slo in {"green", "yellow", "red"} else ("green" if healthy else "red")
        task.agent_outputs["health_gate"] = {
            "healthy": healthy,
            "failed_signals": failed_signals,
            "slo_status": slo_status,
        }
        if not healthy:
            self._emit_audit(
                "auto_rollback",
                task,
                {
                    "reason": "health_gate_failure",
                    "failed_signals": failed_signals,
                },
            )
            return PhaseResult(
                phase="health_gate",
                status="failed",
                detail=(
                    "health gate failed; auto rollback executed"
                    + (f" ({', '.join(failed_signals)})" if failed_signals else "")
                ),
                timestamp=time.time(),
            )
        return PhaseResult(
            phase="health_gate",
            status="success",
            detail="health gate passed",
            timestamp=time.time(),
        )

    def _run_platform_health_gate_phase(self, task: Task) -> PhaseResult:
        payload = task.agent_outputs.get("platform_health", {})
        if not isinstance(payload, dict):
            payload = {}
        raw_issues = payload.get("issues")
        issues = (
            [item.strip() for item in raw_issues if isinstance(item, str) and item.strip()]
            if isinstance(raw_issues, list)
            else []
        )
        healthy_flag = payload.get("healthy")
        healthy = bool(healthy_flag) if isinstance(healthy_flag, bool) else not issues
        safe_mode_required = bool(payload.get("safe_mode_required", not healthy))
        task.agent_outputs["platform_health_gate"] = {
            "healthy": healthy,
            "issues": issues,
            "safe_mode_required": safe_mode_required,
        }
        if not healthy:
            if safe_mode_required:
                task.safe_mode = True
            return PhaseResult(
                phase="platform_health_gate",
                status="failed",
                detail=(
                    "platform health gate failed"
                    + ("; safe mode engaged" if safe_mode_required else "")
                    + (f" ({', '.join(issues)})" if issues else "")
                ),
                timestamp=time.time(),
            )
        return PhaseResult(
            phase="platform_health_gate",
            status="success",
            detail="platform health gate passed",
            timestamp=time.time(),
        )

    def _run_dr_gate_phase(self, task: Task) -> PhaseResult:
        payload = task.agent_outputs.get("dr", {})
        if not isinstance(payload, dict):
            payload = {}
        pass_value = payload.get("pass")
        dr_pass = bool(pass_value) if isinstance(pass_value, bool) else True
        rto = payload.get("rto_seconds")
        rpo = payload.get("rpo_seconds")
        raw_failures = payload.get("failures")
        failures = (
            [item.strip() for item in raw_failures if isinstance(item, str) and item.strip()]
            if isinstance(raw_failures, list)
            else []
        )
        task.agent_outputs["dr_gate"] = {
            "pass": dr_pass,
            "rto_seconds": int(rto) if isinstance(rto, (int, float)) else 0,
            "rpo_seconds": int(rpo) if isinstance(rpo, (int, float)) else 0,
            "failures": failures,
        }
        if not dr_pass:
            return PhaseResult(
                phase="dr_gate",
                status="failed",
                detail="dr gate failed" + (f": {', '.join(failures)}" if failures else ""),
                timestamp=time.time(),
            )
        return PhaseResult(
            phase="dr_gate",
            status="success",
            detail="dr gate passed",
            timestamp=time.time(),
        )

    def _run_access_gate_phase(self, task: Task) -> PhaseResult:
        payload = task.agent_outputs.get("access", {})
        if not isinstance(payload, dict):
            payload = {}
        compliant_value = payload.get("compliant")
        compliant = bool(compliant_value) if isinstance(compliant_value, bool) else True
        raw_violations = payload.get("violations")
        violations = (
            [item.strip() for item in raw_violations if isinstance(item, str) and item.strip()]
            if isinstance(raw_violations, list)
            else []
        )
        raw_recertified = payload.get("recertified_principals")
        recertified = (
            [item.strip() for item in raw_recertified if isinstance(item, str) and item.strip()]
            if isinstance(raw_recertified, list)
            else []
        )
        task.agent_outputs["access_gate"] = {
            "compliant": compliant,
            "violations": violations,
            "recertified_principals": recertified,
        }
        if not compliant:
            return PhaseResult(
                phase="access_gate",
                status="failed",
                detail="access gate failed" + (f": {', '.join(violations)}" if violations else ""),
                timestamp=time.time(),
            )
        return PhaseResult(
            phase="access_gate",
            status="success",
            detail="access gate passed",
            timestamp=time.time(),
        )

    def _run_human_checkpoints_phase(self, task: Task) -> PhaseResult:
        if self.approval_manager is None:
            task.agent_outputs["human_checkpoints"] = {
                "approved": True,
                "missing": [],
            }
            return PhaseResult(
                phase="human_checkpoints",
                status="success",
                detail="human checkpoint stub completed (no approval manager configured)",
                timestamp=time.time(),
            )

        missing = self.approval_manager.missing_required()
        task.agent_outputs["human_checkpoints"] = {
            "approved": len(missing) == 0,
            "missing": missing,
        }
        if missing:
            return PhaseResult(
                phase="human_checkpoints",
                status="failed",
                detail=f"missing required approvals: {', '.join(missing)}",
                timestamp=time.time(),
            )
        return PhaseResult(
            phase="human_checkpoints",
            status="success",
            detail="required approvals present",
            timestamp=time.time(),
        )

    def _validate_phase_handoff(self, phase: str, task: Task) -> Optional[PhaseResult]:
        if self.contract_registry is None:
            return None

        payload = self._build_phase_payload(phase, task)
        validation = self.contract_registry.validate(phase, payload)
        if not validation.valid:
            return PhaseResult(
                phase=phase,
                status="failed",
                detail=f"handoff schema validation failed: {'; '.join(validation.errors)}",
                timestamp=time.time(),
            )

        task.handoff_artifacts[phase] = payload
        return None

    def _build_phase_payload(self, phase: str, task: Task) -> Dict:
        if phase in task.agent_outputs:
            return task.agent_outputs[phase]
        builder = self.phase_payload_builders.get(phase)
        if builder is not None:
            return builder(task)
        return self._default_phase_payload(phase, task)

    def _default_phase_payload(self, phase: str, task: Task) -> Dict:
        if phase == "ingress_guard":
            return {
                "sanitized": False,
                "tainted": False,
                "redactions": [],
                "notes": [],
            }
        if phase == "budget_envelope":
            return {
                "token_budget": task.budget_token_limit or 1,
                "tool_budget": task.budget_tool_limit or 1,
                "time_budget_seconds": task.budget_time_limit_seconds or 1.0,
                "budget_id": task.budget_envelope_id or "",
                "policy": "priority_profile_v1",
            }
        if phase == "intent":
            return {
                "rewritten_prompt": task.prompt,
                "target_files": [],
                "success_criteria": [],
            }
        if phase == "triage":
            return {
                "task_type": "bug",
                "risk_level": "low",
                "scope_size": "small",
                "intensity": "normal",
                "confidence_score": 0.9,
            }
        if phase == "router_gate":
            return {
                "allowed": True,
                "confidence": 1.0,
                "threshold": 0.7,
                "route": "normal",
                "failures": [],
            }
        if phase == "requirements":
            return {
                "acceptance_criteria": ["placeholder acceptance criteria"],
                "non_goals": [],
                "open_questions": [],
            }
        if phase == "requirements_gate":
            return {
                "clear": True,
                "missing_items": [],
                "next_action": "proceed",
                "failures": [],
            }
        if phase == "versioning":
            return {"work_branch": task.work_branch or ""}
        if phase == "plan":
            return {
                "steps": ["stub plan step"],
                "test_plan": ["run deterministic checks"],
                "risk_notes": [],
            }
        if phase == "code":
            return {
                "changes": ["stub code change"],
                "files_touched": ["README.md"],
            }
        if phase == "test":
            return {
                "tests_added": ["tests/test_stub.py"],
                "checks_to_run": ["unit"],
            }
        if phase == "ci_gate":
            return {
                "pass": True,
                "checks": [],
                "failed_checks": [],
                "flaky_quarantined": [],
            }
        if phase == "security_gate":
            return {
                "pass": True,
                "critical_findings": [],
                "high_findings": [],
                "waiver_candidates": [],
            }
        if phase == "qa_perf_gate":
            return {
                "pass": True,
                "qa_matrix": {},
                "budget_violations": [],
            }
        if phase == "review":
            return {
                "findings": [],
                "risk_summary": "low risk",
                "required_fixes": [],
            }
        if phase == "reviewer_gate":
            return {
                "pass": True,
                "required_fixes": [],
                "max_severity": "none",
            }
        if phase == "docs":
            return {
                "changelog": "updated",
                "runbook_delta": "none",
                "migration_notes": "none",
            }
        if phase == "execute":
            tool_name = self.execute_tool_name if self.tool_runner else "stub.execute"
            return {
                "changes": ["stub change applied"],
                "tools_used": [tool_name],
            }
        if phase == "verify":
            return {
                "checks": ["lint", "unit"],
                "status": "pass",
            }
        if phase == "gate_pre_merge":
            return {"allowed": True, "failures": []}
        if phase == "human_checkpoints":
            return {"approved": True, "missing": []}
        if phase == "waiver_gate":
            return {"waiver_needed": False, "reasons": [], "requires_human": False}
        if phase == "release_rollout":
            return {
                "staged": True,
                "stages": ["dev", "staging", "prod"],
                "canary": True,
                "mode": "environment_staged",
            }
        if phase == "deploy_runner":
            return {
                "deployed_stage": "prod",
                "rollout_complete": True,
                "strategy": "canary",
            }
        if phase == "platform_health_gate":
            return {"healthy": True, "issues": [], "safe_mode_required": False}
        if phase == "dr_gate":
            return {"pass": True, "rto_seconds": 0, "rpo_seconds": 0, "failures": []}
        if phase == "access_gate":
            return {"compliant": True, "violations": [], "recertified_principals": []}
        if phase == "finalize":
            return {"outcome": "completed", "summary": "task finalized"}
        return {"phase": phase}

    @staticmethod
    def _redact_prompt(text: str) -> Tuple[str, List[str]]:
        redacted = text
        redactions: List[str] = []
        patterns = [
            ("openai_api_key", r"\bsk-[A-Za-z0-9]{16,}\b"),
            ("github_token", r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"),
            ("aws_access_key_id", r"\bAKIA[0-9A-Z]{16}\b"),
            ("bearer_token", r"(?i)\bbearer\s+[A-Za-z0-9._-]{16,}\b"),
            ("credential_assignment", r"(?i)\b(api[_-]?key|token|password|secret)\b\s*[:=]\s*['\"]?[^\s,'\"]+"),
        ]
        for label, pattern in patterns:
            matches = re.findall(pattern, redacted)
            if not matches:
                continue
            redacted = re.sub(pattern, f"[REDACTED_{label.upper()}]", redacted)
            redactions.append(f"{label}:{len(matches)}")
        return redacted, redactions

    @staticmethod
    def _is_tainted_prompt(text: str) -> bool:
        lower = text.lower()
        markers = [
            "```",
            "<script",
            "http://",
            "https://",
            "-----begin",
            "traceback",
            "stack trace",
            "untrusted",
            "pasted log",
        ]
        return any(marker in lower for marker in markers)

    @staticmethod
    def _default_policy_context(_: Task) -> PolicyContext:
        return PolicyContext(
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
            post_deploy_smoke_passed=True,
            slo_healthy=True,
            alerts_healthy=True,
        )

    def _emit_audit(self, event_type: str, task: Task, payload: Dict) -> None:
        if self.audit_logger is None:
            return
        self.audit_logger.emit(event_type=event_type, task_id=task.task_id, payload=payload)

    def _emit_phase_event(self, task: Task, event: Dict[str, Any]) -> None:
        callback = self.phase_event_callback
        if callback is None:
            return
        callback(task, event)

    def _write_provenance_bundle(self, task: Task, outcome: str) -> None:
        if self.provenance_bundler is None:
            return
        path = self.provenance_bundler.write_bundle(
            task_id=task.task_id,
            task_payload=asdict(task),
            outcome=outcome,
            metadata=self._build_provenance_metadata(task=task, outcome=outcome),
        )
        task.provenance_bundle_path = str(path)

    def _build_provenance_metadata(self, task: Task, outcome: str) -> Dict:
        pr_title = f"codex: {task.prompt.strip()[:72]}" if task.prompt.strip() else f"codex task {task.task_id}"
        return {
            "pipeline_phase_order": self.phase_order,
            "base_branch": self.base_branch,
            "work_branch": task.work_branch,
            "task_state_revision": task.state_revision,
            "outcome": outcome,
            "pr_metadata": {
                "branch": task.work_branch,
                "base_branch": self.base_branch,
                "title": pr_title,
                "provenance_bundle": task.provenance_bundle_path,
            },
        }

    def _check_budget(self, task: Task, start_time: float) -> List[str]:
        elapsed = time.time() - start_time
        task.token_estimate = self._estimate_task_tokens(task)
        failures: List[str] = []
        if task.budget_token_limit is not None and task.token_estimate > task.budget_token_limit:
            failures.append("token_budget")
        if task.budget_tool_limit is not None and task.tool_calls > task.budget_tool_limit:
            failures.append("tool_budget")
        if task.budget_time_limit_seconds is not None and elapsed > task.budget_time_limit_seconds:
            failures.append("time_budget_seconds")
        if self.resilience_manager is not None:
            failures.extend(
                self.resilience_manager.budget_exceeded(
                    task_id=task.task_id,
                    elapsed_seconds=elapsed,
                    tool_calls=task.tool_calls,
                )
            )
        seen = set()
        unique: List[str] = []
        for failure in failures:
            if failure in seen:
                continue
            seen.add(failure)
            unique.append(failure)
        return unique

    def _replay_invalidation_reasons(self, bundle: Dict[str, Any]) -> List[str]:
        issues: List[str] = []
        material = bundle.get("material")
        if not isinstance(material, dict):
            issues.append("bundle missing material object")
            return issues
        digest = bundle.get("digest")
        if not isinstance(digest, str) or not digest.strip():
            issues.append("bundle missing digest")
        else:
            canonical = json.dumps(material, sort_keys=True, separators=(",", ":"))
            expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            if expected != digest:
                issues.append("bundle digest mismatch")
        task_payload = material.get("task")
        if not isinstance(task_payload, dict):
            issues.append("bundle missing task payload")
            return issues
        history = task_payload.get("phase_history")
        if not isinstance(history, list):
            issues.append("bundle phase_history is not a list")
            return issues
        allowed = set(self.phase_order) | {"budget"}
        unknown: List[str] = []
        for item in history:
            if not isinstance(item, dict):
                continue
            phase = item.get("phase")
            if isinstance(phase, str) and phase not in allowed and phase not in unknown:
                unknown.append(phase)
        if unknown:
            issues.append(f"bundle contains unknown phase(s): {', '.join(sorted(unknown))}")
        return issues

    @staticmethod
    def _estimate_task_tokens(task: Task) -> int:
        total_chars = len(task.prompt)
        for payload in task.agent_outputs.values():
            try:
                total_chars += len(json.dumps(payload, sort_keys=True))
            except Exception:
                total_chars += len(str(payload))
        return max(1, total_chars // 4)

    @staticmethod
    def _waiver_expired(expiry: object) -> bool:
        if expiry is None:
            return True
        now = datetime.now(timezone.utc)
        if isinstance(expiry, (int, float)):
            return datetime.fromtimestamp(float(expiry), tz=timezone.utc) <= now
        if isinstance(expiry, str):
            text = expiry.strip()
            if not text:
                return True
            try:
                if text.endswith("Z"):
                    text = text[:-1] + "+00:00"
                dt = datetime.fromisoformat(text)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt <= now
            except Exception:
                return True
        return True

    @staticmethod
    def _coerce_confidence_score(payload: Dict) -> float:
        if not isinstance(payload, dict):
            return 1.0
        value = payload.get("confidence_score", payload.get("confidence", 1.0))
        try:
            score = float(value)
        except (TypeError, ValueError):
            return 1.0
        if score < 0:
            return 0.0
        if score > 1:
            return 1.0
        return score
