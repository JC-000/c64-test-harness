#!/usr/bin/env python3
"""Probe CIA TOD behaviour in VICE and (optionally) on Ultimate 64 Elite.

TOD ($DC08-$DC0B on CIA1) is a candidate 6502-side wall-clock source for
network poll timeouts.  Per the MOS 6526 datasheet TOD powers up stopped
and must be written to start it; KERNAL+BASIC at READY never starts it,
so a naive read of TOD in a fresh VICE or U64E yields 01:00:00.00
indefinitely.  The ``start_tod`` helper here writes the canonical
clear-CRB + hours + minutes + seconds + tenths sequence so subsequent
reads observe the real clock domain.

Findings that motivated the host-side-wall-clock design in
``c64_test_harness.poll_until`` (see docs/bridge_networking.md):

* **VICE 3.10**: once started, TOD is virtual-CPU-clocked.  Measured:
  normal=3.0s TOD per 3.0s wall; warp=94.1s TOD per 3.0s wall
  (~31x acceleration, matches jiffy warp factor).  TOD is NOT a
  wall-clock source in VICE -- it accelerates with the emulated CPU.

* **Real Ultimate 64 Elite**: once started, TOD is a true wall clock,
  decoupled from the CPU.  Measured flat across the full 1-48 MHz turbo
  range (tod/wall ratio ~1.0 at every setting), matching proper 6526
  behaviour where TOD is driven by the 50/60 Hz mains reference.

Consequence: code that must work under VICE warp AND on real U64 hardware
cannot use TOD as a 6502-side deadline.  Host-side wall-clock timeouts
with bounded 6502 peek bursts are the only pattern that generalises.  On
U64 alone, TOD would be a valid pure-6502 timeout source at any turbo
speed -- worth banking if host round-trip latency ever becomes a
bottleneck for UCI peek routines.

This script is kept as a regression check: if a future VICE release
moves TOD onto a wall-clock domain, or if U64 firmware regresses and
couples TOD to the CPU clock, we want to notice.

CIA1 TOD register map::

    $DC08 - 10ths of second (BCD).  Reading UNLATCHES.
    $DC09 - seconds (BCD, 0-59)
    $DC0A - minutes (BCD, 0-59)
    $DC0B - hours + AM/PM bit 7.  Reading LATCHES.
    $DC0F - CRB; bit 7 = 0 means writes set TOD, = 1 means writes set alarm.

Canonical read order: $DC0B (latch), $DC0A, $DC09, $DC08 (unlatch).
Canonical start sequence: clear $DC0F bit 7, then write $DC0B, $DC0A,
$DC09, $DC08 (writing tenths unlatches and starts the counter).

Usage::

    python3 scripts/verify_tod_warp.py                 # VICE normal + warp
    U64_HOST=192.168.1.81 python3 scripts/verify_tod_warp.py --u64   # add U64 probe
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# Allow running from worktree without install
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from c64_test_harness.backends.vice_lifecycle import ViceConfig, ViceProcess  # noqa: E402
from c64_test_harness.backends.vice_manager import PortAllocator  # noqa: E402
from c64_test_harness.memory import read_bytes, write_bytes  # noqa: E402

sys.path.insert(0, str(ROOT / "tests"))
from conftest import connect_binary_transport  # noqa: E402


MEASURE_WALL_S = 3.0


def bcd_to_int(b: int) -> int:
    return ((b >> 4) & 0x0F) * 10 + (b & 0x0F)


def read_tod(transport) -> tuple[int, int, int, int]:
    """Read CIA1 TOD in canonical latch order.  Returns raw BCD bytes."""
    hours = read_bytes(transport, 0xDC0B, 1)[0]
    minutes = read_bytes(transport, 0xDC0A, 1)[0]
    seconds = read_bytes(transport, 0xDC09, 1)[0]
    tenths = read_bytes(transport, 0xDC08, 1)[0]
    return hours, minutes, seconds, tenths


def start_tod(transport) -> None:
    """Start CIA1 TOD at 00:00:00.0 per the 6526 datasheet sequence.

    Clears CRB bit 7 (``$DC0F``) to put writes in "set TOD" mode, then
    writes hours (latches), minutes, seconds, and tenths (unlatches and
    starts the counter).
    """
    crb = read_bytes(transport, 0xDC0F, 1)[0]
    if crb & 0x80:
        write_bytes(transport, 0xDC0F, [crb & 0x7F])
    write_bytes(transport, 0xDC0B, [0x00])
    write_bytes(transport, 0xDC0A, [0x00])
    write_bytes(transport, 0xDC09, [0x00])
    write_bytes(transport, 0xDC08, [0x00])


def tod_to_tenths(h: int, m: int, s: int, t: int) -> int:
    """Convert raw BCD TOD to total tenths since top of hour."""
    return (
        bcd_to_int(h & 0x7F) * 36000
        + bcd_to_int(m) * 600
        + bcd_to_int(s) * 10
        + (t & 0x0F)
    )


def format_tod(h: int, m: int, s: int, t: int) -> str:
    return (
        f"{bcd_to_int(h & 0x7F):02d}:{bcd_to_int(m):02d}:"
        f"{bcd_to_int(s):02d}.{t & 0x0F}"
    )


def measure(read_tod_fn, start_tod_fn, resume_fn=None) -> tuple[float, float]:
    """Start TOD, sleep, read deltas.  Returns (wall_s, tod_delta_s)."""
    start_tod_fn()
    if resume_fn is not None:
        resume_fn()
    time.sleep(0.2)
    t0 = time.monotonic()
    start = read_tod_fn()
    if resume_fn is not None:
        resume_fn()
    time.sleep(MEASURE_WALL_S)
    end = read_tod_fn()
    wall = time.monotonic() - t0
    delta = tod_to_tenths(*end) - tod_to_tenths(*start)
    # Handle hour rollover
    if delta < 0:
        delta += 360000
    return wall, delta / 10.0


def run_vice_case(label: str, warp: bool) -> tuple[float, float]:
    print(f"\n=== VICE: {label} (warp={warp}) ===", flush=True)
    allocator = PortAllocator(port_range_start=6511, port_range_end=6531)
    port = allocator.allocate()
    reservation = allocator.take_socket(port)
    if reservation is not None:
        reservation.close()
    config = ViceConfig(port=port, warp=warp, sound=False, minimize=True)
    with ViceProcess(config) as vice:
        transport = connect_binary_transport(port, proc=vice)
        try:
            transport.resume()
            time.sleep(1.0)
            wall, tod_delta = measure(
                read_tod_fn=lambda: read_tod(transport),
                start_tod_fn=lambda: start_tod(transport),
                resume_fn=lambda: transport.resume(),
            )
            start = read_tod(transport)
            print(f"wall: {wall:.2f}s   tod delta: {tod_delta:.1f}s   "
                  f"ratio: {tod_delta / wall:.2f}x   "
                  f"(final clock: {format_tod(*start)})", flush=True)
            return wall, tod_delta
        finally:
            transport.close()
            allocator.release(port)


def run_u64_case(host: str) -> tuple[float, float]:
    from c64_test_harness.backends.ultimate64 import Ultimate64Transport
    print(f"\n=== Ultimate 64 Elite: {host} ===", flush=True)
    transport = Ultimate64Transport(host=host)

    def _read():
        h = transport.read_memory(0xDC0B, 1)[0]
        m = transport.read_memory(0xDC0A, 1)[0]
        s = transport.read_memory(0xDC09, 1)[0]
        t = transport.read_memory(0xDC08, 1)[0]
        return h, m, s, t

    def _start():
        crb = transport.read_memory(0xDC0F, 1)[0]
        if crb & 0x80:
            transport.write_memory(0xDC0F, bytes([crb & 0x7F]))
        transport.write_memory(0xDC0B, bytes([0x00]))
        transport.write_memory(0xDC0A, bytes([0x00]))
        transport.write_memory(0xDC09, bytes([0x00]))
        transport.write_memory(0xDC08, bytes([0x00]))

    wall, tod_delta = measure(_read, _start)
    final = _read()
    print(f"wall: {wall:.2f}s   tod delta: {tod_delta:.1f}s   "
          f"ratio: {tod_delta / wall:.2f}x   "
          f"(final clock: {format_tod(*final)})", flush=True)
    return wall, tod_delta


def classify(ratio: float) -> str:
    if ratio < 0.1:
        return "STOPPED (TOD never started or emulation stalled)"
    if 0.85 <= ratio <= 1.15:
        return "WALL-CLOCK (decoupled from CPU)"
    if ratio > 5.0:
        return "CPU-CLOCKED (accelerates with emulated CPU)"
    return "UNEXPECTED"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--u64", action="store_true",
                        help="Also probe Ultimate 64 at $U64_HOST (or --u64-host)")
    parser.add_argument("--u64-host", default=os.environ.get("U64_HOST", ""),
                        help="U64 host (falls back to $U64_HOST)")
    args = parser.parse_args()

    results: list[tuple[str, float, float]] = []

    wall, delta = run_vice_case("normal speed", warp=False)
    results.append(("VICE normal", wall, delta))
    wall, delta = run_vice_case("warp mode", warp=True)
    results.append(("VICE warp", wall, delta))

    if args.u64:
        if not args.u64_host:
            print("\n--u64 given but no host (set U64_HOST or --u64-host)",
                  file=sys.stderr)
            return 2
        wall, delta = run_u64_case(args.u64_host)
        results.append((f"U64 ({args.u64_host})", wall, delta))

    print("\n=== Summary ===")
    print(f"{'target':<20} {'wall':>8} {'tod Δ':>10} {'ratio':>8}   verdict")
    print("-" * 72)
    for label, wall, delta in results:
        ratio = delta / wall if wall > 0 else 0.0
        print(f"{label:<20} {wall:>7.2f}s {delta:>9.1f}s {ratio:>7.2f}x   "
              f"{classify(ratio)}")

    print("\nExpected:")
    print("  VICE normal  : ratio ~1.0  (wall ≈ tod)")
    print("  VICE warp    : ratio ~30x  (TOD accelerates with emulated CPU)")
    print("  U64 hardware : ratio ~1.0  (TOD is true wall-clock at any turbo)")
    print("\nSee MEMORY.md 'CIA TOD — emulation platform differences' for context.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
