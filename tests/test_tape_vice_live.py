"""The TAP encoder and the pulse stimulus on a real KERNAL, under VICE (issue #584).

``tests/test_tape.py`` checks both offline: the encoder against a decoder
written from the format's description, the stimulus on a small 6502 model.
Neither is a C64. Here the KERNAL itself loads a TAP built by
``build_tap_from_prg`` from VICE's datasette, and the stimulus routine runs
on VICE's 6510 and VIC.

First run 2026-11-27 (VICE 3.10): both tests pass, and the load test was
shown able to fail by mutating the encoder in a scratch copy, one VICE run
per mutation: a medium pulse as short as a short one, or only 0x38 long
(the program never arrives); a wrong block checksum (ST=$20); a data byte
changed after the checksum (ST=$20, one byte differs); the repeat copies
left out (never arrives). Leaving out only the data block's repeat copy was
NOT caught until the wait on $2D/$2E below was added: the bytes are in RAM
after the first copy, while the KERNAL is still waiting for the second.

VICE's autostart of a ``.tap`` resets, types ``LOAD``, presses play and
then ``RUN``. The KERNAL reads the pulses: the result depends on the pulse
lengths, with the device-1 traps on or off.
"""

from __future__ import annotations

import pytest

from c64_test_harness.backends.vice_lifecycle import ViceConfig, ViceProcess
from c64_test_harness.backends.vice_manager import PortAllocator
from c64_test_harness.memory import read_bytes, wait_for_memory
from c64_test_harness.tape import (
    STIMULUS_DONE,
    build_tap_from_prg,
    build_tape_pulse_stimulus,
    run_tape_stimulus,
    tape_test_program,
)
from conftest import connect_binary_transport, require_vice_or_skip
from live_fixture_teardown import attempt_steps, raise_teardown_failures

pytestmark = pytest.mark.vice_live


def test_the_kernal_loads_a_tap_built_from_a_prg(tmp_path):
    """VICE autostarts the TAP: the KERNAL reads header and data from the
    datasette, and the program is in RAM with the end pointer at its end."""
    require_vice_or_skip()
    prg = tape_test_program()
    tap = tmp_path / "known.tap"
    tap.write_bytes(build_tap_from_prg(prg, name="KNOWN"))

    allocator = PortAllocator(port_range_start=6511, port_range_end=6531)
    port = allocator.allocate()
    reservation = allocator.take_socket(port)
    if reservation is not None:
        reservation.close()
    # +trapdevice1: no KERNAL traps for device 1, so nothing but the
    # datasette's pulses can deliver the program. (VICE uses those traps for
    # .t64 images; the load fails the same way on a mis-timed TAP with them
    # on or off, so this only pins what was measured.)
    config = ViceConfig(port=port, warp=True, sound=False, prg_path=str(tap),
                        extra_args=["+trapdevice1"])
    failures: list = []
    with ViceProcess(config) as vice:
        transport = None
        try:
            transport = connect_binary_transport(port, proc=vice)
            # The last two bytes of the program are the BASIC end marker, so
            # the last line's terminator and text are what arrive last.
            tail = prg.data[-12:-2]
            got = wait_for_memory(transport, prg.end_address - 12, tail, timeout=180.0,
                                  poll_interval=0.25)
            assert got is not None, "the program never appeared at $0801"
            # The bytes are in RAM after the first of the data block's two
            # copies. BASIC moves its end-of-program pointer ($2D/$2E) only
            # when the KERNAL's LOAD has returned, after the second copy.
            end = prg.end_address
            done = wait_for_memory(transport, 0x002D, bytes((end & 0xFF, end >> 8)),
                                   timeout=60.0, poll_interval=0.25)
            assert done is not None, (
                "BASIC never took the program's end from a finished LOAD "
                f"(ST=${read_bytes(transport, 0x0090, 1)[0]:02X}): a tape error, or the "
                "KERNAL is still waiting for the block's second copy")
            check = prg.verify_load(transport)
            assert check.complete, check
            assert not check.status & 0x3C, f"tape error in ST: ${check.status:02X}"
        finally:
            failures = attempt_steps([
                ("transport.close()", transport.close if transport is not None else None),
                (f"allocator.release({port})", lambda: allocator.release(port)),
            ])
    raise_teardown_failures("tape vice teardown", failures)


def test_the_stimulus_runs_to_completion_and_restores_the_machine(binary_transport):
    """The routine returns to BASIC, reports done, and leaves
    ``$D011`` and ``$D015`` as it found them."""
    t = binary_transport
    stim = build_tape_pulse_stimulus(2000, tap_value=0x30)
    assert run_tape_stimulus(t, stim, reset=True, margin=3.0) is True
    assert read_bytes(t, stim.done_address, 1)[0] == STIMULUS_DONE
    assert read_bytes(t, 0xD011, 1)[0] & 0x10 == 0x10, "the screen was left blanked"
    assert read_bytes(t, 0xD015, 1)[0] == 0x00
