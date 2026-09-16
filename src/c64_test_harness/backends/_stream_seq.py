"""Sequence-number accounting shared by the U64 UDP stream receivers.

Private: :class:`~.u64_audio_capture.AudioCapture` and
:class:`~.u64_debug_capture.DebugCapture` both carry a 16-bit little-endian
sequence number per datagram and need the same answer to "was anything lost".

The receivers used to move their last-seen sequence number *backwards* when a
late or duplicated datagram arrived, so the next in-order datagram was
charged as a gap too: an adjacent swap read as 2 dropped, a duplicate as 1,
and a datagram *d* positions late as *d* + 1 (#430).  This tracker keeps the
highest sequence number seen, remembers which recent numbers a gap charged as
dropped and which it actually received (with a payload digest), and
classifies each datagram that is not the next expected one:

* **Forward gap** (``0 < seq - expected < 0x8000``): every skipped number is
  counted dropped.  The most recent ``window - 1`` of them are remembered.
* **Late** (``seq`` is a remembered missing number not yet arrived): one drop
  is un-counted and one reorder is counted.
* **Held** (``seq`` was actually received within the window **and** its
  payload digest is identical): one reorder; the caller sets the payload
  aside, undecided.  A true network duplicate and a counter that restarted
  over byte-identical payloads (digital silence) look the same here, so the
  *next* datagram decides:

  - it **continues the held run** (last held + 1, still behind the highest
    number, not a missing number): the stream restarted.  Resync, and the
    held payloads are **re-admitted** ahead of it, so nothing is lost.  A
    continuation that is itself identical is held too, up to
    ``MAX_HELD_DUPLICATES``; one more is a restart.
  - anything else (the stream carries on past the highest number, a late
    packet, another backward step): the held datagrams were duplicates and
    are discarded.

* **Resync** (anything else behind the highest number): one reorder, nothing
  dropped, remembered state forgotten, and ``seq`` becomes the tracker's
  position; the caller keeps the payload and counts a resync.  This covers:

  - a restarted counter whose payloads differ, including one restarting
    below numbers already received (#443 review);
  - a forward loss of 32,768..65,535 packets, which reads as a backward step;
  - **a packet at least ``window`` positions late.**  Its missing entry has
    been pruned, so it resyncs: it is appended where it arrived, the highest
    number moves back to it, and the next in-order datagram is charged a gap
    back up to where the stream really is.  That overcounts ``dropped`` by
    about the lateness, as before #430, and is the known overcount left.

**Declared residuals of the held rule** (grade: no UDP duplication has been
measured on this bench, so none of these has been seen):

- a run of more than ``MAX_HELD_DUPLICATES`` consecutive, consecutively
  numbered network duplicates is taken as a restart (resync, re-admitted);
- datagrams still held when the capture stops are discarded as duplicates
  (:meth:`SequenceTracker.flush_held`), so a silent restart in the last
  ``MAX_HELD_DUPLICATES`` packets of a capture loses those packets
  (counted in :attr:`SequenceTracker.discarded`);
- a duplicate run immediately followed by the next number *after the run*
  that is still behind the highest reads as a restart (re-sent 48, 49 and
  then a re-sent 50 with different bytes);
- **a silent restart can lose the datagrams held for it and still report
  the time base intact** -- but only when all three of these hold:

  1. the restart's payloads are byte-identical to the old stream's
     (digital silence), so the digest cannot tell a restart from a
     duplicate and the run is held;
  2. a number the *old* stream lost falls inside that held run.  The
     triggering set is **exactly 1..``max_held``, inclusive at the top**
     (#451): ``max_held`` itself still triggers it, ``max_held`` + 1 does
     not, and 0 never does, because nothing precedes the first number the
     tracker observes so 0 is never a tracked missing number;
  3. that number is still inside the window, so it is remembered as an
     unarrived missing number.

  The held run then reaches a number the stream is still owed, which does
  not read as a continuation: the held datagrams are taken as duplicates
  and discarded, and the restart's datagram fills the old stream's missing
  slot instead.  An old stream 0..500 that never received 5, followed by a
  restart 0..599, keeps **1,095 of the 1,100 datagrams sent** with
  ``dropped`` 0, one resync, and -- for audio -- ``time_base_intact``
  ``True``.  A silent restart with no such lost number keeps every
  datagram, so this is not "silent restarts lose packets".  Declared and
  accepted rather than fixed: it is the same ambiguity as the residual
  above (a re-sent 48, 49 then a late 50 is indistinguishable from a
  restart), and no duplication or restart has been measured on the device.

  **The loss is countable**: those discarded payloads are in
  :attr:`SequenceTracker.discarded`, surfaced as ``payloads_discarded`` on
  both ``CaptureResult`` and ``DebugCaptureResult``, and
  ``packets_received`` == delivered + ``payloads_discarded`` closes the
  books.  Without it a capture missing a fifth of its audio reads clean in
  every field a caller can assert on -- ``packets_dropped`` 0,
  ``sequence_resyncs`` 0, ``time_base_intact`` ``True`` -- with only
  ``packets_reordered`` and two WARNING lines as evidence (#443 round 3,
  reviewer-8: 52 datagrams sent, 52 received, 41 in the WAV, 11
  discarded).  The flag is deliberately *not* moved: a true duplicate's
  payload is a correct discard, so a non-zero count is not by itself a
  fault.  It is the caller's to assert on.

  **How reachable any of this is on hardware is unestablished** (#452):
  the sequence number is generated FPGA-side and nobody has established
  whether starting a stream resets it, so a mid-capture restart may be
  routine or may be unreachable.  That question grades this residual.

  More generally, **a restart whose first number is an unarrived missing
  number inside the window places that datagram in the old stream's slot
  and un-counts the old drop.**

Evidence grade for every residual above: **offline loopback through the
real receivers, n=1 per case, no device** (#443 review round 2).

Memory: the missing and received books each hold at most ``window`` entries,
pruned by age as the highest number advances; at most
``MAX_HELD_DUPLICATES`` payloads are held.
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: Datagrams of reordering the tracker can repair, and how far back it
#: remembers what it received.  **No measured basis:** no reorder or
#: duplicate has been observed on this bench (#410, #430: zero backward steps
#: in 28 captures), so there is no lateness distribution to size it from.  It
#: bounds memory, and lateness at ~4 s of audio (250 packets/s) or ~0.4 s of
#: debug stream (~2,400 packets/s).
SEQ_REORDER_WINDOW = 1024

#: Digest-identical datagrams held at once before a continuing run is taken
#: as a restart.  Chosen, not measured: no duplicate has been seen at all.
#: 8 audio packets are 32 ms; 8 debug packets ~3 ms.
MAX_HELD_DUPLICATES = 8

NEXT = "next"
GAP = "gap"
LATE = "late"
HELD = "held"
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
    #: The next sequence number the tracker expected.
    expected: int | None = None
    #: RESYNC: tokens of held datagrams to keep, in arrival order, *before*
    #: this datagram.
    readmit: tuple = ()
    #: Held datagrams this event decided were duplicates (discard them).
    discarded_held: int = 0


@dataclass(frozen=True)
class _Arrived:
    """A missing entry whose packet has since arrived late."""

    digest: object


@dataclass
class SequenceTracker:
    """Counts true loss, reorders and resyncs for a 16-bit sequence stream."""

    window: int = SEQ_REORDER_WINDOW
    max_held: int = MAX_HELD_DUPLICATES
    dropped: int = 0
    #: Datagrams that arrived behind the highest number seen (late, held or
    #: resync) -- one per datagram, not one per backward step.
    reordered: int = 0
    resyncs: int = 0
    #: Datagrams whose payload was discarded rather than delivered: held
    #: runs decided to be duplicates, both mid-stream and at ``stop()``.
    #: A true duplicate's payload is a correct discard, but a silent
    #: restart's is a genuine loss the other counters cannot show (#443
    #: round 3) -- see the fourth declared residual in the module docstring.
    discarded: int = 0
    highest: int | None = None
    _missing: dict[int, object] = field(default_factory=dict)
    _received: dict[int, object] = field(default_factory=dict)
    _held: list[tuple[int, object, object]] = field(default_factory=list)

    def bind(self, seq: int, slot: object) -> None:
        """Attach *slot* to a remembered missing number (returned when LATE)."""
        if seq in self._missing and not isinstance(self._missing[seq], _Arrived):
            self._missing[seq] = slot

    def flush_held(self) -> int:
        """Decide every held datagram is a duplicate; returns how many.

        This is the **only** place a received datagram's payload is dropped
        on the floor, from either caller -- :meth:`observe` when a held run
        turns out not to continue, and the receivers' ``stop()`` for
        whatever is still held.  It counts them in :attr:`discarded` so the
        loss is assertable rather than only logged (#443 round 3).
        """
        n = len(self._held)
        self._held.clear()
        self.discarded += n
        return n

    def _age(self, seq: int) -> int:
        assert self.highest is not None
        return (self.highest - seq) & 0xFFFF

    def _behind(self, seq: int) -> bool:
        assert self.highest is not None
        return ((seq - self.highest - 1) & 0xFFFF) >= 0x8000

    def _unarrived_missing(self, seq: int) -> bool:
        return (
            seq in self._missing
            and not isinstance(self._missing[seq], _Arrived)
            and self._age(seq) < self.window
        )

    def _seen_identical(self, seq: int, digest: object) -> bool:
        if self._age(seq) >= self.window:
            return False
        entry = self._missing.get(seq)
        if isinstance(entry, _Arrived):
            return entry.digest == digest
        return seq in self._received and self._received[seq] == digest

    def _advance(self, seq: int, digest: object) -> None:
        self.highest = seq
        self._received[seq] = digest
        self._prune()

    def observe(self, seq: int, digest: object = None, token: object = None) -> SeqEvent:
        """Classify one datagram.

        :param digest: An equality-comparable fingerprint of the payload
            (the receivers pass ``zlib.crc32``).
        :param token: What to hand back in ``readmit`` if this datagram is
            held and later re-admitted (the receivers pass the payload).
        """
        seq &= 0xFFFF
        if self.highest is None:
            self._advance(seq, digest)
            return SeqEvent(NEXT)

        discarded = 0
        if self._held:
            last = self._held[-1][0]
            if (
                seq == (last + 1) & 0xFFFF
                and self._behind(seq)
                and not self._unarrived_missing(seq)
            ):
                self.reordered += 1
                if self._seen_identical(seq, digest) and len(self._held) < self.max_held:
                    self._held.append((seq, digest, token))
                    return SeqEvent(HELD)
                return self._restart(seq, digest)
            discarded = self.flush_held()

        ev = self._classify(seq, digest, token)
        ev.discarded_held = discarded
        return ev

    def _classify(self, seq: int, digest: object, token: object) -> SeqEvent:
        assert self.highest is not None
        expected = (self.highest + 1) & 0xFFFF
        if seq == expected:
            self._advance(seq, digest)
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
            self._advance(seq, digest)
            return SeqEvent(
                GAP, tracked_missing=tracked, untracked=gap - n_tracked,
                expected=expected,
            )

        self.reordered += 1
        if self._unarrived_missing(seq):
            self.dropped -= 1
            slot = self._missing[seq]
            self._missing[seq] = _Arrived(digest)
            return SeqEvent(LATE, slot=slot, expected=expected)
        if self._seen_identical(seq, digest):
            self._held.append((seq, digest, token))
            return SeqEvent(HELD, expected=expected)
        self.resyncs += 1
        self._missing.clear()
        self._received.clear()
        self._advance(seq, digest)
        return SeqEvent(RESYNC, expected=expected)

    def _restart(self, seq: int, digest: object) -> SeqEvent:
        """The held run continued: a restarted counter.  Re-admit it."""
        assert self.highest is not None
        expected = (self.highest + 1) & 0xFFFF
        held, self._held = self._held, []
        self.resyncs += 1
        self._missing.clear()
        self._received.clear()
        for s, d, _ in held:
            self._received[s] = d
        self._advance(seq, digest)
        return SeqEvent(
            RESYNC, expected=expected, readmit=tuple(t for _, _, t in held)
        )

    def _prune(self) -> None:
        for book in (self._missing, self._received):
            while book:
                oldest = next(iter(book))
                if self._age(oldest) < self.window:
                    break
                del book[oldest]
