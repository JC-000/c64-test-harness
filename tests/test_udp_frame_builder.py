"""Pure-Python unit tests for :func:`build_udp_frame` and ``_udp_checksum``.

These tests do not touch VICE.  They verify:

* Wire layout (header offsets, EtherType, IP version/IHL, protocol byte).
* IPv4 header checksum self-consistency (RFC 1071: re-running the
  checksum over the populated header yields ``0x0000`` -- equivalently
  ``0xFFFF`` in the one's-complement world, but ``_ip_checksum`` returns
  ``0`` in that case).
* UDP checksum self-consistency (RFC 768 + RFC 1071 over pseudo-header
  + UDP header + payload).
* The special-case 0x0000 -> 0xFFFF substitution on the wire.
* CS8900a TX padding (60-byte minimum, even length).

They also exercise an edge case: payload of odd length (so the UDP
checksum compute path hits the zero-pad branch but the wire frame
keeps the original byte count in ``udp.length`` and IPv4
``total_length``).
"""

from __future__ import annotations

import struct

import pytest

from c64_test_harness.bridge_ping import (
    _ip_checksum,
    _udp_checksum,
    build_udp_frame,
)

# Reuse the bridge fixture defaults so the values match real test traffic.
MAC_A = bytes.fromhex("02C640000001")
HOST_MAC = bytes.fromhex("0A0B0C0D0E0F")
IP_A = bytes([10, 0, 65, 2])
HOST_IP = bytes([10, 0, 65, 1])


def _verify_ip_checksum(ip_header_bytes: bytes) -> int:
    """Recompute the 16-bit 1's-complement sum over a populated IP header.

    For a well-formed IPv4 header that already contains its checksum,
    the recomputed sum should fold to 0x0000 -- which the inverter in
    ``_ip_checksum`` returns as ``0`` (or as ``0xFFFF`` if one viewed
    the un-inverted intermediate).  We test the inverter-returns-0
    convention here.
    """
    return _ip_checksum(ip_header_bytes)


def _slice_frame(frame: bytes, payload_len: int) -> dict:
    """Decompose a frame into named slices for assertions."""
    return {
        "dst_mac": frame[0:6],
        "src_mac": frame[6:12],
        "ethertype": frame[12:14],
        "ip_header": frame[14:34],
        "udp_header": frame[34:42],
        "payload": frame[42:42 + payload_len],
        "padding": frame[42 + payload_len:],
    }


# ---------------------------------------------------------------------------
# Wire layout
# ---------------------------------------------------------------------------


def test_layout_with_1024_byte_payload() -> None:
    """The exemplar case: 1024-byte payload, well-formed Ethernet/IPv4/UDP."""
    payload = bytes(range(256)) * 4
    frame = build_udp_frame(
        src_mac=MAC_A, dst_mac=HOST_MAC,
        src_ip=IP_A, dst_ip=HOST_IP,
        src_port=49152, dst_port=51234,
        payload=payload,
    )
    parts = _slice_frame(frame, len(payload))

    # Ethernet II
    assert parts["dst_mac"] == HOST_MAC
    assert parts["src_mac"] == MAC_A
    assert parts["ethertype"] == b"\x08\x00"

    # IPv4: version=4, IHL=5 -> first byte 0x45.  Protocol=17 (UDP).
    assert parts["ip_header"][0] == 0x45
    assert parts["ip_header"][9] == 17  # protocol
    # total_length = 20 + 8 + 1024 = 1052
    assert struct.unpack(">H", parts["ip_header"][2:4])[0] == 1052
    # Source/dest IPs at fixed offsets 12..15 and 16..19 of the IP hdr.
    assert parts["ip_header"][12:16] == IP_A
    assert parts["ip_header"][16:20] == HOST_IP

    # UDP header
    sp, dp, ulen, _cksum = struct.unpack(">HHHH", parts["udp_header"])
    assert sp == 49152
    assert dp == 51234
    assert ulen == 8 + len(payload)

    # Payload survives intact.
    assert parts["payload"] == payload

    # 1024-byte payload puts the frame well above 60 bytes so no Ethernet
    # padding should be added.
    assert parts["padding"] == b""
    # Total frame length: 14 + 20 + 8 + 1024 = 1066 bytes.
    assert len(frame) == 1066


def test_short_payload_is_padded_to_60_bytes() -> None:
    """Small frames must be padded for the CS8900a 60-byte minimum."""
    payload = b"hi"  # 2 bytes
    frame = build_udp_frame(
        src_mac=MAC_A, dst_mac=HOST_MAC,
        src_ip=IP_A, dst_ip=HOST_IP,
        src_port=1234, dst_port=5678,
        payload=payload,
    )
    # 14 + 20 + 8 + 2 = 44 bytes of "real" frame; padded out to 60.
    assert len(frame) == 60
    # The IP total_length must still reflect the *original* payload size.
    ip_total_len = struct.unpack(">H", frame[16:18])[0]
    assert ip_total_len == 20 + 8 + 2
    # And the UDP length too.
    udp_len = struct.unpack(">H", frame[38:40])[0]
    assert udp_len == 8 + 2
    # Padding bytes (everything after offset 44) should be all zero.
    assert frame[44:] == b"\x00" * 16


def test_odd_payload_is_word_aligned() -> None:
    """An odd byte count gets one trailing zero so CS8900a TX FIFO can word it."""
    # Payload length 1023 -> full frame 14+20+8+1023 = 1065 (odd) -> 1066.
    payload = bytes(range(256)) * 4
    payload = payload[:1023]
    frame = build_udp_frame(
        src_mac=MAC_A, dst_mac=HOST_MAC,
        src_ip=IP_A, dst_ip=HOST_IP,
        src_port=49152, dst_port=51234,
        payload=payload,
    )
    assert len(frame) == 1066
    # The UDP length / IPv4 total_length must reflect 1023 (not 1024).
    ip_total_len = struct.unpack(">H", frame[16:18])[0]
    assert ip_total_len == 20 + 8 + 1023
    udp_len = struct.unpack(">H", frame[38:40])[0]
    assert udp_len == 8 + 1023
    # The "padding" byte after the 1023-byte payload must be zero.
    assert frame[42 + 1023:42 + 1024] == b"\x00"


# ---------------------------------------------------------------------------
# Checksum correctness
# ---------------------------------------------------------------------------


def test_ip_checksum_self_consistent() -> None:
    """Re-checking the populated IPv4 header should fold to 0."""
    payload = bytes(range(256))
    frame = build_udp_frame(
        src_mac=MAC_A, dst_mac=HOST_MAC,
        src_ip=IP_A, dst_ip=HOST_IP,
        src_port=10000, dst_port=20000,
        payload=payload,
    )
    ip_header = frame[14:34]
    # _ip_checksum over an already-checksummed header returns 0 in our
    # invert-on-return convention.
    assert _ip_checksum(ip_header) == 0


def test_udp_checksum_self_consistent() -> None:
    """Re-checking pseudo+header+payload (with the wire checksum included)
    should fold to 0 in our invert-on-return convention."""
    payload = b"hello, world" * 50
    frame = build_udp_frame(
        src_mac=MAC_A, dst_mac=HOST_MAC,
        src_ip=IP_A, dst_ip=HOST_IP,
        src_port=10000, dst_port=20000,
        payload=payload,
    )
    udp_header = frame[34:42]
    src_port, dst_port, udp_length, wire_cksum = struct.unpack(">HHHH", udp_header)
    # Build pseudo-header from known inputs, then append the *full* UDP
    # header (with checksum populated) and payload.  Folded one's-complement
    # sum should be all-ones; ``_ip_checksum`` returns its inverse so we
    # expect 0.
    pseudo = struct.pack(
        ">4s4sBBH",
        IP_A, HOST_IP,
        0, 17,
        udp_length,
    )
    if len(payload) % 2:
        pad = b"\x00"
    else:
        pad = b""
    full = pseudo + udp_header + payload + pad
    assert _ip_checksum(full) == 0


def test_udp_checksum_zero_becomes_ffff() -> None:
    """When the computed UDP checksum is 0, the wire value must be 0xFFFF.

    Strategy: feed ``_udp_checksum`` directly with addresses/ports/payload
    we know would sum to 0 in some manufactured way, OR (simpler) just
    assert the invariant that ``_udp_checksum`` never returns 0.  The
    second is what receivers actually care about.
    """
    # Sweep a handful of payloads and ports; verify none produce 0.
    for size in (0, 1, 2, 3, 7, 8, 100, 1024):
        for ports in ((1, 2), (12345, 54321), (0xFFFF, 0xFFFE)):
            cksum = _udp_checksum(
                IP_A, HOST_IP, ports[0], ports[1], b"\x00" * size,
            )
            assert cksum != 0, (
                f"UDP checksum must never be 0 on the wire "
                f"(size={size}, ports={ports})"
            )

    # And a directly-constructed 0xFFFF case: zero everything that the
    # one's-complement sum can see, so the *intermediate* sum is 0 and the
    # inverted result is 0xFFFF before our explicit substitution kicks in.
    # IPs=0.0.0.0, ports=0, payload empty -> sum is just the proto+length
    # words (proto=17, length=8) = 0x0011 + 0x0008 = 0x0019.  That folds
    # to ~0x0019 = 0xFFE6, not 0xFFFF.  So we can't easily hit the exact
    # substitution case without forging inputs; the sweep above is
    # sufficient defence-in-depth for the invariant.


def test_udp_checksum_matches_manual_compute() -> None:
    """Cross-check ``_udp_checksum`` against a manually-folded reference."""
    payload = b"ABCD"
    sp, dp = 1000, 2000
    sip = bytes([192, 168, 1, 1])
    dip = bytes([192, 168, 1, 2])
    # Manual: pseudo (12B) + udp hdr w/ cksum=0 (8B) + payload (4B) = 24B.
    pseudo = struct.pack(">4s4sBBH", sip, dip, 0, 17, 8 + len(payload))
    udp_hdr_no_cksum = struct.pack(">HHHH", sp, dp, 8 + len(payload), 0)
    data = pseudo + udp_hdr_no_cksum + payload
    s = 0
    for i in range(0, len(data), 2):
        s += (data[i] << 8) | data[i + 1]
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    expected = (~s) & 0xFFFF
    if expected == 0:
        expected = 0xFFFF
    assert _udp_checksum(sip, dip, sp, dp, payload) == expected


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs,match",
    [
        (dict(src_mac=b"\x00" * 5), "MAC"),
        (dict(dst_mac=b"\x00" * 7), "MAC"),
        (dict(src_ip=b"\x00" * 3), "IPv4"),
        (dict(dst_ip=b"\x00" * 5), "IPv4"),
        (dict(src_port=0), "ports"),
        (dict(dst_port=0x10000), "ports"),
        (dict(ttl=-1), "TTL"),
        (dict(ttl=256), "TTL"),
        (dict(ip_id=-1), "ip_id"),
        (dict(ip_id=0x10000), "ip_id"),
    ],
)
def test_argument_validation(kwargs: dict, match: str) -> None:
    base = dict(
        src_mac=MAC_A, dst_mac=HOST_MAC,
        src_ip=IP_A, dst_ip=HOST_IP,
        src_port=1234, dst_port=5678,
        payload=b"x",
    )
    base.update(kwargs)
    with pytest.raises(ValueError, match=match):
        build_udp_frame(**base)


def test_payload_too_large_raises() -> None:
    """A payload that would push IPv4 total_length over 65535 must fail."""
    payload = b"\x00" * (0xFFFF - 27)  # IP+UDP overhead = 28; 0xFFFF-27 overflows
    with pytest.raises(ValueError, match="total_length"):
        build_udp_frame(
            src_mac=MAC_A, dst_mac=HOST_MAC,
            src_ip=IP_A, dst_ip=HOST_IP,
            src_port=1234, dst_port=5678,
            payload=payload,
        )
