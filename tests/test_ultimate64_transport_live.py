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


def test_read_registers_removed_from_protocol(transport: Ultimate64Transport) -> None:
    """``read_registers`` is not part of ``C64Transport`` — VICE-only.

    The Ultimate64 transport must not advertise the attribute at all
    (so that ``hasattr`` checks in cross-backend helpers can dispatch
    cleanly).
    """
    assert not hasattr(transport, "read_registers")


def test_read_palette(transport: Ultimate64Transport) -> None:
    """``read_palette`` returns the canonical 16-entry VIC palette."""
    palette = transport.read_palette()
    assert len(palette) == 16
    assert palette[0] == (0x00, 0x00, 0x00)
    assert palette[1] == (0xFF, 0xFF, 0xFF)


def test_read_framebuffer_returns_one_frame(transport: Ultimate64Transport) -> None:
    """Capturing one frame should produce a dict matching the VICE shape.

    Requires the device to be able to reach the host on UDP
    ``DEFAULT_VIDEO_PORT`` (11000).  Skips with a clear message if the
    stream cannot be received (firewall, NAT, etc.).
    """
    from c64_test_harness.transport import TransportError

    try:
        fb = transport.read_framebuffer(timeout=3.0)
    except TransportError as exc:
        pytest.skip(f"U64 video stream not reachable from this host: {exc}")

    assert set(fb.keys()) == {"debug_rect", "inner_rect", "bpp", "palette", "bytes"}
    dx, dy, dw, dh = fb["debug_rect"]
    assert dx == 0 and dy == 0
    assert dw > 0 and dh > 0
    ix, iy, iw, ih = fb["inner_rect"]
    assert (iw, ih) == (dw, dh)  # U64 stream has no debug border
    assert fb["bpp"] == 8
    assert isinstance(fb["bytes"], bytes)
    # 1 byte per pixel after unpacking.
    assert len(fb["bytes"]) == dw * dh
