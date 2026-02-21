from __future__ import annotations

import time
from typing import Callable, Dict, List

from agents.backend_adapter_v1 import BackendStructuredModelClient, ValidatedBackendClientV1
from agents.model_client import HostedModelClient
from orchestrator.supervisor import PhaseResult, Task


class BasicAgentSuite:
    """
    Minimal agent set backed by a single hosted model client.
    """

    AGENT_PHASES = {
        "intent": "intent_agent",
        "triage": "triage_agent",
        "requirements": "requirements_agent",
        "plan": "planner_agent",
        "code": "coder_agent",
        "test": "test_agent",
        "review": "reviewer_agent",
        "docs": "docs_agent",
    }

    def __init__(
        self,
        model_client: HostedModelClient,
        strict_code_file_edits: bool = False,
    ) -> None:
        self.model_client = model_client
        self.strict_code_file_edits = strict_code_file_edits

    @classmethod
    def from_validated_backend_v1(
        cls,
        validated_client: ValidatedBackendClientV1,
        default_model: str,
        strict_code_file_edits: bool = False,
    ) -> "BasicAgentSuite":
        return cls(
            BackendStructuredModelClient(
                validated_client=validated_client,
                default_model=default_model,
            ),
            strict_code_file_edits=strict_code_file_edits,
        )

    def build_phase_handlers(self) -> Dict[str, Callable[[Task], PhaseResult]]:
        handlers: Dict[str, Callable[[Task], PhaseResult]] = {}
        for phase, agent_name in self.AGENT_PHASES.items():
            handlers[phase] = self._make_handler(phase, agent_name)
        return handlers

    def _make_handler(self, phase: str, agent_name: str) -> Callable[[Task], PhaseResult]:
        def _handler(task: Task) -> PhaseResult:
            try:
                prompt = task.prompt
                if phase != "intent":
                    prompt = self._resolved_prompt(task, phase)
                payload = self.model_client.generate_structured(
                    agent_name=agent_name,
                    prompt=prompt,
                    context={
                        "task_id": task.task_id,
                        "priority": task.priority,
                        "work_branch": task.work_branch,
                        "agent_outputs": task.agent_outputs,
                    },
                )
            except Exception as exc:
                return PhaseResult(
                    phase=phase,
                    status="failed",
                    detail=f"{agent_name} error: {exc}",
                    timestamp=time.time(),
                )

            if not isinstance(payload, dict):
                return PhaseResult(
                    phase=phase,
                    status="failed",
                    detail=f"{agent_name} returned non-object payload",
                    timestamp=time.time(),
                )
            if phase == "code" and self.strict_code_file_edits:
                validation = self._validate_code_payload(task, payload)
                if validation is not None:
                    return PhaseResult(
                        phase=phase,
                        status="failed",
                        detail=f"{agent_name} payload invalid: {validation}",
                        timestamp=time.time(),
                    )

            task.agent_outputs[phase] = payload
            return PhaseResult(
                phase=phase,
                status="success",
                detail=f"{agent_name} produced payload",
                timestamp=time.time(),
            )

        return _handler

    @staticmethod
    def _resolved_prompt(task: Task, phase: str) -> str:
        intent_payload = task.agent_outputs.get("intent", {})
        base_prompt = task.prompt
        if not isinstance(intent_payload, dict):
            return base_prompt
        value = intent_payload.get("rewritten_prompt")
        if isinstance(value, str) and value.strip():
            base_prompt = value.strip()

        if phase == "code":
            raw_targets = intent_payload.get("target_files")
            targets = [item.strip() for item in raw_targets if isinstance(item, str) and item.strip()] if isinstance(raw_targets, list) else []
            if targets:
                return (
                    f"{base_prompt}\n\n"
                    f"Use exact target files only: {', '.join(targets)}.\n"
                    "Return full file_edits for each touched target file."
                )
        return base_prompt

    @staticmethod
    def _validate_code_payload(task: Task, payload: Dict) -> str | None:
        files_touched = payload.get("files_touched")
        if not isinstance(files_touched, list):
            return "files_touched must be an array"
        touched = [item.strip() for item in files_touched if isinstance(item, str) and item.strip()]
        intent_payload = task.agent_outputs.get("intent", {})
        intent_targets = []
        if isinstance(intent_payload, dict):
            raw_targets = intent_payload.get("target_files")
            if isinstance(raw_targets, list):
                intent_targets = [item.strip() for item in raw_targets if isinstance(item, str) and item.strip()]

        if not touched:
            if intent_targets:
                return "files_touched must include at least one intent target file"
            return None

        raw_edits = payload.get("file_edits")
        if not isinstance(raw_edits, list) or not raw_edits:
            return "files_touched is non-empty but file_edits is missing"

        edited_paths: List[str] = []
        for edit in raw_edits:
            if not isinstance(edit, dict):
                continue
            path = edit.get("path")
            content = edit.get("content")
            if isinstance(path, str) and path.strip() and isinstance(content, str):
                edited_paths.append(path.strip())

        if not edited_paths:
            return "file_edits must include at least one valid {path, content} object"

        missing = sorted(set(touched) - set(edited_paths))
        if missing:
            return f"file_edits missing touched file(s): {', '.join(missing)}"
        if intent_targets:
            unexpected = sorted(set(touched) - set(intent_targets))
            if unexpected:
                return (
                    "files_touched contains paths outside intent target_files: "
                    + ", ".join(unexpected)
                )
        return None
