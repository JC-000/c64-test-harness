#!/usr/bin/env python3
"""Stress test for cross-process port allocation and VICE startup.

Two test phases:

Phase 1 — Allocation race
    Spawns N processes simultaneously, each creating its own PortAllocator
    on the same port range.  All workers hold their ports until every worker
    has allocated, then verify zero duplicates.  Repeated across multiple
    rounds to catch intermittent races.

Phase 2 — VICE startup (unless --skip-vice)
    Spawns N processes simultaneously, each allocating a port, releasing the
    reservation socket, launching VICE, and connecting via the binary monitor.
    Tests the gap between reservation release and VICE bind under concurrency,
    with configurable inter-worker delays to find the safe minimum.

Usage:
    python3 scripts/stress_port_allocation.py [OPTIONS]

Options:
    --workers N         Concurrent worker processes (default: 6)
    --port-start N      Start of port range (default: 6540)
    --port-end N        End of port range (default: 6560)
    --rounds N          Rounds per test (default: 5)
    --delays LIST       Comma-separated inter-worker delays for VICE phase
                        (default: 0,0.1,0.25,0.5)
    --skip-vice         Run allocation phase only (no VICE needed)
    --verbose           Print per-worker details
"""

from __future__ import annotations

import argparse
import multiprocessing
import multiprocessing.pool
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from itertools import groupby

# Ensure our package is importable when run from repo root
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from c64_test_harness.backends.vice_lifecycle import ViceConfig, ViceProcess
from c64_test_harness.backends.vice_manager import PortAllocator
from c64_test_harness.backends.vice_binary import BinaryViceTransport


# ---------------------------------------------------------------------------
# Phase 1 — Pure allocation race
# ---------------------------------------------------------------------------

def _alloc_worker(
    worker_id: int,
    port_start: int,
    port_end: int,
    barrier_flag_path: str,
) -> dict:
    """Allocate a port, signal ready, hold until all workers done."""
    t0 = time.monotonic()
    error = None
    port = None
    try:
        alloc = PortAllocator(port_range_start=port_start, port_range_end=port_end)
        port = alloc.allocate()
        alloc_ms = (time.monotonic() - t0) * 1000

        # Signal that we've allocated
        flag = f"{barrier_flag_path}.{worker_id}"
        with open(flag, "w") as f:
            f.write(str(port))

        # Hold port until the parent cleans up (pool termination closes us)
        # Wait up to 30s — parent will collect results well before then
        time.sleep(30)

    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        alloc_ms = (time.monotonic() - t0) * 1000

    return {
        "worker_id": worker_id,
        "port": port,
        "alloc_ms": alloc_ms,
        "error": error,
    }


def run_alloc_phase(
    num_workers: int,
    port_start: int,
    port_end: int,
    rounds: int,
    verbose: bool,
) -> int:
    """Run allocation race tests. Returns number of collision rounds."""
    print(f"{'='*70}")
    print("PHASE 1: Allocation Race (simultaneous, all ports held concurrently)")
    print(f"{'='*70}")
    print(f"  {num_workers} workers, ports {port_start}-{port_end - 1}, {rounds} rounds\n")

    collision_rounds = 0
    ctx = multiprocessing.get_context("spawn")

    for rnd in range(1, rounds + 1):
        import tempfile
        barrier_dir = tempfile.mkdtemp(prefix="c64_stress_")
        barrier_flag = os.path.join(barrier_dir, "ready")

        print(f"  Round {rnd}/{rounds}...", end=" ", flush=True)

        pool = multiprocessing.pool.Pool(processes=num_workers, context=ctx)
        futures = []
        for i in range(num_workers):
            f = pool.apply_async(
                _alloc_worker,
                args=(i, port_start, port_end, barrier_flag),
            )
            futures.append(f)

        # Wait for all workers to signal allocation (or timeout)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            flags = [f"{barrier_flag}.{i}" for i in range(num_workers)]
            ready = sum(1 for f in flags if os.path.exists(f))
            if ready >= num_workers:
                break
            time.sleep(0.05)

        # Read allocated ports from flag files
        ports = []
        errors = []
        for i in range(num_workers):
            flag = f"{barrier_flag}.{i}"
            if os.path.exists(flag):
                with open(flag) as f:
                    ports.append(int(f.read().strip()))
            else:
                errors.append(f"W{i}: timed out waiting for allocation")

        # Terminate pool (workers are sleeping — this is the intended cleanup)
        pool.terminate()
        pool.join()

        # Clean up flag files
        for i in range(num_workers):
            flag = f"{barrier_flag}.{i}"
            try:
                os.unlink(flag)
            except FileNotFoundError:
                pass
        try:
            os.rmdir(barrier_dir)
        except OSError:
            pass

        unique = len(set(ports))
        collisions = len(ports) - unique

        status = "OK" if collisions == 0 and not errors else "FAIL"
        collision_str = f" COLLISIONS={collisions}" if collisions else ""
        error_str = f" ERRORS={len(errors)}" if errors else ""
        print(
            f"[{status}] allocated={len(ports)}/{num_workers} "
            f"unique={unique}{collision_str}{error_str}"
        )

        if verbose:
            for i, p in enumerate(ports):
                print(f"    W{i}: port={p}")
            for e in errors:
                print(f"    {e}")

        if collisions > 0:
            collision_rounds += 1

    return collision_rounds


# ---------------------------------------------------------------------------
# Phase 2 — VICE startup race
# ---------------------------------------------------------------------------

@dataclass
class ViceWorkerResult:
    worker_id: int
    port: int | None
    pid: int | None
    allocated: bool
    monitor_ready: bool
    error: str | None
    alloc_ms: float
    total_s: float


def _vice_worker(
    worker_id: int,
    port_start: int,
    port_end: int,
    startup_delay: float,
) -> dict:
    """Allocate port, start VICE, connect binary monitor, clean up."""
    t0 = time.monotonic()
    port = None
    pid = None
    allocated = False
    monitor_ready = False
    error = None

    if startup_delay > 0:
        time.sleep(startup_delay * worker_id)

    try:
        alloc = PortAllocator(port_range_start=port_start, port_range_end=port_end)
        port = alloc.allocate()
        alloc_ms = (time.monotonic() - t0) * 1000
        allocated = True

        # Release reservation, start VICE
        reservation = alloc.take_socket(port)
        if reservation is not None:
            reservation.close()

        config = ViceConfig(
            port=port, warp=True, sound=False, minimize=True,
        )
        proc = ViceProcess(config)
        proc.start()
        pid = proc.pid

        # Binary monitor: connect directly with retries (no TCP probe).
        # The persistent connection IS the monitor session.
        deadline = time.monotonic() + 20.0
        transport = None
        while time.monotonic() < deadline:
            if proc._proc is not None and proc._proc.poll() is not None:
                break
            try:
                transport = BinaryViceTransport(port=port, timeout=5.0)
                break
            except Exception:
                time.sleep(1)
        monitor_ready = transport is not None
        if transport is not None:
            transport.close()

        proc.stop()
        alloc.release(port)

    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        alloc_ms = (time.monotonic() - t0) * 1000

    return {
        "worker_id": worker_id,
        "port": port,
        "pid": pid,
        "allocated": allocated,
        "monitor_ready": monitor_ready,
        "error": error,
        "alloc_ms": alloc_ms,
        "total_s": time.monotonic() - t0,
    }


@dataclass
class ViceRoundSummary:
    delay: float
    round_num: int
    num_workers: int
    allocated: int
    unique_ports: int
    monitor_ready: int
    errors: int
    avg_alloc_ms: float
    avg_total_s: float
    wall_s: float
    error_details: list[str] = field(default_factory=list)


def run_vice_round(
    num_workers: int,
    port_start: int,
    port_end: int,
    delay: float,
    round_num: int,
    verbose: bool,
) -> ViceRoundSummary:
    """Spawn workers, start VICE, collect results."""
    wall_start = time.monotonic()
    ctx = multiprocessing.get_context("spawn")

    with multiprocessing.pool.Pool(processes=num_workers, context=ctx) as pool:
        futures = []
        for i in range(num_workers):
            f = pool.apply_async(
                _vice_worker,
                args=(i, port_start, port_end, delay),
            )
            futures.append(f)

        results = []
        for f in futures:
            try:
                results.append(f.get(timeout=90))
            except Exception as e:
                results.append({
                    "worker_id": -1, "port": None, "pid": None,
                    "allocated": False, "monitor_ready": False,
                    "error": f"Pool error: {e}", "alloc_ms": 0, "total_s": 0,
                })

    wall_s = time.monotonic() - wall_start

    allocated = sum(1 for r in results if r["allocated"])
    ports = [r["port"] for r in results if r["port"] is not None]
    unique_ports = len(set(ports))
    monitor_ready = sum(1 for r in results if r["monitor_ready"])
    errors = sum(1 for r in results if r["error"] is not None)
    error_details = [
        f"  W{r['worker_id']}: {r['error']}" for r in results if r["error"]
    ]

    alloc_times = [r["alloc_ms"] for r in results if r["allocated"]]
    avg_alloc = sum(alloc_times) / len(alloc_times) if alloc_times else 0
    total_times = [r["total_s"] for r in results]
    avg_total = sum(total_times) / len(total_times) if total_times else 0

    if verbose:
        for r in sorted(results, key=lambda x: x["worker_id"]):
            status = "OK" if r["error"] is None else "FAIL"
            vice_s = "monitor=OK" if r["monitor_ready"] else "monitor=FAIL"
            print(
                f"    W{r['worker_id']:2d}: [{status}] port={r['port']} "
                f"pid={r['pid']} alloc={r['alloc_ms']:.0f}ms "
                f"{vice_s} total={r['total_s']:.1f}s"
                + (f" err={r['error']}" if r["error"] else "")
            )

    return ViceRoundSummary(
        delay=delay, round_num=round_num, num_workers=num_workers,
        allocated=allocated, unique_ports=unique_ports,
        monitor_ready=monitor_ready, errors=errors,
        avg_alloc_ms=avg_alloc, avg_total_s=avg_total,
        wall_s=wall_s, error_details=error_details,
    )


def run_vice_phase(
    num_workers: int,
    port_start: int,
    port_end: int,
    delays: list[float],
    rounds: int,
    verbose: bool,
) -> list[ViceRoundSummary]:
    """Run VICE startup tests across multiple delays."""
    print(f"\n{'='*70}")
    print(f"PHASE 2: VICE Startup Race (binary monitor)")
    print(f"{'='*70}")
    print(f"  {num_workers} workers, ports {port_start}-{port_end - 1}")
    print(f"  Delays: {delays}, {rounds} rounds each\n")

    all_summaries: list[ViceRoundSummary] = []

    for delay in delays:
        print(f"  --- delay={delay:.3f}s ---")

        for rnd in range(1, rounds + 1):
            print(f"  Round {rnd}/{rounds}...", end=" ", flush=True)
            summary = run_vice_round(
                num_workers=num_workers,
                port_start=port_start,
                port_end=port_end,
                delay=delay,
                round_num=rnd,
                verbose=verbose,
            )
            all_summaries.append(summary)

            print(
                f"alloc={summary.allocated}/{summary.num_workers} "
                f"vice={summary.monitor_ready}/{summary.num_workers} "
                f"unique={summary.unique_ports} "
                f"avg_alloc={summary.avg_alloc_ms:.0f}ms "
                f"wall={summary.wall_s:.1f}s"
                + (f" ERRORS={summary.errors}" if summary.errors else "")
            )
            if verbose and summary.error_details:
                for line in summary.error_details:
                    print(line)

        # Allow port cleanup between delay values
        time.sleep(2.0)

    return all_summaries


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Stress test cross-process port allocation and VICE startup"
    )
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--port-start", type=int, default=6540)
    parser.add_argument("--port-end", type=int, default=6560)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--delays", default="0,0.1,0.25,0.5",
                        help="Comma-separated inter-worker delays for VICE phase")
    parser.add_argument("--skip-vice", action="store_true",
                        help="Run allocation phase only (no VICE needed)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    port_range = args.port_end - args.port_start
    delays = [float(d) for d in args.delays.split(",")]

    print("Cross-process port allocation stress test")
    print(f"  Workers: {args.workers}")
    print(f"  Port range: {args.port_start}-{args.port_end - 1} ({port_range} ports)")
    print(f"  Rounds: {args.rounds}")
    print(f"  VICE: {'skipped' if args.skip_vice else 'enabled'}")
    if not args.skip_vice:
        print(f"  Monitor: binary")
        print(f"  VICE delays: {delays}")
    print()

    if args.workers > port_range:
        print(f"WARNING: {args.workers} workers > {port_range} ports — "
              f"some allocation failures expected\n")

    # Phase 1: allocation race
    collision_rounds = run_alloc_phase(
        num_workers=args.workers,
        port_start=args.port_start,
        port_end=args.port_end,
        rounds=args.rounds,
        verbose=args.verbose,
    )

    # Phase 2: VICE startup (optional)
    vice_summaries: list[ViceRoundSummary] = []
    if not args.skip_vice:
        if shutil.which("x64sc") is None:
            print("\nSkipping VICE phase — x64sc not found on PATH")
        else:
            vice_summaries = run_vice_phase(
                num_workers=args.workers,
                port_start=args.port_start,
                port_end=args.port_end,
                delays=delays,
                rounds=args.rounds,
                verbose=args.verbose,
            )

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    print(f"\n{'='*70}")
    print("SUMMARY")
    print(f"{'='*70}")

    print(f"\nPhase 1 — Allocation Race:")
    if collision_rounds == 0:
        print(f"  [+] {args.rounds}/{args.rounds} rounds with zero collisions")
        print(f"      bind()-based reservation prevents cross-process races")
    else:
        print(f"  [-] {collision_rounds}/{args.rounds} rounds had port collisions!")

    if vice_summaries:
        print(f"\nPhase 2 — VICE Startup:")
        header = f"  {'Delay':>8s}  {'Rounds':>6s}  {'Alloc%':>6s}  {'VICE%':>6s}  {'Errors':>6s}  {'AvgAlloc':>8s}  {'AvgWall':>7s}"
        print(header)
        print("  " + "-" * (len(header) - 2))

        for delay, group in groupby(vice_summaries, key=lambda s: s.delay):
            rounds_list = list(group)
            total_w = sum(s.num_workers for s in rounds_list)
            total_alloc = sum(s.allocated for s in rounds_list)
            total_vice = sum(s.monitor_ready for s in rounds_list)
            total_err = sum(s.errors for s in rounds_list)
            avg_alloc_ms = sum(s.avg_alloc_ms for s in rounds_list) / len(rounds_list)
            avg_wall = sum(s.wall_s for s in rounds_list) / len(rounds_list)

            alloc_pct = total_alloc / total_w * 100 if total_w else 0
            vice_pct = total_vice / total_w * 100 if total_w else 0

            print(
                f"  {delay:>8.3f}  {len(rounds_list):>6d}  {alloc_pct:>5.0f}%  "
                f"{vice_pct:>5.0f}%  {total_err:>6d}  {avg_alloc_ms:>7.0f}ms  "
                f"{avg_wall:>6.1f}s"
            )

        total_vice_ok = sum(s.monitor_ready for s in vice_summaries)
        total_vice_total = sum(s.num_workers for s in vice_summaries)
        total_errors = sum(s.errors for s in vice_summaries)

        print(f"\n  Overall: {total_vice_ok}/{total_vice_total} VICE instances started "
              f"({total_errors} errors)")

        if total_errors > 0:
            print("\n  Error breakdown:")
            for s in vice_summaries:
                for detail in s.error_details:
                    print(f"  {detail}")

    # Exit code
    if collision_rounds > 0:
        print(f"\n[-] FAILED — {collision_rounds} allocation race(s) detected")
        sys.exit(1)
    else:
        print(f"\n[+] PASSED — port allocation is cross-process safe")
        sys.exit(0)


if __name__ == "__main__":
    main()
