#!/usr/bin/env python3
"""Run U64 live test files in parallel pytest processes.

Each test file runs in its own ``python -m pytest`` child.  **This script takes
no DeviceLock itself** (#323).  What serialises the device is
``tests/conftest.py``'s autouse ``device_lock_guard``, which every child applies
to each ``*_live.py`` test: tests from different files interleave on the device,
no two tests hold it at once, and other lanes queue on the same lock.

Be clear about what the pool does *not* serialise.  A file does not have the
device to itself for its whole run, so device state one test leaves behind can
be seen by the next test of another file, just as it can between lanes.  The
90 s per-file subprocess timeout also includes that child's per-test waits
behind the other workers' tests and behind other lanes.

**Locking is per test, not per run**, and that is deliberate beyond #323 (#324):
the client drains ``/Temp`` from a lock-release callback that fires only on the
outermost release, so any whole-run hold would defer every per-test drain on a
leak-prone device to the end of the run.  See ``docs/device_locking.md`` rule 1.

It used to take ``DeviceLock(host)`` in the pool worker before launching
pytest.  The child's guard then queued on the flock its own parent held --
``allow_nested`` joins holds within one process only -- and, with the parent
alive and heartbeating, kept extending its wait until the 90 s timeout killed
it.  ``tests/test_parallel_runner_lock_shape.py`` reproduces that offline.

Usage:
    python3 scripts/run_u64_parallel_locked.py <HOST> [--workers N]
    U64_HOST=<device> python3 scripts/run_u64_parallel_locked.py [--workers N]

No default host -- this script runs the live suite in parallel against
whatever it is pointed at, so it refuses to pick one (#243). Default
workers=4 (one per test file).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _u64_host import require_u64_host  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Test files to run (each in its own pytest subprocess; conftest locks each test)
TEST_FILES = [
    "tests/test_ultimate64_client_live.py",
    "tests/test_ultimate64_transport_live.py",
    "tests/test_u64_feature_parity_live.py",
    "tests/test_sid_u64_live.py",
]


def _run_file(test_file: str, host: str, password: str | None) -> dict:
    """Run one live test file in a pytest child.  Takes no lock (#323).

    The child's conftest ``device_lock_guard`` locks each test; a lock held
    here would be one that guard queues on.
    """
    pid = os.getpid()
    t0 = time.monotonic()

    env = {**os.environ, "U64_HOST": host}
    if password:
        env["U64_PASSWORD"] = password

    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", test_file, "-v", "--tb=short"],
            capture_output=True,
            text=True,
            timeout=90,
            cwd=str(PROJECT_ROOT),
            env=env,
        )
        returncode = result.returncode
        # Extract summary line
        summary = "no output"
        for line in reversed(result.stdout.splitlines()):
            if "passed" in line or "failed" in line or "error" in line:
                summary = line.strip()
                break
    except subprocess.TimeoutExpired:
        returncode = 1
        summary = "SUBPROCESS TIMEOUT (the 90 s includes per-test lock waits)"

    return {
        "file": test_file,
        "pid": pid,
        "returncode": returncode,
        "summary": summary,
        "elapsed": round(time.monotonic() - t0, 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("host", nargs="?", default=None,
                    help="Device host/IP (or set $U64_HOST). No default.")
    ap.add_argument("--workers", type=int, default=len(TEST_FILES))
    args = ap.parse_args()

    args.host = require_u64_host(
        args.host, argv0="python3 scripts/run_u64_parallel_locked.py"
    )
    password = os.environ.get("U64_PASSWORD")

    print(
        f"=== U64 Live Tests — parallel files, per-test DeviceLock via conftest ===\n"
        f"  Host:    {args.host}\n"
        f"  Workers: {args.workers}\n"
        f"  Files:   {len(TEST_FILES)}\n"
    )

    t0 = time.monotonic()
    results = []

    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(_run_file, f, args.host, password): f
            for f in TEST_FILES
        }
        for future in as_completed(futures):
            r = future.result()
            results.append(r)
            status = "PASS" if r["returncode"] == 0 else "FAIL"
            print(
                f"  [{status}] {r['file']}  "
                f"(pid={r['pid']}, {r['elapsed']}s)"
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

    print(f"\n  ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
