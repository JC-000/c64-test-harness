"""Does a monitor-HELD CPU pin LIN/CYC, or do they keep advancing?

docs/vice_upstream_bugs.md bug 6 rules out "the monitor is holding the
CPU" on the grounds that a frozen raster means nothing is being emulated.
That inference was never measured directly.  Two arms, same machine:

  held    -- consecutive read_registers with NO resume between them
  running -- read_registers with a resume + sleep between them
"""
from __future__ import annotations
import sys, time

sys.path.insert(0, "/Users/someone/Documents/c64-test-harness/src")
sys.path.insert(0, "/Users/someone/Documents/c64-test-harness/tests")

from c64_test_harness.backends.vice_lifecycle import ViceConfig, ViceProcess
from c64_test_harness.backends.vice_manager import PortAllocator
from conftest import connect_binary_transport


def sample(t, n, resume_between):
    out = []
    for _ in range(n):
        r = t.read_registers()
        out.append((r.get("LIN", -1), r.get("CYC", -1)))
        if resume_between:
            t.resume()
        time.sleep(0.10)
    return out


def main():
    allocator = PortAllocator(port_range_start=6511, port_range_end=6531)
    port = allocator.allocate()
    res = allocator.take_socket(port)
    if res is not None:
        res.close()
    cfg = ViceConfig(port=port, warp=False, sound=False)
    vice = ViceProcess(cfg)
    vice.start()
    try:
        t = connect_binary_transport(port, proc=vice)
        time.sleep(3.0)          # let the KERNAL reach READY.
        t.resume()
        time.sleep(1.0)

        held = sample(t, 8, resume_between=False)
        print("HELD    (no resume between reads):")
        for p in held:
            print(f"    LIN={p[0]:<4} CYC={p[1]}")
        print(f"  distinct positions: {len(set(held))}")

        t.resume()
        time.sleep(0.3)
        running = sample(t, 8, resume_between=True)
        print("RUNNING (resume between reads):")
        for p in running:
            print(f"    LIN={p[0]:<4} CYC={p[1]}")
        print(f"  distinct positions: {len(set(running))}")

        print()
        print("VERDICT:")
        if len(set(held)) == 1 and len(set(running)) > 1:
            print("  A monitor-HELD CPU PINS LIN/CYC exactly like the stall.")
            print("  => a frozen raster does NOT by itself prove the machine")
            print("     stopped emulating; it is also what a held CPU looks like.")
        elif len(set(held)) > 1:
            print("  LIN/CYC ADVANCE while the monitor holds the CPU.")
            print("  => bug 6's inference stands: frozen raster means not emulating.")
        else:
            print("  Inconclusive: the running arm did not advance either.")
    finally:
        vice.stop()


if __name__ == "__main__":
    main()
