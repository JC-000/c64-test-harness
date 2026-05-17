#!/usr/bin/env python3
"""Ultimate 64 end-to-end hello: connect, read screen, type, verify.

Drives a real Ultimate 64 over its REST API and exercises the high-level
harness helpers (``wait_for_text``, ``wait_for_stable``, ``send_text``,
``send_key``) against the hardware backend.

What it does:
  1. Acquires the cross-process ``DeviceLock`` for the U64 host so we
     don't collide with another agent / test run on shared hardware.
  2. Opens an ``Ultimate64Transport`` to the device.
  3. Prints identity info (product + firmware) via the public
     ``transport.client`` accessor.
  4. Waits for the ``READY.`` prompt to confirm the C64 is at BASIC.
  5. Types ``PRINT 2+2`` + RETURN.
  6. Waits briefly, re-reads the screen, and verifies ``4`` appears.
  7. Closes the transport and releases the lock.

Safety: READ-ONLY with respect to device configuration. Nothing is
loaded, nothing is mounted, nothing is reconfigured. The only side
effect is a few keystrokes typed into BASIC — user can clear with
RUN/STOP+RESTORE if desired.

Usage:
    python3 examples/ultimate64_hello.py --host 192.168.1.81

    # Or via environment variables (handy in CI / scripted runs):
    U64_HOST=192.168.1.81 U64_PASSWORD=secret \
        python3 examples/ultimate64_hello.py
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from c64_test_harness import (
    DeviceLock,
    DeviceLockTimeout,
    ScreenGrid,
    Ultimate64Transport,
    send_key,
    send_text,
    wait_for_stable,
    wait_for_text,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--host",
        default=os.environ.get("U64_HOST", "192.168.1.81"),
        help="Ultimate 64 host / IP (default: $U64_HOST or 192.168.1.81)",
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("U64_PASSWORD"),
        help="X-Password header value (default: $U64_PASSWORD if set)",
    )
    parser.add_argument("--timeout", type=float, default=20.0,
                        help="Per-step timeout in seconds (default: 20)")
    parser.add_argument("--lock-timeout", type=float, default=60.0,
                        help="Seconds to wait for the device lock (default: 60)")
    args = parser.parse_args()

    # Cross-process device lock: prevents concurrent agents / scripts
    # from talking to the same U64 at the same time. If somebody else
    # holds it longer than --lock-timeout, we bow out cleanly rather
    # than racing for the wire.
    lock = DeviceLock(args.host)
    try:
        lock.acquire_or_raise(timeout=args.lock_timeout)
    except DeviceLockTimeout as e:
        print(f"Skipping: {e}")
        return 0

    try:
        print(f"Connecting to Ultimate 64 at {args.host} ...")
        transport = Ultimate64Transport(
            host=args.host, password=args.password, timeout=8.0,
        )
        try:
            # Identity check via the public client accessor.
            info = transport.client.get_info()
            inner = info.get("info", info) if isinstance(info, dict) else {}
            product = inner.get("product", "<unknown>")
            firmware = inner.get("firmware_version", "<unknown>")
            print(f"  product={product}  firmware={firmware}")

            # Confirm BASIC is idle before we touch the keyboard.
            print("Waiting for READY. prompt ...")
            grid = wait_for_text(
                transport,
                "READY.",
                timeout=args.timeout,
                verbose=False,
            )
            if grid is None:
                print(f"ERROR: READY. not found within {args.timeout}s", file=sys.stderr)
                return 2

            # Let the screen settle (avoids typing while kernal is still drawing).
            wait_for_stable(transport, timeout=args.timeout, stable_count=2)

            # Inject "PRINT 2+2" + RETURN.
            print("Typing: PRINT 2+2 <RETURN>")
            send_text(transport, "PRINT 2+2")
            send_key(transport, 13)  # RETURN

            # Give BASIC a moment to execute and redraw.
            deadline = time.monotonic() + args.timeout
            found_four = False
            while time.monotonic() < deadline:
                g = ScreenGrid.from_transport(transport)
                text = g.continuous_text()
                # Expect BASIC's output " 4" (with leading space) after the
                # echoed command and before the next READY. prompt.
                if " 4" in text and text.rfind(" 4") > text.rfind("PRINT 2+2"):
                    found_four = True
                    break
                time.sleep(0.25)

            if not found_four:
                print("ERROR: did not see '4' after PRINT 2+2", file=sys.stderr)
                print("--- last screen ---", file=sys.stderr)
                print(ScreenGrid.from_transport(transport).text(), file=sys.stderr)
                return 3

            print("OK: BASIC returned 4 as expected.")
            return 0
        finally:
            transport.close()
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
