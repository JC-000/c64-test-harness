#!/usr/bin/env python3
"""Probe CIA TOD behavior in VICE under warp -- regression check.

Reads CIA1 TOD ($DC08-$DC0B) before and after a wall-clock sleep, once
with warp=False and once with warp=True.  Originally written to test
the hypothesis that TOD might be wall-clocked under warp (and therefore
usable as a warp-immune 6502-side timeout source for bridge_ping poll
loops).  Both possible "TOD is unusable" outcomes disprove the hypothesis:

  (a) TOD never advances at all.  This is what we observe in the default
      VICE 3.10 + sound=False + BASIC-READY configuration: per the 6526
      datasheet TOD must be written to start, and KERNAL+BASIC at READY
      never starts it -- both CIA1 and CIA2 stay pinned at 01:00:00.00
      regardless of warp.

  (b) Even if started, TOD in VICE 3.10 is virtual-CPU-clocked: it
      accelerates with warp at the same factor as jiffy ($A0-$A2)
      (~30x at default warp).  See MEMORY.md.

Either outcome means a 6502-side timeout cannot ride on TOD.  The fix
in 0.10.2 pushes the wall clock to the host (Python) via the
``c64_test_harness.poll_until`` module -- see docs/bridge_networking.md.

This script is kept as a regression check so that if a future VICE
release changes either TOD startup behaviour or its clock domain we
will notice.

CIA1 TOD register map:
    $DC08 - 10ths of second (BCD).  Reading UNLATCHES.
    $DC09 - seconds (BCD, 0-59)
    $DC0A - minutes (BCD, 0-59)
    $DC0B - hours + AM/PM bit 7.  Reading LATCHES.

Canonical read order: $DC0B (latch), $DC0A, $DC09, $DC08 (unlatch).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

# Allow running from worktree without install
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from c64_test_harness.backends.vice_lifecycle import ViceConfig, ViceProcess  # noqa: E402
from c64_test_harness.backends.vice_manager import PortAllocator  # noqa: E402
from c64_test_harness.memory import read_bytes  # noqa: E402

sys.path.insert(0, str(ROOT / "tests"))
from conftest import connect_binary_transport  # noqa: E402


def bcd_to_int(b: int) -> int:
    return ((b >> 4) & 0x0F) * 10 + (b & 0x0F)


def read_tod(transport) -> tuple[int, int, int, int]:
    """Read CIA1 TOD in canonical order.

    Returns (hours_raw, minutes_raw, seconds_raw, tenths_raw).
    Reads $DC0B first to latch, $DC08 last to unlatch.
    """
    hours = read_bytes(transport, 0xDC0B, 1)[0]
    minutes = read_bytes(transport, 0xDC0A, 1)[0]
    seconds = read_bytes(transport, 0xDC09, 1)[0]
    tenths = read_bytes(transport, 0xDC08, 1)[0]
    return hours, minutes, seconds, tenths


def tod_to_tenths(h: int, m: int, s: int, t: int) -> int:
    """Convert raw BCD TOD to total tenths since top of hour."""
    return bcd_to_int(m) * 600 + bcd_to_int(s) * 10 + bcd_to_int(t)


def format_tod(h: int, m: int, s: int, t: int) -> str:
    return (
        f"h={h:#04x} m={m:#04x}({bcd_to_int(m):02d}) "
        f"s={s:#04x}({bcd_to_int(s):02d}) t={t:#04x}({t & 0x0F}) "
        f"tenths_since_hour={tod_to_tenths(h, m, s, t)}"
    )


def run_case(label: str, warp: bool) -> int:
    print(f"\n=== Case: {label} (warp={warp}) ===", flush=True)
    allocator = PortAllocator(port_range_start=6511, port_range_end=6531)
    port = allocator.allocate()
    reservation = allocator.take_socket(port)
    if reservation is not None:
        reservation.close()
    config = ViceConfig(port=port, warp=warp, sound=False, minimize=True)
    with ViceProcess(config) as vice:
        transport = connect_binary_transport(port, proc=vice)
        try:
            # Let BASIC settle so TOD has been running
            transport.resume()
            time.sleep(1.5)

            t0 = read_tod(transport)
            tenths0 = tod_to_tenths(*t0)
            print(f"start: {format_tod(*t0)}", flush=True)

            transport.resume()
            t_sleep_start = time.monotonic()
            time.sleep(2.0)
            wall = time.monotonic() - t_sleep_start

            t1 = read_tod(transport)
            tenths1 = tod_to_tenths(*t1)
            print(f"end:   {format_tod(*t1)}", flush=True)

            delta = (tenths1 - tenths0) % 36000
            print(
                f"wall sleep: {wall:.3f}s  |  TOD delta: {delta} tenths "
                f"({delta / 10.0:.2f}s)",
                flush=True,
            )
            return delta
        finally:
            transport.close()
            allocator.release(port)


def main() -> int:
    delta_normal = run_case("normal speed", warp=False)
    delta_warp = run_case("warp mode", warp=True)

    print("\n=== Summary ===")
    print(f"  normal delta: {delta_normal} tenths ({delta_normal / 10.0:.2f}s)")
    print(f"  warp   delta: {delta_warp} tenths ({delta_warp / 10.0:.2f}s)")

    # Interpretation
    if delta_normal == 0 and delta_warp == 0:
        print("VERDICT: TOD never started (default BASIC-READY config). "
              "Confirms case (a) -- TOD is unusable as a 6502-side wall clock.")
        return 0
    if 15 <= delta_normal <= 30 and 15 <= delta_warp <= 30:
        print("UNEXPECTED: TOD appears wall-clocked in both modes. "
              "If you see this, re-check whether VICE has fixed the TOD "
              "clock-domain bug -- the host-side wall-clock pattern in "
              "poll_until.py would still be needed for U64 generality, but "
              "VICE-only code could move back to TOD.")
        return 0
    if delta_warp > delta_normal * 5:
        print("VERDICT: TOD accelerates under warp. Confirms case (b) -- "
              "TOD is virtual-CPU-clocked and unusable as a warp-immune source.")
        return 0
    print("VERDICT: unexpected result, review raw numbers above.")
    return 3


if __name__ == "__main__":
    sys.exit(main())
