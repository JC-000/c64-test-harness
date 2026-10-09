"""Tests for SocketDMAClient and SocketDMAIdentifyUDP.

Uses an in-process fake TCP server to assert on the exact bytes sent and
to feed synthetic replies back to the client; uses a fake UDP responder
for identify.  No live hardware required.
"""
from __future__ import annotations

import json
import socket
import struct
import threading
import time
from contextlib import closing
from typing import Optional
from unittest.mock import MagicMock

import pytest

from c64_test_harness.backends.u64_socket_dma import (
    REU_WRITE_MAX_CHUNK,
    SocketDMAClient,
    SocketDMAIdentifyUDP,
)
from c64_test_harness.backends.ultimate64 import Ultimate64Transport
from c64_test_harness.backends.ultimate64_client import Ultimate64Error


# ---------- fake TCP server ------------------------------------------------


class FakeSocketDMAServer:
    """One-connection fake of the U64 SocketDMA TCP/64 endpoint.

    Reads the 2-byte LE opcode + 2-byte LE length + payload framing,
    records the requests, and replies according to ``replies`` (a dict
    keyed by opcode).  ``replies[opcode]`` is the bytes the server sends
    after that op; if the opcode is not in the dict, the server stays
    silent (matches real-device behaviour for write-only ops).
    """

    def __init__(self, replies: Optional[dict[int, bytes]] = None,
                 disconnect_on: Optional[set[int]] = None) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self.requests: list[tuple[int, bytes]] = []
        self._replies = replies or {}
        self._disconnect_on = disconnect_on or set()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._client_sock: Optional[socket.socket] = None
        self._stop = False
        self._thread.start()

    def _serve(self) -> None:
        try:
            self._sock.settimeout(2.0)
            client, _ = self._sock.accept()
            self._client_sock = client
            client.settimeout(2.0)
            while not self._stop:
                header = self._recv_exact(client, 4)
                if header is None:
                    return
                opcode, length = struct.unpack("<HH", header)
                payload = b""
                if length:
                    payload = self._recv_exact(client, length) or b""
                self.requests.append((opcode, payload))
                if opcode in self._replies:
                    client.sendall(self._replies[opcode])
                if opcode in self._disconnect_on:
                    return
        except (OSError, socket.timeout):
            return
        finally:
            try:
                if self._client_sock is not None:
                    self._client_sock.close()
            except OSError:
                pass

    @staticmethod
    def _recv_exact(sock: socket.socket, n: int) -> Optional[bytes]:
        buf = bytearray()
        while len(buf) < n:
            try:
                chunk = sock.recv(n - len(buf))
            except (OSError, socket.timeout):
                return None
            if not chunk:
                return None
            buf.extend(chunk)
        return bytes(buf)

    def stop(self) -> None:
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=2.0)


@pytest.fixture
def fake_server():
    servers: list[FakeSocketDMAServer] = []

    def _make(replies=None, disconnect_on=None):
        s = FakeSocketDMAServer(replies=replies, disconnect_on=disconnect_on)
        servers.append(s)
        return s

    yield _make
    for s in servers:
        s.stop()


# ---------- TCP client tests ----------------------------------------------


def test_reset_sends_opcode_only(fake_server):
    srv = fake_server()
    client = SocketDMAClient("127.0.0.1", port=srv.port, timeout=2.0)
    with client:
        client.reset()
    # Give server thread a beat to record
    for _ in range(50):
        if srv.requests:
            break
        time.sleep(0.01)
    assert srv.requests == [(0xFF04, b"")]


def test_dma_load_packs_address_le(fake_server):
    srv = fake_server()
    with SocketDMAClient("127.0.0.1", port=srv.port, timeout=2.0) as c:
        c.dma_load(0x0801, b"\x01\x02\x03")
    for _ in range(50):
        if srv.requests:
            break
        time.sleep(0.01)
    assert len(srv.requests) == 1
    opcode, payload = srv.requests[0]
    assert opcode == 0xFF01
    assert payload == b"\x01\x08\x01\x02\x03"


def test_dma_load_run_uses_dmarun_opcode(fake_server):
    srv = fake_server()
    with SocketDMAClient("127.0.0.1", port=srv.port, timeout=2.0) as c:
        c.dma_load(0x1000, b"\xAA", run=True)
    for _ in range(50):
        if srv.requests:
            break
        time.sleep(0.01)
    assert srv.requests[0][0] == 0xFF02
    assert srv.requests[0][1] == b"\x00\x10\xAA"


def test_dma_jump_packs_address_le(fake_server):
    srv = fake_server()
    with SocketDMAClient("127.0.0.1", port=srv.port, timeout=2.0) as c:
        c.dma_jump(0xFCE2)
    for _ in range(50):
        if srv.requests:
            break
        time.sleep(0.01)
    assert srv.requests == [(0xFF09, b"\xE2\xFC")]


def test_inject_keys_sends_petscii_payload(fake_server):
    srv = fake_server()
    with SocketDMAClient("127.0.0.1", port=srv.port, timeout=2.0) as c:
        c.inject_keys("RUN\r")
    for _ in range(50):
        if srv.requests:
            break
        time.sleep(0.01)
    assert srv.requests == [(0xFF03, b"RUN\r")]


_IDENTIFY_REPLY = bytes([4]) + b"Fake"


def test_reu_write_packs_24bit_offset(fake_server):
    srv = fake_server(replies={0xFF0E: _IDENTIFY_REPLY})
    with SocketDMAClient("127.0.0.1", port=srv.port, timeout=2.0) as c:
        c.reu_write(0x123456, b"\xDE\xAD\xBE\xEF")
    # sync=True (default): the write is followed by an IDENTIFY barrier.
    assert [op for op, _ in srv.requests] == [0xFF07, 0xFF0E]
    opcode, payload = srv.requests[0]
    assert payload == b"\x56\x34\x12\xDE\xAD\xBE\xEF"


def test_reu_write_sync_false_skips_barrier(fake_server):
    srv = fake_server()
    with SocketDMAClient("127.0.0.1", port=srv.port, timeout=2.0) as c:
        c.reu_write(0x000000, b"\x01\x02", sync=False)
    for _ in range(50):
        if srv.requests:
            break
        time.sleep(0.01)
    assert [op for op, _ in srv.requests] == [0xFF07]


def test_reu_write_barrier_failure_raises(fake_server):
    # Server silent on IDENTIFY -> barrier recv times out -> clear error.
    srv = fake_server()
    with SocketDMAClient("127.0.0.1", port=srv.port, timeout=0.3) as c:
        with pytest.raises(Ultimate64Error, match="completion barrier"):
            c.reu_write(0x000000, b"\x01\x02")


def test_reu_write_rejects_oversize_offset(fake_server):
    srv = fake_server()
    with SocketDMAClient("127.0.0.1", port=srv.port, timeout=2.0) as c:
        with pytest.raises(Ultimate64Error):
            c.reu_write(0x1000000, b"\x00")


def test_reu_write_chunks_oversize_payload_on_the_wire(fake_server):
    """A 64 KiB + 1 REU write becomes two framed REUWRITE commands.

    Wire-level counterpart of the chunking unit tests in
    ``test_snapshot_reu.py``: each command's 16-bit length field covers
    the 3-byte offset prefix + data, so a chunk carries at most
    ``REU_WRITE_MAX_CHUNK`` (65532) data bytes and the second command's
    offset advances by exactly one chunk.
    """
    data = bytes((i * 13) & 0xFF for i in range(65536 + 1))
    srv = fake_server(replies={0xFF0E: _IDENTIFY_REPLY})
    with SocketDMAClient("127.0.0.1", port=srv.port, timeout=2.0) as c:
        c.reu_write(0x000100, data)
    assert [op for op, _ in srv.requests] == [0xFF07, 0xFF07, 0xFF0E]
    srv.requests = srv.requests[:2]
    first, second = (payload for _, payload in srv.requests)
    assert len(first) == 3 + REU_WRITE_MAX_CHUNK == 0xFFFF
    assert int.from_bytes(first[:3], "little") == 0x000100
    assert int.from_bytes(second[:3], "little") == 0x000100 + REU_WRITE_MAX_CHUNK
    assert first[3:] + second[3:] == data


def test_identify_returns_title(fake_server):
    title = b"Ultimate-64"
    reply = bytes([len(title)]) + title
    srv = fake_server(replies={0xFF0E: reply})
    with SocketDMAClient("127.0.0.1", port=srv.port, timeout=2.0) as c:
        info = c.identify()
    assert info == {"title": "Ultimate-64"}


def test_authenticate_success_sets_authenticated(fake_server):
    srv = fake_server(replies={0xFF1F: b"\x01"})
    c = SocketDMAClient("127.0.0.1", port=srv.port, password="secret",
                        timeout=2.0)
    with c:
        assert c._authenticated is True
    for _ in range(50):
        if srv.requests:
            break
        time.sleep(0.01)
    assert srv.requests[0] == (0xFF1F, b"secret")


def test_authenticate_failure_raises(fake_server):
    srv = fake_server(replies={0xFF1F: b"\x00"}, disconnect_on={0xFF1F})
    with pytest.raises(Ultimate64Error, match="rejected"):
        SocketDMAClient(
            "127.0.0.1", port=srv.port, password="bad", timeout=2.0
        ).__enter__()


def test_authenticate_without_password_raises():
    c = SocketDMAClient("127.0.0.1", port=1, timeout=0.5)
    with pytest.raises(Ultimate64Error, match="without a password"):
        c.authenticate()


def test_connect_failure_raises():
    # Bind+close to grab a port that nobody listens on.
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(Ultimate64Error, match="connect"):
        SocketDMAClient("127.0.0.1", port=port, timeout=0.5).__enter__()


def test_payload_too_large_raises(fake_server):
    srv = fake_server()
    with SocketDMAClient("127.0.0.1", port=srv.port, timeout=2.0) as c:
        with pytest.raises(Ultimate64Error, match="too large"):
            # _send rejects payloads > 0xFFFF.
            c._send(0xFF01, b"\x00" * 0x10000)


def test_dma_write_packs_address(fake_server):
    srv = fake_server()
    with SocketDMAClient("127.0.0.1", port=srv.port, timeout=2.0) as c:
        c.dma_write(0xC000, b"\x12\x34")
    for _ in range(50):
        if srv.requests:
            break
        time.sleep(0.01)
    assert srv.requests[0] == (0xFF06, b"\x00\xC0\x12\x34")


def test_one_shot_call_outside_context_manager(fake_server):
    srv = fake_server()
    c = SocketDMAClient("127.0.0.1", port=srv.port, timeout=2.0)
    c.reset()
    for _ in range(50):
        if srv.requests:
            break
        time.sleep(0.01)
    assert srv.requests == [(0xFF04, b"")]
    # one-shot path should leave no socket open
    assert c._sock is None


# ---------- UDP identify tests --------------------------------------------


class FakeUDPIdentifyServer:
    def __init__(self, reply: bytes) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("127.0.0.1", 0))
        self.port = self._sock.getsockname()[1]
        self._reply = reply
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        self._sock.settimeout(2.0)
        try:
            data, addr = self._sock.recvfrom(4096)
            self._sock.sendto(self._reply, addr)
        except (OSError, socket.timeout):
            return

    def stop(self) -> None:
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=2.0)


def test_udp_identify_parses_json_reply():
    payload = json.dumps({
        "product": "Ultimate-64",
        "firmware_version": "V3.14",
        "hostname": "ultimate-64",
    }).encode("ascii")
    srv = FakeUDPIdentifyServer(payload)
    try:
        replies = SocketDMAIdentifyUDP.identify(
            host="127.0.0.1", port=srv.port, timeout=1.0
        )
    finally:
        srv.stop()
    assert len(replies) == 1
    assert replies[0]["product"] == "Ultimate-64"
    assert replies[0]["source_addr"] == "127.0.0.1"


def test_udp_identify_handles_non_json_reply():
    srv = FakeUDPIdentifyServer(b"json,host,Ultimate-64")
    try:
        replies = SocketDMAIdentifyUDP.identify(
            host="127.0.0.1", port=srv.port, timeout=1.0
        )
    finally:
        srv.stop()
    assert len(replies) == 1
    assert replies[0]["raw"] == "json,host,Ultimate-64"


def test_udp_identify_returns_empty_on_timeout():
    # No server bound — sendto succeeds (UDP) but recv times out.
    with closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    # Now port is free; no responder.
    replies = SocketDMAIdentifyUDP.identify(
        host="127.0.0.1", port=port, timeout=0.3
    )
    assert replies == []


# ---------- Ultimate64Transport.inject_joystick ---------------------------


def test_inject_joystick_port1_writes_dc01():
    fake_client = MagicMock()
    t = Ultimate64Transport(host="ignored", client=fake_client)
    # value is active-high (protocol convention); bits 0-4 are inverted
    # onto the active-low CIA lines, bits 5-7 pass through.
    t.inject_joystick(1, 0x7F)
    fake_client.write_mem.assert_called_once_with(0xDC01, bytes([0x7F ^ 0x1F]))


def test_inject_joystick_port2_writes_dc00():
    fake_client = MagicMock()
    t = Ultimate64Transport(host="ignored", client=fake_client)
    t.inject_joystick(2, 0xEF)
    fake_client.write_mem.assert_called_once_with(0xDC00, bytes([0xEF ^ 0x1F]))


def test_inject_joystick_invalid_port_raises():
    fake_client = MagicMock()
    t = Ultimate64Transport(host="ignored", client=fake_client)
    with pytest.raises(ValueError, match="port must be 1 or 2"):
        t.inject_joystick(3, 0x00)
    fake_client.write_mem.assert_not_called()


def test_inject_joystick_invalid_value_raises():
    fake_client = MagicMock()
    t = Ultimate64Transport(host="ignored", client=fake_client)
    with pytest.raises(ValueError, match="out of byte range"):
        t.inject_joystick(1, 0x100)
    fake_client.write_mem.assert_not_called()


# ---------------------------------------------------------------------------
# Issue #223: the firmware closes an idle connection after 1 s
# ---------------------------------------------------------------------------
#
# ``software/network/socket_dma.cc`` sets SO_RCVTIMEO = 1 s on the accepted
# socket and the command loop closes it on the first recv <= 0.  Measured on
# the U64E (fw 3.15 fork, 2026-09-05): IDENTIFY after a 0.90 s gap 3/3 ok,
# after 1.00 s "closed by peer" 3/3.  ``_FirmwareLikeSock`` models exactly
# that against a fake clock: bytes written after the idle limit are never
# read and every recv answers EOF.


class _FirmwareLikeSock:
    """One accepted SocketDMA connection as the firmware behaves."""

    IDLE_CLOSE = 1.0

    def __init__(self, clock) -> None:
        self._clock = clock
        self._last_cmd = clock()
        self.closed_by_peer = False
        self.sent: list[bytes] = []
        self._replies: list[bytes] = []
        self.closed = False

    def settimeout(self, value) -> None:
        self.timeout = value

    def gettimeout(self):
        return getattr(self, "timeout", None)

    def sendall(self, data: bytes) -> None:
        if self._clock() - self._last_cmd >= self.IDLE_CLOSE:
            self.closed_by_peer = True
        if self.closed_by_peer:
            # The kernel accepts the bytes; the peer never reads them.
            return
        self.sent.append(bytes(data))
        self._last_cmd = self._clock()
        opcode = data[0] | (data[1] << 8)
        if opcode == 0xFF0E:
            self._replies.append(b"\x04FAKE")

    def recv(self, n: int) -> bytes:
        if self.closed_by_peer or not self._replies:
            return b""
        out = self._replies[0][:n]
        self._replies[0] = self._replies[0][n:]
        if not self._replies[0]:
            self._replies.pop(0)
        return out

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def firmware_like(monkeypatch):
    """SocketDMAClient against _FirmwareLikeSock with a controllable clock."""
    from c64_test_harness.backends import u64_socket_dma as mod

    state = {"now": 1000.0, "socks": []}

    def clock() -> float:
        return state["now"]

    def create_connection(addr, timeout=None):
        s = _FirmwareLikeSock(clock)
        state["socks"].append(s)
        return s

    monkeypatch.setattr(mod.time, "monotonic", clock)
    monkeypatch.setattr(mod.socket, "create_connection", create_connection)
    return state


def test_idle_gap_reopens_connection_before_next_command(firmware_like) -> None:
    state = firmware_like
    with SocketDMAClient(host="fake") as c:
        assert c.identify() == {"title": "FAKE"}
        state["now"] += 1.5                      # device closes the socket
        assert c.identify() == {"title": "FAKE"}  # transparently reconnected
        assert len(state["socks"]) == 2
        assert state["socks"][0].closed is True
        assert c.idle_reconnects == 1


def test_within_idle_window_connection_is_reused(firmware_like) -> None:
    state = firmware_like
    with SocketDMAClient(host="fake") as c:
        c.identify()
        state["now"] += 0.5
        c.identify()
        assert len(state["socks"]) == 1
        assert c.idle_reconnects == 0


def test_idle_reconnect_threshold_is_below_the_firmware_timer(firmware_like) -> None:
    """The default must reopen strictly before the device's 1 s close."""
    from c64_test_harness.backends.u64_socket_dma import IDLE_RECONNECT_SECONDS

    assert 0 < IDLE_RECONNECT_SECONDS < _FirmwareLikeSock.IDLE_CLOSE
    state = firmware_like
    with SocketDMAClient(host="fake") as c:
        c.identify()
        state["now"] += IDLE_RECONNECT_SECONDS + 1e-3   # at-or-past the threshold
        c.identify()
        assert len(state["socks"]) == 2


def test_idle_reconnect_none_surfaces_the_peer_close(firmware_like) -> None:
    state = firmware_like
    with SocketDMAClient(host="fake", idle_reconnect=None) as c:
        c.identify()
        state["now"] += 1.5
        with pytest.raises(Ultimate64Error, match="closed by peer.*idle"):
            c.identify()
        assert len(state["socks"]) == 1


def test_fire_and_forget_after_idle_gap_is_not_lost(firmware_like) -> None:
    """The dangerous case: DMAWRITE has no reply, so without the reconnect
    the bytes vanish silently and only a later barrier notices."""
    state = firmware_like
    with SocketDMAClient(host="fake") as c:
        c.identify()
        state["now"] += 1.5
        c.dma_write(0x4000, b"\x01\x02\x03\x04")
        assert len(state["socks"]) == 2
        assert state["socks"][1].sent[-1][:2] == b"\x06\xff"   # reached the peer
        assert state["socks"][0].sent[-1][:2] != b"\x06\xff"


def test_idle_reconnect_rejects_non_positive() -> None:
    with pytest.raises(ValueError):
        SocketDMAClient(host="fake", idle_reconnect=0)


# ---------------------------------------------------------------------------
# A slow-draining device: the send timeout must scale with the payload
# ---------------------------------------------------------------------------
#
# A large command's ``sendall`` cannot finish until the device has taken most
# of it, and Python applies the socket timeout to the whole ``sendall``
# (whether the C64U's REU drain or its WiFi/VPN path was the slow part on
# 2026-10-09 was not measured).
# The REUWRITE barrier already budgets the worst C64U drain rate
# (``_REU_DRAIN_FLOOR_BPS``); the sends before it must too, or a 64 KiB
# command times out at the flat 5 s client timeout before the barrier is
# reached (amiga64 on the C64U, fw 1.1.0, 2026-10-09: "SocketDMA send
# failed: timed out" on the first 1 MB REUWRITE).


class _SlowDrainSock:
    """A connection whose peer reads at ``rate`` bytes/s, with no buffering.

    ``sendall`` fails the way a real socket does when the drain outlasts the
    timeout in force; ``recv`` records the timeout in force for the reply.
    """

    def __init__(self, rate: float) -> None:
        self.rate = rate
        self.timeout = None
        self.send_timeouts: list[tuple[int, float]] = []
        self.recv_timeouts: list[float] = []
        self._replies: list[bytes] = []

    def settimeout(self, value) -> None:
        self.timeout = value

    def gettimeout(self):
        return self.timeout

    def sendall(self, data: bytes) -> None:
        self.send_timeouts.append((len(data), self.timeout))
        if self.timeout is not None and len(data) / self.rate > self.timeout:
            raise socket.timeout("timed out")
        if data[:2] == b"\x0e\xff":
            self._replies.append(b"\x04FAKE")

    def recv(self, n: int) -> bytes:
        self.recv_timeouts.append(self.timeout)
        if not self._replies:
            return b""
        out = self._replies[0][:n]
        self._replies[0] = self._replies[0][n:]
        if not self._replies[0]:
            self._replies.pop(0)
        return out

    def close(self) -> None:
        pass


@pytest.fixture
def slow_drain(monkeypatch):
    from c64_test_harness.backends import u64_socket_dma as mod

    socks: list[_SlowDrainSock] = []

    def create_connection(addr, timeout=None):
        s = _SlowDrainSock(rate=5 * 1024)   # the slowest C64U drain observed
        socks.append(s)
        return s

    monkeypatch.setattr(mod.socket, "create_connection", create_connection)
    return socks


def test_large_reu_write_survives_a_slow_drain(slow_drain) -> None:
    """1 MB at 5 KiB/s: each 64 KiB command takes ~12.8 s to drain, more
    than the flat 5 s client timeout."""
    from c64_test_harness.backends.u64_socket_dma import (
        REU_WRITE_MAX_CHUNK, _REU_DRAIN_FLOOR_BPS,
    )

    with SocketDMAClient(host="fake", timeout=5.0) as c:
        c.reu_write(0xF00000, bytes(1 << 20))
    sock = slow_drain[0]
    writes = [(n, t) for n, t in sock.send_timeouts if n > 4]
    assert len(writes) == -(-(1 << 20) // REU_WRITE_MAX_CHUNK)
    for n, t in writes:
        assert t >= 5.0 + (n - 4) / _REU_DRAIN_FLOOR_BPS


def test_send_scaling_keeps_the_barrier_budget(slow_drain) -> None:
    """The barrier's IDENTIFY send must not shrink the barrier's own scaled
    recv timeout back to the flat client timeout."""
    from c64_test_harness.backends.u64_socket_dma import _REU_DRAIN_FLOOR_BPS

    size = 200_000
    with SocketDMAClient(host="fake", timeout=5.0) as c:
        c.reu_write(0, bytes(size))
        assert slow_drain[0].timeout == 5.0      # restored afterwards
    barrier = 5.0 + size / _REU_DRAIN_FLOOR_BPS
    assert slow_drain[0].recv_timeouts
    assert min(slow_drain[0].recv_timeouts) >= barrier
    # The IDENTIFY itself queues behind the drain, so its send needs it too.
    (n, t), = [(n, t) for n, t in slow_drain[0].send_timeouts if n == 4]
    assert t >= barrier


def test_small_command_keeps_the_flat_timeout(slow_drain) -> None:
    with SocketDMAClient(host="fake", timeout=5.0) as c:
        c.identify()
    (n, t), = slow_drain[0].send_timeouts
    assert n == 4 and t == pytest.approx(5.0, abs=0.01)


def test_stretched_send_timeout_is_put_back(slow_drain) -> None:
    """Without a barrier to reset it, a later reply read must not inherit a
    write's drain budget (a dead device would then hang for that long)."""
    with SocketDMAClient(host="fake", timeout=5.0) as c:
        c.reu_write(0, bytes(100_000), sync=False)
        assert slow_drain[0].timeout == 5.0


def test_blocking_client_needs_no_budget(slow_drain) -> None:
    """``timeout=None`` (the stdlib's "blocking") stays usable for every
    command, the barrier included."""
    with SocketDMAClient(host="fake", timeout=None) as c:
        assert c.identify() == {"title": "FAKE"}
        c.reu_write(0, bytes(100_000))
    assert all(t is None for _, t in slow_drain[0].send_timeouts)


def test_failed_reu_send_names_the_range_it_may_have_left_stale(slow_drain) -> None:
    from c64_test_harness.backends.u64_socket_dma import REU_WRITE_MAX_CHUNK

    with SocketDMAClient(host="fake", timeout=0.1) as c:
        c._sock.rate = 1                  # every command times out
        with pytest.raises(Ultimate64Error, match=r"REU 0x100000-0x10fffb may hold stale"):
            c.reu_write(0x100000, bytes(2 * REU_WRITE_MAX_CHUNK))
        assert c._sock is None            # a cut-off stream is not reused


def test_reu_error_that_sent_nothing_is_not_labelled_stale(slow_drain, monkeypatch) -> None:
    """A reconnect that fails before a chunk is sent wrote nothing to the REU."""
    from c64_test_harness.backends import u64_socket_dma as mod
    from c64_test_harness.backends.u64_socket_dma import REU_WRITE_MAX_CHUNK

    with SocketDMAClient(host="fake", timeout=5.0, idle_reconnect=None) as c:
        real_send = mod.SocketDMAClient._send
        calls = []

        def send(self, opcode, payload=b""):
            calls.append(opcode)
            if len(calls) == 2:
                raise Ultimate64Error("SocketDMA connect to fake:64 failed: refused")
            return real_send(self, opcode, payload)

        monkeypatch.setattr(mod.SocketDMAClient, "_send", send)
        with pytest.raises(Ultimate64Error) as info:
            c.reu_write(0, bytes(2 * REU_WRITE_MAX_CHUNK), sync=False)
    assert "may hold stale" not in str(info.value)


def test_interrupted_send_drops_the_connection(slow_drain) -> None:
    """A command cut off by a non-OSError (Ctrl-C) leaves the stream mid-
    command; the next command must start on a fresh connection."""
    with SocketDMAClient(host="fake", timeout=5.0) as c:
        def interrupted(data):
            raise KeyboardInterrupt
        c._sock.sendall = interrupted
        with pytest.raises(KeyboardInterrupt):
            c.reu_write(0, bytes(1000), sync=False)
        assert c._sock is None
