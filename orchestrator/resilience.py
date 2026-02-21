from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Set


@dataclass(frozen=True)
class TaskBudget:
    max_wall_time_seconds: float
    max_phase_attempts: int
    max_tool_calls: int


@dataclass(frozen=True)
class ResilienceConfig:
    max_retries_per_phase: int
    fallback_phases: Set[str]
    safe_mode_failure_threshold: int


@dataclass
class ResilienceState:
    phase_attempts: Dict[str, int] = field(default_factory=dict)
    total_phase_attempts: int = 0
    total_failures: int = 0


class ResilienceManager:
    def __init__(self, budget: TaskBudget, config: ResilienceConfig) -> None:
        self.budget = budget
        self.config = config
        self.states: Dict[str, ResilienceState] = {}

    def _state(self, task_id: str) -> ResilienceState:
        return self.states.setdefault(task_id, ResilienceState())

    def record_phase_attempt(self, task_id: str, phase: str) -> int:
        state = self._state(task_id)
        state.total_phase_attempts += 1
        state.phase_attempts[phase] = state.phase_attempts.get(phase, 0) + 1
        return state.phase_attempts[phase]

    def record_failure(self, task_id: str) -> int:
        state = self._state(task_id)
        state.total_failures += 1
        return state.total_failures

    def retry_allowed(self, task_id: str, phase: str) -> bool:
        state = self._state(task_id)
        attempts = state.phase_attempts.get(phase, 0)
        return attempts <= self.config.max_retries_per_phase

    def fallback_allowed(self, phase: str, degraded_mode: bool) -> bool:
        return (phase in self.config.fallback_phases) and (not degraded_mode)

    def should_enter_safe_mode(self, task_id: str) -> bool:
        state = self._state(task_id)
        return state.total_failures >= self.config.safe_mode_failure_threshold

    def budget_exceeded(
        self,
        task_id: str,
        elapsed_seconds: float,
        tool_calls: int,
    ) -> List[str]:
        state = self._state(task_id)
        failures: List[str] = []
        if elapsed_seconds > self.budget.max_wall_time_seconds:
            failures.append("max_wall_time_seconds")
        if state.total_phase_attempts > self.budget.max_phase_attempts:
            failures.append("max_phase_attempts")
        if tool_calls > self.budget.max_tool_calls:
            failures.append("max_tool_calls")
        return failures
