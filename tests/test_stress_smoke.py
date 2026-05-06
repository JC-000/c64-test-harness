"""Subprocess smoke tests for the stress runners under scripts/.

`pyproject.toml` restricts pytest collection to `tests/`, so the stress
runners in `scripts/` aren't exercised by the suite. The macOS-spawn
fix on this branch exists because of exactly that gap — a portability
bug shipped in `scripts/stress_cross_process.py` that the suite never
saw. These wrappers run each stress runner with minimum-cost params,
purely as a smoke check that the script bootstraps and its lightest
phase completes. The full multi-phase / multi-VICE runs stay invokable
manually via the scripts directly.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _run_script(name: str, *args: str, timeout: float = 120.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / name), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_stress_cross_process_lock_only_smoke():
    """No-VICE phase is enough to catch spawn-mode pickling bugs at p.start()."""
    r = _run_script(
        "stress_cross_process.py",
        "--phase", "lock-only",
        "--workers", "2",
        "--rounds", "1",
    )
    assert r.returncode == 0, (
        f"stress_cross_process.py --phase lock-only exited {r.returncode}\n"
        f"--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}"
    )


def test_stress_port_allocation_skip_vice_smoke():
    r = _run_script(
        "stress_port_allocation.py",
        "--workers", "4",
        "--rounds", "1",
        "--skip-vice",
    )
    assert r.returncode == 0, (
        f"stress_port_allocation.py --skip-vice exited {r.returncode}\n"
        f"--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}"
    )


U64_HOST = os.environ.get("U64_HOST")


@pytest.mark.skipif(not U64_HOST, reason="U64_HOST not set — live Ultimate 64 stress requires hardware")
def test_stress_u64_queue_smoke():
    r = _run_script(
        "stress_u64_queue.py",
        U64_HOST or "",
        "--workers", "2",
        "--rounds", "1",
        timeout=180.0,
    )
    assert r.returncode == 0, (
        f"stress_u64_queue.py exited {r.returncode}\n"
        f"--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}"
    )
