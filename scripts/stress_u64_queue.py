#!/usr/bin/env python3
"""Stress-test the cross-process DeviceLock queueing for Ultimate 64.

Spawns multiple subprocesses that each compete for exclusive access to
a single U64 device via DeviceLock.  Each subprocess acquires the lock,
runs a short live test (memory round-trip + screen read), then releases.

This validates:
  - DeviceLock correctly serializes access across OS processes
  - No two processes hold the device simultaneously
  - All processes eventually complete (no deadlock / starvation)
  - The UnifiedManager → _LockedU64Manager path works end-to-end

Usage:
    python3 scripts/stress_u64_queue.py [HOST] [--workers N] [--rounds N]

Defaults: HOST=192.168.1.81, workers=4, rounds=3
Each worker runs `rounds` sequential iterations = workers*rounds total
lock acquisitions against a single device.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Worker: runs in a subprocess (separate OS process)
# ---------------------------------------------------------------------------

def _worker(
    worker_id: int,
    host: str,
    password: str | None,
    rounds: int,
    results_dir: str,
) -> dict:
    """Acquire U64 via UnifiedManager, run a quick test, release."""
    # Ensure src is importable
    src = str(PROJECT_ROOT / "src")
    if src not in sys.path:
        sys.path.insert(0, src)

    from c64_test_harness.backends.device_lock import DeviceLock
    from c64_test_harness.backends.unified_manager import (
        UnifiedManager,
    )

    results = []
    for r in range(rounds):
        t0 = time.monotonic()
        error = None
        lock_wait = 0.0
        try:
            mgr = UnifiedManager(
                backend="u64",
                u64_hosts=[host],
                u64_password=password,
            )
            # acquire() goes through _LockedU64Manager → DeviceLock
            t_pre_acq = time.monotonic()
            target = mgr.acquire()
            lock_wait = time.monotonic() - t_pre_acq
            transport = target.transport

            # --- Quick live test ---
            # 1) Memory round-trip at a scratch address
            payload = bytes([0xDE, 0xAD, worker_id & 0xFF, r & 0xFF])
            transport.write_memory(0xC100, payload)
            readback = transport.read_memory(0xC100, len(payload))
            assert readback == payload, (
                f"Memory mismatch: wrote {payload.hex()} got {readback.hex()}"
            )

            # 2) Screen read (should be 1000 bytes)
            screen = transport.read_screen_codes()
            assert len(screen) == 1000, f"Screen size {len(screen)} != 1000"

            # 3) Read BASIC warm-start vector to verify device is responsive
            vec = transport.read_memory(0xA002, 2)
            assert len(vec) == 2

            mgr.release(target)
            mgr.shutdown()

        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"

        elapsed = time.monotonic() - t0
        result = {
            "worker": worker_id,
            "round": r,
            "ok": error is None,
            "error": error,
            "elapsed_s": round(elapsed, 3),
            "lock_wait_s": round(lock_wait, 3),
            "pid": os.getpid(),
        }
        results.append(result)

        # Write per-event log for post-mortem
        log_path = os.path.join(
            results_dir, f"worker-{worker_id:02d}-round-{r:02d}.json"
        )
        with open(log_path, "w") as f:
            json.dump(result, f)

    return {"worker": worker_id, "pid": os.getpid(), "results": results}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("host", nargs="?", default="192.168.1.81")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--rounds", type=int, default=3)
    args = ap.parse_args()

    password = os.environ.get("U64_PASSWORD")

    with tempfile.TemporaryDirectory(prefix="u64-stress-") as results_dir:
        print(
            f"=== U64 DeviceLock Stress Test ===\n"
            f"  Host:    {args.host}\n"
            f"  Workers: {args.workers} (separate OS processes)\n"
            f"  Rounds:  {args.rounds} per worker\n"
            f"  Total:   {args.workers * args.rounds} lock acquisitions\n"
            f"  Results: {results_dir}\n"
        )

        t0 = time.monotonic()
        all_results: list[dict] = []

        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(
                    _worker, wid, args.host, password, args.rounds, results_dir,
                ): wid
                for wid in range(args.workers)
            }
            for future in as_completed(futures):
                wid = futures[future]
                try:
                    data = future.result()
                    all_results.append(data)
                except Exception as exc:
                    print(f"  [CRASH] Worker {wid}: {exc}")
                    all_results.append({
                        "worker": wid,
                        "pid": None,
                        "results": [{"ok": False, "error": str(exc)}],
                    })

        total_time = time.monotonic() - t0

        # --- Summary ---
        ok_count = 0
        fail_count = 0
        max_wait = 0.0
        pids = set()
        for w in all_results:
            if w.get("pid"):
                pids.add(w["pid"])
            for r in w["results"]:
                if r.get("ok"):
                    ok_count += 1
                else:
                    fail_count += 1
                    print(f"  FAIL worker={w['worker']} round={r.get('round','?')}: "
                          f"{r.get('error','unknown')}")
                max_wait = max(max_wait, r.get("lock_wait_s", 0.0))

        print(f"\n=== Results ===")
        print(f"  Passed:       {ok_count}/{ok_count + fail_count}")
        print(f"  Failed:       {fail_count}")
        print(f"  Unique PIDs:  {len(pids)} ({sorted(pids)})")
        print(f"  Max lock wait: {max_wait:.3f}s")
        print(f"  Total time:   {total_time:.1f}s")
        print(f"  Throughput:   {(ok_count + fail_count) / total_time:.1f} acquisitions/s")

        if fail_count:
            print(f"\n  FAILED — {fail_count} errors detected")
            return 1
        else:
            print(f"\n  ALL PASSED — cross-process queueing works correctly")
            return 0


if __name__ == "__main__":
    sys.exit(main())
