"""What does TestScreen leave behind that breaks TestKeyboard?

The bisect localised it: TestKeyboard alone never fails (0/50), but with
TestScreen in front of it the first keyboard test fails at the module's
base rate. So this replays that exact order in one process and reads the
machine between the two, which is the state the bisect made worth
looking at.
"""
from __future__ import annotations

import socket
import sys
import time

sys.path.insert(0, "/Users/someone/Documents/c64-test-harness/src")
sys.path.insert(0, "/Users/someone/Documents/c64-test-harness/tests")

from c64_test_harness.backends.vice_lifecycle import ViceConfig, ViceProcess
from c64_test_harness.keyboard import send_text
from c64_test_harness.screen import ScreenGrid
from conftest import connect_binary_transport

KEYCOUNT, KEYBUF = 0x00C6, 0x0277


def restore_basic(t):
    t.write_memory(0xCF00, bytes([0x58, 0x4C, 0xCD, 0xE5]))
    t.set_registers({"PC": 0xCF00})
    t.resume()
    time.sleep(0.5)


def state(t, label):
    c6 = t.read_memory(KEYCOUNT, 1)[0]
    kb = t.read_memory(KEYBUF, 10)
    pc = t.read_registers().get("PC", -1)
    return f"{label:34} $C6={c6:2d} $0277={kb.hex()} PC={pc:#06x}"


def wait_for(t, needle, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if needle.upper() in ScreenGrid.from_transport(t).continuous_text().upper():
            return True
        t.resume()
        time.sleep(1.0)
    return False


def cycle(t) -> tuple[bool, list[str]]:
    trace = []

    # --- TestScreen, in order ---
    restore_basic(t)                                   # test_screen_grid_reads
    ScreenGrid.from_transport(t)
    trace.append(state(t, "after screen_grid_reads"))

    restore_basic(t)                                   # test_wait_for_text
    send_text(t, 'PRINT"HELLO VICE"\r')
    t.resume()
    found = wait_for(t, "HELLO VICE")
    trace.append(state(t, f"after wait_for_text(found={found})"))

    restore_basic(t)                                   # test_wait_for_stable
    ScreenGrid.from_transport(t)
    trace.append(state(t, "after wait_for_stable"))

    restore_basic(t)                                   # test_dump_screen
    ScreenGrid.from_transport(t).dump("t")
    trace.append(state(t, "after dump_screen  <- TestScreen ends"))

    # --- TestKeyboard's first test ---
    restore_basic(t)
    trace.append(state(t, "after TestKeyboard restore_basic"))
    send_text(t, "PRINT 2+3\r")
    trace.append(state(t, "after send_text('PRINT 2+3')"))
    t.resume()
    ok = wait_for(t, "5")
    trace.append(state(t, f"after wait_for('5') found={ok}"))
    return ok, trace


def main():
    # A FRESH VICE per cycle, because pytest starts one per run and the
    # failure appears in the first keyboard test after boot.  Reusing one
    # emulator across cycles makes every cycle after the first a
    # different, warmed situation -- which is why an earlier version of
    # this probe reported 119 clean cycles that were really one faithful
    # trial and 118 unfaithful ones.
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    for i in range(1, n + 1):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        cfg = ViceConfig(port=port, warp=True, sound=False)
        proc = ViceProcess(cfg)
        proc.start()
        try:
            t = connect_binary_transport(port, proc=proc, timeout=30)
            try:
                ok, trace = cycle(t)
                if not ok:
                    print(f"\n*** FAILED at cycle {i} ***", flush=True)
                    for line in trace:
                        print("   ", line, flush=True)
                    print(ScreenGrid.from_transport(t).dump("at failure"),
                          flush=True)
                    return
                if i % 5 == 0:
                    print(f"  cycle {i}: ok", flush=True)
            finally:
                t.close()
        finally:
            proc.stop()
    print(f"never reproduced in {n} fresh-VICE cycles", flush=True)


if __name__ == "__main__":
    main()
