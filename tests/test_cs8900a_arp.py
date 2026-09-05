"""ARP in the CS8900a ping and responder routines (issue #218).

The harness's 6502 ping routines never sent or answered ARP.  On a
macOS host that costs every echo reply: the host keeps a stale neighbour
entry and queues the replies behind revalidation, so a pinger that never
ARPs gets 0/8 with its requests visibly leaving the wire, and 6/6 once
one ARP request precedes the ping (issue #212).  ip65 does both --
``icmp_ping`` resolves first (``ip65/arp.s`` ``arp_lookup``) and
``arp_process`` answers requests for its own address -- and is immune.

Three layers are pinned here, none of which needs VICE:

* **Frame builders and parser** against a byte layout written out by
  hand from RFC 826 (offsets are ip65's ``ap_*`` constants), never
  derived from the builder under test.
* **Structure of the emitted 6502** -- the compares and stores that make
  the routine an ARP responder, and the fall-through to the ICMP path for
  a frame that is not ARP.  Also that the default output of every builder
  is byte-identical to what master emitted before this change (SHA-256 of
  the bytes, computed from ``git show master:`` at 6c81160 and hard-coded,
  so the pin survives the merge instead of becoming tautological).
* **Behaviour**, by running the emitted code on ``tests/cs8900a_sim.py``
  -- a small 6502 interpreter with a behavioural CS8900a -- and looking
  at what the routine transmits when handed an ARP request.  The
  two-VICE bridge tests in ``tests/test_bridge_arp.py`` are the live
  counterpart; this is the one that runs in the default suite.
"""

from __future__ import annotations

import hashlib
import re

import pytest

from c64_test_harness import bridge_ping as bp
from c64_test_harness.bridge_ping import (
    PPTR_HI,
    PPTR_LO,
    RTDATA_LO,
    TXCMD_LO,
    TXLEN_HI,
    TXLEN_LO,
    ArpPacket,
    build_arp_reply_frame,
    build_arp_request_frame,
    build_echo_request_frame,
    parse_arp,
)
from cs8900a_sim import run_routine

LDA_IMM, LDA_ABS, STA_ABS, CMP_IMM, BNE, JMP = 0xA9, 0xAD, 0x8D, 0xC9, 0xD0, 0x4C

MAC_A = bytes.fromhex("02C640000001")
MAC_B = bytes.fromhex("02C640000002")
IP_A = bytes([10, 0, 65, 2])
IP_B = bytes([10, 0, 65, 3])

# The layout every bridge test uses.
LOAD, TX_BUF, ARP_BUF, RX_BUF, RESULT = 0xC000, 0xC300, 0xC380, 0xC400, 0xC0FF
IP = bytes([10, 0, 0, 2])
MAC = bytes.fromhex("021122334455")


def _abs(addr: int) -> bytes:
    return bytes([addr & 0xFF, addr >> 8])


def _lda(addr: int) -> bytes:
    return bytes([LDA_ABS]) + _abs(addr)


def _sta(addr: int) -> bytes:
    return bytes([STA_ABS]) + _abs(addr)


def _cmp_at(rx_off: int, value: int) -> bytes:
    """``LDA rx_buf+off / CMP #value / BNE`` -- the builders' field check."""
    return _lda(RX_BUF + rx_off) + bytes([CMP_IMM, value, BNE])


def _store_imm(value: int, addr: int) -> bytes:
    return bytes([LDA_IMM, value]) + _sta(addr)


# ===========================================================================
# 1. Frames and parser -- hand-computed RFC 826 layout
# ===========================================================================

# Hand-written, field by field.  ip65 arp.s offsets: ap_hw=14 ap_proto=16
# ap_hwlen=18 ap_protolen=19 ap_op=20 ap_shw=22 ap_sp=28 ap_thw=32 ap_tp=38
# ap_packlen=42; then zero padding to the 60-byte ethernet minimum.
ARP_REQUEST_A_FOR_B = bytes.fromhex(
    "ffffffffffff"          # 0   dst: broadcast
    "02c640000001"          # 6   src: MAC_A
    "0806"                  # 12  ethertype ARP
    "0001"                  # 14  htype ethernet
    "0800"                  # 16  ptype IPv4
    "06"                    # 18  hlen
    "04"                    # 19  plen
    "0001"                  # 20  opcode request
    "02c640000001"          # 22  sender MAC = MAC_A
    "0a004102"              # 28  sender IP  = 10.0.65.2
    "000000000000"          # 32  target MAC = unknown
    "0a004103"              # 38  target IP  = 10.0.65.3
    + "00" * 18             # 42  pad to 60
)

ARP_REPLY_B_TO_A = bytes.fromhex(
    "02c640000001"          # 0   dst: MAC_A (unicast back to the asker)
    "02c640000002"          # 6   src: MAC_B
    "0806"
    "0001" "0800" "06" "04"
    "0002"                  # 20  opcode reply
    "02c640000002"          # 22  sender MAC = MAC_B
    "0a004103"              # 28  sender IP  = 10.0.65.3
    "02c640000001"          # 32  target MAC = MAC_A
    "0a004102"              # 38  target IP  = 10.0.65.2
    + "00" * 18
)


def test_arp_request_frame_matches_the_hand_written_layout() -> None:
    assert len(ARP_REQUEST_A_FOR_B) == 60
    got = build_arp_request_frame(MAC_A, IP_A, IP_B)
    assert got == ARP_REQUEST_A_FOR_B, f"\n got {got.hex()}\n exp {ARP_REQUEST_A_FOR_B.hex()}"


def test_arp_reply_frame_matches_the_hand_written_layout() -> None:
    assert len(ARP_REPLY_B_TO_A) == 60
    got = build_arp_reply_frame(MAC_B, IP_B, MAC_A, IP_A)
    assert got == ARP_REPLY_B_TO_A, f"\n got {got.hex()}\n exp {ARP_REPLY_B_TO_A.hex()}"


def test_parse_arp_reads_every_field_of_the_hand_written_frames() -> None:
    req = parse_arp(ARP_REQUEST_A_FOR_B)
    assert req == ArpPacket(
        dst_mac=b"\xff" * 6, src_mac=MAC_A, opcode=1,
        sender_mac=MAC_A, sender_ip=IP_A,
        target_mac=b"\x00" * 6, target_ip=IP_B,
    )
    assert req.is_request and not req.is_reply
    rep = parse_arp(ARP_REPLY_B_TO_A)
    assert rep == ArpPacket(
        dst_mac=MAC_A, src_mac=MAC_B, opcode=2,
        sender_mac=MAC_B, sender_ip=IP_B,
        target_mac=MAC_A, target_ip=IP_A,
    )
    assert rep.is_reply and not rep.is_request


def test_parse_arp_accepts_an_unpadded_42_byte_packet() -> None:
    """ip65's ``ap_packlen`` is 42: the wire pads, a parser must not require it."""
    assert parse_arp(ARP_REQUEST_A_FOR_B[:42]) == parse_arp(ARP_REQUEST_A_FOR_B)


@pytest.mark.parametrize("frame, why", [
    (build_echo_request_frame(MAC_A, MAC_B, IP_A, IP_B).frame, "IPv4, not ARP"),
    (ARP_REQUEST_A_FOR_B[:41], "one byte short of the 42-byte packet"),
    (ARP_REQUEST_A_FOR_B[:14] + b"\x00\x02" + ARP_REQUEST_A_FOR_B[16:], "htype != ethernet"),
    (ARP_REQUEST_A_FOR_B[:16] + b"\x86\xdd" + ARP_REQUEST_A_FOR_B[18:], "ptype != IPv4"),
    (ARP_REQUEST_A_FOR_B[:18] + b"\x08\x04" + ARP_REQUEST_A_FOR_B[20:], "hlen != 6"),
    (ARP_REQUEST_A_FOR_B[:19] + b"\x10" + ARP_REQUEST_A_FOR_B[20:], "plen != 4"),
])
def test_parse_arp_returns_none_for_anything_that_is_not_ethernet_ipv4_arp(
    frame: bytes, why: str,
) -> None:
    assert parse_arp(frame) is None, why


@pytest.mark.parametrize("bad_mac", [b"", MAC_A[:5], MAC_A + b"\x00"])
def test_arp_builders_reject_a_mac_that_is_not_six_bytes(bad_mac: bytes) -> None:
    with pytest.raises(ValueError):
        build_arp_request_frame(bad_mac, IP_A, IP_B)
    with pytest.raises(ValueError):
        build_arp_reply_frame(bad_mac, IP_B, MAC_A, IP_A)
    with pytest.raises(ValueError):
        build_arp_reply_frame(MAC_B, IP_B, bad_mac, IP_A)


@pytest.mark.parametrize("bad_ip", [b"", IP_A[:3], IP_A + b"\x00"])
def test_arp_builders_reject_an_ip_that_is_not_four_bytes(bad_ip: bytes) -> None:
    with pytest.raises(ValueError):
        build_arp_request_frame(MAC_A, bad_ip, IP_B)
    with pytest.raises(ValueError):
        build_arp_request_frame(MAC_A, IP_A, bad_ip)
    with pytest.raises(ValueError):
        build_arp_reply_frame(MAC_B, bad_ip, MAC_A, IP_A)


# ===========================================================================
# 2. Default output is byte-identical to master (6c81160)
# ===========================================================================

# SHA-256 of each builder's output for the fixed arguments below, computed
# from ``git show master:src/c64_test_harness/bridge_ping.py`` at 6c81160
# loaded as a scratch module.  Hard-coded rather than recomputed from
# ``git show`` at test time: once this branch merges, master *is* the new
# code and a live recomputation would compare the builder with itself.
_MASTER_DIGESTS: dict[str, tuple[str, int]] = {
    "build_tx_code": ("f05938fe33a1563cc2cc5c5e2ee85353503f95f41f9eb5822b8e32f006071853", 79),
    "build_ping_and_wait_code": ("6710d1e4735071870114017d26e3b9806c2e5b2fa1006a4693b57b46655e38d7", 256),
    "build_ping_and_wait_tod_code": ("9b5e5ed178f5328f0173c57f87dbb9fdeef1630a6468fb4c59311ca74b4b79ef", 380),
    "build_icmp_responder_code": ("7ec06447be98d246675e46591156869593cb6cb0774a995481e8028c2c07ba7d", 401),
    "build_icmp_responder_tod_code": ("15392373d6fae3b6158e9dcd6a7d2e419bd8898371062c58ce89bcba50f997a8", 525),
    "build_read_and_respond_echo_request_code": ("47afe09e52942f69f14fecbafa98c39fec354030a08026e4a00a647427023c65", 349),
}

_LEGACY_CALLS = {
    "build_tx_code": lambda: bp.build_tx_code(LOAD, TX_BUF, 60, RESULT),
    "build_ping_and_wait_code": lambda: bp.build_ping_and_wait_code(
        LOAD, TX_BUF, 60, RX_BUF, RESULT, 0x1234, 1),
    "build_ping_and_wait_tod_code": lambda: bp.build_ping_and_wait_tod_code(
        LOAD, TX_BUF, 60, RX_BUF, RESULT, 0x1234, 1),
    "build_icmp_responder_code": lambda: bp.build_icmp_responder_code(LOAD, RX_BUF, IP, RESULT),
    "build_icmp_responder_tod_code": lambda: bp.build_icmp_responder_tod_code(
        LOAD, RX_BUF, IP, RESULT),
    "build_read_and_respond_echo_request_code":
        lambda: bp.build_read_and_respond_echo_request_code(LOAD, RX_BUF, IP, RESULT),
}


@pytest.mark.parametrize("name", sorted(_MASTER_DIGESTS))
def test_builder_without_the_arp_parameter_emits_masters_exact_bytes(name: str) -> None:
    """Opt-in means opt-in: a caller that does not ask for ARP gets the old bytes.

    Every pre-#218 test and every downstream layout that sized its code
    window from these routines keeps working unchanged.
    """
    code = _LEGACY_CALLS[name]()
    digest, length = _MASTER_DIGESTS[name]
    assert len(code) == length, f"{name}: length {len(code)} != master's {length}"
    assert hashlib.sha256(code).hexdigest() == digest, (
        f"{name}: default output no longer byte-identical to master 6c81160"
    )


# ===========================================================================
# 3. Ping side: ARP request transmitted before the echo request
# ===========================================================================

PING_ARP_BUILDERS = {
    "build_ping_and_wait_code": lambda: bp.build_ping_and_wait_code(
        LOAD, TX_BUF, 60, RX_BUF, RESULT, 0x1234, 1,
        arp_frame_buf=ARP_BUF, arp_frame_len=60),
    "build_ping_and_wait_tod_code": lambda: bp.build_ping_and_wait_tod_code(
        LOAD, TX_BUF, 60, RX_BUF, RESULT, 0x1234, 1,
        arp_frame_buf=ARP_BUF, arp_frame_len=60),
}


def _tx_sites(code: bytes) -> list[tuple[int, int, int]]:
    """``(offset, tx_len, frame_buf)`` for every TX handshake in *code*.

    Each site is ``STA TXCMD_LO`` followed (in order) by the TxLength
    stores and then the ``LDA #lo / STA $FB / LDA #hi / STA $FC`` pointer
    setup for the RTDATA copy loop.
    """
    sites = []
    for m in re.finditer(re.escape(_sta(TXCMD_LO)), code):
        off = m.start()
        len_lo = code.find(_sta(TXLEN_LO), off)
        len_hi = code.find(_sta(TXLEN_HI), len_lo)
        assert len_lo > 0 and len_hi > 0, f"TX site at {off} has no TxLength stores"
        tx_len = code[len_lo - 1] | (code[len_hi - 1] << 8)
        ptr = re.compile(rb"\xA9(.)\x85\xFB\xA9(.)\x85\xFC", re.DOTALL).search(code, len_hi)
        assert ptr, f"TX site at {off} has no frame pointer setup"
        sites.append((off, tx_len, ptr.group(1)[0] | (ptr.group(2)[0] << 8)))
    return sites


def _first_rxevent_pptr(code: bytes) -> int:
    seq = _store_imm(0x24, PPTR_LO) + _store_imm(0x01, PPTR_HI)
    off = code.find(seq)
    assert off >= 0, "no PPPtr=RxEvent write"
    return off


@pytest.mark.parametrize("name", sorted(PING_ARP_BUILDERS))
def test_ping_builder_with_arp_transmits_the_arp_frame_then_the_echo_request(name: str) -> None:
    """Two TX handshakes, ARP buffer first, echo second, both before the RX poll."""
    code = PING_ARP_BUILDERS[name]()
    sites = _tx_sites(code)
    assert [(ln, buf) for _, ln, buf in sites] == [(60, ARP_BUF), (60, TX_BUF)], (
        f"{name}: TX sites (offset, len, buf) = {[(o, l, hex(b)) for o, l, b in sites]}; "
        "expected the ARP frame first, then the echo request"
    )
    assert sites[-1][0] < _first_rxevent_pptr(code), (
        f"{name}: the echo TX must precede the first RxEvent poll"
    )


@pytest.mark.parametrize("name", sorted(PING_ARP_BUILDERS))
def test_ping_builder_with_arp_is_the_legacy_routine_plus_one_tx_block(name: str) -> None:
    """The ARP TX is a prefix insertion: everything after it is the old code.

    Guards against the ARP support being wired in by rewriting the echo
    path (which the byte-identity pin would not see, since that pin only
    covers the no-ARP call).
    """
    with_arp = PING_ARP_BUILDERS[name]()
    legacy = _LEGACY_CALLS[name]()
    arp_site, echo_site = (s[0] for s in _tx_sites(with_arp))
    legacy_echo_site = _tx_sites(legacy)[0][0]
    # Bytes before the first TX (SEI, clockport, TOD start...) are shared.
    assert with_arp[:arp_site - 2] == legacy[:legacy_echo_site - 2]
    # And the echo TX onwards is the legacy tail, modulo absolute
    # JMP operands that moved with the insertion.
    shift = echo_site - legacy_echo_site
    tail_new = with_arp[echo_site - 2:]
    tail_old = legacy[legacy_echo_site - 2:]
    assert len(tail_new) == len(tail_old)
    diffs = [i for i, (x, y) in enumerate(zip(tail_new, tail_old)) if x != y]
    # Every difference must be an absolute operand that moved by the
    # shift: a JMP target, or (TOD builder) the LDA abs,X table addresses
    # patched after build.
    relocatable = {JMP, 0xBD}
    for i in diffs:
        j = next((k for k in (i - 1, i - 2) if k >= 0 and tail_old[k] in relocatable), None)
        assert j is not None, f"{name}: byte {i} of the echo path differs and is not an operand"
        old_target = tail_old[j + 1] | (tail_old[j + 2] << 8)
        new_target = tail_new[j + 1] | (tail_new[j + 2] << 8)
        assert new_target - old_target == shift, (
            f"{name}: byte {i} of the echo path differs and is not a relocated operand"
        )


def test_ping_builders_reject_arp_len_without_a_buffer() -> None:
    with pytest.raises(ValueError, match="arp_frame_buf"):
        bp.build_ping_and_wait_code(LOAD, TX_BUF, 60, RX_BUF, RESULT, 0x1234, 1, arp_frame_len=60)
    with pytest.raises(ValueError, match="arp_frame_buf"):
        bp.build_ping_and_wait_tod_code(
            LOAD, TX_BUF, 60, RX_BUF, RESULT, 0x1234, 1, arp_frame_len=60)


def test_ping_builders_default_arp_len_to_the_padded_frame_length() -> None:
    explicit = bp.build_ping_and_wait_code(
        LOAD, TX_BUF, 60, RX_BUF, RESULT, 0x1234, 1, arp_frame_buf=ARP_BUF, arp_frame_len=60)
    implicit = bp.build_ping_and_wait_code(
        LOAD, TX_BUF, 60, RX_BUF, RESULT, 0x1234, 1, arp_frame_buf=ARP_BUF)
    assert explicit == implicit
    assert bp.ARP_FRAME_LEN == 60


def _echo_reply_for(echo_frame: bytes) -> bytes:
    """What a correct peer sends back for *echo_frame*: swap addresses, type 0."""
    f = bytearray(echo_frame)
    f[0:6], f[6:12] = echo_frame[6:12], echo_frame[0:6]
    f[26:30], f[30:34] = echo_frame[30:34], echo_frame[26:30]
    f[34] = 0
    cksum = (int.from_bytes(f[36:38], "big") + 0x0800)
    cksum = (cksum & 0xFFFF) + (cksum >> 16)
    f[36:38] = cksum.to_bytes(2, "big")
    return bytes(f)


@pytest.mark.parametrize("name", sorted(PING_ARP_BUILDERS))
def test_ping_with_arp_puts_arp_then_echo_on_the_wire_and_still_matches_the_reply(
    name: str,
) -> None:
    """Run the routine: the chip sees ARP request, echo request; the ARP reply
    that comes back is dropped and the echo reply is matched."""
    echo = build_echo_request_frame(MAC_A, MAC_B, IP_A, IP_B, identifier=0x1234, sequence=1)
    arp = build_arp_request_frame(MAC_A, IP_A, IP_B)
    builder = getattr(bp, name)
    code = builder(LOAD, TX_BUF, len(echo.frame), RX_BUF, RESULT, 0x1234, 1,
                   arp_frame_buf=ARP_BUF, arp_frame_len=len(arp))
    cpu, chip = run_routine(
        code, LOAD,
        rx_frames=[ARP_REPLY_B_TO_A, _echo_reply_for(echo.frame)],
        preload={TX_BUF: echo.frame, ARP_BUF: arp},
    )
    assert chip.tx_frames == [arp, echo.frame], (
        f"{name}: wire order was {[f[12:14].hex() for f in chip.tx_frames]}"
    )
    assert cpu.mem[RESULT] == 0x01, f"{name}: echo reply not matched (result={cpu.mem[RESULT]:#x})"
    assert cpu.mem[RX_BUF + 34] == 0x00 and cpu.mem[RX_BUF + 26:RX_BUF + 30] == IP_B


# ===========================================================================
# 4. Responder side: answers ARP requests for its own IP
# ===========================================================================

RESPONDER_ARP_BUILDERS = {
    "build_icmp_responder_code": lambda: bp.build_icmp_responder_code(
        LOAD, RX_BUF, IP, RESULT, my_mac=MAC),
    "build_icmp_responder_tod_code": lambda: bp.build_icmp_responder_tod_code(
        LOAD, RX_BUF, IP, RESULT, my_mac=MAC),
    "build_read_and_respond_echo_request_code":
        lambda: bp.build_read_and_respond_echo_request_code(
            LOAD, RX_BUF, IP, RESULT, my_mac=MAC),
}


def _resolve_bne(code: bytes, bne_off: int) -> int:
    """Offset the BNE at *bne_off* lands on, following one JMP trampoline."""
    disp = code[bne_off + 1]
    if disp & 0x80:
        disp -= 0x100
    target = bne_off + 2 + disp
    if code[target] == JMP:
        target = (code[target + 1] | (code[target + 2] << 8)) - LOAD
    return target


@pytest.mark.parametrize("name", sorted(RESPONDER_ARP_BUILDERS))
def test_responder_checks_ethertype_opcode_and_target_ip(name: str) -> None:
    """The three compares that make it an ARP responder, at ip65's offsets."""
    code = RESPONDER_ARP_BUILDERS[name]()
    arp_lo = code.find(_cmp_at(13, 0x06))
    assert arp_lo >= 0, f"{name}: no ethertype 0x0806 compare at rx+13"
    # The high-byte compare must be *this* block's, immediately before the
    # 0x06 compare -- the ICMP path has its own rx+12 == 0x08 check further
    # down, so "somewhere in the code" would be satisfied without it
    # (adversarial review of #218: that mutation survived).
    # A check is 7 bytes: LDA abs, CMP #, BNE rel -- _cmp_at is the first six.
    assert code[arp_lo - 7:arp_lo - 1] == _cmp_at(12, 0x08), (
        f"{name}: the ARP block does not check the ethertype high byte before "
        "the 0x06 compare; a frame of ethertype 0xXX06 (e.g. 0x8906) is taken for ARP"
    )
    assert _cmp_at(20, 0x00) in code and _cmp_at(21, 0x01) in code, (
        f"{name}: no opcode==1 (request) compare at rx+20/21"
    )
    for i in range(4):
        assert _cmp_at(38 + i, IP[i]) in code, (
            f"{name}: no target-IP compare for byte {i} at rx+{38 + i}"
        )


@pytest.mark.parametrize("name", sorted(RESPONDER_ARP_BUILDERS))
def test_responder_builds_the_reply_from_its_own_mac_and_ip(name: str) -> None:
    """Sender MAC/IP become ours, opcode becomes 2 -- ip65 arp_process's @request."""
    code = RESPONDER_ARP_BUILDERS[name]()
    for i in range(6):
        assert _store_imm(MAC[i], RX_BUF + 22 + i) in code, (
            f"{name}: sender MAC byte {i} not overwritten with my_mac"
        )
    for i in range(4):
        assert _store_imm(IP[i], RX_BUF + 28 + i) in code, (
            f"{name}: sender IP byte {i} not overwritten with my_ip"
        )
    assert _store_imm(0x02, RX_BUF + 21) in code, f"{name}: opcode not set to reply"


@pytest.mark.parametrize("name", sorted(RESPONDER_ARP_BUILDERS))
def test_responder_with_arp_has_two_tx_sites_and_without_has_one(name: str) -> None:
    with_arp = RESPONDER_ARP_BUILDERS[name]()
    legacy = _LEGACY_CALLS[name]()
    assert len(_tx_sites(with_arp)) == 2, f"{name}: ARP reply needs its own TX"
    assert len(_tx_sites(legacy)) == 1
    assert all(ln == bp._FIXED_RX_BYTES for _, ln, _ in _tx_sites(with_arp))
    assert all(buf == RX_BUF for _, _, buf in _tx_sites(with_arp)), "replies go out of rx_buf"


@pytest.mark.parametrize("name", sorted(RESPONDER_ARP_BUILDERS))
def test_responder_falls_through_to_the_icmp_path_when_not_arp(name: str) -> None:
    """The BNE after ``CMP #$06`` lands on the IPv4 check ``LDA rx+12 / CMP #$08``."""
    code = RESPONDER_ARP_BUILDERS[name]()
    off = code.find(_cmp_at(13, 0x06))
    assert off >= 0
    target = _resolve_bne(code, off + 5)
    ipv4_check = _lda(RX_BUF + 12) + bytes([CMP_IMM, 0x08, BNE])
    assert code[target:target + len(ipv4_check)] == ipv4_check, (
        f"{name}: a non-ARP frame lands at offset {target} "
        f"({code[target:target + 6].hex()}), not on the IPv4 ethertype check"
    )


@pytest.mark.parametrize("name", sorted(RESPONDER_ARP_BUILDERS))
def test_responder_rejects_a_mac_that_is_not_six_bytes(name: str) -> None:
    builder = getattr(bp, name)
    with pytest.raises(ValueError, match="6 bytes"):
        builder(LOAD, RX_BUF, IP, RESULT, my_mac=MAC[:5])


def _run_responder(name: str, frames: list[bytes]):
    builder = getattr(bp, name)
    code = builder(LOAD, RX_BUF, IP_B, RESULT, my_mac=MAC_B)
    return run_routine(code, LOAD, rx_frames=frames)


@pytest.mark.parametrize("name", sorted(RESPONDER_ARP_BUILDERS))
def test_responder_answers_an_arp_request_for_its_ip(name: str) -> None:
    """Behavioural: one ARP request in, one correct ARP reply out."""
    echo = build_echo_request_frame(MAC_A, MAC_B, IP_A, IP_B).frame
    if name == "build_read_and_respond_echo_request_code":
        # The consume routine handles exactly one frame per call.
        cpu, chip = _run_responder(name, [ARP_REQUEST_A_FOR_B])
        assert cpu.mem[RESULT] == bp.RESULT_ARP_REPLY_SENT == 0x03
    else:
        cpu, chip = _run_responder(name, [ARP_REQUEST_A_FOR_B, echo])
        assert cpu.mem[RESULT] == 0x01, "must go on to answer the echo request"
        assert len(chip.tx_frames) == 2
        assert chip.tx_frames[1][12:14] == b"\x08\x00" and chip.tx_frames[1][34] == 0x00
    reply = parse_arp(chip.tx_frames[0])
    assert reply is not None, f"{name}: first TX is not ARP: {chip.tx_frames[0].hex()}"
    assert reply.is_reply
    assert reply.sender_mac == MAC_B and reply.sender_ip == IP_B, "sender must be me"
    assert reply.target_mac == MAC_A and reply.target_ip == IP_A, "target must be the asker"
    assert reply.dst_mac == MAC_A and reply.src_mac == MAC_B
    assert chip.tx_frames[0] == ARP_REPLY_B_TO_A, (
        f"\n got {chip.tx_frames[0].hex()}\n exp {ARP_REPLY_B_TO_A.hex()}"
    )


@pytest.mark.parametrize("name", sorted(RESPONDER_ARP_BUILDERS))
def test_responder_ignores_arp_for_another_ip_and_arp_replies(name: str) -> None:
    other = build_arp_request_frame(MAC_A, IP_A, bytes([10, 0, 65, 99]))
    # Ethertype 0x8906 with an otherwise perfect ARP body for us: only the
    # high byte says it is not ARP.
    lookalike = ARP_REQUEST_A_FOR_B[:12] + b"\x89\x06" + ARP_REQUEST_A_FOR_B[14:]
    echo = build_echo_request_frame(MAC_A, MAC_B, IP_A, IP_B).frame
    if name == "build_read_and_respond_echo_request_code":
        for frame in (other, ARP_REPLY_B_TO_A, lookalike):
            cpu, chip = _run_responder(name, [frame])
            assert cpu.mem[RESULT] == 0x02 and chip.tx_frames == [], (
                f"{name}: answered a frame it must ignore: {frame[:22].hex()}"
            )
    else:
        cpu, chip = _run_responder(name, [other, ARP_REPLY_B_TO_A, lookalike, echo])
        assert cpu.mem[RESULT] == 0x01
        assert len(chip.tx_frames) == 1 and chip.tx_frames[0][12:14] == b"\x08\x00", (
            f"{name}: transmitted {[f[12:14].hex() for f in chip.tx_frames]}"
        )


def test_legacy_responder_without_mac_drains_arp_and_still_answers_echo() -> None:
    """The opt-out path is unchanged: ARP is a non-matching frame, dropped."""
    echo = build_echo_request_frame(MAC_A, MAC_B, IP_A, IP_B).frame
    code = bp.build_icmp_responder_code(LOAD, RX_BUF, IP_B, RESULT)
    cpu, chip = run_routine(code, LOAD, rx_frames=[ARP_REQUEST_A_FOR_B, echo])
    assert cpu.mem[RESULT] == 0x01
    assert len(chip.tx_frames) == 1 and chip.tx_frames[0][12:14] == b"\x08\x00"


# ===========================================================================
# 5. Orchestrators
# ===========================================================================

class _FakeTransport:
    """64 KiB of RAM; ``jsr`` is scripted by the test through monkeypatch."""

    def __init__(self) -> None:
        self.mem = bytearray(0x10000)
        self.writes: list[tuple[int, bytes]] = []

    def write_memory(self, addr: int, data, *, override=None) -> None:
        data = bytes(data)
        self.mem[addr:addr + len(data)] = data
        self.writes.append((addr, data))

    def read_memory(self, addr: int, length: int) -> bytes:
        return bytes(self.mem[addr:addr + length])


def _script(monkeypatch, transport: _FakeTransport, result_addr: int, results: list[int]):
    """Each ``jsr`` / ``poll_until_ready`` pops the next result byte into RAM."""
    calls: list[int] = []

    def fake_jsr(t, addr, timeout=5.0, **kw):
        calls.append(addr)
        t.mem[result_addr] = results.pop(0)
        return {}

    def fake_poll(t, code_addr, result_addr, *, timeout_s, **kw):
        calls.append(code_addr)
        value = results.pop(0)
        t.mem[result_addr] = value
        return value

    monkeypatch.setattr("c64_test_harness.execute.jsr", fake_jsr)
    monkeypatch.setattr("c64_test_harness.poll_until.poll_until_ready", fake_poll)
    return calls


def test_run_ping_and_wait_transmits_an_arp_request_before_the_echo(monkeypatch) -> None:
    """The orchestrator derives the ARP request from the echo frame it is given."""
    t = _FakeTransport()
    echo = build_echo_request_frame(MAC_A, MAC_B, IP_A, IP_B, identifier=0xBEEF, sequence=7)
    # TX arp -> 0x01, TX echo -> 0x01, peek -> 0x01, match -> 0x01
    calls = _script(monkeypatch, t, RESULT, [0x01, 0x01, 0x01, 0x01])
    r = bp.run_ping_and_wait(
        t, tx_frame=echo.frame, rx_buf=RX_BUF, result_addr=RESULT,
        identifier=0xBEEF, sequence=7, tx_frame_buf=TX_BUF, timeout_s=1.0,
    )
    assert r == 0x01
    frames = [d for a, d in t.writes if a == TX_BUF]
    assert len(frames) == 2, f"expected ARP then echo written to tx_frame_buf, got {len(frames)}"
    arp = parse_arp(frames[0])
    assert arp is not None and arp.is_request
    assert arp.sender_mac == MAC_A and arp.sender_ip == IP_A and arp.target_ip == IP_B
    assert frames[0] == build_arp_request_frame(MAC_A, IP_A, IP_B)
    assert frames[1] == echo.frame
    assert calls[:2] == [bp._DEFAULT_CONSUME_ADDR] * 2, "two TX jsr calls before any peek"


def test_run_ping_and_wait_arp_false_keeps_the_old_single_transmit(monkeypatch) -> None:
    t = _FakeTransport()
    echo = build_echo_request_frame(MAC_A, MAC_B, IP_A, IP_B)
    calls = _script(monkeypatch, t, RESULT, [0x01, 0x01, 0x01])
    r = bp.run_ping_and_wait(
        t, tx_frame=echo.frame, rx_buf=RX_BUF, result_addr=RESULT,
        identifier=echo.identifier, sequence=echo.sequence, tx_frame_buf=TX_BUF,
        timeout_s=1.0, arp=False,
    )
    assert r == 0x01
    assert [d for a, d in t.writes if a == TX_BUF] == [echo.frame]
    assert calls[0] == bp._DEFAULT_CONSUME_ADDR and calls[1] == bp._DEFAULT_PEEK_ADDR


def test_run_ping_and_wait_arp_needs_an_ipv4_frame() -> None:
    t = _FakeTransport()
    raw = b"\xff" * 6 + MAC_A + b"\x88\xb5" + b"\x00" * 46
    with pytest.raises(ValueError, match="IPv4"):
        bp.run_ping_and_wait(
            t, tx_frame=raw, rx_buf=RX_BUF, result_addr=RESULT,
            identifier=1, sequence=1, tx_frame_buf=TX_BUF,
        )


def test_run_icmp_responder_with_mac_loads_the_arp_capable_body_and_keeps_polling(
    monkeypatch,
) -> None:
    t = _FakeTransport()
    # peek -> ready, body -> ARP replied (0x03), peek -> ready, body -> echo replied
    calls = _script(monkeypatch, t, RESULT, [0x01, 0x03, 0x01, 0x01])
    r = bp.run_icmp_responder(
        t, rx_buf=RX_BUF, my_ip=IP_B, result_addr=RESULT, timeout_s=1.0, my_mac=MAC_B,
    )
    assert r == 0x01
    assert calls == [bp._DEFAULT_PEEK_ADDR, bp._DEFAULT_CONSUME_ADDR] * 2
    loaded = dict((a, d) for a, d in t.writes if a in (bp._DEFAULT_PEEK_ADDR, bp._DEFAULT_CONSUME_ADDR))
    assert loaded[bp._DEFAULT_CONSUME_ADDR] == bp.build_read_and_respond_echo_request_code(
        load_addr=bp._DEFAULT_CONSUME_ADDR, rx_buf=RX_BUF, my_ip=IP_B,
        result_addr=RESULT, my_mac=MAC_B,
    )


def test_run_icmp_responder_without_mac_loads_the_legacy_body(monkeypatch) -> None:
    t = _FakeTransport()
    _script(monkeypatch, t, RESULT, [0x01, 0x01])
    assert bp.run_icmp_responder(
        t, rx_buf=RX_BUF, my_ip=IP_B, result_addr=RESULT, timeout_s=1.0) == 0x01
    loaded = dict((a, d) for a, d in t.writes if a == bp._DEFAULT_CONSUME_ADDR)
    assert loaded[bp._DEFAULT_CONSUME_ADDR] == bp.build_read_and_respond_echo_request_code(
        load_addr=bp._DEFAULT_CONSUME_ADDR, rx_buf=RX_BUF, my_ip=IP_B, result_addr=RESULT,
    )


# ===========================================================================
# 6. Package-root exports
# ===========================================================================

def test_arp_symbols_are_exported_at_the_package_root() -> None:
    import c64_test_harness as pkg

    for name in ("ArpPacket", "build_arp_request_frame", "build_arp_reply_frame", "parse_arp"):
        assert getattr(pkg, name) is getattr(bp, name)
        assert name in pkg.__all__
