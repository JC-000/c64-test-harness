"""Live read-only integration tests for Ultimate64Transport.

Gated by the ``U64_HOST`` env var — e.g.:

    U64_HOST=192.168.1.81 python3 -m pytest tests/test_ultimate64_transport_live.py -v

Only read-only operations — no write_memory, no inject_keys, no resets.
"""
from __future__ import annotations

import os

import pytest

from c64_test_harness.backends.device_lock import DeviceLock
from c64_test_harness.backends.ultimate64 import Ultimate64Transport
from c64_test_harness.transport import C64Transport

_HOST = os.environ.get("U64_HOST")
_PW = os.environ.get("U64_PASSWORD")

pytestmark = pytest.mark.skipif(
    not _HOST,
    reason="U64_HOST not set — live Ultimate device tests disabled",
)


@pytest.fixture(scope="module")
def transport() -> Ultimate64Transport:
    lock = DeviceLock(_HOST)
    if not lock.acquire(timeout=120.0):
        pytest.skip(f"Could not acquire device lock for {_HOST}")
    t = Ultimate64Transport(host=_HOST, password=_PW, timeout=8.0)
    yield t
    t.close()
    lock.release()


def test_protocol_conformance(transport: Ultimate64Transport) -> None:
    assert isinstance(transport, C64Transport)


def test_dimensions(transport: Ultimate64Transport) -> None:
    assert transport.screen_cols == 40
    assert transport.screen_rows == 25


def test_read_memory_screen_area(transport: Ultimate64Transport) -> None:
    data = transport.read_memory(0x0400, 1000)
    assert isinstance(data, bytes)
    assert len(data) == 1000


def test_read_memory_small_range(transport: Ultimate64Transport) -> None:
    data = transport.read_memory(0xA000, 16)  # BASIC ROM area
    assert isinstance(data, bytes)
    assert len(data) == 16


def test_read_screen_codes(transport: Ultimate64Transport) -> None:
    codes = transport.read_screen_codes()
    assert isinstance(codes, list)
    assert len(codes) == 1000
    assert all(isinstance(c, int) and 0 <= c <= 255 for c in codes)


def test_read_registers_not_supported(transport: Ultimate64Transport) -> None:
    with pytest.raises(NotImplementedError):
        transport.read_registers()
