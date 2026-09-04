"""``headless_sid_config()`` really does clock a SID; ``sound=False`` does not.

Issue #193.  The mocked tests pin the argv this produces.  Only an
emulator can answer the question the issue is actually about: does the
thing answering ``$D41B`` behave like an oscillator, or like a counter
that merely looks like one?

**The SID is sampled by the 6510, not by the host.**  The binary
monitor's memory reads go through bank ``default``, which does not see
live I/O -- a host-side ``read_memory($D012)`` returns a constant, and a
host-side ``read_memory($D41B)`` returns the RAM under the I/O window
rather than the chip.  So the setup and the sampling both run as 6502
code and the host only reads back the RAM buffer.  A probe written the
other way round reports "not a SID" for every configuration, including
the healthy one, which is a convincing and completely wrong answer.

**The discriminator is TEST** (``$D412`` bit 3), because neither half of
the question is answerable alone.  A real SID freezes OSC3 while TEST is
asserted; VICE's sound-disabled fallback (``sid.c:137``,
``maincpu_clk % 256``) cannot freeze, because nothing about it is
connected to the voice.  Conversely a *stalled* sound core
(``sounddev="dummy"``) freezes under both, so the frozen half alone
would pass it.  Healthy is frozen-then-moving, and nothing else is.
"""

from __future__ import annotations

import time

import pytest

from c64_test_harness.backends.vice_lifecycle import (
    ViceConfig,
    ViceProcess,
    headless_sid_config,
)
from c64_test_harness.backends.vice_manager import PortAllocator
from c64_test_harness.execute import jsr
from c64_test_harness.screen import wait_for_text

from conftest import connect_binary_transport, require_vice_or_skip

pytestmark = pytest.mark.vice_live

#: All clear of every HARNESS_SCRATCH span (docs/memory_safety.md) and of
#: the $0334 jsr trampoline.
SETUP_ADDR = 0xC920
SAMPLER_ADDR = 0xC900
BUF_ADDR = 0xC980
CTRL_SLOT = 0xC9F0
SAMPLES = 16

#: Voice 3 control bits.
SAW = 0x20
TEST = 0x08

#:  LDA #$00 / STA $D40E     voice 3 frequency, low
#:  LDA #$20 / STA $D40F     voice 3 frequency, high
#:  LDA $C9F0 / STA $D412    control byte, handed over by the host
#:  RTS
SETUP_CODE = bytes([
    0xA9, 0x00, 0x8D, 0x0E, 0xD4,
    0xA9, 0x20, 0x8D, 0x0F, 0xD4,
    0xAD, 0xF0, 0xC9, 0x8D, 0x12, 0xD4,
    0x60,
])

#:  LDX #$00
#:  loop: LDA $D41B / STA $C980,X / INX / CPX #$10 / BNE loop
#:  RTS
#:
#: Sixteen cycles per pass, which is what makes the failing case legible:
#: under the fallback the samples rise by exactly that stride.
SAMPLER_CODE = bytes([
    0xA2, 0x00,
    0xAD, 0x1B, 0xD4,
    0x9D, 0x80, 0xC9,
    0xE8,
    0xE0, SAMPLES,
    0xD0, 0xF5,
    0x60,
])


def _sample_osc3(transport, control: int) -> list[int]:
    """Set voice 3's control byte, run the sampler, return the buffer."""
    transport.write_memory(CTRL_SLOT, bytes([control]))
    # Clear the buffer first, so a sampler that never ran is visible as
    # sixteen zeros rather than as last call's data.
    transport.write_memory(BUF_ADDR, bytes(SAMPLES))
    jsr(transport, SETUP_ADDR, timeout=5.0)
    jsr(transport, SAMPLER_ADDR, timeout=5.0)
    return list(transport.read_memory(BUF_ADDR, SAMPLES))


def _boot_and_load(transport) -> None:
    transport.resume()
    assert wait_for_text(transport, "READY.", timeout=20.0, poll_interval=0.2,
                         verbose=False) is not None, "C64 never reached READY."
    transport.write_memory(SETUP_ADDR, SETUP_CODE)
    transport.write_memory(SAMPLER_ADDR, SAMPLER_CODE)


def _osc3_under(config: ViceConfig) -> tuple[list[int], list[int]]:
    """Boot *config*, return (samples with TEST set, samples with it clear)."""
    require_vice_or_skip()
    allocator = PortAllocator(port_range_start=6511, port_range_end=6531)
    port = allocator.allocate()
    reservation = allocator.take_socket(port)
    if reservation is not None:
        reservation.close()
    config.port = port
    with ViceProcess(config) as vice:
        transport = connect_binary_transport(port, proc=vice)
        try:
            time.sleep(2.0)
            _boot_and_load(transport)
            return (_sample_osc3(transport, SAW | TEST),
                    _sample_osc3(transport, SAW))
        finally:
            transport.close()
            allocator.release(port)


def test_sound_disabled_answers_osc3_with_a_counter():
    """The defect itself: TEST is ignored, and the stride gives it away.

    Sixteen cycles per sampling pass, samples 16 apart: that is
    ``maincpu_clk % 256``, not a voice.  Nothing raises, and a caller
    reading this sees an immaculate ramp.
    """
    frozen, _moving = _osc3_under(ViceConfig(warp=False, sound=False))

    assert len(set(frozen)) > 1, (
        f"OSC3 froze under TEST with sound disabled: {frozen}.  Issue #193's "
        f"premise is that it cannot, because the fallback is a clock"
    )
    strides = {(b - a) % 256 for a, b in zip(frozen, frozen[1:])}
    assert strides == {16}, (
        f"expected the sampling loop's own 16-cycle stride from "
        f"maincpu_clk % 256, got strides {sorted(strides)} in {frozen}"
    )


def test_headless_sid_config_clocks_a_real_sid(tmp_path):
    """The recipe the harness recommends, end to end.

    Both halves matter.  Frozen-under-TEST alone would also pass for
    ``sounddev="dummy"``, where the sound core has stalled and every read
    returns stale state; moving-when-released alone would also pass for
    the counter above.
    """
    config = headless_sid_config(tmp_path / "osc3.wav",
                                 base=ViceConfig(warp=False))
    frozen, moving = _osc3_under(config)

    assert len(set(frozen)) == 1, (
        f"OSC3 did not hold still under TEST: {frozen}.  A real SID resets "
        f"the accumulator and holds it; this is not being clocked as one"
    )
    assert len(set(moving)) > 1, (
        f"OSC3 did not move with TEST released: {moving}.  Sound is on but "
        f"nothing is draining the buffer, so the SID has stopped advancing "
        f"-- the sounddev='dummy' failure mode, on a device that should drain"
    )
