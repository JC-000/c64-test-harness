#!/usr/bin/env python3
"""Minimal example: connect to VICE, wait for text, exit."""

from c64_test_harness import ViceTransport, ScreenGrid, wait_for_text

transport = ViceTransport()  # localhost:6510

print("Waiting for 'READY.' on screen...")
grid = wait_for_text(transport, "READY.", timeout=30)

if grid:
    print("Found! Screen contents:")
    print(grid.text())
else:
    print("Timed out.")
