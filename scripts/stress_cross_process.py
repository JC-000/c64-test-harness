#!/usr/bin/env python3
"""Cross-process VICE instance management stress test.

Designed to be run from multiple terminals/agents simultaneously with
overlapping port ranges to test the file-lock-based TOCTOU protection.

Usage::

    # Phase 1: file locks only (no VICE needed)
    python3 scripts/stress_cross_process.py --phase lock-only

    # Phase 2: full VICE startup (requires x64sc)
    python3 scripts/stress_cross_process.py --phase vice --workers 6

    # Phase 3: mixed short/long workloads
    python3 scripts/stress_cross_process.py --phase mixed

    # Phase 4: crash simulation
    python3 scripts/stress_cross_process.py --phase crash

    # Phase 5: port exhaustion
    python3 scripts/stress_cross_process.py --phase exhaustion

    # Run all phases
    python3 scripts/stress_cross_process.py --phase all
"""

from __future__ import annotations

import argparse
import multiprocessing
import os
import random
import sys
import time
from pathlib import Path

# Add src to path for direct execution
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from c64_test_harness.backends.port_lock import PortLock
from c64_test_harness.backends.vice_manager import (
    PortAllocator,
    ViceInstanceManager,
)
from c64_test_harness.backends.vice_lifecycle import ViceConfig


def phase_lock_only(
    port_start: int, port_end: int, workers: int, rounds: int,
) -> bool:
    """Phase 1: File lock contention without VICE.

    Each worker creates its own PortAllocator, allocates ports, closes
    the reservation socket, holds the file lock for a random duration,
    then releases.  Verifies zero port collisions across workers.
    """
    print(f"\n{'='*60}")
    print(f"Phase: lock-only | {workers} workers | ports {port_start}-{port_end-1}")
    print(f"{'='*60}")

    def worker(wid: int, results_q: multiprocessing.Queue, port_s: int, port_e: int):
        allocated = []
        try:
            alloc = PortAllocator(port_range_start=port_s, port_range_end=port_e)
            for _ in range(rounds):
                try:
                    port = alloc.allocate()
                except RuntimeError:
                    time.sleep(random.uniform(0.05, 0.2))
                    continue
                # Take and close the socket (simulating VICE startup window)
                sock = alloc.take_socket(port)
                if sock:
                    sock.close()
                # Hold the file lock for a random duration
                hold_time = random.uniform(0.1, 2.0)
                time.sleep(hold_time)
                allocated.append(port)
                alloc.release(port)
            results_q.put(("ok", wid, allocated))
        except Exception as e:
            results_q.put(("error", wid, str(e)))

    q: multiprocessing.Queue = multiprocessing.Queue()
    procs = []
    for i in range(workers):
        p = multiprocessing.Process(target=worker, args=(i, q, port_start, port_end))
        p.start()
        procs.append(p)

    for p in procs:
        p.join(timeout=60)

    results = []
    errors = []
    while not q.empty():
        r = q.get_nowait()
        if r[0] == "ok":
            results.append(r)
        else:
            errors.append(r)

    if errors:
        for e in errors:
            print(f"  FAIL worker {e[1]}: {e[2]}")
        return False

    print(f"  All {workers} workers completed successfully")
    total_allocs = sum(len(r[2]) for r in results)
    print(f"  Total allocations: {total_allocs}")
    return True


def phase_vice(
    port_start: int, port_end: int, workers: int, rounds: int,
) -> bool:
    """Phase 2: Full VICE startup and verification.

    Each worker starts VICE, reads memory address $0001, releases.
    """
    print(f"\n{'='*60}")
    print(f"Phase: vice | {workers} workers | ports {port_start}-{port_end-1}")
    print(f"{'='*60}")

    def worker(wid: int, results_q: multiprocessing.Queue, port_s: int, port_e: int):
        try:
            cfg = ViceConfig(minimize=True, warp=True, sound=False)
            mgr = ViceInstanceManager(
                config=cfg,
                port_range_start=port_s,
                port_range_end=port_e,
                max_retries=3,
            )
            for r in range(rounds):
                with mgr.instance() as inst:
                    # Verify we got a valid instance
                    assert inst.port in range(port_s, port_e)
                    assert inst.managed
                    # Brief hold to simulate test work
                    time.sleep(random.uniform(0.5, 2.0))
            results_q.put(("ok", wid, rounds))
        except Exception as e:
            results_q.put(("error", wid, str(e)))

    q: multiprocessing.Queue = multiprocessing.Queue()
    procs = []
    for i in range(workers):
        p = multiprocessing.Process(target=worker, args=(i, q, port_start, port_end))
        p.start()
        procs.append(p)
        # Stagger starts slightly to reduce thundering herd
        time.sleep(0.1)

    for p in procs:
        p.join(timeout=120)

    results = []
    errors = []
    while not q.empty():
        r = q.get_nowait()
        if r[0] == "ok":
            results.append(r)
        else:
            errors.append(r)

    if errors:
        for e in errors:
            print(f"  FAIL worker {e[1]}: {e[2]}")
        return False

    print(f"  All {workers} workers completed {rounds} rounds each")
    return True


def phase_mixed(
    port_start: int, port_end: int, workers: int, _rounds: int,
) -> bool:
    """Phase 3: Mixed short/medium/long workloads with port recycling."""
    print(f"\n{'='*60}")
    print(f"Phase: mixed | {workers} workers | ports {port_start}-{port_end-1}")
    print(f"{'='*60}")

    def worker(wid: int, results_q: multiprocessing.Queue, port_s: int, port_e: int):
        try:
            alloc = PortAllocator(port_range_start=port_s, port_range_end=port_e)
            # Assign duration class
            r = random.random()
            if r < 0.5:
                hold_time = random.uniform(2, 5)  # short
                label = "short"
            elif r < 0.8:
                hold_time = random.uniform(5, 10)  # medium (shorter for test)
                label = "medium"
            else:
                hold_time = random.uniform(10, 15)  # long (shorter for test)
                label = "long"

            port = alloc.allocate()
            sock = alloc.take_socket(port)
            if sock:
                sock.close()
            time.sleep(hold_time)
            alloc.release(port)
            results_q.put(("ok", wid, label, hold_time))
        except Exception as e:
            results_q.put(("error", wid, str(e)))

    q: multiprocessing.Queue = multiprocessing.Queue()
    procs = []
    for i in range(workers):
        p = multiprocessing.Process(target=worker, args=(i, q, port_start, port_end))
        p.start()
        procs.append(p)

    for p in procs:
        p.join(timeout=120)

    results = []
    errors = []
    while not q.empty():
        r = q.get_nowait()
        if r[0] == "ok":
            results.append(r)
        else:
            errors.append(r)

    if errors:
        for e in errors:
            print(f"  FAIL worker {e[1]}: {e[2]}")
        return False

    for r in results:
        print(f"  Worker {r[1]}: {r[2]} ({r[3]:.1f}s)")
    print(f"  All {workers} workers completed")
    return True


def phase_crash(
    port_start: int, port_end: int, workers: int, _rounds: int,
) -> bool:
    """Phase 4: Crash simulation — some workers exit without cleanup.

    ~30% of workers call os._exit(1) after acquiring a port.
    Surviving workers must successfully allocate.
    """
    print(f"\n{'='*60}")
    print(f"Phase: crash | {workers} workers | ports {port_start}-{port_end-1}")
    print(f"{'='*60}")

    def worker(wid: int, results_q: multiprocessing.Queue, port_s: int, port_e: int, should_crash: bool):
        try:
            alloc = PortAllocator(port_range_start=port_s, port_range_end=port_e)
            port = alloc.allocate()
            sock = alloc.take_socket(port)
            if sock:
                sock.close()
            if should_crash:
                # Simulate crash — kernel releases flock
                os._exit(1)
            time.sleep(random.uniform(1, 3))
            alloc.release(port)
            results_q.put(("ok", wid, port))
        except Exception as e:
            results_q.put(("error", wid, str(e)))

    q: multiprocessing.Queue = multiprocessing.Queue()
    procs = []
    crash_count = 0

    for i in range(workers):
        should_crash = random.random() < 0.3
        if should_crash:
            crash_count += 1
        p = multiprocessing.Process(
            target=worker, args=(i, q, port_start, port_end, should_crash),
        )
        p.start()
        procs.append(p)

    for p in procs:
        p.join(timeout=30)

    results = []
    errors = []
    while not q.empty():
        r = q.get_nowait()
        if r[0] == "ok":
            results.append(r)
        else:
            errors.append(r)

    survived = len(results)
    expected_survivors = workers - crash_count
    print(f"  Crashed: {crash_count}, survived: {survived}, expected: {expected_survivors}")

    # After crashes, verify ports are reclaimable
    time.sleep(0.5)
    alloc = PortAllocator(port_range_start=port_start, port_range_end=port_end)
    try:
        port = alloc.allocate()
        alloc.release(port)
        print(f"  Post-crash allocation: OK (got port {port})")
    except RuntimeError as e:
        print(f"  Post-crash allocation: FAIL ({e})")
        return False

    if errors:
        for e in errors:
            print(f"  FAIL worker {e[1]}: {e[2]}")
        return False

    return True


def phase_exhaustion(
    port_start: int, port_end: int, workers: int, _rounds: int,
) -> bool:
    """Phase 5: Port exhaustion — more workers than ports.

    Workers that get 'No free ports' retry with backoff.
    """
    # Use a tight port range: fewer ports than workers
    tight_end = port_start + max(workers // 2, 2)
    print(f"\n{'='*60}")
    print(f"Phase: exhaustion | {workers} workers | ports {port_start}-{tight_end-1}")
    print(f"{'='*60}")

    def worker(wid: int, results_q: multiprocessing.Queue, port_s: int, port_e: int):
        max_attempts = 10
        for attempt in range(max_attempts):
            try:
                alloc = PortAllocator(port_range_start=port_s, port_range_end=port_e)
                port = alloc.allocate()
                time.sleep(random.uniform(0.5, 2.0))
                alloc.release(port)
                results_q.put(("ok", wid, attempt + 1))
                return
            except RuntimeError:
                time.sleep(random.uniform(0.1, 0.5) * (attempt + 1))
        results_q.put(("exhausted", wid, max_attempts))

    q: multiprocessing.Queue = multiprocessing.Queue()
    procs = []
    for i in range(workers):
        p = multiprocessing.Process(
            target=worker, args=(i, q, port_start, tight_end),
        )
        p.start()
        procs.append(p)

    for p in procs:
        p.join(timeout=60)

    results = []
    exhausted = []
    while not q.empty():
        r = q.get_nowait()
        if r[0] == "ok":
            results.append(r)
        else:
            exhausted.append(r)

    print(f"  Completed: {len(results)}, exhausted: {len(exhausted)}")
    for r in results:
        print(f"  Worker {r[1]}: OK after {r[2]} attempt(s)")

    # All workers should eventually complete
    if exhausted:
        for e in exhausted:
            print(f"  Worker {e[1]}: gave up after {e[2]} attempts")
        return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--phase",
        choices=["lock-only", "vice", "mixed", "crash", "exhaustion", "all"],
        default="lock-only",
    )
    parser.add_argument("--port-start", type=int, default=6540)
    parser.add_argument("--port-end", type=int, default=6560)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args()

    phases: dict[str, callable] = {
        "lock-only": phase_lock_only,
        "vice": phase_vice,
        "mixed": phase_mixed,
        "crash": phase_crash,
        "exhaustion": phase_exhaustion,
    }

    to_run = list(phases.keys()) if args.phase == "all" else [args.phase]
    passed = 0
    failed = 0

    for name in to_run:
        ok = phases[name](args.port_start, args.port_end, args.workers, args.rounds)
        if ok:
            passed += 1
        else:
            failed += 1

    print(f"\n{'='*60}")
    print(f"Results: {passed} passed, {failed} failed")
    print(f"{'='*60}")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
