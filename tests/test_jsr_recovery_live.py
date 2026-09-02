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
from c64_test_harness.transport import TimeoutError

pytestmark = pytest.mark.vice_live

#: Harness-claimed scratch page (docs/memory_safety.md); clear of BASIC,
#: the KERNAL workspace, and the cassette-buffer trampolines.
COUNTER_ADDR = 0xC000
HANG_ADDR = 0xC100
INC_ADDR = 0xC110

#: Default trampoline: JSR at $0334, breakpoint on the NOP at $0337.
POST_RTS_LANDING = 0x0337


def test_hung_routine_is_recovered_and_the_next_call_runs(binary_transport):
    t = binary_transport

    # JMP * -- an infinite loop that never RTSes.
    load_code(t, HANG_ADDR, [0x4C, HANG_ADDR & 0xFF, HANG_ADDR >> 8])
    # INC $C000; RTS -- observable side effect, then a clean return.
    load_code(t, INC_ADDR, [0xEE, COUNTER_ADDR & 0xFF, COUNTER_ADDR >> 8, 0x60])
    t.write_memory(COUNTER_ADDR, bytes([0x41]))
    assert t.read_memory(HANG_ADDR, 3) == b"\x4c\x00\xc1", "hang loop did not land"
    assert t.read_memory(INC_ADDR, 4) == b"\xee\x00\xc0\x60", "INC routine did not land"

    sp_before = t.read_registers()["SP"]

    with pytest.raises(RoutineHung) as excinfo:
        jsr(t, HANG_ADDR, timeout=2.0, recover_on_timeout=True)
    exc = excinfo.value

    # It is still a TimeoutError -- existing callers keep catching it.
    assert isinstance(exc, TimeoutError)
    assert exc.addr == HANG_ADDR
    assert exc.elapsed >= 2.0, exc.elapsed
    # Binmon answered while the CPU was spinning: the register read landed
    # and found the CPU on the only instruction the loop has.
    assert exc.hung_pc == HANG_ADDR, str(exc)
    assert exc.recovered is True, str(exc)
    assert f"${POST_RTS_LANDING:04X}" in exc.detail, exc.detail

    # The frame the hung call left on the stack is gone.  A no-op recovery
    # leaves SP two bytes lower here.
    assert t.read_registers()["SP"] == sp_before

    # And the same boot is still callable: the second routine runs, returns,
    # and lands on the trampoline's post-RTS breakpoint.
    regs = jsr(t, INC_ADDR, timeout=5.0)
    assert regs["PC"] == POST_RTS_LANDING
    assert regs["SP"] == sp_before
    assert t.read_memory(COUNTER_ADDR, 1) == b"\x42", "INC $C000 did not run"
