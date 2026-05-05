#!/usr/bin/env python3
"""Wrapper to run the live U64 SID test with U64_HOST set in the process env.

Usage:
    python3 scripts/run_sid_u64_live.py [HOST]

Defaults HOST to 192.168.1.81.
"""
from __future__ import annotations

import os
import sys


def main() -> int:
    host = sys.argv[1] if len(sys.argv) > 1 else "192.168.1.81"
    os.environ["U64_HOST"] = host
    import pytest
    return pytest.main(["tests/test_sid_u64_live.py", "-v"])


if __name__ == "__main__":
    sys.exit(main())
