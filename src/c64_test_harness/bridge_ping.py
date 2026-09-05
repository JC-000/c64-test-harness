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

ARP (issue #218): :func:`build_arp_request_frame` / :func:`parse_arp` on
the host side; the ping builders take ``arp_frame_buf`` to resolve before
they ping, and the responders take ``my_mac`` to answer requests for
their IP -- both opt-in, both mirroring ip65's ``arp.s``.  A host that
never sees an ARP exchange queues every echo reply (issue #212).

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
# UDP frame builder (Ethernet + IPv4 + UDP, IPv4 only, no fragmentation)
#
# Companion to :func:`build_echo_request_frame` but for UDP datagrams.
# Caller supplies the L2/L3/L4 addressing and the payload; the helper
# returns a wire-ready frame with both IPv4 and UDP checksums populated.
#
# Limits:
#   * IPv4 only (Ethernet II EtherType 0x0800; IP version 4, IHL 5).
#   * No fragmentation -- caller is responsible for keeping
#     (14 + 20 + 8 + len(payload)) <= path MTU.  At the standard
#     1500-byte Ethernet MTU that caps the payload at 1472 bytes.
#   * No options.  Caller may not supply IP/UDP options.
#   * UDP checksum is always computed (the optional-checksum=0 case
#     is intentionally not used: every real Linux/macOS stack accepts
#     a populated checksum and most accept 0, but supplying the real
#     value avoids surprises when packet captures are scrutinised).
# ---------------------------------------------------------------------------

# IPv4 protocol number for UDP (RFC 768)
_IP_PROTO_UDP = 17


def _udp_checksum(
    src_ip: bytes,
    dst_ip: bytes,
    src_port: int,
    dst_port: int,
    payload: bytes,
) -> int:
    """Compute the UDP checksum for IPv4 (RFC 768 + RFC 1071 fold).

    The wire checksum is a 16-bit one's-complement sum over:

      1. The 12-byte pseudo-header
         ``src_ip | dst_ip | 0x00 | proto=17 | udp_length``
      2. The 8-byte UDP header
         ``src_port | dst_port | udp_length | checksum=0``
      3. The payload, zero-padded to an even byte count for the
         purpose of the sum (the wire payload is *not* padded).

    A computed result of 0x0000 is replaced with 0xFFFF on the wire
    so receivers can distinguish "checksum present and zero" from the
    "checksum disabled" sentinel.
    """
    assert len(src_ip) == 4 and len(dst_ip) == 4
    udp_length = 8 + len(payload)
    pseudo = struct.pack(
        ">4s4sBBH",
        src_ip, dst_ip,
        0, _IP_PROTO_UDP,
        udp_length,
    )
    udp_hdr_no_cksum = struct.pack(
        ">HHHH",
        src_port, dst_port,
        udp_length,
        0,
    )
    data = pseudo + udp_hdr_no_cksum + payload
    if len(data) % 2:
        data = data + b"\x00"
    s = 0
    for i in range(0, len(data), 2):
        s += (data[i] << 8) | data[i + 1]
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    cksum = (~s) & 0xFFFF
    # Per RFC 768: a computed 0 must be transmitted as all-ones.
    return cksum if cksum != 0 else 0xFFFF


def build_udp_frame(
    src_mac: bytes,
    dst_mac: bytes,
    src_ip: bytes,
    dst_ip: bytes,
    src_port: int,
    dst_port: int,
    payload: bytes,
    *,
    ttl: int = 64,
    ip_id: int = 0x0000,
) -> bytes:
    """Build a complete Ethernet + IPv4 + UDP frame.

    Layout (offsets within the returned ``bytes``):

    ===  ===  ===========================================
    Off  Len  Field
    ===  ===  ===========================================
      0    6  Destination MAC
      6    6  Source MAC
     12    2  EtherType (0x0800)
     14   20  IPv4 header (IHL=5, no options)
     34    8  UDP header
     42    N  UDP payload
    ===  ===  ===========================================

    Both checksums are computed:

    * IPv4 header checksum -- 16-bit 1's complement over the 20-byte
      header with the checksum field zeroed during compute (RFC 1071).
    * UDP checksum -- pseudo-header + UDP header + payload (zero-padded
      for the compute only); RFC 768 / RFC 1071.  A computed 0 becomes
      0xFFFF on the wire so it does not collide with the
      ``checksum-disabled`` sentinel.

    The returned frame is **pad-aligned to the CS8900a transmit
    requirements**:

    * Minimum 60 bytes (the chip adds the 4-byte FCS for a 64-byte
      on-wire frame).  Frames shorter than 60 bytes are zero-padded.
    * Length is rounded up to an even byte count -- the CS8900a TX
      FIFO is word-oriented; an odd byte tail would leave half a word
      stuck.  Padding bytes are post-payload zeros and are *not*
      reflected in the IPv4 ``total_length`` or the UDP ``length``
      fields, so receivers see exactly ``len(payload)`` UDP bytes.

    Parameters
    ----------
    src_mac, dst_mac
        6-byte Ethernet MAC addresses.  Use ``b"\\xff\\xff\\xff\\xff\\xff\\xff"``
        for broadcast or the host bridge's MAC for unicast to the host.
    src_ip, dst_ip
        4-byte IPv4 addresses (network order).  E.g.
        ``bytes([10, 0, 65, 2])`` for ``10.0.65.2``.
    src_port, dst_port
        UDP ports in host byte order (1-65535).
    payload
        UDP payload bytes.  May exceed 512 bytes but must keep the
        full frame under the path MTU (no fragmentation).
    ttl
        IPv4 Time-To-Live.  Default 64 matches Linux/macOS hosts.
    ip_id
        IPv4 Identification field.  Default 0; this builder never
        fragments, but a *receiver* that reassembles keyed on
        (src, dst, proto, id) will fuse distinct datagrams that share
        the default -- ip65's own reassembler did exactly that, silently
        merging seven staged datagrams into two (found by the
        c64-wireguard project).  Pass a distinct ``ip_id`` per datagram
        when staging several.

    Returns
    -------
    bytes
        The full Ethernet frame ready to upload to C64 RAM and feed
        through :func:`build_tx_code`.
    """
    if len(src_mac) != 6 or len(dst_mac) != 6:
        raise ValueError("MAC addresses must be 6 bytes")
    if len(src_ip) != 4 or len(dst_ip) != 4:
        raise ValueError("IPv4 addresses must be 4 bytes")
    if not (0 < src_port < 0x10000) or not (0 < dst_port < 0x10000):
        raise ValueError("UDP ports must be in 1..65535")
    if not (0 <= ttl <= 255):
        raise ValueError("TTL must be in 0..255")
    if not (0 <= ip_id <= 0xFFFF):
        raise ValueError("ip_id must be in 0..65535")

    udp_length = 8 + len(payload)
    ip_total_len = 20 + udp_length
    if ip_total_len > 0xFFFF:
        raise ValueError(
            f"IPv4 total_length {ip_total_len} exceeds 65535; "
            "split the payload before calling build_udp_frame()"
        )

    # --- UDP header ---
    udp_cksum = _udp_checksum(src_ip, dst_ip, src_port, dst_port, payload)
    udp_header = struct.pack(
        ">HHHH",
        src_port, dst_port,
        udp_length,
        udp_cksum,
    )

    # --- IPv4 header (checksum computed with the field set to 0) ---
    # Version=4, IHL=5 -> first byte 0x45.
    # TOS=0.  Flags=0 (DF=0, MF=0), Fragment offset=0.  Protocol=UDP.
    ip_no_cksum = struct.pack(
        ">BBHHHBBH4s4s",
        0x45, 0x00, ip_total_len,
        ip_id, 0x0000,
        ttl, _IP_PROTO_UDP, 0x0000,
        src_ip, dst_ip,
    )
    ip_cksum = _ip_checksum(ip_no_cksum)
    ip_header = struct.pack(
        ">BBHHHBBH4s4s",
        0x45, 0x00, ip_total_len,
        ip_id, 0x0000,
        ttl, _IP_PROTO_UDP, ip_cksum,
        src_ip, dst_ip,
    )

    # --- Ethernet II header + IP + UDP + payload ---
    frame = dst_mac + src_mac + b"\x08\x00" + ip_header + udp_header + payload

    # CS8900a TX padding: 60-byte minimum (chip appends 4-byte FCS), and
    # the TX FIFO is word-oriented so length must be even.
    if len(frame) < 60:
        frame = frame + b"\x00" * (60 - len(frame))
    if len(frame) % 2:
        frame = frame + b"\x00"
    return frame


# ---------------------------------------------------------------------------
# ARP (RFC 826) frame builders and parser -- issue #218
#
# The 6502 routines below neither sent nor answered ARP until #218.  On a
# macOS host that costs every echo reply: the host keeps a stale neighbour
# entry and queues the replies behind revalidation, so a pinger that never
# ARPs gets 0/8 with the requests visibly leaving the wire, and 6/6 once one
# ARP request precedes the ping (issue #212).  ip65 does both -- ``icmp_ping``
# resolves first (``ip65/arp.s`` ``arp_lookup``) and ``arp_process`` answers
# requests for its own address -- and is immune.  These helpers build the
# frames the 6502 side transmits and parse the ones it receives; the field
# offsets are ip65's ``ap_*`` constants.
# ---------------------------------------------------------------------------

#: EtherType of ARP.
ETHERTYPE_ARP = 0x0806
#: Length of the frames :func:`build_arp_request_frame` and
#: :func:`build_arp_reply_frame` return: a 42-byte packet zero-padded to
#: the ethernet minimum, which is also exactly what the fixed-length frame
#: reader drains (:data:`_FIXED_RX_BYTES`), so a received request's fields
#: sit at the same offsets in ``rx_buf`` as in these frames.
ARP_FRAME_LEN = 60
ARP_OP_REQUEST = 1
ARP_OP_REPLY = 2
#: Result byte :func:`build_read_and_respond_echo_request_code` stores after
#: answering an ARP request: the frame was consumed and a reply transmitted,
#: and the caller should keep polling for the echo request.
RESULT_ARP_REPLY_SENT = 0x03

# Byte offsets within an ethernet+ARP frame (ip65 arp.s ``ap_*``).
_ARP_HW = 14            # hardware type (ethernet = 0x0001)
_ARP_PROTO = 16         # protocol type (IPv4 = 0x0800)
_ARP_HWLEN = 18         # 6
_ARP_PROTOLEN = 19      # 4
_ARP_OP = 20            # 1 request, 2 reply
_ARP_SHW = 22           # sender hardware address
_ARP_SP = 28            # sender protocol address
_ARP_THW = 32           # target hardware address
_ARP_TP = 38            # target protocol address
_ARP_PACKLEN = 42       # ethernet header + ARP packet

_BROADCAST_MAC = b"\xff" * 6


@dataclass(frozen=True)
class ArpPacket:
    """The addressing fields of a parsed ethernet+ARP frame.

    ``dst_mac``/``src_mac`` are the ethernet header's; the other four are
    the ARP body's (RFC 826 SHA/SPA/THA/TPA).
    """

    dst_mac: bytes
    src_mac: bytes
    opcode: int
    sender_mac: bytes
    sender_ip: bytes
    target_mac: bytes
    target_ip: bytes

    @property
    def is_request(self) -> bool:
        return self.opcode == ARP_OP_REQUEST

    @property
    def is_reply(self) -> bool:
        return self.opcode == ARP_OP_REPLY


def _check_mac(name: str, mac: bytes) -> None:
    if len(mac) != 6:
        raise ValueError(f"{name} must be 6 bytes, got {len(mac)}")


def _check_ip(name: str, ip: bytes) -> None:
    if len(ip) != 4:
        raise ValueError(f"{name} must be 4 bytes, got {len(ip)}")


def _arp_frame(
    dst_mac: bytes,
    src_mac: bytes,
    opcode: int,
    sender_mac: bytes,
    sender_ip: bytes,
    target_mac: bytes,
    target_ip: bytes,
) -> bytes:
    body = (
        dst_mac + src_mac + struct.pack(">H", ETHERTYPE_ARP)
        + struct.pack(">HHBBH", 0x0001, 0x0800, 6, 4, opcode)
        + sender_mac + sender_ip + target_mac + target_ip
    )
    assert len(body) == _ARP_PACKLEN
    return body + b"\x00" * (ARP_FRAME_LEN - len(body))


def build_arp_request_frame(src_mac: bytes, src_ip: bytes, target_ip: bytes) -> bytes:
    """Broadcast ARP request "who has *target_ip*, tell *src_ip*".

    What ip65's ``arp_lookup`` sends on a cache miss: ethernet destination
    ``ff:ff:ff:ff:ff:ff``, sender fields ours, target hardware address
    zero.  Returns :data:`ARP_FRAME_LEN` (60) bytes, wire-ready for
    :func:`build_tx_code` or the ``arp_frame_buf`` parameter of the
    ``build_ping_and_wait*`` builders.  Transmit one before the first ping
    to a host whose neighbour cache may be stale (issue #212).
    """
    _check_mac("src_mac", src_mac)
    _check_ip("src_ip", src_ip)
    _check_ip("target_ip", target_ip)
    return _arp_frame(
        _BROADCAST_MAC, src_mac, ARP_OP_REQUEST,
        src_mac, src_ip, b"\x00" * 6, target_ip,
    )


def build_arp_reply_frame(
    src_mac: bytes, src_ip: bytes, target_mac: bytes, target_ip: bytes,
) -> bytes:
    """Unicast ARP reply "*src_ip* is at *src_mac*" to *target_mac*/*target_ip*.

    The frame the 6502 responders emit in reply to a request for their
    IP (built in place from the request, so this is the host-side twin
    used to verify them).  Returns :data:`ARP_FRAME_LEN` bytes.
    """
    _check_mac("src_mac", src_mac)
    _check_ip("src_ip", src_ip)
    _check_mac("target_mac", target_mac)
    _check_ip("target_ip", target_ip)
    return _arp_frame(
        target_mac, src_mac, ARP_OP_REPLY,
        src_mac, src_ip, target_mac, target_ip,
    )


def parse_arp(frame: bytes) -> ArpPacket | None:
    """Parse an ethernet frame as ARP; ``None`` unless it is ethernet/IPv4 ARP.

    Accepts the unpadded 42-byte packet as well as a padded one.  Returns
    ``None`` for any other ethertype, a short frame, or an ARP packet
    whose hardware/protocol types or address lengths are not ethernet
    (6-byte) over IPv4 (4-byte) -- the only combination the 6502 side
    handles.
    """
    if len(frame) < _ARP_PACKLEN:
        return None
    if frame[12:14] != struct.pack(">H", ETHERTYPE_ARP):
        return None
    hw, proto, hwlen, protolen, op = struct.unpack(">HHBBH", frame[_ARP_HW:_ARP_SHW])
    if (hw, proto, hwlen, protolen) != (0x0001, 0x0800, 6, 4):
        return None
    return ArpPacket(
        dst_mac=bytes(frame[0:6]),
        src_mac=bytes(frame[6:12]),
        opcode=op,
        sender_mac=bytes(frame[_ARP_SHW:_ARP_SP]),
        sender_ip=bytes(frame[_ARP_SP:_ARP_THW]),
        target_mac=bytes(frame[_ARP_THW:_ARP_TP]),
        target_ip=bytes(frame[_ARP_TP:_ARP_PACKLEN]),
    )


# ---------------------------------------------------------------------------
# CS8900a initialisation blobs (same as tests/test_ethernet_bridge.py)
# ---------------------------------------------------------------------------

#: RxCTL (PP 0x0104): PromiscuousA | RxOKA | IndividualA | BroadcastA,
#: plus the register's own number in the low 6 bits.
#:
#: On a real CS8900a the low 6 bits of every control/status register are
#: **read-only and report the register's own number** -- measured reset
#: values say so across the board (RxCTL 0x0005, LineCTL 0x0013, SelfCTL
#: 0x0015, BusCTL 0x0017, BusST 0x0018).  The harness's old 0x00D8 reads
#: back as **0x00C5** -- the tell, not the cause.  The cause is that
#: 0x00D8 never contained RxOKA (0x0100) in the first place, and without
#: RxOKA the receiver accepts nothing.  It appeared to work under VICE
#: because cs8900.c's acceptance filter never consults RxOKA -- it accepts
#: on the address filter alone (issue #207).
#:
#: PromiscuousA is kept for one reason only: it is what every harness
#: routine has always programmed, and dropping it would silently narrow
#: what downstream tests receive.  It is *not* needed for correctness on
#: either backend -- ``ethernet.set_cs8900a_mac`` does reach VICE's
#: emulated chip (measured 2026-09-05: the 6510 reads the programmed IA
#: back), and the VICE bridge suites pass with
#: :data:`CS8900A_RXCTL_VALUE_IP65`.  ip65's ``cs8900a.s`` uses 0x0D05,
#: the same value without PromiscuousA, which is the right choice for a
#: real driver on a busy segment where promiscuous mode floods the RX
#: FIFO with traffic the caller does not want.  Pass it explicitly to
#: :func:`cs8900a_rxctl_inline_code` when that is what you want.
CS8900A_RXCTL_VALUE = 0x0D85

#: TxCMD (PP 0x0108): "transmit after the whole frame is in the FIFO"
#: (0x00C0) **plus the register's own number** in the low 6 bits, which is
#: 0x09.  The harness wrote a bare 0x00C0 for years with no measured
#: fault -- unlike the RxCTL case this is parity with ip65 (which writes
#: 0x00C9), adopted so the two drivers program the chip identically, not
#: a fix.  Whether a real chip reads PP 0x0108 back as 0x00C9 has not been
#: measured; do not cite it.
CS8900A_TXCMD_VALUE = 0x00C9

#: Mask applied to the high byte of RxEvent (PP 0x0124) when polling for a
#: received frame: RxOK (0x0100) | IndividualAdr (0x0400) | Broadcast
#: (0x0800).  The harness used to mask 0x01, i.e. RxOK alone, so a frame
#: the chip signalled without raising RxOK was invisible and the poll
#: simply timed out with the reply sitting in the FIFO.  ip65 masks 0x0D.
CS8900A_RXEVENT_MASK = 0x0D

#: ip65's RxCTL value: RxOKA | IndividualA | BroadcastA + register number,
#: i.e. :data:`CS8900A_RXCTL_VALUE` without PromiscuousA.  Accepts frames
#: addressed to the programmed Individual Address plus broadcast, and
#: nothing else.  This is the one place the harness diverges from ip65
#: on purpose; see :data:`CS8900A_RXCTL_VALUE` for why.
CS8900A_RXCTL_VALUE_IP65 = 0x0D05

#: LineCTL bits that let the chip move frames at all: SerRxON (bit 6) and
#: SerTxON (bit 7).  cs8900.c clears both on reset (:420-421) and sets them
#: only from a LineCTL write (:923-931); without them TX frames are dropped
#: at :780 after the routine has "successfully" written them, and RxOK is
#: never raised (:1060).
CS8900A_LINECTL_ENABLE = 0x00C0


def cs8900a_rxctl_inline_code(value: int = CS8900A_RXCTL_VALUE) -> bytes:
    """Clockport enable + ``RxCTL (PP 0x0104) = value``, **no RTS**.

    The inline form is what gets prepended to a larger routine; see
    :func:`cs8900a_rxctl_code` for the callable-blob form.
    """
    return _clockport_enable_bytes() + bytes([
        0xA9, 0x04, 0x8D, PPTR_LO & 0xFF, PPTR_LO >> 8,
        0xA9, 0x01, 0x8D, PPTR_HI & 0xFF, PPTR_HI >> 8,
        0xA9, value & 0xFF, 0x8D, PPDATA_LO & 0xFF, PPDATA_LO >> 8,
        0xA9, (value >> 8) & 0xFF, 0x8D, PPDATA_HI & 0xFF, PPDATA_HI >> 8,
    ])


def cs8900a_rxctl_code() -> bytes:
    """RxCTL (PP 0x0104) = :data:`CS8900A_RXCTL_VALUE`, then RTS.

    Enables the RR clockport first, then programs the register.  Verified
    live on both backends: the two-VICE bridge tests and a real CS8900a on
    a U64E expansion-port cartridge, which reads the value straight back
    (issue #207).
    """
    return cs8900a_rxctl_inline_code() + bytes([0x60])


def cs8900a_linectl_or_inline_code(mask: int = CS8900A_LINECTL_ENABLE) -> bytes:
    """``LineCTL (PP 0x0112) low byte |= mask`` as a read-OR-write, **no RTS**.

    Read-modify-write on the low byte only, so the other LineCTL bits
    (and the untouched high byte) survive; the mask defaults to
    SerRxON | SerTxON.  This is the inline equivalent of the three-step
    read / OR on the host / :func:`cs8900a_write_linectl_code` dance that
    ``tests/test_ethernet_bridge.py`` does, folded into the routine so a
    single JSR brings the chip up.
    """
    return bytes([
        0xA9, 0x12, 0x8D, PPTR_LO & 0xFF, PPTR_LO >> 8,
        0xA9, 0x01, 0x8D, PPTR_HI & 0xFF, PPTR_HI >> 8,
        0xAD, PPDATA_LO & 0xFF, PPDATA_LO >> 8,     # LDA PPData lo
        0x09, mask & 0xFF,                          # ORA #mask
        0x8D, PPDATA_LO & 0xFF, PPDATA_LO >> 8,     # STA PPData lo
    ])


def cs8900a_enable_inline_code() -> bytes:
    """Everything a fresh CS8900a needs before it will pass a frame, **no RTS**.

    RxCTL = :data:`CS8900A_RXCTL_VALUE` then LineCTL |= SerRxON | SerTxON.
    Prepend to any TX or RX routine that runs against a chip nobody else
    has initialised (the RR clockport is enabled as part of it).
    """
    return cs8900a_rxctl_inline_code() + cs8900a_linectl_or_inline_code()


def cs8900a_set_mac_inline_code(mac: bytes) -> bytes:
    """Program the Individual Address (PP 0x0158-0x015D) from the 6510, **no RTS**.

    The 6502-side counterpart to
    :func:`c64_test_harness.ethernet.set_cs8900a_mac`, which programs the
    same registers with host-side ``write_memory``.  That host-side route
    is **VICE-only**: on an Ultimate 64 a host write to ``$DE02``/``$DE04``
    goes through the machine's DMA engine and never reaches the expansion
    port, so on real hardware the MAC has to be written by code running on
    the 6510 (issue #209).

    Must follow a clockport enable -- prepend
    :func:`cs8900a_enable_inline_code`, which does it as part of bringing
    the chip up.

    :param mac: 6 raw bytes, in wire order.
    :raises ValueError: if *mac* is not exactly 6 bytes.
    """
    if len(mac) != 6:
        raise ValueError(f"MAC must be 6 bytes, got {len(mac)}")
    out = bytearray()
    for i in range(3):
        pp = 0x0158 + i * 2
        out += bytes([
            0xA9, pp & 0xFF, 0x8D, PPTR_LO & 0xFF, PPTR_LO >> 8,
            0xA9, pp >> 8, 0x8D, PPTR_HI & 0xFF, PPTR_HI >> 8,
            0xA9, mac[i * 2], 0x8D, PPDATA_LO & 0xFF, PPDATA_LO >> 8,
            0xA9, mac[i * 2 + 1], 0x8D, PPDATA_HI & 0xFF, PPDATA_HI >> 8,
        ])
    return bytes(out)


def cs8900a_set_mac_code(mac: bytes) -> bytes:
    """Clockport enable + program the Individual Address, then RTS.

    Callable-blob form of :func:`cs8900a_set_mac_inline_code`.
    """
    return (_clockport_enable_bytes() + cs8900a_set_mac_inline_code(mac)
            + bytes([0x60]))


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

def _emit_tx_frame(a: Asm, frame_buf: int, frame_len: int, prefix: str) -> None:
    """Emit the CS8900a TX handshake for ``frame_len`` bytes at ``frame_buf``.

    TxCMD = :data:`CS8900A_TXCMD_VALUE`, TxLength = ``frame_len``, PPPtr =
    BusST (0x0138), poll ``Rdy4TxNOW``, then copy the frame into RTDATA
    low half first through ``($FB),Y``.  This is the one TX sequence every
    builder emits; ``prefix`` keeps the two labels unique in a routine that
    transmits more than once (ARP request then echo request, or ARP reply
    then echo reply -- issue #218).  ``frame_len`` must be even and at
    most 256: the copy loop counts in Y.
    """
    a.emit(0xA9, CS8900A_TXCMD_VALUE & 0xFF, 0x8D, TXCMD_LO & 0xFF, TXCMD_LO >> 8)
    a.emit(0xA9, 0x00, 0x8D, TXCMD_HI & 0xFF, TXCMD_HI >> 8)
    a.emit(0xA9, frame_len & 0xFF, 0x8D, TXLEN_LO & 0xFF, TXLEN_LO >> 8)
    a.emit(0xA9, (frame_len >> 8) & 0xFF, 0x8D, TXLEN_HI & 0xFF, TXLEN_HI >> 8)
    a.emit(0xA9, 0x38, 0x8D, PPTR_LO & 0xFF, PPTR_LO >> 8)
    a.emit(0xA9, 0x01, 0x8D, PPTR_HI & 0xFF, PPTR_HI >> 8)
    a.label(f"{prefix}_txw")
    a.emit(0xAD, PPDATA_HI & 0xFF, PPDATA_HI >> 8)
    a.emit(0x29, 0x01)
    a.branch(0xF0, f"{prefix}_txw")
    a.emit(0xA9, frame_buf & 0xFF, 0x85, 0xFB)
    a.emit(0xA9, (frame_buf >> 8) & 0xFF, 0x85, 0xFC)
    a.emit(0xA0, 0x00)
    a.label(f"{prefix}_txlp")
    a.emit(0xB1, 0xFB)
    a.emit(0x8D, RTDATA_LO & 0xFF, RTDATA_LO >> 8)
    a.emit(0xC8)
    a.emit(0xB1, 0xFB)
    a.emit(0x8D, RTDATA_HI & 0xFF, RTDATA_HI >> 8)
    a.emit(0xC8)
    a.emit(0xC0, frame_len & 0xFF)
    a.branch(0xD0, f"{prefix}_txlp")


def _resolve_arp_frame(
    arp_frame_buf: int | None, arp_frame_len: int | None,
) -> tuple[int, int] | None:
    """``(buf, len)`` for the optional ARP-first transmit, or ``None``.

    ``arp_frame_len`` defaults to :data:`ARP_FRAME_LEN`, what
    :func:`build_arp_request_frame` returns; giving a length without a
    buffer is a caller mistake, not a silent no-op.
    """
    if arp_frame_buf is None:
        if arp_frame_len is not None:
            raise ValueError("arp_frame_len given without arp_frame_buf")
        return None
    return arp_frame_buf, (ARP_FRAME_LEN if arp_frame_len is None else arp_frame_len)


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
    _emit_tx_frame(a, frame_buf, frame_len, "tx")
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
      - Reads exactly _FIXED_RX_BYTES bytes into rx_buf in wire order.
        The fixed-length read avoids trusting RxLength (VICE's TFE
        emulation has been observed to return bogus RxLength on the
        first ICMP read with this caller pattern).
      - Zero page: only $FB/$FC (the destination pointer).

    The fixed-length read is sufficient because we only need to inspect
    the IP header (offset 14-33) and ICMP header (34-41), and ethernet
    frames are minimum 60 bytes anyway -- our test sends 60-byte frames.

    **Half ordering is not free choice** (issue #210).  The two-byte
    header words must be read HIGH half first; reading $DE08 before
    $DE09 desynchronises a real CS8900a's FIFO by one byte, after which
    RxLength is garbage and every data word arrives byte-swapped.  VICE's
    cs8900.c implements the same datasheet order but advances its RX
    pointer on the low read, so a low-first header still left the body
    aligned there (the discarded header bytes were wrong even under VICE,
    and nothing checked them) -- which is why the bug survived until the
    harness met real silicon.  This mirrors ip65's ``cs8900a.s``
    exactly: high-half-first for the two header words, low-half-first for
    the data body.  Measured against a byte-ramp payload on a U64E with an
    external RR-Net cartridge; see issue #210 for the raw FIFO dumps.

    Nothing is stored in $F1-$F4 any more.  It used to park RxStatus and
    RxLength there, which is exactly where the TOD poll loop keeps its
    deadline ($F2/$F3) and ones table ($F4) -- so every dropped frame
    corrupted the deadline of the routine that read it (issue #208).
    """
    # Header: RxStatus then RxLength, two words, HIGH half first.
    # Both are discarded -- the body read is fixed-length.
    for _ in range(2):
        a.emit(0xAD, RTDATA_HI & 0xFF, RTDATA_HI >> 8)
        a.emit(0xAD, RTDATA_LO & 0xFF, RTDATA_LO >> 8)

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
    # advanced to the start of the next frame.  Needed here because the
    # body read above is a fixed _FIXED_RX_BYTES, so a longer frame leaves
    # a tail in the FIFO.  Measured on hardware: one frame occupies exactly
    # 4 header bytes + RxLength data bytes, and every read past that
    # returns $00 until SkipNow is issued.  ip65 reads the whole frame and
    # never issues SkipNow; whether the skip is still required after a
    # complete read is unmeasured.
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
    a.emit(0x29, CS8900A_RXEVENT_MASK)
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


def _check_my_mac(my_mac: bytes | None) -> None:
    if my_mac is not None and len(my_mac) != 6:
        raise ValueError(f"my_mac must be 6 bytes, got {len(my_mac)}")


def _emit_arp_responder(
    a: Asm,
    rx_buf: int,
    my_ip: bytes,
    my_mac: bytes,
    *,
    drop_label: str,
    after_reply_label: str,
    prefix: str = "arp",
) -> None:
    """Answer an ARP request for ``my_ip`` sitting in ``rx_buf``; else fall through.

    ip65's ``arp_process`` ``@request`` path (``ip65/arp.s``), issue #218.
    Emitted between the frame read and the IPv4 checks of a responder:

    * ethertype != 0x0806 -> fall through to whatever follows (the ICMP
      path), so a non-ARP frame is handled exactly as before;
    * ARP but not a request, or a request for another IP -> ``JMP
      drop_label``;
    * a request for us -> rewrite ``rx_buf`` in place into the reply
      (ethernet dst and target hardware address := the sender's, target
      protocol address := the sender's, ethernet src and sender hardware
      address := ``my_mac``, sender protocol address := ``my_ip``, opcode
      := 2), transmit :data:`_FIXED_RX_BYTES` bytes of it, then ``JMP
      after_reply_label``.

    Offsets are ip65's ``ap_*``; the received request is at the same
    offsets because the reader drains a fixed 60 bytes and an ARP frame
    is 42 bytes padded to 60.  Clobbers A, X, and ``$FB/$FC`` (in the TX).
    """
    def chk(off: int, val: int, fail: str) -> None:
        addr = rx_buf + off
        a.emit(0xAD, addr & 0xFF, (addr >> 8) & 0xFF)
        a.emit(0xC9, val & 0xFF)
        a.branch(0xD0, fail)

    def abs_x(opcode: int, addr: int) -> None:
        a.emit(opcode, addr & 0xFF, (addr >> 8) & 0xFF)

    drop_t, not_t, reply, not_arp = (
        f"{prefix}_drop_t", f"{prefix}_not_t", f"{prefix}_reply", f"{prefix}_not_arp",
    )
    chk(12, 0x08, drop_t)          # ethertype hi (0x08 for both IPv4 and ARP)
    chk(13, 0x06, not_t)           # ethertype lo: 0x06 = ARP, else fall through
    chk(_ARP_OP, 0x00, drop_t)     # opcode hi
    chk(_ARP_OP + 1, ARP_OP_REQUEST, drop_t)
    for i in range(4):             # target protocol address == my_ip
        chk(_ARP_TP + i, my_ip[i], drop_t)
    a.jmp(reply)

    a.label(drop_t)
    a.jmp(drop_label)
    a.label(not_t)
    a.jmp(not_arp)

    a.label(reply)
    # eth dst [0..5] and target HW [32..37] := sender HW [22..27]
    a.emit(0xA2, 0x05)                       # LDX #5
    a.label(f"{prefix}_cp6")
    abs_x(0xBD, rx_buf + _ARP_SHW)           # LDA rx+22,X
    abs_x(0x9D, rx_buf + 0)                  # STA rx+0,X
    abs_x(0x9D, rx_buf + _ARP_THW)           # STA rx+32,X
    a.emit(0xCA)                             # DEX
    a.branch(0x10, f"{prefix}_cp6")          # BPL
    # target IP [38..41] := sender IP [28..31]
    a.emit(0xA2, 0x03)
    a.label(f"{prefix}_cp4")
    abs_x(0xBD, rx_buf + _ARP_SP)
    abs_x(0x9D, rx_buf + _ARP_TP)
    a.emit(0xCA)
    a.branch(0x10, f"{prefix}_cp4")
    # sender HW [22..27] and eth src [6..11] := my_mac
    for i in range(6):
        a.emit(0xA9, my_mac[i])
        a.emit(0x8D, (rx_buf + _ARP_SHW + i) & 0xFF, ((rx_buf + _ARP_SHW + i) >> 8) & 0xFF)
        a.emit(0x8D, (rx_buf + 6 + i) & 0xFF, ((rx_buf + 6 + i) >> 8) & 0xFF)
    # sender IP [28..31] := my_ip
    for i in range(4):
        a.emit(0xA9, my_ip[i])
        a.emit(0x8D, (rx_buf + _ARP_SP + i) & 0xFF, ((rx_buf + _ARP_SP + i) >> 8) & 0xFF)
    # opcode := reply (high byte already verified 0)
    a.emit(0xA9, ARP_OP_REPLY)
    a.emit(0x8D, (rx_buf + _ARP_OP + 1) & 0xFF, ((rx_buf + _ARP_OP + 1) >> 8) & 0xFF)

    _emit_tx_frame(a, rx_buf, _FIXED_RX_BYTES, prefix)
    a.jmp(after_reply_label)

    a.label(not_arp)


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
    *,
    arp_frame_buf: int | None = None,
    arp_frame_len: int | None = None,
) -> bytes:
    """Build a 6502 routine that TXes an echo request and waits for the reply.

    This combines :func:`build_tx_code` and :func:`build_rx_echo_reply_code`
    into a single routine, run via one ``jsr()`` call.  This is important
    because while the binary monitor is paused (between JSRs) the CS8900a
    may not pump TAP frames reliably, so TX and RX must happen without
    a CPU pause in between.

    ``arp_frame_buf`` (issue #218): address of a pre-built ARP request
    (:func:`build_arp_request_frame`; ``arp_frame_len`` defaults to its
    :data:`ARP_FRAME_LEN`).  When given, the routine transmits it *before*
    the echo request, in the same run, so a host whose neighbour cache is
    stale answers the ping instead of queuing the reply (issue #212).  The
    ARP reply that comes back is drained like any other non-matching frame.
    Without it the output is byte-identical to the pre-#218 routine.

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
    arp = _resolve_arp_frame(arp_frame_buf, arp_frame_len)
    id_hi = (identifier >> 8) & 0xFF
    id_lo = identifier & 0xFF
    seq_hi = (sequence >> 8) & 0xFF
    seq_lo = sequence & 0xFF

    a = Asm(org=load_addr)
    a.emit(0x78)  # SEI
    _emit_clockport_enable(a)

    # --- TX the ARP request first if asked to (issue #218), then the echo ---
    if arp is not None:
        _emit_tx_frame(a, arp[0], arp[1], "arp")
    _emit_tx_frame(a, tx_frame_buf, tx_frame_len, "pw")

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
    *,
    my_mac: bytes | None = None,
) -> bytes:
    """Build a 6502 routine that receives one ICMP echo request and replies.

    Polls RX, checks for an IPv4/ICMP echo request addressed to ``my_ip``,
    transforms it in place into an echo reply (swap MAC, swap IP, set
    type=0, patch ICMP checksum), and TXes it back.  Writes 0x01 or 0xFF
    to ``result_addr``.

    With ``my_mac`` (issue #218) the routine also answers ARP requests for
    ``my_ip`` while it waits -- reply transmitted, then back to polling
    for the echo request -- the way ip65's ``arp_process`` does, so a
    peer that must resolve us first gets an answer.  ARP frames that are
    not requests for ``my_ip`` are dropped.  Without ``my_mac`` the
    output is byte-identical to the pre-#218 routine and ARP is dropped
    like any other non-ICMP frame.

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
    _check_my_mac(my_mac)

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

    if my_mac is not None:
        _emit_arp_responder(a, rx_buf, my_ip, my_mac,
                            drop_label="drop", after_reply_label="drop")

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
    _emit_tx_frame(a, rx_buf, _FIXED_RX_BYTES, "reply")

    a.emit(0xA9, 0x01, 0x8D, result_addr & 0xFF, (result_addr >> 8) & 0xFF)
    a.emit(0x58)
    a.emit(0x60)

    a.label("timeout")
    a.emit(0xA9, 0xFF, 0x8D, result_addr & 0xFF, (result_addr >> 8) & 0xFF)
    a.emit(0x58)
    a.emit(0x60)

    return a.build()


# ---------------------------------------------------------------------------
# Host-side wall-clock pattern (preferred under VICE, warp included; VICE-only:
# it is built on jsr(), which Ultimate64Transport does not have -- use the
# *_tod_code builders with run_subroutine on hardware)
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

    Polls RxEvent (PP 0x0124, hi byte masked with
    :data:`CS8900A_RXEVENT_MASK`) for ``batch_size``
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
    a.emit(0x29, CS8900A_RXEVENT_MASK)               # AND #CS8900A_RXEVENT_MASK
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
    *,
    my_mac: bytes | None = None,
) -> bytes:
    """Drain a waiting echo request, swap+TX a reply, no polling.

    Assumes ``RxEvent`` has already fired.  Writes:

    * ``0x01`` -- request consumed and reply transmitted
    * ``0x02`` -- frame consumed but did not match (host should re-poll)
    * ``0x03`` (:data:`RESULT_ARP_REPLY_SENT`) -- the frame was an ARP
      request for ``my_ip`` and a reply was transmitted; host should
      re-poll.  Only with ``my_mac`` (issue #218); without it ARP is a
      non-match and the output is byte-identical to the pre-#218 routine.
    """
    assert len(my_ip) == 4
    _check_my_mac(my_mac)

    a = Asm(org=load_addr)
    a.emit(0x78)
    _emit_clockport_enable(a)

    _emit_read_frame(a, rx_buf)

    def chk(off: int, val: int, fail: str) -> None:
        addr = rx_buf + off
        a.emit(0xAD, addr & 0xFF, (addr >> 8) & 0xFF)
        a.emit(0xC9, val & 0xFF)
        a.branch(0xD0, fail)

    if my_mac is not None:
        _emit_arp_responder(a, rx_buf, my_ip, my_mac,
                            drop_label="rrm", after_reply_label="rr_arp_done")

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
    _emit_tx_frame(a, rx_buf, _FIXED_RX_BYTES, "reply")

    a.emit(0xA9, 0x01, 0x8D, result_addr & 0xFF, (result_addr >> 8) & 0xFF)
    a.emit(0x58)
    a.emit(0x60)

    a.label("rrm")
    a.emit(0xA9, 0x02, 0x8D, result_addr & 0xFF, (result_addr >> 8) & 0xFF)
    a.emit(0x58)
    a.emit(0x60)

    if my_mac is not None:
        a.label("rr_arp_done")
        a.emit(0xA9, RESULT_ARP_REPLY_SENT, 0x8D, result_addr & 0xFF, (result_addr >> 8) & 0xFF)
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
    arp: bool = True,
) -> int:
    """Transmit an echo request, then poll for a matching reply.

    Loads a TX routine, transmits ``tx_frame``, then loops:
    ``poll_until_ready`` -> ``read_and_match_echo_reply`` -> on mismatch,
    re-poll; on match, return ``0x01``; on wall-clock timeout, return
    ``0xFF``.

    With ``arp=True`` (the default; issue #218) an ARP request for the
    echo's destination IP -- built from ``tx_frame``'s own source MAC,
    source IP and destination IP -- is transmitted first, through the same
    ``tx_frame_buf`` and TX routine.  That is what ip65's ``icmp_ping``
    does and what a macOS peer needs before it will deliver replies
    (issue #212).  The ARP reply is consumed as a non-matching frame.
    ``tx_frame`` must be IPv4 for this; pass ``arp=False`` to send it raw.
    The addresses are read at the fixed offsets 26 and 30, i.e. an IPv4
    header without options (IHL=5) -- what :func:`build_echo_request_frame`
    builds and what every 6502 routine here assumes as well.

    The wall-clock budget is owned by Python via
    :func:`c64_test_harness.poll_until.poll_until_ready`, so this works
    correctly under VICE warp mode.  **VICE-only**: it is built on
    :func:`~c64_test_harness.execute.jsr`, which
    ``Ultimate64Transport`` does not provide (issue #209).  On hardware
    use :func:`build_ping_and_wait_tod_code` with ``run_subroutine``.
    """
    import time as _time
    from .execute import jsr, load_code
    from .memory import read_bytes, write_bytes
    from .poll_until import poll_until_ready

    frames = []
    if arp:
        if tx_frame[12:14] != b"\x08\x00":
            raise ValueError(
                "run_ping_and_wait(arp=True) needs an IPv4 tx_frame to derive the "
                f"ARP request from; ethertype is {tx_frame[12:14].hex()} "
                "(pass arp=False to transmit it as-is)"
            )
        frames.append(build_arp_request_frame(
            src_mac=tx_frame[6:12], src_ip=tx_frame[26:30], target_ip=tx_frame[30:34],
        ))
    frames.append(tx_frame)

    for frame in frames:
        tx_code = build_tx_code(
            load_addr=consume_addr,
            frame_buf=tx_frame_buf,
            frame_len=len(frame),
            result_addr=result_addr,
        )
        load_code(transport, consume_addr, tx_code)
        write_bytes(transport, tx_frame_buf, frame)
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
    my_mac: bytes | None = None,
) -> int:
    """Wait for an ICMP echo request and reply to it.

    Loops: ``poll_until_ready`` -> ``read_and_respond_echo_request`` ->
    on mismatch, re-poll; on success, return ``0x01``; on wall-clock
    timeout, return ``0xFF``.

    With ``my_mac`` (issue #218) ARP requests for ``my_ip`` are answered
    while waiting (the consume routine reports
    :data:`RESULT_ARP_REPLY_SENT` and the loop re-polls), so a peer that
    resolves before pinging -- :func:`run_ping_and_wait`'s default, ip65,
    any real IP stack -- gets its reply.
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
        my_mac=my_mac,
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
        if body_result in (0x02, RESULT_ARP_REPLY_SENT):
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
    a.emit(0x29, CS8900A_RXEVENT_MASK)
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
    *,
    arp_frame_buf: int | None = None,
    arp_frame_len: int | None = None,
) -> bytes:
    """Shippable-application ping-and-wait with CIA1 TOD timeout.

    Pure 6502 equivalent of :func:`build_ping_and_wait_code` but using
    CIA1 Time-of-Day as the deadline source.  Runs standalone on real
    C64 / U64E / VICE normal mode; **not** usable under VICE warp.

    Steps:

    1. Enable RR clockport; start CIA1 TOD at 00:00:00.0; store deadline.
    1a. If ``arp_frame_buf`` is given, TX the ARP request there first
        (issue #218; see :func:`build_ping_and_wait_code`).
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
        arp_frame_buf: Optional ARP request to transmit first.
        arp_frame_len: Its length; defaults to :data:`ARP_FRAME_LEN`.

    Raises:
        ValueError: if ``deadline_tenths`` is out of range, or
            ``arp_frame_len`` is given without ``arp_frame_buf``.
    """
    _validate_deadline_tenths(deadline_tenths)
    arp = _resolve_arp_frame(arp_frame_buf, arp_frame_len)
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

    # --- TX the ARP request first if asked to (issue #218), then the echo ---
    if arp is not None:
        _emit_tx_frame(a, arp[0], arp[1], "arp")
    _emit_tx_frame(a, tx_frame_buf, tx_frame_len, "pw")

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
    *,
    my_mac: bytes | None = None,
) -> bytes:
    """Shippable-application ICMP responder with CIA1 TOD timeout.

    Pure 6502 equivalent of :func:`build_icmp_responder_code`: polls
    the CS8900a RX queue, receives one ICMP echo request addressed to
    ``my_ip``, transforms it in place into an echo reply (swap MACs,
    swap IPs, set ICMP type=0, patch checksum), and transmits it.  Uses
    CIA1 Time-of-Day for the poll deadline.

    Writes ``0x01`` at ``result_addr`` on successful reply TX, ``0xFF``
    on TOD expiry.  Non-matching frames are drained and polling
    continues against the same deadline.  With ``my_mac`` (issue #218)
    ARP requests for ``my_ip`` are answered along the way, against the
    same deadline; see :func:`build_icmp_responder_code`.

    Args:
        load_addr: Where the routine will live.
        rx_buf: RX frame buffer (at least 64 bytes).
        my_ip: 4-byte IP address of this C64.
        result_addr: 1-byte status slot.
        deadline_tenths: Timeout in tenths-of-a-second (1..599).
        my_mac: 6-byte MAC to answer ARP with; ``None`` disables ARP.

    Raises:
        ValueError: if ``deadline_tenths`` is out of range or ``my_mac``
            is not 6 bytes.
    """
    _validate_deadline_tenths(deadline_tenths)
    assert len(my_ip) == 4
    _check_my_mac(my_mac)

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

    if my_mac is not None:
        _emit_arp_responder(a, rx_buf, my_ip, my_mac,
                            drop_label="drop", after_reply_label="drop")

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
    _emit_tx_frame(a, rx_buf, _FIXED_RX_BYTES, "reply")

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
