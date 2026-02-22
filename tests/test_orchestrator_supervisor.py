import tempfile
import time
import unittest
from pathlib import Path

from agents.basic_agents import BasicAgentSuite
from agents.model_client import StaticHostedModelClient
from contracts.registry import ContractRegistry
from orchestrator.approvals import (
    CHECKPOINT_MERGE,
    CHECKPOINT_REQUIREMENTS,
    ApprovalManager,
    ApprovalRecord,
)
from orchestrator.audit import AuditLogger, ProvenanceBundler
from orchestrator.supervisor import PhaseResult, StateStore, Supervisor, Task, TaskQueue
from orchestrator.tool_runner import ToolResult, ToolRunner
from orchestrator.resilience import ResilienceConfig, ResilienceManager, TaskBudget
from orchestrator.verification import FlakyPolicy, VerificationRunner
from policy.engine import PolicyContext
from orchestrator.versioning import VersioningResult


class TaskQueueTests(unittest.TestCase):
    def test_priority_order_is_respected(self) -> None:
        queue = TaskQueue()
        base = time.time()
        queue.enqueue(
            Task(task_id="t1", prompt="a", priority="background", created_at=base)
        )
        queue.enqueue(
            Task(
                task_id="t2",
                prompt="b",
                priority="interactive",
                created_at=base + 1,
            )
        )

        first = queue.dequeue()
        second = queue.dequeue()

        assert first is not None
        assert second is not None
        self.assertEqual("t2", first.task_id)
        self.assertEqual("t1", second.task_id)


class SupervisorTests(unittest.TestCase):
    def test_create_task_persists_state_packet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            store = StateStore(state_path)
            supervisor = Supervisor(TaskQueue(), store)

            task = supervisor.create_task("hello world")
            saved = store.load_task(task.task_id)

            self.assertIsNotNone(saved)
            assert saved is not None
            self.assertEqual("queued", saved["status"])
            self.assertEqual("hello world", saved["prompt"])

    def test_process_next_runs_all_phases_until_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            store = StateStore(state_path)
            supervisor = Supervisor(TaskQueue(), store)

            supervisor.create_task("ship it")
            task = supervisor.process_next()

            assert task is not None
            self.assertEqual("completed", task.status)
            self.assertEqual(17, len(task.phase_history))
            self.assertEqual("ingress_guard", task.phase_history[0].phase)
            self.assertEqual("budget_envelope", task.phase_history[1].phase)
            self.assertEqual("intent", task.phase_history[2].phase)
            self.assertEqual("router_gate", task.phase_history[4].phase)
            self.assertEqual("finalize", task.phase_history[-1].phase)
            self.assertEqual("gate_pre_merge", task.phase_history[-2].phase)
            self.assertEqual("human_checkpoints", task.phase_history[-3].phase)
            self.assertEqual("versioning", task.phase_history[6].phase)
            self.assertEqual("code", task.phase_history[8].phase)
            self.assertEqual("docs", task.phase_history[11].phase)
            self.assertIsNotNone(task.work_branch)

    def test_process_next_blocks_on_failed_phase(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            store = StateStore(state_path)

            def failing_requirements(_: Task) -> PhaseResult:
                return PhaseResult(
                    phase="requirements",
                    status="failed",
                    detail="requirements ambiguous",
                    timestamp=time.time(),
                )

            supervisor = Supervisor(
                queue=TaskQueue(),
                state_store=store,
                phase_handlers={"requirements": failing_requirements},
            )

            supervisor.create_task("do the thing")
            task = supervisor.process_next()

            assert task is not None
            self.assertEqual("blocked", task.status)
            self.assertEqual(
                [
                    "ingress_guard",
                    "budget_envelope",
                    "intent",
                    "triage",
                    "router_gate",
                    "requirements",
                ],
                [p.phase for p in task.phase_history],
            )

    def test_run_prompt_creates_and_processes_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.json")
            supervisor = Supervisor(TaskQueue(), store)

            task = supervisor.run_prompt("test prompt", priority="interactive")
            self.assertEqual("completed", task.status)
            self.assertEqual("interactive", task.priority)

    def test_dry_run_skips_real_versioning_and_tool_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.json")

            class CountingVersioningAgent:
                def __init__(self) -> None:
                    self.create_calls = 0
                    self.guard_calls = 0

                def create_branch_for_task(
                    self, task_id: str, prompt: str, base_branch: str = "main"
                ) -> VersioningResult:
                    self.create_calls += 1
                    return VersioningResult(ok=True, detail="created", branch=f"codex/{task_id}-x")

                def ensure_not_protected_branch(self) -> VersioningResult:
                    self.guard_calls += 1
                    return VersioningResult(ok=True, detail="ok", branch="codex/ok")

            calls = {"tool": 0}
            tool_runner = ToolRunner(
                handlers={
                    "repo.search": lambda _req: (
                        calls.__setitem__("tool", calls["tool"] + 1) or ToolResult(ok=True, detail="ok")
                    )
                }
            )
            versioning = CountingVersioningAgent()
            supervisor = Supervisor(
                queue=TaskQueue(),
                state_store=store,
                versioning_agent=versioning,  # type: ignore[arg-type]
                tool_runner=tool_runner,
            )

            task = supervisor.run_prompt("simulate only", dry_run=True)
            self.assertEqual("completed", task.status)
            self.assertEqual("dry_run", task.execution_mode)
            self.assertEqual(0, versioning.create_calls)
            self.assertEqual(0, versioning.guard_calls)
            self.assertEqual(0, calls["tool"])
            self.assertIn("dry-run", task.phase_history[6].detail)
            self.assertIn("dry-run", [p.detail for p in task.phase_history if p.phase == "execute"][0])

    def test_execute_blocks_untrusted_non_read_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.json")
            tool_runner = ToolRunner(
                handlers={
                    "test.run": lambda req: ToolResult(ok=True, detail="tests ok")
                }
            )

            supervisor = Supervisor(
                TaskQueue(),
                store,
                tool_runner=tool_runner,
                execute_tool_name="test.run",
            )
            task = supervisor.create_task("query")
            task.trusted_context = False

            processed = supervisor.process_next()
            assert processed is not None
            self.assertEqual("blocked", processed.status)
            self.assertEqual("execute", processed.phase_history[-1].phase)
            self.assertIn("untrusted context cannot run tool", processed.phase_history[-1].detail)

    def test_execute_applies_file_edits_from_code_agent_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            store = StateStore(repo / "state.json")

            def repo_write(req):
                target = repo / req.params["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(req.params["content"], encoding="utf-8")
                return ToolResult(ok=True, detail=f"wrote {target}")

            tool_runner = ToolRunner(
                handlers={
                    "repo.write": repo_write,
                    "repo.search": lambda _req: ToolResult(ok=True, detail="search ok"),
                },
                safe_tools={"repo.write", "repo.search", "repo.read", "test.run", "lint.run"},
            )
            handlers = {
                "code": lambda task: (
                    task.agent_outputs.__setitem__(
                        "code",
                        {
                            "changes": ["replace ui"],
                            "files_touched": ["src/ui.js"],
                            "file_edits": [{"path": "src/ui.js", "content": "export const x = 1;\n"}],
                        },
                    )
                    or PhaseResult(
                        phase="code",
                        status="success",
                        detail="coder_agent produced payload",
                        timestamp=time.time(),
                    )
                )
            }
            supervisor = Supervisor(
                queue=TaskQueue(),
                state_store=store,
                tool_runner=tool_runner,
                phase_handlers=handlers,
            )
            supervisor.create_task("apply edit")
            task = supervisor.process_next()

            assert task is not None
            self.assertEqual("completed", task.status)
            self.assertEqual("export const x = 1;\n", (repo / "src/ui.js").read_text(encoding="utf-8"))
            execute_phase = [p for p in task.phase_history if p.phase == "execute"][0]
            self.assertIn("applied 1 file edit", execute_phase.detail)

    def test_pre_merge_gate_blocks_when_policy_context_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.json")

            def failing_ctx(_: Task) -> PolicyContext:
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

            supervisor = Supervisor(
                queue=TaskQueue(),
                state_store=store,
                policy_context_resolver=failing_ctx,
            )

            supervisor.create_task("security regression")
            task = supervisor.process_next()

            assert task is not None
            self.assertEqual("blocked", task.status)
            self.assertEqual("gate_pre_merge", task.phase_history[-1].phase)
            self.assertEqual("failed", task.phase_history[-1].status)
            self.assertIn("security", task.phase_history[-1].detail)

    def test_execute_is_blocked_on_protected_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.json")

            class FakeVersioningAgent:
                def create_branch_for_task(
                    self, task_id: str, prompt: str, base_branch: str = "main"
                ) -> VersioningResult:
                    return VersioningResult(
                        ok=True, detail="branch created", branch=f"codex/{task_id}-x"
                    )

                def ensure_not_protected_branch(self) -> VersioningResult:
                    return VersioningResult(
                        ok=False,
                        detail="protected branch edit blocked: main",
                        branch="main",
                    )

            supervisor = Supervisor(
                queue=TaskQueue(),
                state_store=store,
                versioning_agent=FakeVersioningAgent(),  # type: ignore[arg-type]
            )

            supervisor.create_task("blocked task")
            task = supervisor.process_next()

            assert task is not None
            self.assertEqual("blocked", task.status)
            self.assertEqual("execute", task.phase_history[-1].phase)
            self.assertEqual("failed", task.phase_history[-1].status)
            self.assertIn("protected branch edit blocked", task.phase_history[-1].detail)

    def test_verify_phase_blocks_on_failed_deterministic_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.json")

            def executor(check: str) -> bool:
                return check != "unit"

            verification_runner = VerificationRunner(
                checks=["lint", "unit"],
                check_executor=executor,
                flaky_policy=FlakyPolicy(
                    max_retries=0,
                    quarantine_after_failures=2,
                    require_owner=True,
                    require_expiry=True,
                ),
            )
            supervisor = Supervisor(
                queue=TaskQueue(),
                state_store=store,
                verification_runner=verification_runner,
            )

            supervisor.create_task("verify failure")
            task = supervisor.process_next()

            assert task is not None
            self.assertEqual("blocked", task.status)
            self.assertEqual("verify", task.phase_history[-1].phase)
            self.assertIn("verification failed checks: unit", task.phase_history[-1].detail)

    def test_contract_validation_blocks_invalid_handoff_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.json")
            schema_dir = Path(__file__).resolve().parents[1] / "contracts" / "schemas"
            registry = ContractRegistry.from_directory(schema_dir)

            supervisor = Supervisor(
                queue=TaskQueue(),
                state_store=store,
                contract_registry=registry,
                phase_payload_builders={
                    # Invalid triage payload: missing required keys.
                    "triage": lambda _task: {"task_type": "bug"}
                },
            )

            supervisor.create_task("invalid handoff")
            task = supervisor.process_next()

            assert task is not None
            self.assertEqual("blocked", task.status)
            self.assertEqual("triage", task.phase_history[-1].phase)
            self.assertEqual("failed", task.phase_history[-1].status)
            self.assertIn("handoff schema validation failed", task.phase_history[-1].detail)

    def test_supervisor_init_fails_when_phase_order_missing_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.json")
            schema_dir = Path(__file__).resolve().parents[1] / "contracts" / "schemas"
            registry = ContractRegistry.from_directory(schema_dir)
            with self.assertRaisesRegex(ValueError, "missing handoff schema"):
                Supervisor(
                    queue=TaskQueue(),
                    state_store=store,
                    contract_registry=registry,
                    phase_order=["intent", "not_real_phase"],
                )

    def test_contract_validation_succeeds_and_records_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.json")
            schema_dir = Path(__file__).resolve().parents[1] / "contracts" / "schemas"
            registry = ContractRegistry.from_directory(schema_dir)
            supervisor = Supervisor(
                queue=TaskQueue(),
                state_store=store,
                contract_registry=registry,
            )

            supervisor.create_task("valid handoff")
            task = supervisor.process_next()

            assert task is not None
            self.assertEqual("completed", task.status)
            for phase in [
                "ingress_guard",
                "budget_envelope",
                "intent",
                "triage",
                "router_gate",
                "requirements",
                "versioning",
                "plan",
                "code",
                "test",
                "review",
                "docs",
                "execute",
                "verify",
                "human_checkpoints",
                "gate_pre_merge",
                "finalize",
            ]:
                self.assertIn(phase, task.handoff_artifacts)

    def test_ingress_guard_redacts_secrets_and_marks_tainted_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            store = StateStore(state_path)
            supervisor = Supervisor(TaskQueue(), store)

            task = supervisor.run_prompt(
                "Use this API key sk-1234567890abcdef1234567890abcdef and pasted log from https://example.com"
            )
            ingress = task.agent_outputs["ingress_guard"]
            self.assertTrue(ingress["sanitized"])
            self.assertTrue(ingress["tainted"])
            self.assertTrue(ingress["redactions"])
            self.assertFalse(task.trusted_context)
            self.assertNotIn("sk-1234567890abcdef1234567890abcdef", task.prompt)

    def test_budget_envelope_blocks_when_tool_budget_exceeded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            store = StateStore(repo / "state.json")

            def repo_write(req):
                target = repo / req.params["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(req.params["content"], encoding="utf-8")
                return ToolResult(ok=True, detail=f"wrote {target}")

            tool_runner = ToolRunner(
                handlers={"repo.write": repo_write, "repo.search": lambda _req: ToolResult(ok=True, detail="search ok")},
                safe_tools={"repo.write", "repo.search", "repo.read", "test.run", "lint.run"},
            )

            edits = []
            touched = []
            for i in range(11):
                rel = f"src/file_{i}.txt"
                touched.append(rel)
                edits.append({"path": rel, "content": f"value-{i}\n"})

            handlers = {
                "code": lambda task: (
                    task.agent_outputs.__setitem__("code", {"changes": ["bulk write"], "files_touched": touched, "file_edits": edits})
                    or PhaseResult(phase="code", status="success", detail="bulk edits prepared", timestamp=time.time())
                )
            }

            supervisor = Supervisor(
                queue=TaskQueue(),
                state_store=store,
                phase_handlers=handlers,
                tool_runner=tool_runner,
            )
            supervisor.create_task("exceed tool budget", priority="background")
            task = supervisor.process_next()

            assert task is not None
            self.assertEqual("blocked", task.status)
            self.assertEqual("budget", task.phase_history[-1].phase)
            self.assertIn("tool_budget", task.phase_history[-1].detail)

    def test_router_gate_uses_deeper_pipeline_on_low_confidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.json")

            def low_confidence_triage(task: Task) -> PhaseResult:
                task.agent_outputs["triage"] = {
                    "task_type": "bug",
                    "risk_level": "medium",
                    "scope_size": "medium",
                    "intensity": "deep",
                    "confidence_score": 0.5,
                }
                return PhaseResult(
                    phase="triage",
                    status="success",
                    detail="triage low confidence",
                    timestamp=time.time(),
                )

            supervisor = Supervisor(
                queue=TaskQueue(),
                state_store=store,
                phase_handlers={"triage": low_confidence_triage},
            )
            supervisor.create_task("route via fallback")
            task = supervisor.process_next()

            assert task is not None
            self.assertEqual("completed", task.status)
            self.assertTrue(task.degraded_mode)
            router = task.agent_outputs["router_gate"]
            self.assertEqual("deeper_pipeline", router["route"])
            self.assertFalse(router["allowed"])

    def test_router_gate_blocks_when_human_triage_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.json")

            def very_low_confidence_triage(task: Task) -> PhaseResult:
                task.agent_outputs["triage"] = {
                    "task_type": "bug",
                    "risk_level": "high",
                    "scope_size": "large",
                    "intensity": "deep",
                    "confidence_score": 0.2,
                }
                return PhaseResult(
                    phase="triage",
                    status="success",
                    detail="triage very low confidence",
                    timestamp=time.time(),
                )

            supervisor = Supervisor(
                queue=TaskQueue(),
                state_store=store,
                phase_handlers={"triage": very_low_confidence_triage},
            )
            supervisor.create_task("needs human triage")
            task = supervisor.process_next()

            assert task is not None
            self.assertEqual("blocked", task.status)
            self.assertEqual("router_gate", task.phase_history[-1].phase)
            self.assertIn("human-triage threshold", task.phase_history[-1].detail)

    def test_basic_agent_suite_handlers_integrate_with_supervisor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.json")
            schema_dir = Path(__file__).resolve().parents[1] / "contracts" / "schemas"
            registry = ContractRegistry.from_directory(schema_dir)
            model = StaticHostedModelClient(
                responses={
                    "intent_agent": {
                        "rewritten_prompt": "Implement requested change with exact file paths and full file_edits.",
                        "target_files": ["file.py"],
                        "success_criteria": ["requested behavior implemented"],
                    },
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
                    "coder_agent": {
                        "changes": ["chg"],
                        "files_touched": ["file.py"],
                    },
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
            )
            handlers = BasicAgentSuite(model).build_phase_handlers()
            supervisor = Supervisor(
                queue=TaskQueue(),
                state_store=store,
                contract_registry=registry,
                phase_handlers=handlers,
            )

            supervisor.create_task("agent integration")
            task = supervisor.process_next()

            assert task is not None
            self.assertEqual("completed", task.status)
            self.assertIn("triage", task.agent_outputs)
            self.assertIn("docs", task.agent_outputs)

    def test_human_checkpoint_phase_blocks_without_required_approvals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.json")
            approvals = ApprovalManager(required_checkpoints={CHECKPOINT_MERGE})
            supervisor = Supervisor(
                queue=TaskQueue(),
                state_store=store,
                approval_manager=approvals,
            )

            supervisor.create_task("approval missing")
            task = supervisor.process_next()

            assert task is not None
            self.assertEqual("blocked", task.status)
            self.assertEqual("human_checkpoints", task.phase_history[-1].phase)
            self.assertIn("missing required approvals", task.phase_history[-1].detail)

    def test_human_checkpoint_phase_passes_with_structured_approvals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.json")
            schema_dir = Path(__file__).resolve().parents[1] / "contracts" / "schemas"
            registry = ContractRegistry.from_directory(schema_dir)
            approvals = ApprovalManager(
                required_checkpoints={CHECKPOINT_REQUIREMENTS, CHECKPOINT_MERGE}
            )
            approvals.submit(
                ApprovalRecord(
                    checkpoint=CHECKPOINT_REQUIREMENTS,
                    approved_by="pm",
                    approved_at=1.0,
                    rationale="clear",
                    criteria_ack=["reviewed"],
                    risk_ack=True,
                    metadata={"acceptance_criteria_confirmed": True},
                )
            )
            approvals.submit(
                ApprovalRecord(
                    checkpoint=CHECKPOINT_MERGE,
                    approved_by="lead",
                    approved_at=2.0,
                    rationale="ready",
                    criteria_ack=["all checks done"],
                    risk_ack=True,
                    metadata={"merge_strategy": "squash"},
                )
            )
            supervisor = Supervisor(
                queue=TaskQueue(),
                state_store=store,
                approval_manager=approvals,
                contract_registry=registry,
            )

            supervisor.create_task("approval present")
            task = supervisor.process_next()

            assert task is not None
            self.assertEqual("completed", task.status)
            self.assertIn("human_checkpoints", task.handoff_artifacts)
            self.assertTrue(task.handoff_artifacts["human_checkpoints"]["approved"])

    def test_resilience_retry_recovers_phase(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.json")
            resilience = ResilienceManager(
                budget=TaskBudget(
                    max_wall_time_seconds=120.0,
                    max_phase_attempts=200,
                    max_tool_calls=20,
                ),
                config=ResilienceConfig(
                    max_retries_per_phase=1,
                    fallback_phases=set(),
                    safe_mode_failure_threshold=5,
                ),
            )
            attempts = {"triage": 0}

            def flaky_triage(_: Task) -> PhaseResult:
                attempts["triage"] += 1
                if attempts["triage"] == 1:
                    return PhaseResult(
                        phase="triage",
                        status="failed",
                        detail="temporary model error",
                        timestamp=time.time(),
                    )
                return PhaseResult(
                    phase="triage",
                    status="success",
                    detail="triage recovered",
                    timestamp=time.time(),
                )

            supervisor = Supervisor(
                queue=TaskQueue(),
                state_store=store,
                resilience_manager=resilience,
                phase_handlers={"triage": flaky_triage},
            )
            supervisor.create_task("retry me")
            task = supervisor.process_next()

            assert task is not None
            self.assertEqual("completed", task.status)
            self.assertGreaterEqual(attempts["triage"], 2)

    def test_resilience_fallback_enables_degraded_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.json")
            resilience = ResilienceManager(
                budget=TaskBudget(
                    max_wall_time_seconds=120.0,
                    max_phase_attempts=200,
                    max_tool_calls=20,
                ),
                config=ResilienceConfig(
                    max_retries_per_phase=0,
                    fallback_phases={"code"},
                    safe_mode_failure_threshold=5,
                ),
            )

            def failing_code(_: Task) -> PhaseResult:
                return PhaseResult(
                    phase="code",
                    status="failed",
                    detail="primary coder unavailable",
                    timestamp=time.time(),
                )

            supervisor = Supervisor(
                queue=TaskQueue(),
                state_store=store,
                resilience_manager=resilience,
                phase_handlers={"code": failing_code},
            )
            supervisor.create_task("fallback path")
            task = supervisor.process_next()

            assert task is not None
            self.assertEqual("completed", task.status)
            self.assertTrue(task.degraded_mode)
            self.assertTrue(any("fallback path used for code" in p.detail for p in task.phase_history))

    def test_resilience_safe_mode_blocks_after_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.json")
            resilience = ResilienceManager(
                budget=TaskBudget(
                    max_wall_time_seconds=120.0,
                    max_phase_attempts=200,
                    max_tool_calls=20,
                ),
                config=ResilienceConfig(
                    max_retries_per_phase=0,
                    fallback_phases=set(),
                    safe_mode_failure_threshold=1,
                ),
            )

            def failing_requirements(_: Task) -> PhaseResult:
                return PhaseResult(
                    phase="requirements",
                    status="failed",
                    detail="hard failure",
                    timestamp=time.time(),
                )

            supervisor = Supervisor(
                queue=TaskQueue(),
                state_store=store,
                resilience_manager=resilience,
                phase_handlers={"requirements": failing_requirements},
            )
            supervisor.create_task("safe mode")
            task = supervisor.process_next()

            assert task is not None
            self.assertEqual("blocked", task.status)
            self.assertTrue(task.safe_mode)
            self.assertIn("safe mode engaged", task.phase_history[-1].detail)

    def test_resilience_budget_blocks_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.json")
            resilience = ResilienceManager(
                budget=TaskBudget(
                    max_wall_time_seconds=120.0,
                    max_phase_attempts=2,
                    max_tool_calls=20,
                ),
                config=ResilienceConfig(
                    max_retries_per_phase=0,
                    fallback_phases=set(),
                    safe_mode_failure_threshold=5,
                ),
            )

            supervisor = Supervisor(
                queue=TaskQueue(),
                state_store=store,
                resilience_manager=resilience,
            )
            supervisor.create_task("budget block")
            task = supervisor.process_next()

            assert task is not None
            self.assertEqual("blocked", task.status)
            self.assertEqual("budget", task.phase_history[-1].phase)
            self.assertIn("budget exceeded: max_phase_attempts", task.phase_history[-1].detail)

    def test_audit_and_provenance_written_for_completed_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store = StateStore(tmp_path / "state.json")
            logger = AuditLogger(tmp_path / "audit.jsonl")
            bundler = ProvenanceBundler(tmp_path / "bundles")

            supervisor = Supervisor(
                queue=TaskQueue(),
                state_store=store,
                audit_logger=logger,
                provenance_bundler=bundler,
            )
            supervisor.create_task("audit complete")
            task = supervisor.process_next()

            assert task is not None
            self.assertEqual("completed", task.status)
            self.assertIsNotNone(task.provenance_bundle_path)
            assert task.provenance_bundle_path is not None
            self.assertTrue(Path(task.provenance_bundle_path).exists())
            self.assertTrue(logger.verify_chain())

            events = logger.read_events()
            event_types = [e["event_type"] for e in events]
            self.assertIn("task_created", event_types)
            self.assertIn("task_started", event_types)
            self.assertIn("task_completed", event_types)

    def test_replay_from_bundle_reconstructs_task_without_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store = StateStore(tmp_path / "state.json")
            logger = AuditLogger(tmp_path / "audit.jsonl")
            bundler = ProvenanceBundler(tmp_path / "bundles")

            supervisor = Supervisor(
                queue=TaskQueue(),
                state_store=store,
                audit_logger=logger,
                provenance_bundler=bundler,
            )
            original = supervisor.run_prompt("build provenance")
            assert original.provenance_bundle_path is not None

            replayed = supervisor.replay_from_bundle(original.provenance_bundle_path)
            self.assertEqual("replay", replayed.execution_mode)
            self.assertEqual("replayed", replayed.status)
            self.assertEqual(original.task_id, replayed.task_id)
            self.assertEqual(len(original.phase_history), len(replayed.phase_history))
            self.assertEqual("task_replayed", logger.read_events()[-1]["event_type"])

    def test_audit_and_provenance_written_for_blocked_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store = StateStore(tmp_path / "state.json")
            logger = AuditLogger(tmp_path / "audit.jsonl")
            bundler = ProvenanceBundler(tmp_path / "bundles")

            def failing_requirements(_: Task) -> PhaseResult:
                return PhaseResult(
                    phase="requirements",
                    status="failed",
                    detail="bad requirements",
                    timestamp=time.time(),
                )

            supervisor = Supervisor(
                queue=TaskQueue(),
                state_store=store,
                audit_logger=logger,
                provenance_bundler=bundler,
                phase_handlers={"requirements": failing_requirements},
            )
            supervisor.create_task("audit blocked")
            task = supervisor.process_next()

            assert task is not None
            self.assertEqual("blocked", task.status)
            self.assertIsNotNone(task.provenance_bundle_path)
            assert task.provenance_bundle_path is not None
            self.assertTrue(Path(task.provenance_bundle_path).exists())
            self.assertTrue(logger.verify_chain())
            self.assertIn("task_blocked", [e["event_type"] for e in logger.read_events()])


if __name__ == "__main__":
    unittest.main()
