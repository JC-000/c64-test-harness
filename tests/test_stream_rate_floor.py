"""Offline pin of the live stream-emission floor's comparison (#432).

Figures are the wired run of 2026-09-23 (see ``tests/stream_rate_floor.py``).
"""
from __future__ import annotations

import pytest

from stream_rate_floor import (
    AUDIO_EMIT_PPS_MIN,
    DEBUG_EMIT_PPS_MIN,
    emission_ok,
    emitted_pps,
    stream_window,
)


@pytest.mark.parametrize(
    "received, dropped, window, floor",
    [
        (1251, 0, 5.06, AUDIO_EMIT_PPS_MIN),     # wired, audio alone
        (14386, 0, 5.06, DEBUG_EMIT_PPS_MIN),    # wired, debug alone
        (14412, 0, 5.119, DEBUG_EMIT_PPS_MIN),   # debug window spanning audio PUTs
    ],
)
def test_the_measured_wired_rates_pass(received, dropped, window, floor) -> None:
    assert emission_ok(received, dropped, window, floor)


@pytest.mark.parametrize(
    "received, window, floor",
    [(1251 // 2, 5.06, AUDIO_EMIT_PPS_MIN), (14386 // 2, 5.06, DEBUG_EMIT_PPS_MIN)],
)
def test_a_halved_emitter_with_contiguous_numbers_fails(received, window, floor) -> None:
    """What the loss bound cannot see: half the packets, no sequence gaps."""
    assert not emission_ok(received, 0, window, floor)


def test_host_side_loss_does_not_lower_the_emitted_rate() -> None:
    """45% lost on the radio (#356) is still a full-rate device."""
    sent = 14386
    lost = int(sent * 0.45)
    assert emitted_pps(sent - lost, lost, 5.06) == emitted_pps(sent, 0, 5.06)
    assert emission_ok(sent - lost, lost, 5.06, DEBUG_EMIT_PPS_MIN)


class _FakeClock:
    """A monotonic clock that the fake start, sleep and stop advance."""

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_a_slow_stop_reply_does_not_stretch_the_window() -> None:
    """The stop PUT's reply latency is not stream time (#493 review).

    The bench has shown ~0.45 s replies.  Counted into a 1 s window, it
    reads a full-rate audio stream as 250 / 1.45 = 172 pps, under the floor.
    """
    clock = _FakeClock()
    calls = []

    def start():
        calls.append("start")
        clock.advance(0.2)          # a slow start reply, before the window

    def stop():
        calls.append("stop")
        clock.advance(0.45)         # the stream stopped; the reply is slow

    window = stream_window(start, stop, 1.0, clock=clock, sleep=clock.advance)
    assert calls == ["start", "stop"]
    assert window == pytest.approx(1.0)
    assert emission_ok(250, 0, window, AUDIO_EMIT_PPS_MIN)


def test_a_stop_that_raises_is_swallowed_and_the_window_kept() -> None:
    """As the live tests did before: a failed stop is not the test's failure."""
    clock = _FakeClock()

    def stop():
        raise RuntimeError("stop PUT failed")

    window = stream_window(lambda: None, stop, 1.0, clock=clock, sleep=clock.advance)
    assert window == pytest.approx(1.0)


def test_a_start_that_raises_never_stops() -> None:
    stopped = []

    def start():
        raise RuntimeError("start PUT failed")

    with pytest.raises(RuntimeError, match="start PUT failed"):
        stream_window(start, lambda: stopped.append(1), 1.0)
    assert stopped == []
