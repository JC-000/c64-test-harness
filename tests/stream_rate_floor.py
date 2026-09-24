"""A floor on how fast the U64 *emits* a stream, for the live stream tests (#432).

The loss bounds (``DEBUG_STREAM_LOSS_MAX``, ``audio_link_loss``) catch a
receiver miscounting sequence numbers.  They pass a device that quietly emits
less, as long as its numbers stay contiguous.  This floor catches that.

It floors the **emitted** rate, (received + dropped) per second of stream
window, not the received rate.  The sequence numbers count what the device
sent whatever the host lost, so the floor measures the device, not the
radio.  The #356 Wi-Fi loss (up to 45%, load-dependent) therefore cannot trip
it.

Measured on the U64E (fw bce4535e, 2026-09-23, 5 s unicast captures, 0
packets lost in 18/18 on wired en4 and Wi-Fi en0, n=3 per cell; #432):

* audio 247.2-249.9 packets/s against 249.69 derived (192 frames per packet
  at 2109375/44 Hz, NTSC);
* debug 2842.4-2843.3 packets/s alone, which is one 360-entry packet per
  360 cycles of the NTSC phi2 clock (1022727/360 = 2840.9).  Running
  alongside audio it read 2814.7-2815.8, because that window also spans
  the audio PUTs.

PAL is unmeasured for both streams.  If the debug stream is also one entry
per phi2 cycle, PAL gives 985248/360 = 2736.8.

Both floors are chosen at 0.8x the lower of those figures, not derived: a
halved emitter fails, and the window-measure error does not.  That error
is the PUT latency at either end plus the in-flight tail after a stop,
about 1-2% here.
"""
from __future__ import annotations

#: Audio packets/s floor: 0.8 x 249.69.
AUDIO_EMIT_PPS_MIN = 200.0
#: Debug packets/s floor: 0.8 x 2736.8, the PAL figure if one entry per phi2 cycle.
DEBUG_EMIT_PPS_MIN = 2190.0


def emitted_pps(received: int, dropped: int, window_s: float) -> float:
    """Packets per second the sequence numbers say were sent."""
    return (received + dropped) / window_s if window_s > 0 else 0.0


def emission_ok(received: int, dropped: int, window_s: float, floor: float) -> bool:
    """The live assertion's comparison."""
    return emitted_pps(received, dropped, window_s) >= floor
