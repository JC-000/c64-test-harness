"""Bridge ICMP ping support for two-VICE bridge tests.

This module provides helpers to ping between two VICE instances that share
a Linux bridge (``br-c64`` + ``tap-c64-0`` + ``tap-c64-1``) with
CS8900a ethernet in RR-Net mode.

The approach is minimal: neither VICE runs a full IP stack.  Instead, a
small 6502 routine in each instance handles one network activity:

* :func:`build_icmp_responder_code` -- 6502 routine that polls the CS8900a
  RX queue, receives one frame, checks if it is an ICMP echo request
  addressed to our IP, transforms it into an ICMP echo reply (swap MACs,
  swap IPs, set type=0, adjust ICMP checksum), and transmits it back.

* :func:`build_rx_echo_reply_code` -- 6502 routine that polls CS8900a RX
  and waits for a specific ICMP echo *reply* (matched by ID+sequence).

* :func:`build_tx_code` -- simple 6502 routine that transmits a pre-built
  frame from memory.

Both routines write a single-byte status flag at a well-known address:

* ``0x00`` -- pending
* ``0x01`` -- success (reply received / responder sent reply)
* ``0xFF`` -- timeout

Register layout (RR-Net mode, CS8900a at ``$DE00``).  This matches the
ip65 ``cs8900a.s`` driver and the physical RR-Net cartridge::

    $DE00/$DE01  ISQ     (bit 0 of $DE01 = RR clockport enable)
    $DE02/$DE03  PPPtr
    $DE04/$DE05  PPData
    $DE08/$DE09  RTDATA  (RX/TX data FIFO)
    $DE0C/$DE0D  TxCMD
    $DE0E/$DE0F  TxLen

**Critical:** the RR clockport enable bit ($DE01 bit 0) MUST be set before
any other CS8900a register access.  All code builders in this module
prepend a clockport-enable snippet via :func:`_clockport_enable_bytes` so
callers do not have to remember this.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# CS8900a registers (RR-Net layout; matches ip65 cs8900a.s)
# ---------------------------------------------------------------------------
ISQ_LO = 0xDE00
ISQ_HI = 0xDE01          # bit 0 = RR clockport enable
PPTR_LO = 0xDE02
PPTR_HI = 0xDE03
PPDATA_LO = 0xDE04
PPDATA_HI = 0xDE05
RTDATA_LO = 0xDE08
RTDATA_HI = 0xDE09
TXCMD_LO = 0xDE0C
TXCMD_HI = 0xDE0D
TXLEN_LO = 0xDE0E
TXLEN_HI = 0xDE0F


def _clockport_enable_bytes() -> bytes:
    """6502 snippet: enable RR clockport bit (LDA $DE01; ORA #$01; STA $DE01).

    Must precede every CS8900a access.  Without it, the chip silently
    drops all register reads/writes.
    """
    return bytes([
        0xAD, ISQ_HI & 0xFF, ISQ_HI >> 8,
        0x09, 0x01,
        0x8D, ISQ_HI & 0xFF, ISQ_HI >> 8,
    ])


def _emit_clockport_enable(a: "Asm") -> None:
    """Emit the RR clockport enable sequence into an Asm buffer."""
    a.emit(0xAD, ISQ_HI & 0xFF, ISQ_HI >> 8)
    a.emit(0x09, 0x01)
    a.emit(0x8D, ISQ_HI & 0xFF, ISQ_HI >> 8)


# ---------------------------------------------------------------------------
# Tiny 6502 assembler with branch fixups
# ---------------------------------------------------------------------------
class Asm:
    """Tiny 6502 assembler with branch + JMP absolute fixups."""

    def __init__(self, org: int = 0) -> None:
        self.org = org
        self.buf = bytearray()
        self.labels: dict[str, int] = {}
        self._branch_fix: list[tuple[int, str]] = []
        self._jmp_fix: list[tuple[int, str]] = []

    @property
    def pos(self) -> int:
        return len(self.buf)

    def label(self, name: str) -> None:
        if name in self.labels:
            raise ValueError(f"duplicate label: {name}")
        self.labels[name] = self.pos

    def emit(self, *data: int) -> None:
        self.buf.extend(data)

    def branch(self, opcode: int, target: str) -> None:
        self.buf.append(opcode)
        self._branch_fix.append((self.pos, target))
        self.buf.append(0)

    def jmp(self, target: str) -> None:
        """JMP absolute to a label; target fixed up at build() time."""
        self.buf.append(0x4C)
        self._jmp_fix.append((self.pos, target))
        self.buf.append(0)
        self.buf.append(0)

    def build(self) -> bytes:
        for fix_pos, label in self._branch_fix:
            if label not in self.labels:
                raise ValueError(f"unresolved branch label: {label}")
            target = self.labels[label]
            disp = target - (fix_pos + 1)
            if not (-128 <= disp <= 127):
                raise ValueError(f"branch to '{label}' out of range: {disp}")
            self.buf[fix_pos] = disp & 0xFF
        for fix_pos, label in self._jmp_fix:
            if label not in self.labels:
                raise ValueError(f"unresolved jmp label: {label}")
            target = self.org + self.labels[label]
            self.buf[fix_pos] = target & 0xFF
            self.buf[fix_pos + 1] = (target >> 8) & 0xFF
        return bytes(self.buf)


# ---------------------------------------------------------------------------
# ICMP checksum helpers
# ---------------------------------------------------------------------------

def _ip_checksum(data: bytes) -> int:
    """Compute the standard IP/ICMP 16-bit 1's complement checksum."""
    if len(data) % 2:
        data = data + b"\x00"
    s = 0
    for i in range(0, len(data), 2):
        s += (data[i] << 8) | data[i + 1]
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return (~s) & 0xFFFF


@dataclass
class EchoRequest:
    """Bundle holding a built frame + metadata for readback verification."""

    frame: bytes
    identifier: int
    sequence: int
    payload: bytes


def build_echo_request_frame(
    src_mac: bytes,
    dst_mac: bytes,
    src_ip: bytes,
    dst_ip: bytes,
    identifier: int = 0x1234,
    sequence: int = 1,
    payload: bytes = b"PING_FROM_C64",
) -> EchoRequest:
    """Build a complete ICMP echo-request ethernet frame.

    Returns an EchoRequest with the full frame bytes ready to upload and
    transmit, plus metadata needed to verify a matching echo reply.
    """
    assert len(src_mac) == 6 and len(dst_mac) == 6
    assert len(src_ip) == 4 and len(dst_ip) == 4

    icmp_body = (
        struct.pack(">BBHHH", 8, 0, 0, identifier, sequence) + payload
    )
    icmp_cksum = _ip_checksum(icmp_body)
    icmp = (
        struct.pack(">BBHHH", 8, 0, icmp_cksum, identifier, sequence) + payload
    )

    ip_total_len = 20 + len(icmp)
    ip_no_cksum = struct.pack(
        ">BBHHHBBH4s4s",
        0x45, 0x00, ip_total_len,
        0x0000, 0x0000,
        64, 0x01, 0x0000,
        src_ip, dst_ip,
    )
    ip_cksum = _ip_checksum(ip_no_cksum)
    ip_header = struct.pack(
        ">BBHHHBBH4s4s",
        0x45, 0x00, ip_total_len,
        0x0000, 0x0000,
        64, 0x01, ip_cksum,
        src_ip, dst_ip,
    )

    frame = dst_mac + src_mac + b"\x08\x00" + ip_header + icmp
    # Pad to 60 bytes minimum (CS8900a adds FCS on wire)
    if len(frame) < 60:
        frame = frame + b"\x00" * (60 - len(frame))
    # Word-align for CS8900a TX
    if len(frame) % 2:
        frame = frame + b"\x00"
    return EchoRequest(
        frame=frame,
        identifier=identifier,
        sequence=sequence,
        payload=payload,
    )


# ---------------------------------------------------------------------------
# CS8900a initialisation blobs (same as tests/test_ethernet_bridge.py)
# ---------------------------------------------------------------------------

def cs8900a_rxctl_code() -> bytes:
    """RxCTL (PP 0x0104) = 0x00D8 (promiscuous + RxOK).

    Enables the RR clockport first, then programs the register.
    """
    return _clockport_enable_bytes() + bytes([
        0xA9, 0x04, 0x8D, PPTR_LO & 0xFF, PPTR_LO >> 8,
        0xA9, 0x01, 0x8D, PPTR_HI & 0xFF, PPTR_HI >> 8,
        0xA9, 0xD8, 0x8D, PPDATA_LO & 0xFF, PPDATA_LO >> 8,
        0xA9, 0x00, 0x8D, PPDATA_HI & 0xFF, PPDATA_HI >> 8,
        0x60,
    ])


def cs8900a_read_linectl_code(dest_addr: int) -> bytes:
    """Read LineCTL (PP 0x0112) into dest_addr / dest_addr+1."""
    lo = dest_addr & 0xFF
    hi = (dest_addr >> 8) & 0xFF
    return _clockport_enable_bytes() + bytes([
        0xA9, 0x12, 0x8D, PPTR_LO & 0xFF, PPTR_LO >> 8,
        0xA9, 0x01, 0x8D, PPTR_HI & 0xFF, PPTR_HI >> 8,
        0xAD, PPDATA_LO & 0xFF, PPDATA_LO >> 8,
        0x8D, lo, hi,
        0xAD, PPDATA_HI & 0xFF, PPDATA_HI >> 8,
        0x8D, (dest_addr + 1) & 0xFF, ((dest_addr + 1) >> 8) & 0xFF,
        0x60,
    ])


def cs8900a_write_linectl_code(lo_value: int, hi_value: int) -> bytes:
    """Write lo/hi to LineCTL (PP 0x0112)."""
    return _clockport_enable_bytes() + bytes([
        0xA9, 0x12, 0x8D, PPTR_LO & 0xFF, PPTR_LO >> 8,
        0xA9, 0x01, 0x8D, PPTR_HI & 0xFF, PPTR_HI >> 8,
        0xA9, lo_value & 0xFF, 0x8D, PPDATA_LO & 0xFF, PPDATA_LO >> 8,
        0xA9, hi_value & 0xFF, 0x8D, PPDATA_HI & 0xFF, PPDATA_HI >> 8,
        0x60,
    ])


# ---------------------------------------------------------------------------
# 6502 code builders
# ---------------------------------------------------------------------------

def build_tx_code(
    load_addr: int,
    frame_buf: int,
    frame_len: int,
    result_addr: int,
) -> bytes:
    """Build a 6502 routine that transmits ``frame_len`` bytes from ``frame_buf``.

    Writes 0x01 to ``result_addr`` on success.  Loads at ``load_addr``.
    """
    a = Asm(org=load_addr)
    a.emit(0x78)  # SEI
    _emit_clockport_enable(a)
    a.emit(0xA9, 0xC0, 0x8D, TXCMD_LO & 0xFF, TXCMD_LO >> 8)
    a.emit(0xA9, 0x00, 0x8D, TXCMD_HI & 0xFF, TXCMD_HI >> 8)
    a.emit(0xA9, frame_len & 0xFF, 0x8D, TXLEN_LO & 0xFF, TXLEN_LO >> 8)
    a.emit(0xA9, (frame_len >> 8) & 0xFF, 0x8D, TXLEN_HI & 0xFF, TXLEN_HI >> 8)
    a.emit(0xA9, 0x38, 0x8D, PPTR_LO & 0xFF, PPTR_LO >> 8)
    a.emit(0xA9, 0x01, 0x8D, PPTR_HI & 0xFF, PPTR_HI >> 8)
    a.label("tw")
    a.emit(0xAD, PPDATA_HI & 0xFF, PPDATA_HI >> 8)
    a.emit(0x29, 0x01)
    a.branch(0xF0, "tw")
    a.emit(0xA9, frame_buf & 0xFF, 0x85, 0xFB)
    a.emit(0xA9, (frame_buf >> 8) & 0xFF, 0x85, 0xFC)
    a.emit(0xA0, 0x00)
    a.label("tl")
    a.emit(0xB1, 0xFB)
    a.emit(0x8D, RTDATA_LO & 0xFF, RTDATA_LO >> 8)
    a.emit(0xC8)
    a.emit(0xB1, 0xFB)
    a.emit(0x8D, RTDATA_HI & 0xFF, RTDATA_HI >> 8)
    a.emit(0xC8)
    a.emit(0xC0, frame_len & 0xFF)
    a.branch(0xD0, "tl")
    a.emit(0xA9, 0x01, 0x8D, result_addr & 0xFF, (result_addr >> 8) & 0xFF)
    a.emit(0x58)
    a.emit(0x60)
    return a.build()


_FIXED_RX_BYTES = 60  # bytes to drain after status+length (drives loop count)


def _emit_skip_packet(a: Asm) -> None:
    """Emit code that issues CS8900a SkipNow (RxCFG bit 6, PP 0x0102).

    Per the ip65 cs8900a driver: read RxCFG low byte, OR with 0x40,
    write it back.  Only the low byte is touched -- the high byte must
    be left alone or the chip drops critical state.
    """
    a.emit(0xA9, 0x02, 0x8D, PPTR_LO & 0xFF, PPTR_LO >> 8)
    a.emit(0xA9, 0x01, 0x8D, PPTR_HI & 0xFF, PPTR_HI >> 8)
    a.emit(0xAD, PPDATA_LO & 0xFF, PPDATA_LO >> 8)  # LDA PPData lo
    a.emit(0x09, 0x40)                                # ORA #$40
    a.emit(0x8D, PPDATA_LO & 0xFF, PPDATA_LO >> 8)  # STA PPData lo


def _emit_read_frame(a: Asm, rx_buf: int) -> None:
    """Emit code to read a frame from CS8900a RTDATA into rx_buf.

    Preconditions:
      - a frame is waiting (RxEvent fired)
    Side effects:
      - RxStatus stored at ZP $F1:$F2
      - RxLength stored at ZP $F3:$F4
      - Reads exactly _FIXED_RX_BYTES bytes (32 words) into rx_buf in
        wire order.  This matches the working pattern in
        tests/test_ethernet_bridge.py and avoids trusting RxLength
        (VICE's TFE emulation has been observed to return bogus
        RxLength on the first ICMP read with this caller pattern).

    The fixed-length read is sufficient because we only need to inspect
    the IP header (offset 14-33) and ICMP header (34-41), and ethernet
    frames are minimum 60 bytes anyway -- our test sends 60-byte frames.
    """
    # Read 4 status+length bytes (discarded) as separate byte reads.
    a.emit(0xAD, RTDATA_LO & 0xFF, RTDATA_LO >> 8)
    a.emit(0x85, 0xF1)
    a.emit(0xAD, RTDATA_HI & 0xFF, RTDATA_HI >> 8)
    a.emit(0x85, 0xF2)
    a.emit(0xAD, RTDATA_LO & 0xFF, RTDATA_LO >> 8)
    a.emit(0x85, 0xF3)
    a.emit(0xAD, RTDATA_HI & 0xFF, RTDATA_HI >> 8)
    a.emit(0x85, 0xF4)

    a.emit(0xA9, rx_buf & 0xFF, 0x85, 0xFB)
    a.emit(0xA9, (rx_buf >> 8) & 0xFF, 0x85, 0xFC)
    a.emit(0xA0, 0x00)
    a.label("_rf_lp")
    a.emit(0xAD, RTDATA_LO & 0xFF, RTDATA_LO >> 8)
    a.emit(0x91, 0xFB)
    a.emit(0xC8)
    a.emit(0xAD, RTDATA_HI & 0xFF, RTDATA_HI >> 8)
    a.emit(0x91, 0xFB)
    a.emit(0xC8)
    a.emit(0xC0, _FIXED_RX_BYTES)
    a.branch(0xD0, "_rf_lp")
    # Skip the rest of the current packet so the CS8900a FIFO is
    # advanced to the start of the next frame.
    _emit_skip_packet(a)


def _emit_poll_rx(
    a: Asm,
    timeout_label: str,
    success_label: str,
    outer: int = 0x04,
) -> None:
    """Emit code to poll RxEvent with a 3-level timeout.

    Jumps to ``success_label`` when a frame is available (BNE, within
    branch range) and to ``timeout_label`` via JMP absolute when the
    outer counters exhaust.  Uses ZP $F0/$F1/$F2 for counters.

    Outer counter default ``0x04`` -> ~4-5 seconds on a PAL C64.
    """
    # PPPtr = 0x0124 (RxEvent)
    a.emit(0xA9, 0x24, 0x8D, PPTR_LO & 0xFF, PPTR_LO >> 8)
    a.emit(0xA9, 0x01, 0x8D, PPTR_HI & 0xFF, PPTR_HI >> 8)
    a.emit(0xA9, 0xFF, 0x85, 0xF0)
    a.emit(0xA9, 0xFF, 0x85, 0xF1)
    a.emit(0xA9, outer & 0xFF, 0x85, 0xF2)
    a.label("_pr_lp")
    a.emit(0xAD, PPDATA_HI & 0xFF, PPDATA_HI >> 8)
    a.emit(0x29, 0x01)
    a.branch(0xD0, success_label)  # got frame
    a.emit(0xC6, 0xF0)
    a.branch(0xD0, "_pr_lp")
    a.emit(0xA9, 0xFF, 0x85, 0xF0)
    a.emit(0xC6, 0xF1)
    a.branch(0xD0, "_pr_lp")
    a.emit(0xA9, 0xFF, 0x85, 0xF1)
    a.emit(0xC6, 0xF2)
    a.branch(0xD0, "_pr_lp")
    a.jmp(timeout_label)


def build_rx_echo_reply_code(
    load_addr: int,
    rx_buf: int,
    result_addr: int,
    identifier: int,
    sequence: int,
) -> bytes:
    """Build a 6502 routine that polls RX and waits for an ICMP echo reply.

    Verifies ethertype=IPv4, protocol=ICMP, type=echo-reply, identifier
    and sequence match (big-endian on the wire).  Writes 0x01 or 0xFF to
    ``result_addr``.

    .. note::

        This is the **test-harness** variant.  For the shippable-
        application equivalent (pure 6502, CIA1 TOD deadline), see
        :func:`build_rx_echo_reply_tod_code`.
    """
    id_hi = (identifier >> 8) & 0xFF
    id_lo = identifier & 0xFF
    seq_hi = (sequence >> 8) & 0xFF
    seq_lo = sequence & 0xFF

    a = Asm(org=load_addr)
    a.emit(0x78)  # SEI
    _emit_clockport_enable(a)

    a.label("reset")
    _emit_poll_rx(a, timeout_label="timeout", success_label="got")

    a.label("got")
    _emit_read_frame(a, rx_buf)

    # Check ethertype [12..13] = 0x08 0x00 (IPv4)
    def chk(off: int, val: int, fail: str) -> None:
        addr = rx_buf + off
        a.emit(0xAD, addr & 0xFF, (addr >> 8) & 0xFF)
        a.emit(0xC9, val & 0xFF)
        a.branch(0xD0, fail)

    # We use 'drop_short' (branchable) -> JMP reset
    chk(12, 0x08, "drop")
    chk(13, 0x00, "drop")
    chk(23, 0x01, "drop")  # protocol = ICMP
    chk(34, 0x00, "drop")  # type = echo reply
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


def build_ping_and_wait_code(
    load_addr: int,
    tx_frame_buf: int,
    tx_frame_len: int,
    rx_buf: int,
    result_addr: int,
    identifier: int,
    sequence: int,
) -> bytes:
    """Build a 6502 routine that TXes an echo request and waits for the reply.

    This combines :func:`build_tx_code` and :func:`build_rx_echo_reply_code`
    into a single routine, run via one ``jsr()`` call.  This is important
    because while the binary monitor is paused (between JSRs) the CS8900a
    may not pump TAP frames reliably, so TX and RX must happen without
    a CPU pause in between.

    .. note::

        Intended to pair with :func:`build_icmp_responder_code` running
        on the peer VICE.  See the stage-4 validation in the
        bridge-networking-rrnet worktree history for current round-trip
        status.

    .. note::

        This is the **test-harness** variant -- it uses an iteration-
        counter timeout that evaporates under VICE warp mode.  For the
        **shippable-application** equivalent (pure 6502, CIA1 TOD
        deadline, correct on real C64 / U64E / VICE normal), see
        :func:`build_ping_and_wait_tod_code`.
    """
    id_hi = (identifier >> 8) & 0xFF
    id_lo = identifier & 0xFF
    seq_hi = (sequence >> 8) & 0xFF
    seq_lo = sequence & 0xFF

    a = Asm(org=load_addr)
    a.emit(0x78)  # SEI
    _emit_clockport_enable(a)

    # --- TX the echo request ---
    a.emit(0xA9, 0xC0, 0x8D, TXCMD_LO & 0xFF, TXCMD_LO >> 8)
    a.emit(0xA9, 0x00, 0x8D, TXCMD_HI & 0xFF, TXCMD_HI >> 8)
    a.emit(0xA9, tx_frame_len & 0xFF, 0x8D, TXLEN_LO & 0xFF, TXLEN_LO >> 8)
    a.emit(0xA9, (tx_frame_len >> 8) & 0xFF, 0x8D, TXLEN_HI & 0xFF, TXLEN_HI >> 8)
    a.emit(0xA9, 0x38, 0x8D, PPTR_LO & 0xFF, PPTR_LO >> 8)
    a.emit(0xA9, 0x01, 0x8D, PPTR_HI & 0xFF, PPTR_HI >> 8)
    a.label("pw_txw")
    a.emit(0xAD, PPDATA_HI & 0xFF, PPDATA_HI >> 8)
    a.emit(0x29, 0x01)
    a.branch(0xF0, "pw_txw")
    a.emit(0xA9, tx_frame_buf & 0xFF, 0x85, 0xFB)
    a.emit(0xA9, (tx_frame_buf >> 8) & 0xFF, 0x85, 0xFC)
    a.emit(0xA0, 0x00)
    a.label("pw_txlp")
    a.emit(0xB1, 0xFB)
    a.emit(0x8D, RTDATA_LO & 0xFF, RTDATA_LO >> 8)
    a.emit(0xC8)
    a.emit(0xB1, 0xFB)
    a.emit(0x8D, RTDATA_HI & 0xFF, RTDATA_HI >> 8)
    a.emit(0xC8)
    a.emit(0xC0, tx_frame_len & 0xFF)
    a.branch(0xD0, "pw_txlp")

    # --- Now poll for the reply (same as build_rx_echo_reply_code body) ---
    a.label("reset")
    _emit_poll_rx(a, timeout_label="timeout", success_label="got")

    a.label("got")
    _emit_read_frame(a, rx_buf)

    def chk(off: int, val: int, fail: str) -> None:
        addr = rx_buf + off
        a.emit(0xAD, addr & 0xFF, (addr >> 8) & 0xFF)
        a.emit(0xC9, val & 0xFF)
        a.branch(0xD0, fail)

    chk(12, 0x08, "drop")
    chk(13, 0x00, "drop")
    chk(23, 0x01, "drop")
    chk(34, 0x00, "drop")
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


def build_icmp_responder_code(
    load_addr: int,
    rx_buf: int,
    my_ip: bytes,
    result_addr: int,
) -> bytes:
    """Build a 6502 routine that receives one ICMP echo request and replies.

    Polls RX, checks for an IPv4/ICMP echo request addressed to ``my_ip``,
    transforms it in place into an echo reply (swap MAC, swap IP, set
    type=0, patch ICMP checksum), and TXes it back.  Writes 0x01 or 0xFF
    to ``result_addr``.

    Uses RR-Net register layout with the clockport enable injected at
    entry.  See ``tests/test_bridge_ping.py`` for a working round-trip
    exercise built on top of this routine.

    .. note::

        This is the **test-harness** variant -- iteration-counter
        timeout, correct under VICE warp (when host-orchestrated).
        For the shippable-application equivalent (pure 6502, CIA1 TOD
        deadline, correct on real C64 / U64E / VICE normal), see
        :func:`build_icmp_responder_tod_code`.
    """
    assert len(my_ip) == 4

    a = Asm(org=load_addr)
    a.emit(0x78)
    _emit_clockport_enable(a)

    a.label("reset")
    _emit_poll_rx(a, timeout_label="timeout", success_label="got")

    a.label("got")
    _emit_read_frame(a, rx_buf)

    def chk(off: int, val: int, fail: str) -> None:
        addr = rx_buf + off
        a.emit(0xAD, addr & 0xFF, (addr >> 8) & 0xFF)
        a.emit(0xC9, val & 0xFF)
        a.branch(0xD0, fail)

    chk(12, 0x08, "drop")   # ethertype hi
    chk(13, 0x00, "drop")   # ethertype lo
    chk(23, 0x01, "drop")   # IP protocol = ICMP
    chk(34, 0x08, "drop")   # ICMP type = echo request
    chk(30, my_ip[0], "drop")
    chk(31, my_ip[1], "drop")
    chk(32, my_ip[2], "drop")
    chk(33, my_ip[3], "drop")
    a.jmp("transform")

    # Short drop trampoline reachable from all chk BNEs
    a.label("drop")
    a.jmp("reset")

    a.label("transform")
    # Swap dest MAC [0..5] with src MAC [6..11] using X as temp
    for i in range(6):
        dst = rx_buf + i
        src = rx_buf + 6 + i
        a.emit(0xAD, dst & 0xFF, (dst >> 8) & 0xFF)  # LDA dst
        a.emit(0xAE, src & 0xFF, (src >> 8) & 0xFF)  # LDX src
        a.emit(0x8E, dst & 0xFF, (dst >> 8) & 0xFF)  # STX dst
        a.emit(0x8D, src & 0xFF, (src >> 8) & 0xFF)  # STA src

    # Swap src IP [26..29] with dst IP [30..33]
    for i in range(4):
        dst = rx_buf + 26 + i
        src = rx_buf + 30 + i
        a.emit(0xAD, dst & 0xFF, (dst >> 8) & 0xFF)
        a.emit(0xAE, src & 0xFF, (src >> 8) & 0xFF)
        a.emit(0x8E, dst & 0xFF, (dst >> 8) & 0xFF)
        a.emit(0x8D, src & 0xFF, (src >> 8) & 0xFF)

    # ICMP type [34] = 0
    type_addr = rx_buf + 34
    a.emit(0xA9, 0x00, 0x8D, type_addr & 0xFF, (type_addr >> 8) & 0xFF)

    # ICMP checksum [36..37] big-endian; type decreased by 8 -> checksum
    # increases by 8 in hi byte.  Add to [36], handle carry into [37],
    # then end-around carry into [36] again.
    ck_hi = rx_buf + 36
    ck_lo = rx_buf + 37
    a.emit(0xAD, ck_hi & 0xFF, (ck_hi >> 8) & 0xFF)
    a.emit(0x18)
    a.emit(0x69, 0x08)
    a.emit(0x8D, ck_hi & 0xFF, (ck_hi >> 8) & 0xFF)
    a.branch(0x90, "ck_done")  # BCC: no carry
    a.emit(0xAD, ck_lo & 0xFF, (ck_lo >> 8) & 0xFF)
    a.emit(0x18)
    a.emit(0x69, 0x01)
    a.emit(0x8D, ck_lo & 0xFF, (ck_lo >> 8) & 0xFF)
    a.label("ck_done")

    # Wait for TxRdy, then transmit fixed _FIXED_RX_BYTES from rx_buf
    a.emit(0xA9, 0xC0, 0x8D, TXCMD_LO & 0xFF, TXCMD_LO >> 8)
    a.emit(0xA9, 0x00, 0x8D, TXCMD_HI & 0xFF, TXCMD_HI >> 8)
    a.emit(0xA9, _FIXED_RX_BYTES & 0xFF, 0x8D, TXLEN_LO & 0xFF, TXLEN_LO >> 8)
    a.emit(0xA9, 0x00, 0x8D, TXLEN_HI & 0xFF, TXLEN_HI >> 8)
    a.emit(0xA9, 0x38, 0x8D, PPTR_LO & 0xFF, PPTR_LO >> 8)
    a.emit(0xA9, 0x01, 0x8D, PPTR_HI & 0xFF, PPTR_HI >> 8)
    a.label("tw")
    a.emit(0xAD, PPDATA_HI & 0xFF, PPDATA_HI >> 8)
    a.emit(0x29, 0x01)
    a.branch(0xF0, "tw")

    # Transmit fixed _FIXED_RX_BYTES bytes from rx_buf (in place)
    a.emit(0xA9, rx_buf & 0xFF, 0x85, 0xFB)
    a.emit(0xA9, (rx_buf >> 8) & 0xFF, 0x85, 0xFC)
    a.emit(0xA0, 0x00)
    a.label("txlp")
    a.emit(0xB1, 0xFB)
    a.emit(0x8D, RTDATA_LO & 0xFF, RTDATA_LO >> 8)
    a.emit(0xC8)
    a.emit(0xB1, 0xFB)
    a.emit(0x8D, RTDATA_HI & 0xFF, RTDATA_HI >> 8)
    a.emit(0xC8)
    a.emit(0xC0, _FIXED_RX_BYTES)
    a.branch(0xD0, "txlp")

    a.emit(0xA9, 0x01, 0x8D, result_addr & 0xFF, (result_addr >> 8) & 0xFF)
    a.emit(0x58)
    a.emit(0x60)

    a.label("timeout")
    a.emit(0xA9, 0xFF, 0x8D, result_addr & 0xFF, (result_addr >> 8) & 0xFF)
    a.emit(0x58)
    a.emit(0x60)

    return a.build()


# ---------------------------------------------------------------------------
# Host-side wall-clock pattern (preferred -- works in VICE warp + on U64)
# ---------------------------------------------------------------------------
#
# The legacy ``build_*_code`` functions above bake a 6502-cycle-denominated
# poll budget into their inner loops via :func:`_emit_poll_rx`.  That budget
# evaporates in microseconds under VICE warp mode, so the pattern fails when
# warp is enabled.  An empirical investigation also ruled out CIA TOD as a
# wall-clock substitute: in our VICE 3.10 + ``sound=False`` configuration,
# both CIA1 and CIA2 TOD registers stay pinned at ``01:00:00.00`` and never
# advance, regardless of warp.
#
# The replacement is to split each "poll RX, then act on the frame" routine
# into two pieces:
#
#   * A bounded "peek batch" routine (:func:`build_rx_peek_code`) that polls
#     RxEvent for a fixed number of iterations and immediately RTSes with
#     ``0x01`` (ready) or ``0xFF`` (not yet).  Driven from Python via
#     :func:`c64_test_harness.poll_until.poll_until_ready`, which owns the
#     wall-clock deadline.
#
#   * A "consume" routine that runs once after the peek reports ready.  It
#     drains the frame, validates it, and (for the responder) TXes a reply.
#     The drain + TX still happen inside a single JSR, so the CS8900a state
#     stays consistent across the RX-then-TX sequence.  Two flavours are
#     provided: :func:`build_read_and_match_echo_reply_code` and
#     :func:`build_read_and_respond_echo_request_code`.
#
# Python orchestrators (:func:`run_ping_and_wait`, :func:`run_icmp_responder`)
# tie the two together.  These are the entry points new callers should use.
#
# The same orchestration shape generalises beyond CS8900a: a future Ultimate
# 64 Elite UCI peek routine would poll its socket-status register at
# ``$DF1C-$DF1F`` instead of CS8900a RxEvent, and ``poll_until_ready`` would
# drive it identically.

_RX_PEEK_BATCH_DEFAULT = 500


def build_rx_peek_code(
    load_addr: int,
    result_addr: int,
    *,
    batch_size: int = _RX_PEEK_BATCH_DEFAULT,
) -> bytes:
    """Build a bounded CS8900a RxEvent peek routine.

    Polls RxEvent (PP 0x0124, hi byte bit 0) for ``batch_size``
    iterations.  Writes ``0x01`` to ``result_addr`` if the bit is set
    on any iteration; writes ``0xFF`` if the loop runs to completion
    without seeing it.  RTSes immediately in either case.

    The routine is designed to be invoked repeatedly from the Python
    side via :func:`c64_test_harness.poll_until.poll_until_ready`,
    which owns the wall-clock deadline.

    Zero-page footprint: ``$F0`` and ``$F1`` (16-bit counter).
    ``$F2`` is NOT used by this routine, freeing it for callers.
    """
    if batch_size < 1 or batch_size > 65535:
        raise ValueError(f"batch_size must be 1..65535, got {batch_size}")

    lo = batch_size & 0xFF
    hi = (batch_size >> 8) & 0xFF

    a = Asm(org=load_addr)
    a.emit(0x78)  # SEI -- prevent KERNAL IRQ corrupting ZP/scratch
    _emit_clockport_enable(a)

    # PPPtr = 0x0124 (RxEvent)
    a.emit(0xA9, 0x24, 0x8D, PPTR_LO & 0xFF, PPTR_LO >> 8)
    a.emit(0xA9, 0x01, 0x8D, PPTR_HI & 0xFF, PPTR_HI >> 8)

    # Initialise 16-bit counter at $F0/$F1
    a.emit(0xA9, lo, 0x85, 0xF0)
    a.emit(0xA9, hi, 0x85, 0xF1)

    a.label("peek_loop")
    a.emit(0xAD, PPDATA_HI & 0xFF, PPDATA_HI >> 8)  # LDA RxEvent hi
    a.emit(0x29, 0x01)                               # AND #$01
    a.branch(0xD0, "peek_hit")                       # BNE -> hit

    # 16-bit decrement of $F0/$F1
    a.emit(0xA5, 0xF0)              # LDA $F0
    a.branch(0xD0, "_dec_lo")       # if lo != 0, just dec lo
    a.emit(0xC6, 0xF1)              # DEC $F1 (hi)
    a.label("_dec_lo")
    a.emit(0xC6, 0xF0)              # DEC $F0 (lo)
    a.emit(0xA5, 0xF0)              # LDA $F0
    a.branch(0xD0, "peek_loop")     # any nonzero -> continue
    a.emit(0xA5, 0xF1)              # LDA $F1
    a.branch(0xD0, "peek_loop")     # if hi still nonzero -> continue

    # Exhausted: write 0xFF to result, restore IRQs, RTS
    a.emit(0xA9, 0xFF, 0x8D, result_addr & 0xFF, (result_addr >> 8) & 0xFF)
    a.emit(0x58)  # CLI
    a.emit(0x60)  # RTS

    a.label("peek_hit")
    a.emit(0xA9, 0x01, 0x8D, result_addr & 0xFF, (result_addr >> 8) & 0xFF)
    a.emit(0x58)
    a.emit(0x60)

    return a.build()


def build_read_and_match_echo_reply_code(
    load_addr: int,
    rx_buf: int,
    result_addr: int,
    identifier: int,
    sequence: int,
) -> bytes:
    """Drain a waiting RX frame and match it against an expected echo reply.

    Assumes ``RxEvent`` has already fired (caller ran a peek that
    returned 0x01).  Writes:

    * ``0x01`` -- match (caller may verify ``rx_buf`` contents)
    * ``0x02`` -- frame consumed but did not match (host should re-poll)
    """
    id_hi = (identifier >> 8) & 0xFF
    id_lo = identifier & 0xFF
    seq_hi = (sequence >> 8) & 0xFF
    seq_lo = sequence & 0xFF

    a = Asm(org=load_addr)
    a.emit(0x78)  # SEI
    _emit_clockport_enable(a)

    _emit_read_frame(a, rx_buf)

    def chk(off: int, val: int, fail: str) -> None:
        addr = rx_buf + off
        a.emit(0xAD, addr & 0xFF, (addr >> 8) & 0xFF)
        a.emit(0xC9, val & 0xFF)
        a.branch(0xD0, fail)

    chk(12, 0x08, "rmm")
    chk(13, 0x00, "rmm")
    chk(23, 0x01, "rmm")
    chk(34, 0x00, "rmm")
    chk(38, id_hi, "rmm")
    chk(39, id_lo, "rmm")
    chk(40, seq_hi, "rmm")
    chk(41, seq_lo, "rmm")

    # success
    a.emit(0xA9, 0x01, 0x8D, result_addr & 0xFF, (result_addr >> 8) & 0xFF)
    a.emit(0x58)
    a.emit(0x60)

    a.label("rmm")
    a.emit(0xA9, 0x02, 0x8D, result_addr & 0xFF, (result_addr >> 8) & 0xFF)
    a.emit(0x58)
    a.emit(0x60)

    return a.build()


def build_read_and_respond_echo_request_code(
    load_addr: int,
    rx_buf: int,
    my_ip: bytes,
    result_addr: int,
) -> bytes:
    """Drain a waiting echo request, swap+TX a reply, no polling.

    Assumes ``RxEvent`` has already fired.  Writes:

    * ``0x01`` -- request consumed and reply transmitted
    * ``0x02`` -- frame consumed but did not match (host should re-poll)
    """
    assert len(my_ip) == 4

    a = Asm(org=load_addr)
    a.emit(0x78)
    _emit_clockport_enable(a)

    _emit_read_frame(a, rx_buf)

    def chk(off: int, val: int, fail: str) -> None:
        addr = rx_buf + off
        a.emit(0xAD, addr & 0xFF, (addr >> 8) & 0xFF)
        a.emit(0xC9, val & 0xFF)
        a.branch(0xD0, fail)

    chk(12, 0x08, "rrm_tramp")
    chk(13, 0x00, "rrm_tramp")
    chk(23, 0x01, "rrm_tramp")
    chk(34, 0x08, "rrm_tramp")
    chk(30, my_ip[0], "rrm_tramp")
    chk(31, my_ip[1], "rrm_tramp")
    chk(32, my_ip[2], "rrm_tramp")
    chk(33, my_ip[3], "rrm_tramp")
    a.jmp("_rrm_skip")
    a.label("rrm_tramp")
    a.jmp("rrm")
    a.label("_rrm_skip")

    # Swap dest MAC [0..5] with src MAC [6..11]
    for i in range(6):
        dst = rx_buf + i
        src = rx_buf + 6 + i
        a.emit(0xAD, dst & 0xFF, (dst >> 8) & 0xFF)
        a.emit(0xAE, src & 0xFF, (src >> 8) & 0xFF)
        a.emit(0x8E, dst & 0xFF, (dst >> 8) & 0xFF)
        a.emit(0x8D, src & 0xFF, (src >> 8) & 0xFF)

    # Swap src IP [26..29] with dst IP [30..33]
    for i in range(4):
        dst = rx_buf + 26 + i
        src = rx_buf + 30 + i
        a.emit(0xAD, dst & 0xFF, (dst >> 8) & 0xFF)
        a.emit(0xAE, src & 0xFF, (src >> 8) & 0xFF)
        a.emit(0x8E, dst & 0xFF, (dst >> 8) & 0xFF)
        a.emit(0x8D, src & 0xFF, (src >> 8) & 0xFF)

    # ICMP type [34] = 0
    type_addr = rx_buf + 34
    a.emit(0xA9, 0x00, 0x8D, type_addr & 0xFF, (type_addr >> 8) & 0xFF)

    # ICMP checksum patch: type decreased by 8 -> checksum hi += 8
    ck_hi = rx_buf + 36
    ck_lo = rx_buf + 37
    a.emit(0xAD, ck_hi & 0xFF, (ck_hi >> 8) & 0xFF)
    a.emit(0x18)
    a.emit(0x69, 0x08)
    a.emit(0x8D, ck_hi & 0xFF, (ck_hi >> 8) & 0xFF)
    a.branch(0x90, "_ck2_done")
    a.emit(0xAD, ck_lo & 0xFF, (ck_lo >> 8) & 0xFF)
    a.emit(0x18)
    a.emit(0x69, 0x01)
    a.emit(0x8D, ck_lo & 0xFF, (ck_lo >> 8) & 0xFF)
    a.label("_ck2_done")

    # Wait for TxRdy then TX _FIXED_RX_BYTES from rx_buf
    a.emit(0xA9, 0xC0, 0x8D, TXCMD_LO & 0xFF, TXCMD_LO >> 8)
    a.emit(0xA9, 0x00, 0x8D, TXCMD_HI & 0xFF, TXCMD_HI >> 8)
    a.emit(0xA9, _FIXED_RX_BYTES & 0xFF, 0x8D, TXLEN_LO & 0xFF, TXLEN_LO >> 8)
    a.emit(0xA9, 0x00, 0x8D, TXLEN_HI & 0xFF, TXLEN_HI >> 8)
    a.emit(0xA9, 0x38, 0x8D, PPTR_LO & 0xFF, PPTR_LO >> 8)
    a.emit(0xA9, 0x01, 0x8D, PPTR_HI & 0xFF, PPTR_HI >> 8)
    a.label("_tw2")
    a.emit(0xAD, PPDATA_HI & 0xFF, PPDATA_HI >> 8)
    a.emit(0x29, 0x01)
    a.branch(0xF0, "_tw2")

    a.emit(0xA9, rx_buf & 0xFF, 0x85, 0xFB)
    a.emit(0xA9, (rx_buf >> 8) & 0xFF, 0x85, 0xFC)
    a.emit(0xA0, 0x00)
    a.label("_txlp2")
    a.emit(0xB1, 0xFB)
    a.emit(0x8D, RTDATA_LO & 0xFF, RTDATA_LO >> 8)
    a.emit(0xC8)
    a.emit(0xB1, 0xFB)
    a.emit(0x8D, RTDATA_HI & 0xFF, RTDATA_HI >> 8)
    a.emit(0xC8)
    a.emit(0xC0, _FIXED_RX_BYTES)
    a.branch(0xD0, "_txlp2")

    a.emit(0xA9, 0x01, 0x8D, result_addr & 0xFF, (result_addr >> 8) & 0xFF)
    a.emit(0x58)
    a.emit(0x60)

    a.label("rrm")
    a.emit(0xA9, 0x02, 0x8D, result_addr & 0xFF, (result_addr >> 8) & 0xFF)
    a.emit(0x58)
    a.emit(0x60)

    return a.build()


# ---------------------------------------------------------------------------
# Python-side orchestrators
# ---------------------------------------------------------------------------

_DEFAULT_PEEK_ADDR = 0xC000
_DEFAULT_CONSUME_ADDR = 0xC100


def run_ping_and_wait(
    transport,
    *,
    tx_frame: bytes,
    rx_buf: int,
    result_addr: int,
    identifier: int,
    sequence: int,
    tx_frame_buf: int,
    timeout_s: float = 5.0,
    peek_addr: int = _DEFAULT_PEEK_ADDR,
    consume_addr: int = _DEFAULT_CONSUME_ADDR,
) -> int:
    """Transmit an echo request, then poll for a matching reply.

    Loads a TX routine, transmits ``tx_frame``, then loops:
    ``poll_until_ready`` -> ``read_and_match_echo_reply`` -> on mismatch,
    re-poll; on match, return ``0x01``; on wall-clock timeout, return
    ``0xFF``.

    The wall-clock budget is owned by Python via
    :func:`c64_test_harness.poll_until.poll_until_ready`, so this works
    correctly under VICE warp mode and on Ultimate 64 hardware.
    """
    import time as _time
    from .execute import jsr, load_code
    from .memory import read_bytes, write_bytes
    from .poll_until import poll_until_ready

    tx_code = build_tx_code(
        load_addr=consume_addr,
        frame_buf=tx_frame_buf,
        frame_len=len(tx_frame),
        result_addr=result_addr,
    )
    load_code(transport, consume_addr, tx_code)
    write_bytes(transport, tx_frame_buf, tx_frame)
    write_bytes(transport, result_addr, [0x00])
    jsr(transport, consume_addr, timeout=5.0)
    tx_result = read_bytes(transport, result_addr, 1)[0]
    if tx_result != 0x01:
        return tx_result

    peek_code = build_rx_peek_code(load_addr=peek_addr, result_addr=result_addr)
    load_code(transport, peek_addr, peek_code)

    match_code = build_read_and_match_echo_reply_code(
        load_addr=consume_addr,
        rx_buf=rx_buf,
        result_addr=result_addr,
        identifier=identifier,
        sequence=sequence,
    )
    load_code(transport, consume_addr, match_code)

    deadline = _time.monotonic() + timeout_s
    while True:
        remaining = deadline - _time.monotonic()
        if remaining <= 0:
            return 0xFF
        peek_result = poll_until_ready(
            transport,
            code_addr=peek_addr,
            result_addr=result_addr,
            timeout_s=remaining,
        )
        if peek_result == 0xFF:
            return 0xFF
        if peek_result != 0x01:
            return peek_result
        write_bytes(transport, result_addr, [0x00])
        jsr(transport, consume_addr, timeout=5.0)
        match_result = read_bytes(transport, result_addr, 1)[0]
        if match_result == 0x01:
            return 0x01
        if match_result == 0x02:
            continue
        return match_result


def run_icmp_responder(
    transport,
    *,
    rx_buf: int,
    my_ip: bytes,
    result_addr: int,
    timeout_s: float = 5.0,
    peek_addr: int = _DEFAULT_PEEK_ADDR,
    consume_addr: int = _DEFAULT_CONSUME_ADDR,
) -> int:
    """Wait for an ICMP echo request and reply to it.

    Loops: ``poll_until_ready`` -> ``read_and_respond_echo_request`` ->
    on mismatch, re-poll; on success, return ``0x01``; on wall-clock
    timeout, return ``0xFF``.
    """
    import time as _time
    from .execute import jsr, load_code
    from .memory import read_bytes, write_bytes
    from .poll_until import poll_until_ready

    peek_code = build_rx_peek_code(load_addr=peek_addr, result_addr=result_addr)
    load_code(transport, peek_addr, peek_code)

    body_code = build_read_and_respond_echo_request_code(
        load_addr=consume_addr,
        rx_buf=rx_buf,
        my_ip=my_ip,
        result_addr=result_addr,
    )
    load_code(transport, consume_addr, body_code)

    deadline = _time.monotonic() + timeout_s
    while True:
        remaining = deadline - _time.monotonic()
        if remaining <= 0:
            return 0xFF
        peek_result = poll_until_ready(
            transport,
            code_addr=peek_addr,
            result_addr=result_addr,
            timeout_s=remaining,
        )
        if peek_result == 0xFF:
            return 0xFF
        if peek_result != 0x01:
            return peek_result
        write_bytes(transport, result_addr, [0x00])
        jsr(transport, consume_addr, timeout=5.0)
        body_result = read_bytes(transport, result_addr, 1)[0]
        if body_result == 0x01:
            return 0x01
        if body_result == 0x02:
            continue
        return body_result


# ---------------------------------------------------------------------------
# Shippable-application variants: TOD-based 6502 timeouts
# ---------------------------------------------------------------------------
#
# These routines mirror the host-driven helpers above but use CIA1
# Time-of-Day as the deadline source instead of iteration counters.
# They are the correct choice for code that ships on disk and runs
# standalone on real C64 / U64E / VICE normal mode.  See
# ``docs/bridge_networking.md`` "Test harness vs shippable application"
# for the full split and ``c64_test_harness.tod_timer`` for the
# low-level CIA1 TOD primitives.
#
# All _tod variants use ZP $F0-$F5 for TOD scratch and cap the
# deadline at 599 tenths (59.9 s) per single call.  They do NOT work
# under VICE warp mode (TOD accelerates with the virtual CPU on
# VICE 3.10; deadlines expire ~31x too fast).
# ---------------------------------------------------------------------------

# CIA1 TOD register addresses (duplicated from tod_timer to keep
# bridge_ping free of module-level imports from tod_timer, since
# tod_timer imports ``Asm`` from us).
_CIA1_TOD_TENTHS = 0xDC08
_CIA1_TOD_SEC = 0xDC09
_CIA1_TOD_MIN = 0xDC0A
_CIA1_TOD_HR = 0xDC0B
_CIA1_CRB = 0xDC0F

_ZP_CUR_LO = 0xF0
_ZP_CUR_HI = 0xF1
_ZP_DEADLINE_LO = 0xF2
_ZP_DEADLINE_HI = 0xF3
_ZP_ONES = 0xF4
_ZP_RAW = 0xF5

_MAX_DEADLINE_TENTHS = 599


def _emit_tod_start_inline(a: Asm) -> None:
    """Inline: clear $DC0F bit 7, write $00 to $DC0B/$0A/$09/$08."""
    a.emit(0xAD, _CIA1_CRB & 0xFF, _CIA1_CRB >> 8)
    a.emit(0x29, 0x7F)
    a.emit(0x8D, _CIA1_CRB & 0xFF, _CIA1_CRB >> 8)
    a.emit(0xA9, 0x00)
    a.emit(0x8D, _CIA1_TOD_HR & 0xFF, _CIA1_TOD_HR >> 8)
    a.emit(0x8D, _CIA1_TOD_MIN & 0xFF, _CIA1_TOD_MIN >> 8)
    a.emit(0x8D, _CIA1_TOD_SEC & 0xFF, _CIA1_TOD_SEC >> 8)
    a.emit(0x8D, _CIA1_TOD_TENTHS & 0xFF, _CIA1_TOD_TENTHS >> 8)


def _emit_tod_sec_table(a: Asm, label: str) -> None:
    """Emit split tens*100 LE16 table (8 lo bytes + 8 hi bytes)."""
    a.label(label)
    for i in range(8):
        a.emit((i * 100) & 0xFF)
    for i in range(8):
        a.emit(((i * 100) >> 8) & 0xFF)


def _emit_tod_ones_table(a: Asm, label: str) -> None:
    """Emit ones*10 table (10 bytes: 0, 10, 20, ... 90)."""
    a.label(label)
    for i in range(10):
        a.emit(i * 10)


def _emit_tod_read_current(a: Asm, min_ok_label: str, done_label: str) -> list[int]:
    """Read CIA1 TOD -> $F0/$F1 (LE16, or $FFFF if minutes > 0).

    Uses ``min_ok_label`` and ``done_label`` as unique labels so multiple
    instances can coexist in one Asm buffer (callers supply fresh
    names).  Returns ``[sec_lo_pos, sec_hi_pos, ones_pos]`` -- byte
    offsets of the three ``LDA abs,X`` operand low-bytes, for
    post-build patching against the sec_tab / ones_tab addresses.
    """
    a.emit(0xAD, _CIA1_TOD_HR & 0xFF, _CIA1_TOD_HR >> 8)    # latch
    a.emit(0xAD, _CIA1_TOD_MIN & 0xFF, _CIA1_TOD_MIN >> 8)   # minutes
    a.branch(0xF0, min_ok_label)
    a.emit(0xAD, _CIA1_TOD_SEC & 0xFF, _CIA1_TOD_SEC >> 8)
    a.emit(0xAD, _CIA1_TOD_TENTHS & 0xFF, _CIA1_TOD_TENTHS >> 8)
    a.emit(0xA9, 0xFF, 0x85, _ZP_CUR_LO)
    a.emit(0xA9, 0xFF, 0x85, _ZP_CUR_HI)
    a.jmp(done_label)

    a.label(min_ok_label)
    a.emit(0xAD, _CIA1_TOD_SEC & 0xFF, _CIA1_TOD_SEC >> 8)
    a.emit(0x85, _ZP_RAW)
    a.emit(0x29, 0x0F)
    a.emit(0x85, _ZP_ONES)
    a.emit(0xA5, _ZP_RAW)
    a.emit(0x4A)
    a.emit(0x4A)
    a.emit(0x4A)
    a.emit(0x4A)
    a.emit(0xAA)            # TAX

    sec_lo_pos = a.pos + 1
    a.emit(0xBD, 0x00, 0x00)   # LDA sec_tab_lo,X (patched)
    a.emit(0x85, _ZP_CUR_LO)
    sec_hi_pos = a.pos + 1
    a.emit(0xBD, 0x00, 0x00)   # LDA sec_tab_hi,X (patched)
    a.emit(0x85, _ZP_CUR_HI)

    a.emit(0xA6, _ZP_ONES)
    ones_pos = a.pos + 1
    a.emit(0xBD, 0x00, 0x00)   # LDA ones_tab,X (patched)
    a.emit(0x18)
    a.emit(0x65, _ZP_CUR_LO)
    a.emit(0x85, _ZP_CUR_LO)
    a.emit(0x90, 0x02)
    a.emit(0xE6, _ZP_CUR_HI)

    a.emit(0xAD, _CIA1_TOD_TENTHS & 0xFF, _CIA1_TOD_TENTHS >> 8)
    a.emit(0x29, 0x0F)
    a.emit(0x18)
    a.emit(0x65, _ZP_CUR_LO)
    a.emit(0x85, _ZP_CUR_LO)
    a.emit(0x90, 0x02)
    a.emit(0xE6, _ZP_CUR_HI)

    a.label(done_label)
    return [sec_lo_pos, sec_hi_pos, ones_pos]


def _emit_tod_poll_rxevent(
    a: Asm,
    got_label: str,
    timeout_label: str,
    min_ok_label: str,
    done_label: str,
    poll_label: str,
) -> list[int]:
    """Emit an inline poll loop: check CS8900a RxEvent, else check TOD.

    Preconditions on entry: PPPtr already points at RxEvent (0x0124)
    and TOD has been started at 00:00:00.0.  $F2/$F3 hold the deadline.

    On frame available -> branch to ``got_label``.
    On deadline elapsed -> ``JMP timeout_label`` (the caller does NOT
    need to emit that jump themselves).

    Returns ``[sec_lo_pos, sec_hi_pos, ones_pos]`` for post-build
    abs,X operand patching.
    """
    a.label(poll_label)
    a.emit(0xAD, PPDATA_HI & 0xFF, PPDATA_HI >> 8)
    a.emit(0x29, 0x01)
    a.branch(0xD0, got_label)

    patch = _emit_tod_read_current(a, min_ok_label, done_label)

    # 16-bit compare: elapsed - deadline.  BCC -> still waiting.
    a.emit(0xA5, _ZP_CUR_LO)
    a.emit(0x38)
    a.emit(0xE5, _ZP_DEADLINE_LO)
    a.emit(0xA5, _ZP_CUR_HI)
    a.emit(0xE5, _ZP_DEADLINE_HI)
    a.branch(0x90, poll_label)

    a.jmp(timeout_label)
    return patch


def _patch_tod_tables(
    buf: bytearray,
    sec_tab_addr: int,
    ones_tab_addr: int,
    patch_positions: list[int],
) -> None:
    """Patch the three ``LDA abs,X`` operand slots in ``buf``."""
    sec_lo_pos, sec_hi_pos, ones_pos = patch_positions
    buf[sec_lo_pos] = sec_tab_addr & 0xFF
    buf[sec_lo_pos + 1] = (sec_tab_addr >> 8) & 0xFF
    buf[sec_hi_pos] = (sec_tab_addr + 8) & 0xFF
    buf[sec_hi_pos + 1] = ((sec_tab_addr + 8) >> 8) & 0xFF
    buf[ones_pos] = ones_tab_addr & 0xFF
    buf[ones_pos + 1] = (ones_tab_addr >> 8) & 0xFF


def _validate_deadline_tenths(deadline_tenths: int) -> None:
    if not (1 <= deadline_tenths <= _MAX_DEADLINE_TENTHS):
        raise ValueError(
            f"deadline_tenths must be in 1..{_MAX_DEADLINE_TENTHS} "
            f"(got {deadline_tenths})"
        )


def build_rx_echo_reply_tod_code(
    load_addr: int,
    rx_buf: int,
    result_addr: int,
    expect_id: int,
    expect_seq: int,
    deadline_tenths: int = 50,
) -> bytes:
    """Shippable-application RX echo reply receiver with CIA1 TOD timeout.

    Pure 6502: polls the CS8900a RX queue, drains incoming frames into
    ``rx_buf``, matches the first IPv4/ICMP echo reply whose identifier
    and sequence number equal ``expect_id`` / ``expect_seq`` (big-endian
    on the wire).  Non-matching frames are drained and polling
    continues against the same TOD deadline.

    Writes ``0x01`` at ``result_addr`` on a match, ``0xFF`` on TOD
    deadline expiry.  Runs standalone -- no host orchestration needed.
    See :mod:`c64_test_harness.tod_timer` for the underlying poll
    primitive and for the test-harness vs shippable-application
    distinction.

    Args:
        load_addr: Where the routine will live in C64 memory.
        rx_buf: RX frame buffer (at least 64 bytes).
        result_addr: 1-byte status slot (0x01 success, 0xFF timeout).
        expect_id: Expected ICMP identifier (16-bit, big-endian on wire).
        expect_seq: Expected ICMP sequence number (16-bit, BE on wire).
        deadline_tenths: Timeout in tenths-of-a-second (1..599).

    Raises:
        ValueError: if ``deadline_tenths`` is out of range.
    """
    _validate_deadline_tenths(deadline_tenths)
    id_hi = (expect_id >> 8) & 0xFF
    id_lo = expect_id & 0xFF
    seq_hi = (expect_seq >> 8) & 0xFF
    seq_lo = expect_seq & 0xFF

    a = Asm(org=load_addr)
    a.emit(0x78)  # SEI
    _emit_clockport_enable(a)

    _emit_tod_start_inline(a)
    a.emit(0xA9, deadline_tenths & 0xFF, 0x85, _ZP_DEADLINE_LO)
    a.emit(0xA9, (deadline_tenths >> 8) & 0xFF, 0x85, _ZP_DEADLINE_HI)

    # PPPtr := 0x0124 (RxEvent)
    a.emit(0xA9, 0x24, 0x8D, PPTR_LO & 0xFF, PPTR_LO >> 8)
    a.emit(0xA9, 0x01, 0x8D, PPTR_HI & 0xFF, PPTR_HI >> 8)

    patch_positions = _emit_tod_poll_rxevent(
        a,
        got_label="got",
        timeout_label="timeout",
        min_ok_label="min_ok1",
        done_label="tod_done1",
        poll_label="poll_top",
    )

    a.label("got")
    _emit_read_frame(a, rx_buf)

    def chk(off: int, val: int, fail: str) -> None:
        addr = rx_buf + off
        a.emit(0xAD, addr & 0xFF, (addr >> 8) & 0xFF)
        a.emit(0xC9, val & 0xFF)
        a.branch(0xD0, fail)

    chk(12, 0x08, "drop")
    chk(13, 0x00, "drop")
    chk(23, 0x01, "drop")
    chk(34, 0x00, "drop")
    chk(38, id_hi, "drop")
    chk(39, id_lo, "drop")
    chk(40, seq_hi, "drop")
    chk(41, seq_lo, "drop")
    a.jmp("success")

    a.label("drop")
    # re-point PPPtr (reading the frame moved it) + loop against same
    # TOD deadline
    a.emit(0xA9, 0x24, 0x8D, PPTR_LO & 0xFF, PPTR_LO >> 8)
    a.emit(0xA9, 0x01, 0x8D, PPTR_HI & 0xFF, PPTR_HI >> 8)
    a.jmp("poll_top")

    a.label("success")
    a.emit(0xA9, 0x01, 0x8D, result_addr & 0xFF, (result_addr >> 8) & 0xFF)
    a.emit(0x58)
    a.emit(0x60)

    a.label("timeout")
    a.emit(0xA9, 0xFF, 0x8D, result_addr & 0xFF, (result_addr >> 8) & 0xFF)
    a.emit(0x58)
    a.emit(0x60)

    _emit_tod_sec_table(a, "sec_tab")
    _emit_tod_ones_table(a, "ones_tab")

    raw = a.build()
    sec_tab_addr = load_addr + a.labels["sec_tab"]
    ones_tab_addr = load_addr + a.labels["ones_tab"]
    buf = bytearray(raw)
    _patch_tod_tables(buf, sec_tab_addr, ones_tab_addr, patch_positions)
    return bytes(buf)


def build_ping_and_wait_tod_code(
    load_addr: int,
    tx_frame_buf: int,
    tx_frame_len: int,
    rx_buf: int,
    result_addr: int,
    identifier: int,
    sequence: int,
    deadline_tenths: int = 50,
) -> bytes:
    """Shippable-application ping-and-wait with CIA1 TOD timeout.

    Pure 6502 equivalent of :func:`build_ping_and_wait_code` but using
    CIA1 Time-of-Day as the deadline source.  Runs standalone on real
    C64 / U64E / VICE normal mode; **not** usable under VICE warp.

    Steps:

    1. Enable RR clockport; start CIA1 TOD at 00:00:00.0; store deadline.
    2. TX the frame at ``tx_frame_buf`` (length ``tx_frame_len``).
    3. Poll CS8900a RxEvent with TOD deadline.
    4. Read the received frame into ``rx_buf``.
    5. Verify ethertype=IPv4, IP protocol=ICMP, type=echo-reply,
       identifier, sequence (all big-endian on the wire).
    6. On match, store 0x01 at ``result_addr``.  On mismatch, drop the
       frame and re-poll against the same TOD deadline.  On TOD expiry,
       store 0xFF.

    Args:
        load_addr: Where the routine will live.
        tx_frame_buf: Address of the pre-built echo request frame.
        tx_frame_len: Frame length in bytes (<= 256).
        rx_buf: RX buffer, at least 64 bytes.
        result_addr: 1-byte status slot (0x01 success, 0xFF timeout).
        identifier: Expected ICMP identifier (16-bit).
        sequence: Expected ICMP sequence (16-bit).
        deadline_tenths: Timeout in tenths-of-a-second (1..599).

    Raises:
        ValueError: if ``deadline_tenths`` is out of range.
    """
    _validate_deadline_tenths(deadline_tenths)
    id_hi = (identifier >> 8) & 0xFF
    id_lo = identifier & 0xFF
    seq_hi = (sequence >> 8) & 0xFF
    seq_lo = sequence & 0xFF

    a = Asm(org=load_addr)
    a.emit(0x78)  # SEI
    _emit_clockport_enable(a)

    _emit_tod_start_inline(a)
    a.emit(0xA9, deadline_tenths & 0xFF, 0x85, _ZP_DEADLINE_LO)
    a.emit(0xA9, (deadline_tenths >> 8) & 0xFF, 0x85, _ZP_DEADLINE_HI)

    # --- TX the echo request (mirrors build_ping_and_wait_code) ---
    a.emit(0xA9, 0xC0, 0x8D, TXCMD_LO & 0xFF, TXCMD_LO >> 8)
    a.emit(0xA9, 0x00, 0x8D, TXCMD_HI & 0xFF, TXCMD_HI >> 8)
    a.emit(0xA9, tx_frame_len & 0xFF, 0x8D, TXLEN_LO & 0xFF, TXLEN_LO >> 8)
    a.emit(0xA9, (tx_frame_len >> 8) & 0xFF, 0x8D, TXLEN_HI & 0xFF, TXLEN_HI >> 8)
    a.emit(0xA9, 0x38, 0x8D, PPTR_LO & 0xFF, PPTR_LO >> 8)
    a.emit(0xA9, 0x01, 0x8D, PPTR_HI & 0xFF, PPTR_HI >> 8)
    a.label("pw_txw")
    a.emit(0xAD, PPDATA_HI & 0xFF, PPDATA_HI >> 8)
    a.emit(0x29, 0x01)
    a.branch(0xF0, "pw_txw")
    a.emit(0xA9, tx_frame_buf & 0xFF, 0x85, 0xFB)
    a.emit(0xA9, (tx_frame_buf >> 8) & 0xFF, 0x85, 0xFC)
    a.emit(0xA0, 0x00)
    a.label("pw_txlp")
    a.emit(0xB1, 0xFB)
    a.emit(0x8D, RTDATA_LO & 0xFF, RTDATA_LO >> 8)
    a.emit(0xC8)
    a.emit(0xB1, 0xFB)
    a.emit(0x8D, RTDATA_HI & 0xFF, RTDATA_HI >> 8)
    a.emit(0xC8)
    a.emit(0xC0, tx_frame_len & 0xFF)
    a.branch(0xD0, "pw_txlp")

    # --- Poll for reply with TOD deadline ---
    a.emit(0xA9, 0x24, 0x8D, PPTR_LO & 0xFF, PPTR_LO >> 8)
    a.emit(0xA9, 0x01, 0x8D, PPTR_HI & 0xFF, PPTR_HI >> 8)

    patch_positions = _emit_tod_poll_rxevent(
        a,
        got_label="got",
        timeout_label="timeout",
        min_ok_label="min_ok1",
        done_label="tod_done1",
        poll_label="poll_top",
    )

    a.label("got")
    _emit_read_frame(a, rx_buf)

    def chk(off: int, val: int, fail: str) -> None:
        addr = rx_buf + off
        a.emit(0xAD, addr & 0xFF, (addr >> 8) & 0xFF)
        a.emit(0xC9, val & 0xFF)
        a.branch(0xD0, fail)

    chk(12, 0x08, "drop")
    chk(13, 0x00, "drop")
    chk(23, 0x01, "drop")
    chk(34, 0x00, "drop")
    chk(38, id_hi, "drop")
    chk(39, id_lo, "drop")
    chk(40, seq_hi, "drop")
    chk(41, seq_lo, "drop")
    a.jmp("success")

    a.label("drop")
    a.emit(0xA9, 0x24, 0x8D, PPTR_LO & 0xFF, PPTR_LO >> 8)
    a.emit(0xA9, 0x01, 0x8D, PPTR_HI & 0xFF, PPTR_HI >> 8)
    a.jmp("poll_top")

    a.label("success")
    a.emit(0xA9, 0x01, 0x8D, result_addr & 0xFF, (result_addr >> 8) & 0xFF)
    a.emit(0x58)
    a.emit(0x60)

    a.label("timeout")
    a.emit(0xA9, 0xFF, 0x8D, result_addr & 0xFF, (result_addr >> 8) & 0xFF)
    a.emit(0x58)
    a.emit(0x60)

    _emit_tod_sec_table(a, "sec_tab")
    _emit_tod_ones_table(a, "ones_tab")

    raw = a.build()
    sec_tab_addr = load_addr + a.labels["sec_tab"]
    ones_tab_addr = load_addr + a.labels["ones_tab"]
    buf = bytearray(raw)
    _patch_tod_tables(buf, sec_tab_addr, ones_tab_addr, patch_positions)
    return bytes(buf)


def build_icmp_responder_tod_code(
    load_addr: int,
    rx_buf: int,
    my_ip: bytes,
    result_addr: int,
    deadline_tenths: int = 50,
) -> bytes:
    """Shippable-application ICMP responder with CIA1 TOD timeout.

    Pure 6502 equivalent of :func:`build_icmp_responder_code`: polls
    the CS8900a RX queue, receives one ICMP echo request addressed to
    ``my_ip``, transforms it in place into an echo reply (swap MACs,
    swap IPs, set ICMP type=0, patch checksum), and transmits it.  Uses
    CIA1 Time-of-Day for the poll deadline.

    Writes ``0x01`` at ``result_addr`` on successful reply TX, ``0xFF``
    on TOD expiry.  Non-matching frames are drained and polling
    continues against the same deadline.

    Args:
        load_addr: Where the routine will live.
        rx_buf: RX frame buffer (at least 64 bytes).
        my_ip: 4-byte IP address of this C64.
        result_addr: 1-byte status slot.
        deadline_tenths: Timeout in tenths-of-a-second (1..599).

    Raises:
        ValueError: if ``deadline_tenths`` is out of range.
    """
    _validate_deadline_tenths(deadline_tenths)
    assert len(my_ip) == 4

    a = Asm(org=load_addr)
    a.emit(0x78)
    _emit_clockport_enable(a)

    _emit_tod_start_inline(a)
    a.emit(0xA9, deadline_tenths & 0xFF, 0x85, _ZP_DEADLINE_LO)
    a.emit(0xA9, (deadline_tenths >> 8) & 0xFF, 0x85, _ZP_DEADLINE_HI)

    a.emit(0xA9, 0x24, 0x8D, PPTR_LO & 0xFF, PPTR_LO >> 8)
    a.emit(0xA9, 0x01, 0x8D, PPTR_HI & 0xFF, PPTR_HI >> 8)

    patch_positions = _emit_tod_poll_rxevent(
        a,
        got_label="got",
        timeout_label="timeout",
        min_ok_label="min_ok1",
        done_label="tod_done1",
        poll_label="poll_top",
    )

    a.label("got")
    _emit_read_frame(a, rx_buf)

    def chk(off: int, val: int, fail: str) -> None:
        addr = rx_buf + off
        a.emit(0xAD, addr & 0xFF, (addr >> 8) & 0xFF)
        a.emit(0xC9, val & 0xFF)
        a.branch(0xD0, fail)

    chk(12, 0x08, "drop")
    chk(13, 0x00, "drop")
    chk(23, 0x01, "drop")   # ICMP
    chk(34, 0x08, "drop")   # type = echo request
    chk(30, my_ip[0], "drop")
    chk(31, my_ip[1], "drop")
    chk(32, my_ip[2], "drop")
    chk(33, my_ip[3], "drop")
    a.jmp("transform")

    a.label("drop")
    a.emit(0xA9, 0x24, 0x8D, PPTR_LO & 0xFF, PPTR_LO >> 8)
    a.emit(0xA9, 0x01, 0x8D, PPTR_HI & 0xFF, PPTR_HI >> 8)
    a.jmp("poll_top")

    a.label("transform")
    # Swap dest MAC [0..5] with src MAC [6..11]
    for i in range(6):
        dst = rx_buf + i
        src = rx_buf + 6 + i
        a.emit(0xAD, dst & 0xFF, (dst >> 8) & 0xFF)
        a.emit(0xAE, src & 0xFF, (src >> 8) & 0xFF)
        a.emit(0x8E, dst & 0xFF, (dst >> 8) & 0xFF)
        a.emit(0x8D, src & 0xFF, (src >> 8) & 0xFF)

    # Swap src IP [26..29] with dst IP [30..33]
    for i in range(4):
        dst = rx_buf + 26 + i
        src = rx_buf + 30 + i
        a.emit(0xAD, dst & 0xFF, (dst >> 8) & 0xFF)
        a.emit(0xAE, src & 0xFF, (src >> 8) & 0xFF)
        a.emit(0x8E, dst & 0xFF, (dst >> 8) & 0xFF)
        a.emit(0x8D, src & 0xFF, (src >> 8) & 0xFF)

    # ICMP type := 0
    type_addr = rx_buf + 34
    a.emit(0xA9, 0x00, 0x8D, type_addr & 0xFF, (type_addr >> 8) & 0xFF)

    # ICMP checksum += 0x0008 (type went from 8 to 0)
    ck_hi = rx_buf + 36
    ck_lo = rx_buf + 37
    a.emit(0xAD, ck_hi & 0xFF, (ck_hi >> 8) & 0xFF)
    a.emit(0x18)
    a.emit(0x69, 0x08)
    a.emit(0x8D, ck_hi & 0xFF, (ck_hi >> 8) & 0xFF)
    a.branch(0x90, "ck_done")
    a.emit(0xAD, ck_lo & 0xFF, (ck_lo >> 8) & 0xFF)
    a.emit(0x18)
    a.emit(0x69, 0x01)
    a.emit(0x8D, ck_lo & 0xFF, (ck_lo >> 8) & 0xFF)
    a.label("ck_done")

    # Wait TxRdy then TX _FIXED_RX_BYTES from rx_buf
    a.emit(0xA9, 0xC0, 0x8D, TXCMD_LO & 0xFF, TXCMD_LO >> 8)
    a.emit(0xA9, 0x00, 0x8D, TXCMD_HI & 0xFF, TXCMD_HI >> 8)
    a.emit(0xA9, _FIXED_RX_BYTES & 0xFF, 0x8D, TXLEN_LO & 0xFF, TXLEN_LO >> 8)
    a.emit(0xA9, 0x00, 0x8D, TXLEN_HI & 0xFF, TXLEN_HI >> 8)
    a.emit(0xA9, 0x38, 0x8D, PPTR_LO & 0xFF, PPTR_LO >> 8)
    a.emit(0xA9, 0x01, 0x8D, PPTR_HI & 0xFF, PPTR_HI >> 8)
    a.label("tw2")
    a.emit(0xAD, PPDATA_HI & 0xFF, PPDATA_HI >> 8)
    a.emit(0x29, 0x01)
    a.branch(0xF0, "tw2")

    a.emit(0xA9, rx_buf & 0xFF, 0x85, 0xFB)
    a.emit(0xA9, (rx_buf >> 8) & 0xFF, 0x85, 0xFC)
    a.emit(0xA0, 0x00)
    a.label("txlp2")
    a.emit(0xB1, 0xFB)
    a.emit(0x8D, RTDATA_LO & 0xFF, RTDATA_LO >> 8)
    a.emit(0xC8)
    a.emit(0xB1, 0xFB)
    a.emit(0x8D, RTDATA_HI & 0xFF, RTDATA_HI >> 8)
    a.emit(0xC8)
    a.emit(0xC0, _FIXED_RX_BYTES)
    a.branch(0xD0, "txlp2")

    a.emit(0xA9, 0x01, 0x8D, result_addr & 0xFF, (result_addr >> 8) & 0xFF)
    a.emit(0x58)
    a.emit(0x60)

    a.label("timeout")
    a.emit(0xA9, 0xFF, 0x8D, result_addr & 0xFF, (result_addr >> 8) & 0xFF)
    a.emit(0x58)
    a.emit(0x60)

    _emit_tod_sec_table(a, "sec_tab")
    _emit_tod_ones_table(a, "ones_tab")

    raw = a.build()
    sec_tab_addr = load_addr + a.labels["sec_tab"]
    ones_tab_addr = load_addr + a.labels["ones_tab"]
    buf = bytearray(raw)
    _patch_tod_tables(buf, sec_tab_addr, ones_tab_addr, patch_positions)
    return bytes(buf)
