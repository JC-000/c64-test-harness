"""Test runner framework — TestScenario, TestResult, TestRunner.

Provides a lightweight test framework for C64 integration tests with
error recovery between scenarios.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable


class TestStatus(Enum):
    """Outcome of a test scenario."""

    PASS = "PASS"
    FAIL = "FAIL"
    ERROR = "ERROR"
    SKIP = "SKIP"


@dataclass
class TestResult:
    """Result of running a single test scenario."""

    name: str
    status: TestStatus
    message: str = ""
    duration: float = 0.0
    screen_dump: str = ""


@dataclass
class TestScenario:
    """A named test with an optional recovery function."""

    name: str
    run: Callable[[], tuple[bool, str]]
    recovery: Callable[[], bool] | None = None


class TestRunner:
    """Runs test scenarios sequentially with error recovery.

    Usage::

        runner = TestRunner()
        runner.add_scenario("Full CSR", test_full_csr, recover_to_menu)
        runner.add_scenario("CN only", test_cn_only, recover_to_menu)
        results = runner.run_all()
        runner.print_summary()
        sys.exit(runner.exit_code)
    """

    def __init__(self) -> None:
        self._scenarios: list[TestScenario] = []
        self._results: list[TestResult] = []

    def add_scenario(
        self,
        name: str,
        run_fn: Callable[[], tuple[bool, str]],
        recovery_fn: Callable[[], bool] | None = None,
    ) -> None:
        """Register a test scenario."""
        self._scenarios.append(TestScenario(name=name, run=run_fn, recovery=recovery_fn))

    def run_all(self) -> list[TestResult]:
        """Run all registered scenarios sequentially.

        Catches exceptions, captures timing, and invokes recovery functions
        on failure/error before proceeding to the next scenario.
        """
        self._results = []
        for scenario in self._scenarios:
            start = time.monotonic()
            try:
                ok, msg = scenario.run()
                duration = time.monotonic() - start
                status = TestStatus.PASS if ok else TestStatus.FAIL
                result = TestResult(
                    name=scenario.name,
                    status=status,
                    message=msg,
                    duration=duration,
                )
                self._results.append(result)
                if not ok and scenario.recovery:
                    scenario.recovery()
            except Exception as e:
                duration = time.monotonic() - start
                result = TestResult(
                    name=scenario.name,
                    status=TestStatus.ERROR,
                    message=f"{type(e).__name__}: {e}",
                    duration=duration,
                )
                self._results.append(result)
                if scenario.recovery:
                    scenario.recovery()
        return self._results

    @property
    def results(self) -> list[TestResult]:
        return list(self._results)

    @property
    def all_passed(self) -> bool:
        return all(r.status == TestStatus.PASS for r in self._results)

    @property
    def exit_code(self) -> int:
        return 0 if self.all_passed else 1

    def print_summary(self) -> None:
        """Print a formatted pass/fail results table."""
        width = 60
        print("\n" + "=" * width)
        print("RESULTS")
        print("=" * width)
        passed = 0
        for r in self._results:
            icon = "+" if r.status == TestStatus.PASS else "-"
            print(f"  [{icon}] {r.name}: {r.status.value}")
            if r.status == TestStatus.PASS:
                passed += 1
        total = len(self._results)
        print(f"\n  {passed}/{total} passed")
        print("=" * width)
