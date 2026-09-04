"""How often does ``jsr()`` hijack a CPU that is inside the KERNAL IRQ handler?

Issue #183 justified making ``preserve_state=True`` the default for
:func:`c64_test_harness.execute.jsr` with a measurement, and issue #188
points out that the measurement shipped without a reproducer and that two
contradictory figure sets were recorded (10 events / 119 stack bytes in
the published issue, 18 / 123 in the docstring as first committed).

This is that reproducer.  It is a probe, not a test: occupancy is a
property of *what the machine is running*, so the numbers below are only
meaningful together with the conditions the script prints.

The instrument
--------------
The CPU is parked in a controlled idle loop in RAM at ``$C900``, clear of
every span in ``memory_policy.HARNESS_SCRATCH`` (the trampoline stays at
its default ``$0334``).  Before each call the probe reads the registers,
which halts the 6510 wherever it happens to be -- the very halt ``jsr()``
is about to hijack.  Classification is then exact and needs no PC range
table for the KERNAL: with a bare idle loop the only other code running
is the interrupt handler, so

    PC outside the parked loop  <=>  the halt landed mid-interrupt

``SP`` at the same halt says *where* in the handler: the hardware pushes
PCH/PCL/P (3 bytes) on entry and the ``$FF48`` dispatcher then pushes
A/X/Y (3 more), so a frame costs 3 bytes if the halt landed before those
pushes and 6+ after.  That is the 4-12 byte range #183 quotes, and it is
what decides whether 6.8 or 11.9 bytes per event is the plausible mean.

The arms
--------
``leak``     -- ``preserve_state=False``, PC forced back to the loop by
                hand afterwards.  This is the pre-#183 behaviour: the
                caller's program carries on, but every mid-IRQ call
                abandons a frame.  Measures occupancy and stack cost.
``preserve`` -- ``preserve_state=True``, today's default.  Control: the
                same workload must show no SP walk at all.
``no-cli``   -- like ``leak``, but the idle loop enables interrupts once
                and never again, so the first abandoned frame masks IRQs
                permanently.  Reproduces the frozen jiffy clock.

Usage::

    python3 scripts/jsr_mid_irq_occupancy_probe.py [--calls N] [--warp]
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time

REPO = "/Users/someone/Documents/c64-test-harness"
sys.path.insert(0, f"{REPO}/src")
sys.path.insert(0, f"{REPO}/tests")

from c64_test_harness.backends.vice_lifecycle import ViceConfig, ViceProcess
from c64_test_harness.backends.vice_manager import PortAllocator
from c64_test_harness.execute import jsr
from conftest import connect_binary_transport

#: Parked idle loop.  Clear of every HARNESS_SCRATCH span, and clear of
#: the $0334 trampoline.
LOOP_ADDR = 0xC900
#: The subroutine jsr() calls: a bare RTS.  Nothing about the workload
#: should depend on what it does.
TARGET_ADDR = 0xC910

#: LDX #$FF; TXS; [CLI; NOP; JMP back-to-CLI]  -- interrupts re-enabled
#: on every pass, so an abandoned frame's stuck I flag is cleared again.
LOOP_CLI = bytes([0xA2, 0xFF, 0x9A, 0x58, 0xEA, 0x4C, 0x03, 0xC9])
#: LDX #$FF; TXS; CLI; [NOP; NOP; JMP past-the-CLI] -- interrupts enabled
#: exactly once, so the first abandoned frame masks them for good.
LOOP_NO_CLI = bytes([0xA2, 0xFF, 0x9A, 0x58, 0xEA, 0xEA, 0x4C, 0x04, 0xC9])

#: Where the leak arm re-enters the loop after a call.  It must be PAST
#: the ``LDX #$FF / TXS``: re-entering at the top would rebuild the stack
#: pointer on every iteration and erase the very SP walk being measured
#: (and, in the no-cli arm, re-run the one-shot ``CLI``).
LOOP_BODY = {id(LOOP_CLI): LOOP_ADDR + 3, id(LOOP_NO_CLI): LOOP_ADDR + 4}

JIFFY = 0xA0


def read_jiffy(t) -> int:
    hi, mid, lo = t.read_memory(JIFFY, 3)
    return (hi << 16) | (mid << 8) | lo


def park(t, loop: bytes) -> tuple[int, int]:
    """Write the idle loop, jump into it, return its PC bounds."""
    t.write_memory(LOOP_ADDR, loop)
    t.write_memory(TARGET_ADDR, bytes([0x60]))  # RTS
    t.set_registers({"PC": LOOP_ADDR})
    t.resume()
    time.sleep(0.2)
    return LOOP_ADDR, LOOP_ADDR + len(loop) - 1


def run_arm(t, name: str, loop: bytes, calls: int, preserve: bool, settle: float):
    lo, hi = park(t, loop)
    body = LOOP_BODY[id(loop)]

    # Baseline SP, sampled at a halt that is *in* the loop so no interrupt
    # frame is live.  Retries because the first halt may itself be mid-IRQ.
    baseline_sp = None
    for _ in range(20):
        regs = t.read_registers()
        if lo <= regs["PC"] <= hi:
            baseline_sp = regs["SP"]
            break
        t.resume()
        time.sleep(0.01)
    if baseline_sp is None:
        raise RuntimeError(f"{name}: could not sample a baseline SP inside the loop")

    jiffy_start = read_jiffy(t)
    events: list[dict] = []
    jiffy_frozen_at = None
    last_jiffy = jiffy_start
    frozen_since = None

    t.resume()
    for i in range(calls):
        regs = t.read_registers()
        pc, sp = regs["PC"], regs["SP"]
        mid_irq = not (lo <= pc <= hi)
        if mid_irq:
            events.append({"i": i, "pc": pc, "sp": sp})

        jsr(t, TARGET_ADDR, preserve_state=preserve)

        if not preserve:
            # The pre-#183 caller: PC forced, SP/FL left as the routine
            # found them.  Put the machine back in its loop by hand so the
            # workload continues -- this is the behaviour under test.
            t.set_registers({"PC": body})
        t.resume()
        time.sleep(settle)

        if i % 25 == 0:
            j = read_jiffy(t)
            if j == last_jiffy and frozen_since is None:
                frozen_since = i
            elif j != last_jiffy:
                frozen_since = None
            if frozen_since is not None and jiffy_frozen_at is None and i - frozen_since >= 50:
                jiffy_frozen_at = frozen_since
            last_jiffy = j

    # Final SP, again sampled inside the loop.
    final_sp = None
    for _ in range(40):
        regs = t.read_registers()
        if lo <= regs["PC"] <= hi:
            final_sp = regs["SP"]
            break
        t.resume()
        time.sleep(0.01)
    jiffy_end = read_jiffy(t)

    walked = None
    if final_sp is not None:
        walked = (baseline_sp - final_sp) % 256

    print(f"\n=== arm: {name}  (preserve_state={preserve}, {calls} calls) ===")
    print(f"  mid-IRQ halts     : {len(events)} of {calls} "
          f"({100.0 * len(events) / calls:.2f}%)")
    print(f"  SP  ${baseline_sp:02X} -> ${(final_sp if final_sp is not None else 0):02X}"
          f"   walked {walked} bytes")
    if events and walked is not None:
        print(f"  bytes per event   : {walked / len(events):.1f}")
    print(f"  jiffy {jiffy_start} -> {jiffy_end}  (+{jiffy_end - jiffy_start})")
    if jiffy_frozen_at is not None:
        print(f"  jiffy FROZE around iteration {jiffy_frozen_at}")
    if events:
        first = events[0]
        print(f"  first event       : i={first['i']} PC=${first['pc']:04X} SP=${first['sp']:02X}")
        sps = [e["sp"] for e in events]
        print(f"  event SPs         : {' '.join(f'${s:02X}' for s in sps[:20])}"
              f"{' ...' if len(sps) > 20 else ''}")
        depth = [(baseline_sp - s) % 256 for s in sps]
        print(f"  frame depth at halt: min={min(depth)} max={max(depth)} "
              f"median={statistics.median(depth)}")
        pcs = sorted({e["pc"] for e in events})
        print(f"  distinct PCs      : {' '.join(f'${p:04X}' for p in pcs[:16])}"
              f"{' ...' if len(pcs) > 16 else ''}")
    return {"name": name, "events": len(events), "calls": calls, "walked": walked}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--calls", type=int, default=1400,
                    help="calls per arm (issue #183 used 1400)")
    ap.add_argument("--settle", type=float, default=0.002,
                    help="seconds the machine runs between halts")
    ap.add_argument("--warp", action="store_true", help="run VICE in warp mode")
    args = ap.parse_args()

    allocator = PortAllocator(port_range_start=6511, port_range_end=6531)
    port = allocator.allocate()
    res = allocator.take_socket(port)
    if res is not None:
        res.close()

    cfg = ViceConfig(port=port, warp=args.warp, sound=False)
    vice = ViceProcess(cfg)
    vice.start()
    try:
        t = connect_binary_transport(port, proc=vice)
        time.sleep(3.0)  # let the KERNAL reach READY
        t.resume()
        time.sleep(1.0)

        print("conditions")
        print(f"  x64sc            : {vice.executable if hasattr(vice, 'executable') else 'PATH x64sc'}")
        print(f"  warp             : {args.warp}")
        print(f"  calls per arm    : {args.calls}")
        print(f"  settle per call  : {args.settle}s")
        print(f"  idle loop        : ${LOOP_ADDR:04X}, target RTS ${TARGET_ADDR:04X}, "
              f"trampoline $0334 (default)")
        print(f"  SP sampled at    : the pre-call halt, i.e. the halt jsr() hijacks")

        summary = [
            run_arm(t, "leak (pre-#183)", LOOP_CLI, args.calls, False, args.settle),
            run_arm(t, "preserve (today)", LOOP_CLI, args.calls, True, args.settle),
            run_arm(t, "no-cli (leak)", LOOP_NO_CLI, args.calls, False, args.settle),
        ]

        print("\nsummary")
        for s in summary:
            rate = 100.0 * s["events"] / s["calls"]
            print(f"  {s['name']:<18} {s['events']:>4}/{s['calls']} "
                  f"({rate:.2f}%)  SP walked {s['walked']}")
    finally:
        vice.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
