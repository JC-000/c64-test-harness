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
CMD_CONDITION_SET = 0x22
CMD_REGISTERS_GET = 0x31
CMD_REGISTERS_SET = 0x32
CMD_DUMP = 0x41
CMD_UNDUMP = 0x42
CMD_RESOURCE_GET = 0x51
CMD_RESOURCE_SET = 0x52
CMD_ADVANCE_INSTRUCTIONS = 0x71
CMD_KEYBOARD_FEED = 0x72
CMD_EXECUTE_UNTIL_RETURN = 0x73
CMD_BANKS_AVAILABLE = 0x82
CMD_REGS_AVAILABLE = 0x83
CMD_DISPLAY_GET = 0x84
CMD_CPUHISTORY_GET = 0x86
CMD_PALETTE_GET = 0x91
CMD_JOYPORT_SET = 0xA2
CMD_USERPORT_SET = 0xB2
CMD_EXIT = 0xAA
CMD_RESET = 0xCC

# Event / response types
EVENT_STOPPED = 0x62
EVENT_RESUMED = 0x63
RESPONSE_CHECKPOINT_INFO = 0x11

# For the binary monitor protocol, each request command's response_type field
# echoes the command opcode for most commands.  Two exceptions: the
# Checkpoint commands (Set/Delete) reply with CHECKPOINT_INFO (0x11), and
# Registers Set replies with a Registers response (0x31).  This map lets
# _wait_for_response detect request_id collisions where an unrelated
# response carries the right id but the wrong payload shape, which would
# otherwise be silently parsed as the expected reply (issue #88-style
# corruption mode).
CMD_TO_RESPONSE_TYPE: dict[int, int] = {
    CMD_MEM_GET: 0x01,
    CMD_MEM_SET: 0x02,
    CMD_CHECKPOINT_SET: 0x11,
    CMD_REGISTERS_GET: 0x31,
    CMD_REGISTERS_SET: 0x31,
    CMD_KEYBOARD_FEED: 0x72,
    CMD_BANKS_AVAILABLE: 0x82,
    CMD_REGS_AVAILABLE: 0x83,
    CMD_DISPLAY_GET: 0x84,
    CMD_CPUHISTORY_GET: 0x86,
    CMD_PALETTE_GET: 0x91,
    CMD_JOYPORT_SET: 0xA2,
    CMD_USERPORT_SET: 0xB2,
    CMD_EXIT: 0xAA,
    CMD_RESET: 0xCC,
}

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
        text_monitor_port: int = 0,
        memory_policy: "MemoryPolicy | None" = None,
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
        self._text_monitor_port = text_monitor_port

        from ..memory_policy import MemoryPolicy as _MemoryPolicy
        self._memory_policy: _MemoryPolicy = memory_policy or _MemoryPolicy.permissive()

        self._req_id = 0
        self._resume_generation = 0  # bumped by resume() et al.; tags queued events
        self._reg_map: dict[str, tuple[int, int]] = {}  # name -> (reg_id, size_bits)
        self._event_queue: deque[tuple[int, _Response]] = deque()  # (generation, resp)
        self._lock = threading.Lock()
        self._text_lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._text_sock: socket.socket | None = None

        self._connect()

    # ----- properties (C64Transport protocol) -----

    @property
    def screen_cols(self) -> int:
        return self._cols

    @property
    def screen_rows(self) -> int:
        return self._rows

    @property
    def memory_policy(self) -> "MemoryPolicy":
        """Active :class:`MemoryPolicy` for this transport.

        Set this to enforce allow-list/deny-list checks on every
        :meth:`write_memory` call.  The default is permissive (every
        write passes).
        """
        return self._memory_policy

    @memory_policy.setter
    def memory_policy(self, policy: "MemoryPolicy") -> None:
        from ..memory_policy import MemoryPolicy as _MemoryPolicy
        if not isinstance(policy, _MemoryPolicy):
            raise TypeError(
                f"memory_policy must be a MemoryPolicy, got {type(policy).__name__}"
            )
        self._memory_policy = policy

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

        if self._text_monitor_port > 0:
            self._connect_text_monitor()

    def _connect_text_monitor(self) -> None:
        """Open TCP connection to VICE text monitor for warp control.

        Retries for up to ``self.timeout`` seconds because the text monitor
        may become ready slightly after the binary monitor.  VICE's text
        monitor does not send a banner on connect — the first prompt only
        appears after a command is sent.
        """
        deadline = time.monotonic() + self.timeout
        last_err: Exception | None = None
        while time.monotonic() < deadline:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(self.timeout)
                sock.connect((self.host, self._text_monitor_port))
                self._text_sock = sock
                return
            except OSError as e:
                last_err = e
                try:
                    sock.close()
                except OSError:
                    pass
                time.sleep(0.5)
        raise ConnectionError(
            f"Cannot connect to VICE text monitor at "
            f"{self.host}:{self._text_monitor_port}: {last_err}"
        )

    def _text_recv_until_prompt(self) -> str:
        """Read from text monitor until we see the (C:$xxxx) prompt."""
        assert self._text_sock is not None
        buf = b""
        while True:
            try:
                chunk = self._text_sock.recv(4096)
            except socket.timeout as e:
                raise TimeoutError(
                    f"Timed out reading from VICE text monitor"
                ) from e
            if not chunk:
                raise ConnectionError("VICE text monitor closed connection")
            buf += chunk
            # Prompt is "(C:$xxxx) " at end of output
            text = buf.decode("ascii", errors="replace")
            if "(C:" in text and text.rstrip().endswith(")"):
                return text

    def _text_command(self, cmd: str) -> str:
        """Send a command to the text monitor and return the response."""
        assert self._text_sock is not None
        with self._text_lock:
            self._text_sock.sendall((cmd + "\n").encode("ascii"))
            return self._text_recv_until_prompt()

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
            expected_type = CMD_TO_RESPONSE_TYPE.get(cmd_type)
            return self._wait_for_response(req_id, expected_response_type=expected_type)

    def _wait_for_response(
        self,
        req_id: int,
        expected_response_type: int | None = None,
    ) -> _Response:
        """Read responses until we get the one matching *req_id*.

        Any unsolicited events (request_id == 0xFFFFFFFF) or responses
        for other request IDs are buffered in the event queue.

        If *expected_response_type* is given, raise :class:`TransportError`
        when a matched-id response carries a different ``response_type``.
        Without this, a colliding request_id would silently misroute a
        response (e.g. CHECKPOINT_INFO bytes parsed as MEM_GET data) — the
        failure shape behind issue #88.
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
                if (
                    expected_response_type is not None
                    and resp.response_type != expected_response_type
                ):
                    raise TransportError(
                        f"VICE binary monitor response_type mismatch for "
                        f"req_id={req_id}: expected {expected_response_type:#x}, "
                        f"got {resp.response_type:#x} (body={resp.body.hex()})"
                    )
                return resp
            # Unsolicited event or stale response — buffer it, tagged with
            # the current resume generation so wait_for_stopped can decide
            # whether to honour or discard it.
            self._event_queue.append((self._resume_generation, resp))

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
            if len(data) != chunk_size:
                raise TransportError(
                    f"Memory Get short read at ${current_addr:04x}: "
                    f"requested {chunk_size} bytes, got {len(data)} "
                    f"(data_len field={data_len}, body_len={len(resp.body)})"
                )
            result.extend(data)

            remaining -= len(data)
            current_addr += len(data)

        return bytes(result[:length])

    def write_memory(
        self,
        addr: int,
        data: bytes | list[int],
        *,
        override: str | None = None,
    ) -> None:
        """Write *data* bytes starting at *addr*.

        Routes through ``self.memory_policy.check_write`` before any byte
        crosses the wire — a violating write raises
        :class:`MemoryPolicyError`.  Pass ``override="<reason>"`` to
        bypass for a single call (logged at WARNING).  The default
        policy is permissive, so existing callers see no behaviour
        change.
        """
        if isinstance(data, list):
            data = bytes(data)
        if not data:
            return

        if not self._memory_policy.is_permissive():
            self._memory_policy.check_write(addr, len(data), override=override)

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

    def inject_joystick(self, port: int, value: int) -> None:
        """Inject joystick state. port=1 or 2, value is the joystick byte (bits 0-4 = up/down/left/right/fire)."""
        if port not in (1, 2):
            raise ValueError(f"port must be 1 or 2, got {port}")
        if not (0 <= value <= 0xFFFF):
            raise ValueError(f"value must fit in u16, got {value}")
        body = struct.pack("<HH", port, value)
        self._send_and_recv(CMD_JOYPORT_SET, body)

    def read_framebuffer(self, use_vic: bool = True, format: int = 0) -> dict:
        """Return raw framebuffer bytes plus geometry. Backend-specific layout — see backend docs."""
        body = struct.pack("<BB", 0x01 if use_vic else 0x00, format & 0xFF)
        resp = self._send_and_recv(CMD_DISPLAY_GET, body)
        data = resp.body
        if len(data) < 4 + 8 + 4 + 1 + 4:
            raise TransportError("Display Get response too short")
        info_len = struct.unpack_from("<I", data, 0)[0]
        debug_w = struct.unpack_from("<H", data, 4)[0]
        debug_h = struct.unpack_from("<H", data, 6)[0]
        inner_x = struct.unpack_from("<H", data, 8)[0]
        inner_y = struct.unpack_from("<H", data, 10)[0]
        inner_w = struct.unpack_from("<H", data, 12)[0]
        inner_h = struct.unpack_from("<H", data, 14)[0]
        bpp = data[16]
        buf_off = 4 + info_len
        if buf_off + 4 > len(data):
            raise TransportError("Display Get response truncated before buffer length")
        buf_len = struct.unpack_from("<I", data, buf_off)[0]
        pixels = data[buf_off + 4:buf_off + 4 + buf_len]
        return {
            "debug_rect": (0, 0, debug_w, debug_h),
            "inner_rect": (inner_x, inner_y, inner_w, inner_h),
            "bpp": bpp,
            "palette": 0,
            "bytes": pixels,
        }

    def read_palette(self, use_vic: bool = True) -> list[tuple[int, int, int]]:
        """Return the active VIC palette as RGB triples."""
        body = bytes([0x01 if use_vic else 0x00])
        resp = self._send_and_recv(CMD_PALETTE_GET, body)
        data = resp.body
        if len(data) < 2:
            raise TransportError("Palette Get response too short")
        count = struct.unpack_from("<H", data, 0)[0]
        off = 2
        result: list[tuple[int, int, int]] = []
        for _ in range(count):
            if off >= len(data):
                break
            item_size = data[off]
            off += 1
            if off + item_size > len(data):
                break
            if item_size >= 3:
                result.append((data[off], data[off + 1], data[off + 2]))
            off += item_size
        return result

    def resume(self) -> None:
        """Resume CPU execution by sending the Exit command.

        Unlike the text monitor, the binary monitor connection stays open
        after Exit.  The CPU resumes and VICE pushes a Resumed event.

        Increments ``_resume_generation`` before the send so that any
        unsolicited events (e.g. STOPPED from an immediately-hit breakpoint)
        that arrive during the CMD_EXIT ack window are tagged at the current
        generation and will be honoured by a subsequent ``wait_for_stopped``
        call rather than discarded as stale.
        """
        self._resume_generation += 1
        self._send_and_recv(CMD_EXIT)

    def close(self) -> None:
        """Close TCP connections to VICE."""
        if self._text_sock is not None:
            try:
                self._text_sock.close()
            except OSError:
                pass
            self._text_sock = None
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

        # Drain events that pre-date the most recent resume() call (generation
        # < current).  Events tagged at the current generation were buffered
        # during resume()'s CMD_EXIT ack window and must be honoured — this is
        # the fix for the resume-race described in issue #103.
        gen = self._resume_generation
        while self._event_queue and self._event_queue[0][0] < gen:
            self._event_queue.popleft()

        # Check whether an EVENT_STOPPED at the current generation is already
        # sitting in the queue (arrived during the resume() ack window).
        for i, (evt_gen, evt_resp) in enumerate(self._event_queue):
            if evt_gen >= gen and evt_resp.response_type == EVENT_STOPPED:
                # Remove it from the queue and return it immediately.
                del self._event_queue[i]
                return self._parse_stopped_event(evt_resp)

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
                if resp.request_id == EVENT_REQUEST_ID:
                    # Other unsolicited event (Resumed, Checkpoint info, …):
                    # buffer for later inspection rather than dropping.
                    self._event_queue.append((gen, resp))
                    continue
                # Non-event response with a non-stopped type while waiting
                # for STOPPED is a wire desync — raise rather than silently
                # discarding bytes that belong to some other request.
                raise TransportError(
                    f"Unexpected non-event response while waiting for "
                    f"STOPPED: response_type={resp.response_type:#x}, "
                    f"req_id={resp.request_id:#x}, "
                    f"body={resp.body.hex()}"
                )

    def resource_get(self, name: str) -> int | str:
        """Get a VICE resource value by name.

        Returns an int for integer resources, str for string resources.
        """
        name_bytes = name.encode("ascii")
        body = bytes([len(name_bytes)]) + name_bytes
        resp = self._send_and_recv(CMD_RESOURCE_GET, body)

        if len(resp.body) < 2:
            raise TransportError("Resource Get response too short")
        resource_type = resp.body[0]
        value_length = resp.body[1]
        value_bytes = resp.body[2:2 + value_length]

        if resource_type == 0x01:
            return int.from_bytes(value_bytes, "little")
        return value_bytes.decode("ascii")

    def resource_set(self, name: str, value: int | str) -> None:
        """Set a VICE resource value by name."""
        name_bytes = name.encode("ascii")
        if isinstance(value, int):
            resource_type = 0x01
            value_bytes = struct.pack("<i", value)
        else:
            resource_type = 0x00
            value_bytes = value.encode("ascii")

        body = bytes([resource_type, len(name_bytes)]) + name_bytes
        body += bytes([len(value_bytes)]) + value_bytes
        self._send_and_recv(CMD_RESOURCE_SET, body)

    def set_warp(self, enabled: bool) -> None:
        """Enable or disable VICE warp mode at runtime.

        Requires text_monitor_port to be set (VICE must be launched with
        both -binarymonitor and -remotemonitor).
        """
        if self._text_sock is None:
            raise TransportError(
                "Warp control requires text monitor connection "
                "(set text_monitor_port when constructing transport)"
            )
        cmd = "warp on" if enabled else "warp off"
        self._text_command(cmd)

    def get_warp(self) -> bool:
        """Return whether VICE warp mode is currently enabled."""
        if self._text_sock is None:
            raise TransportError(
                "Warp control requires text monitor connection "
                "(set text_monitor_port when constructing transport)"
            )
        response = self._text_command("warp")
        return "is on" in response.lower()

    # ----- code-flow / inspection -----

    def single_step(
        self, count: int = 1, step_over_subroutines: bool = False
    ) -> None:
        """Advance the CPU by *count* instructions; waits for the STOPPED event."""
        if count < 0 or count > 0xFFFF:
            raise ValueError(f"count must fit in u16, got {count}")
        body = struct.pack(
            "<BH", 0x01 if step_over_subroutines else 0x00, count
        )
        self._resume_generation += 1
        self._send_and_recv(CMD_ADVANCE_INSTRUCTIONS, body)
        self.wait_for_stopped()

    def step_out(self) -> None:
        """Resume execution until the next RTS; waits for STOPPED."""
        self._resume_generation += 1
        self._send_and_recv(CMD_EXECUTE_UNTIL_RETURN, b"")
        self.wait_for_stopped()

    def set_condition(self, checkpoint_num: int, expression: str) -> None:
        """Attach a condition expression to an existing checkpoint."""
        if not (0 <= checkpoint_num <= 0xFFFFFFFF):
            raise ValueError(
                f"checkpoint_num must fit in u32, got {checkpoint_num}"
            )
        expr_bytes = expression.encode("ascii")
        if len(expr_bytes) > 0xFF:
            raise ValueError(
                f"expression too long ({len(expr_bytes)} bytes, max 255)"
            )
        body = struct.pack("<IB", checkpoint_num, len(expr_bytes)) + expr_bytes
        self._send_and_recv(CMD_CONDITION_SET, body)

    def cpu_history(
        self, count: int = 16, memspace: int = 0
    ) -> list[dict]:
        """Return up to *count* recent CPU history records (VICE 3.10+)."""
        if count < 0 or count > 0xFFFFFFFF:
            raise ValueError(f"count must fit in u32, got {count}")
        body = struct.pack("<BI", memspace & 0xFF, count)
        resp = self._send_and_recv(CMD_CPUHISTORY_GET, body)
        data = resp.body
        if len(data) < 4:
            return []
        record_count = struct.unpack_from("<I", data, 0)[0]
        off = 4
        records: list[dict] = []
        for _ in range(record_count):
            if off >= len(data):
                break
            item_size = data[off]
            off += 1
            if off + item_size > len(data):
                break
            entry = data[off:off + item_size]
            off += item_size
            rec = self._parse_cpu_history_entry(entry)
            if rec is not None:
                records.append(rec)
        return records

    def banks_available(self) -> list[tuple[int, str]]:
        """Return the list of (bank_id, name) pairs available in main memspace."""
        resp = self._send_and_recv(CMD_BANKS_AVAILABLE, b"")
        data = resp.body
        if len(data) < 2:
            return []
        count = struct.unpack_from("<H", data, 0)[0]
        off = 2
        result: list[tuple[int, str]] = []
        for _ in range(count):
            if off >= len(data):
                break
            item_size = data[off]
            off += 1
            if off + item_size > len(data):
                break
            if item_size < 3:
                off += item_size
                continue
            bank_id = struct.unpack_from("<H", data, off)[0]
            name_len = data[off + 2]
            name = data[off + 3:off + 3 + name_len].decode(
                "ascii", errors="replace"
            )
            off += item_size
            result.append((bank_id, name))
        return result

    def registers_available(self, memspace: int = 0) -> list[dict]:
        """Return register descriptors: [{id, size_bits, name}, ...]."""
        body = bytes([memspace & 0xFF])
        resp = self._send_and_recv(CMD_REGS_AVAILABLE, body)
        data = resp.body
        if len(data) < 2:
            return []
        count = struct.unpack_from("<H", data, 0)[0]
        off = 2
        result: list[dict] = []
        for _ in range(count):
            if off >= len(data):
                break
            item_size = data[off]
            off += 1
            if off + item_size > len(data):
                break
            if item_size < 3:
                off += item_size
                continue
            reg_id = data[off]
            size_bits = data[off + 1]
            name_len = data[off + 2]
            name = data[off + 3:off + 3 + name_len].decode(
                "ascii", errors="replace"
            )
            off += item_size
            result.append({"id": reg_id, "size_bits": size_bits, "name": name})
        return result

    # ----- I/O injection -----

    def inject_userport(self, value: int) -> None:
        """Drive the userport with *value* (u16)."""
        if not (0 <= value <= 0xFFFF):
            raise ValueError(f"value must fit in u16, got {value}")
        body = struct.pack("<H", value)
        self._send_and_recv(CMD_USERPORT_SET, body)

    # ----- snapshots / state -----

    def dump_snapshot(
        self,
        filename: str,
        save_roms: bool = False,
        save_disks: bool = True,
    ) -> None:
        """Write a VICE snapshot to *filename* on the host filesystem."""
        name_bytes = filename.encode("utf-8")
        if len(name_bytes) > 0xFF:
            raise ValueError(
                f"filename too long ({len(name_bytes)} bytes, max 255)"
            )
        body = struct.pack(
            "<BBB",
            0x01 if save_roms else 0x00,
            0x01 if save_disks else 0x00,
            len(name_bytes),
        ) + name_bytes
        self._send_and_recv(CMD_DUMP, body)

    def undump_snapshot(self, filename: str) -> int:
        """Restore a VICE snapshot from *filename* and return the new PC."""
        name_bytes = filename.encode("utf-8")
        if len(name_bytes) > 0xFF:
            raise ValueError(
                f"filename too long ({len(name_bytes)} bytes, max 255)"
            )
        body = struct.pack("<B", len(name_bytes)) + name_bytes
        resp = self._send_and_recv(CMD_UNDUMP, body)
        if len(resp.body) < 2:
            raise TransportError("Undump response too short")
        return struct.unpack_from("<H", resp.body, 0)[0]

    # ----- reset -----

    def reset(self, reset_type: int = 0) -> None:
        """Reset the machine. type 0=soft, 1=hard, 8..11=drive 0..3."""
        if reset_type not in (0, 1, 8, 9, 10, 11):
            raise ValueError(
                f"reset_type must be 0,1,8,9,10,11; got {reset_type}"
            )
        self._send_and_recv(CMD_RESET, bytes([reset_type]))

    # ----- text-monitor extras -----

    def detach_drive(self, device: int) -> None:
        """Detach the image attached to *device* (1=tape, 8..11=drives)."""
        if device not in (1, 8, 9, 10, 11):
            raise ValueError(
                f"device must be 1, 8, 9, 10 or 11; got {device}"
            )
        if self._text_sock is None:
            raise TransportError(
                "detach_drive requires text monitor connection "
                "(set text_monitor_port when constructing transport)"
            )
        self._text_command(f"detach {device}")

    def attach_drive(
        self, device: int, image_path: str, read_only: bool = False
    ) -> None:
        """Attach *image_path* to *device* without auto-running it."""
        if device not in (1, 8, 9, 10, 11):
            raise ValueError(
                f"device must be 1, 8, 9, 10 or 11; got {device}"
            )
        if self._text_sock is None:
            raise TransportError(
                "attach_drive requires text monitor connection "
                "(set text_monitor_port when constructing transport)"
            )
        self._text_command(f'attach "{image_path}" {device}')

    def screenshot_to_file(self, filename: str, format: str = "png") -> None:
        """Have VICE write a screenshot to *filename* in the given format."""
        if self._text_sock is None:
            raise TransportError(
                "screenshot_to_file requires text monitor connection "
                "(set text_monitor_port when constructing transport)"
            )
        self._text_command(f'screenshot "{filename}" {format}')

    def profile_start(self, mode: str = "on") -> None:
        """Start the VICE profiler. mode is one of {on, ...}."""
        if mode not in ("on", "off", "flat", "graph", "func", "disass", "context", "clear"):
            raise ValueError(
                f"mode must be one of on/off/flat/graph/func/disass/"
                f"context/clear; got {mode!r}"
            )
        if self._text_sock is None:
            raise TransportError(
                "profile_start requires text monitor connection "
                "(set text_monitor_port when constructing transport)"
            )
        self._text_command(f"profile {mode}")

    def profile_stop(self) -> None:
        """Stop the VICE profiler."""
        if self._text_sock is None:
            raise TransportError(
                "profile_stop requires text monitor connection "
                "(set text_monitor_port when constructing transport)"
            )
        self._text_command("profile off")

    def profile_dump(self, mode: str = "flat") -> str:
        """Return the captured profiler output for *mode*."""
        if mode not in ("flat", "graph", "func", "disass", "context", "clear"):
            raise ValueError(
                f"mode must be one of flat/graph/func/disass/context/"
                f"clear; got {mode!r}"
            )
        if self._text_sock is None:
            raise TransportError(
                "profile_dump requires text monitor connection "
                "(set text_monitor_port when constructing transport)"
            )
        return self._text_command(f"profile {mode}")

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

    def _parse_cpu_history_entry(self, entry: bytes) -> dict | None:
        """Decode one CPU history entry (defensive about layout drift).

        VICE's per-entry layout is:
          register_count(2 LE) +
          register_count * (item_size(1) + reg_id(1) + value(...)) +
          cycle(8 LE) + instr_len(1) + opcode + up to 3 operand bytes
        Older builds and non-mainline forks have shipped slightly different
        records, so anything that cannot be parsed cleanly returns None.
        """
        if len(entry) < 2:
            return None
        regs: dict[str, int] = {}
        reg_count = struct.unpack_from("<H", entry, 0)[0]
        off = 2
        id_to_name: dict[int, str] = {
            rid: name for name, (rid, _) in self._reg_map.items()
        }
        for _ in range(reg_count):
            if off >= len(entry):
                return None
            item_size = entry[off]
            off += 1
            if off + item_size > len(entry):
                return None
            if item_size >= 1:
                rid = entry[off]
                val = int.from_bytes(
                    entry[off + 1:off + item_size], "little"
                )
                name = id_to_name.get(rid)
                if name is not None:
                    regs[name] = val
            off += item_size
        cycle = 0
        if off + 8 <= len(entry):
            cycle = struct.unpack_from("<Q", entry, off)[0]
            off += 8
        instr_len = 0
        if off < len(entry):
            instr_len = entry[off]
            off += 1
        instr_bytes = entry[off:off + min(instr_len, 4)]
        return {
            "cycle": cycle,
            "pc": regs.get("PC", 0),
            "a": regs.get("A", 0),
            "x": regs.get("X", 0),
            "y": regs.get("Y", 0),
            "sp": regs.get("SP", 0),
            "sr": regs.get("FL", regs.get("FLAGS", regs.get("SR", 0))),
            "instruction": bytes(instr_bytes),
            "registers": regs,
        }
