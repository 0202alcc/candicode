from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Set


@dataclass(frozen=True)
class FlakyPolicy:
    max_retries: int
    quarantine_after_failures: int
    require_owner: bool
    require_expiry: bool


@dataclass(frozen=True)
class VerificationResult:
    status: str
    checks: List[str]
    flaky_quarantined: List[str]
    retry_counts: Dict[str, int]
    failures: List[str]


class FlakyRegistry:
    def __init__(self) -> None:
        self._records: Dict[str, Dict[str, object]] = {}

    def record_failure(self, check_name: str) -> None:
        rec = self._records.setdefault(
            check_name,
            {"failures": 0, "owner": None, "expiry": None, "quarantined": False},
        )
        rec["failures"] = int(rec["failures"]) + 1

    def set_metadata(self, check_name: str, owner: Optional[str], expiry: Optional[str]) -> None:
        rec = self._records.setdefault(
            check_name,
            {"failures": 0, "owner": None, "expiry": None, "quarantined": False},
        )
        rec["owner"] = owner
        rec["expiry"] = expiry

    def failures(self, check_name: str) -> int:
        rec = self._records.get(check_name)
        if rec is None:
            return 0
        return int(rec["failures"])

    def quarantine(self, check_name: str) -> None:
        rec = self._records.setdefault(
            check_name,
            {"failures": 0, "owner": None, "expiry": None, "quarantined": False},
        )
        rec["quarantined"] = True

    def is_quarantined(self, check_name: str) -> bool:
        rec = self._records.get(check_name)
        if rec is None:
            return False
        return bool(rec["quarantined"])

    def has_required_metadata(self, check_name: str, policy: FlakyPolicy) -> bool:
        rec = self._records.get(check_name)
        if rec is None:
            return not policy.require_owner and not policy.require_expiry

        if policy.require_owner and not rec.get("owner"):
            return False
        if policy.require_expiry and not rec.get("expiry"):
            return False
        return True


class VerificationRunner:
    def __init__(
        self,
        checks: List[str],
        check_executor: Callable[[str], bool],
        flaky_policy: FlakyPolicy,
        flaky_registry: Optional[FlakyRegistry] = None,
    ) -> None:
        self.checks = list(checks)
        self.check_executor = check_executor
        self.flaky_policy = flaky_policy
        self.flaky_registry = flaky_registry or FlakyRegistry()

    def run(self, checks: Optional[List[str]] = None) -> VerificationResult:
        selected = checks if checks is not None else self.checks
        ordered_checks = sorted(selected)
        failures: List[str] = []
        quarantined: List[str] = []
        retry_counts: Dict[str, int] = {}

        for check in ordered_checks:
            if self.flaky_registry.is_quarantined(check):
                quarantined.append(check)
                continue

            passed = self.check_executor(check)
            retries = 0
            while not passed and retries < self.flaky_policy.max_retries:
                retries += 1
                passed = self.check_executor(check)
            retry_counts[check] = retries

            if passed:
                continue

            self.flaky_registry.record_failure(check)
            failure_count = self.flaky_registry.failures(check)

            if failure_count >= self.flaky_policy.quarantine_after_failures:
                if self.flaky_registry.has_required_metadata(check, self.flaky_policy):
                    self.flaky_registry.quarantine(check)
                    quarantined.append(check)
                    continue

            failures.append(check)

        status = "pass" if not failures else "fail"
        return VerificationResult(
            status=status,
            checks=ordered_checks,
            flaky_quarantined=sorted(set(quarantined)),
            retry_counts=retry_counts,
            failures=failures,
        )
