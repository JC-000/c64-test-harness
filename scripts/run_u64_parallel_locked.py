#!/usr/bin/env python3
"""Run U64 live test files in parallel pytest processes.

Each test file runs in its own ``python -m pytest`` child.  **This script takes
no DeviceLock itself** (#323).  What serialises the device is
``tests/conftest.py``'s autouse ``device_lock_guard``, which every child applies
to each ``*_live.py`` test: tests from different files interleave on the device,
no two tests hold it at once, and other lanes queue on the same lock.

Be clear about what the pool does *not* serialise.  A file does not have the
device to itself for its whole run, so device state one test leaves behind can
be seen by the next test of another file, just as it can between lanes.

**No per-file wall-clock timeout by default** (#380).  A child's run time is
its tests *plus* every per-test wait for the lock -- behind the other workers'
tests and behind other lanes -- and the guard extends that wait indefinitely
behind a live, progressing holder.  So any fixed cap measures contention, not
a hang: the old 90 s killed files whose own tests were fast.

The cost of no cap is a hang that nothing bounds.  A child that hangs *inside*
a test still holds the device lock, and while its heartbeat thread keeps the
lockfile fresh it reads to every waiter as a live, progressing holder: every
lane behind it -- the other workers and other lanes -- waits until someone
kills it.  The guard's timeout (``U64_DEVICE_LOCK_TIMEOUT``, else conftest's
300 s) bounds only a wait behind a holder that stops heartbeating (dead PID or
stale lockfile), or a wait overtaken by repeated handoffs; it never bounds a
wait behind a single hung holder.  Defaulting to ``None`` is a deliberate
trade-off: no file is killed for contention, at the price of an unbounded hang.
For an unattended run, pass a generous ``--file-timeout SECONDS``, sized to the
file's tests plus the lock waits you are willing to serve, since the cap
includes those waits.

**Locking is per test, not per run**, and that is deliberate beyond #323 (#324).
``close()`` still drains a leaking client whatever the lock state, but the
lock-release ``/Temp`` drain -- the only one that catches a client a test never
closes -- fires only on the outermost release, and its callbacks are held
weakly.  A hold here would defer that drain on a leak-prone device to the end of
the run, and a client collected before then would never drain.  See
``docs/device_locking.md`` rule 1.

It used to take ``DeviceLock(host)`` in the pool worker before launching
pytest.  The child's guard then queued on the flock its own parent held --
``allow_nested`` joins holds within one process only -- and, with the parent
alive and heartbeating, kept extending its wait until the 90 s timeout killed
it.  ``tests/test_parallel_runner_lock_shape.py`` reproduces that offline.

Usage:
    python3 scripts/run_u64_parallel_locked.py <HOST> [--workers N] [--file-timeout SECONDS]
    U64_HOST=<device> python3 scripts/run_u64_parallel_locked.py [--workers N] [--file-timeout SECONDS]

No default host -- this script runs the live suite in parallel against
whatever it is pointed at, so it refuses to pick one (#243). Default
workers=4 (one per test file).
"""
from __future__ import annotations

import argparse
import math
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


def _run_file(
    test_file: str,
    host: str,
    password: str | None,
    file_timeout: float | None = None,
) -> dict:
    """Run one live test file in a pytest child.  Takes no lock (#323).

    The child's conftest ``device_lock_guard`` locks each test; a lock held
    here would be one that guard queues on.  *file_timeout* ``None`` (the
    default) puts no wall-clock cap on the child, because its run time
    includes those per-test lock waits (#380).
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
            timeout=file_timeout,
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
        summary = (
            f"SUBPROCESS TIMEOUT after --file-timeout {file_timeout:g} s "
            "(the cap includes per-test lock waits, #380)"
        )

    return {
        "file": test_file,
        "pid": pid,
        "returncode": returncode,
        "summary": summary,
        "elapsed": round(time.monotonic() - t0, 1),
    }


def _positive_seconds(raw: str) -> float:
    """argparse type for ``--file-timeout``: a finite number of seconds > 0."""
    try:
        value = float(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number of seconds: {raw!r}")
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError(
            f"must be finite and > 0 (omit the flag for no cap): {raw!r}"
        )
    return value


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("host", nargs="?", default=None,
                    help="Device host/IP (or set $U64_HOST). No default.")
    ap.add_argument("--workers", type=int, default=len(TEST_FILES))
    ap.add_argument(
        "--file-timeout", type=_positive_seconds, default=None, metavar="SECONDS",
        help="Wall-clock cap per file. Default: none -- a file's run includes "
        "its per-test lock waits, which the conftest guard bounds (#380).",
    )
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
            pool.submit(_run_file, f, args.host, password, args.file_timeout): f
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
