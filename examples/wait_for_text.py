#!/usr/bin/env python3
"""Minimal example: connect to VICE binary monitor, wait for text, exit."""

import time

from c64_test_harness import BinaryViceTransport, ScreenGrid

transport = BinaryViceTransport()  # localhost:6510

print("Waiting for 'READY.' on screen...")
deadline = time.monotonic() + 30
grid = None
while time.monotonic() < deadline:
    g = ScreenGrid.from_transport(transport)
    if g.has_text("READY."):
        grid = g
        break
    transport.resume()
    time.sleep(1.0)

if grid:
    print("Found! Screen contents:")
    print(grid.text())
else:
    print("Timed out.")

transport.close()
