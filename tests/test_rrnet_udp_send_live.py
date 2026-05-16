"""Live RR-Net UDP send exemplar (VICE -> host, >512-byte payload).

What this exercises
-------------------

A single VICE instance with the CS8900a (RR-Net) ethernet cartridge
attached to one side of the host bridge transmits a complete
Ethernet+IPv4+UDP frame.  The frame is built host-side with
:func:`c64_test_harness.bridge_ping.build_udp_frame`, uploaded to C64
RAM, and TX'd via :func:`c64_test_harness.bridge_ping.build_tx_code`.
A regular Python ``socket`` listener on the host receives the datagram
and asserts the payload bytes match exactly.

This is the **first** UDP-aware live test in the harness -- the existing
two-VICE bridge tests only do raw L2 frames or ICMP echo.  It is
deliberately one-directional (C64 -> host) and uses a fixed 1024-byte
payload so the path also covers the "frame longer than 512 bytes"
regime that earlier exemplars never hit.

Gate
----

Set ``RRNET_UDP_LIVE=1`` to run.  When unset the whole module skips
cleanly.  Because the bridge tap and pcap-loop-back semantics are
substantially different on macOS, the live test is currently gated to
``sys.platform == "linux"``.  On macOS it will skip with a pointer to
``docs/bridge_networking.md``; the frame-builder unit tests
(``test_udp_frame_builder.py``) still run on both platforms.
"""

from __future__ import annotations

import os
import shutil
import socket
import sys
import threading
import time

import pytest

# Skip the entire module if the gate is unset -- collection still works,
# but tests will be marked skipped.  Module-level skip keeps the imports
# below from running on machines without VICE.
_LIVE_GATE = os.environ.get("RRNET_UDP_LIVE") == "1"
_IS_LINUX = sys.platform == "linux"

pytestmark = [
    pytest.mark.skipif(
        not _LIVE_GATE,
        reason="RRNET_UDP_LIVE=1 is required for this live test",
    ),
    pytest.mark.skipif(
        not _IS_LINUX,
        reason=(
            "RR-Net UDP bridge test currently requires Linux bridge tap "
            "(see docs/bridge_networking.md); macOS bridge support TBD"
        ),
    ),
]

# Defer heavy imports past the gate so plain test collection on a stock
# machine (no VICE, no harness venv) does not blow up.
if _LIVE_GATE and _IS_LINUX:
    from bridge_platform import (
        BRIDGE_NAME,
        ETHERNET_DRIVER,
        IFACE_A,
        SETUP_HINT,
        iface_present,
    )
    from c64_test_harness.backends.vice_binary import BinaryViceTransport
    from c64_test_harness.backends.vice_lifecycle import ViceConfig, ViceProcess
    from c64_test_harness.backends.vice_manager import PortAllocator
    from c64_test_harness.bridge_ping import build_tx_code, build_udp_frame
    from c64_test_harness.ethernet import set_cs8900a_mac
    from c64_test_harness.execute import jsr, load_code
    from c64_test_harness.memory import read_bytes, write_bytes
    from conftest import (
        _bridge_init_cs8900a,
        _bridge_wait_ready,
        connect_binary_transport,
    )

    # Add a runtime "is x64sc on PATH" + "are the interfaces up" skip --
    # mirrors the pattern in tests/test_ethernet_bridge.py so a
    # half-configured machine bails cleanly rather than hanging in VICE
    # bringup.
    pytestmark.extend([
        pytest.mark.skipif(
            shutil.which("x64sc") is None,
            reason="x64sc not found on PATH",
        ),
        pytest.mark.skipif(
            not iface_present(IFACE_A),
            reason=f"{IFACE_A} not found ({SETUP_HINT})",
        ),
        pytest.mark.skipif(
            not iface_present(BRIDGE_NAME),
            reason=(
                f"{BRIDGE_NAME} not found; the host bridge must be up "
                f"({SETUP_HINT})"
            ),
        ),
    ])


# ---------------------------------------------------------------------------
# Constants (kept in sync with conftest.bridge_vice_pair / bridge_ping_demo.py)
# ---------------------------------------------------------------------------
# C64 MAC and IP -- the same locally-administered MAC used by the bridge
# fixture pair.  The host bridge already knows this address pattern.
C64_MAC = bytes.fromhex("02C640000001")
C64_IP = bytes([10, 0, 65, 2])

# Host bridge IP (set by scripts/setup-bridge-tap.sh).  The host MAC is
# not fixed -- we use Ethernet broadcast for the destination so we don't
# need ARP.  Linux raw-AF_PACKET bridges happily flood-deliver broadcast
# frames to the host stack on the bridge interface; the kernel then
# accepts the IPv4/UDP packet as locally-destined because dst_ip
# matches the bridge's IP.
HOST_IP = bytes([10, 0, 65, 1])
HOST_MAC_BROADCAST = b"\xff\xff\xff\xff\xff\xff"

# UDP ports.  Source port is arbitrary, dst port is what the host
# listener binds.  Pick a high ephemeral port so the test cannot
# accidentally collide with a system service.
SRC_PORT = 49152
DST_PORT = 51234

# Payload: 1024 bytes of an easy-to-spot pattern (matches the brief).
PAYLOAD = bytes(range(256)) * 4
assert len(PAYLOAD) == 1024

# Memory layout (mirrors test_ethernet_bridge.py: code in $C000-$C0DF,
# result flag at $C0F0, TX frame buffer at $C100).
CODE = 0xC000
RESULT = 0xC0F0
FRAME_BUF = 0xC100
SCRATCH = 0xC1E0  # used by _bridge_init_cs8900a for LineCTL readback


# ---------------------------------------------------------------------------
# Host listener
# ---------------------------------------------------------------------------

def _listen_for_udp(
    bind_ip: str,
    bind_port: int,
    timeout: float,
    out: dict,
) -> None:
    """Block on a single UDP recv with *timeout*.

    Result stored in *out* under either ``"data"`` (the received bytes,
    payload only -- not the full datagram) or ``"error"`` (string).
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((bind_ip, bind_port))
            sock.settimeout(timeout)
            data, src = sock.recvfrom(65535)
            out["data"] = data
            out["src"] = src
        finally:
            sock.close()
    except Exception as e:  # noqa: BLE001 -- relay all errors to the test
        out["error"] = f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def single_vice_with_rrnet():
    """Bring up ONE VICE instance on the bridge's interface A side.

    Only runs when the live gate + Linux + interface checks pass.  The
    fixture is module-scoped so the (slow) VICE boot is shared across
    all tests in this file -- there is currently only one, but the
    shape leaves room for future cases.
    """
    allocator = PortAllocator(port_range_start=6580, port_range_end=6600)
    port = allocator.allocate()
    reservation = allocator.take_socket(port)
    if reservation is not None:
        reservation.close()

    config = ViceConfig(
        port=port,
        warp=False,
        sound=False,
        ethernet=True,
        ethernet_mode="rrnet",
        ethernet_interface=IFACE_A,
        ethernet_driver=ETHERNET_DRIVER,
    )

    vice = ViceProcess(config)
    try:
        vice.start()
        transport = connect_binary_transport(port, proc=vice)
        try:
            _bridge_wait_ready(transport)
            _bridge_init_cs8900a(transport, SCRATCH, CODE)
            set_cs8900a_mac(transport, C64_MAC)
            yield transport
        finally:
            transport.close()
    finally:
        vice.stop()
        allocator.release(port)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRrnetUdpSend:
    """C64 -> host UDP send via the RR-Net cartridge on the L2 bridge."""

    def test_c64_sends_1024_byte_udp_datagram_to_host(
        self,
        single_vice_with_rrnet: "BinaryViceTransport",
    ) -> None:
        """Build a 1024-byte UDP frame on the host, TX from C64, host receives it."""
        transport = single_vice_with_rrnet

        # ---- Build the frame on the host ----
        frame = build_udp_frame(
            src_mac=C64_MAC,
            dst_mac=HOST_MAC_BROADCAST,
            src_ip=C64_IP,
            dst_ip=HOST_IP,
            src_port=SRC_PORT,
            dst_port=DST_PORT,
            payload=PAYLOAD,
        )
        frame_len = len(frame)
        # 14 + 20 + 8 + 1024 = 1066; no Ethernet padding for this size.
        assert frame_len == 1066

        # ---- Stage host listener BEFORE C64 transmit ----
        # Bind to 0.0.0.0 (not just HOST_IP) -- the kernel still routes
        # broadcast-destined UDP to a socket bound to INADDR_ANY on the
        # right port, and binding to 0.0.0.0 avoids surprises if the
        # bridge's IP changes between runs.
        listener_out: dict = {}
        listener = threading.Thread(
            target=_listen_for_udp,
            kwargs=dict(
                bind_ip="0.0.0.0",
                bind_port=DST_PORT,
                timeout=15.0,
                out=listener_out,
            ),
            daemon=True,
        )
        listener.start()
        # Brief settle: make sure the bind has happened before VICE TXes.
        time.sleep(0.3)

        # ---- Load frame + TX code on the C64, then run TX ----
        tx_code = build_tx_code(
            load_addr=CODE,
            frame_buf=FRAME_BUF,
            frame_len=frame_len,
            result_addr=RESULT,
        )
        load_code(transport, CODE, tx_code)
        write_bytes(transport, FRAME_BUF, frame)
        write_bytes(transport, RESULT, [0x00])

        jsr(transport, CODE, timeout=10.0)

        tx_result = bytes(read_bytes(transport, RESULT, 1))
        assert tx_result == b"\x01", (
            f"TX routine did not complete (result=0x{tx_result[0]:02X})"
        )

        # ---- Wait for the listener to pick up the datagram ----
        listener.join(timeout=20.0)
        assert not listener.is_alive(), "host UDP listener did not return"
        if "error" in listener_out:
            pytest.fail(f"host listener errored: {listener_out['error']}")
        assert "data" in listener_out, (
            "host listener returned without data and without an error -- "
            "did the VICE frame actually leave the cartridge?"
        )

        received = listener_out["data"]
        assert received == PAYLOAD, (
            f"host received {len(received)} bytes; expected {len(PAYLOAD)}. "
            f"First mismatch at index "
            f"{next((i for i, (a, b) in enumerate(zip(received, PAYLOAD)) if a != b), -1)}"
        )

        # Sanity: the source IP / port the kernel reports must match what
        # we built into the frame.  If this disagrees, something on the
        # bridge (NAT? bridge MAC learning?) is rewriting the packet --
        # surface that loudly so the next agent doesn't chase phantom
        # checksum bugs.
        src_ip_str, src_port = listener_out["src"]
        assert src_ip_str == "10.0.65.2", (
            f"unexpected source IP from listener: {src_ip_str}"
        )
        assert src_port == SRC_PORT, (
            f"unexpected source port from listener: {src_port}"
        )
