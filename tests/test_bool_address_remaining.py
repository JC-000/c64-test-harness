"""A flag is not a C64 address on the remaining entry points either (#357).

#340 (PR #350) and #352 (PR #354) refuse a ``bool`` on the U64 client and
on both transports' ``read_memory``/``write_memory``.  ``bool`` subclasses
``int``, so the rest of the address-taking surface still let one through --
and several of them *launder* it into a plain int before any guarded
transport sees it (``True & 0xFF == 1``, ``True + 0 == 1``):

* ``SocketDMAClient.dma_write/dma_load/dma_jump/reu_write`` -- a write to,
  and a CPU jump to, ``$0001`` (the 6510 processor port);
* ``Ultimate64Transport.socket_dma_reu_write`` (REU offset 1) and
  ``Ultimate64Transport.read_memory``, whose ``length <= 0`` early return
  ran before the refusal;
* ``BinaryViceTransport.set_checkpoint``;
* ``execute.goto/jsr/run_subroutine/set_breakpoint/wait_for_pc/load_code``
  -- ``jsr``/``run_subroutine`` assemble ``JSR addr`` into a trampoline,
  ``goto`` masks ``PC & 0xFFFF``;
* ``memory.read_bytes*/write_bytes/read_word_le/read_dword_le/hex_dump`` --
  the chunked paths pass ``addr + offset``;
* ``ethernet.set_cs8900a_mac(base=...)`` -- ``base + offset``.

Ruling (reviewer, #354): the refusal comes **first**, before any range
check, empty-data or zero-length early return, policy, lock, connection or
wire use.  Every test below drives a recording fake whose own methods have
no guard, so "nothing was sent" is observed on the fake, not inferred from
a downstream check.  Positive controls: the real ints ``0`` and ``1`` and an
``IntEnum`` member still go out.

``numpy.bool_``: the precedents refused only ``bool``; ``np.True_`` failed
later and differently (``_wire_hex16``: "address out of range"; VICE and
SocketDMA ``struct.pack``: ``struct.error``) -- but on ``goto``/``jsr`` it
got through (``np.True_ & 0xFFFF == np.int64(1)``, which ``struct.pack``
accepts).  The shared helper (``c64_test_harness._address``) now refuses it
everywhere with the same message.  ``int`` subclasses stay accepted.

No device, no VICE, no socket.
"""
from __future__ import annotations

import enum
import struct
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from c64_test_harness import ethernet, execute, memory
from c64_test_harness._address import is_bool_like, refuse_bool_address
from c64_test_harness.backends import ultimate64 as transport_mod
from c64_test_harness.backends.u64_socket_dma import SocketDMAClient
from c64_test_harness.backends.ultimate64 import Ultimate64Transport
from c64_test_harness.backends.ultimate64_client import Ultimate64Client
from c64_test_harness.backends.vice_binary import (
    CMD_CHECKPOINT_SET,
    CMD_MEM_SET,
    BinaryViceTransport,
)

try:  # numpy is not a harness dependency; its bool is tested when present.
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

_TEST_HOST = "198.51.100.7"  # TEST-NET-2; nothing is there, nothing is sent.

FLAGS = [True, False]
if np is not None:
    FLAGS += [np.True_, np.False_]
_IDS = [f"{type(f).__module__}.{f!r}" for f in FLAGS]
flags = pytest.mark.parametrize("flag", FLAGS, ids=_IDS)


class _Addr(enum.IntEnum):
    PORT = 1


# --------------------------------------------------------------------------- #
# The helper                                                                  #
# --------------------------------------------------------------------------- #

@flags
def test_helper_refuses_flags(flag):
    assert is_bool_like(flag)
    with pytest.raises(ValueError, match="thing must be an int, not bool"):
        refuse_bool_address(flag, "thing")


@pytest.mark.parametrize("value", [0, 1, 0xFFFF, _Addr.PORT, -1, 1.0, "1", None])
def test_helper_passes_everything_else(value):
    """Only the flag is this helper's business; range/type stay with callers."""
    assert not is_bool_like(value)
    refuse_bool_address(value)


@pytest.mark.parametrize("spelling", ["bool", "bool_"])
def test_helper_detects_both_numpy_spellings_without_numpy(spelling):
    """numpy 2.x names its scalar ``numpy.bool``; numpy 1.x ``numpy.bool_``.
    The venv only has one of them, so both are pinned with synthetic types
    (review round 1 on #376: dropping ``"bool_"`` otherwise survived)."""
    fake = type(spelling, (), {"__module__": "numpy"})()
    assert is_bool_like(fake)
    with pytest.raises(ValueError, match="not bool"):
        refuse_bool_address(fake)


@pytest.mark.parametrize("spelling", ["bool", "bool_"])
def test_helper_ignores_same_named_types_outside_numpy(spelling):
    """Control: the module check is load-bearing, not just the name."""
    other = type(spelling, (), {"__module__": "mylib"})()
    assert not is_bool_like(other)
    refuse_bool_address(other)


def test_helper_passes_numpy_ints():
    if np is None:
        pytest.skip("numpy not installed")
    assert not is_bool_like(np.int64(1))
    refuse_bool_address(np.uint16(1))


# --------------------------------------------------------------------------- #
# SocketDMAClient -- recorded _send / _connect, never a socket                #
# --------------------------------------------------------------------------- #

@pytest.fixture
def dma():
    c = SocketDMAClient(_TEST_HOST)
    rec = SimpleNamespace(sends=[], connects=0, identifies=0)

    def _send(opcode, payload=b""):
        rec.sends.append((opcode, bytes(payload)))

    def _connect():
        rec.connects += 1
        raise AssertionError("SocketDMAClient tried to connect")

    def _identify():
        rec.identifies += 1
        return {}

    c._send = _send  # type: ignore[method-assign]
    c._connect = _connect  # type: ignore[method-assign]
    c.identify = _identify  # type: ignore[method-assign]
    return c, rec


_DMA_CALLS = {
    "dma_write": lambda c, a: c.dma_write(a, b"\x37"),
    "dma_write-empty": lambda c, a: c.dma_write(a, b""),
    "dma_load": lambda c, a: c.dma_load(a, b"\x37"),
    "dma_load-empty-run": lambda c, a: c.dma_load(a, b"", run=True),
    "dma_jump": lambda c, a: c.dma_jump(a),
    "reu_write": lambda c, a: c.reu_write(a, b"\x37"),
    "reu_write-empty": lambda c, a: c.reu_write(a, b""),
    "reu_write-nosync": lambda c, a: c.reu_write(a, b"\x37", sync=False),
}


@flags
@pytest.mark.parametrize("call", list(_DMA_CALLS), ids=list(_DMA_CALLS))
def test_socket_dma_client_refuses_a_flag_before_any_send(dma, flag, call):
    c, rec = dma
    with pytest.raises(ValueError, match="not bool"):
        _DMA_CALLS[call](c, flag)
    assert rec.sends == [] and rec.connects == 0 and rec.identifies == 0


@pytest.mark.parametrize("address", [0, 1, _Addr.PORT])
def test_socket_dma_client_real_ints_still_send(dma, address):
    c, rec = dma
    n = int(address)
    c.dma_write(address, b"\x37")
    c.dma_load(address, b"\x37")
    c.dma_jump(address)
    c.reu_write(address, b"\x37", sync=False)
    lo = struct.pack("<H", n)
    assert [p for _, p in rec.sends] == [
        lo + b"\x37", lo + b"\x37", lo, struct.pack("<I", n)[:3] + b"\x37",
    ]


# --------------------------------------------------------------------------- #
# Ultimate64Transport -- read_memory ordering, socket_dma_reu_write           #
# --------------------------------------------------------------------------- #

class _Recorder:
    """Stands in for ``Ultimate64Client._request``; records every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []

    def __call__(self, method, path, *, body=None, content_type=None, query=None, **kw):
        self.calls.append((method, path, query))
        if path == "/v1/machine:readmem":
            return 200, bytes(int(query["length"]))
        return 200, b""


class _FakeSocketDMA:
    """Replaces the class ``Ultimate64Transport`` instantiates."""

    events: list[tuple] = []

    def __init__(self, **kw) -> None:
        _FakeSocketDMA.events.append(("init",))

    def __enter__(self):
        _FakeSocketDMA.events.append(("enter",))
        return self

    def __exit__(self, *exc):
        return None

    def close(self) -> None:
        pass

    def reu_write(self, offset, data) -> None:
        _FakeSocketDMA.events.append(("reu_write", offset, bytes(data)))


@pytest.fixture
def u64(monkeypatch):
    c = Ultimate64Client(_TEST_HOST, write_mem_query_threshold=48,
                         warn_unlocked=False, temp_hygiene=False)
    rec = _Recorder()
    c._request = rec  # type: ignore[method-assign]
    _FakeSocketDMA.events = []
    monkeypatch.setattr(transport_mod, "SocketDMAClient", _FakeSocketDMA)
    return Ultimate64Transport(host=_TEST_HOST, client=c), rec


@flags
@pytest.mark.parametrize("length", [1, 0, -1], ids=["1B", "0B", "neg"])
def test_u64_transport_read_memory_refuses_a_flag_before_the_early_return(u64, flag, length):
    t, rec = u64
    with pytest.raises(ValueError, match="not bool"):
        t.read_memory(flag, length)
    assert rec.calls == []


def test_u64_transport_read_memory_zero_length_control(u64):
    """Control: the early return itself still exists for a real address."""
    t, rec = u64
    assert t.read_memory(1, 0) == b""
    assert rec.calls == []


@flags
@pytest.mark.parametrize("data", [b"\x37", b""], ids=["1B", "empty"])
def test_u64_transport_socket_dma_reu_write_refuses_a_flag(u64, flag, data):
    t, rec = u64
    with pytest.raises(ValueError, match="not bool"):
        t.socket_dma_reu_write(flag, data)
    assert _FakeSocketDMA.events == [] and rec.calls == []


def test_u64_transport_socket_dma_reu_write_control(u64):
    t, _ = u64
    t.socket_dma_reu_write(1, b"\x37")
    assert ("reu_write", 1, b"\x37") in _FakeSocketDMA.events


# --------------------------------------------------------------------------- #
# BinaryViceTransport.set_checkpoint                                          #
# --------------------------------------------------------------------------- #

class _Resp:
    def __init__(self, body: bytes) -> None:
        self.body = body


def _vice():
    with patch.object(BinaryViceTransport, "_connect"):
        t = BinaryViceTransport()
    calls: list[tuple[int, bytes]] = []

    def _send_and_recv(cmd_type, body=b""):
        calls.append((cmd_type, bytes(body)))
        return _Resp(struct.pack("<I", 7) + bytes(18))

    t._send_and_recv = _send_and_recv  # type: ignore[method-assign]
    return t, calls


@flags
def test_vice_set_checkpoint_refuses_a_flag_before_any_command(flag):
    t, calls = _vice()
    with pytest.raises(ValueError, match="not bool"):
        t.set_checkpoint(flag)
    assert calls == []


@pytest.mark.parametrize("address", [0, 1, _Addr.PORT])
def test_vice_set_checkpoint_real_ints_still_send(address):
    t, calls = _vice()
    assert t.set_checkpoint(address) == 7
    assert calls[0][0] == CMD_CHECKPOINT_SET
    assert struct.unpack_from("<HH", calls[0][1]) == (int(address), int(address))


@flags
def test_vice_write_memory_now_refuses_numpy_bool_too(flag):
    """Consolidation: the #354 precedent goes through the shared helper."""
    t, calls = _vice()
    with pytest.raises(ValueError, match="write_memory address must be an int, not bool"):
        t.write_memory(flag, b"\x37")
    assert calls == []


def test_vice_write_memory_control():
    t, calls = _vice()
    t.write_memory(1, b"\x37")
    assert calls[0][0] == CMD_MEM_SET


# --------------------------------------------------------------------------- #
# execute.* -- a VICE-shaped recording fake with no guard of its own          #
# --------------------------------------------------------------------------- #

class _RecordingPolicy:
    def __init__(self, log: list) -> None:
        self._log = log

    def check_call(self, addr, *, override=None) -> None:
        self._log.append(("check_call", addr))


class _Rec:
    """Every transport method records; none refuses anything."""

    screen_cols = 40
    screen_rows = 25

    def __init__(self) -> None:
        self.log: list[tuple] = []
        self._regs = {"PC": 0x0800, "A": 0, "X": 0, "Y": 0, "SP": 0xFF, "FL": 0x20}
        self._last_cp = 0
        self.execution_policy = _RecordingPolicy(self.log)

    def read_memory(self, addr, length):
        self.log.append(("read_memory", addr, length))
        return bytes([0x02] * length)

    def write_memory(self, addr, data, *, override=None):
        self.log.append(("write_memory", addr, bytes(data)))

    def read_registers(self):
        self.log.append(("read_registers",))
        return dict(self._regs)

    def set_registers(self, regs):
        self.log.append(("set_registers", dict(regs)))
        self._regs.update({k.upper(): v for k, v in regs.items()})

    def set_checkpoint(self, addr, **kw):
        self.log.append(("set_checkpoint", addr))
        self._last_cp = addr
        return 1

    def delete_checkpoint(self, n):
        self.log.append(("delete_checkpoint", n))

    def resume(self):
        self.log.append(("resume",))

    def wait_for_stopped(self, timeout=None):
        self.log.append(("wait_for_stopped",))
        self._regs["PC"] = self._last_cp
        return self._last_cp

    def inject_keys(self, codes):
        self.log.append(("inject_keys", list(codes)))


_EXEC_CALLS = {
    "goto": lambda t, a: execute.goto(t, a),
    "goto-cold": lambda t, a: execute.goto(t, a, cold=True),
    "set_breakpoint": lambda t, a: execute.set_breakpoint(t, a),
    "wait_for_pc": lambda t, a: execute.wait_for_pc(t, a, timeout=0.01),
    "load_code": lambda t, a: execute.load_code(t, a, b"\x60"),
    "jsr": lambda t, a: execute.jsr(t, a, timeout=0.01),
    "jsr-recover": lambda t, a: execute.jsr(t, a, timeout=0.01, recover_on_timeout=True),
    "jsr-nopreserve": lambda t, a: execute.jsr(t, a, timeout=0.01, preserve_state=False),
    "jsr-scratch_addr": lambda t, a: execute.jsr(t, 0xC000, timeout=0.01, scratch_addr=a),
    "run_subroutine": lambda t, a: execute.run_subroutine(SimpleNamespace(transport=t), a, timeout=0.01),
    "run_subroutine-trampoline_addr": lambda t, a: execute.run_subroutine(
        SimpleNamespace(transport=t), 0xC000, timeout=0.01, trampoline_addr=a),
}


@flags
@pytest.mark.parametrize("call", list(_EXEC_CALLS), ids=list(_EXEC_CALLS))
def test_execute_refuses_a_flag_before_policy_or_transport(flag, call):
    t = _Rec()
    with pytest.raises(ValueError, match="not bool"):
        _EXEC_CALLS[call](t, flag)
    assert t.log == []


@flags
@pytest.mark.parametrize("which", ["addr", "trampoline_addr"])
def test_run_subroutine_u64_branch_refuses_a_flag(u64, flag, which):
    """The U64 branch assembles the trampoline itself; nothing may go out."""
    t, rec = u64
    kw = {"trampoline_addr": flag} if which == "trampoline_addr" else {}
    addr = 0xC000 if which == "trampoline_addr" else flag
    with pytest.raises(ValueError, match="not bool"):
        execute.run_subroutine(SimpleNamespace(transport=t), addr, timeout=0.01, **kw)
    assert rec.calls == []


@pytest.mark.parametrize("address", [1, _Addr.PORT])
def test_execute_real_ints_still_reach_the_transport(address):
    t = _Rec()
    execute.goto(t, address)
    assert t.log[:2] == [("set_registers", {"PC": 1}), ("resume",)]

    t = _Rec()
    assert execute.set_breakpoint(t, address) == 1
    assert t.log == [("set_checkpoint", 1)]

    t = _Rec()
    t._last_cp = 1
    assert execute.wait_for_pc(t, address)["PC"] == 1

    t = _Rec()
    execute.load_code(t, address, b"\x60")
    assert t.log == [("write_memory", 1, b"\x60")]

    t = _Rec()
    execute.jsr(t, address, timeout=0.01)
    assert ("check_call", 1) in t.log
    assert ("write_memory", 0x0334, bytes([0x20, 0x01, 0x00, 0xEA, 0xEA])) in t.log

    t = _Rec()
    execute.jsr(t, 0xC000, timeout=0.01, scratch_addr=address)
    assert ("write_memory", 1, bytes([0x20, 0x00, 0xC0, 0xEA, 0xEA])) in t.log


def test_run_subroutine_u64_control(u64):
    """Control: an int address on the U64 branch does reach the wire."""
    t, rec = u64

    def _done_on_flag(method, path, *, query=None, **kw):
        rec.calls.append((method, path, query))
        if path == "/v1/machine:readmem":
            fill = 0x02 if query["address"] == "03F1" else 0x00
            return 200, bytes([fill]) * int(query["length"])
        return 200, b""

    t._client._request = _done_on_flag  # type: ignore[method-assign]
    execute.run_subroutine(SimpleNamespace(transport=t), 1, timeout=5.0)
    writes = [q for _, p, q in rec.calls if p == "/v1/machine:writemem"]
    # The 14-byte trampoline at $0360 carries JSR $0001 (20 01 00).
    assert any(q["address"] == "0360" and "200100" in q.get("data", "") for q in writes)


# --------------------------------------------------------------------------- #
# memory.* -- the chunked paths launder True into 1                           #
# --------------------------------------------------------------------------- #

_MEM_CALLS = {
    "read_bytes": lambda t, a: memory.read_bytes(t, a, 1),
    "read_bytes-auto-chunked": lambda t, a: memory.read_bytes(t, a, 300),
    "read_bytes-zero": lambda t, a: memory.read_bytes(t, a, 0),
    "read_bytes_chunked": lambda t, a: memory.read_bytes_chunked(t, a, 300),
    "read_bytes_chunked-zero": lambda t, a: memory.read_bytes_chunked(t, a, 0),
    "read_bytes_verified": lambda t, a: memory.read_bytes_verified(t, a, 1),
    # max_attempts=1 is itself a ValueError: the flag must be named first.
    "read_bytes_verified-bad-attempts": lambda t, a: memory.read_bytes_verified(t, a, 1, max_attempts=1),
    "write_bytes": lambda t, a: memory.write_bytes(t, a, b"\x37"),
    "write_bytes-chunked": lambda t, a: memory.write_bytes(t, a, bytes(200)),
    "write_bytes-empty": lambda t, a: memory.write_bytes(t, a, b""),
    "read_word_le": lambda t, a: memory.read_word_le(t, a),
    "read_dword_le": lambda t, a: memory.read_dword_le(t, a),
    "hex_dump": lambda t, a: memory.hex_dump(t, a, 16),
}


@flags
@pytest.mark.parametrize("call", list(_MEM_CALLS), ids=list(_MEM_CALLS))
def test_memory_helpers_refuse_a_flag_before_any_transport_call(flag, call):
    t = _Rec()
    with pytest.raises(ValueError, match="not bool"):
        _MEM_CALLS[call](t, flag)
    assert t.log == []


@flags
@pytest.mark.parametrize("call", list(_MEM_CALLS), ids=list(_MEM_CALLS))
def test_memory_helpers_name_themselves_in_the_refusal(flag, call):
    """The refusal names the entry point the caller used, not a helper it
    delegates to: ``hex_dump(True, ...)`` says ``hex_dump``, not
    ``read_bytes``.  Without this, deleting hex_dump's own guard survived
    (read_bytes refused on its behalf)."""
    name = call.split("-")[0]
    t = _Rec()
    with pytest.raises(ValueError, match=rf"^{name} address must be an int, not bool"):
        _MEM_CALLS[call](t, flag)


@pytest.mark.parametrize("address", [0, 1, _Addr.PORT])
def test_memory_helpers_real_ints_still_reach_the_transport(address):
    n = int(address)
    t = _Rec()
    memory.read_bytes_chunked(t, address, 300)
    memory.write_bytes(t, address, bytes(200))
    memory.read_word_le(t, address)
    addrs = [e[1] for e in t.log]
    assert addrs[0] == n and addrs[-1] == n and len(addrs) >= 4


# --------------------------------------------------------------------------- #
# ethernet.set_cs8900a_mac(base=...)                                          #
# --------------------------------------------------------------------------- #

@flags
def test_set_cs8900a_mac_refuses_a_flag_base(flag):
    t = _Rec()
    with pytest.raises(ValueError, match="not bool"):
        ethernet.set_cs8900a_mac(t, bytes(6), base=flag)
    assert t.log == []


def test_set_cs8900a_mac_control():
    t = _Rec()
    ethernet.set_cs8900a_mac(t, bytes(6), base=0xDE00)
    assert t.log[0] == ("read_memory", 0xDE01, 1)


# --------------------------------------------------------------------------- #
# Consolidation: the #350 precedents share the helper (numpy.bool_ included) #
# --------------------------------------------------------------------------- #

@flags
def test_u64_precedents_refuse_numpy_bool_with_the_same_message(u64, flag):
    t, rec = u64
    with pytest.raises(ValueError, match="^address must be an int, not bool"):
        t._client.write_mem(flag, b"\x37")
    with pytest.raises(ValueError, match="^address must be an int, not bool"):
        t._client.read_mem(flag, 1)
    with pytest.raises(ValueError, match="^write_memory address must be an int, not bool"):
        t.write_memory(flag, b"\x37")
    assert rec.calls == []
