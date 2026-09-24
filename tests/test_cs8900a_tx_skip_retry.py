"""Every TX site SkipNows received frames when ``Rdy4TxNOW`` is clear (issue #487).

ip65's ``send`` (``drivers/cs8900a.s:452-467``, source-read at
``~/Documents/c64-https/ip65``) bids, then checks BusST bit 8
(``Rdy4TxNOW``) up to 8 times, and on each clear read issues SkipNow
(RxCFG ``|= $40``) to free a received frame from the shared buffer.
The harness only polled, and on silicon a chip starved by unread RX frames
never asserts ``Rdy4TxNOW`` until they are drained or the chip is reset
(measured, U64E fw 3.15 ``bce4535e``, 2026-09-23, #303: 3 injected host
frames -> ``0x04`` 5/6 vs 0/6; persistent 16/16).  Since #487
``_emit_tx_frame`` does ip65's 8 checks with a SkipNow after each clear
one, then falls into the old bounded poll -- so every builder transmits
through a starved chip without ``drain_first``.

These run on the simulated chip's starvation model
(``Cs8900aSim.tx_starved_by_rx``: any queued frame starves every bid).  The
live check (starved chip, no ``drain_first``) waits for the RR-Net to be
re-cabled.
"""
from __future__ import annotations

import pytest

import c64_test_harness.bridge_ping as bp
from c64_test_harness.bridge_ping import build_echo_request_frame
from cs8900a_sim import Cpu6502, Cs8900aSim
from test_cs8900a_drain import _ChipThatAnswersLater, _echo_reply_for, _stale

LOAD, TX_BUF, ARP_BUF, RX_BUF, RESULT = 0x4000, 0x5000, 0x5080, 0x5100, 0x5300
BIG_BUF = 0x7000        # build_tx_code frames up to 1514 B: clear of RESULT
MAC_C64 = bytes.fromhex("02c640000001")
MAC_HOST = bytes.fromhex("c05627b11638")
IP_C64, IP_HOST = bytes([10, 0, 66, 201]), bytes([10, 0, 66, 1])
TRIES = 8               # ip65's count; bp.CS8900A_TX_SKIP_TRIES must equal it


def _frame(n: int) -> bytes:
    return bytes((i * 7 + 3) & 0xFF for i in range(n))


def _jsr(chip: Cs8900aSim, code: bytes, preload: dict[int, bytes]) -> Cpu6502:
    cpu = Cpu6502(chip)
    for addr, data in preload.items():
        cpu.load(addr, data)
    cpu.load(RESULT, b"\x00")
    cpu.load(LOAD, code)
    cpu.jsr(LOAD, max_steps=5_000_000)
    return cpu


# --------------------------------------------------------------------------- #
# build_tx_code                                                                #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("n", [60, 258, 1514])
@pytest.mark.parametrize("queued", [1, 3, TRIES])
def test_a_starved_chip_transmits_without_drain_first(n: int, queued: int) -> None:
    chip = Cs8900aSim(rx_queue=[_frame(64) for _ in range(queued)], tx_starved_by_rx=True)
    cpu = _jsr(chip, bp.build_tx_code(LOAD, BIG_BUF, n, RESULT), {BIG_BUF: _frame(n) + b"\0"})
    assert cpu.read(RESULT) == 0x01
    assert chip.tx_frames == [_frame(n)]
    assert chip.rx_queue == []


def test_more_frames_than_tries_still_refuses_and_skips_exactly_the_bound() -> None:
    """ip65's bound: 8 SkipNows, then give up (here: the old bounded poll, 0x04)."""
    chip = Cs8900aSim(rx_queue=[_frame(64) for _ in range(TRIES + 1)], tx_starved_by_rx=True)
    cpu = _jsr(chip, bp.build_tx_code(LOAD, BIG_BUF, 60, RESULT), {BIG_BUF: _frame(60)})
    assert cpu.read(RESULT) == bp.RESULT_TX_NOT_READY
    assert chip.tx_frames == []
    assert len(chip.rx_queue) == 1


def test_the_try_count_is_ip65s() -> None:
    assert bp.CS8900A_TX_SKIP_TRIES == TRIES


def test_a_ready_chip_skips_nothing() -> None:
    """Frames queued but the bid is ready: nothing is discarded."""
    chip = Cs8900aSim(rx_queue=[_frame(64) for _ in range(3)])
    cpu = _jsr(chip, bp.build_tx_code(LOAD, BIG_BUF, 60, RESULT), {BIG_BUF: _frame(60)})
    assert cpu.read(RESULT) == 0x01
    assert len(chip.rx_queue) == 3


def test_a_wedged_chip_still_answers_0x04_after_the_tries() -> None:
    """Nothing to skip and Rdy4TxNOW never asserts: the bounded poll still ends it."""
    chip = Cs8900aSim(tx_ready_budget=0)
    cpu = _jsr(chip, bp.build_tx_code(LOAD, BIG_BUF, 60, RESULT), {BIG_BUF: _frame(60)})
    assert cpu.read(RESULT) == bp.RESULT_TX_NOT_READY
    assert chip.busst_hi_reads == TRIES + bp.CS8900A_TX_READY_MAX_POLLS


# --------------------------------------------------------------------------- #
# The ping and responder builders                                              #
# --------------------------------------------------------------------------- #

PINGS = {
    "build_ping_and_wait_code": lambda: bp.build_ping_and_wait_code(
        LOAD, TX_BUF, 60, RX_BUF, RESULT, 0x1234, 1),
    "build_ping_and_wait_tod_code": lambda: bp.build_ping_and_wait_tod_code(
        LOAD, TX_BUF, 60, RX_BUF, RESULT, 0x1234, 1),
}


@pytest.mark.parametrize("name", sorted(PINGS))
def test_a_ping_through_a_starved_chip_matches_without_drain_first(name: str) -> None:
    echo = build_echo_request_frame(MAC_C64, MAC_HOST, IP_C64, IP_HOST, identifier=0x1234, sequence=1)
    chip = _ChipThatAnswersLater([_stale(i) for i in range(3)], echo.frame,
                                 _echo_reply_for(echo.frame))
    chip.tx_starved_by_rx = True
    cpu = _jsr(chip, PINGS[name](), {TX_BUF: echo.frame})
    assert chip.tx_frames[:1] == [echo.frame], f"{name}: the echo request never went out"
    assert chip.skips_at_first_tx == 3
    assert cpu.read(RESULT) == 0x01, f"{name}: reply not matched"


RESPONDERS = {
    "build_icmp_responder_code": lambda: bp.build_icmp_responder_code(LOAD, RX_BUF, IP_C64, RESULT),
    "build_icmp_responder_tod_code": lambda: bp.build_icmp_responder_tod_code(
        LOAD, RX_BUF, IP_C64, RESULT),
    "build_read_and_respond_echo_request_code": lambda: bp.build_read_and_respond_echo_request_code(
        LOAD, RX_BUF, IP_C64, RESULT),
}


@pytest.mark.parametrize("name", sorted(RESPONDERS))
def test_a_responder_replies_through_a_starved_chip(name: str) -> None:
    """The request is read, two more frames sit behind it, and the reply's bid
    is starved until they are skipped."""
    req = build_echo_request_frame(MAC_HOST, MAC_C64, IP_HOST, IP_C64, identifier=0x77, sequence=5)
    chip = Cs8900aSim(rx_queue=[req.frame, _stale(0), _stale(1)], tx_starved_by_rx=True)
    cpu = _jsr(chip, RESPONDERS[name](), {})
    assert cpu.read(RESULT) == 0x01, f"{name}: result {cpu.read(RESULT):#04x}"
    assert len(chip.tx_frames) == 1
    assert chip.tx_frames[0][:6] == MAC_HOST and chip.tx_frames[0][34] == 0   # echo reply
    assert chip.rx_queue == []
