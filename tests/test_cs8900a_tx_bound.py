"""Bounded CS8900a hardware waits, run on the simulated chip (#234, #235, #236, #238).

Under VICE the emulated chip asserts ``Rdy4TxNOW`` at once and RESET is
instantaneous, so no VICE test can fail on any of these.  The protection
is here and in the structural pins of ``test_cs8900a_register_pins.py``:

* **#236** -- every TX site gives up after
  :data:`~c64_test_harness.bridge_ping.CS8900A_TX_READY_MAX_POLLS` polls
  and stores :data:`~c64_test_harness.bridge_ping.RESULT_TX_NOT_READY`
  instead of spinning forever on a wedged chip.  All nine call sites of
  ``_emit_tx_frame`` are reached, each on a chip that readies exactly the
  bids before it.
* **#238** -- ``frame_len`` odd, zero, or above 256 is refused at emit
  time on every public route to the copy loop.
* **#234** -- :func:`~c64_test_harness.bridge_ping.build_cs8900a_reset_code`
  resets the chip through SelfCTL, waits a bounded time for RESET to
  self-clear, and re-initialises; a RESET that never clears returns
  :data:`~c64_test_harness.bridge_ping.RESULT_RESET_TIMEOUT`.
* **#235** -- ``build_tx_code``'s docstring states what ``0x01`` does and
  does not mean.

Hardware facts are pinned as literals (65,536 polls, ``0x04``/``0x05``,
SelfCTL ``$0114`` bit 6) so a wrong constant cannot validate itself.
"""
from __future__ import annotations

import pytest

from c64_test_harness import bridge_ping as bp
from c64_test_harness.bridge_ping import (
    Asm,
    build_arp_request_frame,
    build_echo_request_frame,
)
from cs8900a_sim import BUSST_RDY4TXNOW, PP_BUSST, Cpu6502, Cs8900aSim

LOAD, TX_BUF, ARP_BUF, RX_BUF, RESULT = 0x4000, 0x5000, 0x5080, 0x5100, 0x5300
MAC_A = bytes.fromhex("02c640000001")
MAC_B = bytes.fromhex("c05627b11638")
IP_A, IP_B = bytes([10, 0, 66, 201]), bytes([10, 0, 66, 1])

MAX_POLLS = 65536          # #234's measured wedge window, literal on purpose
TX_NOT_READY = 0x04
RESET_TIMEOUT = 0x05
PP_SELFCTL, PP_RXCTL, PP_LINECTL, PP_IA = 0x0114, 0x0104, 0x0112, 0x0158

ECHO = build_echo_request_frame(MAC_A, MAC_B, IP_A, IP_B, identifier=0x1234, sequence=1)
ARP_REQ = build_arp_request_frame(MAC_A, IP_A, IP_B)
# What the responders receive: an echo request to IP_B, an ARP request for IP_B.
ECHO_TO_B = build_echo_request_frame(MAC_A, MAC_B, IP_A, IP_B).frame
ARP_FOR_B = build_arp_request_frame(MAC_A, IP_A, IP_B)


def _run(code: bytes, chip: Cs8900aSim, preload: dict[int, bytes] | None = None,
         max_steps: int = 3_000_000) -> Cpu6502:
    cpu = Cpu6502(chip)
    for addr, data in (preload or {}).items():
        cpu.load(addr, data)
    cpu.load(LOAD, code)
    cpu.mem[RESULT] = 0xAA
    cpu.jsr(LOAD, max_steps=max_steps)
    return cpu


# ===========================================================================
# #236: every TX site is bounded
# ===========================================================================

def _echo_reply_for(echo_frame: bytes) -> bytes:
    f = bytearray(echo_frame)
    f[0:6], f[6:12] = echo_frame[6:12], echo_frame[0:6]
    f[26:30], f[30:34] = echo_frame[30:34], echo_frame[26:30]
    f[34] = 0
    cksum = int.from_bytes(f[36:38], "big") + 0x0800
    f[36:38] = ((cksum & 0xFFFF) + (cksum >> 16)).to_bytes(2, "big")
    return bytes(f)


ECHO_REPLY = _echo_reply_for(ECHO.frame)


class Site:
    """One ``_emit_tx_frame`` call site and how to reach it.

    ``ready`` bids are satisfied before the site under test, so on a chip
    with ``tx_ready_budget=ready`` the wedge lands exactly on it.  ``rx``
    is queued so a fully ready chip runs the routine to a terminating exit
    (the simulated TOD never advances, so a routine left polling never
    returns); ``ok`` is ``(result, frames sent)`` on that ready chip.
    """

    def __init__(self, build, rx, ready, ok):
        self.build, self.rx, self.ready, self.ok = build, rx, ready, ok


# Together these reach all nine _emit_tx_frame call sites: build_tx_code;
# the ARP and echo sites of both ping builders; the reply sites of the three
# responders; and _emit_arp_responder (reached from each [arp] responder).
SITES = {
    "build_tx_code": Site(
        lambda: bp.build_tx_code(LOAD, TX_BUF, len(ECHO.frame), RESULT), [], 0, (0x01, 1)),
    "build_ping_and_wait_code/echo": Site(
        lambda: bp.build_ping_and_wait_code(
            LOAD, TX_BUF, len(ECHO.frame), RX_BUF, RESULT, 0x1234, 1),
        [ECHO_REPLY], 0, (0x01, 1)),
    "build_ping_and_wait_code/arp": Site(
        lambda: bp.build_ping_and_wait_code(
            LOAD, TX_BUF, len(ECHO.frame), RX_BUF, RESULT, 0x1234, 1, arp_frame_buf=ARP_BUF),
        [ECHO_REPLY], 0, (0x01, 2)),
    "build_ping_and_wait_code/echo-after-arp": Site(
        lambda: bp.build_ping_and_wait_code(
            LOAD, TX_BUF, len(ECHO.frame), RX_BUF, RESULT, 0x1234, 1, arp_frame_buf=ARP_BUF),
        [ECHO_REPLY], 1, (0x01, 2)),
    "build_ping_and_wait_tod_code/arp": Site(
        lambda: bp.build_ping_and_wait_tod_code(
            LOAD, TX_BUF, len(ECHO.frame), RX_BUF, RESULT, 0x1234, 1, arp_frame_buf=ARP_BUF),
        [ECHO_REPLY], 0, (0x01, 2)),
    "build_ping_and_wait_tod_code/echo-after-arp": Site(
        lambda: bp.build_ping_and_wait_tod_code(
            LOAD, TX_BUF, len(ECHO.frame), RX_BUF, RESULT, 0x1234, 1, arp_frame_buf=ARP_BUF),
        [ECHO_REPLY], 1, (0x01, 2)),
    "build_icmp_responder_code/reply": Site(
        lambda: bp.build_icmp_responder_code(LOAD, RX_BUF, IP_B, RESULT),
        [ECHO_TO_B], 0, (0x01, 1)),
    "build_icmp_responder_code/arp-reply": Site(
        lambda: bp.build_icmp_responder_code(LOAD, RX_BUF, IP_B, RESULT, my_mac=MAC_B),
        [ARP_FOR_B, ECHO_TO_B], 0, (0x01, 2)),
    "build_read_and_respond_echo_request_code/reply": Site(
        lambda: bp.build_read_and_respond_echo_request_code(LOAD, RX_BUF, IP_B, RESULT),
        [ECHO_TO_B], 0, (0x01, 1)),
    "build_read_and_respond_echo_request_code/arp-reply": Site(
        lambda: bp.build_read_and_respond_echo_request_code(
            LOAD, RX_BUF, IP_B, RESULT, my_mac=MAC_B),
        [ARP_FOR_B], 0, (bp.RESULT_ARP_REPLY_SENT, 1)),
    "build_icmp_responder_tod_code/reply": Site(
        lambda: bp.build_icmp_responder_tod_code(LOAD, RX_BUF, IP_B, RESULT),
        [ECHO_TO_B], 0, (0x01, 1)),
    "build_icmp_responder_tod_code/arp-reply": Site(
        lambda: bp.build_icmp_responder_tod_code(LOAD, RX_BUF, IP_B, RESULT, my_mac=MAC_B),
        [ARP_FOR_B, ECHO_TO_B], 0, (0x01, 2)),
}


def _site_run(name: str, budget: int | None):
    site = SITES[name]
    chip = Cs8900aSim(rx_queue=list(site.rx), tx_ready_budget=budget)
    cpu = _run(site.build(), chip, {TX_BUF: ECHO.frame, ARP_BUF: ARP_REQ})
    return cpu, chip


@pytest.mark.parametrize("name", sorted(SITES))
def test_a_wedged_chip_returns_not_ready_instead_of_hanging(name: str) -> None:
    """Rdy4TxNOW never asserts for the site under test: the routine must
    return, store 0x04, poll exactly 65,536 times, and copy nothing.

    Before #236 this raised ``SimError: step budget ... exhausted`` -- the
    6510 spinning on BusST, which on hardware is a stopped machine."""
    ready = SITES[name].ready
    cpu, chip = _site_run(name, ready)
    assert cpu.mem[RESULT] == TX_NOT_READY, (
        f"{name}: result {cpu.mem[RESULT]:#04x}, expected RESULT_TX_NOT_READY"
    )
    assert len(chip.tx_frames) == ready, f"{name}: {len(chip.tx_frames)} frame(s) sent"
    assert chip.tx_bids == ready + 1, f"{name}: the wedged bid was never made"
    # Each ready bid is satisfied on its first poll; the wedged one uses the budget.
    assert chip.busst_hi_reads == ready + MAX_POLLS, (
        f"{name}: {chip.busst_hi_reads} BusST polls, expected {ready} + {MAX_POLLS}"
    )


@pytest.mark.parametrize("name", sorted(SITES))
def test_a_ready_chip_still_completes_at_every_site(name: str) -> None:
    """The bound is not allowed to cost a frame, or change the result, when
    the chip is ready: every site transmits on its first poll."""
    cpu, chip = _site_run(name, None)
    assert (cpu.mem[RESULT], len(chip.tx_frames)) == SITES[name].ok
    assert chip.busst_hi_reads == len(chip.tx_frames)


class _ReadyOnPoll(Cs8900aSim):
    """Rdy4TxNOW asserts on the *k*-th BusST high-byte read and stays set."""

    def __init__(self, k: int) -> None:
        super().__init__()
        self.k = k

    def _pp_read(self, pp: int) -> int:
        if pp == PP_BUSST and self.clockport:
            return BUSST_RDY4TXNOW if self.busst_hi_reads >= self.k else 0x0018
        return super()._pp_read(pp)


def _emit_one_tx(max_polls: int) -> bytes:
    a = Asm(org=LOAD)
    a.emit(0xA9, 0x01, 0x8D, 0x01, 0xDE)          # clockport on
    bp._emit_tx_frame(a, TX_BUF, 60, "t", "fail", max_polls=max_polls)
    a.emit(0xA9, 0x01, 0x8D, RESULT & 0xFF, RESULT >> 8, 0x60)
    a.label("fail")
    a.emit(0xA9, TX_NOT_READY, 0x8D, RESULT & 0xFF, RESULT >> 8, 0x60)
    return a.build()


@pytest.mark.parametrize("n", [1, 2, 255, 256, 257, 511, 65535, 65536])
def test_the_poll_bound_is_exact(n: int) -> None:
    """X:Y encodes *n* polls exactly: ready on poll *n* transmits, never
    ready gives up after exactly *n* reads.  The 256-boundary cases are
    where an 8-bit counter, or an off-by-one in the ``lo``/``hi`` split,
    would show."""
    chip = Cs8900aSim(tx_ready_budget=0)
    cpu = _run(_emit_one_tx(n), chip, {TX_BUF: ECHO.frame})
    assert (cpu.mem[RESULT], chip.busst_hi_reads, chip.tx_frames) == (TX_NOT_READY, n, [])

    chip = _ReadyOnPoll(n)
    cpu = _run(_emit_one_tx(n), chip, {TX_BUF: ECHO.frame})
    assert cpu.mem[RESULT] == 0x01 and chip.busst_hi_reads == n
    assert chip.tx_frames == [ECHO.frame[:60]]


@pytest.mark.parametrize("n", [0, -1, 65537])
def test_a_poll_bound_outside_1_to_65536_is_refused(n: int) -> None:
    with pytest.raises(ValueError, match="max_polls"):
        _emit_one_tx(n)


def test_tx_bound_constants_are_the_documented_literals() -> None:
    assert bp.CS8900A_TX_READY_MAX_POLLS == MAX_POLLS
    assert bp.RESULT_TX_NOT_READY == TX_NOT_READY
    codes = {0x01, 0x02, 0xFF, bp.RESULT_ARP_REPLY_SENT}
    assert TX_NOT_READY not in codes and RESET_TIMEOUT not in codes | {TX_NOT_READY}


# ===========================================================================
# #238: frame_len must be even and at most 256, refused at emit time
# ===========================================================================

BAD_LENS = [0, 1, 61, 255, 257, 258, 300, 512, 1066, 1514, -2]
GOOD_LENS = [2, 60, 62, 254, 256]

LEN_ROUTES = {
    "build_tx_code": lambda n: bp.build_tx_code(LOAD, TX_BUF, n, RESULT),
    "build_ping_and_wait_code.tx_frame_len": lambda n: bp.build_ping_and_wait_code(
        LOAD, TX_BUF, n, RX_BUF, RESULT, 1, 1),
    "build_ping_and_wait_code.arp_frame_len": lambda n: bp.build_ping_and_wait_code(
        LOAD, TX_BUF, 60, RX_BUF, RESULT, 1, 1, arp_frame_buf=ARP_BUF, arp_frame_len=n),
    "build_ping_and_wait_tod_code.tx_frame_len": lambda n: bp.build_ping_and_wait_tod_code(
        LOAD, TX_BUF, n, RX_BUF, RESULT, 1, 1),
    "build_ping_and_wait_tod_code.arp_frame_len": lambda n: bp.build_ping_and_wait_tod_code(
        LOAD, TX_BUF, 60, RX_BUF, RESULT, 1, 1, arp_frame_buf=ARP_BUF, arp_frame_len=n),
}


@pytest.mark.parametrize("n", BAD_LENS)
@pytest.mark.parametrize("route", sorted(LEN_ROUTES))
def test_a_frame_len_the_copy_loop_cannot_honour_is_refused(route: str, n: int) -> None:
    """Odd: one frame escapes, then the 6510 hangs with SEI in force.  Even
    and above 256: a silent no-op reported as 0x01.  Measured on hardware
    in #238; both must fail loudly here instead."""
    with pytest.raises(ValueError, match=r"even.*256|256.*even"):
        LEN_ROUTES[route](n)


@pytest.mark.parametrize("n", GOOD_LENS)
@pytest.mark.parametrize("route", sorted(LEN_ROUTES))
def test_even_lengths_up_to_256_are_accepted(route: str, n: int) -> None:
    LEN_ROUTES[route](n)


def test_a_256_byte_frame_goes_out_whole() -> None:
    frame = bytes(range(256))
    chip = Cs8900aSim()
    cpu = _run(bp.build_tx_code(LOAD, TX_BUF, 256, RESULT), chip, {TX_BUF: frame})
    assert cpu.mem[RESULT] == 0x01 and chip.tx_frames == [frame]


# ===========================================================================
# #235: the result byte's contract is written down where callers read it
# ===========================================================================

def test_build_tx_code_docstring_states_the_real_contract() -> None:
    doc = " ".join((bp.build_tx_code.__doc__ or "").split())
    assert "on success" not in doc, "0x01 is not a success flag (#235)"
    assert "Rdy4TxNOW" in doc and "delivery" in doc
    assert "RESULT_TX_NOT_READY" in doc
    assert "even" in doc and "256" in doc, "the #238 precondition belongs on the public builder"


# ===========================================================================
# #234: chip reset through SelfCTL, bounded, then re-init
# ===========================================================================

def _reset_run(chip: Cs8900aSim, **kw) -> Cpu6502:
    return _run(bp.build_cs8900a_reset_code(LOAD, RESULT, **kw), chip)


def _writes_after_reset(chip: Cs8900aSim) -> list[tuple[int, bool, int]]:
    i = next(i for i, (pp, hi, v) in enumerate(chip.pp_writes)
             if pp == PP_SELFCTL and not hi and v & 0x40)
    return chip.pp_writes[:i], chip.pp_writes[i + 1:]


def test_reset_writes_selfctl_reset_waits_for_it_to_clear_then_reinitialises() -> None:
    chip = Cs8900aSim(reset_clears_after=3)
    cpu = _reset_run(chip, mac=MAC_A)
    assert cpu.mem[RESULT] == 0x01
    assert chip.resets == 1
    assert chip.selfctl_reads == 4, "three reads with RESET set, one clear"
    before, after = _writes_after_reset(chip)
    assert before == [], "nothing is programmed before the reset it would be lost to"
    assert (PP_SELFCTL, True, 0) not in chip.pp_writes, "ip65 writes only SelfCTL's low byte"
    # ip65's init order after the reset: RxCTL, Individual Address, LineCTL.
    order = [pp for pp, _, _ in after]
    assert order[:2] == [PP_RXCTL, PP_RXCTL]
    assert order[2:8] == [PP_IA, PP_IA, PP_IA + 2, PP_IA + 2, PP_IA + 4, PP_IA + 4]
    assert order[8:] == [PP_LINECTL]
    assert after[0][2] | after[1][2] << 8 == bp.CS8900A_RXCTL_VALUE
    assert bytes(v for pp, _, v in after[2:8]) == MAC_A
    assert after[8][2] & 0xC0 == 0xC0, "SerRxON | SerTxON"


def test_reset_without_mac_programs_rxctl_and_linectl_only() -> None:
    chip = Cs8900aSim()
    cpu = _reset_run(chip, rxctl_value=bp.CS8900A_RXCTL_VALUE_IP65)
    _, after = _writes_after_reset(chip)
    assert cpu.mem[RESULT] == 0x01
    assert [pp for pp, _, _ in after] == [PP_RXCTL, PP_RXCTL, PP_LINECTL]
    assert after[0][2] | after[1][2] << 8 == 0x0D05


def test_a_reset_that_never_clears_returns_timeout_and_does_not_reinitialise() -> None:
    chip = Cs8900aSim(reset_clears_after=None)
    cpu = _reset_run(chip, mac=MAC_A)
    assert cpu.mem[RESULT] == RESET_TIMEOUT
    assert chip.selfctl_reads == MAX_POLLS
    _, after = _writes_after_reset(chip)
    assert after == [], "nothing is programmed into a chip still in reset"


@pytest.mark.parametrize("n", [1, 256, 257])
def test_reset_bound_is_exact(n: int) -> None:
    chip = Cs8900aSim(reset_clears_after=n - 1)     # clear on read n
    assert _reset_run(chip, max_polls=n).mem[RESULT] == 0x01
    assert chip.selfctl_reads == n
    chip = Cs8900aSim(reset_clears_after=n)         # clear on read n+1: too late
    assert _reset_run(chip, max_polls=n).mem[RESULT] == RESET_TIMEOUT
    assert chip.selfctl_reads == n


def test_reset_then_a_bounded_tx_is_the_readiness_probe() -> None:
    """#234 asked for a probe that tells a wedged chip from a ready one
    without hanging: reset, then transmit through the bounded poll.  A
    ready chip sends; a wedged one reports 0x04."""
    for budget, want in ((None, 0x01), (0, TX_NOT_READY)):
        chip = Cs8900aSim(tx_ready_budget=budget)
        cpu = Cpu6502(chip)
        cpu.load(TX_BUF, ECHO.frame)
        cpu.load(LOAD, bp.build_cs8900a_reset_code(LOAD, RESULT))
        cpu.jsr(LOAD)
        assert cpu.mem[RESULT] == 0x01
        cpu.load(0x6000, bp.build_tx_code(0x6000, TX_BUF, len(ECHO.frame), RESULT))
        cpu.jsr(0x6000, max_steps=3_000_000)
        assert cpu.mem[RESULT] == want


def test_reset_rejects_a_bad_mac_and_a_bad_bound() -> None:
    with pytest.raises(ValueError):
        bp.build_cs8900a_reset_code(LOAD, RESULT, mac=b"\x00" * 5)
    with pytest.raises(ValueError, match="max_polls"):
        bp.build_cs8900a_reset_code(LOAD, RESULT, max_polls=0)


def test_reset_constants_are_the_documented_literals() -> None:
    assert bp.CS8900A_RESET_MAX_POLLS == MAX_POLLS
    assert bp.RESULT_RESET_TIMEOUT == RESET_TIMEOUT
