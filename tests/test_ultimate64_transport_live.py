"""Live integration tests for Ultimate64Transport.

Gated by the ``U64_HOST`` env var — e.g.:

    U64_HOST=192.168.1.81 python3 -m pytest tests/test_ultimate64_transport_live.py -v

Most tests are read-only. The ``TestSetSpeed`` and ``TestResetScopes``
classes added for PR #122 coverage exercise the protocol's
``set_speed`` / ``get_speed`` / ``reset(scope=...)`` surface on real
hardware; every test in those classes restores the device to 1 MHz in
a ``finally`` block so downstream CIA-timer measurements still see the
expected native clock.

The ``reset(scope='machine')`` case triggers a full FPGA reboot (~8 s
to recover) and is therefore gated by an additional ``U64_DESTRUCTIVE=1``
env var. When that variable is unset the test skips cleanly so
module-scoped state is never disturbed by default.
"""
from __future__ import annotations

import os
import time

import pytest

from c64_test_harness.backends.device_lock import DeviceLock
from c64_test_harness.backends.ultimate64 import Ultimate64Transport
from c64_test_harness.backends.ultimate64_helpers import get_turbo_mhz
from c64_test_harness.backends.ultimate64_probe import is_u64_reachable
from c64_test_harness.transport import C64Transport

_HOST = os.environ.get("U64_HOST")
_PW = os.environ.get("U64_PASSWORD")
_DESTRUCTIVE = os.environ.get("U64_DESTRUCTIVE") == "1"

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
    try:
        t.set_speed(1)  # leave device at native clock for downstream tests
    except Exception:
        pass
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


# ---------------------------------------------------------------------------
# PR #122 coverage — set_speed / get_speed against real hardware
# ---------------------------------------------------------------------------


class TestSetSpeed:
    """``set_speed`` / ``get_speed`` round-trip through real U64 turbo state.

    Every test restores 1 MHz in ``finally`` so the module-scoped
    transport leaves the device at the native clock for downstream
    tests (e.g. CIA-timer measurements).
    """

    def test_set_speed_1_disables_turbo(
        self, transport: Ultimate64Transport
    ) -> None:
        try:
            transport.set_speed(1)
            assert transport.get_speed() == 1
            assert get_turbo_mhz(transport.client) is None
        finally:
            transport.set_speed(1)

    def test_set_speed_none_selects_max(
        self, transport: Ultimate64Transport
    ) -> None:
        """``set_speed(None)`` enables turbo at the device max (48 MHz).

        Note the asymmetry with VICE (where ``set_speed(None)`` ⇒ warp on,
        and ``get_speed()`` returns ``None``): on U64, ``set_speed(None)``
        maps to ``set_turbo_mhz(client, 48)``, which sets Turbo Control to
        ``"Manual"`` at 48 MHz, so ``get_speed()`` reads back ``48`` (not
        ``None``). ``get_speed()`` only returns ``None`` when turbo is on
        but the CPU-Speed enum is unrecognised — a state ``set_speed``
        does not produce.
        """
        try:
            transport.set_speed(None)
            assert transport.get_speed() == 48
            assert get_turbo_mhz(transport.client) == 48
        finally:
            transport.set_speed(1)

    def test_set_speed_4(self, transport: Ultimate64Transport) -> None:
        try:
            transport.set_speed(4)
            assert transport.get_speed() == 4
            assert get_turbo_mhz(transport.client) == 4
        finally:
            transport.set_speed(1)

    def test_set_speed_round_trip(
        self, transport: Ultimate64Transport
    ) -> None:
        """Capture, change to two other values, restore — reads agree with sets."""
        try:
            original = transport.get_speed()
            transport.set_speed(2)
            assert transport.get_speed() == 2
            assert get_turbo_mhz(transport.client) == 2

            transport.set_speed(8)
            assert transport.get_speed() == 8
            assert get_turbo_mhz(transport.client) == 8

            transport.set_speed(original)
            assert transport.get_speed() == original
        finally:
            transport.set_speed(1)

    def test_set_speed_unsupported_raises(
        self, transport: Ultimate64Transport
    ) -> None:
        """Speeds not in the device CPU-Speed enum raise ``ValueError``.

        ``set_turbo_mhz`` validates locally via ``cpu_speed_enum`` so no
        request hits the wire and device state is untouched.
        """
        try:
            with pytest.raises(ValueError):
                transport.set_speed(7)  # 7 MHz is not a supported step
            assert transport.get_speed() == 1
        finally:
            transport.set_speed(1)


# ---------------------------------------------------------------------------
# PR #122 coverage — reset(scope=..., drive=...) against real hardware
# ---------------------------------------------------------------------------


class TestResetScopes:
    """``reset(scope=...)`` dispatches to the right REST endpoint.

    ``scope='cpu'`` and ``scope='drive'`` are exercised by default.
    ``scope='machine'`` triggers a full FPGA reboot and is gated by
    ``U64_DESTRUCTIVE=1`` so default test runs are not disrupted.
    """

    def test_reset_scope_cpu_keeps_device_responsive(
        self, transport: Ultimate64Transport
    ) -> None:
        """Soft 6510 reset must leave the device reachable & memory I/O alive."""
        try:
            transport.reset(scope="cpu")
            time.sleep(1.0)
            assert is_u64_reachable(_HOST, password=_PW)
            data = transport.read_memory(0xA000, 16)
            assert isinstance(data, bytes) and len(data) == 16
        finally:
            transport.set_speed(1)

    def test_reset_scope_default_is_cpu(
        self, transport: Ultimate64Transport
    ) -> None:
        """Calling ``reset()`` with no kwargs must behave like ``scope='cpu'``."""
        try:
            transport.reset()
            time.sleep(1.0)
            assert is_u64_reachable(_HOST, password=_PW)
        finally:
            transport.set_speed(1)

    def test_reset_scope_drive_a(
        self, transport: Ultimate64Transport
    ) -> None:
        """``reset(scope='drive', drive='a')`` must dispatch cleanly."""
        try:
            transport.reset(scope="drive", drive="a")
            assert is_u64_reachable(_HOST, password=_PW)
        finally:
            transport.set_speed(1)

    def test_reset_scope_drive_missing_drive_kwarg(
        self, transport: Ultimate64Transport
    ) -> None:
        """``scope='drive'`` without a ``drive=`` kwarg raises ``ValueError``.

        Verified at the protocol layer; no request hits the device.
        """
        with pytest.raises(ValueError, match="drive"):
            transport.reset(scope="drive")

    def test_reset_scope_unknown_raises(
        self, transport: Ultimate64Transport
    ) -> None:
        """Unknown scopes are rejected client-side — device untouched."""
        with pytest.raises(ValueError):
            transport.reset(scope="everything")

    @pytest.mark.skipif(
        not _DESTRUCTIVE,
        reason=(
            "reset(scope='machine') triggers an ~8s reboot — "
            "set U64_DESTRUCTIVE=1 to opt in"
        ),
    )
    def test_reset_scope_machine_reboots_and_recovers(
        self, transport: Ultimate64Transport
    ) -> None:
        """Full FPGA reboot — device must come back reachable within ~15s.

        Destructive: the reboot disrupts any other activity on the device.
        Placed last in the class so the cheaper cases run first.
        """
        try:
            transport.reset(scope="machine")
            deadline = time.monotonic() + 15.0
            recovered = False
            while time.monotonic() < deadline:
                if is_u64_reachable(_HOST, password=_PW):
                    recovered = True
                    break
                time.sleep(0.5)
            assert recovered, (
                f"U64 at {_HOST} did not become reachable within 15s "
                f"after reset(scope='machine')"
            )
            data = transport.read_memory(0xA000, 16)
            assert isinstance(data, bytes) and len(data) == 16
        finally:
            try:
                transport.set_speed(1)
            except Exception:
                pass
