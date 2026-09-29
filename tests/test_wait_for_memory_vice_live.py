"""``wait_for_memory`` polls a byte after ``goto()`` without freezing VICE.

Issue #514.  Every binary-monitor command halts the 6510 at the next
vsync and it stays halted until ``resume()`` -- so ``goto()`` followed by a
bare ``read_bytes`` poll loop stops the target at the first read and then
reads the same frozen byte for ever.  A settle sleep only outruns the halt
for a workload short enough to finish first; it fixes nothing.

The workload is the issue's own 19-byte repro: a delay loop whose length is
read from RAM, then ``$A5`` stored to a flag and an idle ``JMP *``.  At
``count=200`` the issue measured a bare poll failing 200/200.

Both arms run in one boot, interleaved, so a slow host cannot make one
arm look better than the other (pair A/B trials on this bench).  The bare
arm is the control: it shows the halt is real on this build, and its
tail -- resume, then the helper sees the flag -- shows the workload was
halted rather than broken.

The exit-state tests use a free-running 16-bit counter: a value that
changes between the helper's last read and a later host read proves the
6510 ran in between, which only the helper's own resume can explain.
"""

from __future__ import annotations

import time

import pytest

from c64_test_harness import wait_for_memory
from c64_test_harness.execute import goto
from c64_test_harness.memory import read_bytes, write_bytes
from c64_test_harness.screen import wait_for_text

pytestmark = pytest.mark.vice_live

#: Clear of every HARNESS_SCRATCH span (docs/memory_safety.md) and of the
#: $0334 trampoline -- the page tests/test_goto_cold_live.py uses.
ENTRY, FLAG_ADDR, COUNT_ADDR = 0xC900, 0xC9F0, 0xC9F1
DONE = 0xA5
#: Issue #514's repro, byte for byte.
DELAY_CODE = bytes([
    0xAE, 0xF1, 0xC9,   # LDX $C9F1        (delay length, from RAM)
    0xA0, 0x00,         # LDY #$00
    0xC8,               # INY
    0xD0, 0xFD,         # BNE $C905        (inner loop, 256 iters/pass)
    0xCA,               # DEX
    0xD0, 0xF8,         # BNE $C903        (outer loop)
    0xA9, 0xA5,         # LDA #$A5
    0x8D, 0xF0, 0xC9,   # STA $C9F0        (FLAG_ADDR <- $A5 when done)
    0x4C, 0x10, 0xC9,   # JMP $C910        (idle forever)
])
#: The issue's headline workload (200/200 bare-read failures).
COUNT = 200

COUNTER_ENTRY, COUNTER_ADDR = 0xCA00, 0xCAF0
#: INC lo / BNE +3 / INC hi / JMP loop -- a 16-bit counter that never stops.
COUNTER_CODE = bytes([
    0xEE, 0xF0, 0xCA,   # INC $CAF0
    0xD0, 0x03,         # BNE $CA08
    0xEE, 0xF1, 0xCA,   # INC $CAF1
    0x4C, 0x00, 0xCA,   # JMP $CA00
])

HELPER_TRIALS = 50
BARE_TRIALS = 10
#: Long enough that a running target finishes many times over (a pass is
#: ~20-50 ms under warp, per the issue); the bare arm must still miss it.
BARE_WINDOW = 1.0


@pytest.fixture
def booted(binary_transport):
    t = binary_transport
    t.resume()
    assert wait_for_text(t, "READY.", timeout=15.0, poll_interval=0.2,
                         verbose=False) is not None, "C64 never reached READY."
    write_bytes(t, ENTRY, DELAY_CODE)
    write_bytes(t, COUNTER_ENTRY, COUNTER_CODE)
    assert read_bytes(t, ENTRY, len(DELAY_CODE)) == DELAY_CODE
    return t


def _arm(t) -> None:
    write_bytes(t, FLAG_ADDR, bytes([0x00]))
    write_bytes(t, COUNT_ADDR, bytes([COUNT]))
    goto(t, ENTRY)


def _bare_poll_sees_flag(t) -> bool:
    """The #514 idiom: goto, then bare reads with no resume between them."""
    deadline = time.monotonic() + BARE_WINDOW
    while time.monotonic() < deadline:
        if read_bytes(t, FLAG_ADDR, 1)[0] == DONE:
            return True
        time.sleep(0.01)
    return False


def test_poll_after_goto_sees_the_flag_where_bare_reads_freeze(booted):
    t = booted
    helper_ok = 0
    bare_ok = 0
    bare_halted_then_finished = 0
    for i in range(HELPER_TRIALS):
        _arm(t)
        got = wait_for_memory(t, FLAG_ADDR, DONE, timeout=5.0,
                              poll_interval=0.01)
        if got == bytes([DONE]):
            helper_ok += 1
        if i < BARE_TRIALS:
            _arm(t)
            if _bare_poll_sees_flag(t):
                bare_ok += 1
            # The workload was frozen, not broken: resuming lets it finish.
            elif wait_for_memory(t, FLAG_ADDR, DONE, timeout=5.0,
                                 poll_interval=0.01) is not None:
                bare_halted_then_finished += 1
    assert helper_ok == HELPER_TRIALS, (
        f"wait_for_memory saw the flag in {helper_ok}/{HELPER_TRIALS} trials"
    )
    assert bare_ok == 0, (
        f"control: bare reads saw the flag in {bare_ok}/{BARE_TRIALS} "
        f"trials -- the workload no longer outlasts the first read, so this "
        f"module no longer exercises #514"
    )
    assert bare_halted_then_finished == BARE_TRIALS


def _start_counter(t) -> None:
    write_bytes(t, COUNTER_ADDR, bytes([0x00, 0x00]))
    goto(t, COUNTER_ENTRY)


def _counter_advanced_since(t, seen: bytes) -> bool:
    time.sleep(0.2)
    return read_bytes(t, COUNTER_ADDR, 2) != seen


def test_machine_runs_after_a_match(booted):
    t = booted
    _start_counter(t)
    got = wait_for_memory(t, COUNTER_ADDR, lambda d: True, length=2,
                          timeout=2.0, poll_interval=0.01)
    assert got is not None
    assert _counter_advanced_since(t, got), "CPU left halted after a match"


def test_machine_runs_after_a_timeout(booted):
    t = booted
    _start_counter(t)
    seen: list[bytes] = []
    start = time.monotonic()

    def never(data: bytes) -> bool:
        seen.append(data)
        if time.monotonic() - start > 5.0:
            raise AssertionError("wait_for_memory ignored its timeout")
        return False

    assert wait_for_memory(t, COUNTER_ADDR, never, length=2,
                           timeout=0.5, poll_interval=0.05) is None
    elapsed = time.monotonic() - start
    assert 0.5 <= elapsed < 2.0, elapsed
    # The counter moved between polls: the helper resumed each time.
    assert len(set(seen)) > 1, seen
    assert _counter_advanced_since(t, seen[-1]), "CPU left halted after timeout"


def test_machine_runs_after_the_predicate_raises(booted):
    t = booted
    _start_counter(t)
    seen: list[bytes] = []

    def boom(data: bytes) -> bool:
        seen.append(data)
        raise RuntimeError("predicate failed")

    with pytest.raises(RuntimeError, match="predicate failed"):
        wait_for_memory(t, COUNTER_ADDR, boom, length=2, timeout=2.0)
    assert _counter_advanced_since(t, seen[-1]), (
        "CPU left halted when the poll raised"
    )
