"""A floor on how fast the U64 *emits* a stream, for the live stream tests (#432).

The loss bounds (``DEBUG_STREAM_LOSS_MAX``, ``audio_link_loss``) catch a
receiver miscounting sequence numbers.  They pass a device that quietly emits
less, as long as its numbers stay contiguous.  This floor catches that.

It floors the **emitted** rate, (received + dropped) per second of stream
window, not the received rate.  The sequence numbers count what the device
*numbered*, whatever the host lost, so the #356 Wi-Fi loss (up to 45%,
load-dependent) cannot trip it.  **Residual, fail-open:** the floor counts
numbers, not packets on the wire.  A device that numbers packets and then
discards them, or whose counter jumps forward, reads as full rate here.
That shows as ``packets_dropped`` instead, which the loss bound backstops.

The window is the sleep between the start PUT's return and the stop call
(:func:`stream_window`).  Neither PUT's reply latency is in the
denominator; the bench has shown ~0.45 s replies, which would read a
full-rate 1 s audio stream as ~172 pps (#493 review).

Measured on the U64E (fw bce4535e, 2026-09-23, 5 s unicast captures, 0
packets lost in 18/18 on wired en4 and Wi-Fi en0, n=3 per cell; #432):

* audio 247.2-249.9 packets/s against 249.69 derived (192 frames per packet
  at 2109375/44 Hz, NTSC);
* debug 2842.4-2843.3 packets/s alone.  That is consistent with one entry
  per NTSC phi2 cycle (a rate match, 360 entries per packet, n=3; Debug
  Stream Mode presumed at its default 6510 Only, not recorded):
  1022727/360 = 2840.9.  Running alongside audio it read 2814.7-2815.8,
  because that window also spans the audio PUTs.

PAL is unmeasured for both streams.  For debug only: if the stream is also
one entry per phi2 cycle on PAL, it gives 985248/360 = 2736.8.

Both floors are chosen at 0.8x the lower figure for their stream, not
derived: audio 0.8x 249.69, debug 0.8x the PAL 2736.8.  A halved emitter
fails, and the window-measure error does not.  That error is chiefly the
in-flight tail after a stop, about 1-2% here.
"""
from __future__ import annotations

import time
from typing import Callable

#: Audio packets/s floor: 0.8 x 249.69.
AUDIO_EMIT_PPS_MIN = 200.0
#: Debug packets/s floor: 0.8 x 2736.8, the PAL figure if the stream is one
#: entry per phi2 cycle there too (unmeasured).
DEBUG_EMIT_PPS_MIN = 2190.0


def emitted_pps(received: int, dropped: int, window_s: float) -> float:
    """Packets per second the sequence numbers say were sent."""
    return (received + dropped) / window_s if window_s > 0 else 0.0


def emission_ok(received: int, dropped: int, window_s: float, floor: float) -> bool:
    """The live assertion's comparison."""
    return emitted_pps(received, dropped, window_s) >= floor


def stream_window(
    start: Callable[[], object],
    stop: Callable[[], object],
    seconds: float,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], object] = time.sleep,
) -> float:
    """Run a stream for *seconds*; returns the window to divide by.

    The window runs from ``start()``'s return to just **before** ``stop()`` is
    called, so the stop PUT's reply latency is not counted as stream time.
    ``stop()`` runs whenever ``start()`` succeeded.  An exception from it is
    swallowed, as the live tests always did: a failed stop is not the
    capture's failure.  If ``start()`` raises, nothing is stopped.
    """
    start()
    window_start = clock()
    try:
        sleep(seconds)
    finally:
        window_end = clock()
        try:
            stop()
        except Exception:
            pass
    return window_end - window_start
