"""Parallel test execution across multiple backend instances.

Provides ``run_parallel()`` to distribute tests across a pool of test
instances managed by any ``BackendManager`` (VICE, Ultimate 64, or the
``UnifiedManager`` dispatch wrapper), and ``ParallelTestResult`` to
collect and summarise the outcomes.

Backwards compatibility
-----------------------
Historically ``run_parallel`` accepted only :class:`ViceInstanceManager`
and passed a ``BinaryViceTransport`` to each test function.  The
function now accepts any object satisfying the
:class:`BackendManager` Protocol (``acquire``/``release``/``shutdown``).

To keep existing VICE callers working without code changes, when the
supplied manager is a :class:`ViceInstanceManager` the test callable
still receives the ``BinaryViceTransport`` (i.e. ``instance.transport``).
For all other managers — :class:`Ultimate64InstanceManager`,
:class:`UnifiedManager`, custom backends — the test callable receives
the acquired instance itself (e.g. :class:`TestTarget` for
``UnifiedManager``, :class:`Ultimate64Instance` for the U64 manager).
Both ``TestTarget`` and ``Ultimate64Instance`` expose ``.transport`` so
the migration is one short line per test body.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable

from .backends.vice_manager import ViceInstanceManager


@dataclass
class SingleTestResult:
    """Outcome of one test function.

    ``pid`` is the VICE OS PID when a VICE-backed instance ran the test,
    and ``None`` for hardware backends (Ultimate 64 has no OS process).
    """

    name: str
    passed: bool
    message: str
    duration: float = 0.0
    pid: int | None = None


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
    manager: Any,
    tests: list[tuple[str, Callable[[Any], tuple[bool, str]]]],
    max_workers: int | None = None,
) -> ParallelTestResult:
    """Run *tests* in parallel, each on its own backend instance.

    Parameters
    ----------
    manager:
        Any object satisfying the
        :class:`c64_test_harness.backends.unified_manager.BackendManager`
        Protocol — i.e. exposes ``acquire()``, ``release(instance)``,
        and ``shutdown()``.  In practice this is one of
        :class:`ViceInstanceManager`,
        :class:`Ultimate64InstanceManager`, or
        :class:`UnifiedManager`.
    tests:
        List of ``(name, test_fn)`` tuples.  Each *test_fn* receives the
        argument described below and must return ``(passed, message)``.

        - When *manager* is a :class:`ViceInstanceManager`, *test_fn*
          receives a ``BinaryViceTransport`` (backwards-compatible
          behaviour).
        - Otherwise *test_fn* receives the acquired instance itself
          (e.g. ``TestTarget`` from ``UnifiedManager``, or
          ``Ultimate64Instance`` from the U64 manager).  Both expose
          ``.transport``.
    max_workers:
        Maximum concurrent tests. Defaults to ``len(tests)``.

    Returns
    -------
    ParallelTestResult
        Aggregated outcomes.
    """
    if max_workers is None:
        max_workers = len(tests)

    # Backwards-compat: legacy VICE callers expect their test functions
    # to receive a ``BinaryViceTransport`` directly.  Anything else gets
    # the acquired instance (which exposes ``.transport`` for both U64
    # and the unified ``TestTarget``).
    unwrap_to_transport = isinstance(manager, ViceInstanceManager)

    result = ParallelTestResult()
    wall_start = time.monotonic()

    def _run_one(
        name: str,
        fn: Callable[[Any], tuple[bool, str]],
    ) -> SingleTestResult:
        t0 = time.monotonic()
        instance = manager.acquire()
        # ``pid`` is optional on the Protocol; default to None if absent.
        instance_pid = getattr(instance, "pid", None)
        arg: Any = instance.transport if unwrap_to_transport else instance
        try:
            passed, message = fn(arg)
            return SingleTestResult(
                name=name, passed=passed, message=message,
                duration=time.monotonic() - t0,
                pid=instance_pid,
            )
        except Exception as e:
            return SingleTestResult(
                name=name, passed=False,
                message=f"ERROR: {type(e).__name__}: {e}",
                duration=time.monotonic() - t0,
                pid=instance_pid,
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
