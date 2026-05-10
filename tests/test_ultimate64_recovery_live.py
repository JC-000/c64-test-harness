"""Live recovery tests against a real Ultimate 64.

Double-gated: requires both ``U64_HOST`` and ``U64_ALLOW_MUTATE`` to be
set. ``recover()`` issues ``reset()`` (and possibly ``reboot()``) on the
device, so this is destructive in the same sense as the turbo round-trip
in ``test_ultimate64_helpers_live.py``.

Device used in development: 192.168.1.81 (Ultimate 64 Elite, fw 3.14).
"""
from __future__ import annotations

import os

import pytest

from c64_test_harness.backends.device_lock import DeviceLock
from c64_test_harness.backends.ultimate64_client import Ultimate64Client
from c64_test_harness.backends.ultimate64_helpers import recover
from c64_test_harness.backends.ultimate64_probe import is_u64_reachable

_HOST = os.environ.get("U64_HOST")
_ALLOW_MUTATE = os.environ.get("U64_ALLOW_MUTATE")

pytestmark = [
    pytest.mark.skipif(
        not _HOST, reason="U64_HOST not set — skipping live U64 tests"
    ),
    pytest.mark.skipif(
        not _ALLOW_MUTATE,
        reason="U64_ALLOW_MUTATE not set — recover() is destructive",
    ),
]


@pytest.fixture(scope="module")
def client() -> Ultimate64Client:
    """Stateless HTTP client for the live device, with device lock."""
    password = os.environ.get("U64_PASSWORD")
    lock = DeviceLock(_HOST or "")
    if not lock.acquire(timeout=120.0):
        pytest.skip(f"Could not acquire device lock for {_HOST}")
    yield Ultimate64Client(host=_HOST or "", password=password, timeout=10.0)
    lock.release()


def test_recover_against_live_device(client: Ultimate64Client) -> None:
    """recover() on a healthy device should return "reset" and leave it reachable."""
    action = recover(client)
    assert action in ("reset", "reboot")
    assert is_u64_reachable(client.host, port=client.port, password=client.password)
