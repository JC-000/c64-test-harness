"""The BA acceptance band in the live stream test must admit a real C64.

``test_u64_streams_live.py`` asserted BA high on >= 99% of *CPU* cycles.
``BusCycle.is_cpu`` is PHI2-high (``u64_debug_capture.py``:
``is_cpu`` -> ``phi2``), so the denominator is every PHI2 cycle of the
capture -- and the VIC holds BA low through the badline fetch on 25
lines of every frame.  99% is therefore not a firmware guarantee the
device failed to meet; it is arithmetically unreachable on a machine
with badlines enabled.  The observed 93.747% is the NTSC 6567R8 figure
(issue #273 class 3a).

This module derives the figure from first principles for the three
plausible machines and checks the band in the live module admits all of
them, while still rejecting the mis-wirings the assertion exists to
catch.  It needs no device: the arithmetic is the thing under test.

Badline mechanics (Bauer, *The MOS 6567/6569 video controller*, § 3.5):
on a badline the VIC pulls BA low three cycles before the first c-access
and holds it low for the 40 c-accesses, so BA is low for 43 cycles.
There are 25 badlines per frame -- one per text row -- when DEN is set
and the display is not blanked.  Sprites pull BA low too (two cycles per
active sprite per line); a BASIC screen has none, which is why the
measured value sits at the no-sprite bound and why the lower edge of the
band is well below it.
"""
from __future__ import annotations

import pytest

from test_u64_streams_live import BA_HIGH_MAX, BA_HIGH_MIN

#: Cycles BA is held low per badline: 3 lead-in + 40 c-accesses.
BADLINE_BA_LOW_CYCLES = 3 + 40
#: One badline per text row.
BADLINES_PER_FRAME = 25

#: ``(name, raster lines, cycles per line)``
MACHINES = [
    ("NTSC 6567R8", 263, 65),
    ("NTSC 6567R56A", 262, 64),
    ("PAL 6569", 312, 63),
]

#: What the U64E actually reported when the >= 0.99 assertion fired.
MEASURED = 0.93747


def ba_high_fraction(lines: int, cycles_per_line: int) -> float:
    """Fraction of PHI2 cycles per frame on which BA is high, no sprites."""
    total = lines * cycles_per_line
    low = BADLINES_PER_FRAME * BADLINE_BA_LOW_CYCLES
    return (total - low) / total


class TestTheArithmetic:
    @pytest.mark.parametrize("name,lines,cycles", MACHINES, ids=lambda v: str(v))
    def test_no_machine_can_reach_99_percent(
        self, name: str, lines: int, cycles: int
    ) -> None:
        """The premise: 0.99 is unreachable, so it was never a guarantee."""
        assert ba_high_fraction(lines, cycles) < 0.99, name

    def test_ntsc_6567r8_matches_what_the_device_reported(self) -> None:
        """Which is why the device was right and the assertion was wrong."""
        predicted = ba_high_fraction(263, 65)
        assert abs(predicted - MEASURED) < 0.001, (
            f"NTSC 6567R8 predicts {predicted:.5f}, device measured "
            f"{MEASURED:.5f}"
        )

    def test_pal_does_not_fit_the_measurement(self) -> None:
        """A control on the identification, not decoration.

        If PAL fitted too, 'the measurement is the badline figure' would
        be a much weaker claim.
        """
        assert abs(ba_high_fraction(312, 63) - MEASURED) > 0.005


#: How far below the lowest badline prediction the band may reach.  A
#: real capture can sit under the no-sprite figure -- sprite DMA pulls
#: BA low for 3 + 2 per active sprite per line -- but not arbitrarily
#: far, and a floor that reaches down into "the bit is noise" territory
#: has stopped testing anything.
BAND_SLACK_BELOW = 0.05
#: How far above the highest prediction.  Tighter on purpose: there is
#: no mechanism that raises BA occupancy above the idle figure, so
#: anything above it is a wiring fault and the band must not admit it.
BAND_SLACK_ABOVE = 0.02


class TestTheBand:
    def test_band_is_no_wider_than_the_arithmetic_justifies(self) -> None:
        """A band that admits everything would pass every mis-wiring.

        Without this, the floor is unpinned: it could be dropped to 0.5
        and every other check here would still pass.
        """
        predictions = [ba_high_fraction(l, c) for _, l, c in MACHINES]
        assert BA_HIGH_MIN >= min(predictions) - BAND_SLACK_BELOW, (
            f"floor {BA_HIGH_MIN} is more than {BAND_SLACK_BELOW} below the "
            f"lowest prediction {min(predictions):.5f}"
        )
        assert BA_HIGH_MAX <= max(predictions) + BAND_SLACK_ABOVE, (
            f"ceiling {BA_HIGH_MAX} is more than {BAND_SLACK_ABOVE} above the "
            f"highest prediction {max(predictions):.5f}"
        )

    @pytest.mark.parametrize("name,lines,cycles", MACHINES, ids=lambda v: str(v))
    def test_band_admits_every_plausible_machine(
        self, name: str, lines: int, cycles: int
    ) -> None:
        frac = ba_high_fraction(lines, cycles)
        assert BA_HIGH_MIN <= frac <= BA_HIGH_MAX, (
            f"{name} gives {frac:.5f}, outside the live test's band "
            f"[{BA_HIGH_MIN}, {BA_HIGH_MAX}]"
        )

    def test_band_admits_the_measured_value(self) -> None:
        assert BA_HIGH_MIN <= MEASURED <= BA_HIGH_MAX

    @pytest.mark.parametrize(
        "name,value",
        [
            # BA read from PHI2 itself: every sampled cycle is PHI2-high.
            ("ba wired to phi2", 1.0),
            # A bit that is simply always set.
            ("ba stuck high", 1.0),
            # A bit that is never set, or an inverted read.
            ("ba stuck low", 0.0),
            # R/W# on a read-heavy trace -- high most of the time, but
            # not badline-shaped.
            ("ba reading rwn", 0.98),
            # The cart-ROM bit on a machine with no cartridge.
            ("ba reading a dead cart bit", 0.02),
            # A bit that just alternates with the bus phase.
            ("ba alternating", 0.5),
        ],
    )
    def test_band_still_rejects_a_miswired_bit(
        self, name: str, value: float
    ) -> None:
        """The band must stay narrow enough to keep doing its job."""
        assert not (BA_HIGH_MIN <= value <= BA_HIGH_MAX), name
