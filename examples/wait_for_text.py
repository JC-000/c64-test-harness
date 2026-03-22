#!/usr/bin/env python3
"""Minimal example: connect to VICE binary monitor, wait for text, exit."""

from c64_test_harness import BinaryViceTransport, wait_for_text

transport = BinaryViceTransport()  # localhost:6510

print("Waiting for 'READY.' on screen...")
grid = wait_for_text(transport, "READY.", timeout=30, verbose=False)

if grid:
    print("Found! Screen contents:")
    print(grid.text())
else:
    print("Timed out.")

transport.close()
