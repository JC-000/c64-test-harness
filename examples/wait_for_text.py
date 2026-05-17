#!/usr/bin/env python3
"""Minimal example: spawn VICE, wait for BASIC's READY. prompt, print matching lines.

Demonstrates the canonical pattern: `ViceInstanceManager` owns the VICE process
lifecycle (port allocation, PID tracking, transport creation, cleanup) and
`wait_for_text` polls the screen via the binary monitor transport. The manager
context cleanly terminates VICE on exit.
"""

import sys

from c64_test_harness import (
    ViceConfig,
    ViceInstanceManager,
    wait_for_text,
)


def main() -> int:
    # No prg_path: VICE boots to the BASIC "READY." prompt on its own.
    config = ViceConfig(warp=True, sound=False)

    with ViceInstanceManager(config=config) as mgr:
        inst = mgr.acquire()
        print(f"VICE PID={inst.pid}, port={inst.port}")

        transport = inst.transport

        print("Waiting for 'READY.' on screen...")
        grid = wait_for_text(transport, "READY.", timeout=30.0, verbose=False)

        if grid is None:
            print("Timed out.")
            mgr.release(inst)
            return 1

        print("Found! Lines containing 'READY.':")
        for line in grid.text().splitlines():
            if "READY." in line:
                print(f"  {line!r}")

        mgr.release(inst)

    return 0


if __name__ == "__main__":
    sys.exit(main())
