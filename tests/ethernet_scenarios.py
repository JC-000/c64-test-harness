"""CS8900a (RR-Net) TX/RX scenarios, parameterised on a host capture.

The bodies of ``tests/test_ethernet.py``'s two capture-dependent tests
live here so that they can be driven twice: against a live VICE with a
real :class:`~c64_test_harness.capture.PacketCapture`, and against a fake
transport and fake capture in ``tests/test_ethernet_capture_wiring.py``
to prove the wiring without an emulator.  ``test_ethernet.py`` cannot be
imported for that purpose -- its module scope probes VICE.

Both scenarios raise plain :class:`AssertionError` on every way the wire
can disagree with the C64, and never skip.  Issue #158's acceptance is
that "no packet captured" is a failure; the previous versions turned an
RX timeout into ``pytest.skip`` and swallowed a failed host send with
``except OSError: pass``, so an RX test whose frame never left the host
reported "packet may not have reached CS8900a" and skipped.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from c64_test_harness.bridge_ping import cs8900a_enable_inline_code
from c64_test_harness.capture import CaptureTimeout, PacketCapture
from c64_test_harness.execute import load_code
from c64_test_harness.memory import read_bytes, write_bytes

# Scratch area
CODE_BASE = 0xC000
DATA_BASE = 0xC100

# CS8900a I/O registers (RR-Net mode at $DE00).
# Matches ip65 cs8900a.s layout:
#   isq       = $DE00   ; ISQ / RR clockport enable ($DE01 bit 0)
#   packetpp  = $DE02   ; PPPtr (16-bit)
#   ppdata    = $DE04   ; PPData (16-bit)
#   rxtxreg   = $DE08   ; RX/TX data FIFO (16-bit)
#   txcmd     = $DE0C   ; TX command
#   txlen     = $DE0E   ; TX length
#
# CRITICAL: the RR clockport MUST be enabled (set bit 0 of $DE01) before
# any CS8900a register access, or the chip ignores all reads/writes.
CS8900A_BASE = 0xDE00
ISQ_LO = CS8900A_BASE + 0x00
ISQ_HI = CS8900A_BASE + 0x01    # bit 0 = RR clockport enable
PPTR = CS8900A_BASE + 0x02      # PacketPage Pointer (16-bit)
PPDATA = CS8900A_BASE + 0x04    # PacketPage Data (16-bit)
RTDATA = CS8900A_BASE + 0x08    # RX/TX data FIFO (16-bit)
TXCMD = CS8900A_BASE + 0x0C     # TX command (16-bit)
TXLEN = CS8900A_BASE + 0x0E     # TX length (16-bit)

# Frame constants
DEST_MAC = b"\xFF\xFF\xFF\xFF\xFF\xFF"  # broadcast
SRC_MAC = b"\x00\x00\x00\x00\x00\x01"  # arbitrary
ETHERTYPE = b"\x88\xB5"                  # local experimental
FRAME_LEN = 64
# Payload: fill with 0xC6, 0x40 ("C", "6" in a loose sense) repeated
PAYLOAD_LEN = FRAME_LEN - 14  # 14 = 6+6+2 header
PAYLOAD = bytes([0xC6, 0x40] * (PAYLOAD_LEN // 2))
FRAME_DATA = DEST_MAC + SRC_MAC + ETHERTYPE + PAYLOAD

# Buffer location in C64 RAM for the frame
FRAME_BUF = 0xC200

# The frame the host injects for the RX scenario.
RX_SRC_MAC = b"\x00\x00\x00\x00\x00\x02"
RX_MARKER = b"\xDE\xAD\xBE\xEF"
RX_FRAME = (
    DEST_MAC + RX_SRC_MAC + ETHERTYPE
    + RX_MARKER + b"\x00" * (FRAME_LEN - 14 - len(RX_MARKER))
)


def is_test_frame(frame: bytes) -> bool:
    """Whether *frame* carries our experimental ethertype (filters strays)."""
    return frame[12:14] == ETHERTYPE


def clockport_enable_code() -> bytes:
    """6502 snippet: enable RR clockport bit (ORA #$01 at $DE01).

    Must be prepended to every CS8900a access routine.  Without it, the
    chip silently drops all register reads and writes.
    """
    return bytes([
        0xAD, ISQ_HI & 0xFF, ISQ_HI >> 8,   # LDA $DE01
        0x09, 0x01,                          # ORA #$01
        0x8D, ISQ_HI & 0xFF, ISQ_HI >> 8,   # STA $DE01
    ])


def binary_jsr(
    transport: Any,
    addr: int,
    timeout: float = 10.0,
    scratch_addr: int = 0x0334,
) -> dict[str, int]:
    """JSR via binary monitor checkpoint mechanism.

    Writes a trampoline (JSR addr; NOP; NOP) at *scratch_addr*, sets a
    checkpoint (breakpoint) at scratch_addr+3, sets PC to scratch_addr,
    resumes, and waits for the CPU to stop at the breakpoint.

    Returns the register state after the subroutine returns.
    """
    trampoline = bytes([
        0x20, addr & 0xFF, (addr >> 8) & 0xFF,  # JSR addr
        0xEA,  # NOP (breakpoint here)
        0xEA,  # NOP
    ])
    transport.write_memory(scratch_addr, trampoline)
    bp_addr = scratch_addr + 3
    bp_num = transport.set_checkpoint(bp_addr)
    try:
        transport.set_registers({"PC": scratch_addr})
        transport.resume()
        transport.wait_for_stopped(timeout=timeout)
        regs = transport.read_registers()
        return regs
    finally:
        transport.delete_checkpoint(bp_num)


def tx_routine() -> bytes:
    """6502 TX routine (RR-Net register layout): send FRAME_LEN bytes from FRAME_BUF.

    Begins with the chip enable (RxCTL, then LineCTL |= SerRxON|SerTxON):
    VICE's cs8900.c accepts TxLength and raises Rdy4TxNOW whether or not
    TX is enabled, then drops the frame at transmit time if it is not, so
    a routine without this "succeeds" on the C64 side and never reaches
    the wire.
    """
    return cs8900a_enable_inline_code() + clockport_enable_code() + bytes([
        # TxCMD = 0x00C0 at $DE0C/$DE0D
        0xA9, 0xC0,
        0x8D, TXCMD & 0xFF, TXCMD >> 8,
        0xA9, 0x00,
        0x8D, (TXCMD + 1) & 0xFF, (TXCMD + 1) >> 8,

        # TxLength = 64 at $DE0E/$DE0F
        0xA9, 0x40,
        0x8D, TXLEN & 0xFF, TXLEN >> 8,
        0xA9, 0x00,
        0x8D, (TXLEN + 1) & 0xFF, (TXLEN + 1) >> 8,

        # PPPtr = 0x0138 (BusST)
        0xA9, 0x38,
        0x8D, PPTR & 0xFF, PPTR >> 8,
        0xA9, 0x01,
        0x8D, (PPTR + 1) & 0xFF, (PPTR + 1) >> 8,
        # Poll PPData hi (bit 0 = Rdy4TxNOW)
        0xAD, (PPDATA + 1) & 0xFF, (PPDATA + 1) >> 8,
        0x29, 0x01,
        0xF0, 0xF9,  # BEQ back -7

        # ZP $FB/$FC = FRAME_BUF
        0xA9, FRAME_BUF & 0xFF, 0x85, 0xFB,
        0xA9, (FRAME_BUF >> 8) & 0xFF, 0x85, 0xFC,

        # Write 64 bytes to RTDATA ($DE08/$DE09)
        0xA0, 0x00,
        # .loop:
        0xB1, 0xFB,
        0x8D, RTDATA & 0xFF, RTDATA >> 8,
        0xC8,
        0xB1, 0xFB,
        0x8D, (RTDATA + 1) & 0xFF, (RTDATA + 1) >> 8,
        0xC8,
        0xC0, 0x40,
        0xD0, 0xF0,  # BNE -16

        # Success
        0xA9, 0x01,
        0x8D, 0x00, 0xC0,
        0x60,
    ])


def rx_routine() -> bytes:
    """6502 RX routine: poll RxEvent, read the frame, store 4 marker bytes.

    1. Poll RxEvent (PP 0x0124) for RxOK (bit 8)
    2. Read RxStatus from RTDATA
    3. Read RxLength from RTDATA
    4. Read first 4 payload bytes (skip 14-byte header = 7 word reads)
    5. Store marker at $C000-$C003, success flag 0x01 at $C004

    A 16-bit timeout counter avoids an infinite loop; on timeout $C000
    is set to 0xFF and the flag is left clear.
    """
    rtd_lo = RTDATA & 0xFF
    rtd_h_lo = (RTDATA + 1) & 0xFF
    # For RR-Net: RTDATA at $DE08/$DE09. The high byte of both addresses
    # is 0xDE so we hard-code it via the constants.
    #
    # The chip enable comes first: without SerRxON the CS8900a never
    # raises RxOK, and without PromiscuousA the reset RxCTL (0x0005, no
    # BroadcastA) filters our broadcast frame out.
    return cs8900a_enable_inline_code() + clockport_enable_code() + bytes([
        # PPPtr = 0x0124 (RxEvent)
        0xA9, 0x24, 0x8D, PPTR & 0xFF, PPTR >> 8,
        0xA9, 0x01, 0x8D, (PPTR + 1) & 0xFF, (PPTR + 1) >> 8,

        # Timeout counter 16-bit at $FD/$FE
        0xA9, 0xFF, 0x85, 0xFD, 0x85, 0xFE,

        # .poll:
        0xAD, (PPDATA + 1) & 0xFF, (PPDATA + 1) >> 8,  # LDA PPData hi
        0x29, 0x01,
        0xD0, 0x0E,  # BNE .got_packet (+14)

        # Decrement timeout
        0xC6, 0xFD, 0xD0, 0xF5,  # DEC $FD; BNE .poll  (-11)
        0xC6, 0xFE, 0xD0, 0xF1,  # DEC $FE; BNE .poll  (-15)

        # Timeout -> $C000 = 0xFF
        0xA9, 0xFF, 0x8D, 0x00, 0xC0, 0x60,

        # .got_packet:
        # Read RxStatus (2 bytes, discard) -- from RTDATA
        0xAD, rtd_lo, 0xDE,
        0xAD, rtd_h_lo, 0xDE,
        # Read RxLength (2 bytes, discard)
        0xAD, rtd_lo, 0xDE,
        0xAD, rtd_h_lo, 0xDE,

        # Skip ethernet header: 14 bytes = 7 word reads
        0xA2, 0x07,
        # .skip:
        0xAD, rtd_lo, 0xDE,
        0xAD, rtd_h_lo, 0xDE,
        0xCA,
        0xD0, 0xF8,  # BNE -8

        # Read 4 marker bytes (2 word reads)
        0xAD, rtd_lo, 0xDE,
        0x8D, 0x00, 0xC0,
        0xAD, rtd_h_lo, 0xDE,
        0x8D, 0x01, 0xC0,
        0xAD, rtd_lo, 0xDE,
        0x8D, 0x02, 0xC0,
        0xAD, rtd_h_lo, 0xDE,
        0x8D, 0x03, 0xC0,

        # Success
        0xA9, 0x01, 0x8D, 0x04, 0xC0, 0x60,
    ])


def run_tx_scenario(transport: Any, capture: PacketCapture, *, timeout: float = 5.0) -> bytes:
    """C64 sends a 64-byte broadcast frame; *capture* must see it on the wire.

    *capture* must already be open and bound before this is called so the
    frame cannot slip past between execution and the first read.
    Returns the captured frame.  Raises :class:`AssertionError` if the
    routine did not complete, if no frame with our ethertype reached
    the wire within *timeout*, or if the frame's bytes disagree.
    """
    write_bytes(transport, FRAME_BUF, FRAME_DATA)
    load_code(transport, CODE_BASE, tx_routine())
    write_bytes(transport, 0xC000, [0x00])  # clear success flag

    binary_jsr(transport, CODE_BASE, timeout=10)

    flag = read_bytes(transport, 0xC000, 1)
    if flag[0] != 0x01:
        raise AssertionError(
            f"TX routine did not complete (success flag 0x{flag[0]:02X}, expected 0x01)"
        )

    try:
        captured = capture.recv(timeout, match=is_test_frame)
    except CaptureTimeout as e:
        raise AssertionError(
            f"the frame the C64 transmitted never reached the wire: no frame with "
            f"ethertype {ETHERTYPE.hex()} captured on {capture.iface} within "
            f"{timeout:.1f}s ({e})"
        ) from e

    if len(captured) < FRAME_LEN:
        raise AssertionError(f"Captured frame too short: {len(captured)} < {FRAME_LEN}")
    if captured[:6] != DEST_MAC:
        raise AssertionError(f"Dest MAC mismatch: {captured[:6].hex()}")
    if captured[6:12] != SRC_MAC:
        raise AssertionError(f"Source MAC mismatch: {captured[6:12].hex()}")
    if captured[12:14] != ETHERTYPE:
        raise AssertionError(f"EtherType mismatch: {captured[12:14].hex()}")
    if captured[14:14 + PAYLOAD_LEN] != PAYLOAD:
        raise AssertionError(
            f"Payload mismatch: {captured[14:18].hex()}... != {PAYLOAD[:4].hex()}..."
        )
    return captured


def run_rx_scenario(
    transport: Any,
    capture: PacketCapture,
    *,
    send_delay: float = 0.5,
    timeout: float = 15.0,
) -> bytes:
    """Host injects RX_FRAME through *capture*; the C64 must receive it.

    The RX routine polls the CS8900a; a thread sends the frame after
    *send_delay* so the poll is already running.  Returns the five
    result bytes ($C000-$C004: marker, flag).  Raises
    :class:`AssertionError` if the host send failed, if the C64's poll
    timed out (the frame never reached the chip), or if the marker read
    back differs.
    """
    load_code(transport, CODE_BASE, rx_routine())
    write_bytes(transport, 0xC000, [0x00] * 5)  # clear result area

    # Trampoline at the scratch area with a breakpoint after the JSR, so
    # the send can happen while the C64 is inside the routine.
    scratch_addr = 0x0334
    trampoline = bytes([
        0x20, CODE_BASE & 0xFF, (CODE_BASE >> 8) & 0xFF,  # JSR CODE_BASE
        0xEA,  # NOP (breakpoint here)
        0xEA,  # NOP
    ])
    transport.write_memory(scratch_addr, trampoline)
    bp_num = transport.set_checkpoint(scratch_addr + 3)

    send_error: list[BaseException] = []

    def _send_packet_delayed() -> None:
        time.sleep(send_delay)
        try:
            capture.send(RX_FRAME)
        except BaseException as e:  # reported below, never swallowed
            send_error.append(e)

    sender = threading.Thread(target=_send_packet_delayed, daemon=True)
    sender.start()
    try:
        transport.set_registers({"PC": scratch_addr})
        transport.resume()
        transport.wait_for_stopped(timeout=timeout)
        transport.read_registers()
    finally:
        transport.delete_checkpoint(bp_num)
    sender.join(timeout=2)

    if send_error:
        raise AssertionError(
            f"host-side send on {capture.iface} failed, so no frame ever left the "
            f"host: {send_error[0]!r}"
        )
    if sender.is_alive():
        raise AssertionError(f"host-side send on {capture.iface} did not return")

    result = read_bytes(transport, 0xC000, 5)
    success = result[4]
    if result[0] == 0xFF and success != 0x01:
        raise AssertionError(
            f"RX poll timed out: the frame put on {capture.iface} never reached the "
            f"CS8900a (result {result.hex()})"
        )
    if success != 0x01:
        raise AssertionError(
            f"RX routine did not complete successfully (flag=0x{success:02X})"
        )
    marker = bytes(result[:4])
    if marker != RX_MARKER:
        raise AssertionError(
            f"RX marker mismatch: got {marker.hex()}, expected {RX_MARKER.hex()}"
        )
    return bytes(result)
