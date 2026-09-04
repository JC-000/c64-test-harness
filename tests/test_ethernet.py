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

Screen polling goes through ``conftest.binary_wait_for_text``, which
resumes between polls and on every exit path; see the ``test_disk_vice.py``
module docstring for the ``resume()`` interaction it exists to handle.
"""

from __future__ import annotations

import os

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
from ethernet_scenarios import (
    capture_failure_disposition,
    resolve_capture_ifaces,
    run_product_id_scenario,
    run_rx_scenario,
    run_tx_scenario,
)

from conftest import (
    binary_wait_for_text,
    connect_binary_transport,
    start_vice_or_skip,
)

# ---------------------------------------------------------------------------
# Skip helpers
# ---------------------------------------------------------------------------


# Platform-dependent: Linux → first tap-*, macOS → first feth*.
TAP_IFACE = first_available_ethernet_iface()

# Where the *host* side captures and sends.  Defaults to VICE's interface;
# C64_ETH_CAPTURE_IFACE / C64_ETH_SEND_IFACE move either to another
# interface -- the peer of the feth pair -- without a code change.  Pivot
# to the peer when a TX failure shows VICE's own BPF descriptor at
# Written=1: the chip put the frame on the interface and the capture was
# on the wrong side of it.  See docs/bridge_networking.md "Host-side
# capture on macOS".
CAPTURE_IFACE: str | None = None
SEND_IFACE: str | None = None
#: Set when a knob names an interface that is not present; the capture
#: fixtures then fail with it (a typo must not become an ENXIO "bind").
IFACE_KNOB_ERROR: str | None = None
if TAP_IFACE:
    try:
        CAPTURE_IFACE, SEND_IFACE = resolve_capture_ifaces(TAP_IFACE, os.environ)
    except ValueError as _e:
        IFACE_KNOB_ERROR = str(_e)

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

    # start_vice_or_skip: converts a mid-launch ViceElevationRequiredError
    # (e.g. the preflight probe was bypassed with MACOS_PCAP_ENABLED=1)
    # into the same skip-or-fail + record the elevation("vice_root")
    # marker uses, instead of a bare fixture error. Called INSIDE the try
    # (adversarial review S2): a refusal must still release the port --
    # calling it before the try let a skip escape straight past
    # allocator.release(port), matching conftest.bridge_vice_pair's
    # pattern of putting the allocator release in the outermost finally.
    vice: ViceProcess | None = None
    try:
        vice = start_vice_or_skip(config)
        transport = connect_binary_transport(port, proc=vice)
        try:
            grid = binary_wait_for_text(transport, "READY.", timeout=30)
            assert grid is not None, "BASIC READY prompt not found"
            yield transport
        finally:
            transport.close()
    finally:
        if vice is not None:
            vice.stop()
        allocator.release(port)


def _open_capture_or_verdict(iface: str) -> PacketCapture:
    """Open once; skip or fail on the exception per its classification.

    Skips only when the path is genuinely absent (every node root-only, no
    nodes, no CAP_NET_RAW, no backend), with the message -- which carries
    the operator's remedy verbatim -- as the reason.  A path that exists
    but is broken (pool eaten while VICE is live, bind failure on the
    interface we just found, wrong DLT) *fails* with the same remedy: a
    skip there is how issue #158 hid.
    """
    if IFACE_KNOB_ERROR:
        pytest.fail(IFACE_KNOB_ERROR, pytrace=False)
    try:
        return open_capture(iface)
    except CaptureUnavailable as e:
        # vice_live=True: the fixtures below depend on vice_ethernet, so a
        # root VICE is up -- every node root-only is then a misconfigured
        # bench (fail with the chmod remedy), not an absent capability.
        verdict, reason = capture_failure_disposition(e, iface=iface, vice_live=True)
        if verdict == "skip":
            pytest.skip(reason)
        pytest.fail(reason, pytrace=False)


@pytest.fixture
def host_capture(vice_ethernet: BinaryViceTransport) -> PacketCapture:
    """An open host-side capture on ``CAPTURE_IFACE`` (TX side).

    Depends on ``vice_ethernet`` so the open happens *after* the elevated
    VICE has taken its two ``/dev/bpf*`` nodes: on macOS a root VICE takes
    the lowest free nodes, which are exactly the ones ``chmod o+rw`` made
    usable, so opening beforehand would report a pool this process no
    longer has.  One :func:`open_capture` call, no separate probe; see
    :func:`_open_capture_or_verdict` for the skip-vs-fail rule.  When the
    path opens the test runs, and a silent wire is then a failure (see
    ``ethernet_scenarios``).
    """
    if IFACE_KNOB_ERROR:
        pytest.fail(IFACE_KNOB_ERROR, pytrace=False)
    assert CAPTURE_IFACE is not None
    cap = _open_capture_or_verdict(CAPTURE_IFACE)
    try:
        yield cap
    finally:
        cap.close()


@pytest.fixture
def host_send_capture(host_capture: PacketCapture) -> PacketCapture:
    """The capture the RX frame is written through: ``SEND_IFACE``.

    The same object as ``host_capture`` unless ``C64_ETH_SEND_IFACE``
    names another interface, in which case a second capture is opened
    there (and consumes a second BPF node).
    """
    assert SEND_IFACE is not None
    if SEND_IFACE == host_capture.iface:
        yield host_capture
        return
    cap = _open_capture_or_verdict(SEND_IFACE)
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
        # The probe lives in ethernet_scenarios (results in RESULT, never in
        # its own bytes) so the wiring tests cover it like TX/RX.
        assert run_product_id_scenario(vice_ethernet) == 0x630E


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
        self,
        vice_ethernet: BinaryViceTransport,
        host_capture: PacketCapture,
        host_send_capture: PacketCapture,
    ) -> None:
        """Host injects a frame on ``SEND_IFACE``; the C64 reads it via CS8900a RX.

        A failed host send, or a C64 poll timeout (the frame never reached
        the chip), is an AssertionError, not a skip.
        """
        run_rx_scenario(
            vice_ethernet, host_capture, send_capture=host_send_capture,
            send_delay=0.5, timeout=15.0,
        )
