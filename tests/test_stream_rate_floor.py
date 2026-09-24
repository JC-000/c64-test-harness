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
