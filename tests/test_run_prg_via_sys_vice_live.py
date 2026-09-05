"""End-to-end check of ``run_prg_via_sys`` on a live VICE (issue #211).

``run_prg_via_sys`` exists because the U64's ``run_prg`` DMA-load drops an
external cartridge; the helper is meant to be backend-agnostic, yet it was
validated on the U64 only and ``tests/test_run_prg_via_sys.py`` drives a
mock.  Nothing proved that reset -> READY. -> write -> SYS actually starts
the program on a real machine.

It did not, under VICE: ``write_memory`` and ``inject_keys`` are both
monitor commands, each halts the 6510, and the original helper never
resumed -- it returned with ``SYS2061`` queued on a stopped machine.  The
U64 halts on neither, which is why the hardware validation never saw it,
and ``wait_for_text`` masks it because it resumes between its own polls.
Found by the red/green review, 2026-09-05; the fix is the ``resume`` at
the end of the helper.  This test is red without it.

The program is ``LDA #$2A / STA $02A7 / RTS`` behind a cc65-style
``10 SYS2061`` stub.  ``$02A7`` is unused by KERNAL and BASIC and cleared
by RAMTAS on every reset, so a stale marker cannot fake a pass.  The
discriminating shape is "helper returns; caller sleeps; ONE read" -- a
polling loop that resumes between reads would hide the defect, and a read
issued immediately halts any implementation by the monitor's contract.
"""

from __future__ import annotations

import time

import pytest

from c64_test_harness.execute import run_prg_via_sys

pytestmark = pytest.mark.vice_live

MARKER = 0x02A7
MARKER_VALUE = 0x2A
PAYLOAD = bytes([0xA9, MARKER_VALUE, 0x8D, MARKER & 0xFF, MARKER >> 8, 0x60])
CC65_STUB = bytes([0x01, 0x08, 0x0B, 0x08, 0x0A, 0x00, 0x9E]) + b"2061" + bytes(3)
PRG = CC65_STUB + PAYLOAD


def test_typed_sys_actually_runs_the_program(binary_transport):
    t = binary_transport
    used = run_prg_via_sys(t, PRG)
    assert used == 2061
    time.sleep(2.0)                     # no resumes in here, on purpose
    assert t.read_memory(MARKER, 1)[0] == MARKER_VALUE, (
        "SYS was typed but never executed: the helper returned with the "
        "6510 halted by its own monitor commands (issue #211 follow-up)"
    )
