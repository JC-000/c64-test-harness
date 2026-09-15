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

#: Worst idle-link loss fraction measured (#410).
MEASURED_IDLE_LOSS_MAX = 0.0141
#: Best (lowest) loss fraction measured under concurrent host traffic (#410).
MEASURED_LOADED_LOSS_MIN = 0.089

#: Largest fraction of a capture's samples that may be fill.
MAX_FILL_FRACTION = 0.05

#: Attempts before a live test gives up on the link.  With the idle-link
#: figures above every capture passes the fill bound, so a retry is needed
#: only while other host traffic loads the link.  Three attempts ride out a
#: short burst of it without turning a saturated link into a long hang.
CAPTURE_ATTEMPTS = 3


def capture_usable(result) -> bool:
    """Exact time base and no more fill than :data:`MAX_FILL_FRACTION`."""
    return bool(result.time_base_intact) and result.fill_fraction <= MAX_FILL_FRACTION


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
