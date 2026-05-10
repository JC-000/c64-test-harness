"""SocketDMA client for the Ultimate 64 binary protocol on TCP port 64.

The U64 firmware exposes a binary command channel on TCP/64 distinct from the
REST API.  It covers capabilities the REST API does not — keyboard injection,
REU writes, Kernal patching, raw RESET, DMA load/jump — and is the source of
truth for the LAN identify reply on UDP/64.

Wire format (confirmed against ``software/network/socket_dma.cc``):

* request: 2-byte LE opcode + 2-byte LE length + payload
* the special ``MOUNT_IMG``/``RUN_IMG``/``RUN_CRT`` opcodes prepend an
  additional 1-byte high-order length byte (24-bit length); none of those
  are exposed by this client today
* response: opcode-specific.  ``AUTHENTICATE`` returns 1 byte (1 = ok,
  0 = fail, then the device sleeps 1s and disconnects).  ``IDENTIFY``
  returns 1 length byte + an ASCII title.  Most other opcodes have no
  reply and the only confirmation is that the connection stays open.

Authentication (opcode 0xFF1F) was added in firmware 3.12.  When a network
password is configured, *every* connection must authenticate first or the
device closes the socket.
"""
from __future__ import annotations

import json
import socket
import struct
from typing import Optional

from .ultimate64_client import Ultimate64Error

__all__ = [
    "SocketDMAClient",
    "SocketDMAIdentifyUDP",
]


_CMD_DMA = 0xFF01
_CMD_DMARUN = 0xFF02
_CMD_KEYB = 0xFF03
_CMD_RESET = 0xFF04
_CMD_DMAWRITE = 0xFF06
_CMD_REUWRITE = 0xFF07
_CMD_KERNALWRITE = 0xFF08
_CMD_DMAJUMP = 0xFF09
_CMD_IDENTIFY = 0xFF0E
_CMD_AUTHENTICATE = 0xFF1F


class SocketDMAClient:
    """Client for the U64 SocketDMA binary protocol on TCP 64.

    Use as a context manager to open the socket once and reuse it across
    several commands.  When used outside ``with``, each public method opens
    and closes its own connection; this is convenient for one-shot calls
    but slow for chained operations because every connect re-authenticates.
    """

    def __init__(
        self,
        host: str,
        port: int = 64,
        password: Optional[str] = None,
        timeout: float = 5.0,
    ) -> None:
        self._host = host
        self._port = port
        self._password = password
        self._timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._authenticated = False

    def __enter__(self) -> "SocketDMAClient":
        self._connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None
                self._authenticated = False

    def _connect(self) -> None:
        if self._sock is not None:
            return
        try:
            sock = socket.create_connection(
                (self._host, self._port), timeout=self._timeout
            )
        except OSError as exc:
            raise Ultimate64Error(
                f"SocketDMA connect to {self._host}:{self._port} failed: {exc}"
            ) from exc
        sock.settimeout(self._timeout)
        self._sock = sock
        self._authenticated = False
        if self._password is not None:
            self.authenticate()

    def _ensure_connected(self) -> socket.socket:
        if self._sock is None:
            self._connect()
        assert self._sock is not None
        return self._sock

    def _send(self, opcode: int, payload: bytes = b"") -> None:
        if len(payload) > 0xFFFF:
            raise Ultimate64Error(
                f"SocketDMA payload too large: {len(payload)} bytes (max 65535)"
            )
        sock = self._ensure_connected()
        header = struct.pack("<HH", opcode, len(payload))
        try:
            sock.sendall(header + payload)
        except OSError as exc:
            self.close()
            raise Ultimate64Error(f"SocketDMA send failed: {exc}") from exc

    def _recv_exact(self, n: int) -> bytes:
        sock = self._ensure_connected()
        buf = bytearray()
        while len(buf) < n:
            try:
                chunk = sock.recv(n - len(buf))
            except OSError as exc:
                raise Ultimate64Error(f"SocketDMA recv failed: {exc}") from exc
            if not chunk:
                raise Ultimate64Error(
                    "SocketDMA connection closed by peer "
                    "(authentication failure or password required?)"
                )
            buf.extend(chunk)
        return bytes(buf)

    # ---- protocol ops ------------------------------------------------

    def authenticate(self) -> None:
        """Send 0xFF1F with the configured password.

        The device replies with a single byte: 1 = success, 0 = failure.  On
        failure the firmware sleeps ~1s before closing the socket; callers
        should treat repeated failures as expensive.
        """
        if self._password is None:
            raise Ultimate64Error(
                "SocketDMA authenticate() called without a password configured"
            )
        payload = self._password.encode("ascii")
        # Open socket without recursive auth attempt.
        if self._sock is None:
            try:
                sock = socket.create_connection(
                    (self._host, self._port), timeout=self._timeout
                )
            except OSError as exc:
                raise Ultimate64Error(
                    f"SocketDMA connect to {self._host}:{self._port} failed: {exc}"
                ) from exc
            sock.settimeout(self._timeout)
            self._sock = sock
        self._send(_CMD_AUTHENTICATE, payload)
        reply = self._recv_exact(1)
        if reply[0] != 1:
            self.close()
            raise Ultimate64Error("SocketDMA authentication rejected by device")
        self._authenticated = True

    def identify(self) -> dict:
        """Send 0xFF0E IDENTIFY; return device-info dict.

        The TCP IDENTIFY response is a single length-prefixed ASCII title
        string (NOT the JSON form — JSON identify is UDP-only).  We return
        ``{"title": <str>}`` for parity with the UDP variant.
        """
        opened = self._sock is None
        try:
            self._send(_CMD_IDENTIFY)
            length = self._recv_exact(1)[0]
            title = self._recv_exact(length).decode("ascii", errors="replace") if length else ""
            return {"title": title}
        finally:
            if opened:
                self.close()

    def reset(self) -> None:
        """Send 0xFF04 RESET (recoverable; equivalent to the menu reset)."""
        opened = self._sock is None
        try:
            self._send(_CMD_RESET)
        finally:
            if opened:
                self.close()

    def inject_keys(self, text: str) -> None:
        """Send 0xFF03 KEYB with the text payload.

        The caller is responsible for PETSCII encoding — this method writes
        the bytes verbatim.  The firmware DMAs them into ``$0277`` and sets
        the count at ``$00C6``; the C64 keyboard buffer is 10 bytes, so do
        not send more than 10 codes per call without pacing.
        """
        payload = text.encode("ascii") if isinstance(text, str) else bytes(text)
        if not payload:
            return
        opened = self._sock is None
        try:
            self._send(_CMD_KEYB, payload)
        finally:
            if opened:
                self.close()

    def reu_write(self, offset: int, data: bytes) -> None:
        """Send 0xFF07 REUWRITE: 3-byte LE offset (24-bit) + data.

        The firmware reads only 3 bytes of offset (REU is 16 MB max).
        """
        if not (0 <= offset <= 0xFFFFFF):
            raise Ultimate64Error(f"REU offset {offset:#x} out of range (24-bit)")
        if not data:
            return
        payload = struct.pack("<I", offset)[:3] + bytes(data)
        opened = self._sock is None
        try:
            self._send(_CMD_REUWRITE, payload)
        finally:
            if opened:
                self.close()

    def dma_load(self, address: int, data: bytes, run: bool = False) -> None:
        """Send 0xFF01 DMA (or 0xFF02 DMARUN if run=True).

        Payload is a 2-byte LE load address followed by program bytes.  When
        ``run=True`` the device starts execution immediately after load.
        """
        if not (0 <= address <= 0xFFFF):
            raise Ultimate64Error(f"DMA load address {address:#x} out of range")
        payload = struct.pack("<H", address) + bytes(data)
        opcode = _CMD_DMARUN if run else _CMD_DMA
        opened = self._sock is None
        try:
            self._send(opcode, payload)
        finally:
            if opened:
                self.close()

    def dma_jump(self, address: int) -> None:
        """Send 0xFF09 DMAJUMP: 2-byte LE address."""
        if not (0 <= address <= 0xFFFF):
            raise Ultimate64Error(f"DMA jump address {address:#x} out of range")
        opened = self._sock is None
        try:
            self._send(_CMD_DMAJUMP, struct.pack("<H", address))
        finally:
            if opened:
                self.close()

    def dma_write(self, address: int, data: bytes) -> None:
        """Send 0xFF06 DMAWRITE: 2-byte LE address + data (no autostart)."""
        if not (0 <= address <= 0xFFFF):
            raise Ultimate64Error(f"DMA write address {address:#x} out of range")
        if not data:
            return
        opened = self._sock is None
        try:
            self._send(_CMD_DMAWRITE, struct.pack("<H", address) + bytes(data))
        finally:
            if opened:
                self.close()


class SocketDMAIdentifyUDP:
    """UDP/64 broadcast / unicast identify.

    The firmware listens on UDP port 64 for the same network identify probe
    used by Assembly64.  A request beginning with ``"json"`` returns a JSON
    object; any other request returns a comma-joined ASCII reply
    ``"<echoed>,<hostname>,<menu_header>"``.
    """

    @staticmethod
    def identify(
        host: str = "<broadcast>",
        timeout: float = 2.0,
        port: int = 64,
        probe: bytes = b"json",
    ) -> list[dict]:
        """Send a UDP identify probe and return device replies.

        With the default ``host="<broadcast>"`` this performs a broadcast on
        UDP/64 and waits ``timeout`` seconds, returning one dict per replier.
        For a single device pass its IP as ``host`` (still uses the generic
        broadcast socket so the response can come from any source address).
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            if host == "<broadcast>":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                target = ("255.255.255.255", port)
            else:
                target = (host, port)
            sock.bind(("", 0))
            sock.settimeout(timeout)
            try:
                sock.sendto(probe, target)
            except OSError as exc:
                raise Ultimate64Error(
                    f"SocketDMA identify sendto {target} failed: {exc}"
                ) from exc

            replies: list[dict] = []
            while True:
                try:
                    data, addr = sock.recvfrom(4096)
                except socket.timeout:
                    break
                except OSError:
                    break
                replies.append(_parse_identify_reply(data, addr, probe))
            return replies
        finally:
            sock.close()


def _parse_identify_reply(data: bytes, addr: tuple, probe: bytes) -> dict:
    text = data.decode("ascii", errors="replace")
    if probe.startswith(b"json"):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                obj.setdefault("source_addr", addr[0])
                return obj
        except (ValueError, json.JSONDecodeError):
            pass
    return {"raw": text, "source_addr": addr[0]}
