#!/usr/bin/env python3
"""Benchmark X25519 scalar multiplication across Ultimate 64 turbo speeds.

Loads x25519.prg onto real Ultimate 64 hardware, runs a full scalar*basepoint
multiplication at each CPU speed, measures jiffy clock timing, and verifies
correctness against RFC 7748.

Requires U64_HOST environment variable (and optionally U64_PASSWORD).

Usage:
    U64_HOST=192.168.1.81 python3 scripts/bench_x25519_u64_turbo.py
    U64_HOST=192.168.1.81 python3 scripts/bench_x25519_u64_turbo.py --all
    U64_HOST=192.168.1.81 python3 scripts/bench_x25519_u64_turbo.py --speeds 48,16,4,1
    U64_HOST=192.168.1.81 python3 scripts/bench_x25519_u64_turbo.py --timeout 1800
"""
from __future__ import annotations

import argparse
import os
import sys
import time

# ---------------------------------------------------------------------------
# Project imports
# ---------------------------------------------------------------------------

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from c64_test_harness.backends.ultimate64 import Ultimate64Transport
from c64_test_harness.backends.ultimate64_client import Ultimate64Client
from c64_test_harness.backends.ultimate64_helpers import (
    get_turbo_mhz,
    set_turbo_mhz,
    set_reu,
    snapshot_state,
    restore_state,
)
from c64_test_harness.labels import Labels
from c64_test_harness.screen import wait_for_text

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PRG_PATH = "/home/someone/c64-x25519/build/x25519.prg"
LABELS_PATH = "/home/someone/c64-x25519/build/labels.txt"

# All 16 supported U64 turbo speeds, fastest first.
ALL_SPEEDS = [48, 40, 32, 24, 20, 16, 14, 12, 10, 8, 6, 5, 4, 3, 2, 1]

# Default subset: covers the range without taking forever at slow speeds.
DEFAULT_SPEEDS = [48, 32, 16, 8, 4, 2, 1]

# RFC 7748 test vector: scalar * basepoint (9)
RFC7748_SCALAR = bytes.fromhex(
    "a546e36bf0527c9d3b16154b82465edd62144c0ac1fc5a18506a2244ba449ac4"
)
RFC7748_EXPECTED = bytes.fromhex(
    "1c9fd88f45606d932a80c71824ae151d15d73e77de38e8e000852e614fae7019"
)

# Memory addresses for the benchmark harness.
SENTINEL_ADDR = 0x0350
SENTINEL_VALUE = 0x42
BENCH_SUB = 0x0360

# Jiffy clock tick rate (CIA-driven, nominally 60 Hz on NTSC).
JIFFY_HZ = 60.0

# ---------------------------------------------------------------------------
# 6502 machine code builder
# ---------------------------------------------------------------------------


def _build_bench_subroutine(
    x25519_base: int,
    bench_ticks: int,
    vic_blank: int,
    vic_unblank: int,
) -> bytes:
    """Build 6502 machine code for the benchmark subroutine at BENCH_SUB.

    Layout:
        SEI
        LDA #$00 ; STA $A0 ; STA $A1 ; STA $A2   -- zero jiffy clock
        CLI
        JSR vic_blank                               -- blank screen for speed
        JSR x25519_base                             -- run the multiplication
        SEI
        LDA $A0 ; STA bench_ticks+0                -- copy jiffy clock
        LDA $A1 ; STA bench_ticks+1
        LDA $A2 ; STA bench_ticks+2
        CLI
        JSR vic_unblank                             -- restore screen
        LDA #$42 ; STA $0350                       -- sentinel
        JMP *                                       -- park CPU
    """
    code = bytearray()

    def emit(*bs: int) -> None:
        code.extend(bs)

    def emit_jsr(addr: int) -> None:
        emit(0x20, addr & 0xFF, (addr >> 8) & 0xFF)

    def emit_lda_imm(val: int) -> None:
        emit(0xA9, val & 0xFF)

    def emit_sta_abs(addr: int) -> None:
        emit(0x8D, addr & 0xFF, (addr >> 8) & 0xFF)

    def emit_lda_abs(addr: int) -> None:
        emit(0xAD, addr & 0xFF, (addr >> 8) & 0xFF)

    # SEI
    emit(0x78)
    # Zero jiffy clock ($A0 = hours, $A1 = minutes, $A2 = jiffies — big-endian)
    emit_lda_imm(0x00)
    emit_sta_abs(0x00A0)
    emit_sta_abs(0x00A1)
    emit_sta_abs(0x00A2)
    # CLI
    emit(0x58)
    # JSR vic_blank — blank VIC screen for faster computation
    emit_jsr(vic_blank)
    # JSR x25519_base — the actual scalar multiplication
    emit_jsr(x25519_base)
    # SEI
    emit(0x78)
    # Copy jiffy clock to bench_ticks (3 bytes, big-endian)
    emit_lda_abs(0x00A0)
    emit_sta_abs(bench_ticks)
    emit_lda_abs(0x00A1)
    emit_sta_abs(bench_ticks + 1)
    emit_lda_abs(0x00A2)
    emit_sta_abs(bench_ticks + 2)
    # CLI
    emit(0x58)
    # JSR vic_unblank — restore screen
    emit_jsr(vic_unblank)
    # LDA #$42; STA $0350 — sentinel to signal completion
    emit_lda_imm(SENTINEL_VALUE)
    emit_sta_abs(SENTINEL_ADDR)
    # JMP * — park CPU in infinite loop (address = current PC)
    park_addr = BENCH_SUB + len(code)
    emit(0x4C, park_addr & 0xFF, (park_addr >> 8) & 0xFF)

    return bytes(code)


# ---------------------------------------------------------------------------
# Polling helper
# ---------------------------------------------------------------------------


def poll_sentinel(
    transport: Ultimate64Transport,
    timeout: float,
    poll_interval: float = 0.5,
) -> bool:
    """Poll SENTINEL_ADDR until it reads SENTINEL_VALUE or timeout expires.

    Returns True on success, False on timeout.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = transport.read_memory(SENTINEL_ADDR, 1)
        if data and data[0] == SENTINEL_VALUE:
            return True
        time.sleep(poll_interval)
    return False


# ---------------------------------------------------------------------------
# Single benchmark run
# ---------------------------------------------------------------------------


def run_one_speed(
    client: Ultimate64Client,
    transport: Ultimate64Transport,
    prg_data: bytes,
    labels: Labels,
    mhz: int,
    timeout: float,
) -> dict | None:
    """Run X25519 benchmark at the given speed.  Returns a result dict or None on failure."""

    x25519_base = labels["x25519_base"]
    x25_scalar = labels["x25_scalar"]
    x25_result = labels["x25_result"]
    bench_ticks = labels["bench_ticks"]
    vic_blank = labels["vic_blank"]
    vic_unblank = labels["vic_unblank"]
    main_loop = labels["main_loop"]

    print(f"\n{'='*60}")
    print(f"  {mhz} MHz")
    print(f"{'='*60}")

    # 1. Set turbo FIRST, then load program
    print(f"  Setting turbo to {mhz} MHz ...", flush=True)
    set_turbo_mhz(client, mhz)
    time.sleep(0.5)

    # 2. Load and run the PRG (resets + loads + auto-starts)
    print("  Loading x25519.prg ...", flush=True)
    client.run_prg(prg_data)
    time.sleep(2.0)

    # 3. Verify program actually started by polling main_loop for JMP $082A
    #    (not just screen text, which can be stale from a previous run)
    print("  Waiting for program init ...", flush=True)
    boot_deadline = time.monotonic() + 120.0
    while time.monotonic() < boot_deadline:
        ml = transport.read_memory(main_loop, 3)
        if ml == bytes([0x4C, 0x2A, 0x08]):
            break
        time.sleep(0.5)
    else:
        print(f"  ERROR: Program did not start (main_loop={ml.hex()}). Skipping.")
        return None
    time.sleep(1.0)  # settle after init

    print("  Setting up benchmark ...", flush=True)

    # 4. Write scalar to x25_scalar via DMA
    transport.write_memory(x25_scalar, RFC7748_SCALAR)

    # 5. Build and write benchmark subroutine
    bench_code = _build_bench_subroutine(x25519_base, bench_ticks, vic_blank, vic_unblank)
    transport.write_memory(BENCH_SUB, bench_code)

    # 6. Zero sentinel
    transport.write_memory(SENTINEL_ADDR, bytes([0x00]))

    # 7. Overwrite main_loop entry with JMP BENCH_SUB to redirect CPU
    #    JMP $0360 = 4C 60 03
    jmp_code = bytes([0x4C, BENCH_SUB & 0xFF, (BENCH_SUB >> 8) & 0xFF])
    transport.write_memory(main_loop, jmp_code)

    # 8. Measure wall-clock time while polling for sentinel
    wall_start = time.monotonic()
    print(f"  Running X25519 (timeout {timeout:.0f}s) ...", end="", flush=True)

    # Adaptive poll interval: faster polling for fast speeds, slower for slow
    if mhz >= 16:
        poll_iv = 0.5
    elif mhz >= 4:
        poll_iv = 2.0
    else:
        poll_iv = 5.0

    completed = poll_sentinel(transport, timeout, poll_interval=poll_iv)
    wall_end = time.monotonic()
    wall_secs = wall_end - wall_start

    if not completed:
        print(f" TIMEOUT after {wall_secs:.1f}s")
        return None

    print(f" done ({wall_secs:.1f}s wall)")

    # 9. Read bench_ticks (3 bytes, big-endian) and result (32 bytes)
    ticks_raw = transport.read_memory(bench_ticks, 3)
    jiffies = (ticks_raw[0] << 16) | (ticks_raw[1] << 8) | ticks_raw[2]

    result_bytes = transport.read_memory(x25_result, 32)

    # 10. Verify correctness
    correct = result_bytes == RFC7748_EXPECTED
    c64_secs = jiffies / JIFFY_HZ

    status = "PASS" if correct else "FAIL"
    print(f"  Jiffies: {jiffies}  ({c64_secs:.2f}s @ {JIFFY_HZ} Hz)")
    print(f"  Wall time: {wall_secs:.2f}s")
    print(f"  Result: {status}")
    if not correct:
        print(f"  Expected: {RFC7748_EXPECTED.hex()}")
        print(f"  Got:      {result_bytes.hex()}")

    return {
        "mhz": mhz,
        "jiffies": jiffies,
        "c64_secs": c64_secs,
        "wall_secs": wall_secs,
        "correct": correct,
        "result_hex": result_bytes.hex(),
    }


# ---------------------------------------------------------------------------
# Summary table
# ---------------------------------------------------------------------------


def print_summary(results: list[dict]) -> None:
    """Print a formatted summary table of all results."""
    if not results:
        print("\nNo results to summarize.")
        return

    # Find 1 MHz result for speedup calculation
    base_jiffies = None
    for r in results:
        if r["mhz"] == 1:
            base_jiffies = r["jiffies"]
            break

    print(f"\n{'='*76}")
    print("  X25519 Benchmark Summary — Ultimate 64")
    print(f"{'='*76}")

    hdr = f"  {'MHz':>4s}  {'Jiffies':>8s}  {'C64 time':>9s}  {'Wall':>8s}  {'Status':>6s}"
    if base_jiffies is not None:
        hdr += f"  {'Speedup':>8s}"
    print(hdr)
    print(f"  {'-'*4}  {'-'*8}  {'-'*9}  {'-'*8}  {'-'*6}", end="")
    if base_jiffies is not None:
        print(f"  {'-'*8}", end="")
    print()

    for r in results:
        c64_time = f"{r['c64_secs']:.2f}s"
        wall_time = f"{r['wall_secs']:.1f}s"
        status = "PASS" if r["correct"] else "FAIL"
        line = f"  {r['mhz']:>4d}  {r['jiffies']:>8d}  {c64_time:>9s}  {wall_time:>8s}  {status:>6s}"
        if base_jiffies is not None and r["jiffies"] > 0:
            speedup = base_jiffies / r["jiffies"]
            line += f"  {speedup:>7.1f}x"
        print(line)

    print(f"{'='*76}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Benchmark X25519 on Ultimate 64 across turbo speeds."
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="Run all 16 turbo speeds (default: 48,32,16,8,4,2,1).",
    )
    p.add_argument(
        "--speeds",
        type=str,
        default=None,
        help="Comma-separated list of MHz values, e.g. '48,16,4,1'.",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=1200.0,
        help="Per-speed timeout in seconds (default: 1200 = 20 min).",
    )
    p.add_argument(
        "--prg",
        type=str,
        default=PRG_PATH,
        help=f"Path to x25519.prg (default: {PRG_PATH}).",
    )
    p.add_argument(
        "--labels",
        type=str,
        default=LABELS_PATH,
        help=f"Path to labels.txt (default: {LABELS_PATH}).",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    # Determine speeds to run
    if args.speeds:
        speeds = [int(s.strip()) for s in args.speeds.split(",")]
        # Validate all speeds
        valid = {48, 40, 32, 24, 20, 16, 14, 12, 10, 8, 6, 5, 4, 3, 2, 1}
        for s in speeds:
            if s not in valid:
                print(f"ERROR: {s} MHz is not a valid U64 CPU speed.")
                print(f"Valid speeds: {sorted(valid)}")
                sys.exit(1)
    elif args.all:
        speeds = list(ALL_SPEEDS)
    else:
        speeds = list(DEFAULT_SPEEDS)

    # Sort fastest first
    speeds.sort(reverse=True)

    # Connect to U64
    host = os.environ.get("U64_HOST")
    if not host:
        print("ERROR: U64_HOST environment variable not set.")
        print("Usage: U64_HOST=192.168.1.81 python3 scripts/bench_x25519_u64_turbo.py")
        sys.exit(1)
    password = os.environ.get("U64_PASSWORD")

    # Load PRG and labels
    print(f"Loading PRG: {args.prg}")
    with open(args.prg, "rb") as f:
        prg_data = f.read()
    print(f"  PRG size: {len(prg_data)} bytes (load addr: ${prg_data[0] | (prg_data[1] << 8):04X})")

    print(f"Loading labels: {args.labels}")
    labels = Labels.from_file(args.labels)
    print(f"  {len(labels)} labels loaded")

    # Verify required labels exist
    required_labels = [
        "x25519_base", "x25_scalar", "x25_result",
        "bench_ticks", "vic_blank", "vic_unblank", "main_loop",
    ]
    for name in required_labels:
        if name not in labels:
            print(f"ERROR: Required label '{name}' not found in {args.labels}")
            sys.exit(1)
        print(f"  {name} = ${labels[name]:04X}")

    # Build bench subroutine (show it once for debug)
    bench_code = _build_bench_subroutine(
        labels["x25519_base"],
        labels["bench_ticks"],
        labels["vic_blank"],
        labels["vic_unblank"],
    )
    print(f"\nBenchmark subroutine: {len(bench_code)} bytes at ${BENCH_SUB:04X}")

    # Connect to device
    print(f"\nConnecting to U64 at {host} ...")
    client = Ultimate64Client(host=host, password=password, timeout=30.0)
    transport = Ultimate64Transport(host=host, password=password, client=client)

    # Verify connectivity
    try:
        info = client.get_info()
        product = info.get("product", "unknown")
        firmware = info.get("firmware_version", "unknown")
        print(f"  Connected: {product}, firmware {firmware}")
    except Exception as e:
        print(f"ERROR: Cannot reach U64 at {host}: {e}")
        sys.exit(1)

    # Snapshot original state for restore
    print("  Snapshotting turbo state ...")
    original_state = snapshot_state(client)
    original_mhz = get_turbo_mhz(client)
    print(f"  Original turbo: {original_mhz} MHz" if original_mhz else "  Original turbo: Off")

    # Enable REU — x25519 program requires 512 KB REU for lookup tables
    print("  Enabling REU (512 KB) ...")
    set_reu(client, enabled=True, size="512 KB")
    time.sleep(0.5)

    print(f"\nWill benchmark {len(speeds)} speed(s): {speeds}")
    print(f"Per-speed timeout: {args.timeout:.0f}s")

    # Run benchmarks
    results: list[dict] = []
    try:
        for mhz in speeds:
            result = run_one_speed(
                client, transport, prg_data, labels, mhz, args.timeout,
            )
            if result is not None:
                results.append(result)
    except KeyboardInterrupt:
        print("\n\nInterrupted by user.")
    finally:
        # Restore original turbo state
        print(f"\nRestoring original turbo state ...")
        try:
            restore_state(client, original_state)
            restored = get_turbo_mhz(client)
            print(f"  Restored: {restored} MHz" if restored else "  Restored: Off")
        except Exception as e:
            print(f"  WARNING: Failed to restore state: {e}")

    # Summary
    print_summary(results)

    # Exit code: non-zero if any result was incorrect
    if any(not r["correct"] for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
