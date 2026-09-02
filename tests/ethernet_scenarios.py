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
from typing import Any, Mapping

from c64_test_harness.bridge_ping import cs8900a_enable_inline_code
from c64_test_harness.disasm import disassemble
from c64_test_harness.capture import (
    CaptureTimeout,
    CaptureUnavailable,
    PacketCapture,
    bpf_descriptor_summary,
)
from c64_test_harness.execute import load_code
from c64_test_harness.memory import read_bytes, write_bytes
from c64_test_harness.transport import TimeoutError as TransportTimeoutError

# Scratch area.  The routine is loaded at CODE_BASE; every result byte it
# stores and every byte the host clears lives in DATA_BASE.  They used to
# share $C000, and "clearing the flag" after load_code() zeroed the first
# opcode into BRK -- the C64 never returned (live, 2026-09-01).
CODE_BASE = 0xC000
DATA_BASE = 0xC100
#: TX: success flag.  RX: marker bytes at +0..+3, success flag at +4.
RESULT = DATA_BASE

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


def capture_failure_disposition(exc: CaptureUnavailable, *, iface: str) -> tuple[str, str]:
    """``("skip" | "fail", reason)`` for a fixture that could not open a capture.

    Skip only when the exception says the capability is *genuinely absent*
    on this host (see ``GENUINELY_ABSENT_CAUSES``); the reason is the
    exception's message, which carries the remedy.  Anything else -- the
    pool eaten while VICE is live, a bind that failed on an interface the
    platform helper just found, a non-ethernet DLT, a cause nobody has
    classified -- is a path that exists and is broken, and a skip there is
    exactly how issue #158 stayed hidden.  Those fail, with the same
    remedy in the message.
    """
    if exc.genuinely_absent:
        return "skip", f"no host-side capture on {iface}: {exc}"
    return (
        "fail",
        f"host-side capture on {iface} exists but could not be opened "
        f"(cause={exc.cause}; this is not absence, so the test fails rather "
        f"than skips): {exc}",
    )


#: Bind the host capture (TX side) to this interface instead of VICE's.
CAPTURE_IFACE_ENV = "C64_ETH_CAPTURE_IFACE"
#: Write the RX frame to this interface instead of the capture's.
SEND_IFACE_ENV = "C64_ETH_SEND_IFACE"


def resolve_capture_ifaces(vice_iface: str, env: Mapping[str, str]) -> tuple[str, str]:
    """``(capture_iface, send_iface)`` for the host side, from *env* overrides.

    Default: both are *vice_iface*.  ``C64_ETH_CAPTURE_IFACE`` moves the
    capture (and, unless ``C64_ETH_SEND_IFACE`` is also set, the send) to
    another interface -- the peer of a feth pair when the live TX run
    shows VICE's descriptor at Written=1 with nothing captured, i.e. the
    frame left on the interface but our capture was on the wrong side.
    Blank values are ignored.
    """
    def _get(name: str) -> str | None:
        v = env.get(name, "")
        v = v.strip() if isinstance(v, str) else ""
        return v or None

    capture_iface = _get(CAPTURE_IFACE_ENV) or vice_iface
    send_iface = _get(SEND_IFACE_ENV) or capture_iface
    return capture_iface, send_iface


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
        try:
            transport.wait_for_stopped(timeout=timeout)
        except (TimeoutError, TransportTimeoutError) as e:
            # BinaryViceTransport raises the transport's TimeoutError, a
            # TransportError and not the builtin; catch both.
            raise AssertionError(jsr_timeout_report(transport, addr, timeout)) from e
        regs = transport.read_registers()
        return regs
    finally:
        transport.delete_checkpoint(bp_num)


def jsr_timeout_report(transport: Any, addr: int, timeout: float, *, window: int = 8) -> str:
    """Where the 6502 is when a JSR to *addr* did not return.

    Registers, a disassembly window around PC with the PC line marked,
    the CS8900a I/O window $DE00-$DE0F, and the first bytes of the routine
    as they are in RAM *now* -- which is how a clobbered first opcode
    (``00 BRK``) shows itself.
    """
    regs = transport.read_registers()
    pc = regs.get("PC", 0) & 0xFFFF
    lines = [
        f"6502 did not return from ${addr:04X} within {timeout:.1f}s.",
        "regs: " + " ".join(
            f"{k}=${regs[k]:0{4 if k == 'PC' else 2}X}"
            for k in ("PC", "A", "X", "Y", "SP") if k in regs
        ),
    ]
    # Decode from an instruction boundary: the routine start when PC is
    # inside the routine (a decode begun mid-instruction never aligns onto
    # PC and the marker is lost), else from PC itself.
    start = addr if addr <= pc < addr + 0x100 else pc
    mem = transport.read_memory(start, (pc - start) + window * 3)
    decoded = disassemble(mem, start)
    at_pc = next((i for i, l in enumerate(decoded) if int(l[:4], 16) == pc), None)
    if at_pc is None:
        # PC is not on a boundary of the code as loaded: the CPU is
        # executing a different instruction stream through these bytes.
        # Show the loaded decode for context, then decode from PC itself.
        lines.append(f"  (PC ${pc:04X} is not an instruction boundary of the code at ${start:04X})")
        lines.extend("  " + l for l in decoded[:window])
        from_pc = disassemble(transport.read_memory(pc, window * 3), pc)
        lines.extend(("> " if i == 0 else "  ") + l for i, l in enumerate(from_pc[:window]))
    else:
        for line in decoded[max(0, at_pc - window // 2):at_pc + window]:
            lines.append(("> " if int(line[:4], 16) == pc else "  ") + line)
    io = transport.read_memory(0xDE00, 16)
    lines.append(f"$DE00: {bytes(io).hex(' ')}")
    code = transport.read_memory(addr, 16)
    lines.append(f"code@${addr:04X}: {bytes(code).hex(' ')}")
    return "\n".join(lines)


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

        # Success flag -> RESULT
        0xA9, 0x01,
        0x8D, RESULT & 0xFF, RESULT >> 8,
        0x60,
    ])


def rx_routine() -> bytes:
    """6502 RX routine: poll RxEvent, read the frame, store 4 marker bytes.

    1. Poll RxEvent (PP 0x0124) for RxOK (bit 8)
    2. Read RxStatus from RTDATA
    3. Read RxLength from RTDATA
    4. Read first 4 payload bytes (skip 14-byte header = 7 word reads)
    5. Store marker at RESULT+0..+3, success flag 0x01 at RESULT+4

    A 16-bit timeout counter avoids an infinite loop; on timeout RESULT+0
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

        # Timeout -> RESULT+0 = 0xFF
        0xA9, 0xFF, 0x8D, RESULT & 0xFF, RESULT >> 8, 0x60,

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

        # Read 4 marker bytes (2 word reads) -> RESULT+0..+3
        0xAD, rtd_lo, 0xDE,
        0x8D, (RESULT + 0) & 0xFF, RESULT >> 8,
        0xAD, rtd_h_lo, 0xDE,
        0x8D, (RESULT + 1) & 0xFF, RESULT >> 8,
        0xAD, rtd_lo, 0xDE,
        0x8D, (RESULT + 2) & 0xFF, RESULT >> 8,
        0xAD, rtd_h_lo, 0xDE,
        0x8D, (RESULT + 3) & 0xFF, RESULT >> 8,

        # Success flag -> RESULT+4
        0xA9, 0x01, 0x8D, (RESULT + 4) & 0xFF, RESULT >> 8, 0x60,
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
    write_bytes(transport, RESULT, [0x00])  # clear success flag (never inside the code)
    load_code(transport, CODE_BASE, tx_routine())

    binary_jsr(transport, CODE_BASE, timeout=10)

    flag = read_bytes(transport, RESULT, 1)
    if flag[0] != 0x01:
        raise AssertionError(
            f"TX routine did not complete (success flag 0x{flag[0]:02X}, expected 0x01)"
        )

    try:
        captured = capture.recv(timeout, match=is_test_frame)
    except CaptureTimeout as e:
        # State what was measured; the netstat -B counters on VICE's own
        # descriptor say which side lost the frame (Written=1: the chip
        # handed it to pcap and our capture is on the wrong side or
        # direction; Written=0: it died inside the emulated CS8900a).
        raise AssertionError(
            f"no frame with ethertype {ETHERTYPE.hex()} captured on {capture.iface} "
            f"within {timeout:.1f}s ({e.seen} non-matching seen); the C64 routine "
            f"reported Rdy4TxNOW and wrote all {FRAME_LEN} bytes. "
            f"{bpf_descriptor_summary(capture.iface)}"
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
    send_capture: PacketCapture | None = None,
    send_delay: float = 0.5,
    timeout: float = 15.0,
    join_timeout: float = 2.0,
) -> bytes:
    """Host injects RX_FRAME; the C64 must receive it.

    The frame is written through *send_capture* (default: *capture*
    itself -- see :func:`resolve_capture_ifaces` for when the two
    differ).  The RX routine polls the CS8900a; a thread sends the frame
    after *send_delay* so the poll is already running.  Returns the five
    result bytes (RESULT+0..+4: marker, flag).  Raises
    :class:`AssertionError` if the host send failed, if the C64's poll
    timed out (the frame never reached the chip), or if the marker read
    back differs.
    """
    sender_cap = send_capture if send_capture is not None else capture
    write_bytes(transport, RESULT, [0x00] * 5)  # clear result area (never inside the code)
    load_code(transport, CODE_BASE, rx_routine())

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
            sender_cap.send(RX_FRAME)
        except BaseException as e:  # reported below, never swallowed
            send_error.append(e)

    sender = threading.Thread(target=_send_packet_delayed, daemon=True)
    sender.start()
    cpu_report: str | None = None
    try:
        transport.set_registers({"PC": scratch_addr})
        transport.resume()
        try:
            transport.wait_for_stopped(timeout=timeout)
        except (TimeoutError, TransportTimeoutError):
            # Same report binary_jsr gives: where the 6502 is, not just
            # that it did not come back.
            cpu_report = jsr_timeout_report(transport, CODE_BASE, timeout)
        else:
            transport.read_registers()
    finally:
        transport.delete_checkpoint(bp_num)
    sender.join(timeout=join_timeout)

    if cpu_report is not None:
        if send_error:
            sent = f"host send on {sender_cap.iface} failed: {send_error[0]!r}"
        elif sender.is_alive():
            sent = f"host send on {sender_cap.iface} did not return"
        else:
            sent = f"host wrote {len(RX_FRAME)} bytes to {sender_cap.iface}"
        raise AssertionError(f"{cpu_report}\n({sent}) {bpf_descriptor_summary(capture.iface)}")

    if send_error:
        raise AssertionError(
            f"host-side send on {sender_cap.iface} failed, so no frame ever left the "
            f"host: {send_error[0]!r}"
        )
    if sender.is_alive():
        raise AssertionError(f"host-side send on {sender_cap.iface} did not return")

    result = read_bytes(transport, RESULT, 5)
    success = result[4]
    if result[0] == 0xFF and success != 0x01:
        sides = [sender_cap.iface]
        if capture.iface != sender_cap.iface:
            sides.append(capture.iface)
        raise AssertionError(
            f"C64 poll for RxOK timed out (result {result.hex()}) after the host "
            f"wrote {len(RX_FRAME)} bytes to {sender_cap.iface}; the frame never reached "
            f"the CS8900a. " + " | ".join(bpf_descriptor_summary(i) for i in sides)
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
