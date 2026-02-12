#!/usr/bin/env python3
"""Example: navigate a C64 menu by sending keys and waiting for screen text."""

from c64_test_harness import (
    ViceTransport, ViceProcess, ViceConfig,
    ScreenGrid, wait_for_text, send_text, send_key,
)

config = ViceConfig(prg_path="build/mygame.prg")

with ViceProcess(config) as vice:
    print("Waiting for VICE monitor...")
    if not vice.wait_for_monitor():
        raise SystemExit("Could not connect to VICE")

    transport = ViceTransport(port=config.port)

    # Wait for main menu
    grid = wait_for_text(transport, "MAIN MENU", timeout=60)
    if not grid:
        raise SystemExit("Main menu did not appear")
    print("Main menu ready")

    # Navigate to option 1
    send_key(transport, "1")
    grid = wait_for_text(transport, "ENTER NAME:", timeout=10)
    if grid:
        send_text(transport, "PLAYER ONE\r")
        print("Name entered!")
