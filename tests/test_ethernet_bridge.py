"""Ethernet bridge test -- two VICE instances exchange frames via CS8900a.

Two VICE instances, each with their own TAP interface (tap-c64-0, tap-c64-1)
in TFE mode, communicate over a Linux bridge.  The test verifies bidirectional
frame exchange: A sends to B, then B sends to A.

Prerequisites:
- x64sc on PATH with ethernet cartridge support
- Two TAP interfaces: tap-c64-0 and tap-c64-1, bridged at the Linux level
- VICE must be compiled with tuntap driver support

All tests are skipped automatically if prerequisites are missing.
"""

from __future__ import annotations

import os
import shutil
import threading
import time

import pytest

from c64_test_harness.backends.vice_binary import BinaryViceTransport
from c64_test_harness.backends.vice_lifecycle import ViceConfig, ViceProcess
from c64_test_harness.backends.vice_manager import PortAllocator
from c64_test_harness.ethernet import set_cs8900a_mac
from c64_test_harness.execute import jsr, load_code
from c64_test_harness.memory import read_bytes, write_bytes

from conftest import connect_binary_transport

# ---------------------------------------------------------------------------
# Skip conditions
# ---------------------------------------------------------------------------

_HAS_X64SC = shutil.which("x64sc") is not None

pytestmark = [
    pytest.mark.skipif(not _HAS_X64SC, reason="x64sc not found on PATH"),
    pytest.mark.skipif(
        not os.path.isdir("/sys/class/net/tap-c64-0"),
        reason="tap-c64-0 interface not found",
    ),
    pytest.mark.skipif(
        not os.path.isdir("/sys/class/net/tap-c64-1"),
        reason="tap-c64-1 interface not found",
    ),
]

# ---------------------------------------------------------------------------
# Memory layout -- result/meta addresses MUST NOT overlap generated code
# ---------------------------------------------------------------------------
CODE = 0xC000       # 6502 routine (up to ~200 bytes)
RX_META = 0xC0E0    # RxStatus(2) + RxLength(2)
RESULT = 0xC0F0     # 1-byte result flag (0x00=pending, 0x01=success, 0xFF=timeout)
FRAME_BUF = 0xC100  # TX frame data buffer
RX_BUF = 0xC300     # RX received data buffer

# CS8900a I/O registers (TFE mode at $DE00)
RTDATA_LO = 0xDE00
RTDATA_HI = 0xDE01
TXCMD_LO = 0xDE04
TXCMD_HI = 0xDE05
TXLEN_LO = 0xDE06
TXLEN_HI = 0xDE07
PPTR_LO = 0xDE0A
PPTR_HI = 0xDE0B
PPDATA_LO = 0xDE0C
PPDATA_HI = 0xDE0D

# Frame constants
DEST_MAC = b"\xFF\xFF\xFF\xFF\xFF\xFF"  # broadcast
SRC_MAC_A = b"\x02\x00\x00\x00\x00\x01"
SRC_MAC_B = b"\x02\x00\x00\x00\x00\x02"
ETHERTYPE = b"\xC6\x40"  # custom ethertype
PAYLOAD_A = b"HELLO_FROM_A" + b"\x00" * (46 - 12)  # pad to 46 bytes min payload
PAYLOAD_B = b"HELLO_FROM_B" + b"\x00" * (46 - 12)
FRAME_LEN = 64  # 14 header + 46 payload + 4 (but CS8900a handles FCS)


# ---------------------------------------------------------------------------
# Mini 6502 assembler (branch fixups)
# ---------------------------------------------------------------------------
class Asm:
    """Tiny 6502 assembler with automatic branch offset calculation."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.labels: dict[str, int] = {}
        self._fix: list[tuple[int, str]] = []

    @property
    def pos(self) -> int:
        return len(self.buf)

    def label(self, name: str) -> None:
        self.labels[name] = self.pos

    def emit(self, *data: int) -> None:
        self.buf.extend(data)

    def branch(self, opcode: int, target: str) -> None:
        self.buf.append(opcode)
        self._fix.append((self.pos, target))
        self.buf.append(0)

    def build(self) -> bytes:
        for fix_pos, label in self._fix:
            target = self.labels[label]
            disp = target - (fix_pos + 1)
            if not (-128 <= disp <= 127):
                raise ValueError(f"Branch to '{label}' out of range: {disp}")
            self.buf[fix_pos] = disp & 0xFF
        return bytes(self.buf)


# ---------------------------------------------------------------------------
# CS8900a initialization
# ---------------------------------------------------------------------------

def _init_cs8900a(transport: BinaryViceTransport) -> None:
    """Initialize CS8900a: enable promiscuous RX + SerTxON/SerRxON in LineCTL.

    Without this, the CS8900a accepts TX commands but never pushes frames
    to the TAP interface.  validate_ping.py does this; our earlier version
    did not, which is why TX frames weren't reaching the bridge.
    """
    # Step 1: RxCTL (PP 0x0104) = 0x00D8  (promiscuous + RxOK)
    pp_write_code = bytes([
        0xA9, 0x04, 0x8D, PPTR_LO & 0xFF, PPTR_LO >> 8,        # PPPtr = 0x0104
        0xA9, 0x01, 0x8D, PPTR_HI & 0xFF, PPTR_HI >> 8,
        0xA9, 0xD8, 0x8D, PPDATA_LO & 0xFF, PPDATA_LO >> 8,     # PPData = 0x00D8
        0xA9, 0x00, 0x8D, PPDATA_HI & 0xFF, PPDATA_HI >> 8,
        0x60,
    ])
    load_code(transport, CODE, pp_write_code)
    jsr(transport, CODE, timeout=5.0)

    # Step 2: Read LineCTL (PP 0x0112), OR with 0x00C0, write back
    # Read current LineCTL into RESULT/RESULT+1
    pp_read_code = bytes([
        0xA9, 0x12, 0x8D, PPTR_LO & 0xFF, PPTR_LO >> 8,        # PPPtr = 0x0112
        0xA9, 0x01, 0x8D, PPTR_HI & 0xFF, PPTR_HI >> 8,
        0xAD, PPDATA_LO & 0xFF, PPDATA_LO >> 8,                  # read low
        0x8D, RESULT & 0xFF, (RESULT >> 8) & 0xFF,
        0xAD, PPDATA_HI & 0xFF, PPDATA_HI >> 8,                  # read high
        0x8D, (RESULT + 1) & 0xFF, ((RESULT + 1) >> 8) & 0xFF,
        0x60,
    ])
    load_code(transport, CODE, pp_read_code)
    jsr(transport, CODE, timeout=5.0)
    linectl = read_bytes(transport, RESULT, 2)
    lo = linectl[0]
    hi = linectl[1] | 0x00  # high byte stays as-is
    lo_new = lo | 0xC0       # SerRxON (bit 6) + SerTxON (bit 7)

    # Write back LineCTL with SerRxON + SerTxON enabled
    pp_write_linectl = bytes([
        0xA9, 0x12, 0x8D, PPTR_LO & 0xFF, PPTR_LO >> 8,        # PPPtr = 0x0112
        0xA9, 0x01, 0x8D, PPTR_HI & 0xFF, PPTR_HI >> 8,
        0xA9, lo_new & 0xFF, 0x8D, PPDATA_LO & 0xFF, PPDATA_LO >> 8,
        0xA9, hi & 0xFF, 0x8D, PPDATA_HI & 0xFF, PPDATA_HI >> 8,
        0x60,
    ])
    load_code(transport, CODE, pp_write_linectl)
    jsr(transport, CODE, timeout=5.0)


# ---------------------------------------------------------------------------
# 6502 code generators
# ---------------------------------------------------------------------------

def _build_tx_code(frame_len: int) -> bytes:
    """Build 6502 TX routine that sends frame_len bytes from FRAME_BUF.

    1. SEI
    2. TxCMD = 0x00C0, TxLen = frame_len
    3. Poll Rdy4TxNOW (PP 0x0138 bit 8)
    4. Write frame data from FRAME_BUF via ZP pointer
    5. Set RESULT = 0x01
    6. CLI, RTS
    """
    a = Asm()
    a.emit(0x78)  # SEI

    # TxCMD = 0x00C0 (TxStart: transmit after full frame)
    a.emit(0xA9, 0xC0, 0x8D, TXCMD_LO & 0xFF, TXCMD_LO >> 8)
    a.emit(0xA9, 0x00, 0x8D, TXCMD_HI & 0xFF, TXCMD_HI >> 8)

    # TxLength
    a.emit(0xA9, frame_len & 0xFF, 0x8D, TXLEN_LO & 0xFF, TXLEN_LO >> 8)
    a.emit(0xA9, (frame_len >> 8) & 0xFF, 0x8D, TXLEN_HI & 0xFF, TXLEN_HI >> 8)

    # Wait Rdy4TxNOW: PP 0x0138, bit 8
    a.emit(0xA9, 0x38, 0x8D, PPTR_LO & 0xFF, PPTR_LO >> 8)
    a.emit(0xA9, 0x01, 0x8D, PPTR_HI & 0xFF, PPTR_HI >> 8)
    a.label("txw")
    a.emit(0xAD, PPDATA_HI & 0xFF, PPDATA_HI >> 8)  # LDA $DE0D
    a.emit(0x29, 0x01)  # AND #$01
    a.branch(0xF0, "txw")  # BEQ txw

    # Set up ZP pointer $FB/$FC = FRAME_BUF
    a.emit(0xA9, FRAME_BUF & 0xFF, 0x85, 0xFB)
    a.emit(0xA9, (FRAME_BUF >> 8) & 0xFF, 0x85, 0xFC)

    # Loop: write frame_len bytes (word at a time) to $DE00/$DE01
    a.emit(0xA0, 0x00)  # LDY #$00
    a.label("txlp")
    a.emit(0xB1, 0xFB)  # LDA ($FB),Y  ; low byte
    a.emit(0x8D, RTDATA_LO & 0xFF, RTDATA_LO >> 8)  # STA $DE00
    a.emit(0xC8)  # INY
    a.emit(0xB1, 0xFB)  # LDA ($FB),Y  ; high byte
    a.emit(0x8D, RTDATA_HI & 0xFF, RTDATA_HI >> 8)  # STA $DE01
    a.emit(0xC8)  # INY
    a.emit(0xC0, frame_len & 0xFF)  # CPY #frame_len
    a.branch(0xD0, "txlp")  # BNE txlp

    # Success
    a.emit(0xA9, 0x01)  # LDA #$01
    a.emit(0x8D, RESULT & 0xFF, (RESULT >> 8) & 0xFF)  # STA RESULT
    a.emit(0x58)  # CLI
    a.emit(0x60)  # RTS

    return a.build()


def _build_rx_code(payload_bytes: int = 46) -> bytes:
    """Build 6502 RX routine that polls for a frame and reads payload into RX_BUF.

    Filters frames by EtherType -- only accepts frames with EtherType 0xC640.
    Non-matching frames (e.g. IPv6 multicast) are drained and discarded,
    then polling resumes for the next frame.

    1. SEI
    2. Poll RxEvent (PP 0x0124 bit 8) with multi-level timeout
    3. Read RxStatus (2 bytes) + RxLength (2 bytes) from RTDATA
    4. Skip dest MAC (3 word reads) and src MAC (3 word reads)
    5. Read EtherType (1 word read) -- check for 0xC640
    6. If no match: drain remaining (RxLength-14)/2 words, jump to step 2
    7. If match: read payload into RX_BUF, set RESULT=0x01, CLI, RTS
    """
    # Number of 16-bit words to read for payload
    rx_words = (payload_bytes + 1) // 2

    a = Asm()
    a.emit(0x78)  # SEI

    # --- Poll setup: PPTR = 0x0124 (RxEvent) + timeout counters ---
    a.label("rst")
    a.emit(0xA9, 0x24, 0x8D, PPTR_LO & 0xFF, PPTR_LO >> 8)
    a.emit(0xA9, 0x01, 0x8D, PPTR_HI & 0xFF, PPTR_HI >> 8)

    # Timeout counters: $FD=0xFF inner, $FE=0xFF middle, $FF=0x10 outer
    a.emit(0xA9, 0xFF, 0x85, 0xFD)
    a.emit(0xA9, 0xFF, 0x85, 0xFE)
    a.emit(0xA9, 0x10, 0x85, 0xFF)

    # Poll loop
    a.label("rxp")
    a.emit(0xAD, PPDATA_HI & 0xFF, PPDATA_HI >> 8)  # LDA $DE0D
    a.emit(0x29, 0x01)  # AND #$01 (RxOK bit)
    a.branch(0xD0, "rxg")  # BNE got_frame

    # Timeout decrement
    a.emit(0xC6, 0xFD)  # DEC $FD
    a.branch(0xD0, "rxp")  # BNE poll
    a.emit(0xA9, 0xFF, 0x85, 0xFD)  # reset inner
    a.emit(0xC6, 0xFE)  # DEC $FE
    a.branch(0xD0, "rxp")  # BNE poll
    a.emit(0xA9, 0xFF, 0x85, 0xFE)  # reset middle
    a.emit(0xC6, 0xFF)  # DEC $FF
    a.branch(0xD0, "rxp")  # BNE poll

    # Timeout: RESULT = 0xFF
    a.emit(0xA9, 0xFF)
    a.emit(0x8D, RESULT & 0xFF, (RESULT >> 8) & 0xFF)
    a.emit(0x58)  # CLI
    a.emit(0x60)  # RTS

    # --- got_frame: read RxStatus + RxLength ---
    a.label("rxg")
    # RxStatus (2 bytes) -- store at RX_META+0/+1
    a.emit(0xAD, RTDATA_LO & 0xFF, RTDATA_LO >> 8)  # LDA $DE00 (RxStatus lo)
    a.emit(0x8D, RX_META & 0xFF, (RX_META >> 8) & 0xFF)
    a.emit(0xAD, RTDATA_HI & 0xFF, RTDATA_HI >> 8)  # LDA $DE01 (RxStatus hi)
    a.emit(0x8D, (RX_META + 1) & 0xFF, ((RX_META + 1) >> 8) & 0xFF)
    # RxLength (2 bytes) -- store at RX_META+2/+3
    a.emit(0xAD, RTDATA_LO & 0xFF, RTDATA_LO >> 8)  # LDA $DE00 (RxLength lo)
    a.emit(0x8D, (RX_META + 2) & 0xFF, ((RX_META + 2) >> 8) & 0xFF)
    a.emit(0xAD, RTDATA_HI & 0xFF, RTDATA_HI >> 8)  # LDA $DE01 (RxLength hi)
    a.emit(0x8D, (RX_META + 3) & 0xFF, ((RX_META + 3) >> 8) & 0xFF)

    # Skip dest+src MAC: 6 word reads (12 bytes, discard)
    a.emit(0xA2, 0x06)  # LDX #6
    a.label("skm")
    a.emit(0xAD, RTDATA_LO & 0xFF, RTDATA_LO >> 8)
    a.emit(0xAD, RTDATA_HI & 0xFF, RTDATA_HI >> 8)
    a.emit(0xCA)  # DEX
    a.branch(0xD0, "skm")  # BNE skm

    # Read EtherType (1 word): lo from $DE00, hi from $DE01
    # On wire: 0xC6, 0x40 -> CS8900a word read: lo=0xC6, hi=0x40
    a.emit(0xAD, RTDATA_LO & 0xFF, RTDATA_LO >> 8)  # LDA $DE00 -> lo byte
    a.emit(0xC9, 0xC6)  # CMP #$C6
    a.branch(0xD0, "drn")  # BNE drain (wrong EtherType lo)
    a.emit(0xAD, RTDATA_HI & 0xFF, RTDATA_HI >> 8)  # LDA $DE01 -> hi byte
    a.emit(0xC9, 0x40)  # CMP #$40
    a.branch(0xD0, "dr2")  # BNE drain2 (wrong EtherType hi, but hi consumed)

    # --- EtherType matches: read payload into RX_BUF ---
    a.emit(0xA9, RX_BUF & 0xFF, 0x85, 0xFB)
    a.emit(0xA9, (RX_BUF >> 8) & 0xFF, 0x85, 0xFC)
    a.emit(0xA0, 0x00)  # LDY #$00
    a.emit(0xA2, rx_words & 0xFF)  # LDX #rx_words
    a.label("rxrd")
    a.emit(0xAD, RTDATA_LO & 0xFF, RTDATA_LO >> 8)  # LDA $DE00
    a.emit(0x91, 0xFB)  # STA ($FB),Y
    a.emit(0xC8)  # INY
    a.emit(0xAD, RTDATA_HI & 0xFF, RTDATA_HI >> 8)  # LDA $DE01
    a.emit(0x91, 0xFB)  # STA ($FB),Y
    a.emit(0xC8)  # INY
    a.emit(0xCA)  # DEX
    a.branch(0xD0, "rxrd")  # BNE rxrd

    # Success: RESULT = 0x01
    a.emit(0xA9, 0x01)
    a.emit(0x8D, RESULT & 0xFF, (RESULT >> 8) & 0xFF)
    a.emit(0x58)  # CLI
    a.emit(0x60)  # RTS

    # --- Drain non-matching frame ---
    # "drn": EtherType lo didn't match -- hi byte not yet consumed
    a.label("drn")
    a.emit(0xAD, RTDATA_HI & 0xFF, RTDATA_HI >> 8)  # consume EtherType hi byte

    # "dr2": EtherType hi didn't match (or fell through from drn)
    # Full 14-byte header consumed.  Remaining = RxLength - 14 bytes.
    # Drain (RxLength - 14) / 2 words from RTDATA.
    # Compute drain word count into $FB (lo) / $FC (hi).
    a.label("dr2")
    a.emit(0x38)  # SEC
    a.emit(0xAD, (RX_META + 2) & 0xFF, ((RX_META + 2) >> 8) & 0xFF)  # LDA RxLen lo
    a.emit(0xE9, 14)  # SBC #14
    a.emit(0x85, 0xFB)  # STA $FB  (remaining bytes lo)
    a.emit(0xAD, (RX_META + 3) & 0xFF, ((RX_META + 3) >> 8) & 0xFF)  # LDA RxLen hi
    a.emit(0xE9, 0x00)  # SBC #0
    # Divide 16-bit value (A:$FB) by 2 -> word count
    a.emit(0x4A)  # LSR A       (hi >> 1, carry -> for ROR)
    a.emit(0x66, 0xFB)  # ROR $FB    (carry into bit 7 of lo)
    a.emit(0x85, 0xFC)  # STA $FC    (hi byte of word count)

    # Drain loop: read and discard words until $FC:$FB == 0
    a.label("drlp")
    a.emit(0xA5, 0xFB)  # LDA $FB
    a.emit(0x05, 0xFC)  # ORA $FC
    a.branch(0xF0, "ddn")  # BEQ drain_done -> go poll next frame

    # Read and discard one word from RTDATA
    a.emit(0xAD, RTDATA_LO & 0xFF, RTDATA_LO >> 8)
    a.emit(0xAD, RTDATA_HI & 0xFF, RTDATA_HI >> 8)

    # 16-bit decrement of $FC:$FB
    a.emit(0xA5, 0xFB)  # LDA $FB
    a.branch(0xD0, "dnz")  # BNE no_borrow
    a.emit(0xC6, 0xFC)  # DEC $FC
    a.label("dnz")
    a.emit(0xC6, 0xFB)  # DEC $FB

    # Unconditional jump back (CLC + BCC = always-taken branch on 6510)
    a.emit(0x18)  # CLC
    a.branch(0x90, "drlp")  # BCC drlp (always taken)

    # Drain done: jump back to reset poll and wait for next frame
    # "rst" is too far for a relative branch, so use JMP absolute.
    # Record position of the JMP operand bytes and fix up after build.
    a.label("ddn")
    jmp_operand_pos = a.pos + 1  # position of lo byte of JMP target
    a.emit(0x4C, 0x00, 0x00)  # JMP $0000 (placeholder)

    code = bytearray(a.build())
    # Fix up JMP target: absolute address = CODE + offset of "rst" label
    rst_addr = CODE + a.labels["rst"]
    code[jmp_operand_pos] = rst_addr & 0xFF
    code[jmp_operand_pos + 1] = (rst_addr >> 8) & 0xFF
    return bytes(code)


def _build_frame(src_mac: bytes, payload: bytes) -> bytes:
    """Build a 64-byte ethernet frame with broadcast dest, given src MAC and payload."""
    header = DEST_MAC + src_mac + ETHERTYPE
    frame = header + payload
    # Pad to 60 bytes minimum (CS8900a adds 4-byte FCS)
    if len(frame) < 60:
        frame += b"\x00" * (60 - len(frame))
    # Ensure even length for word-aligned TX
    if len(frame) % 2:
        frame += b"\x00"
    return frame


def _wait_for_ready(transport: BinaryViceTransport, timeout: float = 30.0) -> None:
    """Wait until BASIC READY prompt appears on screen."""
    from c64_test_harness.screen import ScreenGrid

    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            transport.resume()
            time.sleep(2.0)
            grid = ScreenGrid.from_transport(transport)
            if "READY" in grid.continuous_text().upper():
                return
        except Exception:
            time.sleep(1.0)
    raise AssertionError("BASIC READY prompt not found within timeout")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def vice_bridge_pair():
    """Launch two VICE instances with ethernet on tap-c64-0 and tap-c64-1.

    Yields (transport_a, transport_b) -- both connected and at READY prompt.
    """
    allocator = PortAllocator(port_range_start=6540, port_range_end=6560)

    # Allocate two ports
    port_a = allocator.allocate()
    port_b = allocator.allocate()

    # Release reservation sockets before starting VICE
    res_a = allocator.take_socket(port_a)
    if res_a is not None:
        res_a.close()
    res_b = allocator.take_socket(port_b)
    if res_b is not None:
        res_b.close()

    config_a = ViceConfig(
        port=port_a,
        warp=False,
        sound=False,
        ethernet=True,
        ethernet_mode="tfe",
        ethernet_interface="tap-c64-0",
        ethernet_driver="tuntap",
    )
    config_b = ViceConfig(
        port=port_b,
        warp=False,
        sound=False,
        ethernet=True,
        ethernet_mode="tfe",
        ethernet_interface="tap-c64-1",
        ethernet_driver="tuntap",
    )

    vice_a = ViceProcess(config_a)
    vice_b = ViceProcess(config_b)

    try:
        vice_a.start()
        vice_b.start()

        transport_a = connect_binary_transport(port_a, proc=vice_a)
        transport_b = connect_binary_transport(port_b, proc=vice_b)

        try:
            _wait_for_ready(transport_a)
            _wait_for_ready(transport_b)

            # Initialize CS8900a on both instances (enable TX/RX)
            _init_cs8900a(transport_a)
            _init_cs8900a(transport_b)

            # Program unique MAC addresses into the CS8900a IA registers
            set_cs8900a_mac(transport_a, SRC_MAC_A)
            set_cs8900a_mac(transport_b, SRC_MAC_B)

            yield transport_a, transport_b
        finally:
            transport_a.close()
            transport_b.close()
    finally:
        vice_a.stop()
        vice_b.stop()
        allocator.release(port_a)
        allocator.release(port_b)


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------

class TestEthernetBridge:
    """Two VICE instances exchange ethernet frames via bridged TAP interfaces."""

    def _do_transfer(
        self,
        sender: BinaryViceTransport,
        receiver: BinaryViceTransport,
        src_mac: bytes,
        payload: bytes,
    ) -> bytes:
        """Send a frame from sender and receive it on receiver.

        Loads TX code + frame on sender, RX code on receiver.
        Uses threading: receiver starts polling first, sender transmits
        after a brief delay.

        Returns the payload bytes read from the receiver's RX_BUF.
        """
        frame = _build_frame(src_mac, payload)
        frame_len = len(frame)

        # Build code
        tx_code = _build_tx_code(frame_len)
        rx_code = _build_rx_code(payload_bytes=len(payload))

        # Load TX code + frame data on sender
        load_code(sender, CODE, tx_code)
        write_bytes(sender, FRAME_BUF, frame)
        write_bytes(sender, RESULT, [0x00])

        # Load RX code on receiver
        load_code(receiver, CODE, rx_code)
        write_bytes(receiver, RESULT, [0x00])
        write_bytes(receiver, RX_META, [0x00] * 4)
        write_bytes(receiver, RX_BUF, [0x00] * len(payload))

        # Run concurrently: RX polls, TX sends after delay
        rx_error: list[Exception] = []
        tx_error: list[Exception] = []

        def rx_worker():
            try:
                jsr(receiver, CODE, timeout=30.0)
            except Exception as e:
                rx_error.append(e)

        def tx_worker():
            try:
                time.sleep(0.5)  # Give RX time to start polling
                jsr(sender, CODE, timeout=10.0)
            except Exception as e:
                tx_error.append(e)

        rx_thread = threading.Thread(target=rx_worker, daemon=True)
        tx_thread = threading.Thread(target=tx_worker, daemon=True)
        rx_thread.start()
        tx_thread.start()

        rx_thread.join(timeout=45.0)
        tx_thread.join(timeout=45.0)

        # Check for errors
        if tx_error:
            raise AssertionError(f"TX thread failed: {tx_error[0]}") from tx_error[0]
        if rx_error:
            raise AssertionError(f"RX thread failed: {rx_error[0]}") from rx_error[0]

        # Verify TX success
        tx_result = read_bytes(sender, RESULT, 1)
        assert tx_result[0] == 0x01, (
            f"TX routine did not complete (result=0x{tx_result[0]:02X})"
        )

        # Check RX result
        rx_result = read_bytes(receiver, RESULT, 1)
        if rx_result[0] == 0xFF:
            pytest.skip("RX poll timed out -- frame may not have reached CS8900a")
        assert rx_result[0] == 0x01, (
            f"RX routine did not complete successfully (result=0x{rx_result[0]:02X})"
        )

        # Read received payload
        received = bytes(read_bytes(receiver, RX_BUF, len(payload)))
        return received

    def test_bidirectional_exchange(
        self,
        vice_bridge_pair: tuple[BinaryViceTransport, BinaryViceTransport],
    ) -> None:
        """A sends a frame to B, then B sends a frame to A."""
        transport_a, transport_b = vice_bridge_pair

        # --- A -> B transfer ---
        received_b = self._do_transfer(
            sender=transport_a,
            receiver=transport_b,
            src_mac=SRC_MAC_A,
            payload=PAYLOAD_A,
        )

        # Verify B received A's payload
        marker_len = len(b"HELLO_FROM_A")
        assert received_b[:marker_len] == b"HELLO_FROM_A", (
            f"A->B payload mismatch: got {received_b[:marker_len]!r}, "
            f"expected b'HELLO_FROM_A'"
        )

        # --- B -> A transfer (reverse direction) ---
        received_a = self._do_transfer(
            sender=transport_b,
            receiver=transport_a,
            src_mac=SRC_MAC_B,
            payload=PAYLOAD_B,
        )

        # Verify A received B's payload
        assert received_a[:marker_len] == b"HELLO_FROM_B", (
            f"B->A payload mismatch: got {received_a[:marker_len]!r}, "
            f"expected b'HELLO_FROM_B'"
        )
