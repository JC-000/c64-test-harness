#!/usr/bin/env python3
"""Run the live U64 SID test module against a device you name.

Usage:
    python3 scripts/run_sid_u64_live.py <HOST>
    U64_HOST=<device> python3 scripts/run_sid_u64_live.py

``U64_HOST`` is a live gate: setting it is what puts a real device in play.
This script therefore never invents a host. If you name one on the command
line it is exported for the test module; if you already set ``U64_HOST`` it
is used verbatim and left alone; if neither, the run is refused.

**Locking is per test, not per run.** This script takes no ``DeviceLock``;
``tests/conftest.py``'s autouse ``device_lock_guard`` locks each live test and
releases it between tests, so another lane can take the device between two
tests of this run.  That is deliberate (#324).  ``close()`` still drains a
leaking client whatever the lock state, but the lock-release ``/Temp`` drain --
the only one that catches a client a test never closes -- fires only on the
outermost release, and its callbacks are held weakly.  A hold here would defer
that drain on a leak-prone device to the end of the run, and a client collected
before then would never drain.  See ``docs/device_locking.md`` rule 1.

See CLAUDE.md § "Standing hardware-safety clause" before pointing this at the
C64 Ultimate.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _u64_host import require_u64_host  # noqa: E402


_MODULES = [
    "tests/test_sid_u64_live.py",
]


def main() -> int:
    require_u64_host(
        sys.argv[1] if len(sys.argv) > 1 else None,
        argv0="python3 scripts/run_sid_u64_live.py",
        export=True,          # the child pytest process needs the gate
    )

    import pytest
    return pytest.main([*_MODULES, "-v"])


if __name__ == "__main__":
    sys.exit(main())
