"""Tests for parallel.py — mocked instances, no VICE required."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from c64_test_harness.parallel import run_parallel, ParallelTestResult, SingleTestResult
from c64_test_harness.backends.vice_manager import ViceInstanceManager


def _make_mock_manager():
    """Create a VICE-typed manager with mocked acquire/release."""
    mgr = MagicMock(spec=ViceInstanceManager)
    call_count = 0

    def fake_acquire():
        nonlocal call_count
        call_count += 1
        inst = MagicMock()
        inst.transport = MagicMock()
        inst.port = 19000 + call_count
        inst.pid = 4000 + call_count
        return inst

    mgr.acquire = MagicMock(side_effect=fake_acquire)
    mgr.release = MagicMock()
    return mgr


class _FakeBackendManager:
    """Minimal non-VICE manager satisfying the BackendManager Protocol.

    Used to exercise the generalized dispatch path: the test callable
    should receive the acquired instance itself (not ``.transport``).
    """

    def __init__(self):
        self.acquired: list[object] = []
        self.released: list[object] = []
        self._counter = 0

    def acquire(self):
        self._counter += 1
        inst = MagicMock()
        inst.transport = MagicMock(name=f"transport-{self._counter}")
        inst.pid = None  # hardware backends have no OS PID
        self.acquired.append(inst)
        return inst

    def release(self, instance):
        self.released.append(instance)

    def shutdown(self):
        pass


class TestRunParallel:
    def test_all_pass(self):
        mgr = _make_mock_manager()
        tests = [
            ("test_a", lambda t: (True, "ok")),
            ("test_b", lambda t: (True, "fine")),
        ]
        result = run_parallel(mgr, tests)
        assert len(result.results) == 2
        assert result.all_passed
        assert result.exit_code == 0
        assert mgr.acquire.call_count == 2
        assert mgr.release.call_count == 2

    def test_mixed_results(self):
        mgr = _make_mock_manager()
        tests = [
            ("pass", lambda t: (True, "ok")),
            ("fail", lambda t: (False, "bad")),
        ]
        result = run_parallel(mgr, tests)
        assert not result.all_passed
        assert result.exit_code == 1

    def test_exception_becomes_error(self):
        mgr = _make_mock_manager()

        def blow_up(t):
            raise ValueError("boom")

        tests = [("explode", blow_up)]
        result = run_parallel(mgr, tests)
        assert len(result.results) == 1
        r = result.results[0]
        assert not r.passed
        assert "ERROR" in r.message
        assert "ValueError" in r.message
        # release must still be called
        assert mgr.release.call_count == 1

    def test_release_called_per_test(self):
        mgr = _make_mock_manager()
        tests = [
            ("a", lambda t: (True, "ok")),
            ("b", lambda t: (True, "ok")),
            ("c", lambda t: (True, "ok")),
        ]
        run_parallel(mgr, tests)
        assert mgr.release.call_count == 3

    def test_print_summary(self, capsys):
        result = ParallelTestResult(
            results=[
                SingleTestResult("a", True, "ok", 1.0),
                SingleTestResult("b", False, "bad", 2.0),
            ],
            total_duration=3.0,
        )
        result.print_summary()
        output = capsys.readouterr().out
        assert "PASS" in output
        assert "FAIL" in output
        assert "1/2 passed" in output

    def test_vice_manager_unwraps_to_transport(self):
        """Legacy VICE callers must still receive ``.transport`` directly."""
        # Build a manager that lets us capture the produced instance so
        # we can assert identity on what the test fn received.
        acquired_instances: list[object] = []
        mgr = MagicMock(spec=ViceInstanceManager)

        def fake_acquire():
            inst = MagicMock()
            inst.transport = MagicMock(name="bin-vice-transport")
            inst.pid = 4242
            acquired_instances.append(inst)
            return inst

        mgr.acquire = MagicMock(side_effect=fake_acquire)
        mgr.release = MagicMock()

        received: list[object] = []

        def fn(t):
            received.append(t)
            return True, "ok"

        run_parallel(mgr, [("a", fn)])

        assert len(received) == 1
        # Must be the transport, NOT the full instance.
        assert received[0] is acquired_instances[0].transport
        assert received[0] is not acquired_instances[0]

    def test_vice_manager_records_pid(self):
        """``pid`` from the instance is propagated to ``SingleTestResult``."""
        mgr = _make_mock_manager()
        result = run_parallel(mgr, [("a", lambda t: (True, "ok"))])
        assert len(result.results) == 1
        # _make_mock_manager assigns pid=4001 on first acquire.
        assert result.results[0].pid == 4001


class TestRunParallelGeneralized:
    """Non-VICE managers: test callable receives the instance itself."""

    def test_non_vice_manager_passes_instance(self):
        mgr = _FakeBackendManager()
        received: list[object] = []

        def fn(inst):
            received.append(inst)
            return True, "ok"

        result = run_parallel(mgr, [("a", fn)])

        assert result.all_passed
        assert len(received) == 1
        # Test fn must have received the acquired instance, NOT
        # ``instance.transport``.
        assert received[0] is mgr.acquired[0]
        # The instance exposes ``.transport`` for downstream use.
        assert received[0].transport is mgr.acquired[0].transport
        # Release is still called.
        assert mgr.released == mgr.acquired

    def test_non_vice_manager_pid_none(self):
        """Hardware backends report ``pid=None`` on the result."""
        mgr = _FakeBackendManager()
        result = run_parallel(mgr, [("a", lambda inst: (True, "ok"))])
        assert result.results[0].pid is None

    def test_non_vice_manager_release_on_exception(self):
        mgr = _FakeBackendManager()

        def explode(inst):
            raise RuntimeError("boom")

        result = run_parallel(mgr, [("x", explode)])
        assert not result.all_passed
        assert "RuntimeError" in result.results[0].message
        # release() must still be called even when the test fn raises.
        assert mgr.released == mgr.acquired

    def test_non_vice_manager_parallel_dispatch(self):
        """Multiple tests fan out through the generalized path."""
        mgr = _FakeBackendManager()
        tests = [
            ("a", lambda inst: (True, "ok")),
            ("b", lambda inst: (True, "ok")),
            ("c", lambda inst: (False, "no")),
        ]
        result = run_parallel(mgr, tests)
        assert len(result.results) == 3
        assert sum(r.passed for r in result.results) == 2
        assert len(mgr.acquired) == 3
        assert len(mgr.released) == 3
