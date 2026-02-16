"""Tests for parallel.py — mocked instances, no VICE required."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from c64_test_harness.parallel import run_parallel, ParallelTestResult, SingleTestResult
from c64_test_harness.backends.vice_manager import ViceInstanceManager


def _make_mock_manager():
    """Create a manager with mocked acquire/release."""
    mgr = MagicMock(spec=ViceInstanceManager)
    call_count = 0

    def fake_acquire():
        nonlocal call_count
        call_count += 1
        inst = MagicMock()
        inst.transport = MagicMock()
        inst.port = 19000 + call_count
        return inst

    mgr.acquire = MagicMock(side_effect=fake_acquire)
    mgr.release = MagicMock()
    return mgr


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
