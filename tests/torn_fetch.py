"""Every ``JMP`` a running self-``JMP`` loop can execute while it is hijacked (#477).

A host DMA write can halt the 6510 between two fetches of one instruction
(``c64.cc`` ``STOP_COND_FORCE``; #426 measured it: the TOD test's three-byte
hijack ran ``JMP $C210``, old low byte and new high byte, into the middle of
its wrapper).  So a write over ``JMP main_loop`` is safe only when every mix
of old and new bytes the CPU can fetch is either the old instruction or the
new one.

:func:`fetched_targets` enumerates those mixes.  The CPU fetches the opcode,
the low operand byte, then the high one; each write lands whole, at any
boundary between fetches, and later writes land no earlier than earlier ones.
"""
from __future__ import annotations

from itertools import combinations_with_replacement


def fetched_targets(main_loop: int, before: bytes,
                    writes: list[tuple[int, bytes]]) -> set[int | None]:
    """Targets of every ``JMP`` fetched at *main_loop* across *writes*.

    *before* is the three bytes at *main_loop* before the first write;
    *writes* are ``(address, data)`` in the order the host sent them, already
    filtered to the ones that touch ``main_loop .. main_loop + 2``.  A fetch
    whose opcode is not ``JMP abs`` yields ``None``.
    """
    states = [bytearray(before)]
    for addr, data in writes:
        state = bytearray(states[-1])
        for i, b in enumerate(data):
            if 0 <= addr + i - main_loop < 3:
                state[addr + i - main_loop] = b
        states.append(state)
    targets: set[int | None] = set()
    # boundaries[k] = how many fetches happen before write k lands (0..3).
    for boundaries in combinations_with_replacement(range(4), len(writes)):
        fetched = bytearray(3)
        for i in range(3):
            landed = sum(1 for b in boundaries if b <= i)
            fetched[i] = states[landed][i]
        targets.add(fetched[1] | fetched[2] << 8 if fetched[0] == 0x4C else None)
    return targets


def span_writes(main_loop: int, writes: list[tuple[int, bytes]]) -> list[tuple[int, bytes]]:
    """The writes that touch ``main_loop .. main_loop + 2``."""
    return [(a, d) for a, d in writes if a < main_loop + 3 and a + len(d) > main_loop]
