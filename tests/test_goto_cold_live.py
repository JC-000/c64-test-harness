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

**The entry state is sampled on the 6510, not read from the host**
(issue #225).  A cold entry does not acknowledge the pending CIA IRQ, so
the KERNAL handler runs the moment ``I`` clears -- and a host-side
``read_registers()`` halts the machine from VICE's vsync hook at a fixed
frame phase that can coincide with that handler: forced with an exec
checkpoint at ``$FF4C`` the old assertion read ``SP=$FA``, the 3-byte
interrupt frame plus the first two ``PHA`` of the ``$FF48`` prologue,
which is the value the flake reported.  So the target routine records
its own entry ``SP`` and status register into RAM before anything else
can push, and the test reads that record; the host-side register view is
used only for ``PC``, which the routine's ``SEI`` makes stable.
"""

from __future__ import annotations

import time

import pytest

from c64_test_harness.execute import goto
from c64_test_harness.screen import wait_for_text

pytestmark = pytest.mark.vice_live

#: Code and sample buffer parked clear of every HARNESS_SCRATCH span
#: (docs/memory_safety.md) and clear of the $0334 trampoline.
LOOP_ADDR = 0xC900
#: ``JMP *`` for the tests that only need the machine parked.
LOOP_CODE = bytes([0x4C, 0x00, 0xC9])
#: Where the sampler routine records what it found at entry.
SAMPLE_BUF = 0xC980
SAMPLE_SP, SAMPLE_FL, SAMPLE_COUNTER, SAMPLE_MARK = 0, 1, 2, 5
SAMPLE_LEN = 6
MARK_DONE = 0xA5

#: 6510 status bits.  Bit 5 is the hardwired unused bit VICE reports set.
I_FLAG = 0x04
D_FLAG = 0x08
UNUSED = 0x20

#: What a mid-IRQ halt leaves behind, as far as a register write can
#: reproduce it: stack eaten by frames that will never be popped, ``I``
#: set by interrupt entry, ``D`` set by a decimal-mode computation.
DIRTY_SP = 0x40
DIRTY_FL = I_FLAG | D_FLAG | UNUSED


def _entry_sampler(org: int, buf: int) -> bytes:
    """A routine that records its own entry ``SP`` and ``P``, then spins.

    ``PHP`` comes first so the status word captured is the one the entry
    handed over, before ``SEI`` sets ``I``; the ``PLA`` that reads it back
    also puts ``SP`` back where the entry left it, so the ``TSX`` after
    it reports the entry value.  An IRQ taken between the entry and the
    ``SEI`` (a cold entry invites one) pushes and pops below that ``PHP``
    and changes nothing the sampler records.  After the ``SEI`` nothing
    can push, so the record is the entry state and the 24-bit counter
    shows the loop is running.
    """
    def at(off: int) -> list[int]:
        a = buf + off
        return [a & 0xFF, a >> 8]

    head = [
        0x08,                           # PHP        ; P at entry
        0x78,                           # SEI        ; no more pushes from IRQs
        0x68,                           # PLA        ; A = entry P, SP = entry SP
        0x8D, *at(SAMPLE_FL),           # STA buf+1
        0xBA,                           # TSX
        0x8E, *at(SAMPLE_SP),           # STX buf    ; SP at entry
        0xA9, MARK_DONE,                # LDA #$A5
        0x8D, *at(SAMPLE_MARK),         # STA buf+5  ; record complete
    ]
    loop = org + len(head)
    body = [
        0xEE, *at(SAMPLE_COUNTER),      # INC buf+2  ; 24-bit counter, low
        0xD0, 0x08,                     # BNE jmp
        0xEE, *at(SAMPLE_COUNTER + 1),  # INC buf+3
        0xD0, 0x03,                     # BNE jmp
        0xEE, *at(SAMPLE_COUNTER + 2),  # INC buf+4
        0x4C, loop & 0xFF, loop >> 8,   # jmp: JMP loop
    ]
    return bytes(head + body)


SAMPLER_CODE = _entry_sampler(LOOP_ADDR, SAMPLE_BUF)


def _boot(t) -> None:
    t.resume()
    assert wait_for_text(t, "READY.", timeout=15.0, poll_interval=0.2,
                         verbose=False) is not None, "C64 never reached READY."


def _read_jiffy(t) -> bytes:
    return t.read_memory(0xA0, 3)


def _enter_sampler(t, dirty: dict[str, int], *, cold: bool) -> dict:
    """Establish *dirty*, ``goto`` the sampler, return what it recorded.

    Every monitor command halts the machine, so the poll for the record
    resumes between reads.  The returned ``counter`` pair is two reads of
    the loop counter with the machine running in between.
    """
    t.write_memory(LOOP_ADDR, SAMPLER_CODE)
    t.write_memory(SAMPLE_BUF, bytes(SAMPLE_LEN))
    t.set_registers(dirty)
    before = t.read_registers()
    for name, value in dirty.items():
        assert before[name] == value, f"could not establish the dirty {name}"

    goto(t, LOOP_ADDR, cold=cold)

    deadline = time.monotonic() + 5.0
    while t.read_memory(SAMPLE_BUF + SAMPLE_MARK, 1)[0] != MARK_DONE:
        assert time.monotonic() < deadline, (
            "the sampler never wrote its record: the jump did not land"
        )
        t.resume()
        time.sleep(0.05)
    record = t.read_memory(SAMPLE_BUF, SAMPLE_LEN)
    pc = t.read_registers()["PC"]
    t.resume()
    time.sleep(0.1)
    later = t.read_memory(SAMPLE_BUF, SAMPLE_LEN)
    return {
        "sp": record[SAMPLE_SP],
        "fl": record[SAMPLE_FL],
        "pc": pc,
        "counter": (record[SAMPLE_COUNTER:SAMPLE_MARK], later[SAMPLE_COUNTER:SAMPLE_MARK]),
    }


def test_cold_entry_rebuilds_sp_and_clears_i_and_d(binary_transport):
    t = binary_transport
    _boot(t)

    got = _enter_sampler(t, {"SP": DIRTY_SP, "FL": DIRTY_FL}, cold=True)

    assert got["sp"] == 0xFF, (
        f"cold entry left SP at ${got['sp']:02X} as recorded by the target at "
        f"entry; it inherited the remains of the halted machine's stack"
    )
    assert not got["fl"] & I_FLAG, (
        f"cold entry left I set (P=${got['fl']:02X} at entry): the target runs "
        f"with interrupts masked, which is the severe half of issue #183"
    )
    assert not got["fl"] & D_FLAG, (
        f"cold entry left D set (P=${got['fl']:02X} at entry): ADC/SBC in the "
        f"target would silently do BCD"
    )
    assert LOOP_ADDR <= got["pc"] < LOOP_ADDR + len(SAMPLER_CODE), (
        f"PC ${got['pc']:04X} is not in the sampler -- the single register "
        f"write did not carry PC alongside SP and FL"
    )
    assert got["counter"][0] != got["counter"][1], (
        f"loop counter stuck at {got['counter'][0].hex()}: the target is not running"
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
    documenting a difference that does not exist.  Sampled on the 6510
    for the same reason as the cold test: ``I`` is inherited clear here,
    so a host-side read has the same chance of landing in the handler.
    """
    t = binary_transport
    _boot(t)

    got = _enter_sampler(t, {"SP": DIRTY_SP}, cold=False)

    assert got["sp"] == DIRTY_SP, (
        f"plain goto changed SP from ${DIRTY_SP:02X} to ${got['sp']:02X} "
        f"as recorded by the target at entry"
    )
