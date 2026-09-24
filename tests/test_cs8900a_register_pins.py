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
it is added rather than needing its own test.  The ``Rdy4TxNOW`` polls are
additionally decoded from their ``PPPtr=BusST`` anchor through the branch
that closes the loop (issue #224): a poll that reads the register once and
falls through is the one regression the classification cannot see.
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
BEQ, BNE = 0xF0, 0xD0

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
#: Clear of the [drain] routines' own span (#487's guard refuses RESULT + 1 = $C100,
#: which the routine covers) and of the TX/ARP frames.
DRAIN_STATUS = 0xC3F0

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
        drain_first=True, drain_status_addr=DRAIN_STATUS),
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
        drain_first=True, drain_status_addr=DRAIN_STATUS),
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


def _expected_tx_sites(name: str) -> int:
    """One TX handshake per builder; a variant that transmits an ARP request
    first (``[arp]``, and ``[drain]`` which is ARP + RX drain, #218/#222)
    adds a second."""
    return 2 if name.endswith(("[arp]", "[drain]")) else 1


LDX_IMM, LDY_IMM, DEX, DEY, JMP_ABS, CLI = 0xA2, 0xA0, 0xCA, 0x88, 0x4C, 0x58
#: The Rdy4TxNOW poll bound (issue #236), as a literal: the 65,536 polls
#: across which #234 measured a wedged chip never asserting.  Encoded in
#: the emitted ``LDY #lo / LDX #hi`` as ``$00 / $00``.
RDY4TXNOW_MAX_POLLS = 65536
#: Result byte a builder stores when Rdy4TxNOW never asserted (issue #236).
TX_NOT_READY = 0x04


def _disp(code: bytes, at: int) -> int:
    """Resolve the relative branch whose opcode sits at *at*."""
    d = code[at + 1]
    return at + 2 + (d - 256 if d >= 128 else d)


#: ip65's try count (``drivers/cs8900a.s`` ``send``, ``ldy #$08``), as a
#: literal (issue #487).
TX_SKIP_TRIES = 8
DEC_ZP, ZP_COUNT = 0xC6, 0xFB
#: SkipNow as every emitter writes it: PPPtr = RxCFG, then its low byte |= $40.
SKIPNOW = pptr_set(PP_RXCFG) + _lda(PPDATA_LO) + bytes([ORA_IMM, 0x40]) + _sta(PPDATA_LO)


def _skip_phase_anchors(code: bytes) -> set[int]:
    """Offsets of every ``PPPtr=BusST`` that opens a #487 skip phase: the one
    immediately after ``LDA #tries / STA $FB``."""
    return {m.start() for m in re.finditer(re.escape(pptr_set(PP_BUSST)), code)
            if code[m.start() - 2:m.start()] == bytes([0x85, ZP_COUNT])}


def _skip_phases(code: bytes) -> list[dict[str, int]]:
    """Decode the ip65-parity skip phase in front of every bounded poll (#487).

    Emitted shape (:func:`bridge_ping._emit_tx_frame`)::

              LDA #8 / STA $FB
        sk:   PPPtr = BusST
              LDA PPData_hi / AND #$01 / BNE go
              PPPtr = RxEvent
              LDA PPData_hi / AND #$0D / BEQ nf
              SkipNow                  ; RxCFG low |= $40
        nf:   DEC $FB / BNE sk
              LDY #lo / LDX #hi / PPPtr = BusST / ...the #236 bounded poll

    Decoded by position from the anchor, like :func:`_rdy4txnow_polls`, so
    a mutated byte is reported rather than silently unmatched.
    """
    out = []
    for at in sorted(_skip_phase_anchors(code)):
        lda = at + len(pptr_set(PP_BUSST))
        rxe = lda + 7
        rxlda = rxe + len(pptr_set(PP_RXEVENT))
        skip = rxlda + 7
        nf = skip + len(SKIPNOW)
        out.append({
            "anchor": at, "tries": code[at - 3], "tries_op": code[at - 4],
            "busst_poll": code[lda:lda + 6], "exit_target": _disp(code, lda + 5),
            "rxevent_ptr": code[rxe:rxlda], "rxevent_poll": code[rxlda:rxlda + 6],
            "nf_target": _disp(code, rxlda + 5), "skip": code[skip:nf], "nf": nf,
            "dec": code[nf:nf + 2], "loop_op": code[nf + 2], "loop_target": _disp(code, nf + 2),
            "then": nf + 4,
        })
    return out


def _rdy4txnow_polls(code: bytes) -> list[dict[str, int]]:
    """Decode the bounded poll around every ``PPPtr=BusST`` write in *code*.

    Emitted shape (:func:`bridge_ping._emit_tx_frame`, issue #236)::

        LDY #lo / LDX #hi                 ; poll budget, X:Y
        PPPtr = BusST
        lda:  LDA PPData_hi / AND #$01 / BNE go
              DEY / BNE lda / DEX / BNE lda
              JMP not_ready               ; budget spent, Rdy4TxNOW never set
        go:   ...copy loop

    ip65's ``send`` (``drivers/cs8900a.s:441-466``) is also a bounded loop
    (eight passes, ``skipframe`` in between); before #236 the harness spun
    ``BEQ`` back to the ``LDA`` forever.

    Decoded by position rather than matched by regex on purpose: a regex
    that *searched* for the loop would simply not match one whose branch
    had been NOPed, which is the escape of issue #224.  Here the
    ``PPPtr=BusST`` write is the anchor and whatever surrounds it is
    reported, so the caller's assertions see the mutated bytes.
    """
    out = []
    for m in re.finditer(re.escape(pptr_set(PP_BUSST)), code):
        if m.start() in _skip_phase_anchors(code):
            continue                      # the #487 skip phase; see _skip_phases
        lda = m.end()
        assert code[lda:lda + 3] == _lda(PPDATA_HI), (
            f"PPPtr=BusST at offset {m.start()} is not followed by LDA PPData_hi "
            f"(got {code[lda:lda + 3].hex()})"
        )
        assert code[lda + 3] == AND_IMM, (
            f"BusST poll at offset {lda} does not mask PPData_hi (opcode "
            f"{code[lda + 3]:#04x} where AND #imm was expected)"
        )
        pre = code[m.start() - 4:m.start()]
        jmp_abs = code[lda + 14] | code[lda + 15] << 8
        out.append({
            "pptr": m.start(), "lda": lda, "mask": code[lda + 4],
            "counter_ops": (pre[0], pre[2]), "counter": (pre[1], pre[3]),
            "exit_op": code[lda + 5], "exit_target": _disp(code, lda + 5),
            "dey": code[lda + 7], "dey_op": code[lda + 8],
            "dey_target": _disp(code, lda + 8),
            "dex": code[lda + 10], "dex_op": code[lda + 11],
            "dex_target": _disp(code, lda + 11),
            "jmp": code[lda + 13], "jmp_off": jmp_abs - LOAD,
            "go": lda + 16,
        })
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
def test_every_rdy4txnow_poll_is_bounded_and_exits_to_not_ready(name: str) -> None:
    """Every BusST poll waits for ``Rdy4TxNOW``, re-reads BusST every pass,
    gives up after :data:`RDY4TXNOW_MAX_POLLS` passes, and gives up into an
    exit that stores ``0x04`` and returns.

    Issue #224: the walkers above classify a poll by the ``LDA / AND`` pair
    and never look at what follows, so a builder whose wait branch had
    been NOPed -- one that writes the frame whether or not the chip said
    ``Rdy4TxNOW`` -- passed every structural test.  Issue #236: the wait
    had no bound, so a wedged chip (#234) hung the 6510 with no diagnosis;
    under VICE the chip is always ready, so only these bytes and the
    simulator tests in ``test_cs8900a_tx_bound.py`` can see a regression.

    Rejected by name, each a plausible edit: a wait branch that is not
    ``BNE`` to the copy (``BEQ`` inverts it, a NOP drops it); a counter
    branch that does not return to the ``LDA`` (re-tests a stale
    accumulator or never re-reads BusST); a lost ``DEX`` stage (the bound
    shrinks to 256); a bound other than 65,536; a give-up path that jumps
    anywhere but a ``RESULT_TX_NOT_READY`` exit.
    """
    code = TX_BUILDERS[name]()
    polls = _rdy4txnow_polls(code)
    assert len(polls) == _expected_tx_sites(name), (
        f"{name}: {len(polls)} BusST poll(s), expected {_expected_tx_sites(name)}"
    )
    for p in polls:
        where = f"{name}: BusST poll at offset {p['lda']}"
        assert p["mask"] == BUSST_RDY4TXNOW_MASK, (
            f"{where} masks 0x{p['mask']:02X}; Rdy4TxNOW is bit 8 of BusST, "
            f"0x{BUSST_RDY4TXNOW_MASK:02X} of the high byte"
        )
        assert p["exit_op"] == BNE and p["exit_target"] == p["go"], (
            f"{where}: after AND #$01 expected BNE to the copy at {p['go']}, got "
            f"opcode {p['exit_op']:#04x} -> {p['exit_target']}"
            + (" (BEQ inverts the wait)" if p["exit_op"] == BEQ else "")
        )
        assert (p["dey"], p["dey_op"], p["dey_target"]) == (DEY, BNE, p["lda"]), (
            f"{where}: inner count is not DEY / BNE back to the LDA "
            f"(got {p['dey']:#04x} {p['dey_op']:#04x} -> {p['dey_target']})"
        )
        assert (p["dex"], p["dex_op"], p["dex_target"]) == (DEX, BNE, p["lda"]), (
            f"{where}: outer count is not DEX / BNE back to the LDA "
            f"(got {p['dex']:#04x} {p['dex_op']:#04x} -> {p['dex_target']})"
        )
        assert p["counter_ops"] == (LDY_IMM, LDX_IMM), (
            f"{where}: the poll budget is not loaded as LDY #lo / LDX #hi "
            f"immediately before PPPtr=BusST (got {p['counter_ops']})"
        )
        lo, hi = p["counter"]
        polls_allowed = ((lo or 256) - 1) + 256 * ((hi or 256) - 1) + 1
        assert polls_allowed == RDY4TXNOW_MAX_POLLS, (
            f"{where}: budget LDY #${lo:02X} / LDX #${hi:02X} allows "
            f"{polls_allowed} polls, expected {RDY4TXNOW_MAX_POLLS}"
        )
        assert p["jmp"] == JMP_ABS, f"{where}: budget exhaustion does not JMP out"
        exit_ = code[p["jmp_off"]:p["jmp_off"] + 7]
        assert exit_ == bytes([LDA_IMM, TX_NOT_READY]) + _sta(RESULT) + bytes([CLI, RTS]), (
            f"{where}: give-up JMP lands on {exit_.hex()}, not "
            f"LDA #${TX_NOT_READY:02X} / STA result / CLI / RTS"
        )


@pytest.mark.parametrize("name", sorted(TX_BUILDERS))
def test_tx_sequence_is_txcmd_txlen_busst_poll_then_data(name: str) -> None:
    """TxCMD, TxLength, PPPtr=BusST, poll Rdy4TxNOW (with its branch), then
    the RTDATA writes -- at **every** TX site of the builder.

    This is the CS8900a's documented TX handshake and ip65's order
    (``drivers/cs8900a.s:441-457``: ``txcmd``, ``txlen``, then the BusST
    spin, then the copy).  Writing data before the chip has said
    Rdy4TxNOW, or TxLength before TxCMD, passes under VICE and loses
    frames on silicon.

    Each site is checked inside its own window, from its ``STA TxCMD_lo``
    to the next site's (or the end of the routine).  The previous form
    took the first hit of each step from the start of the routine, so on
    a two-site builder the second site's poll satisfied the first site's
    search and a poll moved out of place was not seen (found while
    mutation-testing #224).
    """
    code = TX_BUILDERS[name]()
    sites = [m.start() for m in re.finditer(re.escape(_sta(TXCMD_LO)), code)]
    assert len(sites) == _expected_tx_sites(name), (
        f"{name}: {len(sites)} TxCMD write(s), expected {_expected_tx_sites(name)}"
    )
    polls = {p["pptr"]: p for p in _rdy4txnow_polls(code)}
    poll_bytes = _lda(PPDATA_HI) + bytes([AND_IMM, BUSST_RDY4TXNOW_MASK, BNE])
    for i, txcmd_lo in enumerate(sites):
        end = sites[i + 1] if i + 1 < len(sites) else len(code)
        window = code[:end]

        def step(seq: bytes, after: int) -> int:
            off = window.find(seq, after) if after >= 0 else -1
            return off

        txcmd_hi = step(_sta(TXCMD_HI), txcmd_lo)
        txlen_lo = step(_sta(TXLEN_LO), txcmd_hi)
        txlen_hi = step(_sta(TXLEN_HI), txlen_lo)
        skip_phase = step(pptr_set(PP_BUSST), txlen_hi)
        busst = step(pptr_set(PP_BUSST), skip_phase + 1) if skip_phase >= 0 else -1
        poll = step(poll_bytes, busst)
        data = step(_sta(RTDATA_LO), poll)
        order = [txcmd_lo, txcmd_hi, txlen_lo, txlen_hi, skip_phase, busst, poll, data]
        assert all(o >= 0 for o in order), (
            f"{name}: TX site {i} (TxCMD at {txcmd_lo}) has a handshake step "
            f"missing (offsets {order}; -1 = not found after the previous step "
            f"before offset {end})"
        )
        assert order == sorted(order)
        assert busst in polls and polls[busst]["dey_target"] == polls[busst]["lda"], (
            f"{name}: TX site {i}: the poll after PPPtr=BusST at {busst} does "
            "not branch back to its LDA"
        )
        assert polls[busst]["go"] <= data, (
            f"{name}: TX site {i}: RTDATA is written inside the poll loop"
        )
        assert skip_phase in _skip_phase_anchors(code), (
            f"{name}: TX site {i}: the first PPPtr=BusST after TxLength at "
            f"{skip_phase} is not a #487 skip phase"
        )


@pytest.mark.parametrize("name", sorted(TX_BUILDERS))
def test_every_tx_site_skips_a_received_frame_per_clear_read_like_ip65(name: str) -> None:
    """Issue #487: before the bounded poll, each TX site checks Rdy4TxNOW
    :data:`TX_SKIP_TRIES` times, going straight to the copy when it is set
    and otherwise issuing SkipNow (only when RxEvent says a frame is
    queued) -- ip65's ``send`` (``drivers/cs8900a.s:452-467``).  Rejected
    by name: another try count, a ready branch that does not reach the
    bounded poll's copy, a SkipNow without the RESET-free ``$40`` bit on
    RxCFG's low byte, a loop that does not return to re-aim at BusST, and a
    phase that falls anywhere but the bounded poll."""
    code = TX_BUILDERS[name]()
    phases = _skip_phases(code)
    polls = _rdy4txnow_polls(code)
    assert len(phases) == _expected_tx_sites(name) == len(polls), (
        f"{name}: {len(phases)} skip phase(s), {len(polls)} bounded poll(s), "
        f"expected {_expected_tx_sites(name)}"
    )
    for ph, poll in zip(phases, polls):
        where = f"{name}: skip phase at offset {ph['anchor']}"
        assert (ph["tries_op"], ph["tries"]) == (LDA_IMM, TX_SKIP_TRIES), (
            f"{where}: try count LDA #${ph['tries']:02X}, expected #{TX_SKIP_TRIES} (ip65)"
        )
        assert ph["busst_poll"] == _lda(PPDATA_HI) + bytes([AND_IMM, BUSST_RDY4TXNOW_MASK, BNE]), (
            f"{where}: does not test Rdy4TxNOW with BNE (got {ph['busst_poll'].hex()})"
        )
        assert ph["exit_target"] == poll["go"], (
            f"{where}: ready branch lands at {ph['exit_target']}, not the copy at {poll['go']}"
        )
        assert ph["rxevent_ptr"] == pptr_set(PP_RXEVENT)
        assert ph["rxevent_poll"][:5] == _lda(PPDATA_HI) + bytes([AND_IMM, RXEVENT_IP65_MASK]) \
            and ph["rxevent_poll"][5] == BEQ and ph["nf_target"] == ph["nf"], (
            f"{where}: RxEvent check is not LDA hi / AND #$0D / BEQ past the SkipNow"
        )
        assert ph["skip"] == SKIPNOW, f"{where}: SkipNow is {ph['skip'].hex()}"
        assert ph["dec"] == bytes([DEC_ZP, ZP_COUNT]) and ph["loop_op"] == BNE \
            and ph["loop_target"] == ph["anchor"], (
            f"{where}: the try loop is not DEC $FB / BNE back to the BusST aim"
        )
        assert ph["then"] == poll["pptr"] - 4, (
            f"{where}: falls through to {ph['then']}, not the bounded poll's "
            f"LDY/LDX at {poll['pptr'] - 4}"
        )


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
    # Since #487 every TX site reads RxEvent in its skip phase, so a
    # transmitter reads it whether or not it also polls for frames.
    assert (PP_RXEVENT in registers) == (name in RX_POLLERS or name in TX_BUILDERS), (
        f"{name}: RxEvent poll presence does not match whether it polls or transmits"
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
