"""Live probe tests against a real Ultimate 64 device.

Gated by the ``U64_HOST`` environment variable.  Skipped when unset.

Run::

    U64_HOST=192.168.1.81 python3 -m pytest tests/test_ultimate64_probe_live.py -v
"""

from __future__ import annotations

import os

import pytest

from c64_test_harness.backends.ultimate64_probe import (
    is_u64_reachable,
    probe_u64,
)

_HOST = os.environ.get("U64_HOST")
_PORT = int(os.environ.get("U64_PORT", "80"))
_PASSWORD = os.environ.get("U64_PASSWORD")

pytestmark = pytest.mark.skipif(
    _HOST is None, reason="U64_HOST not set — skipping live probe tests"
)


def test_probe_real_device_all_pass():
    """Full probe against a live U64 — all checks should pass."""
    assert _HOST is not None  # for type checker
    result = probe_u64(_HOST, port=_PORT, password=_PASSWORD)
    assert result.reachable is True, f"probe failed: {result.summary}"
    assert result.ping_ok is True
    assert result.port_ok is True
    assert result.api_ok is True
    assert result.latency_ms is not None
    assert result.error is None


def test_probe_bad_host_unreachable():
    """Probe a TEST-NET address that should be unreachable."""
    # 192.0.2.1 is from RFC 5737 TEST-NET-1 — not routable.
    result = probe_u64("192.0.2.1", ping_timeout=2.0, tcp_timeout=2.0)
    assert result.reachable is False
    assert result.error is not None


def test_is_u64_reachable_true():
    """Quick boolean check returns True for the live device."""
    assert _HOST is not None
    assert is_u64_reachable(_HOST, port=_PORT, password=_PASSWORD) is True
