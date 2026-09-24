"""``build_tx_code(..., drain_first=True)``: free a TX buffer starved by unread RX (issue #303).

Measured on silicon (U64E fw 3.15 ``bce4535e``, external RR-Net to en4,
2026-09-23; #303's evidence comment): frames the host puts on the link sit
unread in the CS8900a's shared buffer, and while they do ``Rdy4TxNOW`` does
not assert, so ``build_tx_code`` stores ``RESULT_TX_NOT_READY`` -- 5/6 with
three injected host frames against 0/6 with none, persistent until the RX
queue is drained or the chip is reset.  ``drain_first`` SkipNows the queue
on the 6510 before the bid, as the ping builders' ``drain_first`` does
(#222).  These run on the simulated chip with its starvation model
(``Cs8900aSim.tx_starved_by_rx``); on silicon ``drain_first`` went 6/6
against 0/6 on a starved chip (#303, 2026-09-23).
"""
from __future__ import annotations

import hashlib

import pytest

import c64_test_harness.bridge_ping as bp
from cs8900a_sim import Cpu6502, Cs8900aSim

LOAD, TX_BUF, RESULT, STATUS = 0xC000, 0xC300, 0xC0FF, 0xC0FE


def _frame(n: int) -> bytes:
    return bytes((i * 7 + 3) & 0xFF for i in range(n))


def _run(code: bytes, frame: bytes, queued: int):
    chip = Cs8900aSim(rx_queue=[_frame(64) for _ in range(queued)], tx_starved_by_rx=True)
    cpu = Cpu6502(chip)
    cpu.load(TX_BUF, frame + b"\x00")
    cpu.load(RESULT, b"\x00")
    cpu.load(STATUS, b"\xee")
    cpu.load(LOAD, code)
    cpu.jsr(LOAD, max_steps=5_000_000)
    return cpu, chip


@pytest.mark.parametrize("n", [60, 258, 1514])
def test_a_queue_deeper_than_the_skip_phase_refuses_the_bid_without_the_drain(n: int) -> None:
    """What the drain still adds since #487: the TX skip phase frees at most
    ``CS8900A_TX_SKIP_TRIES`` frames, so one more starves the bid -> 0x04."""
    queued = bp.CS8900A_TX_SKIP_TRIES + 1
    cpu, chip = _run(bp.build_tx_code(LOAD, TX_BUF, n, RESULT), _frame(n), queued=queued)
    assert cpu.read(RESULT) == bp.RESULT_TX_NOT_READY
    assert chip.tx_frames == []
    assert len(chip.rx_queue) == 1


@pytest.mark.parametrize("n", [60, 258, 1514])
@pytest.mark.parametrize("queued", [1, 3])
def test_drain_first_frees_the_buffer_and_the_frame_goes_out(n: int, queued: int) -> None:
    cpu, chip = _run(bp.build_tx_code(LOAD, TX_BUF, n, RESULT, drain_first=True),
                     _frame(n), queued=queued)
    assert cpu.read(RESULT) == 0x01
    assert chip.tx_frames == [_frame(n)]
    assert chip.rx_queue == []


@pytest.mark.parametrize("queued", [0, 3])
def test_drain_status_addr_reports_the_budget_left(queued: int) -> None:
    """Budget left = ``DRAIN_RX_MAX_FRAMES - skipped``, as on the ping builders."""
    code = bp.build_tx_code(LOAD, TX_BUF, 60, RESULT, drain_first=True,
                            drain_status_addr=STATUS)
    cpu, _ = _run(code, _frame(60), queued=queued)
    assert cpu.read(STATUS) == bp.DRAIN_RX_MAX_FRAMES - queued


def test_a_bound_hit_drain_still_bids_and_reports_zero() -> None:
    """More frames than the drain's bound and the TX skip phase's together:
    the status says so (0) and the bid is refused rather than hanging."""
    code = bp.build_tx_code(LOAD, TX_BUF, 60, RESULT, drain_first=True,
                            drain_status_addr=STATUS)
    extra = bp.CS8900A_TX_SKIP_TRIES + 1
    cpu, chip = _run(code, _frame(60), queued=bp.DRAIN_RX_MAX_FRAMES + extra)
    assert cpu.read(STATUS) == 0
    assert cpu.read(RESULT) == bp.RESULT_TX_NOT_READY
    assert len(chip.rx_queue) == 1


def test_drain_status_addr_without_drain_first_is_refused() -> None:
    with pytest.raises(ValueError, match="drain_status_addr given without drain_first"):
        bp.build_tx_code(LOAD, TX_BUF, 60, RESULT, drain_status_addr=STATUS)


def test_drain_first_composes_with_the_odd_length_opt_in() -> None:
    cpu, chip = _run(bp.build_tx_code(LOAD, TX_BUF, 61, RESULT, drain_first=True,
                                      allow_odd_frame_len=True), _frame(61), queued=2)
    assert cpu.read(RESULT) == 0x01
    assert chip.tx_frames == [_frame(61)]


#: Digests of the default routine (``drain_first`` off).  Taken before
#: ``drain_first`` existed (af0effd) and re-pinned deliberately for #487,
#: whose skip phase adds 60 bytes; with it removed the bytes are 9ec1273's
#: except one relocated JMP operand (scratch provenance487.py).
_DEFAULT_DIGESTS = {
    60: ("51053ba39fcb9e4a6962fba9976e24381a8e797763c3d151757d7d8a942c52cf", 159),
    258: ("cb3ee473c0b98c9fb525a6af33d1485cd24a0ff52826f8cc6922fbe54cef3c96", 180),
    512: ("60b6b901993f0c7335ddd66cdd38fc5fde592497d912462783600f2dcb54bb6e", 164),
    1514: ("2386815a041a6b0160a12ee59b1a6dfb0e3dfe91a48f35f59bcb70b6399459ca", 180),
}


@pytest.mark.parametrize("n", sorted(_DEFAULT_DIGESTS))
def test_the_default_emits_the_pinned_bytes(n: int) -> None:
    for code in (bp.build_tx_code(LOAD, TX_BUF, n, RESULT),
                 bp.build_tx_code(LOAD, TX_BUF, n, RESULT, drain_first=False)):
        assert (hashlib.sha256(code).hexdigest(), len(code)) == _DEFAULT_DIGESTS[n]


def test_a_part_read_frame_still_starves_the_bid() -> None:
    """A frame the 6510 has started reading but not finished still holds the
    buffer: the queue is empty, the stream is not, and BusST reports no
    Rdy4TxNOW.  Read at the register (since #487 a TX routine would SkipNow
    the part-read frame and transmit, which is not what this pins)."""
    chip = Cs8900aSim(rx_queue=[_frame(64)], tx_starved_by_rx=True)
    chip.clockport = 1
    for _ in range(6):                      # header (4) + 2 body bytes
        chip.read(0xDE09)
    assert chip.rx_queue == []
    chip.write(0xDE02, 0x38)
    chip.write(0xDE03, 0x01)                # PPPtr = BusST
    assert chip.read(0xDE05) & 0x01 == 0
