#!/usr/bin/env python3
"""Prototype: transport-level monitor logging for c64-test-harness.

# =========================================================================
# EVALUATION OF APPROACHES
# =========================================================================
#
# 1. VICE-side logging (`log` / `logname` commands)
#    -----------------------------------------------
#    Pros:
#    - Captures VICE's perspective, could reveal internal parser errors
#    - Zero code changes in our harness (just send two extra commands)
#    - Might capture VICE-initiated messages (breakpoint hits, etc.)
#
#    Cons:
#    - Requires extra TCP command per connection (we use per-command
#      connections, so that means `logname` + `log on` EVERY time we
#      connect -- doubling our overhead, or hoping state persists across
#      connections, which is unreliable)
#    - Log format is not machine-parseable (no timestamps, no framing,
#      no distinction between sent and received)
#    - Session-scope uncertainty: VICE may reset `log on` state when the
#      TCP connection closes, meaning we'd only ever log the `log on`
#      command itself
#    - Cannot control output format or rotation
#    - `-moncommands` only fires once at startup, before TCP connections
#
# 2. Transport-level logging (our side)
#    -----------------------------------
#    Pros:
#    - Full control over format, timestamps, and log destination
#    - Captures exact bytes on the wire (both directions)
#    - Works identically for text and binary protocols
#    - Timing data (command latency) comes free
#    - Can be enabled/disabled per-session without restarting VICE
#    - Zero VICE-side changes or dependencies
#    - Can correlate with Python-side call stacks if needed
#
#    Cons:
#    - Does not see VICE's internal state (e.g., why a command failed)
#    - Must be maintained as part of our codebase
#
# 3. Recommendation
#    ---------------
#    Transport-level logging is the clear winner for the use case of
#    "agents blaming VICE for their own bugs."
#
#    When an agent says "VICE returned wrong data," the first question is
#    always "what did you actually send?"  Transport logging answers this
#    directly with exact bytes and timestamps.  VICE-side logging would
#    only show the same data in a less useful format.
#
#    VICE-side logging is a nice complement for rare edge cases (e.g.,
#    suspected VICE bugs), but should NOT be the primary mechanism.
#
#    Recommended combo:
#    - Always: transport-level logging (Python side), enabled via
#      HarnessConfig or environment variable
#    - Optional: VICE -moncommands with `logname`/`log on` for deep
#      debugging sessions (manual, not automated)
#
# 4. Where it fits architecturally
#    ------------------------------
#    Option A: Modify ViceTransport._command() directly
#      + Simple, minimal code
#      - Mixes concerns (transport + logging)
#      - Hard to reuse for BinaryViceTransport
#
#    Option B: Wrapper/decorator class (recommended)
#      + Clean separation of concerns
#      + Works with any transport (text or binary) via composition
#      + Can be toggled on/off without changing transport code
#      + Testable independently
#      - One extra class to maintain
#
#    Option C: Socket-level monkey-patching
#      + Zero changes to transport classes
#      - Fragile, hard to debug, implicit
#
#    Option D: Mixin class
#      + Reusable across transports
#      - Python MRO complexity, diamond inheritance risk
#
#    RECOMMENDATION: Option B (wrapper class).  Compose a LoggingTransport
#    that wraps any C64Transport implementation.  The wrapper intercepts
#    calls, logs them, delegates to the inner transport, logs the response,
#    and returns it.  This works for both text and future binary transports.
#
#    For the lower-level socket logging (seeing raw bytes), instrument
#    _connect() to return a LoggingSocket wrapper.  This is demonstrated
#    below.
#
# =========================================================================

Usage:
    python3 scripts/prototype_monitor_logger.py

If VICE is running on port 6510, it will connect and log some commands.
Otherwise, it demonstrates the logging infrastructure with simulated I/O.
"""

from __future__ import annotations

import io
import os
import socket
import sys
import time
from datetime import datetime, timezone
from typing import Protocol

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


# =========================================================================
# Part 1: LoggingSocket -- wraps a socket to intercept all I/O
# =========================================================================

class LoggingSocket:
    """Wraps a socket.socket to log all send/recv calls.

    This is the core primitive: by wrapping the socket itself, we capture
    the exact bytes on the wire regardless of what the transport layer does.
    """

    def __init__(self, sock: socket.socket, logger: MonitorLogger, label: str = "") -> None:
        self._sock = sock
        self._logger = logger
        self._label = label

    # -- Delegate all socket methods, intercepting send/recv --

    def sendall(self, data: bytes) -> None:
        self._logger.log_send(data, self._label)
        return self._sock.sendall(data)

    def send(self, data: bytes, flags: int = 0) -> int:
        self._logger.log_send(data, self._label)
        return self._sock.send(data, flags)

    def recv(self, bufsize: int) -> bytes:
        data = self._sock.recv(bufsize)
        if data:
            self._logger.log_recv(data, self._label)
        return data

    # -- Pass through everything else --

    def settimeout(self, timeout: float | None) -> None:
        self._sock.settimeout(timeout)

    def setblocking(self, flag: bool) -> None:
        self._sock.setblocking(flag)

    def connect(self, address: tuple) -> None:
        self._logger.log_event("CONNECT", f"{address[0]}:{address[1]}")
        self._sock.connect(address)

    def close(self) -> None:
        self._logger.log_event("DISCONNECT", self._label)
        self._sock.close()

    def fileno(self) -> int:
        return self._sock.fileno()

    # Allow use in contexts expecting a real socket (best-effort)
    def __getattr__(self, name: str):
        return getattr(self._sock, name)


# =========================================================================
# Part 2: MonitorLogger -- formats and writes log entries
# =========================================================================

class MonitorLogger:
    """Formats and writes monitor communication log entries.

    Supports both text and binary protocol data.
    """

    # Known binary monitor command types (VICE binary protocol)
    BINARY_CMD_TYPES = {
        0x01: "MEM_GET",
        0x02: "MEM_SET",
        0x11: "CHECKPOINT_GET",
        0x12: "CHECKPOINT_SET",
        0x13: "CHECKPOINT_DELETE",
        0x14: "CHECKPOINT_LIST",
        0x21: "REGISTERS_GET",
        0x22: "REGISTERS_SET",
        0x31: "DUMP",
        0x41: "ADVANCE_INSTRUCTIONS",
        0x71: "PING",
        0x81: "EXIT",
        0xAA: "QUIT",
        0xBB: "RESET",
    }

    def __init__(
        self,
        log_file: str = "/tmp/c64_monitor.log",
        binary_mode: bool = False,
        also_print: bool = False,
    ) -> None:
        self._log_file = log_file
        self._binary_mode = binary_mode
        self._also_print = also_print
        self._file: io.TextIOWrapper | None = None
        self._cmd_start: float | None = None

    def open(self) -> None:
        self._file = open(self._log_file, "a", encoding="utf-8")
        self.log_event("SESSION", f"opened log {self._log_file}")

    def close(self) -> None:
        if self._file:
            self.log_event("SESSION", "closed")
            self._file.close()
            self._file = None

    def __enter__(self) -> MonitorLogger:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _timestamp(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]

    def _write(self, line: str) -> None:
        if self._file:
            self._file.write(line + "\n")
            self._file.flush()
        if self._also_print:
            print(line)

    def log_event(self, event: str, detail: str = "") -> None:
        ts = self._timestamp()
        self._write(f"[{ts}] {event} {detail}".rstrip())

    def log_send(self, data: bytes, label: str = "") -> None:
        ts = self._timestamp()
        self._cmd_start = time.monotonic()
        if self._binary_mode:
            cmd_type = self._decode_binary_cmd(data)
            hex_str = data.hex(" ")
            self._write(
                f"[{ts}] SEND ({len(data)} bytes) [{cmd_type}]: {hex_str}"
            )
        else:
            text = data.decode("latin-1", errors="replace").rstrip("\n")
            self._write(f"[{ts}] SEND ({len(data)} bytes): {text}")

    def log_recv(self, data: bytes, label: str = "") -> None:
        ts = self._timestamp()
        elapsed = ""
        if self._cmd_start is not None:
            dt = (time.monotonic() - self._cmd_start) * 1000
            elapsed = f" [{dt:.1f}ms]"
            self._cmd_start = None

        if self._binary_mode:
            resp_type = self._decode_binary_response(data)
            hex_str = data.hex(" ")
            # Truncate very long hex dumps
            if len(hex_str) > 200:
                hex_str = hex_str[:200] + "..."
            self._write(
                f"[{ts}] RECV ({len(data)} bytes) [{resp_type}]{elapsed}: {hex_str}"
            )
        else:
            text = data.decode("latin-1", errors="replace").rstrip()
            # For multi-line responses, indent continuation lines
            lines = text.split("\n")
            first = lines[0]
            self._write(f"[{ts}] RECV ({len(data)} bytes){elapsed}: {first}")
            for line in lines[1:]:
                self._write(f"           | {line}")

    def _decode_binary_cmd(self, data: bytes) -> str:
        if len(data) >= 4 and data[0] == 0x02:  # STX header byte
            cmd_byte = data[3] if len(data) > 3 else 0
            return self.BINARY_CMD_TYPES.get(cmd_byte, f"0x{cmd_byte:02x}")
        return "UNKNOWN"

    def _decode_binary_response(self, data: bytes) -> str:
        if len(data) >= 4 and data[0] == 0x02:
            cmd_byte = data[3] if len(data) > 3 else 0
            return self.BINARY_CMD_TYPES.get(cmd_byte, f"0x{cmd_byte:02x}")
        return "TEXT"


# =========================================================================
# Part 3: LoggingViceTransport -- wraps ViceTransport with logging
# =========================================================================

class LoggingViceTransport:
    """Wraps a ViceTransport to add monitor logging.

    This demonstrates the recommended architectural approach: composition.
    The wrapper delegates all C64Transport methods to the inner transport,
    adding logging around each call.

    For raw socket-level logging, it overrides _connect() to wrap the socket.
    For higher-level command logging, it wraps _command().

    In production, this could also implement the C64Transport protocol
    directly so it's a drop-in replacement.
    """

    def __init__(
        self,
        inner,  # ViceTransport instance
        logger: MonitorLogger,
    ) -> None:
        self._inner = inner
        self._logger = logger

    # --- Socket-level wrapping (low-level approach) ---

    def _connect(self) -> LoggingSocket:
        """Override _connect to wrap the returned socket with logging."""
        sock = self._inner._connect()
        return LoggingSocket(sock, self._logger, label=f"port={self._inner.port}")

    # --- Command-level wrapping (high-level approach) ---

    def raw_command(self, cmd: str) -> str:
        """Log the command and response at the command level."""
        self._logger.log_send(cmd.encode("latin-1") + b"\n")
        t0 = time.monotonic()
        try:
            resp = self._inner.raw_command(cmd)
            dt = (time.monotonic() - t0) * 1000
            self._logger.log_recv(
                resp.encode("latin-1"),
                label=f"[{dt:.1f}ms]",
            )
            return resp
        except Exception as e:
            self._logger.log_event("ERROR", f"{type(e).__name__}: {e}")
            raise

    def read_memory(self, addr: int, length: int) -> bytes:
        self._logger.log_event("CMD", f"read_memory(0x{addr:04x}, {length})")
        result = self._inner.read_memory(addr, length)
        self._logger.log_event("RESULT", f"read_memory -> {len(result)} bytes")
        return result

    def write_memory(self, addr: int, data: bytes | list[int]) -> None:
        self._logger.log_event("CMD", f"write_memory(0x{addr:04x}, {len(data)} bytes)")
        self._inner.write_memory(addr, data)

    def read_registers(self) -> dict[str, int]:
        self._logger.log_event("CMD", "read_registers()")
        regs = self._inner.read_registers()
        self._logger.log_event("RESULT", f"registers: {regs}")
        return regs

    def resume(self) -> None:
        self._logger.log_event("CMD", "resume()")
        self._inner.resume()

    # Pass through properties
    @property
    def screen_cols(self) -> int:
        return self._inner.screen_cols

    @property
    def screen_rows(self) -> int:
        return self._inner.screen_rows

    def close(self) -> None:
        self._inner.close()


# =========================================================================
# Part 4: Demo / test
# =========================================================================

def demo_with_live_vice(port: int = 6510) -> None:
    """If VICE is running, demonstrate logging against it."""
    from c64_test_harness.backends.vice import ViceTransport

    log_path = "/tmp/c64_monitor.log"
    print(f"Logging to: {log_path}")

    with MonitorLogger(log_file=log_path, also_print=True) as logger:
        inner = ViceTransport(port=port, timeout=5.0)
        logged = LoggingViceTransport(inner, logger)

        print("\n--- Reading registers ---")
        try:
            regs = logged.read_registers()
            print(f"  Registers: {regs}")
        except Exception as e:
            print(f"  Error: {e}")

        print("\n--- Reading memory $0400-$040F ---")
        try:
            data = logged.read_memory(0x0400, 16)
            print(f"  Data: {data.hex(' ')}")
        except Exception as e:
            print(f"  Error: {e}")

        print("\n--- Writing memory $C000 ---")
        try:
            logged.write_memory(0xC000, [0xEA, 0xEA, 0x60])
        except Exception as e:
            print(f"  Error: {e}")

        print(f"\nFull log written to: {log_path}")


def demo_simulated() -> None:
    """Demonstrate logging format without a live VICE instance."""
    log_path = "/tmp/c64_monitor_demo.log"
    print(f"Simulated demo -- logging to: {log_path}")

    with MonitorLogger(log_file=log_path, also_print=True) as logger:
        # Simulate a text protocol session
        logger.log_event("CONNECT", "127.0.0.1:6510")
        logger.log_send(b"r\n")
        time.sleep(0.05)  # simulate network latency
        logger.log_recv(
            b"  ADDR A  X  Y  SP 00 01 NV-BDIZC LIN CYC  STOPWATCH\n"
            b".;fce2 00 00 00 fd 2f 37 00100100 000 000    5830338\n"
            b"(C:$fce2) \n"
        )

        logger.log_event("DISCONNECT", "")
        logger.log_event("CONNECT", "127.0.0.1:6510")

        logger.log_send(b"m 0400 040f\n")
        time.sleep(0.05)
        logger.log_recv(
            b">C:0400  20 20 20 20 20 20 20 20  20 20 20 20 20 20 20 20   ................\n"
            b"(C:$0400) \n"
        )

        logger.log_event("DISCONNECT", "")

        # Simulate a binary protocol session
        print("\n--- Binary protocol demo ---")
        bin_logger = MonitorLogger(log_file=log_path, binary_mode=True, also_print=True)
        bin_logger.open()

        # Simulated binary MEM_GET request (STX + length + reqid + cmd + body)
        bin_logger.log_send(bytes([0x02, 0x08, 0x00, 0x01, 0x01, 0x00, 0x04, 0x0F, 0x04, 0x00, 0x00]))
        time.sleep(0.05)
        # Simulated binary response
        bin_logger.log_recv(bytes([0x02, 0x14, 0x00, 0x01, 0x00, 0x00]) + bytes(16))

        bin_logger.close()

    print(f"\nFull log written to: {log_path}")


def _check_vice_listening(port: int) -> bool:
    """Quick check if anything is listening on the given port."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.0)
        s.connect(("127.0.0.1", port))
        s.close()
        return True
    except OSError:
        return False


# =========================================================================
# Part 5: Integration sketch (how this would look in vice.py)
# =========================================================================

INTEGRATION_SKETCH = '''
# =========================================================================
# INTEGRATION SKETCH -- how monitor logging would fit into ViceTransport
# =========================================================================
#
# This shows the minimal changes to ViceTransport._command() and
# _connect() to add optional logging.  The key design principle is
# that logging is OFF by default and adds zero overhead when disabled.
#
# Changes to ViceTransport.__init__():
#
#     def __init__(
#         self,
#         host: str = "127.0.0.1",
#         port: int = 6510,
#         timeout: float = 5.0,
#         ...
#         monitor_log: str | None = None,  # NEW: path to log file, or None
#     ) -> None:
#         ...existing init...
#         self._monitor_log: MonitorLogger | None = None
#         if monitor_log:
#             self._monitor_log = MonitorLogger(log_file=monitor_log)
#             self._monitor_log.open()
#
# Changes to _connect():
#
#     def _connect(self) -> socket.socket:
#         """Create a fresh TCP connection for a single command."""
#         try:
#             s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
#             s.settimeout(self.timeout)
#             s.connect((self.host, self.port))
#             if self._monitor_log:
#                 self._monitor_log.log_event("CONNECT", f"{self.host}:{self.port}")
#             self._drain(s, 0.3)
#             return s
#         except OSError as e:
#             if self._monitor_log:
#                 self._monitor_log.log_event("CONNECT_FAIL", str(e))
#             raise ConnectionError(...) from e
#
# Changes to _command():
#
#     def _command(self, cmd: str) -> str:
#         """Send a monitor command and return the text response."""
#         s = self._connect()
#         try:
#             payload = (cmd + "\\n").encode()
#             if self._monitor_log:
#                 self._monitor_log.log_send(payload)
#             s.sendall(payload)
#             time.sleep(0.1)
#             resp = self._drain(s, 0.5).strip()
#             if self._monitor_log:
#                 self._monitor_log.log_recv(resp.encode("latin-1"))
#             return resp
#         finally:
#             if self._monitor_log:
#                 self._monitor_log.log_event("DISCONNECT", "")
#             s.close()
#
# Changes to close():
#
#     def close(self) -> None:
#         if self._monitor_log:
#             self._monitor_log.close()
#             self._monitor_log = None
#
# ---------------------------------------------------------------------------
# ALTERNATIVE: The wrapper approach (recommended over direct integration)
# ---------------------------------------------------------------------------
#
# Instead of modifying ViceTransport, use the LoggingViceTransport wrapper
# shown above.  In HarnessConfig or test setup:
#
#     transport = ViceTransport(port=6510)
#     if config.monitor_log:
#         logger = MonitorLogger(log_file=config.monitor_log)
#         logger.open()
#         transport = LoggingViceTransport(transport, logger)
#
# This keeps ViceTransport clean and works for any future transport
# (BinaryViceTransport, HardwareTransport, etc.).
#
# For HarnessConfig integration, add:
#     monitor_log: str = ""  # empty = disabled
#
# And in from_env():
#     monitor_log = os.environ.get("C64_MONITOR_LOG", "")
#
# This lets agents enable logging with:
#     export C64_MONITOR_LOG=/tmp/c64_monitor.log
# =========================================================================
'''


def main() -> None:
    print("=" * 70)
    print("c64-test-harness Monitor Logging Prototype")
    print("=" * 70)

    # Try live VICE first
    if _check_vice_listening(6510):
        print("\nVICE detected on port 6510 -- running live demo")
        demo_with_live_vice(6510)
    else:
        print("\nNo VICE on port 6510 -- running simulated demo")
        demo_simulated()

    print("\n" + "=" * 70)
    print("INTEGRATION SKETCH")
    print("=" * 70)
    print(INTEGRATION_SKETCH)


if __name__ == "__main__":
    main()
