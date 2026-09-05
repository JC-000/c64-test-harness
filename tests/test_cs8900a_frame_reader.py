"""Structural pins on the CS8900a RX frame reader (issues #208, #210).

These are unit tests on purpose.  Both bugs they guard are invisible to
the two-VICE bridge suite:

* **#210 (RTDATA half ordering).**  VICE's CS8900a emulation tolerates
  either order, so ``tests/test_bridge_ping.py`` passes with the reader
  correct *or* broken.  Real silicon does not: reading ``$DE08`` before
  ``$DE09`` desynchronises the FIFO by one byte, RxLength comes back
  garbage and every data word arrives byte-swapped.  Only hardware can
  fail on that, and hardware is not in the default suite -- so the order
  is pinned here instead.

* **#208 (zero-page collision).**  The reader used to park RxStatus in
  ``$F1:$F2`` and RxLength in ``$F3:$F4``, which is where the TOD poll
  loop keeps its deadline (``$F2``/``$F3``) and ones table (``$F4``).
  Every *dropped* frame therefore corrupted the deadline of the routine
  that read it.  The bridge tests almost never take the drop path,
  because the only traffic on the bridge is the frame under test.

Reference for both: ip65's ``drivers/cs8900a.s``, which reads the two
header words high-half-first and the data body low-half-first.
"""

from __future__ import annotations

from c64_test_harness.bridge_ping import (
    RTDATA_HI,
    RTDATA_LO,
    Asm,
    _emit_read_frame,
    _FIXED_RX_BYTES,
)

LDA_ABS = 0xAD
STA_ZP = 0x85

READ_RTDATA_HI = bytes([LDA_ABS, RTDATA_HI & 0xFF, RTDATA_HI >> 8])
READ_RTDATA_LO = bytes([LDA_ABS, RTDATA_LO & 0xFF, RTDATA_LO >> 8])

RX_BUF = 0xC300


def _reader_bytes() -> bytes:
    a = Asm(org=0xC000)
    _emit_read_frame(a, RX_BUF)
    return a.build()


def test_header_reads_high_half_before_low_half() -> None:
    """The four header reads are HI, LO, HI, LO -- not LO, HI, LO, HI.

    This is the whole of #210.  Reading the low half first desynchronises
    a real CS8900a's FIFO by one byte; ip65's driver reads the RxStatus
    and RxLength words high-half-first for exactly this reason.
    """
    code = _reader_bytes()
    expected = (READ_RTDATA_HI + READ_RTDATA_LO) * 2
    assert code.startswith(expected), (
        "frame reader must open with HI,LO,HI,LO reads of RTDATA "
        f"({expected.hex()}); got {code[:len(expected)].hex()}"
    )


def test_header_is_not_the_pre_210_low_first_order() -> None:
    """Explicitly reject the exact byte sequence the bug had.

    Belt and braces against a well-meaning 'tidy-up' that swaps the pair
    back: the old order reads correctly under VICE, so nothing else in
    the default suite would notice.
    """
    code = _reader_bytes()
    broken = (READ_RTDATA_LO + READ_RTDATA_HI) * 2
    assert not code.startswith(broken), (
        "frame reader has regressed to the pre-#210 low-half-first header "
        "order; this passes under VICE and fails on real silicon"
    )


def test_reader_does_not_touch_the_tod_zero_page_slots() -> None:
    """No ``STA $F1``..``STA $F4`` anywhere in the reader (issue #208).

    ``$F2``/``$F3`` hold the TOD deadline and ``$F4`` the ones table, so a
    store here silently breaks the timeout of any routine that reads a
    frame and keeps polling.
    """
    code = _reader_bytes()
    for zp in (0xF1, 0xF2, 0xF3, 0xF4):
        assert bytes([STA_ZP, zp]) not in code, (
            f"frame reader stores to ${zp:02X}, which the TOD poll loop owns "
            "(issue #208)"
        )


def test_body_preserves_wire_order() -> None:
    """Data words are read low-half-first and stored in wire order.

    ip65 reads the body low half first (the opposite of the header) and
    stores low byte then high byte, so ``rx_buf`` ends up holding the
    frame exactly as it appeared on the wire -- which is what every
    offset-based check in this module then relies on.
    """
    code = _reader_bytes()
    body = bytes([
        LDA_ABS, RTDATA_LO & 0xFF, RTDATA_LO >> 8,
        0x91, 0xFB,          # STA (ptr),Y
        0xC8,                # INY
        LDA_ABS, RTDATA_HI & 0xFF, RTDATA_HI >> 8,
        0x91, 0xFB,
        0xC8,
    ])
    assert body in code, "data body must read LO then HI and store both in order"


def test_body_drains_a_whole_minimum_frame() -> None:
    """The fixed-length body read covers a minimum ethernet frame.

    Everything downstream indexes into ``rx_buf`` at fixed offsets (the
    ICMP identifier sits at 38-39), so the drain has to reach at least
    the 60-byte minimum frame.
    """
    assert _FIXED_RX_BYTES >= 42, "must cover ARP; ICMP checks reach offset 41"
    code = _reader_bytes()
    assert bytes([0xC0, _FIXED_RX_BYTES]) in code, "CPY #_FIXED_RX_BYTES missing"
