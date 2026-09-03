"""``jsr(recover_on_timeout=True)`` survives a hung routine on a real VICE.

Issue #156.  The mocked tests in ``tests/test_execute.py`` pin the
sequence of transport calls; this one makes the emulator actually hang and
checks the two things a mock cannot: that the binary monitor really does
answer while the CPU spins, and that the machine is callable afterwards.

Why the assertions are shaped the way they are.  A bare second ``jsr``
after an *unrecovered* hang also appears to work on VICE -- it rewrites the
trampoline, sets PC and resumes, and the hung ``JMP *`` is simply
abandoned.  What it leaves behind is the trampoline's return frame: SP is
two bytes lower per hang, and a suite probing six hangs in one boot walks
the stack down.  So the falsifier here is SP, not "the next call
returned": after recovery SP must equal the value captured before the
hung call, and a no-op recovery fails that.
"""

from __future__ import annotations

import pytest

from c64_test_harness.execute import RoutineHung, jsr, load_code
from c64_test_harness.screen import wait_for_text
from c64_test_harness.transport import TimeoutError

pytestmark = pytest.mark.vice_live

#: Harness-claimed scratch page (docs/memory_safety.md); clear of BASIC,
#: the KERNAL workspace, and the trampolines.  $C000 sits inside the UCI
#: stub block, which is harmless on VICE (no UCI) -- the same address
#: tests/test_vice_jam_live.py uses.
COUNTER_ADDR = 0xC000
HANG_ADDR = 0xC100
INC_ADDR = 0xC110
SETTLE_ADDR = 0xC120

#: Default trampoline: JSR at $0334, breakpoint on the NOP at $0337.
POST_RTS_LANDING = 0x0337

#: Interrupt-disable bit of the 6510 status register (VICE reports it as FL).
I_FLAG = 0x04


def test_hung_routine_is_recovered_and_the_next_call_runs(binary_transport):
    t = binary_transport

    # Let the machine finish booting.  Nothing before this point waited
    # for it: ViceProcess.start does not wait for boot, and the fixture's
    # first monitor command halted the machine at the next vsync -- which
    # can be mid-KERNAL-reset, ~25 frames under SEI before its final CLI.
    # wait_for_text resumes the CPU between screen reads.
    t.resume()
    assert wait_for_text(t, "READY.", timeout=15.0, poll_interval=0.2,
                         verbose=False) is not None, "C64 never reached READY."

    # Then settle into a known register state.  wait_for_text now resumes
    # on every exit path, so the CPU is running here; the next monitor
    # command re-halts it, now and then inside the IRQ handler with I set.
    # jsr() would capture that FL and put it back, flaking the I-clear
    # checks below -- so this one call opts out.  A trivial CLI; RTS
    # through the trampoline with preserve_state=False leaves the CPU
    # halted at the checkpoint with I deterministically clear; the default
    # restore would write the pre-call FL back over the CLI and make this
    # settle step a no-op.
    load_code(t, SETTLE_ADDR, [0x58, 0x60])
    assert jsr(t, SETTLE_ADDR, timeout=5.0,
               preserve_state=False)["PC"] == POST_RTS_LANDING

    # SEI; JMP * -- an infinite loop that never RTSes.  The SEI is what
    # makes hung_pc deterministic: the monitor pauses at the next
    # instruction boundary from a per-frame hook, and the KERNAL's CIA
    # IRQ is not frame-aligned, so with I clear the pause can land inside
    # $EA31 instead of on the loop.  It also exercises the SEI'd-routine
    # case a real hang often is.
    loop = HANG_ADDR + 1
    load_code(t, HANG_ADDR, [0x78, 0x4C, loop & 0xFF, loop >> 8])
    # INC $C000; RTS -- observable side effect, then a clean return.
    load_code(t, INC_ADDR, [0xEE, COUNTER_ADDR & 0xFF, COUNTER_ADDR >> 8, 0x60])
    t.write_memory(COUNTER_ADDR, bytes([0x41]))
    assert t.read_memory(HANG_ADDR, 4) == b"\x78\x4c\x01\xc1", "hang loop did not land"
    assert t.read_memory(INC_ADDR, 4) == b"\xee\x00\xc0\x60", "INC routine did not land"

    regs_before = t.read_registers()
    sp_before = regs_before["SP"]
    # I is clear at BASIC READY; the SEI in the stub sets it.  A recovery
    # that restores SP but not FL leaves every later test with IRQs off.
    assert regs_before["FL"] & I_FLAG == 0, "precondition: I clear before the hang"

    with pytest.raises(RoutineHung) as excinfo:
        jsr(t, HANG_ADDR, timeout=2.0, recover_on_timeout=True)
    exc = excinfo.value

    # It is still a TimeoutError -- existing callers keep catching it.
    assert isinstance(exc, TimeoutError)
    assert exc.addr == HANG_ADDR
    assert exc.elapsed >= 2.0, exc.elapsed
    # Binmon answered while the CPU was spinning: the register read landed
    # and found the CPU on the JMP -- the only instruction the loop has
    # once the SEI has executed.
    assert exc.hung_pc == HANG_ADDR + 1, str(exc)
    assert exc.recovered is True, str(exc)
    assert f"${POST_RTS_LANDING:04X}" in exc.detail, exc.detail

    # The frame the hung call left on the stack is gone.  A no-op recovery
    # leaves SP two bytes lower here -- and the SEI still in force.
    regs_after = t.read_registers()
    assert regs_after["SP"] == sp_before
    assert regs_after["FL"] & I_FLAG == 0, "SEI from the hung routine not undone"

    # And the same boot is still callable: the second routine runs, returns,
    # and lands on the trampoline's post-RTS breakpoint.
    regs = jsr(t, INC_ADDR, timeout=5.0)
    assert regs["PC"] == POST_RTS_LANDING
    assert regs["SP"] == sp_before
    assert t.read_memory(COUNTER_ADDR, 1) == b"\x42", "INC $C000 did not run"
