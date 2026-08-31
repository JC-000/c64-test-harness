"""Reader-framing tests for the UCI command interface (issue #155).

These assert the *shape of the emitted 6502*, not device behaviour, so they
run with no emulator and no hardware.

The defect: ``_build_read_response`` and ``_build_read_status`` wrote
``CMD_NEXT_DATA`` ($02) to $DF1C after **every** byte. Per Register API v1.1
§2.4.1 that bit is DATA_ACC, and

    "Writing to this bit also causes the transfer of the data/status queues
     to be aborted and reset. Thus, the data response and status response
     queues will be empty after writing this bit."

Reads already advance the queue pointer on the hardware side
(``command_protocol.vhd``, ``response_pointer <= response_pointer + 1`` on the
C64 read strobe), so the per-byte write was not an advance — it was an accept
that flushed the rest. Every response longer than one byte was truncated to
its first byte, on every firmware version.

Correct sequence (Gideon's own client, ``software/6502/unsorted/uci_wedge.s``):

    push command
    while (ctrl & DATA_AV):  read $DF1E     # no control write
    while (ctrl & STAT_AV):  read $DF1F     # no control write
    write CMD_NEXT_DATA once                # DATA_ACC releases both queues

Status must be drained *before* the accept, or the accept destroys it.
"""
from __future__ import annotations

import pytest

from c64_test_harness.uci_network import (
    CMD_NEXT_DATA,
    UCI_CONTROL_STATUS_REG,
    UCI_RESP_DATA_REG,
    UCI_STATUS_DATA_REG,
    build_socket_read,
    _build_acknowledge,
    _build_acknowledge_tsx,
    _build_read_response,
    _build_read_response_tsx,
    _build_read_status,
    _build_read_status_tsx,
    _lo,
    _hi,
)

_LDA_IMM = 0xA9
_STA_ABS = 0x8D
_LDA_ABS = 0xAD

#: LDA #CMD_NEXT_DATA ; STA $DF1C — the DATA_ACC accept.
ACCEPT = [
    _LDA_IMM, CMD_NEXT_DATA,
    _STA_ABS, _lo(UCI_CONTROL_STATUS_REG), _hi(UCI_CONTROL_STATUS_REG),
]

#: LDA $DF1F — a status-queue read.
READ_STATUS_BYTE = [_LDA_ABS, _lo(UCI_STATUS_DATA_REG), _hi(UCI_STATUS_DATA_REG)]

#: LDA $DF1E — a response-queue read.
READ_RESP_BYTE = [_LDA_ABS, _lo(UCI_RESP_DATA_REG), _hi(UCI_RESP_DATA_REG)]


def _find_all(haystack, needle) -> list[int]:
    """Every start index at which *needle* occurs in *haystack*."""
    hay = list(haystack)
    n = len(needle)
    return [i for i in range(len(hay) - n + 1) if hay[i:i + n] == list(needle)]


def _count(haystack, needle) -> int:
    return len(_find_all(haystack, needle))


# ------------------------------------------------- no accept inside the loops
def test_read_response_fragment_has_no_per_byte_accept():
    frag = _build_read_response(0xC000, 0xC400)
    assert _count(frag, ACCEPT) == 0, (
        "the response drain must not write DATA_ACC — doing so flushes the "
        "queue and truncates the reply to one byte (issue #155)"
    )


def test_read_status_fragment_has_no_per_byte_accept():
    frag = _build_read_status(0xC000, 0xC400)
    assert _count(frag, ACCEPT) == 0


def test_read_response_tsx_fragment_has_no_per_byte_accept():
    frag = _build_read_response_tsx(0xC000, 0xC100, 0xC400)
    assert _count(frag, ACCEPT) == 0


def test_read_status_tsx_fragment_has_no_per_byte_accept():
    frag = _build_read_status_tsx(0xC000, 0xC100, 0xC400)
    assert _count(frag, ACCEPT) == 0


# --------------------------------------------- the accept happens exactly once
def test_acknowledge_emits_exactly_one_accept():
    """The accept is required, not something to avoid.

    The VHDL gates the whole block on ``state(1)='1'``, so a write while idle
    is a strict no-op — the old "avoid it" comment was wrong. Without the
    accept the interface is left in Data More and the next command stalls.
    """
    assert _count(_build_acknowledge(), ACCEPT) == 1


def test_acknowledge_tsx_emits_exactly_one_accept():
    assert _count(_build_acknowledge_tsx(0xC000), ACCEPT) == 1


@pytest.mark.parametrize("turbo_safe", [False, True])
def test_socket_read_routine_accepts_exactly_once(turbo_safe):
    code = build_socket_read(turbo_safe=turbo_safe)
    assert _count(code, ACCEPT) == 1


@pytest.mark.parametrize("turbo_safe", [False, True])
def test_status_is_drained_before_the_accept(turbo_safe):
    """Accepting before the status drain destroys the status text."""
    code = build_socket_read(turbo_safe=turbo_safe)
    accepts = _find_all(code, ACCEPT)
    status_reads = _find_all(code, READ_STATUS_BYTE)
    assert accepts and status_reads
    assert max(status_reads) < accepts[0], (
        "the status queue must be drained before DATA_ACC is written"
    )


@pytest.mark.parametrize("turbo_safe", [False, True])
def test_response_is_drained_before_the_status(turbo_safe):
    code = build_socket_read(turbo_safe=turbo_safe)
    resp_reads = _find_all(code, READ_RESP_BYTE)
    status_reads = _find_all(code, READ_STATUS_BYTE)
    assert resp_reads and status_reads
    assert max(resp_reads) < min(status_reads)


# ------------------------------------------------- never latch abort as "done"
@pytest.mark.parametrize("turbo_safe", [False, True])
def test_accept_never_sets_the_abort_bit(turbo_safe):
    """$06 would latch ABORT (bit 2), which forces response_valid low for
    every future reply until the Ultimate clears it — unrecoverable
    client-side. The accept must be a bare $02."""
    code = list(build_socket_read(turbo_safe=turbo_safe))
    for idx in _find_all(code, [_LDA_IMM]):
        if idx + 4 < len(code) and code[idx + 2:idx + 5] == [
            _STA_ABS, _lo(UCI_CONTROL_STATUS_REG), _hi(UCI_CONTROL_STATUS_REG)
        ]:
            written = code[idx + 1]
            assert not (written & 0x04) or written == 0x04, (
                f"control write ${written:02X} combines ABORT with other bits"
            )
