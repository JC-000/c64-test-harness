"""A real 6510 jam must surface as a ``TransportError``, on a real VICE.

Commit 754d03f taught ``wait_for_stopped`` to report VICE's ``0x61`` JAM
event instead of dropping it.  Until the ``-jamaction`` pin was changed
that path was unreachable on every harness launch: VICE emits the event
only from ``monitor_binary_ui_jam_dialog``, which ``machine_jam`` calls
only when ``jam_action == 0`` (DIALOG; S ``machine.c:131-139``), and the
harness pinned 1 (CONTINUE).  The mocked tests in
``tests/test_vice_binary_unit.py`` could not see that, because they put
the frame on a fake wire themselves.  This test makes the emulator emit
it.

Opcode ``$02`` is one of the 6502's JAM/KIL encodings: the CPU halts on
it and never advances.  With the binary monitor connected the "dialog"
is routed to the monitor, the machine stops, and the transport must
raise rather than wait out its timeout.
"""

from __future__ import annotations

import pytest

from c64_test_harness.execute import load_code
from c64_test_harness.transport import TransportError

pytestmark = pytest.mark.vice_live

#: Scratch RAM, clear of BASIC and the KERNAL's own workspace.
JAM_ADDR = 0xC000


def test_a_jam_is_reported_not_timed_out(binary_transport):
    t = binary_transport
    load_code(t, JAM_ADDR, [0x02])  # JAM
    assert t.read_memory(JAM_ADDR, 1) == b"\x02", "the JAM opcode did not land"

    t.set_registers({"PC": JAM_ADDR})
    t.resume()

    # A timeout here means the CPU halted in place with no event (the pin
    # is CONTINUE again: machine_jam returns JAM_NONE, S machine.c:145-150,
    # and JAM()'s default branch is a bare CLK++, S maincpu.c:606-628) or
    # the event was dropped; either is the defect this test exists to
    # catch, and both fail it.
    with pytest.raises(TransportError, match="jammed") as excinfo:
        t.wait_for_stopped(timeout=15)

    # VICE reports the jam with reg_pc still on the offending opcode.  The
    # 3.10 frame is bodiless (S ``monitor_binary.c:389`` passes length 0
    # despite the manual's 2-byte PC body), so on this build the address
    # comes from the register-read fallback; the machine is sitting in
    # the monitor and must answer it.  Either way the message names it.
    assert f"jammed at ${JAM_ADDR:04x}" in str(excinfo.value), str(excinfo.value)
