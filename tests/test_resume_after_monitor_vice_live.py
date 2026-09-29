"""Library pollers that must resume VICE after a halting monitor command.

Issue #514's sweep.  On VICE every binary-monitor command halts the 6510
at the next vsync until ``resume()``.  Two library routines depended on
the machine moving between their own commands and never resumed:

* ``watch_progress`` polled ``read_memory`` in a loop, so the watched
  program froze at the first poll and the watcher reported ``Stalled``
  and then ``Timeout`` for a program that was making progress.
* ``extract_reu_contents`` programs an REU->C64 transfer through the REC
  registers and reads the staging window back.  In x64sc the command
  write only pulls BA; ``reu_dma_start`` runs from the CPU loop
  (``c64/cart/reu.c`` ``reu_dma``, ``mainc64cpu.c``), which does not run
  while the monitor holds the machine -- so the read-back returned the
  staging window's own RAM, not the REU.
"""

from __future__ import annotations

import time

import pytest

from c64_test_harness import watch_progress
from c64_test_harness.backends.vice_lifecycle import ViceConfig, ViceProcess
from c64_test_harness.execute import goto
from c64_test_harness.memory import read_bytes, write_bytes
from c64_test_harness.screen import wait_for_text
from c64_test_harness.snapshot import extract_reu_contents
from c64_test_harness.backends.vice_manager import PortAllocator

from conftest import connect_binary_transport, require_vice_or_skip
from live_fixture_teardown import attempt_steps, raise_teardown_failures

pytestmark = pytest.mark.vice_live

COUNTER_ENTRY, COUNTER_ADDR = 0xCA00, 0xCAF0
#: INC lo / BNE +3 / INC hi / JMP loop -- a 16-bit counter that never stops.
COUNTER_CODE = bytes([
    0xEE, 0xF0, 0xCA,   # INC $CAF0
    0xD0, 0x03,         # BNE $CA08
    0xEE, 0xF1, 0xCA,   # INC $CAF1
    0x4C, 0x00, 0xCA,   # JMP $CA00
])

STAGING, STAGING_LEN = 0x0800, 0x8000


@pytest.fixture
def reu_transport():
    """VICE with a 128 KB REU, booted to READY."""
    require_vice_or_skip()
    allocator = PortAllocator(port_range_start=6511, port_range_end=6531)
    port = allocator.allocate()
    reservation = allocator.take_socket(port)
    if reservation is not None:
        reservation.close()
    config = ViceConfig(port=port, warp=True, sound=False, minimize=True,
                        extra_args=["-reu", "-reusize", "128"])
    failures: list = []
    with ViceProcess(config) as vice:
        transport = None
        try:
            transport = connect_binary_transport(port, proc=vice)
            transport.resume()
            assert wait_for_text(transport, "READY.", timeout=15.0,
                                 poll_interval=0.2, verbose=False) is not None
            yield transport
        finally:
            failures = attempt_steps([
                ("transport.close()", transport.close if transport is not None else None),
                (f"allocator.release({port})", lambda: allocator.release(port)),
            ])
    raise_teardown_failures("reu_transport teardown", failures)


def test_watch_progress_sees_a_running_program_advance(binary_transport):
    t = binary_transport
    t.resume()
    assert wait_for_text(t, "READY.", timeout=15.0, poll_interval=0.2,
                         verbose=False) is not None
    write_bytes(t, COUNTER_ENTRY, COUNTER_CODE)
    write_bytes(t, COUNTER_ADDR, bytes([0, 0]))
    goto(t, COUNTER_ENTRY)

    kinds = []
    for event in watch_progress(t, {"counter": (COUNTER_ADDR, 2)},
                                poll_interval=0.05, idle_timeout=0.5,
                                overall_timeout=1.5):
        kinds.append(event.kind)
    # A program that counts every cycle changes between every pair of
    # polls; a frozen one reports one baseline Advanced, then Stalled.
    assert "Stalled" not in kinds, kinds
    assert kinds.count("Advanced") >= 5, kinds
    assert kinds[-1] == "Timeout", kinds
    # The generator's last poll resumed: the counter still moves.
    seen = read_bytes(t, COUNTER_ADDR, 2)
    t.resume()
    time.sleep(0.2)
    assert read_bytes(t, COUNTER_ADDR, 2) != seen


def _pattern(seed: int) -> bytes:
    return bytes((i * 7 + seed) & 0xFF for i in range(STAGING_LEN))


def test_extract_reu_contents_returns_the_reu_not_the_staging_window(reu_transport):
    t = reu_transport
    in_reu = _pattern(3)
    in_ram = _pattern(101)
    # Seed the REU: stage the pattern, C64->REU through the REC, and let
    # the CPU run so x64sc actually performs the DMA.
    t.write_memory(STAGING, in_reu)
    t.write_memory(0xDF02, bytes([STAGING & 0xFF, STAGING >> 8, 0, 0, 0,
                                  STAGING_LEN & 0xFF, STAGING_LEN >> 8, 0, 0]))
    t.write_memory(0xDF01, bytes([0x90]))   # execute, FF00 off, C64->REU
    t.resume()
    time.sleep(0.2)
    # Then give the staging window different bytes.
    t.write_memory(STAGING, in_ram)
    assert t.read_memory(STAGING, STAGING_LEN) == in_ram

    got = extract_reu_contents(t, STAGING_LEN)

    assert got[:16] == in_reu[:16], (
        f"extract returned {got[:16].hex()} -- staging RAM starts "
        f"{in_ram[:16].hex()}, REU starts {in_reu[:16].hex()}"
    )
    assert got == in_reu
    # And the staging window was put back.
    assert t.read_memory(STAGING, STAGING_LEN) == in_ram
