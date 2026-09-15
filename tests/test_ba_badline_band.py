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
and the display is not blanked.  Sprites pull BA low too; a BASIC screen
has none, which is why the measured value sits at the no-sprite bound.

**What is derived, what is chosen, what is unknown (issue #280).**

* *Derived*: the per-machine predictions below, and every rejection value
  in :data:`MISWIRINGS` -- each is computed from the debug-word bit layout
  (``u64_debug_capture.py`` ``BusCycle``: bit 31 PHI2, 30 GAME#,
  29 EXROM#, 28 cart-ROM-active, 27 BA, 26 IRQ#, 25 NMI#, 24 R/W#) and
  the stated machine condition, not picked.  None was measured with a
  deliberately mis-wired bit.
* *Chosen*: the band edges ``BA_HIGH_MIN = 0.90`` / ``BA_HIGH_MAX = 0.96``
  and the slacks.  They are not derived from anything; they are pinned
  exactly by :class:`TestTheBandEdgesArePinned`, so moving an edge fails
  here and forces the justification to be revisited.
* *Unknown*: R/W# (bit 24) and IRQ# (bit 26).  Their high-fraction
  depends on the program's read/write mix and IRQ-handler duration, and
  no capture has measured either.  The band therefore makes **no** claim
  to reject a BA read from those bits -- an idle-screen R/W# could sit
  inside ``[0.90, 0.96]``.  The earlier ``("ba reading rwn", 0.98)`` case
  was an invented number doing real work next to the ceiling; it is gone.
  More generally, no band can reject a signal that happens to sit near 93%.
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


PREDICTIONS = {name: ba_high_fraction(l, c) for name, l, c in MACHINES}


def _rejected(value: float) -> bool:
    return not (BA_HIGH_MIN <= value <= BA_HIGH_MAX)


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
#: BA low too -- but not arbitrarily far.  Chosen, not derived.
BAND_SLACK_BELOW = 0.05
#: How far above the highest prediction.  Tighter on purpose: there is
#: no mechanism that raises BA occupancy above the idle figure while the
#: display is on.  Chosen, not derived.
BAND_SLACK_ABOVE = 0.02


class TestTheBand:
    def test_band_is_no_wider_than_the_arithmetic_justifies(self) -> None:
        """A band that admits everything would pass every mis-wiring."""
        predictions = list(PREDICTIONS.values())
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


class TestTheBandEdgesArePinned:
    """The edges are chosen numbers, so pin them exactly.

    The two tests above bound the edges only loosely: the floor may sit
    anywhere in ``[lowest prediction - 0.05, lowest prediction]`` and the
    ceiling anywhere in ``[highest prediction, highest prediction + 0.02]``
    without either failing, and every derived mis-wiring is far outside
    both.  So without this pin an edge could move by up to ~0.05 silently.
    Change these together with ``test_u64_streams_live.py`` and with the
    reasoning in this module's docstring.
    """

    def test_floor(self) -> None:
        assert BA_HIGH_MIN == 0.90

    def test_ceiling(self) -> None:
        assert BA_HIGH_MAX == 0.96

    def test_pinned_edges_are_not_the_loose_bounds(self) -> None:
        """Vacuity guard: the pin must be stricter than the slack tests.

        If an edge equalled its loose bound, the pin would add nothing.
        """
        predictions = list(PREDICTIONS.values())
        assert BA_HIGH_MIN > min(predictions) - BAND_SLACK_BELOW
        assert BA_HIGH_MAX < max(predictions) + BAND_SLACK_ABOVE


def _inverted(name: str) -> float:
    return 1.0 - PREDICTIONS[name]


#: ``(case id, value, derivation)``.  Every value is computed from the bit
#: layout and a stated machine condition; none is a measurement.
MISWIRINGS = [
    # Denominator is PHI2-high cycles, so bit 31 is 1 on every one of them.
    ("bit31 phi2 read as ba", 1.0,
     "every sampled cycle is PHI2-high by construction of is_cpu"),
    # Active-low read of an active-high BA: the complement of each prediction.
    *[
        (f"ba polarity inverted, {name}", _inverted(name),
         "1 - badline prediction for this machine")
        for name, _, _ in MACHINES
    ],
    # Bit 28 = not (ROMH# and ROML#).  No cartridge (and no internal cart
    # emulation): EXROM#/GAME# pulled high, the PLA asserts neither line.
    ("bit28 cart-rom-active read as ba, no cartridge", 0.0,
     "neither ROML# nor ROMH# asserts with EXROM#/GAME# high"),
    # Bits 30/29 raw lines, no cartridge: pulled up, always 1.
    ("bit30 game# read as ba, no cartridge", 1.0, "GAME# pulled high"),
    ("bit29 exrom# read as ba, no cartridge", 1.0, "EXROM# pulled high"),
    # Bit 25 raw NMI#: idle BASIC screen, no RESTORE, no CIA2 NMI enabled.
    ("bit25 nmi# read as ba, no nmi source", 1.0, "NMI# stays high"),
    # Stuck bits, by definition.
    ("ba stuck high", 1.0, "constant 1"),
    ("ba stuck low", 0.0, "constant 0"),
]

#: Signals whose high-fraction cannot be derived without a capture.  Listed
#: so their absence from MISWIRINGS is a decision, not an oversight.
UNDERIVED = {
    "bit24 r/w#": "read/write mix of the running program; unmeasured",
    "bit26 irq#": "IRQ handler duration per frame; unmeasured",
}


class TestTheBandStillRejectsDerivedMiswirings:
    @pytest.mark.parametrize(
        "name,value,why", MISWIRINGS, ids=[m[0] for m in MISWIRINGS]
    )
    def test_band_rejects(self, name: str, value: float, why: str) -> None:
        assert _rejected(value), f"{name} ({why}) = {value:.5f} is inside the band"

    def test_inverted_values_really_are_derived(self) -> None:
        """The polarity cases track the arithmetic, not a literal."""
        values = {m[0]: m[1] for m in MISWIRINGS}
        for name, lines, cycles in MACHINES:
            assert values[f"ba polarity inverted, {name}"] == pytest.approx(
                1.0 - ba_high_fraction(lines, cycles)
            )

    def test_bit_read_cases_match_a_synthetic_idle_frame(self) -> None:
        """Tie each bit-read value to the real ``BusCycle`` layout.

        Build one NTSC 6567R8 frame of debug words for the stated
        condition -- no cartridge, no NMI source, BA low on the 25 x 43
        badline cycles -- with PHI2 high, as ``is_cpu`` filters.  Then read
        each named bit over the ``is_cpu`` cycles.  This is a model of the
        condition, not a capture; what it checks is that every value in
        :data:`MISWIRINGS` is what that bit of that word would give.
        """
        from c64_test_harness.backends.u64_debug_capture import BusCycle

        lines, cycles = 263, 65
        total_phi2_high = lines * cycles
        low = BADLINES_PER_FRAME * BADLINE_BA_LOW_CYCLES
        # Lines that idle high on both clock phases with no cartridge and
        # no NMI source.
        idle_lines = (1 << 30) | (1 << 29) | (1 << 25)
        frame = []
        for i in range(total_phi2_high):
            # PHI2-high (6510) word: BA low on the badline cycles.
            frame.append(BusCycle((1 << 31) | idle_lines
                                  | (0 if i < low else 1 << 27)))
            # PHI2-low (VIC) word: bit 31 clear, GAME#/EXROM#/NMI# still
            # high, BA low.  is_cpu must drop every one of these, so a phi2
            # that reads any other always-high bit (e.g. bit 30) counts
            # them and fails the length check below.
            frame.append(BusCycle(idle_lines))
        cpu = [c for c in frame if c.is_cpu]
        assert len(frame) == 2 * total_phi2_high
        assert len(cpu) == total_phi2_high, (
            f"is_cpu kept {len(cpu)} of {len(frame)} words; expected only the "
            f"{total_phi2_high} PHI2-high ones"
        )
        assert all(c.raw >> 31 & 1 for c in cpu)

        def frac(bit: int) -> float:
            return sum(1 for c in cpu if c.raw >> bit & 1) / len(cpu)

        values = {m[0]: m[1] for m in MISWIRINGS}
        assert frac(27) == pytest.approx(PREDICTIONS["NTSC 6567R8"])
        assert frac(27) == pytest.approx(sum(1 for c in cpu if c.ba) / len(cpu))
        assert values["bit31 phi2 read as ba"] == frac(31)
        assert values["bit30 game# read as ba, no cartridge"] == frac(30)
        assert values["bit29 exrom# read as ba, no cartridge"] == frac(29)
        assert values["bit28 cart-rom-active read as ba, no cartridge"] == frac(28)
        assert values["bit25 nmi# read as ba, no nmi source"] == frac(25)
        assert values["ba polarity inverted, NTSC 6567R8"] == pytest.approx(
            1.0 - frac(27))

    def test_rejection_check_can_fail(self) -> None:
        """Positive control: an in-band value is not reported as rejected."""
        assert not _rejected(MEASURED)
        for value in PREDICTIONS.values():
            assert not _rejected(value)

    def test_no_invented_near_edge_case_remains(self) -> None:
        """Every rejection value is a derived constant, far from the band.

        The removed ``rwn`` case sat 0.02 above the ceiling with no
        derivation.  If a near-edge case is ever added again it must come
        with evidence, so this fails first.
        """
        for name, value, _ in MISWIRINGS:
            gap = min(abs(value - BA_HIGH_MIN), abs(value - BA_HIGH_MAX))
            assert gap > 0.03, f"{name} sits {gap:.3f} from a band edge"

    def test_underived_signals_are_not_claimed(self) -> None:
        ids = " ".join(m[0] for m in MISWIRINGS)
        for bit in ("bit24", "bit26", "rwn", "irq"):
            assert bit not in ids, f"{bit} has no derivation (see UNDERIVED)"
        assert set(UNDERIVED) == {"bit24 r/w#", "bit26 irq#"}
