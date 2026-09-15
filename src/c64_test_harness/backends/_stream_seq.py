"""Sequence-number accounting shared by the U64 UDP stream receivers.

Private: :class:`~.u64_audio_capture.AudioCapture` and
:class:`~.u64_debug_capture.DebugCapture` both carry a 16-bit little-endian
sequence number per datagram and need the same answer to "was anything lost".

The receivers used to move their last-seen sequence number *backwards* when a
late or duplicated datagram arrived, so the next in-order datagram was
charged as a gap too: an adjacent swap read as 2 dropped, a duplicate as 1,
and a datagram *d* positions late as *d* + 1 (#430).  This tracker keeps the
highest sequence number seen, remembers which recent numbers a gap charged as
dropped, and un-charges one when it turns up late.

Rules, with ``d = (highest - seq) & 0xFFFF`` for a datagram that is not the
next expected one:

* **Forward gap** (``0 < seq - expected < 0x8000``): every skipped number is
  counted dropped.  The most recent ``window - 1`` of them are remembered.
* **Late** (``seq`` is a remembered missing number): one drop is un-counted
  and one backward event is counted.
* **Duplicate** (otherwise, ``d < window``): nothing dropped, one backward
  event; the caller should discard the payload.  A datagram later than the
  window whose drop was already charged also lands here -- its drop stays
  counted, which is the only overcount left and needs ``window`` datagrams of
  lateness.
* **Resync** (``d >= window``): the stream's counter restarted, or a packet
  arrived absurdly late.  Treated as the new position: one backward event,
  nothing dropped, remembered gaps forgotten, payload kept.  Without this a
  restarted counter would have every later datagram discarded as a duplicate.
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: Datagrams of reordering the tracker can repair.  The longest loss burst
#: measured on the U64E was 313 debug packets (#356) and no reorder has been
#: seen at all (#410, #430), so 1024 covers both with room: ~4 s of audio
#: (250 packets/s), ~0.4 s of debug stream (~2,400 packets/s).
SEQ_REORDER_WINDOW = 1024

NEXT = "next"
GAP = "gap"
LATE = "late"
DUPLICATE = "duplicate"
RESYNC = "resync"


@dataclass
class SeqEvent:
    """What one datagram's sequence number meant."""

    kind: str
    #: GAP: the skipped numbers the tracker remembers, oldest first.
    tracked_missing: tuple[int, ...] = ()
    #: GAP: skipped numbers too old to remember (counted, not tracked).
    untracked: int = 0
    #: LATE: the value :meth:`SequenceTracker.bind` attached to this number.
    slot: object = None
    #: The step the receiver logs: expected sequence number.
    expected: int | None = None


@dataclass
class SequenceTracker:
    """Counts true loss and backward events for a 16-bit sequence stream."""

    window: int = SEQ_REORDER_WINDOW
    dropped: int = 0
    reordered: int = 0
    highest: int | None = None
    _missing: dict[int, object] = field(default_factory=dict)

    def bind(self, seq: int, slot: object) -> None:
        """Attach *slot* to a remembered missing number (returned when LATE)."""
        if seq in self._missing:
            self._missing[seq] = slot

    def observe(self, seq: int) -> SeqEvent:
        seq &= 0xFFFF
        if self.highest is None:
            self.highest = seq
            return SeqEvent(NEXT)
        expected = (self.highest + 1) & 0xFFFF
        if seq == expected:
            self.highest = seq
            self._prune()
            return SeqEvent(NEXT)
        gap = (seq - expected) & 0xFFFF
        if gap < 0x8000:
            self.dropped += gap
            n_tracked = min(gap, self.window - 1)
            tracked = tuple(
                (seq - i) & 0xFFFF for i in range(n_tracked, 0, -1)
            )
            for s in tracked:
                self._missing[s] = None
            self.highest = seq
            self._prune()
            return SeqEvent(
                GAP, tracked_missing=tracked, untracked=gap - n_tracked,
                expected=expected,
            )
        self.reordered += 1
        if seq in self._missing:
            self.dropped -= 1
            return SeqEvent(LATE, slot=self._missing.pop(seq), expected=expected)
        if (self.highest - seq) & 0xFFFF < self.window:
            return SeqEvent(DUPLICATE, expected=expected)
        self.highest = seq
        self._missing.clear()
        return SeqEvent(RESYNC, expected=expected)

    def _prune(self) -> None:
        assert self.highest is not None
        while self._missing:
            oldest = next(iter(self._missing))
            if (self.highest - oldest) & 0xFFFF < self.window:
                break
            del self._missing[oldest]
