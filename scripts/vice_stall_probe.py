"""Reproduce the TestKeyboard wedge and interrogate the halted machine.

Runs the minimal sequence the fixture runs, in a loop, until the CPU is
found pinned at $CF00.  Then asks the questions the failure report
cannot: how many resumes does it take to move (if any), what checkpoints
does VICE think are set, and what does the event queue hold.
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

CMD_CHECKPOINT_LIST = 0x14


def restore_basic(t):
    t.write_memory(0xCF00, bytes([0x58, 0x4C, 0xCD, 0xE5]))
    t.set_registers({"PC": 0xCF00})
    t.resume()
    time.sleep(0.5)


def pc(t):
    return t.read_registers().get("PC", -1)


def is_wedged(t, samples=6):
    """Wedged means the CPU never leaves the stub at $CF00.

    Not "the PC repeated": the BASIC idle loop is ~8 bytes, so repeated
    samples there are coincidence, not a wedge.  Measured that false
    positive on the first run of this probe.
    """
    seen = []
    for _ in range(samples):
        seen.append(pc(t))
        if seen[-1] != 0xCF00:
            return False, seen
        t.resume()
        time.sleep(0.1)
    return True, seen


def interrogate(t):
    print("\n=== INTERROGATION ===", flush=True)

    # Exercise the PRODUCTION detector against this genuine stall.  Stubs
    # can show that `_emulator_is_stalled` distinguishes a constant raster
    # from an advancing one; only a real stall shows it firing on the
    # thing it was built for, and the stall is far easier to induce here
    # than through the test suite.
    import test_vice_core as tc
    stalled, raster = tc._emulator_is_stalled(t)
    print(f"\n*** tests/test_vice_core.py::_emulator_is_stalled -> "
          f"stalled={stalled} raster={raster}", flush=True)
    print("*** production failure report follows:", flush=True)
    print(tc._machine_failure_report(t, "<verification>"), flush=True)

    print(f"registers: {t.read_registers()}", flush=True)
    stub = t.read_memory(0xCF00, 8)
    print(f"memory at $CF00: {stub.hex()}  (stub should be 584ccde5)",
          flush=True)
    print(f"checkpoints: {t.checkpoint_list()}", flush=True)

    # LIN/CYC are the raster position: they advance whenever the *machine*
    # is emulating, whether or not the CPU is executing.  Frozen LIN means
    # the whole emulator is halted, not just the 6510.
    print("\n-- is the machine emulating at all? (raster position) --",
          flush=True)
    for i in range(5):
        r = t.read_registers()
        print(f"  LIN={r.get('LIN')} CYC={r.get('CYC')} PC={r.get('PC'):#06x}",
              flush=True)
        t.resume()
        time.sleep(0.2)

    print("\n-- does it move with more resumes? --", flush=True)
    moved_at = None
    start = pc(t)
    for i in range(1, 41):
        t.resume()
        time.sleep(0.1)
        now = pc(t)
        if now != start:
            moved_at = i
            print(f"  moved after {i} resumes: {start:#06x} -> {now:#06x}",
                  flush=True)
            break
    if moved_at is None:
        print(f"  still pinned at {start:#06x} after 40 resumes", flush=True)

    print("\n-- what checkpoints does VICE think are set? --", flush=True)
    try:
        t._send_command(CMD_CHECKPOINT_LIST, b"")
        deadline = time.monotonic() + 3.0
        seen = 0
        while time.monotonic() < deadline:
            try:
                r = t._recv_response()
            except Exception:
                break
            print(f"  resp type={r.response_type:#04x} err={r.error_code:#04x} "
                  f"len={len(r.body)} body={r.body[:24].hex()}", flush=True)
            seen += 1
            if r.response_type == 0x14:
                break
        if not seen:
            print("  (no response)", flush=True)
    except Exception as e:
        print(f"  checkpoint list failed: {type(e).__name__}: {e}", flush=True)

    print(f"\n-- transport event queue: {len(t._event_queue)} entries, "
          f"resume_generation={t._resume_generation} --", flush=True)
    import collections
    kinds = collections.Counter(r.response_type for _, r in t._event_queue)
    for k in sorted(kinds):
        note = {0x31: "REGISTER_INFO", 0x62: "STOPPED", 0x63: "RESUMED",
                0x61: "*** JAM ***"}.get(k, "")
        print(f"  type={k:#04x} x{kinds[k]:<5} {note}", flush=True)
    jam = [(g, r) for g, r in t._event_queue if r.response_type == 0x61]
    print(f"\n  JAM (0x61) events in queue: {len(jam)}", flush=True)
    for g, r in jam[:5]:
        print(f"    gen={g} len={len(r.body)} body={r.body.hex()}", flush=True)

    print(f"\n-- JAMAction resource: {t.resource_get('JAMAction')!r} --",
          flush=True)


def main():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    cfg = ViceConfig(port=port, warp=True, sound=False)
    proc = ViceProcess(cfg)
    proc.start()
    try:
        t = connect_binary_transport(port, proc=proc, timeout=30)
        for cycle in range(1, 400):
            restore_basic(t)
            wedged, seen = is_wedged(t)
            if wedged:
                print(f"\nWEDGED at cycle {cycle}: PC "
                      f"{[hex(p) for p in seen]}", flush=True)
                interrogate(t)
                break
            # Mimic the test body so the sequence matches the real one.
            send_text(t, "PRINT 2+3\r")
            t.resume()
            for _ in range(3):
                ScreenGrid.from_transport(t)
                t.resume()
                time.sleep(0.2)
            if cycle % 25 == 0:
                print(f"  cycle {cycle}: healthy", flush=True)
        else:
            print("never wedged", flush=True)
        t.close()
    finally:
        proc.stop()


if __name__ == "__main__":
    main()
