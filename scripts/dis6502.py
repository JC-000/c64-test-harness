"""Thin CLI shim over c64_test_harness.disasm's 6502/6510 opcode table.

Used by scripts/vice_keyecho_probe.py (``from dis6502 import dis``) to
render VICE's CPU history, and from the command line on memory dumps::

    python3 scripts/dis6502.py <rom.bin> <base-hex> <start-end> [<start-end> ...]

Two disassemblers landed from two PRs -- this one (issue #170) and the
library module built for jsr-timeout failure messages -- each with its
own 256-entry opcode table. They have since been consolidated: the table
lives only in ``c64_test_harness.disasm``; this module carries none of
its own, and asks that module for its established lower-case-bytes,
``.byte``-truncated-tail rendering via ``disassemble(..., upper=False)``
rather than re-deriving it from a duplicate table.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from c64_test_harness.disasm import disassemble  # noqa: E402


def dis(mem: bytes, base: int, start: int, end: int) -> str:
    """Disassemble the instructions of ``mem`` (which starts at address
    ``base``) whose addresses fall in ``[start, end)``.

    Matches the original script's semantics: decoding starts at *start*
    and always runs to a full instruction (or the truncated tail of
    ``mem``), even if that instruction's last byte lands at or past
    *end* -- only which instructions to *start* is bounded by *end*.
    """
    submem = bytes(mem[start - base:])
    lines = disassemble(submem, start, upper=False)
    return "\n".join(line for line in lines if int(line[:4], 16) < end)


if __name__ == "__main__":
    rom = open(sys.argv[1], "rb").read()
    base = int(sys.argv[2], 16)
    for rng in sys.argv[3:]:
        s, e = (int(x, 16) for x in rng.split("-"))
        print(f"--- {rng}")
        print(dis(rom, base, s, e))
