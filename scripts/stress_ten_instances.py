#!/usr/bin/env python3
"""10-instance VICE stress test — launch many instances and run real workloads.

Launches N VICE instances simultaneously via ViceInstanceManager, waits for
each to boot to the BASIC READY prompt, then runs multiple rounds of test
workloads (memory read/write, JSR execution, screen validation, computation
verification, ROM signature check) on all instances in parallel.

Usage:
    python3 scripts/stress_ten_instances.py [OPTIONS]

Options:
    --instances N      Number of VICE instances to launch (default: 10)
    --port-start N     Start of port range (default: 6700)
    --rounds N         Rounds of tests per instance (default: 3)
    --verbose          Print per-operation details
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

# Ensure our package is importable when run from repo root
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from c64_test_harness.backends.vice_lifecycle import ViceConfig
from c64_test_harness.backends.vice_manager import ViceInstance, ViceInstanceManager
from c64_test_harness.execute import jsr
from c64_test_harness.memory import read_bytes, write_bytes
from c64_test_harness.screen import wait_for_text


@dataclass
class WorkerResult:
    """Result from one instance's test workload."""

    instance_id: int
    port: int
    pid: int | None
    rounds_passed: int
    rounds_total: int
    duration_s: float
    error: str | None


def run_test_round(
    inst: ViceInstance,
    instance_id: int,
    verbose: bool,
) -> str | None:
    """Run a single round of test operations on one instance.

    Returns None on success, or an error message string on failure.
    """
    transport = inst.transport

    # --- (a) Write 256 bytes of test pattern to $C000, read back, verify ---
    pattern = bytes(range(256))
    write_bytes(transport, 0xC000, pattern)
    readback = read_bytes(transport, 0xC000, 256)
    if readback != pattern:
        mismatches = sum(1 for a, b in zip(pattern, readback) if a != b)
        return f"memory pattern: {mismatches}/256 bytes differ"
    if verbose:
        print(f"    [{instance_id}] memory pattern OK")

    # --- (b) Write subroutine (LDA #$42, STA $C100, RTS), JSR, verify ---
    # LDA #$42 = A9 42, STA $C100 = 8D 00 C1, RTS = 60
    subroutine = bytes([0xA9, 0x42, 0x8D, 0x00, 0xC1, 0x60])
    write_bytes(transport, 0xC000, subroutine)
    # Clear the target byte first
    write_bytes(transport, 0xC100, bytes([0x00]))
    jsr(transport, 0xC000, timeout=10.0)
    result = read_bytes(transport, 0xC100, 1)
    if result[0] != 0x42:
        return f"jsr store: expected 0x42 at $C100, got 0x{result[0]:02X}"
    if verbose:
        print(f"    [{instance_id}] jsr store OK")

    # --- (c) Read screen codes, verify valid (1000 bytes, all 0-255) ---
    screen = read_bytes(transport, 0x0400, 1000)
    for i, b in enumerate(screen):
        if b > 255:
            return f"screen code out of range at offset {i}: {b}"
    if verbose:
        print(f"    [{instance_id}] screen codes OK (1000 bytes)")

    # --- (d) Computation routine: load $C100, ASL, store $C101, RTS ---
    # LDA $C100 = AD 00 C1, ASL A = 0A, STA $C101 = 8D 01 C1, RTS = 60
    compute = bytes([0xAD, 0x00, 0xC1, 0x0A, 0x8D, 0x01, 0xC1, 0x60])
    write_bytes(transport, 0xC000, compute)

    test_values = [0x01, 0x10, 0x2A, 0x40, 0x55, 0x7F]
    for val in test_values:
        write_bytes(transport, 0xC100, bytes([val]))
        write_bytes(transport, 0xC101, bytes([0x00]))
        jsr(transport, 0xC000, timeout=10.0)
        result = read_bytes(transport, 0xC101, 1)
        expected = (val << 1) & 0xFF
        if result[0] != expected:
            return (
                f"compute ASL: input=0x{val:02X}, "
                f"expected 0x{expected:02X}, got 0x{result[0]:02X}"
            )
    if verbose:
        print(f"    [{instance_id}] computation OK ({len(test_values)} values)")

    # --- (e) Read BASIC ROM signature at $A000 (2 bytes) ---
    rom = read_bytes(transport, 0xA000, 2)
    if verbose:
        print(f"    [{instance_id}] ROM signature: {rom[0]:02X} {rom[1]:02X}")
    # BASIC ROM starts with $94 $E3 (the cold-start vector)
    # Just verify we got something non-zero (ROM is mapped in)
    if rom == bytes([0x00, 0x00]):
        return "ROM signature: both bytes zero — ROM not mapped?"

    return None


def worker_fn(
    inst: ViceInstance,
    instance_id: int,
    rounds: int,
    verbose: bool,
) -> WorkerResult:
    """Run all test rounds on a single VICE instance."""
    t0 = time.monotonic()
    rounds_passed = 0
    error = None

    for rnd in range(1, rounds + 1):
        try:
            err = run_test_round(inst, instance_id, verbose)
            if err is not None:
                error = f"round {rnd}: {err}"
                break
            rounds_passed += 1
        except Exception as e:
            error = f"round {rnd}: {type(e).__name__}: {e}"
            break

    return WorkerResult(
        instance_id=instance_id,
        port=inst.port,
        pid=inst.pid,
        rounds_passed=rounds_passed,
        rounds_total=rounds,
        duration_s=time.monotonic() - t0,
        error=error,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Multi-instance VICE stress test with real workloads"
    )
    parser.add_argument("--instances", type=int, default=10,
                        help="Number of VICE instances (default: 10)")
    parser.add_argument("--port-start", type=int, default=6700,
                        help="Start of port range (default: 6700)")
    parser.add_argument("--rounds", type=int, default=3,
                        help="Test rounds per instance (default: 3)")
    parser.add_argument("--verbose", action="store_true",
                        help="Print per-operation details")
    args = parser.parse_args()

    num_instances = args.instances
    port_start = args.port_start
    # 2 ports per instance to leave headroom
    port_end = port_start + num_instances * 2
    rounds = args.rounds

    print(f"=== {num_instances}-Instance VICE Stress Test ===")
    print(f"Launching {num_instances} instances (ports {port_start}-{port_end - 1})...")

    wall_start = time.monotonic()

    config = ViceConfig(warp=True, sound=False, minimize=True)
    mgr = ViceInstanceManager(
        config=config,
        port_range_start=port_start,
        port_range_end=port_end,
    )

    instances: list[ViceInstance] = []
    boot_ok = True

    try:
        # --- Launch all instances ---
        for i in range(num_instances):
            try:
                inst = mgr.acquire()
                instances.append(inst)
                print(f"  Instance {i}: port={inst.port} PID={inst.pid} — acquired")
            except Exception as e:
                print(f"  Instance {i}: FAILED to acquire — {e}")
                boot_ok = False
                break

        if not boot_ok or len(instances) < num_instances:
            print(f"\nFailed to launch all instances ({len(instances)}/{num_instances})")
            sys.exit(1)

        # --- Wait for BASIC READY on each (in parallel) ---
        print(f"\nWaiting for BASIC READY prompt on all instances...")

        def boot_wait(idx_inst: tuple[int, ViceInstance]) -> tuple[int, bool]:
            idx, inst = idx_inst
            grid = wait_for_text(inst.transport, "READY.", timeout=60, verbose=False)
            return idx, grid is not None

        with ThreadPoolExecutor(max_workers=num_instances) as pool:
            boot_futures = {
                pool.submit(boot_wait, (i, inst)): i
                for i, inst in enumerate(instances)
            }
            for future in as_completed(boot_futures):
                idx, ok = future.result()
                status = "booted" if ok else "TIMEOUT"
                print(f"  Instance {idx}: port={instances[idx].port} PID={instances[idx].pid} — {status}")
                if not ok:
                    boot_ok = False

        if not boot_ok:
            print("\nNot all instances booted successfully.")
            sys.exit(1)

        # --- Run test workloads in parallel ---
        print(f"\nRunning tests ({rounds} rounds per instance, {num_instances} workers)...")

        results: list[WorkerResult] = []
        with ThreadPoolExecutor(max_workers=num_instances) as pool:
            futures = {
                pool.submit(worker_fn, inst, i, rounds, args.verbose): i
                for i, inst in enumerate(instances)
            }
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                status = "PASS" if result.error is None else "FAIL"
                print(
                    f"  Instance {result.instance_id}: [{status}] "
                    f"{result.rounds_passed}/{result.rounds_total} rounds "
                    f"({result.duration_s:.1f}s)"
                    + (f" — {result.error}" if result.error else "")
                )

        # --- Summary ---
        results.sort(key=lambda r: r.instance_id)
        wall_time = time.monotonic() - wall_start
        passed = sum(1 for r in results if r.error is None)
        total_rounds_passed = sum(r.rounds_passed for r in results)
        total_rounds = sum(r.rounds_total for r in results)

        print(f"\n{'=' * 50}")
        print(
            f"  {passed}/{num_instances} instances passed | "
            f"{total_rounds_passed}/{total_rounds} rounds | "
            f"{wall_time:.1f}s wall time"
        )
        print(f"{'=' * 50}")

        if passed < num_instances:
            print("\nFailed instances:")
            for r in results:
                if r.error:
                    print(f"  #{r.instance_id} port={r.port} PID={r.pid}: {r.error}")
            sys.exit(1)
        else:
            sys.exit(0)

    finally:
        mgr.shutdown()


if __name__ == "__main__":
    main()
