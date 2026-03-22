#!/usr/bin/env python3
"""Test VICE's built-in monitor logging commands (log on / logname).

This script investigates whether VICE's text monitor supports the `log` and
`logname` commands over the remote TCP interface.  It also tests whether a
`-moncommands` startup script can enable logging at VICE start.

Findings are printed to stdout.  If VICE (x64sc) is not on PATH the script
still completes -- it just reports that live testing was skipped.

Usage:
    python3 scripts/test_monitor_logging.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from c64_test_harness.backends.vice import ViceTransport
from c64_test_harness.backends.vice_lifecycle import ViceConfig, ViceProcess

VICE_EXE = "x64sc"
PORT = 6520  # avoid collisions with default 6510


def _vice_available() -> bool:
    return shutil.which(VICE_EXE) is not None


# ---------------------------------------------------------------------------
# Test 1: send `logname` / `log on` interactively
# ---------------------------------------------------------------------------

def test_log_commands_interactive(port: int, log_path: str) -> dict:
    """Connect to a running VICE instance, send log commands, then exercise
    the monitor and check what ended up in the log file."""

    t = ViceTransport(port=port, timeout=5.0)
    results: dict = {}

    # -- Try to enable logging --
    resp_logname = t.raw_command(f"logname \"{log_path}\"")
    results["logname_response"] = resp_logname

    resp_log_on = t.raw_command("log on")
    results["log_on_response"] = resp_log_on

    # -- Now issue some normal commands that should be logged --
    resp_regs = t.raw_command("r")
    results["r_response"] = resp_regs

    resp_mem = t.raw_command("m 0400 040f")
    results["m_response"] = resp_mem

    # Read what ended up in the log
    time.sleep(0.5)  # give VICE a moment to flush
    if os.path.exists(log_path):
        with open(log_path) as f:
            results["log_contents"] = f.read()
    else:
        results["log_contents"] = "<file not created>"

    return results


# ---------------------------------------------------------------------------
# Test 2: -moncommands startup script
# ---------------------------------------------------------------------------

def test_moncommands_startup(log_path: str) -> dict:
    """Launch VICE with a -moncommands file that enables logging, then send
    a few commands and check the log."""

    results: dict = {}

    # Create a moncommands file
    moncommands = tempfile.NamedTemporaryFile(
        mode="w", suffix=".mon", delete=False, prefix="vice_monlog_"
    )
    moncommands_path = moncommands.name
    moncommands.write(f'logname "{log_path}"\n')
    moncommands.write("log on\n")
    moncommands.close()
    results["moncommands_path"] = moncommands_path
    results["moncommands_content"] = open(moncommands_path).read()

    port = PORT + 1
    config = ViceConfig(
        executable=VICE_EXE,
        port=port,
        warp=True,
        ntsc=True,
        sound=False,
        minimize=True,
        extra_args=["-moncommands", moncommands_path],
    )

    try:
        with ViceProcess(config) as proc:
            proc.wait_for_monitor(timeout=10)

            t = ViceTransport(port=port, timeout=5.0)

            # Issue commands after moncommands has (hopefully) run
            resp_r = t.raw_command("r")
            results["r_response"] = resp_r

            resp_m = t.raw_command("m 0400 040f")
            results["m_response"] = resp_m

            time.sleep(0.5)
            if os.path.exists(log_path):
                with open(log_path) as f:
                    results["log_contents"] = f.read()
            else:
                results["log_contents"] = "<file not created>"

    except Exception as e:
        results["error"] = str(e)
    finally:
        os.unlink(moncommands_path)

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 70)
    print("VICE Monitor Logging Investigation")
    print("=" * 70)

    if not _vice_available():
        print(f"\n{VICE_EXE} not found on PATH -- skipping live tests.")
        print("The script structure is still valid; run on a machine with VICE.\n")
        _print_expected_findings()
        return

    tmpdir = tempfile.mkdtemp(prefix="vice_logtest_")
    print(f"Temp dir: {tmpdir}")

    # --- Test 1: interactive log commands ---
    print("\n--- Test 1: Interactive log/logname commands ---")
    log1 = os.path.join(tmpdir, "test1_interactive.log")
    config1 = ViceConfig(
        executable=VICE_EXE, port=PORT, warp=True, ntsc=True,
        sound=False, minimize=True,
    )

    try:
        with ViceProcess(config1) as proc:
            proc.wait_for_monitor(timeout=10)
            r1 = test_log_commands_interactive(PORT, log1)
            _print_results("Test 1", r1)
    except Exception as e:
        print(f"  ERROR: {e}")

    # --- Test 2: -moncommands startup ---
    print("\n--- Test 2: -moncommands startup script ---")
    log2 = os.path.join(tmpdir, "test2_moncommands.log")
    try:
        r2 = test_moncommands_startup(log2)
        _print_results("Test 2", r2)
    except Exception as e:
        print(f"  ERROR: {e}")

    print(f"\nLog files left in: {tmpdir}")
    print("Inspect them manually for full details.\n")

    _print_expected_findings()


def _print_results(label: str, results: dict) -> None:
    for k, v in results.items():
        if isinstance(v, str) and len(v) > 200:
            v = v[:200] + "..."
        print(f"  {k}: {v!r}")


def _print_expected_findings() -> None:
    print("\n" + "=" * 70)
    print("EXPECTED FINDINGS (from VICE documentation and prior testing)")
    print("=" * 70)
    print("""
1. `log` and `logname` commands:
   - These are documented in VICE's monitor help (`help log`, `help logname`).
   - `logname` sets the filename, `log on` enables logging.
   - Over TCP remote monitor, each connection is independent. VICE processes
     the `logname` and `log on` commands, but since we close and reopen the
     connection for each subsequent command, the log state may or may not
     persist between connections.

2. Key behavior with per-command connections:
   - VICE's text monitor accepts one TCP connection at a time.
   - When we disconnect, VICE closes the monitor session.
   - The `log` state may be session-scoped (lost on disconnect) or global.
   - This is the critical question: does `log on` persist across connections?

3. -moncommands approach:
   - The `-moncommands` file runs commands when the monitor initializes.
   - This is more promising because it runs before any TCP connection.
   - But: the log file may only capture the moncommands session itself,
     not subsequent TCP sessions.

4. Log format (when it works):
   - VICE logs the literal command text and response, similar to what you
     see on a serial terminal connected to the monitor.
   - Not machine-parseable (no timestamps, no framing).

5. Verdict:
   - VICE-side logging is unreliable for our use case because:
     a) Per-command TCP connections mean state may not persist
     b) We cannot control the log format
     c) The log may miss commands if state is session-scoped
   - Transport-level logging (Python side) is strictly superior for
     debugging agent mistakes.
""")


if __name__ == "__main__":
    main()
