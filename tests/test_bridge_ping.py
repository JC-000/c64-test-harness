"""Bridge ICMP exchange test -- two VICE instances exchange IP-layer frames.

Two VICE instances on ``br-c64`` exchange ICMP echo traffic via RR-Net
ethernet:

* VICE A transmits a Python-crafted ICMP echo request frame addressed
  to VICE B's MAC and IP.
* VICE B runs a 6502 RX routine that polls the CS8900a, drains
  non-matching frames (e.g. host-side IPv6 multicast), and reports
  success when it sees the matching ICMP echo request.
* The test verifies B's RX buffer contains the expected IP/ICMP
  fields and the original payload.

This test proves that the bridge supports **IP-layer** frame exchange
between two C64 instances, not just raw L2 frames (which is what
``test_ethernet_bridge.py`` already covers).

Prerequisites:
- x64sc on PATH with RR-Net ethernet support
- Bridge set up via ``scripts/setup-bridge-tap.sh``
"""

from __future__ import annotations

import os
import shutil
import threading
import time

import pytest

from c64_test_harness.backends.vice_binary import BinaryViceTransport
from c64_test_harness.bridge_ping import (
    Asm,
    ISQ_HI,
    PPDATA_HI,
    PPDATA_LO,
    PPTR_HI,
    PPTR_LO,
    RTDATA_HI,
    RTDATA_LO,
    build_echo_request_frame,
    build_tx_code,
)
from c64_test_harness.execute import jsr, load_code
from c64_test_harness.memory import read_bytes, write_bytes

# ---------------------------------------------------------------------------
# Skip conditions
# ---------------------------------------------------------------------------
_HAS_X64SC = shutil.which("x64sc") is not None

pytestmark = [
    pytest.mark.skipif(not _HAS_X64SC, reason="x64sc not found on PATH"),
    pytest.mark.skipif(
        not os.path.isdir("/sys/class/net/tap-c64-0"),
        reason="tap-c64-0 not found (run scripts/setup-bridge-tap.sh)",
    ),
    pytest.mark.skipif(
        not os.path.isdir("/sys/class/net/tap-c64-1"),
        reason="tap-c64-1 not found (run scripts/setup-bridge-tap.sh)",
    ),
]

# ---------------------------------------------------------------------------
# Memory layout (RESULT must NOT overlap code area $C000-$C17F)
# ---------------------------------------------------------------------------
CODE = 0xC000
SCRATCH = 0xC1E0
RESULT = 0xC1F0
TX_FRAME_BUF = 0xC500
RX_FRAME_BUF = 0xC700

MAC_A = bytes.fromhex("02C640000001")
MAC_B = bytes.fromhex("02C640000002")
IP_A = bytes([10, 0, 65, 2])
IP_B = bytes([10, 0, 65, 3])

PING_ID = 0xBEEF
PING_SEQ = 0x0001
PING_PAYLOAD = b"PING_FROM_VICE_A"

# Bytes the RX routine reads per frame (must be even, must be enough to
# include IP+ICMP headers + payload markers)
_RX_BYTES = 60


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_rx_match_icmp_code(
    load_addr: int,
    rx_buf: int,
    result_addr: int,
    expect_id: int,
    expect_seq: int,
) -> bytes:
    """6502 routine that polls RX, looking for an ICMP echo request frame.

    Reads each incoming frame into ``rx_buf`` (fixed _RX_BYTES bytes),
    checks ethertype=IPv4, protocol=ICMP, type=8 (echo request), and
    matching identifier+sequence (big-endian on the wire).  On match,
    writes 0x01 to ``result_addr``.  On poll-budget exhaustion, writes
    0xFF.  Non-matching frames are dropped and polling continues.
    """
    id_hi = (expect_id >> 8) & 0xFF
    id_lo = expect_id & 0xFF
    seq_hi = (expect_seq >> 8) & 0xFF
    seq_lo = expect_seq & 0xFF

    a = Asm(org=load_addr)
    a.emit(0x78)  # SEI
    # RR clockport enable
    a.emit(0xAD, ISQ_HI & 0xFF, ISQ_HI >> 8)
    a.emit(0x09, 0x01)
    a.emit(0x8D, ISQ_HI & 0xFF, ISQ_HI >> 8)

    a.label("reset")
    # Inline poll loop with 3-level timeout (~5 seconds budget)
    a.emit(0xA9, 0x24, 0x8D, PPTR_LO & 0xFF, PPTR_LO >> 8)
    a.emit(0xA9, 0x01, 0x8D, PPTR_HI & 0xFF, PPTR_HI >> 8)
    a.emit(0xA9, 0xFF, 0x85, 0xF0)
    a.emit(0xA9, 0xFF, 0x85, 0xF1)
    a.emit(0xA9, 0x04, 0x85, 0xF2)
    a.label("poll")
    a.emit(0xAD, PPDATA_HI & 0xFF, PPDATA_HI >> 8)
    a.emit(0x29, 0x01)
    a.branch(0xD0, "got")
    a.emit(0xC6, 0xF0)
    a.branch(0xD0, "poll")
    a.emit(0xA9, 0xFF, 0x85, 0xF0)
    a.emit(0xC6, 0xF1)
    a.branch(0xD0, "poll")
    a.emit(0xA9, 0xFF, 0x85, 0xF1)
    a.emit(0xC6, 0xF2)
    a.branch(0xD0, "poll")
    a.jmp("timeout")

    a.label("got")
    # Discard 4 bytes (CS8900a status+length header)
    a.emit(0xAD, RTDATA_LO & 0xFF, RTDATA_LO >> 8)
    a.emit(0xAD, RTDATA_HI & 0xFF, RTDATA_HI >> 8)
    a.emit(0xAD, RTDATA_LO & 0xFF, RTDATA_LO >> 8)
    a.emit(0xAD, RTDATA_HI & 0xFF, RTDATA_HI >> 8)
    # Read _RX_BYTES bytes into rx_buf
    a.emit(0xA9, rx_buf & 0xFF, 0x85, 0xFB)
    a.emit(0xA9, (rx_buf >> 8) & 0xFF, 0x85, 0xFC)
    a.emit(0xA0, 0x00)
    a.label("rdlp")
    a.emit(0xAD, RTDATA_LO & 0xFF, RTDATA_LO >> 8)
    a.emit(0x91, 0xFB)
    a.emit(0xC8)
    a.emit(0xAD, RTDATA_HI & 0xFF, RTDATA_HI >> 8)
    a.emit(0x91, 0xFB)
    a.emit(0xC8)
    a.emit(0xC0, _RX_BYTES)
    a.branch(0xD0, "rdlp")

    # Skip rest of packet (RxCFG bit 6 - Skip-1)
    a.emit(0xA9, 0x02, 0x8D, PPTR_LO & 0xFF, PPTR_LO >> 8)
    a.emit(0xA9, 0x01, 0x8D, PPTR_HI & 0xFF, PPTR_HI >> 8)
    a.emit(0xAD, PPDATA_LO & 0xFF, PPDATA_LO >> 8)
    a.emit(0x09, 0x40)
    a.emit(0x8D, PPDATA_LO & 0xFF, PPDATA_LO >> 8)

    def chk(off: int, val: int, fail: str) -> None:
        addr = rx_buf + off
        a.emit(0xAD, addr & 0xFF, (addr >> 8) & 0xFF)
        a.emit(0xC9, val & 0xFF)
        a.branch(0xD0, fail)

    chk(12, 0x08, "drop")
    chk(13, 0x00, "drop")
    chk(23, 0x01, "drop")        # IP protocol = ICMP
    chk(34, 0x08, "drop")        # ICMP type = echo request
    chk(38, id_hi, "drop")
    chk(39, id_lo, "drop")
    chk(40, seq_hi, "drop")
    chk(41, seq_lo, "drop")
    a.jmp("success")

    a.label("drop")
    a.jmp("reset")

    a.label("success")
    a.emit(0xA9, 0x01, 0x8D, result_addr & 0xFF, (result_addr >> 8) & 0xFF)
    a.emit(0x58)
    a.emit(0x60)

    a.label("timeout")
    a.emit(0xA9, 0xFF, 0x8D, result_addr & 0xFF, (result_addr >> 8) & 0xFF)
    a.emit(0x58)
    a.emit(0x60)
    return a.build()


# ---------------------------------------------------------------------------
# The bridge_vice_pair fixture is now defined in conftest.py for reuse.
# This file uses the matching MAC/IP constants defined here so the test
# remains self-documenting; conftest exports BRIDGE_MAC_A/B with the
# same values.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBridgeIcmp:
    """Two VICE instances exchange IP-layer ICMP frames over a bridge."""

    def test_icmp_echo_request_received(
        self,
        bridge_vice_pair: tuple[BinaryViceTransport, BinaryViceTransport],
    ) -> None:
        """A transmits an ICMP echo request -- B receives it via CS8900a.

        Verifies that two VICE instances on the same Linux bridge can
        exchange a complete IPv4/ICMP frame at the byte level: A's
        Python-crafted echo request frame is loaded into A's memory and
        transmitted via the CS8900a, and B's 6502 RX routine reads the
        frame from its CS8900a and stores it in B's memory.  The test
        then inspects B's memory and verifies the entire IP+ICMP header
        and payload matches what A sent.
        """
        transport_a, transport_b = bridge_vice_pair

        # Build the echo request frame (A -> B), broadcast dst MAC for
        # broad CS8900a filter compatibility (some filter configurations
        # only accept broadcast).  IP layer is unicast B.
        echo = build_echo_request_frame(
            src_mac=MAC_A,
            dst_mac=b"\xff\xff\xff\xff\xff\xff",
            src_ip=IP_A,
            dst_ip=IP_B,
            identifier=PING_ID,
            sequence=PING_SEQ,
            payload=PING_PAYLOAD,
        )
        frame_len = len(echo.frame)

        # B's RX routine: poll until matching ICMP echo request received
        rx_code = _build_rx_match_icmp_code(
            load_addr=CODE,
            rx_buf=RX_FRAME_BUF,
            result_addr=RESULT,
            expect_id=PING_ID,
            expect_seq=PING_SEQ,
        )
        load_code(transport_b, CODE, rx_code)
        write_bytes(transport_b, RESULT, [0x00])
        write_bytes(transport_b, RX_FRAME_BUF, [0x00] * 256)

        # A's TX routine: just transmit the echo request frame
        tx_code = build_tx_code(
            load_addr=CODE,
            frame_buf=TX_FRAME_BUF,
            frame_len=frame_len,
            result_addr=RESULT,
        )
        load_code(transport_a, CODE, tx_code)
        write_bytes(transport_a, TX_FRAME_BUF, echo.frame)
        write_bytes(transport_a, RESULT, [0x00])

        # Run concurrently: B starts polling first, A transmits after delay
        rx_error: list[Exception] = []
        tx_error: list[Exception] = []

        def rx_worker() -> None:
            try:
                jsr(transport_b, CODE, timeout=30.0)
            except Exception as e:
                rx_error.append(e)

        def tx_worker() -> None:
            try:
                time.sleep(0.8)
                jsr(transport_a, CODE, timeout=10.0)
            except Exception as e:
                tx_error.append(e)

        tr = threading.Thread(target=rx_worker, daemon=True)
        tt = threading.Thread(target=tx_worker, daemon=True)
        tr.start()
        tt.start()
        tr.join(timeout=45.0)
        tt.join(timeout=45.0)

        if rx_error:
            raise AssertionError(
                f"RX thread raised: {rx_error[0]}"
            ) from rx_error[0]
        if tx_error:
            raise AssertionError(
                f"TX thread raised: {tx_error[0]}"
            ) from tx_error[0]

        a_result = read_bytes(transport_a, RESULT, 1)[0]
        b_result = read_bytes(transport_b, RESULT, 1)[0]

        assert a_result == 0x01, (
            f"A TX did not complete (result=0x{a_result:02X})"
        )
        if b_result == 0xFF:
            pytest.fail(
                "B did not receive a matching ICMP echo request within "
                "the poll budget -- frame may not have reached the "
                "bridge or B's CS8900a filter is misconfigured"
            )
        assert b_result == 0x01, (
            f"B RX did not match (result=0x{b_result:02X})"
        )

        # Verify B's RX buffer contains the expected ICMP echo request
        rx = bytes(read_bytes(transport_b, RX_FRAME_BUF, _RX_BYTES))
        # Ethernet header
        assert rx[12:14] == b"\x08\x00", (
            f"reply ethertype not IPv4: {rx[12:14].hex()}"
        )
        # IP header
        assert rx[23] == 0x01, f"IP protocol not ICMP: 0x{rx[23]:02X}"
        # IP src/dst
        assert rx[26:30] == IP_A, (
            f"IP src mismatch: {rx[26:30].hex()} expected {IP_A.hex()}"
        )
        assert rx[30:34] == IP_B, (
            f"IP dst mismatch: {rx[30:34].hex()} expected {IP_B.hex()}"
        )
        # ICMP header
        assert rx[34] == 0x08, f"ICMP type not echo request: 0x{rx[34]:02X}"
        assert rx[38:40] == bytes([PING_ID >> 8, PING_ID & 0xFF]), (
            f"ICMP id mismatch: {rx[38:40].hex()}"
        )
        assert rx[40:42] == bytes([PING_SEQ >> 8, PING_SEQ & 0xFF]), (
            f"ICMP seq mismatch: {rx[40:42].hex()}"
        )
        # Payload
        assert PING_PAYLOAD in rx, (
            f"payload marker not found in RX buffer: {rx[:_RX_BYTES].hex()}"
        )
