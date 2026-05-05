"""Blind-agent liveness probe and UnifiedManager tests for Ultimate 64.

Requires a live U64 device — gated by the U64_HOST environment variable.
"""

import os
import pytest

from c64_test_harness.backends.ultimate64_probe import (
    ProbeResult,
    is_u64_reachable,
    probe_u64,
)
from c64_test_harness.backends.unified_manager import UnifiedManager

U64_HOST = os.environ.get("U64_HOST", "")
pytestmark = pytest.mark.skipif(not U64_HOST, reason="U64_HOST not set")


# ---------------------------------------------------------------------------
# Probe tests
# ---------------------------------------------------------------------------

class TestProbe:
    """Probe a real U64 device and an unreachable TEST-NET address."""

    def test_probe_reachable(self):
        result = probe_u64(U64_HOST)
        assert result.reachable is True
        assert result.ping_ok is True
        assert result.port_ok is True

    def test_probe_unreachable(self):
        result = probe_u64("192.0.2.1", ping_timeout=1.0)
        assert result.reachable is False

    def test_is_u64_reachable(self):
        assert is_u64_reachable(U64_HOST) is True

    def test_summary_non_empty(self):
        result = probe_u64(U64_HOST)
        assert isinstance(result.summary, str)
        assert len(result.summary) > 0


# ---------------------------------------------------------------------------
# UnifiedManager acquire/release tests
# ---------------------------------------------------------------------------

class TestUnifiedManager:
    """Acquire and release cycle via UnifiedManager."""

    def test_acquire_release_cycle(self):
        mgr = UnifiedManager(backend="u64", u64_hosts=[U64_HOST])
        try:
            target = mgr.acquire()
            assert target.backend == "u64"
            assert target.transport is not None
            mgr.release(target)
        finally:
            mgr.shutdown()

    def test_memory_read(self):
        mgr = UnifiedManager(backend="u64", u64_hosts=[U64_HOST])
        try:
            target = mgr.acquire()
            data = target.transport.read_memory(0xA000, 8)
            assert isinstance(data, bytes)
            assert len(data) == 8
            mgr.release(target)
        finally:
            mgr.shutdown()
