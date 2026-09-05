"""Self-tests of the CS8900a simulator used by ``test_cs8900a_arp.py``.

An oracle that cannot fail is not an oracle.  These pin what
``tests/cs8900a_sim.py`` can and cannot distinguish, so a test written
against it knows what it is proving:

* the FIFO hands out the RxStatus / RxLength header **as bytes in the
  documented order** (high half then low half), so the harness reader --
  which reads high-half-first, issue #210 -- observes the right words,
  and a reader that reads low-half-first observes byte-swapped ones.
  The adversarial review of #218 found the first version of the
  simulator returned the half the register asked for regardless of
  order, so the low-first mutation passed every simulated test.
* the body is handed out in wire order for a low-half-first reader.

What the simulator still does **not** model is listed in its module
docstring; nothing here should be read as evidence about those.
"""

from __future__ import annotations

from c64_test_harness.bridge_ping import (
    RTDATA_HI,
    RTDATA_LO,
    Asm,
    _emit_clockport_enable,
    _emit_read_frame,
    _FIXED_RX_BYTES,
)
from cs8900a_sim import run_routine

LOAD, RX_BUF, RESULT = 0xC000, 0xC300, 0xC0FF
FRAME = bytes(range(0x10, 0x10 + 60))          # a 60-byte ramp, no repeated bytes


def _reader(header_high_first: bool) -> bytes:
    """A drain-one-frame routine whose header order is the parameter.

    ``True`` is the harness's ``_emit_read_frame`` verbatim; ``False`` is
    the pre-#210 order rebuilt here so the oracle's verdict on it can be
    pinned without mutating the source.
    """
    a = Asm(org=LOAD)
    a.emit(0x78)
    _emit_clockport_enable(a)
    if header_high_first:
        _emit_read_frame(a, RX_BUF)
    else:
        for _ in range(2):
            a.emit(0xAD, RTDATA_LO & 0xFF, RTDATA_LO >> 8)
            a.emit(0xAD, RTDATA_HI & 0xFF, RTDATA_HI >> 8)
        a.emit(0xA9, RX_BUF & 0xFF, 0x85, 0xFB)
        a.emit(0xA9, (RX_BUF >> 8) & 0xFF, 0x85, 0xFC)
        a.emit(0xA0, 0x00)
        a.label("lp")
        a.emit(0xAD, RTDATA_LO & 0xFF, RTDATA_LO >> 8)
        a.emit(0x91, 0xFB)
        a.emit(0xC8)
        a.emit(0xAD, RTDATA_HI & 0xFF, RTDATA_HI >> 8)
        a.emit(0x91, 0xFB)
        a.emit(0xC8)
        a.emit(0xC0, _FIXED_RX_BYTES)
        a.branch(0xD0, "lp")
    a.emit(0xA9, 0x01, 0x8D, RESULT & 0xFF, RESULT >> 8)
    a.emit(0x58)
    a.emit(0x60)
    return a.build()


def _header_words_as_read(chip) -> tuple[int, int]:
    """(RxStatus, RxLength) assembled the way the routine labelled its reads.

    The first four RTDATA reads are the header; whichever register each
    read came from decides which half of the word the routine took it for.
    """
    reads = chip.rtdata_reads[:4]
    words = []
    for pair in (reads[0:2], reads[2:4]):
        hi = next(v for reg, v in pair if reg == RTDATA_HI)
        lo = next(v for reg, v in pair if reg == RTDATA_LO)
        words.append((hi << 8) | lo)
    return words[0], words[1]


def test_harness_reader_observes_rxstatus_and_rxlength_correctly() -> None:
    """High-half-first (the #210 order): RxStatus=RxOK, RxLength=len(frame)."""
    cpu, chip = run_routine(_reader(header_high_first=True), LOAD, rx_frames=[FRAME])
    assert cpu.mem[RESULT] == 0x01
    status, length = _header_words_as_read(chip)
    assert status == 0x0100, f"RxStatus read as {status:#06x}, expected RxOK 0x0100"
    assert length == len(FRAME), f"RxLength read as {length}, expected {len(FRAME)}"
    assert bytes(cpu.mem[RX_BUF:RX_BUF + 60]) == FRAME, "body must land in wire order"


def test_low_first_header_reader_observes_byte_swapped_words() -> None:
    """The oracle can tell the pre-#210 order apart: its RxLength is wrong.

    This is the test the adversarial review asked for: with the earlier
    simulator both readers saw RxLength=60 and the low-first mutation of
    ``_emit_read_frame`` survived.  Note what is and is not claimed -- the
    header words come out byte-swapped (0x3C00 for 60), which is enough to
    fail a reader that trusts RxLength; whether the *body* also shifts on
    silicon (the #210 report says it does) is not something this model
    reproduces, and ``test_cs8900a_frame_reader.py`` remains the pin for
    the order itself.
    """
    cpu, chip = run_routine(_reader(header_high_first=False), LOAD, rx_frames=[FRAME])
    status, length = _header_words_as_read(chip)
    assert length != len(FRAME), (
        "simulator cannot distinguish header read order: the low-first reader saw "
        f"RxLength={length} -- the oracle is blind to the #210 regression again"
    )
    assert (status, length) == (0x0001, 0x3C00), (status, length)


def test_body_reads_pop_bytes_in_wire_order_low_half_first() -> None:
    cpu, chip = run_routine(_reader(header_high_first=True), LOAD, rx_frames=[FRAME])
    body = [(reg, v) for reg, v in chip.rtdata_reads[4:4 + 60]]
    assert [reg for reg, _ in body] == [RTDATA_LO, RTDATA_HI] * 30
    assert bytes(v for _, v in body) == FRAME


def test_reads_past_the_frame_return_zero_until_skipnow() -> None:
    """Measured on hardware (#210): every read past RxLength bytes is $00."""
    cpu, chip = run_routine(_reader(header_high_first=True), LOAD, rx_frames=[FRAME[:40]])
    tail = [v for _, v in chip.rtdata_reads[4 + 40:4 + 60]]
    assert tail == [0] * 20
