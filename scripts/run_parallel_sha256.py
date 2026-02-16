#!/usr/bin/env python3
"""Parallel SHA-256 validation — 3 concurrent VICE instances.

Builds the c64-aes256-ecdsa project, launches 3 VICE instances via
ViceInstanceManager, and runs the SHA-256 direct-memory tests from
test_sha256_direct.py against each instance in parallel.

Usage:
    python3 scripts/run_parallel_sha256.py [--iterations N] [--workers N]
"""

from __future__ import annotations

import os
import random
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
CRYPTO_ROOT = "/home/someone/c64-aes256-ecdsa"
PRG_PATH = os.path.join(CRYPTO_ROOT, "build", "aes256keygen.prg")
LABELS_PATH = os.path.join(CRYPTO_ROOT, "build", "labels.txt")
TOOLS_DIR = os.path.join(CRYPTO_ROOT, "tools")

# Ensure our package and the external tools are importable
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))
sys.path.insert(0, TOOLS_DIR)

from c64_test_harness import (
    Labels,
    ViceTransport,
    wait_for_text,
    dump_screen,
)
from c64_test_harness.backends.vice_lifecycle import ViceConfig
from c64_test_harness.backends.vice_manager import ViceInstanceManager

import test_sha256_direct


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

NUM_WORKERS = 3
ITERATIONS_PER_WORKER = 10
PORT_RANGE_START = 6510
PORT_RANGE_END = 6513  # exactly 3 ports


def parse_args() -> tuple[int, int]:
    iterations = ITERATIONS_PER_WORKER
    workers = NUM_WORKERS
    if "--iterations" in sys.argv:
        idx = sys.argv.index("--iterations")
        if idx + 1 < len(sys.argv):
            iterations = int(sys.argv[idx + 1])
    if "--workers" in sys.argv:
        idx = sys.argv.index("--workers")
        if idx + 1 < len(sys.argv):
            workers = int(sys.argv[idx + 1])
    return iterations, workers


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

def worker(
    worker_id: int,
    transport: ViceTransport,
    labels: Labels,
    iterations: int,
    seed: int,
) -> tuple[int, int, int, float]:
    """Run SHA-256 tests on one VICE instance.

    Returns (worker_id, passed, failed, duration).
    """
    random.seed(seed)
    t0 = time.monotonic()
    print(f"  [Worker {worker_id}] Starting ({iterations} iterations, seed={seed})")

    passed, failed = test_sha256_direct.run_tests(
        transport, labels, iterations=iterations, do_cross_validate=False,
    )

    duration = time.monotonic() - t0
    print(f"  [Worker {worker_id}] Done: {passed} passed, {failed} failed ({duration:.1f}s)")
    return worker_id, passed, failed, duration


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    iterations, num_workers = parse_args()

    # Build
    print("=== Building c64-aes256-ecdsa ===")
    subprocess.run(["make", "clean"], capture_output=True, cwd=CRYPTO_ROOT)
    result = subprocess.run(["make"], capture_output=True, text=True, cwd=CRYPTO_ROOT)
    if result.returncode != 0:
        print(f"Build failed:\n{result.stderr}")
        return 1
    print("  Build OK")

    if not os.path.exists(PRG_PATH):
        print(f"FATAL: {PRG_PATH} not found")
        return 1

    # Load labels
    labels = Labels.from_file(LABELS_PATH)
    required = [
        "sha256_hash", "sha256_init", "sha256_update", "sha256_final",
        "sha256_h0", "sha256_block", "sha256_process_block",
        "input_buffer", "input_length",
    ]
    for name in required:
        if labels.address(name) is None:
            print(f"FATAL: '{name}' label not found")
            return 1
    print(f"  Labels loaded, sha256_hash at ${labels['sha256_hash']:04X}")

    # Adjust port range for requested worker count
    port_end = PORT_RANGE_START + num_workers

    # Launch instances
    print(f"\n=== Starting {num_workers} VICE instances (ports {PORT_RANGE_START}-{port_end - 1}) ===")
    config = ViceConfig(prg_path=PRG_PATH, warp=True, ntsc=True, sound=False)

    with ViceInstanceManager(
        config=config,
        port_range_start=PORT_RANGE_START,
        port_range_end=port_end,
    ) as mgr:
        # Acquire all instances up front
        instances = []
        for i in range(num_workers):
            inst = mgr.acquire()
            print(f"  Instance {i}: port {inst.port}, PID {inst.process.pid if inst.process else '?'}")
            instances.append(inst)

        # Wait for each instance's main menu
        print("\n=== Waiting for main menus ===")
        for i, inst in enumerate(instances):
            grid = wait_for_text(inst.transport, "Q=QUIT", timeout=60.0)
            if grid is None:
                print(f"  FATAL: Instance {i} (port {inst.port}) menu did not appear")
                dump_screen(inst.transport, f"startup_{i}")
                return 1
            print(f"  Instance {i}: menu ready")

        # Run tests in parallel
        print(f"\n=== Running SHA-256 tests ({iterations} iterations x {num_workers} workers) ===")
        master_seed = random.randint(0, 2**32 - 1)
        print(f"  Master seed: {master_seed}")

        wall_start = time.monotonic()
        results: list[tuple[int, int, int, float]] = []

        with ThreadPoolExecutor(max_workers=num_workers) as pool:
            futures = {}
            for i, inst in enumerate(instances):
                worker_seed = master_seed + i
                fut = pool.submit(
                    worker, i, inst.transport, labels, iterations, worker_seed,
                )
                futures[fut] = i

            for fut in as_completed(futures):
                try:
                    results.append(fut.result())
                except Exception as e:
                    wid = futures[fut]
                    print(f"  [Worker {wid}] EXCEPTION: {e}")
                    results.append((wid, 0, iterations, 0.0))

        wall_time = time.monotonic() - wall_start

        # Release instances
        for inst in instances:
            mgr.release(inst)

    # Summary
    total_passed = sum(r[1] for r in results)
    total_failed = sum(r[2] for r in results)
    total_tests = total_passed + total_failed

    print("\n" + "=" * 60)
    print("PARALLEL SHA-256 RESULTS")
    print("=" * 60)
    for wid, p, f, d in sorted(results):
        status = "PASS" if f == 0 else "FAIL"
        print(f"  Worker {wid}: [{status}] {p}/{p + f} passed ({d:.1f}s)")
    print("-" * 60)
    print(f"  Total: {total_passed}/{total_tests} passed")
    print(f"  Wall time: {wall_time:.1f}s")
    if total_failed == 0:
        print(f"\n  [+] ALL {total_tests} TESTS PASSED across {num_workers} instances")
    else:
        print(f"\n  [-] {total_failed} TEST(S) FAILED")
    print("=" * 60)

    return 0 if total_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
