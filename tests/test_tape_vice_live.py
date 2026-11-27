"""The TAP encoder and the pulse stimulus on a real KERNAL, under VICE (issue #584).

``tests/test_tape.py`` checks both offline: the encoder against a decoder
written from the format's description, the stimulus on a small 6502 model.
Neither is a C64. Here the KERNAL itself loads a TAP built by
``build_tap_from_prg`` from VICE's datasette, and the stimulus routine runs
on VICE's 6510 and VIC.

**Written but not yet run** (the lane that wrote it could not spawn VICE);
the first run is the supervisor's. If the autostart of a ``.tap`` behaves
differently from what is assumed below (reset, ``LOAD``, play, ``RUN``),
that is a fault in this test, not evidence about the encoder.
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
    config = ViceConfig(port=port, warp=True, sound=False, prg_path=str(tap))
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
