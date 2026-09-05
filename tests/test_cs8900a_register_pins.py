"""Register-level pins on the CS8900a code builders (PR #213, issues #207/#209/#210).

PR #213's second commit (0fe0664) aligned the harness's last two
divergences from ip65's ``drivers/cs8900a.s`` and shipped without a test
that could fail if either drifted back:

* **TxCMD (PP 0x0108) is written as 0x00C9, not a bare 0x00C0.**  On a
  real CS8900a the low 6 bits of every control register are its own
  register number -- the same fact that broke RxCTL in #207.  VICE's
  cs8900.c does not care, so the two-VICE bridge suite passes with either
  value; only silicon distinguishes them.

* **The RxEvent poll masks 0x0D (RxOK | IndividualAdr | Broadcast), not
  0x01 (RxOK alone).**  A frame the chip signalled without raising RxOK
  was invisible to the old mask and the poll timed out with the reply
  sitting in the FIFO.  The BusST ``Rdy4TxNOW`` polls, which look
  byte-for-byte identical apart from the PPPtr they follow, must keep
  masking 0x01.

* **The Individual Address is programmed from the 6510** by
  :func:`cs8900a_set_mac_inline_code` (#209), because host-side
  ``write_memory`` never reaches a hardware cartridge.  Nothing checked
  the PP offsets, the wire order, or that the callable form is the inline
  form with the clockport enable in front and an RTS behind.

Every test here is a pure byte-walk over the emitted 6502 code.  None of
it spawns VICE, and none of it can be replaced by the bridge suite, for
the reason above: VICE tolerates the exact regressions these tests reject.
The walkers classify each ``LDA PPData_hi / AND #imm`` poll by the most
recent ``PPPtr`` write before it, so a new poll site is checked the moment
it is added rather than needing its own test.
"""

from __future__ import annotations

import re
from typing import Callable

import pytest

from c64_test_harness import bridge_ping as bp
from c64_test_harness.bridge_ping import (
    CS8900A_LINECTL_ENABLE,
    CS8900A_RXCTL_VALUE,
    CS8900A_RXEVENT_MASK,
    CS8900A_TXCMD_VALUE,
    PPDATA_HI,
    PPDATA_LO,
    PPTR_HI,
    PPTR_LO,
    RTDATA_LO,
    TXCMD_HI,
    TXCMD_LO,
    TXLEN_HI,
    TXLEN_LO,
    Asm,
    _clockport_enable_bytes,
    _emit_poll_rx,
    _emit_read_frame,
    _emit_tod_poll_rxevent,
    cs8900a_enable_inline_code,
    cs8900a_linectl_or_inline_code,
    cs8900a_read_linectl_code,
    cs8900a_rxctl_inline_code,
    cs8900a_set_mac_code,
    cs8900a_set_mac_inline_code,
    cs8900a_write_linectl_code,
)
from c64_test_harness.ethernet import set_cs8900a_mac
from conftest import MockTransport

LDA_IMM, LDA_ABS, STA_ABS, AND_IMM, ORA_IMM, RTS = 0xA9, 0xAD, 0x8D, 0x29, 0x09, 0x60

# PacketPage offsets, per the CS8900a datasheet and ip65's cs8900a.s.
PP_RXCFG = 0x0102
PP_RXCTL = 0x0104
PP_LINECTL = 0x0112
PP_RXEVENT = 0x0124
PP_BUSST = 0x0138
PP_IA = 0x0158          # Individual Address, three words 0x0158/0x015A/0x015C

# Hardware facts pinned as literals on purpose: deriving them from the
# constants under test would let a wrong constant validate its own use.
TXCMD_IP65 = 0x00C9             # TxStart-after-full-frame | register number 9
RXEVENT_IP65_MASK = 0x0D        # RxOK | IndividualAdr | Broadcast, high byte
BUSST_RDY4TXNOW_MASK = 0x01     # bit 8 of BusST, i.e. bit 0 of the high byte


def _abs(addr: int) -> bytes:
    return bytes([addr & 0xFF, addr >> 8])


def _sta(addr: int) -> bytes:
    return bytes([STA_ABS]) + _abs(addr)


def _lda(addr: int) -> bytes:
    return bytes([LDA_ABS]) + _abs(addr)


def _lda_sta(value: int, addr: int) -> bytes:
    return bytes([LDA_IMM, value]) + _sta(addr)


def pptr_set(pp: int) -> bytes:
    """The two-store sequence every builder uses to aim PPPtr at *pp*."""
    return _lda_sta(pp & 0xFF, PPTR_LO) + _lda_sta(pp >> 8, PPTR_HI)


# Any PPPtr write:   LDA #lo / STA PPTR_LO / LDA #hi / STA PPTR_HI
_PPTR_SET = re.compile(
    re.escape(bytes([LDA_IMM])) + b"(.)" + re.escape(_sta(PPTR_LO))
    + re.escape(bytes([LDA_IMM])) + b"(.)" + re.escape(_sta(PPTR_HI)),
    re.DOTALL,
)
# Any high-byte poll: LDA PPDATA_HI / AND #mask
_POLL_HI = re.compile(
    re.escape(_lda(PPDATA_HI) + bytes([AND_IMM])) + b"(.)", re.DOTALL
)

IP = bytes([10, 0, 0, 2])
LOAD, TX_BUF, ARP_BUF, RX_BUF, RESULT = 0xC000, 0xC300, 0xC380, 0xC400, 0xC0FF
MY_MAC = bytes.fromhex("02C640000002")

# Every builder that emits a TxCMD write.  The ``[arp]`` entries are the
# same builders with issue #218's ARP support switched on (an ARP request
# transmitted before the echo; an ARP reply transmitted from the responder),
# which adds a second TX site to each -- every walker below covers both.
TX_BUILDERS: dict[str, Callable[[], bytes]] = {
    "build_tx_code": lambda: bp.build_tx_code(LOAD, TX_BUF, 60, RESULT),
    "build_ping_and_wait_code": lambda: bp.build_ping_and_wait_code(
        LOAD, TX_BUF, 60, RX_BUF, RESULT, 0x1234, 1),
    "build_ping_and_wait_code[arp]": lambda: bp.build_ping_and_wait_code(
        LOAD, TX_BUF, 60, RX_BUF, RESULT, 0x1234, 1, arp_frame_buf=ARP_BUF),
    "build_ping_and_wait_code[drain]": lambda: bp.build_ping_and_wait_code(
        LOAD, TX_BUF, 60, RX_BUF, RESULT, 0x1234, 1, arp_frame_buf=ARP_BUF,
        drain_first=True, drain_status_addr=RESULT + 1),
    "build_icmp_responder_code": lambda: bp.build_icmp_responder_code(
        LOAD, RX_BUF, IP, RESULT),
    "build_icmp_responder_code[arp]": lambda: bp.build_icmp_responder_code(
        LOAD, RX_BUF, IP, RESULT, my_mac=MY_MAC),
    "build_read_and_respond_echo_request_code":
        lambda: bp.build_read_and_respond_echo_request_code(LOAD, RX_BUF, IP, RESULT),
    "build_read_and_respond_echo_request_code[arp]":
        lambda: bp.build_read_and_respond_echo_request_code(
            LOAD, RX_BUF, IP, RESULT, my_mac=MY_MAC),
    "build_ping_and_wait_tod_code": lambda: bp.build_ping_and_wait_tod_code(
        LOAD, TX_BUF, 60, RX_BUF, RESULT, 0x1234, 1),
    "build_ping_and_wait_tod_code[drain]": lambda: bp.build_ping_and_wait_tod_code(
        LOAD, TX_BUF, 60, RX_BUF, RESULT, 0x1234, 1, arp_frame_buf=ARP_BUF,
        drain_first=True, drain_status_addr=RESULT + 1),
    "build_ping_and_wait_tod_code[arp]": lambda: bp.build_ping_and_wait_tod_code(
        LOAD, TX_BUF, 60, RX_BUF, RESULT, 0x1234, 1, arp_frame_buf=ARP_BUF),
    "build_icmp_responder_tod_code": lambda: bp.build_icmp_responder_tod_code(
        LOAD, RX_BUF, IP, RESULT),
    "build_icmp_responder_tod_code[arp]": lambda: bp.build_icmp_responder_tod_code(
        LOAD, RX_BUF, IP, RESULT, my_mac=MY_MAC),
}

# Every builder that polls RxEvent.
RX_POLLERS: dict[str, Callable[[], bytes]] = {
    "build_rx_echo_reply_code": lambda: bp.build_rx_echo_reply_code(
        LOAD, RX_BUF, RESULT, 0x1234, 1),
    "build_ping_and_wait_code": TX_BUILDERS["build_ping_and_wait_code"],
    "build_ping_and_wait_code[arp]": TX_BUILDERS["build_ping_and_wait_code[arp]"],
    "build_ping_and_wait_code[drain]": TX_BUILDERS["build_ping_and_wait_code[drain]"],
    "build_icmp_responder_code": TX_BUILDERS["build_icmp_responder_code"],
    "build_icmp_responder_code[arp]": TX_BUILDERS["build_icmp_responder_code[arp]"],
    "build_rx_peek_code": lambda: bp.build_rx_peek_code(LOAD, RESULT),
    "build_rx_echo_reply_tod_code": lambda: bp.build_rx_echo_reply_tod_code(
        LOAD, RX_BUF, RESULT, 0x1234, 1),
    "build_ping_and_wait_tod_code": TX_BUILDERS["build_ping_and_wait_tod_code"],
    "build_ping_and_wait_tod_code[arp]": TX_BUILDERS["build_ping_and_wait_tod_code[arp]"],
    "build_ping_and_wait_tod_code[drain]": TX_BUILDERS["build_ping_and_wait_tod_code[drain]"],
    "build_icmp_responder_tod_code": TX_BUILDERS["build_icmp_responder_tod_code"],
    "build_icmp_responder_tod_code[arp]": TX_BUILDERS["build_icmp_responder_tod_code[arp]"],
}

# Builders that neither transmit nor poll: they assume RxEvent already fired.
NO_POLL_BUILDERS: dict[str, Callable[[], bytes]] = {
    "build_read_and_match_echo_reply_code":
        lambda: bp.build_read_and_match_echo_reply_code(LOAD, RX_BUF, RESULT, 0x1234, 1),
}

ALL_BUILDERS = {**TX_BUILDERS, **RX_POLLERS, **NO_POLL_BUILDERS}


def _polls_by_register(code: bytes) -> list[tuple[int, int, int]]:
    """Return ``(offset, pp_register, mask)`` for every high-byte poll in *code*.

    ``pp_register`` is whatever the nearest preceding PPPtr write aimed
    at; a poll with no PPPtr write before it fails the test that called
    us, because such a poll reads whatever register the previous routine
    left selected.
    """
    ptr_sets = [(m.start(), m.group(2)[0] << 8 | m.group(1)[0])
                for m in _PPTR_SET.finditer(code)]
    out = []
    for m in _POLL_HI.finditer(code):
        before = [pp for off, pp in ptr_sets if off < m.start()]
        assert before, (
            f"poll at offset {m.start()} has no PPPtr write before it"
        )
        out.append((m.start(), before[-1], m.group(1)[0]))
    return out


# ---------------------------------------------------------------------------
# TxCMD
# ---------------------------------------------------------------------------

def test_txcmd_constant_carries_the_register_number() -> None:
    """0x00C9 = TxStart-after-full-frame (0xC0) | TxCMD's own number (0x09).

    The low 6 bits are what the harness omitted for years and what a real
    chip reads back regardless (issue #207 for RxCTL, PR #213 for TxCMD).
    """
    assert CS8900A_TXCMD_VALUE == 0x00C9
    assert CS8900A_TXCMD_VALUE & 0x3F == 0x09, "low 6 bits must be TxCMD's register number"
    assert CS8900A_TXCMD_VALUE & 0xC0 == 0xC0, "bits 6-7 must request TxStart after the full frame"


@pytest.mark.parametrize("name", sorted(TX_BUILDERS))
def test_every_txcmd_write_is_the_ip65_value(name: str) -> None:
    """Each ``STA $DE0C`` is preceded by ``LDA #$C9``, each ``STA $DE0D`` by ``LDA #$00``.

    Walks every TX site rather than one, so a builder that re-inlines the
    old literal 0xC0 (the exact shape of the pre-#213 code) is caught
    wherever it happens.
    """
    code = TX_BUILDERS[name]()
    lo_sites = [m.start() for m in re.finditer(re.escape(_sta(TXCMD_LO)), code)]
    hi_sites = [m.start() for m in re.finditer(re.escape(_sta(TXCMD_HI)), code)]
    assert lo_sites, f"{name} never writes TxCMD low byte"
    assert len(hi_sites) == len(lo_sites), f"{name}: TxCMD low/high writes unpaired"
    for off in lo_sites:
        got = code[off - 2:off]
        assert got == bytes([LDA_IMM, TXCMD_IP65 & 0xFF]), (
            f"{name}: TxCMD low byte at offset {off} is loaded from {got.hex()}, "
            f"expected LDA #${TXCMD_IP65 & 0xFF:02X} (ip65's 0x00C9; "
            "a bare 0xC0 drops the register number)"
        )
    for off in hi_sites:
        got = code[off - 2:off]
        assert got == bytes([LDA_IMM, TXCMD_IP65 >> 8]), (
            f"{name}: TxCMD high byte at offset {off} loaded from {got.hex()}"
        )


@pytest.mark.parametrize("name", sorted(TX_BUILDERS))
def test_tx_sequence_is_txcmd_txlen_busst_poll_then_data(name: str) -> None:
    """TxCMD, TxLength, PPPtr=BusST, poll Rdy4TxNOW, then the RTDATA writes.

    This is the CS8900a's documented TX handshake and ip65's order.  Writing
    data before the chip has said Rdy4TxNOW, or TxLength before TxCMD,
    passes under VICE and loses frames on silicon.
    """
    code = TX_BUILDERS[name]()
    txcmd_lo = code.find(_sta(TXCMD_LO))
    txcmd_hi = code.find(_sta(TXCMD_HI), txcmd_lo)
    txlen_lo = code.find(_sta(TXLEN_LO), txcmd_hi)
    txlen_hi = code.find(_sta(TXLEN_HI), txlen_lo)
    busst = code.find(pptr_set(PP_BUSST), txlen_hi)
    poll = code.find(_lda(PPDATA_HI) + bytes([AND_IMM, BUSST_RDY4TXNOW_MASK]), busst)
    data = code.find(_sta(RTDATA_LO), poll)
    order = [txcmd_lo, txcmd_hi, txlen_lo, txlen_hi, busst, poll, data]
    assert all(o >= 0 for o in order), (
        f"{name}: TX handshake step missing (offsets {order}; -1 = not found "
        "after the previous step)"
    )
    assert order == sorted(order)


# ---------------------------------------------------------------------------
# RxEvent vs BusST polls
# ---------------------------------------------------------------------------

def test_rxevent_mask_is_ip65s_three_bits() -> None:
    """RxOK (0x0100) | IndividualAdr (0x0400) | Broadcast (0x0800), high byte."""
    assert CS8900A_RXEVENT_MASK == 0x0D
    assert CS8900A_RXEVENT_MASK & 0x01, "RxOK must stay in the mask"
    assert CS8900A_RXEVENT_MASK != 0x01, (
        "RxOK alone is the pre-#213 mask that missed frames the chip signalled "
        "via IndividualAdr/Broadcast"
    )


@pytest.mark.parametrize("name", sorted(ALL_BUILDERS))
def test_each_poll_masks_the_register_its_pptr_points_at(name: str) -> None:
    """Every ``LDA PPData_hi / AND #m`` is classified by the PPPtr set before it.

    PPPtr=0x0124 (RxEvent) polls must mask :data:`CS8900A_RXEVENT_MASK`;
    PPPtr=0x0138 (BusST) polls must mask 0x01 (Rdy4TxNOW).  The two loops
    are byte-identical except for that immediate, which is exactly why the
    #213 change had to be applied by hand at three sites and left seven
    others alone -- and why a search-and-replace in either direction is
    the likely regression.
    """
    code = ALL_BUILDERS[name]()
    polls = _polls_by_register(code)
    expected_mask = {PP_RXEVENT: RXEVENT_IP65_MASK, PP_BUSST: BUSST_RDY4TXNOW_MASK}
    for off, pp, mask in polls:
        assert pp in expected_mask, (
            f"{name}: poll at offset {off} follows PPPtr=0x{pp:04X}, which is "
            "neither RxEvent nor BusST"
        )
        assert mask == expected_mask[pp], (
            f"{name}: poll at offset {off} of PP 0x{pp:04X} masks 0x{mask:02X}, "
            f"expected 0x{expected_mask[pp]:02X}"
        )

    registers = {pp for _, pp, _ in polls}
    assert (PP_BUSST in registers) == (name in TX_BUILDERS), (
        f"{name}: BusST poll presence does not match whether it transmits"
    )
    assert (PP_RXEVENT in registers) == (name in RX_POLLERS), (
        f"{name}: RxEvent poll presence does not match whether it polls"
    )


def test_poll_rx_emitter_masks_rxevent_with_the_new_mask() -> None:
    """Site 1 of 3: the counter-based poll used by the non-TOD builders."""
    a = Asm(org=LOAD)
    a.label("hit")
    _emit_poll_rx(a, "timeout", "hit")
    a.label("timeout")
    code = a.build()
    assert code.startswith(pptr_set(PP_RXEVENT))
    assert _lda(PPDATA_HI) + bytes([AND_IMM, RXEVENT_IP65_MASK]) in code
    assert _lda(PPDATA_HI) + bytes([AND_IMM, 0x01]) not in code, (
        "_emit_poll_rx has regressed to the RxOK-only mask"
    )


def test_tod_poll_emitter_masks_rxevent_with_the_new_mask() -> None:
    """Site 3 of 3: the TOD-deadline poll.  (Site 2 is build_rx_peek_code,
    covered by the builder walk above.)"""
    a = Asm(org=LOAD)
    a.label("got")
    _emit_tod_poll_rxevent(a, "got", "timeout", "min_ok", "done", "poll")
    a.label("timeout")     # min_ok and done are defined by the emitter itself
    code = a.build()
    assert code.startswith(_lda(PPDATA_HI) + bytes([AND_IMM, RXEVENT_IP65_MASK])), (
        f"TOD poll must open with LDA PPData_hi / AND #${RXEVENT_IP65_MASK:02X}; "
        f"got {code[:5].hex()}"
    )


# ---------------------------------------------------------------------------
# Individual Address (MAC) from the 6510 -- issue #209
# ---------------------------------------------------------------------------

MAC = bytes.fromhex("021122334455")


def test_set_mac_inline_writes_three_ia_words_in_wire_order() -> None:
    """PP 0x0158 <- mac[0..1], 0x015A <- mac[2..3], 0x015C <- mac[4..5].

    Low half of each PPData word takes the earlier wire byte.  Swapping
    the halves programs a MAC with every byte pair reversed; the chip then
    filters for an address nobody sends to, which looks exactly like a
    dead link once PromiscuousA is off.
    """
    expected = b"".join(
        pptr_set(PP_IA + 2 * i)
        + _lda_sta(MAC[2 * i], PPDATA_LO)
        + _lda_sta(MAC[2 * i + 1], PPDATA_HI)
        for i in range(3)
    )
    got = cs8900a_set_mac_inline_code(MAC)
    assert got == expected, f"IA write sequence differs:\n got {got.hex()}\n exp {expected.hex()}"


def test_set_mac_inline_has_no_clockport_enable_and_no_rts() -> None:
    inline = cs8900a_set_mac_inline_code(MAC)
    assert _clockport_enable_bytes() not in inline, (
        "inline form must not enable the clockport itself; the caller's "
        "cs8900a_enable_inline_code already did"
    )
    assert not inline.endswith(bytes([RTS])), "inline form must not RTS mid-routine"


def test_set_mac_code_is_clockport_then_inline_then_rts() -> None:
    assert cs8900a_set_mac_code(MAC) == (
        _clockport_enable_bytes() + cs8900a_set_mac_inline_code(MAC) + bytes([RTS])
    )


@pytest.mark.parametrize("bad", [b"", MAC[:5], MAC + b"\x66"])
def test_set_mac_rejects_anything_but_six_bytes(bad: bytes) -> None:
    with pytest.raises(ValueError, match="6 bytes"):
        cs8900a_set_mac_inline_code(bad)
    with pytest.raises(ValueError, match="6 bytes"):
        cs8900a_set_mac_code(bad)


def test_6510_and_host_mac_routes_program_identical_register_writes() -> None:
    """The 6502 blob performs the same (address, byte) stores the host route does.

    ``ethernet.set_cs8900a_mac`` is the VICE-only host-side twin (docs:
    "Real silicon diverges", point 3).  Decoding every ``LDA #v / STA a``
    in the inline blob and comparing it with the host route's write log
    proves the two agree on offsets, order and byte placement, so a
    change to either one without the other fails here.
    """
    t = MockTransport()
    set_cs8900a_mac(t, MAC)
    host_writes = [(addr, data[0]) for addr, data in t.written_memory]
    assert host_writes[0] == (0xDE01, 0x01), "host route enables the clockport first"

    blob_writes = [
        (m.group(2)[0] | m.group(3)[0] << 8, m.group(1)[0])
        for m in re.finditer(rb"\xA9(.)\x8D(.)(.)", cs8900a_set_mac_inline_code(MAC), re.DOTALL)
    ]
    assert blob_writes == host_writes[1:], (
        "6510 IA programming differs from the host-side route:\n"
        f" 6510 {[(hex(a), hex(v)) for a, v in blob_writes]}\n"
        f" host {[(hex(a), hex(v)) for a, v in host_writes[1:]]}"
    )


# ---------------------------------------------------------------------------
# Chip enable: RxCTL then LineCTL, RMW on LineCTL's low byte only
# ---------------------------------------------------------------------------

def test_enable_inline_programs_rxctl_before_linectl() -> None:
    """RxCTL is set before SerRxON|SerTxON turn the receiver on.

    Enabling reception first would run the chip for a moment on its reset
    RxCTL (0x0005: no RxOKA), so anything that arrived in that window is
    discarded.  The enable is the composition of the two helpers, in that
    order, with the clockport enable in front.
    """
    code = cs8900a_enable_inline_code()
    assert code.startswith(_clockport_enable_bytes())
    rxctl = code.find(pptr_set(PP_RXCTL))
    linectl = code.find(pptr_set(PP_LINECTL))
    assert rxctl >= 0 and linectl >= 0, "enable must program both RxCTL and LineCTL"
    assert rxctl < linectl, "RxCTL must be programmed before LineCTL enables RX/TX"
    assert code == cs8900a_rxctl_inline_code() + cs8900a_linectl_or_inline_code()


def test_rxctl_inline_writes_the_promiscuous_value_low_then_high() -> None:
    code = cs8900a_rxctl_inline_code()
    assert code == _clockport_enable_bytes() + pptr_set(PP_RXCTL) + _lda_sta(
        CS8900A_RXCTL_VALUE & 0xFF, PPDATA_LO) + _lda_sta(CS8900A_RXCTL_VALUE >> 8, PPDATA_HI)
    assert CS8900A_RXCTL_VALUE & 0x3F == 0x05, "low 6 bits must be RxCTL's register number (#207)"
    assert CS8900A_RXCTL_VALUE & 0x0100, "RxOKA must be set or the receiver accepts nothing (#207)"
    # The ARP responders (#218) depend on two more acceptance bits that
    # PromiscuousA happens to make redundant under the harness value but
    # ip65's value relies on outright; nothing else pins them.
    assert CS8900A_RXCTL_VALUE & 0x0800, (
        "BroadcastA must be set: an ARP request is a broadcast frame, and without "
        "it the responder never sees the request it is meant to answer (#218)"
    )
    assert CS8900A_RXCTL_VALUE & 0x0400, (
        "IndividualA must be set: the ARP reply and the echo reply are unicast to the "
        "programmed IA, and without it the pinger never receives its answer (#218)"
    )
    for bit, name in ((0x0800, "BroadcastA"), (0x0400, "IndividualA")):
        assert bp.CS8900A_RXCTL_VALUE_IP65 & bit, (
            f"{name} must be set in the ip65 value too: it has no PromiscuousA to "
            "fall back on, so this bit alone admits the frame (#218)"
        )


def test_linectl_or_is_a_low_byte_read_modify_write_only() -> None:
    """LDA PPData_lo / ORA #mask / STA PPData_lo -- and the high byte is never stored.

    A plain store would clobber the other LineCTL bits; a high-byte store
    would drop whatever the chip holds there.  ip65 does the same RMW.
    """
    code = cs8900a_linectl_or_inline_code()
    assert code == pptr_set(PP_LINECTL) + _lda(PPDATA_LO) + bytes(
        [ORA_IMM, CS8900A_LINECTL_ENABLE & 0xFF]) + _sta(PPDATA_LO)
    assert _sta(PPDATA_HI) not in code, "LineCTL high byte must be left alone"
    assert CS8900A_LINECTL_ENABLE & 0xC0 == 0xC0, "SerRxON (bit 6) and SerTxON (bit 7)"


def test_read_and_write_linectl_blobs_aim_at_pp_0112_and_rts() -> None:
    dest = 0xC010
    assert cs8900a_read_linectl_code(dest) == (
        _clockport_enable_bytes() + pptr_set(PP_LINECTL)
        + _lda(PPDATA_LO) + _sta(dest) + _lda(PPDATA_HI) + _sta(dest + 1) + bytes([RTS])
    )
    assert cs8900a_write_linectl_code(0xD3, 0x00) == (
        _clockport_enable_bytes() + pptr_set(PP_LINECTL)
        + _lda_sta(0xD3, PPDATA_LO) + _lda_sta(0x00, PPDATA_HI) + bytes([RTS])
    )


# ---------------------------------------------------------------------------
# SkipNow after every frame read
# ---------------------------------------------------------------------------

def test_frame_reader_ends_with_skipnow_on_rxcfg_low_byte() -> None:
    """The reader's last act is RxCFG (PP 0x0102) low byte |= 0x40 (SkipNow).

    Measured on hardware: one frame occupies 4 header bytes + RxLength
    data bytes, and every read past that returns $00 until SkipNow is
    issued -- the FIFO does not roll on to the next frame by itself.  The
    high byte must not be written (the chip drops state if it is).
    """
    a = Asm(org=LOAD)
    _emit_read_frame(a, RX_BUF)
    code = a.build()
    skip = pptr_set(PP_RXCFG) + _lda(PPDATA_LO) + bytes([ORA_IMM, 0x40]) + _sta(PPDATA_LO)
    assert code.endswith(skip), (
        f"frame reader must end with the SkipNow RMW ({skip.hex()}); "
        f"tail is {code[-len(skip):].hex()}"
    )
    assert _sta(PPDATA_HI) not in code[code.rfind(pptr_set(PP_RXCFG)):], (
        "SkipNow must not store RxCFG's high byte"
    )


def test_ip65_rxctl_constant_is_ip65s_literal() -> None:
    """``CS8900A_RXCTL_VALUE_IP65`` is what ip65's ``cs8900a.s`` writes: RxOKA
    | IndividualA | BroadcastA over register number 5, and **no**
    PromiscuousA (bit 7).  It exists so a caller can ask for ip65 parity by
    name; a drift here silently hands them the harness's promiscuous value
    instead (mutation escape found 2026-09-05)."""
    from c64_test_harness.bridge_ping import CS8900A_RXCTL_VALUE_IP65

    assert CS8900A_RXCTL_VALUE_IP65 == 0x0D05
    assert CS8900A_RXCTL_VALUE_IP65 & 0x0080 == 0, "ip65 does not set PromiscuousA"
    assert CS8900A_RXCTL_VALUE ^ CS8900A_RXCTL_VALUE_IP65 == 0x0080, (
        "the harness value must differ from ip65's by PromiscuousA alone"
    )
