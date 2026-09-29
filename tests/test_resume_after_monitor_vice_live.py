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

from c64_test_harness import wait_for_memory, watch_progress
from c64_test_harness.backends.vice_lifecycle import ViceConfig, ViceProcess
from c64_test_harness.execute import goto
from c64_test_harness.memory import read_bytes, write_bytes
from c64_test_harness.screen import wait_for_stable, wait_for_text
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


#: A program living inside the staging window: INC $CB00 / BNE / INC $CB01 /
#: JMP $1000.  Its counter sits outside the window, so it survives the
#: extract's write-back only if the program itself kept running correctly.
PROG_ADDR, PROG_COUNTER = 0x1000, 0xCB00
PROG_CODE = bytes([
    0xEE, 0x00, 0xCB,   # INC $CB00
    0xD0, 0xFB,         # BNE $1000
    0xEE, 0x01, 0xCB,   # INC $CB01
    0x4C, 0x00, 0x10,   # JMP $1000
])
KIL = 0x02
REU_BANKS = 4


def _seed_reu(t, banks: list[bytes]) -> None:
    for i, data in enumerate(banks):
        off = i * STAGING_LEN
        t.write_memory(STAGING, data)
        t.write_memory(0xDF02, bytes([STAGING & 0xFF, STAGING >> 8,
                                      off & 0xFF, (off >> 8) & 0xFF, off >> 16,
                                      STAGING_LEN & 0xFF, STAGING_LEN >> 8, 0, 0]))
        t.write_memory(0xDF01, bytes([0x90]))   # execute, FF00 off, C64->REU
        t.resume()
        time.sleep(0.1)


def _pc(t) -> int:
    return t.read_registers()["PC"]


@pytest.mark.parametrize("settle", [0.0, 0.05])
def test_extract_does_not_run_reu_data_in_a_program_inside_the_window(
    reu_transport, settle
):
    # Review of PR #516: with the resume that makes x64sc perform the DMA,
    # a program executing inside $0800-$87FF ran the REU bytes -- a KIL-filled
    # bank jammed it 3/3.  The extract must park the CPU outside the window.
    t = reu_transport
    banks = [bytes([KIL]) * STAGING_LEN] + [_pattern(17 * i) for i in range(1, REU_BANKS)]
    _seed_reu(t, banks)
    in_ram = bytearray(_pattern(101))
    in_ram[PROG_ADDR - STAGING:PROG_ADDR - STAGING + len(PROG_CODE)] = PROG_CODE
    in_ram = bytes(in_ram)
    t.write_memory(STAGING, in_ram)
    t.write_memory(PROG_COUNTER, bytes([0, 0]))
    goto(t, PROG_ADDR)
    time.sleep(0.1)

    for _ in range(2):  # 8 bank transfers per parametrisation
        before = t.read_registers()  # halts; the extract must hand this back
        got = extract_reu_contents(t, REU_BANKS * STAGING_LEN, settle=settle)
        assert got == b"".join(banks), "extract returned the wrong bytes"
        after = t.read_registers()
        assert {k: after[k] for k in ("PC", "SP", "FL", "A", "X", "Y")} == {
            k: before[k] for k in ("PC", "SP", "FL", "A", "X", "Y")
        }, f"registers changed across the extract: {before} -> {after}"
        t.resume()
        time.sleep(0.1)

    assert t.read_memory(STAGING, STAGING_LEN) == in_ram, "window not restored"
    # The machine has run freely since the last extract, so the halt can
    # land in the KERNAL IRQ handler; what must not happen is a PC left in
    # the window outside the program (a KIL jam parks it on a $02).
    pc = _pc(t)
    assert (PROG_ADDR <= pc < PROG_ADDR + len(PROG_CODE)
            or not STAGING <= pc < STAGING + STAGING_LEN), (
        f"program not running: PC ${pc:04X} (a KIL jam parks it on $02)"
    )
    seen = t.read_memory(PROG_COUNTER, 2)
    t.resume()
    time.sleep(0.2)
    assert t.read_memory(PROG_COUNTER, 2) != seen, "program stopped counting"


# -- stale monitor traps (#516 re-verify) -------------------------------------
#
# VICE queues a monitor trap at every vsync that finds a command waiting on
# the socket (monitor.c monitor_vsync_hook -> monitor_startup_trap), and
# interrupt_do_trap runs every queued trap back to back.  A command sent
# while the CPU is stalled across a vsync -- a 32 KB REU DMA stalls it for
# ~1.67 PAL frames -- therefore queues a spare trap, which re-enters the
# monitor the moment the next EXIT leaves it: that resume runs nothing.

C2_COUNTER = 0xC200
#: INC $C200 / BNE / INC $C201 / JMP $C100 -- outside the staging window.
C2_LOOP = bytes([0xEE, 0x00, 0xC2, 0xD0, 0xFB, 0xEE, 0x01, 0xC2, 0x4C, 0x00, 0xC1])


def _lda_sta(value: int, addr: int) -> bytes:
    return bytes([0xA9, value, 0x8D, addr & 0xFF, addr >> 8])


#: At $C000: program a 32 KB C64->REU transfer from $0800, fire it, count,
#: repeat -- a guest that keeps the CPU stalled across vsyncs.
GUEST_DMA = (
    _lda_sta(0x00, 0xDF02) + _lda_sta(0x08, 0xDF03) + _lda_sta(0, 0xDF04)
    + _lda_sta(0, 0xDF05) + _lda_sta(0, 0xDF06) + _lda_sta(0, 0xDF07)
    + _lda_sta(0x80, 0xDF08) + _lda_sta(0x90, 0xDF01)
    + bytes([0xEE, 0x00, 0xC2, 0xD0, 0x03, 0xEE, 0x01, 0xC2, 0x4C, 0x00, 0xC0])
)
TRIALS = 8


def _one_resume_runs(t) -> bool:
    before = t.read_memory(C2_COUNTER, 2)
    t.resume()
    time.sleep(0.1)
    return t.read_memory(C2_COUNTER, 2) != before


def test_one_resume_after_an_extract_runs_the_program(reu_transport):
    t = reu_transport
    t.write_memory(0xC100, C2_LOOP)
    ran = 0
    for _ in range(TRIALS):
        goto(t, 0xC100)
        time.sleep(0.05)
        extract_reu_contents(t, STAGING_LEN, settle=0)
        ran += _one_resume_runs(t)
    assert ran == TRIALS, f"the first resume after an extract ran {ran}/{TRIALS}"


def test_waiters_leave_a_dma_heavy_guest_running(reu_transport):
    t = reu_transport
    t.write_memory(0xC000, GUEST_DMA)
    wfm_running = watch_running = 0
    for _ in range(TRIALS):
        goto(t, 0xC000)
        time.sleep(0.05)
        seen: list[bytes] = []
        assert wait_for_memory(t, C2_COUNTER, lambda d: seen.append(d) or False,
                               length=2, timeout=0.3, poll_interval=0.005) is None
        time.sleep(0.2)
        wfm_running += t.read_memory(C2_COUNTER, 2) != seen[-1]

        goto(t, 0xC000)
        time.sleep(0.05)
        last = None
        for event in watch_progress(t, {"c": (C2_COUNTER, 2)}, poll_interval=0.005,
                                    idle_timeout=5.0, overall_timeout=0.3):
            last = event.values.get("c", last)
        time.sleep(0.2)
        watch_running += t.read_memory(C2_COUNTER, 2) != last
    assert (wfm_running, watch_running) == (TRIALS, TRIALS), (
        f"running on exit: wait_for_memory {wfm_running}/{TRIALS}, "
        f"watch_progress {watch_running}/{TRIALS}"
    )


class _CounterTap:
    """Delegates to a transport; after every screen read it also reads the
    counter, inside the same halt, so the test knows the value the waiter's
    last read saw."""

    def __init__(self, t) -> None:
        self._t = t
        self.last: bytes | None = None

    def __getattr__(self, name):
        return getattr(self._t, name)

    def read_screen_codes(self) -> list[int]:
        codes = self._t.read_screen_codes()
        self.last = self._t.read_memory(C2_COUNTER, 2)
        return codes


def test_screen_waiters_leave_a_dma_heavy_guest_running(reu_transport):
    # Same exposure through the screen waiters' exit resume: "READY." is on
    # screen and the screen is static, so both waiters match while the
    # guest keeps the CPU in back-to-back REU DMAs.
    t = reu_transport
    t.write_memory(0xC000, GUEST_DMA)
    text_running = stable_running = 0
    for _ in range(TRIALS):
        for waiter in ("text", "stable"):
            goto(t, 0xC000)
            time.sleep(0.05)
            tap = _CounterTap(t)
            if waiter == "text":
                # Matches on the first read, which goto() left to arrive
                # while the guest runs -- and so, usually, mid-DMA.
                assert wait_for_text(tap, "READY.", timeout=5.0,
                                     poll_interval=0.005,
                                     verbose=False) is not None
            else:
                assert wait_for_stable(tap, timeout=5.0, poll_interval=0.005,
                                       stable_count=2) is not None
            time.sleep(0.2)
            moved = t.read_memory(C2_COUNTER, 2) != tap.last
            if waiter == "text":
                text_running += moved
            else:
                stable_running += moved
    assert (text_running, stable_running) == (TRIALS, TRIALS), (
        f"running on exit: wait_for_text {text_running}/{TRIALS}, "
        f"wait_for_stable {stable_running}/{TRIALS}"
    )


# -- cost of the confirmation, and a text monitor on the side (#516 round 3) --

EXIT_COST_TRIALS = 20
#: The clean path listens for the whole stale-trap window; a stale Stopped
#: was measured 0.08-0.24 ms after the EXIT.  A first-poll match without
#: confirmation takes ~1-6 ms, so 20 ms leaves room and still fails a 50 ms
#: window.
EXIT_COST_MEDIAN_LIMIT = 0.020


def test_a_confirmed_waiter_exit_stays_cheap(binary_transport):
    t = binary_transport
    t.resume()
    assert wait_for_text(t, "READY.", timeout=15.0, poll_interval=0.2,
                         verbose=False) is not None
    costs = []
    for _ in range(EXIT_COST_TRIALS):
        start = time.monotonic()
        assert wait_for_text(t, "READY.", timeout=5.0, poll_interval=0.01,
                             verbose=False) is not None
        costs.append(time.monotonic() - start)
    median = sorted(costs)[len(costs) // 2]
    assert median < EXIT_COST_MEDIAN_LIMIT, (
        f"median first-poll wait_for_text {median * 1000:.1f} ms"
    )


@pytest.fixture
def text_monitor_transport():
    """VICE with the binary monitor and a remote text monitor, booted."""
    require_vice_or_skip()
    allocator = PortAllocator(port_range_start=6511, port_range_end=6531)
    port = allocator.allocate()
    text_port = allocator.allocate()
    for p in (port, text_port):
        reservation = allocator.take_socket(p)
        if reservation is not None:
            reservation.close()
    config = ViceConfig(port=port, text_monitor_port=text_port, warp=True,
                        sound=False, minimize=True)
    failures: list = []
    with ViceProcess(config) as vice:
        transport = None
        try:
            transport = connect_binary_transport(port, proc=vice,
                                                 text_monitor_port=text_port)
            transport.resume()
            assert wait_for_text(transport, "READY.", timeout=15.0,
                                 poll_interval=0.2, verbose=False) is not None
            yield transport
        finally:
            failures = attempt_steps([
                ("transport.close()", transport.close if transport is not None else None),
                (f"allocator.release({port})", lambda: allocator.release(port)),
                (f"allocator.release({text_port})", lambda: allocator.release(text_port)),
            ])
    raise_teardown_failures("text_monitor_transport teardown", failures)


def test_a_text_monitor_stop_is_not_taken_for_a_stale_trap(text_monitor_transport):
    # A text-monitor command enters the monitor too, and the binary monitor
    # reports that entry as a bare Stopped.  Issued from another thread in
    # the middle of a confirmation window it was classified "stale" and
    # resumed over (5/5, #516 round 3).  Nothing here stalls the CPU, so
    # every "stale" verdict is a misreading.
    import threading

    t = text_monitor_transport
    verdicts: list[str] = []
    real_after_exit = t._after_exit

    def spy(window):
        v = real_after_exit(window)
        verdicts.append(v)
        return v

    t._after_exit = spy
    stop = threading.Event()
    errors: list[BaseException] = []

    wrong: list[int] = []
    calls = [0]

    def hammer() -> None:
        # VICE was started with warp on, so every reply must say so.  Before
        # the marker-matched reply, a stray prompt from the binary traffic
        # was taken as the reply ~18 times in 65k calls (#516).
        try:
            while not stop.is_set():
                calls[0] += 1
                if t.get_warp() is not True:
                    wrong.append(calls[0])
        except BaseException as exc:  # surfaced below
            errors.append(exc)

    worker = threading.Thread(target=hammer, daemon=True)
    worker.start()
    try:
        for _ in range(40):
            assert wait_for_text(t, "READY.", timeout=5.0, poll_interval=0.01,
                                 verbose=False) is not None
    finally:
        stop.set()
        worker.join(timeout=10.0)
    assert not errors, errors
    assert verdicts, "no confirmed resume happened"
    assert "stale" not in verdicts, verdicts
    assert not wrong, f"get_warp() misread {len(wrong)} of {calls[0]} replies"
