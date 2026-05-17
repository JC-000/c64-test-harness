#!/usr/bin/env python3
"""Example: navigate a C64 menu by sending keys and waiting for screen text."""

from c64_test_harness import (
    ViceConfig, ViceInstanceManager,
    send_text, send_key, wait_for_text,
)


config = ViceConfig(prg_path="build/mygame.prg")

with ViceInstanceManager(config=config) as mgr:
    inst = mgr.acquire()
    transport = inst.transport

    # Wait for main menu
    grid = wait_for_text(transport, "MAIN MENU", timeout=60.0)
    if grid is None:
        raise SystemExit("Main menu did not appear")
    print("Main menu ready")

    # Navigate to option 1
    send_key(transport, "1")
    grid = wait_for_text(transport, "ENTER NAME:", timeout=10.0)
    if grid is not None:
        send_text(transport, "PLAYER ONE\r")
        print("Name entered!")

    mgr.release(inst)
