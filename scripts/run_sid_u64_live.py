#!/usr/bin/env python3
"""Run the live U64 SID test module against a device you name.

Usage:
    python3 scripts/run_sid_u64_live.py <HOST>
    U64_HOST=<device> python3 scripts/run_sid_u64_live.py

``U64_HOST`` is a live gate: setting it is what puts a real device in play.
This script therefore never invents a host. If you name one on the command
line it is exported for the test module; if you already set ``U64_HOST`` it
is used verbatim and left alone; if neither, the run is refused.

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
