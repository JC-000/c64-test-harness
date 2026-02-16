"""Parallel test execution across multiple VICE instances.

Provides ``run_parallel()`` to distribute tests across a pool of VICE
instances managed by a ``ViceInstanceManager``, and ``ParallelTestResult``
to collect and summarise the outcomes.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable

from .backends.vice import ViceTransport
from .backends.vice_manager import ViceInstanceManager


@dataclass
class SingleTestResult:
    """Outcome of one test function."""

    name: str
    passed: bool
    message: str
    duration: float = 0.0


@dataclass
class ParallelTestResult:
    """Aggregated results from a parallel test run."""

    results: list[SingleTestResult] = field(default_factory=list)
    total_duration: float = 0.0

    @property
    def all_passed(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def exit_code(self) -> int:
        return 0 if self.all_passed else 1

    def print_summary(self) -> None:
        """Print a human-readable results table."""
        print("\n" + "=" * 60)
        print("PARALLEL TEST RESULTS")
        print("=" * 60)
        for r in self.results:
            status = "PASS" if r.passed else "FAIL"
            print(f"  [{status}] {r.name} ({r.duration:.1f}s): {r.message}")
        passed = sum(1 for r in self.results if r.passed)
        total = len(self.results)
        print("-" * 60)
        print(f"  {passed}/{total} passed in {self.total_duration:.1f}s")
        if self.all_passed:
            print(f"  [+] ALL {total} TESTS PASSED")
        else:
            print(f"  [-] {total - passed} TEST(S) FAILED")
        print("=" * 60)


def run_parallel(
    manager: ViceInstanceManager,
    tests: list[tuple[str, Callable[[ViceTransport], tuple[bool, str]]]],
    max_workers: int | None = None,
) -> ParallelTestResult:
    """Run *tests* in parallel, each on its own VICE instance.

    Parameters
    ----------
    manager:
        Instance manager to acquire/release VICE instances from.
    tests:
        List of ``(name, test_fn)`` tuples. Each *test_fn* receives a
        ``ViceTransport`` and must return ``(passed, message)``.
    max_workers:
        Maximum concurrent tests. Defaults to ``len(tests)``.

    Returns
    -------
    ParallelTestResult
        Aggregated outcomes.
    """
    if max_workers is None:
        max_workers = len(tests)

    result = ParallelTestResult()
    wall_start = time.monotonic()

    def _run_one(name: str, fn: Callable[[ViceTransport], tuple[bool, str]]) -> SingleTestResult:
        t0 = time.monotonic()
        instance = manager.acquire()
        try:
            passed, message = fn(instance.transport)
            return SingleTestResult(
                name=name, passed=passed, message=message,
                duration=time.monotonic() - t0,
            )
        except Exception as e:
            return SingleTestResult(
                name=name, passed=False,
                message=f"ERROR: {type(e).__name__}: {e}",
                duration=time.monotonic() - t0,
            )
        finally:
            manager.release(instance)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_run_one, name, fn): name
            for name, fn in tests
        }
        for future in as_completed(futures):
            result.results.append(future.result())

    result.total_duration = time.monotonic() - wall_start
    return result
