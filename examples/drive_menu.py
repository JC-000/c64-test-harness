#!/usr/bin/env python3
"""Example: navigate a C64 menu by sending keys and waiting for screen text."""

import time

from c64_test_harness import (
    BinaryViceTransport, ViceProcess, ViceConfig,
    ScreenGrid, send_text, send_key, wait_for_text,
)


def connect_binary_transport(port, timeout=30.0, proc=None):
    """Connect to VICE binary monitor with retries."""
    deadline = time.monotonic() + timeout
    last_err = None
    while time.monotonic() < deadline:
        if proc is not None and proc._proc is not None and proc._proc.poll() is not None:
            raise RuntimeError("VICE process exited during binary monitor connect")
        try:
            return BinaryViceTransport(port=port)
        except Exception as e:
            last_err = e
            time.sleep(1)
    raise ConnectionError(f"Could not connect to binary monitor on port {port}: {last_err}")



config = ViceConfig(prg_path="build/mygame.prg")

with ViceProcess(config) as vice:
    vice.start()
    transport = connect_binary_transport(port=config.port, proc=vice)

    # Wait for main menu
    grid = wait_for_text(transport, "MAIN MENU", timeout=60, verbose=False)
    if not grid:
        transport.close()
        raise SystemExit("Main menu did not appear")
    print("Main menu ready")

    # Navigate to option 1
    send_key(transport, "1")
    grid = wait_for_text(transport, "ENTER NAME:", timeout=10, verbose=False)
    if grid:
        send_text(transport, "PLAYER ONE\r")
        print("Name entered!")

    transport.close()
