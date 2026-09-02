"""Ethernet / RR-Net (CS8900a) integration tests (binary monitor transport).

Validates that VICE can emulate the CS8900a ethernet chip via the RR-Net
cartridge mode, connected to a host ethernet interface.  Tests probe the
chip ID register and exercise TX/RX packet I/O.  The interface is
selected via ``bridge_platform.first_available_ethernet_iface()`` using
the matching ``ETHERNET_DRIVER``; platform-specific details (TAP+iproute2
on Linux, feth+BSD bridge on macOS) live in that module and the matching
``scripts/setup-bridge-*-{linux,macos}.sh`` setup scripts.  Uses
``BinaryViceTransport`` (``-binarymonitor``) for all VICE communication;
all tests are skipped automatically if ``x64sc`` is missing, no ethernet
interface is available, or VICE lacks the required driver support.

Host-side capture and injection go through ``c64_test_harness.capture``
(``AF_PACKET`` on Linux, ``/dev/bpf*`` on macOS -- issue #158).  The TX
and RX bodies live in ``tests/ethernet_scenarios.py`` so that
``tests/test_ethernet_capture_wiring.py`` can prove, with fakes, that
they fail rather than skip when no frame is seen.  They skip only when
:func:`~c64_test_harness.capture.open_capture` reports the path is
genuinely unavailable, and then the skip reason *is* the remedy.

See ``test_disk_vice.py`` module docstring for the screen polling /
``resume()`` interaction explanation.
"""

from __future__ import annotations

import time

import pytest

from bridge_platform import (
    ETHERNET_DRIVER,
    SETUP_HINT,
    first_available_ethernet_iface,
    probe_vice_pcap_ok,
)
from c64_test_harness.backends.vice_binary import BinaryViceTransport
from c64_test_harness.backends.vice_lifecycle import ViceConfig, ViceProcess
from c64_test_harness.backends.vice_manager import PortAllocator
from c64_test_harness.capture import (
    CaptureUnavailable,
    PacketCapture,
    open_capture,
)
from c64_test_harness.execute import load_code
from c64_test_harness.memory import read_bytes
from c64_test_harness.screen import ScreenGrid
from ethernet_scenarios import (
    CODE_BASE,
    PPDATA,
    PPTR,
    binary_jsr,
    clockport_enable_code,
    run_rx_scenario,
    run_tx_scenario,
)

from conftest import connect_binary_transport

# ---------------------------------------------------------------------------
# Skip helpers
# ---------------------------------------------------------------------------


# Platform-dependent: Linux → first tap-*, macOS → first feth*.
TAP_IFACE = first_available_ethernet_iface()

# On macOS 26 Tahoe the Homebrew VICE 3.10 bottle crashes immediately when
# launched with ``-ethernetiodriver pcap -ethernetioif feth<N>`` (the
# binary monitor never becomes reachable, VICE exits, and the host sees a
# system crash dialog for x64sc). Root cause is upstream and likely shares
# the same init-order-bug cluster as the known ``x64sc --version`` crash
# (archdep_program_path_set_argv0 called after the pcap init path on macOS
# 26; see docs/development.md "macOS (Homebrew)" caveats). Rather than
# gate the tests behind an opt-in env var -- which makes full-suite runs
# on a fresh machine hit a crash dialog before they learn the env var
# exists -- we actively probe VICE once per process and skip cleanly if
# the probe fails. ``MACOS_PCAP_DISABLED=1`` short-circuits the probe to
# "broken" and ``MACOS_PCAP_ENABLED=1`` short-circuits it to "ok". See
# ``tests/bridge_platform.probe_vice_pcap_ok`` and
# ``scripts/probe-vice-feth.sh`` for deeper diagnosis.
_PCAP_OK, _PCAP_REASON = probe_vice_pcap_ok(iface=TAP_IFACE)

pytestmark = [
    pytest.mark.vice_live,
    pytest.mark.skipif(
        TAP_IFACE is None,
        reason=f"No ethernet interface available for VICE ({SETUP_HINT})",
    ),
    pytest.mark.skipif(not _PCAP_OK, reason=_PCAP_REASON),
]


# ---------------------------------------------------------------------------
# Binary transport helpers
# ---------------------------------------------------------------------------

def _binary_wait_for_text(
    transport: BinaryViceTransport,
    needle: str,
    timeout: float = 30.0,
    poll_interval: float = 2.0,
) -> ScreenGrid | None:
    """Wait until *needle* appears on screen, resuming between polls.

    See ``test_disk_vice.py`` module docstring for why this is needed.
    """
    needle_upper = needle.upper()
    start = time.monotonic()
    while True:
        elapsed = time.monotonic() - start
        if elapsed >= timeout:
            return None
        try:
            transport.resume()
            time.sleep(poll_interval)
            grid = ScreenGrid.from_transport(transport)
            if needle_upper in grid.continuous_text().upper():
                return grid
        except Exception:
            time.sleep(poll_interval)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def vice_ethernet():
    """Launch VICE with RR-Net on the TAP interface, yield transport."""
    allocator = PortAllocator(port_range_start=6511, port_range_end=6531)
    port = allocator.allocate()
    reservation = allocator.take_socket(port)
    if reservation is not None:
        reservation.close()

    config = ViceConfig(
        port=port,
        warp=False,  # warp can cause timing issues with ethernet
        sound=False,
        ethernet=True,
        ethernet_mode="rrnet",
        ethernet_interface=TAP_IFACE or "",
        ethernet_driver=ETHERNET_DRIVER,
    )

    with ViceProcess(config) as vice:
        transport = connect_binary_transport(port, proc=vice)
        try:
            grid = _binary_wait_for_text(transport, "READY.", timeout=30)
            assert grid is not None, "BASIC READY prompt not found"
            yield transport
        finally:
            transport.close()
            allocator.release(port)


@pytest.fixture
def host_capture(vice_ethernet: BinaryViceTransport) -> PacketCapture:
    """An open host-side capture on ``TAP_IFACE``, or a skip that names the fix.

    Depends on ``vice_ethernet`` so the open happens *after* the elevated
    VICE has taken its two ``/dev/bpf*`` nodes: on macOS a root VICE takes
    the lowest free nodes, which are exactly the ones ``chmod o+rw`` made
    usable, so opening beforehand would report a pool this process no
    longer has.

    One :func:`open_capture` call, no separate probe: the exception it
    raises is the verdict.  Skips only when the path is genuinely absent,
    with the message -- which carries the operator's remedy verbatim --
    as the reason.  When the path exists the test runs, and a silent wire
    is then a *failure* (see ``ethernet_scenarios``).
    """
    assert TAP_IFACE is not None
    try:
        cap = open_capture(TAP_IFACE)
    except CaptureUnavailable as e:
        pytest.skip(f"no host-side capture on {TAP_IFACE}: {e}")
    try:
        yield cap
    finally:
        cap.close()


# ---------------------------------------------------------------------------
# Part 2: CS8900a Probe Test
# ---------------------------------------------------------------------------


class TestCS8900aProbe:
    """Verify CS8900a chip is present by reading Product ID register."""

    def test_product_id(self, vice_ethernet: BinaryViceTransport) -> None:
        """Read CS8900a Product ID -- expect 0x630E.

        RR-Net mode: PPPtr lives at $DE02/$DE03 and PPData at $DE04/$DE05.
        The RR clockport bit ($DE01 bit 0) MUST be enabled first.
        """
        transport = vice_ethernet

        probe_code = clockport_enable_code() + bytes([
            0xA9, 0x00,                          # LDA #$00
            0x8D, PPTR & 0xFF, PPTR >> 8,        # STA $DE02 (PPPtr lo)
            0x8D, (PPTR + 1) & 0xFF, (PPTR + 1) >> 8,  # STA $DE03 (PPPtr hi)
            0xAD, PPDATA & 0xFF, PPDATA >> 8,    # LDA $DE04 (PPData lo)
            0x8D, 0x00, 0xC0,                    # STA $C000
            0xAD, (PPDATA + 1) & 0xFF, (PPDATA + 1) >> 8,  # LDA $DE05 (PPData hi)
            0x8D, 0x01, 0xC0,                    # STA $C001
            0x60,                                # RTS
        ])

        load_code(transport, CODE_BASE, probe_code)
        binary_jsr(transport, CODE_BASE, timeout=10)

        result = read_bytes(transport, 0xC000, 2)
        chip_id = result[0] | (result[1] << 8)

        assert result[0] == 0x0E, f"PP Data low: expected 0x0E, got 0x{result[0]:02X}"
        assert result[1] == 0x63, f"PP Data high: expected 0x63, got 0x{result[1]:02X}"
        assert chip_id == 0x630E, f"CS8900a Product ID: expected 0x630E, got 0x{chip_id:04X}"


# ---------------------------------------------------------------------------
# Part 3: Ethernet Traffic Tests
# ---------------------------------------------------------------------------


class TestEthernetTX:
    """Send an ethernet frame from the C64 and capture it on the host."""

    def test_send_broadcast_frame(
        self, vice_ethernet: BinaryViceTransport, host_capture: PacketCapture
    ) -> None:
        """C64 sends a 64-byte broadcast frame; the host must capture it.

        The capture is open and bound before the routine runs.  No frame
        with our ethertype within 5 s is an AssertionError, not a skip.
        """
        run_tx_scenario(vice_ethernet, host_capture, timeout=5.0)


class TestEthernetRX:
    """Send a packet from the host and have the C64 receive it."""

    def test_receive_frame(
        self, vice_ethernet: BinaryViceTransport, host_capture: PacketCapture
    ) -> None:
        """Host injects a frame on the interface; the C64 reads it via CS8900a RX.

        A failed host send, or a C64 poll timeout (the frame never reached
        the chip), is an AssertionError, not a skip.
        """
        run_rx_scenario(vice_ethernet, host_capture, send_delay=0.5, timeout=15.0)
