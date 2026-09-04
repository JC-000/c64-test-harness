"""``goto(cold=True)`` really does hand its target a clean machine.

Issue #192.  The mocked tests in ``tests/test_execute.py`` pin what
``goto`` writes; they cannot check the two things that actually matter on
a 6510, both of which are inference until an emulator says otherwise:

* that the binary monitor accepts ``PC``, ``SP`` and ``FL`` in **one**
  ``REGISTERS_SET`` -- ``jsr()``'s restore already relies on this, so it
  is very likely, but "very likely" is not a measurement;
* that clearing ``I`` on the way in leaves the machine **taking
  interrupts again**.  That is the whole point of a cold entry, and no
  mock can show it: the falsifier is the jiffy clock advancing, which
  needs a real IRQ to fire.

The dirty pre-state is set by hand rather than waited for.  Issue #188's
probe measured mid-IRQ halts at ~1.1-1.4% of calls, so parking the CPU
until one happens by itself would be a slow and flaky way to arrange a
condition that ``set_registers`` can state exactly.
"""

from __future__ import annotations

import time

import pytest

from c64_test_harness.execute import goto
from c64_test_harness.screen import wait_for_text

pytestmark = pytest.mark.vice_live

#: ``JMP *`` parked clear of every HARNESS_SCRATCH span (docs/memory_safety.md)
#: and clear of the $0334 trampoline.
LOOP_ADDR = 0xC900
LOOP_CODE = bytes([0x4C, 0x00, 0xC9])

#: 6510 status bits.  Bit 5 is the hardwired unused bit VICE reports set.
I_FLAG = 0x04
D_FLAG = 0x08
UNUSED = 0x20

#: What a mid-IRQ halt leaves behind, as far as a register write can
#: reproduce it: stack eaten by frames that will never be popped, ``I``
#: set by interrupt entry, ``D`` set by a decimal-mode computation.
DIRTY_SP = 0x40
DIRTY_FL = I_FLAG | D_FLAG | UNUSED


def _boot(t) -> None:
    t.resume()
    assert wait_for_text(t, "READY.", timeout=15.0, poll_interval=0.2,
                         verbose=False) is not None, "C64 never reached READY."


def _read_jiffy(t) -> bytes:
    return t.read_memory(0xA0, 3)


def test_cold_entry_rebuilds_sp_and_clears_i_and_d(binary_transport):
    t = binary_transport
    _boot(t)
    t.write_memory(LOOP_ADDR, LOOP_CODE)

    t.set_registers({"SP": DIRTY_SP, "FL": DIRTY_FL})
    before = t.read_registers()
    assert before["SP"] == DIRTY_SP, "could not establish the dirty stack pointer"
    assert before["FL"] & I_FLAG, "could not establish the dirty I flag"

    goto(t, LOOP_ADDR, cold=True)

    time.sleep(0.2)
    after = t.read_registers()
    assert after["SP"] == 0xFF, (
        f"cold entry left SP at ${after['SP']:02X}; the target inherited the "
        f"remains of the halted machine's stack"
    )
    assert not after["FL"] & I_FLAG, (
        f"cold entry left I set (FL=${after['FL']:02X}): the target runs with "
        f"interrupts masked, which is the severe half of issue #183"
    )
    assert not after["FL"] & D_FLAG, (
        f"cold entry left D set (FL=${after['FL']:02X}): ADC/SBC in the target "
        f"would silently do BCD"
    )
    assert LOOP_ADDR <= after["PC"] <= LOOP_ADDR + 2, (
        f"PC ${after['PC']:04X} is not in the parked loop -- the single "
        f"register write did not carry PC alongside SP and FL"
    )


def test_cold_entry_leaves_the_machine_taking_interrupts(binary_transport):
    """The falsifier a mock cannot provide: the jiffy clock must move.

    Entered with ``I`` still set, ``$A0-$A2`` freezes for good -- exactly
    what issue #183's no-CLI arm measured.
    """
    t = binary_transport
    _boot(t)
    t.write_memory(LOOP_ADDR, LOOP_CODE)
    t.set_registers({"SP": DIRTY_SP, "FL": DIRTY_FL})

    goto(t, LOOP_ADDR, cold=True)

    t.resume()
    time.sleep(0.3)
    first = _read_jiffy(t)
    t.resume()
    time.sleep(0.5)
    second = _read_jiffy(t)
    assert first != second, (
        f"jiffy clock frozen at {first.hex()} across 0.5s in the parked loop: "
        f"IRQs are still masked after a cold entry"
    )


def test_warm_goto_leaves_the_stack_pointer_alone(binary_transport):
    """The contrast that makes *cold* meaningful, on real hardware state.

    A plain ``goto`` is a one-way jump that deliberately inherits whatever
    the monitor halted; if it quietly rebuilt SP too, ``cold`` would be
    documenting a difference that does not exist.
    """
    t = binary_transport
    _boot(t)
    t.write_memory(LOOP_ADDR, LOOP_CODE)
    t.set_registers({"SP": DIRTY_SP})

    goto(t, LOOP_ADDR)

    time.sleep(0.2)
    after = t.read_registers()
    assert after["SP"] == DIRTY_SP, (
        f"plain goto changed SP from ${DIRTY_SP:02X} to ${after['SP']:02X}"
    )
