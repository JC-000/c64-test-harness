"""ViceTransport — C64Transport implementation via VICE's TCP text monitor.

Uses per-command TCP connections (same model as vicemon.py — VICE doesn't
support persistent connections well).

Key fixes over vicemon.py:

1. ``read_memory()`` parses ``>C:XXXX`` anywhere in line (not just at start),
   fixing the prepended-prompt bug.
2. ``inject_keys()`` writes batches of up to 10 to the keyboard buffer in a
   single command, instead of 2 TCP connections per character.
"""

from __future__ import annotations

import re
import socket
import time

from ..transport import ConnectionError, TimeoutError, TransportError


class ViceTransport:
    """C64Transport backed by VICE's remote text monitor."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 6510,
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

    @property
    def screen_cols(self) -> int:
        return self._cols

    @property
    def screen_rows(self) -> int:
        return self._rows

    # ----- low-level TCP -----

    def _connect(self) -> socket.socket:
        """Create a fresh TCP connection for a single command."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(self.timeout)
            s.connect((self.host, self.port))
            self._drain(s, 0.3)  # consume initial prompt
            return s
        except OSError as e:
            raise ConnectionError(f"Cannot connect to VICE at {self.host}:{self.port}: {e}") from e

    def _drain(self, sock: socket.socket, wait: float = 0.3) -> str:
        """Read all available data from socket."""
        data = b""
        sock.setblocking(False)
        deadline = time.time() + wait
        while time.time() < deadline:
            try:
                chunk = sock.recv(4096)
                if chunk:
                    data += chunk
                    deadline = time.time() + 0.1
                else:
                    break
            except BlockingIOError:
                time.sleep(0.02)
        sock.setblocking(True)
        sock.settimeout(self.timeout)
        return data.decode("latin-1", errors="replace")

    def _command(self, cmd: str) -> str:
        """Send a monitor command and return the text response."""
        s = self._connect()
        try:
            s.sendall((cmd + "\n").encode())
            time.sleep(0.1)
            return self._drain(s, 0.5).strip()
        finally:
            s.close()

    # ----- C64Transport interface -----

    def read_memory(self, addr: int, length: int) -> bytes:
        """Read memory using the fixed parser (bug fix #1).

        Searches for ``>C:XXXX`` *anywhere* in each response line, not
        just at the start, handling the prepended-prompt case.
        """
        end = addr + length - 1
        resp = self._command(f"m {addr:04x} {end:04x}")
        return bytes(_parse_mem_response(resp, length))

    def write_memory(self, addr: int, data: bytes | list[int]) -> None:
        hex_str = " ".join(f"{b:02x}" for b in data)
        self._command(f">C:{addr:04x} {hex_str}")

    def read_screen_codes(self) -> list[int]:
        total = self._cols * self._rows
        data = self.read_memory(self.screen_base, total)
        return list(data)

    def inject_keys(self, petscii_codes: list[int]) -> None:
        """Write PETSCII codes to keyboard buffer in batches (bug fix #4)."""
        for i in range(0, len(petscii_codes), self.keybuf_max):
            batch = petscii_codes[i : i + self.keybuf_max]
            self.write_memory(self.keybuf_addr, batch)
            self.write_memory(self.keybuf_count_addr, [len(batch)])

    def read_registers(self) -> dict[str, int]:
        resp = self._command("r")
        regs: dict[str, int] = {}
        for line in resp.split("\n"):
            m = re.match(
                r"\.;([0-9a-fA-F]{4})\s+([0-9a-fA-F]{2})\s+([0-9a-fA-F]{2})"
                r"\s+([0-9a-fA-F]{2})\s+([0-9a-fA-F]{2})",
                line,
            )
            if m:
                regs["PC"] = int(m.group(1), 16)
                regs["A"] = int(m.group(2), 16)
                regs["X"] = int(m.group(3), 16)
                regs["Y"] = int(m.group(4), 16)
                regs["SP"] = int(m.group(5), 16)
        return regs

    def resume(self) -> None:
        s = self._connect()
        try:
            s.sendall(b"x\n")
            time.sleep(0.1)
        finally:
            s.close()

    def raw_command(self, cmd: str) -> str:
        return self._command(cmd)

    def close(self) -> None:
        pass  # per-command connections, nothing to close


# ---------------------------------------------------------------------------
# Response parsing (extracted for unit testing without a live VICE)
# ---------------------------------------------------------------------------

#: Regex matching ">C:XXXX" anywhere in a line (bug fix #1)
_MEM_LINE_RE = re.compile(r">C:[0-9a-fA-F]{4}\s{2}(.*)")


def _parse_mem_response(resp: str, max_bytes: int) -> list[int]:
    """Parse a VICE ``m`` command response into a list of byte values.

    Handles the prepended-prompt bug: ``(C:$XXXX) >C:40ab  05 18 ...``
    The regex finds ``>C:`` *anywhere* in the line.
    """
    result: list[int] = []
    for line in resp.split("\n"):
        m = _MEM_LINE_RE.search(line)
        if not m:
            continue
        hex_section = m.group(1)
        # Groups of hex bytes separated by double-space
        parts = hex_section.split("  ")
        for part in parts:
            part = part.strip()
            if not part:
                continue
            tokens = part.split()
            if tokens and all(
                len(t) == 2 and all(c in "0123456789abcdefABCDEF" for c in t)
                for t in tokens
            ):
                for t in tokens:
                    result.append(int(t, 16))
    return result[:max_bytes]
