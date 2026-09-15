"""How much gap-filled audio a live test may analyse, and how often it retries (#410).

``AudioCapture`` zero-fills every lost packet since #410, so a lossy capture
keeps an exact time base.  What a test still has to decide is how much
silence it will analyse.  Measured on the U64E (fw 3.15 bce4535e, 2026-09-15,
host on Wi-Fi en0, 5.5 s unicast audio captures; #410 comments):

* **Idle link:** loss fraction 0 to 0.0141 per capture.  That is 24 captures
  across three paired runs: 0-20 of ~1,380 packets, and 0.0137 / 0.0094 in
  the research arms.
* **Concurrent host traffic** that never touches the device: 0.089 to 0.174
  (arm C, n=6).  With the debug stream also running: 0.117 to 0.261 (arm B,
  n=8).  The DeviceLock does not isolate other lanes' host traffic.

:data:`MAX_FILL_FRACTION` sits between the two populations.  It is 3.5x the
worst idle capture, so an idle link passes on the first attempt, and 0.56x
the best loaded capture, so a capture taken while someone else saturates the
link is retried, not analysed.  The edge is chosen inside that band, not
derived.  ``tests/test_audio_gap_fill.py`` pins it against both figures.
"""
from __future__ import annotations

from c64_test_harness.backends.u64_audio_capture import AUDIO_FRAMES_PER_PACKET

#: Worst idle-link loss fraction measured (#410).
MEASURED_IDLE_LOSS_MAX = 0.0141
#: Best (lowest) loss fraction measured under concurrent host traffic (#410).
MEASURED_LOADED_LOSS_MIN = 0.089

#: Largest fraction of a capture's samples that may be fill.
MAX_FILL_FRACTION = 0.05

#: Largest share of the *stream* a capture may be missing outright (#443).
#: Discarded payloads are packets of stream time that never reached the WAV
#: (see :func:`lost_time_fraction`).  Not a zero test: a genuine
#: retransmission is discarded correctly, and a run must not fail on one.
#: Same 0.05 as the fill bound and for the same reason -- both bound how
#: much of an analysed window is not where the caller thinks it is -- but a
#: separate constant, because the two are different quantities and the
#: measurements behind them are not the same.
MAX_LOST_TIME_FRACTION = 0.05

#: Attempts before a live test gives up on the link.  With the idle-link
#: figures above every capture passes the fill bound, so a retry is needed
#: only while other host traffic loads the link.  Three attempts ride out a
#: short burst of it without turning a saturated link into a long hang.
CAPTURE_ATTEMPTS = 3


def payloads_discarded(result) -> int:
    """Datagrams whose PCM was discarded rather than delivered (#443).

    Two paths reach it, and **neither shows in any fill field** -- a
    restarted counter over a number that was itself lost (52 datagrams in,
    41 packets in the WAV, 11 discarded) and a tail of duplicate-looking
    datagrams still held when the capture stops (106 in, 100 in the WAV, 6
    discarded).  Both leave ``packets_dropped``, ``packets_filled`` and
    ``fill_fraction`` at 0, ``filled_frame_ranges`` at ``()``,
    ``sequence_resyncs`` at 0 and ``time_base_intact`` True.

    ``getattr`` with 0 so this module reads the same before and after #443
    lands the field: a result without it reads as "none discarded", which
    is the old meaning.
    """
    return int(getattr(result, "payloads_discarded", 0) or 0)


def lost_time_fraction(result) -> float:
    """Upper bound on the share of stream time missing from the WAV (#443).

    The discarded bytes are byte-identical to PCM already in the file, so
    the harm is **duration, not content**: what is gone is the stream time
    those datagrams carried.  The fraction is that time over the stream the
    capture should have been -- what is in the file plus what was discarded.

    An upper bound, not a measurement: a discard is only *provably* lost
    time when it was a restart rather than a true duplicate, and the
    receiver cannot tell those apart (that is the whole residual).
    """
    discarded = payloads_discarded(result)
    if not discarded:
        return 0.0
    lost_frames = discarded * AUDIO_FRAMES_PER_PACKET
    stream_frames = result.total_samples + lost_frames
    return lost_frames / stream_frames if stream_frames else 0.0


def capture_usable(result) -> bool:
    """Exact time base, fill within bound, and lost time within bound.

    Lost time is **bounded, not forbidden**.  A genuine retransmission is
    discarded correctly -- 101 datagrams received, 100 packets in the WAV,
    1 discarded, everything else clean -- and gating on zero discards would
    fail such a run rarely and inexplicably, on a bench that has never
    recorded a duplicate at all.

    Bounding discards *only when* ``packets_reordered`` is zero was the
    other candidate and is rejected: every held datagram counts a reorder
    (``_stream_seq`` increments it before the held/late/resync split), so
    the reorder count is nonzero in the duplicate case **and** in the
    restart case it is meant to catch.  It would exempt exactly the stream
    that needs catching.  Lost time separates them on size instead: 1 of
    101 is 1.0%, 11 of 52 is 21%.
    """
    return (
        bool(result.time_base_intact)
        and result.fill_fraction <= MAX_FILL_FRACTION
        and lost_time_fraction(result) <= MAX_LOST_TIME_FRACTION
    )


def fill_near(result, frame: int, guard: int) -> bool:
    """Whether any filled range lies within *guard* frames of *frame*.

    A zero-filled range starts and ends with a step, which an edge detector
    can take for a real edge.  A fill that covers a real edge moves the
    detected edge to the fill's boundary.  Either way the detected edge is
    within the range, so a test rejects a capture where this is True.
    """
    return any(
        start - guard <= frame < start + count + guard
        for start, count in result.filled_frame_ranges
    )
