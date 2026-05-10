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


def test_reu_write_packs_24bit_offset(fake_server):
    srv = fake_server()
    with SocketDMAClient("127.0.0.1", port=srv.port, timeout=2.0) as c:
        c.reu_write(0x123456, b"\xDE\xAD\xBE\xEF")
    for _ in range(50):
        if srv.requests:
            break
        time.sleep(0.01)
    assert len(srv.requests) == 1
    opcode, payload = srv.requests[0]
    assert opcode == 0xFF07
    assert payload == b"\x56\x34\x12\xDE\xAD\xBE\xEF"


def test_reu_write_rejects_oversize_offset(fake_server):
    srv = fake_server()
    with SocketDMAClient("127.0.0.1", port=srv.port, timeout=2.0) as c:
        with pytest.raises(Ultimate64Error):
            c.reu_write(0x1000000, b"\x00")


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
    t.inject_joystick(1, 0x7F)
    fake_client.write_mem.assert_called_once_with(0xDC01, b"\x7F")


def test_inject_joystick_port2_writes_dc00():
    fake_client = MagicMock()
    t = Ultimate64Transport(host="ignored", client=fake_client)
    t.inject_joystick(2, 0xEF)
    fake_client.write_mem.assert_called_once_with(0xDC00, b"\xEF")


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
