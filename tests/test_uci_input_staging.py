"""Staged UCI input must not sit inside the routine that reads it (issue #322).

``uci_tcp_connect``/``uci_udp_connect`` stage the hostname, and
``uci_socket_read``/``uci_socket_close`` the socket id, at ``$C100``, then
``_execute_uci_routine`` writes the routine at ``$C000``.  Plain routines
(116-174 B) end below ``$C100``; turbo routines (348-491 B) do not, so the
upload overwrote the input and the routine sent bytes of its own code as
the hostname or socket id.

These tests drive the **real helpers and the real ``_execute_uci_routine``**
against a memory-backed transport.  When the helper types ``SYS`` (the
write to ``$00C6``), the whole memory image -- exactly as the host left it --
runs in the 6502 interpreter from ``test_uci_turbo_fence_register`` against
its fake UCI, so what is asserted is what the routine actually sent.

Evidence grade: interpreter model only; nothing here has run on a device.
"""
from __future__ import annotations

import pytest

from c64_test_harness import uci_network as u
from c64_test_harness.uci_network import (
    NET_CMD_SOCKET_CLOSE,
    NET_CMD_SOCKET_READ,
    NET_CMD_TCP_CONNECT,
    NET_CMD_UDP_CONNECT,
    TARGET_NETWORK,
    _CODE_ADDR,
    build_socket_close,
    build_socket_read,
    build_tcp_connect,
    build_udp_connect,
    uci_socket_close,
    uci_socket_read,
    uci_tcp_connect,
    uci_udp_connect,
)
from test_uci_turbo_fence_register import _UciMachine  # same-directory helper

_KEYBUF_COUNT = 0x00C6


class _MemTransport:
    """Transport over a 64 KiB image; ``SYS`` runs the image in the interpreter."""

    def __init__(self, reply: bytes = b"\x03") -> None:
        self.mem = bytearray(0x10000)
        self.reply = reply
        self.writes: list[tuple[int, int]] = []
        self.cmd_bytes: list[int] | None = None

    def write_memory(self, addr: int, data: bytes) -> None:
        data = bytes(data)
        self.writes.append((addr, len(data)))
        self.mem[addr:addr + len(data)] = data
        if addr == _KEYBUF_COUNT:
            cpu = _UciMachine(b"", data=self.reply)
            cpu.mem[:] = self.mem
            cpu.run()
            self.mem[:] = cpu.mem
            self.cmd_bytes = cpu.cmd_bytes

    def read_memory(self, addr: int, length: int) -> bytes:
        return bytes(self.mem[addr:addr + length])

    def reset(self, *args, **kwargs) -> None:  # pragma: no cover - timeout only
        raise AssertionError("routine timed out in the interpreter model")

    def code_span(self) -> tuple[int, int]:
        spans = [(a, n) for a, n in self.writes if a == _CODE_ADDR]
        assert len(spans) == 1, spans
        return _CODE_ADDR, _CODE_ADDR + spans[0][1]


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(u.time, "sleep", lambda s: None)


PATHS = pytest.mark.parametrize("turbo", [False, True], ids=["plain", "turbo"])
HOSTS = ["a", "10.0.0.2", "host.example.org"]


@PATHS
@pytest.mark.parametrize("host", HOSTS)
@pytest.mark.parametrize("helper,cmd", [
    (uci_tcp_connect, NET_CMD_TCP_CONNECT),
    (uci_udp_connect, NET_CMD_UDP_CONNECT),
], ids=["tcp", "udp"])
class TestConnectSendsTheHostname:
    def test_the_routine_sends_the_staged_hostname(
        self, turbo: bool, host: str, helper, cmd: int
    ) -> None:
        t = _MemTransport(reply=b"\x05")
        sock = helper(t, host, 0x1234, timeout=1.0, turbo_safe=turbo)
        assert t.cmd_bytes == [TARGET_NETWORK, cmd, 0x34, 0x12,
                               *host.encode("ascii"), 0x00]
        assert sock == 5


@PATHS
class TestSocketIdReachesTheCommand:
    def test_socket_read(self, turbo: bool) -> None:
        payload = b"payload!!"
        t = _MemTransport(reply=bytes([len(payload), 0]) + payload)
        got = uci_socket_read(t, 0x2A, len(payload), timeout=1.0,
                              turbo_safe=turbo)
        assert t.cmd_bytes == [TARGET_NETWORK, NET_CMD_SOCKET_READ, 0x2A,
                               len(payload), 0]
        assert got == payload

    def test_socket_close(self, turbo: bool) -> None:
        t = _MemTransport(reply=b"")
        uci_socket_close(t, 0x2A, timeout=1.0, turbo_safe=turbo)
        assert t.cmd_bytes == [TARGET_NETWORK, NET_CMD_SOCKET_CLOSE, 0x2A]


CALLS = [
    ("tcp", lambda t, ts: uci_tcp_connect(t, "host.example.org", 80,
                                          timeout=1.0, turbo_safe=ts)),
    ("udp", lambda t, ts: uci_udp_connect(t, "host.example.org", 53,
                                          timeout=1.0, turbo_safe=ts)),
    ("read", lambda t, ts: uci_socket_read(t, 1, 4, timeout=1.0, turbo_safe=ts)),
    ("close", lambda t, ts: uci_socket_close(t, 1, timeout=1.0, turbo_safe=ts)),
]


@PATHS
@pytest.mark.parametrize("name,call", CALLS, ids=[c[0] for c in CALLS])
def test_no_staged_write_lands_inside_the_routine(turbo: bool, name, call) -> None:
    """Structural form of the defect: any host write that intersects the
    routine's own span is either overwritten by the upload or overwrites it."""
    t = _MemTransport(reply=b"\x04\x00abcd" if name == "read" else b"\x01")
    call(t, turbo)
    lo, hi = t.code_span()
    clash = [(hex(a), n) for a, n in t.writes
             if a != _CODE_ADDR and a < hi and a + n > lo]
    assert not clash, f"writes inside routine span ${lo:04X}-${hi - 1:04X}: {clash}"


class TestTheBuilderGuard:
    """A caller who passes an input address inside the routine gets an error
    at build time instead of a routine that reads its own code."""

    def test_turbo_connect_refuses_a_host_inside_the_routine(self) -> None:
        with pytest.raises(ValueError, match="host_addr"):
            build_tcp_connect(0xC100, 80, turbo_safe=True)

    def test_turbo_udp_connect_refuses_a_host_inside_the_routine(self) -> None:
        with pytest.raises(ValueError, match="host_addr"):
            build_udp_connect(0xC100, 53, turbo_safe=True)

    def test_turbo_read_refuses_a_socket_id_inside_the_routine(self) -> None:
        with pytest.raises(ValueError, match="socket_id_addr"):
            build_socket_read(0xC100, turbo_safe=True)

    def test_turbo_close_refuses_a_socket_id_inside_the_routine(self) -> None:
        with pytest.raises(ValueError, match="socket_id_addr"):
            build_socket_close(0xC100, turbo_safe=True)

    def test_an_address_just_past_the_routine_is_accepted(self) -> None:
        """Positive control on the bound: end of routine is exclusive."""
        n = len(build_socket_close(turbo_safe=True))
        build_socket_close(_CODE_ADDR + n, turbo_safe=True)

    def test_an_address_at_the_last_routine_byte_is_refused(self) -> None:
        n = len(build_socket_close(turbo_safe=True))
        with pytest.raises(ValueError):
            build_socket_close(_CODE_ADDR + n - 1, turbo_safe=True)

    @pytest.mark.parametrize("builder", [build_tcp_connect, build_udp_connect,
                                         build_socket_read, build_socket_close])
    def test_turbo_defaults_build(self, builder) -> None:
        builder(turbo_safe=True)

    @pytest.mark.parametrize("builder", [build_tcp_connect, build_udp_connect,
                                         build_socket_read, build_socket_close])
    def test_plain_defaults_still_read_c100(self, builder) -> None:
        """Plain routines are unchanged: they still read input at ``$C100``."""
        assert b"\x00\xc1" in builder(turbo_safe=False)
