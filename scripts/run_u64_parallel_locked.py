#!/usr/bin/env python3
"""Run U64 live test files in parallel with cross-process DeviceLock.

Each test file runs in its own subprocess.  Before pytest starts, the
subprocess acquires a DeviceLock for the U64 device, ensuring exclusive
access.  This lets multiple agents/processes safely share a single device.

Usage:
    python3 scripts/run_u64_parallel_locked.py [HOST] [--workers N]

Defaults: HOST=192.168.1.81, workers=4 (one per test file)
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Test files to run (each in its own locked subprocess)
TEST_FILES = [
    "tests/test_ultimate64_client_live.py",
    "tests/test_ultimate64_transport_live.py",
    "tests/test_u64_feature_parity_live.py",
    "tests/test_sid_u64_live.py",
]


def _run_locked(test_file: str, host: str, password: str | None) -> dict:
    """Acquire DeviceLock, run pytest, release lock."""
    src = str(PROJECT_ROOT / "src")
    if src not in sys.path:
        sys.path.insert(0, src)

    from c64_test_harness.backends.device_lock import DeviceLock

    pid = os.getpid()
    t0 = time.monotonic()

    lock = DeviceLock(host)
    t_pre = time.monotonic()
    acquired = lock.acquire(timeout=120.0)
    lock_wait = time.monotonic() - t_pre

    if not acquired:
        return {
            "file": test_file,
            "pid": pid,
            "returncode": 1,
            "summary": "TIMEOUT acquiring DeviceLock",
            "elapsed": time.monotonic() - t0,
            "lock_wait": lock_wait,
        }

    try:
        env = {**os.environ, "U64_HOST": host}
        if password:
            env["U64_PASSWORD"] = password

        result = subprocess.run(
            [sys.executable, "-m", "pytest", test_file, "-v", "--tb=short"],
            capture_output=True,
            text=True,
            timeout=90,
            cwd=str(PROJECT_ROOT),
            env=env,
        )

        # Extract summary line
        summary = "no output"
        for line in reversed(result.stdout.splitlines()):
            if "passed" in line or "failed" in line or "error" in line:
                summary = line.strip()
                break
    except subprocess.TimeoutExpired:
        result = type("R", (), {"returncode": 1})()
        summary = "SUBPROCESS TIMEOUT"
    finally:
        lock.release()

    return {
        "file": test_file,
        "pid": pid,
        "returncode": result.returncode,
        "summary": summary,
        "elapsed": round(time.monotonic() - t0, 1),
        "lock_wait": round(lock_wait, 3),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("host", nargs="?", default="192.168.1.81")
    ap.add_argument("--workers", type=int, default=len(TEST_FILES))
    args = ap.parse_args()
    password = os.environ.get("U64_PASSWORD")

    print(
        f"=== U64 Live Tests — Parallel with DeviceLock ===\n"
        f"  Host:    {args.host}\n"
        f"  Workers: {args.workers}\n"
        f"  Files:   {len(TEST_FILES)}\n"
    )

    t0 = time.monotonic()
    results = []

    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(_run_locked, f, args.host, password): f
            for f in TEST_FILES
        }
        for future in as_completed(futures):
            r = future.result()
            results.append(r)
            status = "PASS" if r["returncode"] == 0 else "FAIL"
            print(
                f"  [{status}] {r['file']}  "
                f"(pid={r['pid']}, {r['elapsed']}s, lock_wait={r['lock_wait']}s)"
            )
            print(f"         {r['summary']}")
            print()

    total = time.monotonic() - t0
    passed = sum(1 for r in results if r["returncode"] == 0)
    failed = len(results) - passed
    pids = sorted(set(r["pid"] for r in results))

    print(f"=== Summary ===")
    print(f"  Files:   {passed}/{len(results)} passed")
    print(f"  PIDs:    {len(pids)} unique: {pids}")
    print(f"  Total:   {total:.1f}s wall time")

    if failed:
        print(f"\n  {failed} file(s) FAILED")
        return 1

    print(f"\n  ALL PASSED — parallel locked execution works")
    return 0


if __name__ == "__main__":
    sys.exit(main())
