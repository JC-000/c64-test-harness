#!/usr/bin/env python3
"""Ultimate 64 end-to-end hello: connect, read screen, type, verify.

Drives a real Ultimate 64 over its REST API and exercises the high-level
harness helpers (``wait_for_text``, ``wait_for_stable``, ``send_text``,
``send_key``) against the hardware backend.

What it does:
  1. Opens an ``Ultimate64Transport`` to the device.
  2. Prints identity info (product + firmware) via ``Ultimate64Client``.
  3. Waits for the ``READY.`` prompt to confirm the C64 is at BASIC.
  4. Types ``PRINT 2+2`` + RETURN.
  5. Waits briefly, re-reads the screen, and verifies ``4`` appears.
  6. Closes the transport.

Safety: READ-ONLY with respect to device configuration. Nothing is
loaded, nothing is mounted, nothing is reconfigured. The only side
effect is a few keystrokes typed into BASIC — user can clear with
RUN/STOP+RESTORE if desired.

Usage:
    python3 examples/ultimate64_hello.py --host 192.168.1.81
"""
from __future__ import annotations

import argparse
import sys
import time

from c64_test_harness import (
    ScreenGrid,
    Ultimate64Transport,
    send_key,
    send_text,
    wait_for_stable,
    wait_for_text,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="192.168.1.81",
                        help="Ultimate 64 host / IP (default: 192.168.1.81)")
    parser.add_argument("--password", default=None,
                        help="X-Password header value (if device requires it)")
    parser.add_argument("--timeout", type=float, default=20.0,
                        help="Per-step timeout in seconds (default: 20)")
    args = parser.parse_args()

    print(f"Connecting to Ultimate 64 at {args.host} ...")
    transport = Ultimate64Transport(host=args.host, password=args.password)
    try:
        # Identity check via the underlying client.
        info = transport._client.get_info()
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


if __name__ == "__main__":
    raise SystemExit(main())
