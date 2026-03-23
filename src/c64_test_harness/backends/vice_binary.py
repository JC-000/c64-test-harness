"""VICE binary monitor transport -- persistent-connection backend.

Uses VICE's binary monitor protocol (-binarymonitor).  Provides:

- A single persistent TCP connection
- No write-size limitations
- Async breakpoint/stop events pushed by VICE
- ~0.08ms latency per command

Wire format is little-endian throughout.  See VICE documentation for the
full binary monitor protocol specification.
"""

from __future__ import annotations

import socket
import struct
import threading
import time
from collections import deque
from typing import NamedTuple

from ..transport import ConnectionError, TimeoutError, TransportError

# ---------------------------------------------------------------------------
# Command IDs
# ---------------------------------------------------------------------------

CMD_MEM_GET = 0x01
CMD_MEM_SET = 0x02
CMD_CHECKPOINT_SET = 0x12
CMD_CHECKPOINT_DEL = 0x13
CMD_REGISTERS_GET = 0x31
CMD_REGISTERS_SET = 0x32
CMD_KEYBOARD_FEED = 0x72
CMD_REGS_AVAILABLE = 0x83
CMD_EXIT = 0xAA

# Event / response types
EVENT_STOPPED = 0x62
EVENT_RESUMED = 0x63
RESPONSE_CHECKPOINT_INFO = 0x11

# Wire constants
STX = 0x02
API_VERSION = 0x02
REQUEST_HEADER_SIZE = 11
RESPONSE_HEADER_SIZE = 12
EVENT_REQUEST_ID = 0xFFFFFFFF


class _Response(NamedTuple):
    """Parsed binary monitor response."""
    response_type: int
    error_code: int
    request_id: int
    body: bytes


class BinaryViceTransport:
    """C64Transport backed by VICE's binary monitor protocol.

    Maintains a single persistent TCP connection and uses the binary wire
    format for all communication.  Provides ~0.08ms latency per command,
    no write size limits, async breakpoint events, and non-destructive
    resume().
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 6502,
        timeout: float = 5.0,
        screen_base: int = 0x0400,
        keybuf_addr: int = 0x0277,
        keybuf_count_addr: int = 0x00C6,
        keybuf_max: int = 10,
        cols: int = 40,
        rows: int = 25,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.screen_base = screen_base
        self.keybuf_addr = keybuf_addr
        self.keybuf_count_addr = keybuf_count_addr
        self.keybuf_max = keybuf_max
        self._cols = cols
        self._rows = rows

        self._req_id = 0
        self._reg_map: dict[str, tuple[int, int]] = {}  # name -> (reg_id, size_bits)
        self._event_queue: deque[_Response] = deque()
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None

        self._connect()

    # ----- properties (C64Transport protocol) -----

    @property
    def screen_cols(self) -> int:
        return self._cols

    @property
    def screen_rows(self) -> int:
        return self._rows

    # ----- connection management -----

    def _connect(self) -> None:
        """Open TCP connection to VICE binary monitor and initialise."""
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.settimeout(self.timeout)
            self._sock.connect((self.host, self.port))
        except OSError as e:
            raise ConnectionError(
                f"Cannot connect to VICE binary monitor at "
                f"{self.host}:{self.port}: {e}"
            ) from e

        # The first command triggers an auto-stop.  We drain those initial
        # events as part of the register-map initialisation.
        self._init_register_map()

    def _next_req_id(self) -> int:
        """Return an incrementing request ID (wraps at 32 bits)."""
        rid = self._req_id
        self._req_id = (self._req_id + 1) & 0xFFFFFFFF
        return rid

    # ----- low-level wire I/O -----

    def _recv_exact(self, n: int) -> bytes:
        """Read exactly *n* bytes from the socket, handling partial reads."""
        assert self._sock is not None
        buf = bytearray()
        while len(buf) < n:
            try:
                chunk = self._sock.recv(n - len(buf))
            except socket.timeout as e:
                raise TimeoutError(
                    f"Timed out reading from VICE binary monitor "
                    f"(got {len(buf)}/{n} bytes)"
                ) from e
            except OSError as e:
                raise ConnectionError(
                    f"Lost connection to VICE binary monitor: {e}"
                ) from e
            if not chunk:
                raise ConnectionError(
                    f"VICE binary monitor closed connection "
                    f"(got {len(buf)}/{n} bytes)"
                )
            buf.extend(chunk)
        return bytes(buf)

    def _recv_response(self) -> _Response:
        """Read one complete response/event from the wire."""
        header = self._recv_exact(RESPONSE_HEADER_SIZE)
        # Bytes 0-1: STX, API version (validate)
        if header[0] != STX or header[1] != API_VERSION:
            raise TransportError(
                f"Invalid response header: expected STX={STX:#x} API={API_VERSION:#x}, "
                f"got {header[0]:#x} {header[1]:#x}"
            )
        body_length = struct.unpack_from("<I", header, 2)[0]
        response_type = header[6]
        error_code = header[7]
        request_id = struct.unpack_from("<I", header, 8)[0]

        body = self._recv_exact(body_length) if body_length > 0 else b""
        return _Response(response_type, error_code, request_id, body)

    def _send_command(self, cmd_type: int, body: bytes = b"") -> int:
        """Send a binary monitor command, return the request ID used."""
        assert self._sock is not None
        req_id = self._next_req_id()
        header = struct.pack(
            "<BBII",
            STX,
            API_VERSION,
            len(body),
            req_id,
        ) + bytes([cmd_type])
        # header is: STX(1) + API(1) + body_len(4) + req_id(4) + cmd(1) = 11
        try:
            self._sock.sendall(header + body)
        except OSError as e:
            raise ConnectionError(
                f"Failed to send command to VICE binary monitor: {e}"
            ) from e
        return req_id

    def _send_and_recv(self, cmd_type: int, body: bytes = b"") -> _Response:
        """Send a command and wait for its response, buffering any events."""
        with self._lock:
            req_id = self._send_command(cmd_type, body)
            return self._wait_for_response(req_id)

    def _wait_for_response(self, req_id: int) -> _Response:
        """Read responses until we get the one matching *req_id*.

        Any unsolicited events (request_id == 0xFFFFFFFF) or responses
        for other request IDs are buffered in the event queue.
        """
        while True:
            resp = self._recv_response()
            if resp.request_id == req_id:
                if resp.error_code != 0x00:
                    raise TransportError(
                        f"VICE binary monitor error {resp.error_code:#x} "
                        f"for command type {resp.response_type:#x} "
                        f"(req_id={req_id}), body={resp.body.hex()}"
                    )
                return resp
            # Unsolicited event or stale response — buffer it
            self._event_queue.append(resp)

    # ----- register map initialisation -----

    def _init_register_map(self) -> None:
        """Query VICE for available registers and cache name -> (id, size_bits).

        Wire format per entry:
          item_size(1)  -- byte count of the rest of this entry
          reg_id(1)     -- register ID (1 byte, NOT 2)
          size_bits(1)  -- register width in bits (8 or 16)
          name_len(1)   -- length of the name string
          name(N)       -- ASCII register name
        """
        # memspace 0x00 = computer main memory
        body = bytes([0x00])
        resp = self._send_and_recv(CMD_REGS_AVAILABLE, body)

        data = resp.body
        if len(data) < 2:
            raise TransportError("Registers Available response too short")

        count = struct.unpack_from("<H", data, 0)[0]
        off = 2
        self._reg_map = {}
        for _ in range(count):
            if off >= len(data):
                break
            item_size = data[off]
            off += 1
            if off + item_size > len(data):
                break
            reg_id = data[off]
            size_bits = data[off + 1]
            name_len = data[off + 2]
            name = data[off + 3:off + 3 + name_len].decode("ascii", errors="replace")
            off += item_size
            self._reg_map[name.upper()] = (reg_id, size_bits)

    # ----- C64Transport interface -----

    def read_memory(self, addr: int, length: int) -> bytes:
        """Read *length* bytes starting at *addr*.

        Automatically chunks reads that span more than 64 KiB (the 16-bit
        address fields are inclusive, so a single request can read at most
        65536 bytes: 0x0000..0xFFFF).
        """
        if length <= 0:
            return b""

        result = bytearray()
        remaining = length
        current_addr = addr

        while remaining > 0:
            # end_addr is inclusive, and both fields are 16-bit
            chunk_size = min(remaining, 0x10000 - (current_addr & 0xFFFF))
            if chunk_size <= 0:
                chunk_size = min(remaining, 0x10000)

            end_addr = (current_addr + chunk_size - 1) & 0xFFFF
            start_lo = current_addr & 0xFFFF

            # side_effects(1) start(2) end(2) memspace(1) bank(2)
            body = struct.pack("<BHHBH", 0x00, start_lo, end_addr, 0x00, 0x00)
            resp = self._send_and_recv(CMD_MEM_GET, body)

            # Response body: length(2) + data(N)
            if len(resp.body) < 2:
                raise TransportError("Memory Get response too short")
            data_len = struct.unpack_from("<H", resp.body, 0)[0]
            data = resp.body[2:2 + data_len]
            result.extend(data)

            remaining -= len(data)
            current_addr += len(data)

        return bytes(result[:length])

    def write_memory(self, addr: int, data: bytes | list[int]) -> None:
        """Write *data* bytes starting at *addr*."""
        if isinstance(data, list):
            data = bytes(data)
        if not data:
            return

        remaining = data
        current_addr = addr

        while remaining:
            chunk_size = min(len(remaining), 0x10000 - (current_addr & 0xFFFF))
            if chunk_size <= 0:
                chunk_size = min(len(remaining), 0x10000)

            chunk = remaining[:chunk_size]
            end_addr = (current_addr + chunk_size - 1) & 0xFFFF
            start_lo = current_addr & 0xFFFF

            # side_effects(1) start(2) end(2) memspace(1) bank(2) data(N)
            body = struct.pack("<BHHBH", 0x00, start_lo, end_addr, 0x00, 0x00)
            body += chunk
            self._send_and_recv(CMD_MEM_SET, body)

            remaining = remaining[chunk_size:]
            current_addr += chunk_size

    def read_screen_codes(self) -> list[int]:
        """Read raw screen codes from screen memory."""
        total = self._cols * self._rows
        data = self.read_memory(self.screen_base, total)
        return list(data)

    def inject_keys(self, petscii_codes: list[int]) -> None:
        """Inject PETSCII key codes via the Keyboard Feed command."""
        if not petscii_codes:
            return
        # Keyboard Feed: length(1) + text(N)
        # The binary protocol length field is 1 byte, max 255
        for i in range(0, len(petscii_codes), 255):
            batch = petscii_codes[i:i + 255]
            body = bytes([len(batch)]) + bytes(batch)
            self._send_and_recv(CMD_KEYBOARD_FEED, body)

    def read_registers(self) -> dict[str, int]:
        """Read CPU registers, returning a dict with keys PC, A, X, Y, SP."""
        # memspace 0x00
        resp = self._send_and_recv(CMD_REGISTERS_GET, bytes([0x00]))
        return self._parse_register_response(resp.body)

    def resume(self) -> None:
        """Resume CPU execution by sending the Exit command.

        Unlike the text monitor, the binary monitor connection stays open
        after Exit.  The CPU resumes and VICE pushes a Resumed event.
        """
        self._send_and_recv(CMD_EXIT)

    def close(self) -> None:
        """Close the TCP connection to VICE."""
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    # ----- extended methods (beyond C64Transport protocol) -----

    def set_checkpoint(
        self,
        addr: int,
        *,
        temporary: bool = False,
        stop_when_hit: bool = True,
        enabled: bool = True,
    ) -> int:
        """Set an execution breakpoint at *addr*.

        Returns the checkpoint number assigned by VICE.
        """
        # start_addr(2) end_addr(2) stop_when_hit(1) enabled(1)
        # cpu_operation(1) temporary(1)
        body = struct.pack(
            "<HHBBBB",
            addr,               # start address
            addr,               # end address (same = single address)
            0x01 if stop_when_hit else 0x00,
            0x01 if enabled else 0x00,
            0x04,               # cpu_operation: exec
            0x01 if temporary else 0x00,
        )
        resp = self._send_and_recv(CMD_CHECKPOINT_SET, body)

        # Response: checkpoint_number(4) + ...
        if len(resp.body) < 4:
            raise TransportError("Checkpoint Set response too short")
        checkpoint_num = struct.unpack_from("<I", resp.body, 0)[0]
        return checkpoint_num

    def delete_checkpoint(self, checkpoint_num: int) -> None:
        """Delete a checkpoint by its number."""
        body = struct.pack("<I", checkpoint_num)
        self._send_and_recv(CMD_CHECKPOINT_DEL, body)

    def set_registers(self, regs: dict[str, int]) -> None:
        """Set CPU registers from a dict (e.g. {"PC": 0xC000, "A": 0x42}).

        Register names are matched case-insensitively against the cached
        register map from VICE.

        Wire format per entry:
          item_size(1)  -- 1 (reg_id) + val_bytes
          reg_id(1)     -- register ID
          value(val_bytes) -- register value, little-endian
        """
        items = []
        for name, value in regs.items():
            key = name.upper()
            if key not in self._reg_map:
                raise ValueError(
                    f"Unknown register {name!r}; known: {sorted(self._reg_map)}"
                )
            reg_id, size_bits = self._reg_map[key]
            val_bytes = max(1, (size_bits + 7) // 8)
            items.append((reg_id, val_bytes, value))

        # memspace(1) count(2) [item_size(1) reg_id(1) value(2)]...
        # VICE binary monitor always uses 2-byte values for register set,
        # regardless of the register's actual bit width.
        body = struct.pack("<BH", 0x00, len(items))
        for reg_id, val_bytes, value in items:
            item_size = 1 + 2  # reg_id(1) + value(2)
            body += struct.pack("<BB", item_size, reg_id)
            body += struct.pack("<H", value & 0xFFFF)

        self._send_and_recv(CMD_REGISTERS_SET, body)

    def wait_for_stopped(self, timeout: float | None = None) -> int:
        """Wait for an EVENT_STOPPED event from VICE.

        Returns the PC value from the stopped event.  Any other events
        received while waiting (Resumed, Checkpoint info) are discarded.

        Raises :class:`TimeoutError` if no stopped event arrives within
        *timeout* seconds.
        """
        if timeout is None:
            timeout = self.timeout

        # Discard any stale events from before this call (e.g. the
        # initial auto-stop event buffered during connect).
        self._event_queue.clear()

        deadline = time.monotonic() + timeout
        assert self._sock is not None
        with self._lock:
            while True:
                remaining_time = deadline - time.monotonic()
                if remaining_time <= 0:
                    raise TimeoutError(
                        f"No stopped event within {timeout}s"
                    )
                self._sock.settimeout(remaining_time)
                try:
                    resp = self._recv_response()
                except TimeoutError:
                    raise TimeoutError(
                        f"No stopped event within {timeout}s"
                    )
                finally:
                    self._sock.settimeout(self.timeout)

                if resp.response_type == EVENT_STOPPED:
                    return self._parse_stopped_event(resp)
                # Discard other events (Resumed, Checkpoint info, etc.)

    # ----- internal helpers -----

    def _parse_stopped_event(self, event: _Response) -> int:
        """Extract the PC value from a Stopped event body."""
        if len(event.body) >= 2:
            return struct.unpack_from("<H", event.body, 0)[0]
        return 0

    def _parse_register_response(self, data: bytes) -> dict[str, int]:
        """Parse a Registers Get response body into a name -> value dict.

        Wire format per entry:
          item_size(1)  -- byte count of the rest of this entry
          reg_id(1)     -- register ID
          value(item_size-1 bytes) -- register value, little-endian
        """
        if len(data) < 2:
            return {}

        # Build reverse map: reg_id -> name
        id_to_name: dict[int, str] = {}
        for name, (reg_id, _) in self._reg_map.items():
            id_to_name[reg_id] = name

        count = struct.unpack_from("<H", data, 0)[0]
        off = 2
        regs: dict[str, int] = {}
        for _ in range(count):
            if off >= len(data):
                break
            item_size = data[off]
            off += 1
            if off + item_size > len(data):
                break
            reg_id = data[off]
            val_bytes = item_size - 1  # remaining bytes are the value
            value = int.from_bytes(data[off + 1:off + 1 + val_bytes], "little")
            off += item_size

            name = id_to_name.get(reg_id)
            if name is not None:
                regs[name] = value

        return regs
