"""A ``bool`` is not a C64 address on the VICE backend either (#352).

``bool`` subclasses ``int``, so ``BinaryViceTransport.read_memory(True, n)``
and ``write_memory(True, data)`` passed the span checks and sent a monitor
Memory Get/Set at ``$0001`` -- the 6510 processor port.  #340 refuses a bool
on every Ultimate 64 entry point; the ``C64Transport`` protocol should refuse
the same inputs on every backend, so a flag passed positionally fails the
same way under VICE.

Both methods now raise ``ValueError`` ("... must be an int, not bool") before
any monitor command.  Offline only: ``_connect`` is patched out and
``_send_and_recv`` -- the single path every monitor command takes -- is
replaced by a recorder, so "no command" is observed, not inferred.  No VICE
is launched.
"""
from __future__ import annotations

import struct
from unittest.mock import patch

import pytest

from c64_test_harness.backends.vice_binary import (
    CMD_MEM_GET,
    CMD_MEM_SET,
    BinaryViceTransport,
)
from c64_test_harness.memory_policy import MemoryPolicy, MemoryPolicyError, MemoryRegion

_BOOLS = [True, False]


class _Resp:
    def __init__(self, body: bytes) -> None:
        self.body = body


class _Recorder:
    """Stands in for ``_send_and_recv``; records (command, start address)."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    def __call__(self, cmd_type: int, body: bytes = b"") -> _Resp:
        start = struct.unpack_from("<H", body, 1)[0] if len(body) >= 3 else -1
        self.calls.append((cmd_type, start))
        if cmd_type == CMD_MEM_GET:
            end = struct.unpack_from("<H", body, 3)[0]
            n = end - start + 1
            return _Resp(struct.pack("<H", n) + bytes(n))
        return _Resp(b"")


def _transport(policy: MemoryPolicy | None = None):
    with patch.object(BinaryViceTransport, "_connect"):
        t = BinaryViceTransport(memory_policy=policy)
    rec = _Recorder()
    t._send_and_recv = rec  # type: ignore[method-assign]
    return t, rec


# --------------------------------------------------------------------------- #
# Refusal                                                                     #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("flag", _BOOLS)
@pytest.mark.parametrize("length", [1, 0], ids=["1B", "0B"])
def test_read_memory_refuses_a_bool_before_any_command(flag, length):
    t, rec = _transport()
    with pytest.raises(ValueError, match="not bool"):
        t.read_memory(flag, length)
    assert rec.calls == []


@pytest.mark.parametrize("flag", _BOOLS)
@pytest.mark.parametrize("data", [b"\x37", [0x37], b""], ids=["bytes", "list", "empty"])
def test_write_memory_refuses_a_bool_before_any_command(flag, data):
    t, rec = _transport()
    with pytest.raises(ValueError, match="not bool"):
        t.write_memory(flag, data)
    assert rec.calls == []


@pytest.mark.parametrize("flag", _BOOLS)
def test_bool_is_refused_before_the_memory_policy(flag):
    """A bool is a bad argument, not a policy decision: the ValueError comes
    first even under a policy that would reserve the bytes it names."""
    policy = MemoryPolicy.permissive().with_reserved(MemoryRegion(0x0000, 0x0002, "zp"))
    t, rec = _transport(policy)
    with pytest.raises(ValueError, match="not bool"):
        t.write_memory(flag, b"\x37")
    assert rec.calls == []


# --------------------------------------------------------------------------- #
# Positive controls                                                           #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("address", [0, 1])
def test_the_real_ints_still_send_commands(address):
    t, rec = _transport()
    assert t.read_memory(address, 1) == b"\x00"
    t.write_memory(address, b"\x37")
    assert rec.calls == [(CMD_MEM_GET, address), (CMD_MEM_SET, address)]


def test_the_policy_control_really_reserves_the_port():
    """Control for the ordering test: an int address under that policy is a
    policy refusal, so the bool test's ValueError is not the policy's."""
    policy = MemoryPolicy.permissive().with_reserved(MemoryRegion(0x0000, 0x0002, "zp"))
    t, rec = _transport(policy)
    with pytest.raises(MemoryPolicyError):
        t.write_memory(1, b"\x37")
    assert rec.calls == []
