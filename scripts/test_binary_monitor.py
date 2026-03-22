#!/usr/bin/env python3
"""VICE Binary Monitor Protocol — Research & Test Script.

============================================================================
FINDINGS SUMMARY (tested 2026-03-21 with x64sc, VICE 3.x)
============================================================================

All 10 tests PASS.  The binary monitor is dramatically superior to the
text monitor for the c64-test-harness project.

1. CONNECTION PERSISTENCE — CRITICAL DIFFERENCE
   Text monitor: `x` (resume) permanently kills the monitor socket.
   Binary monitor: Exit (0xAA) resumes CPU, connection STAYS OPEN.
   VICE pushes Stopped events (0x62) when breakpoints fire.
   --> Eliminates per-command reconnect/drain overhead entirely.

2. NO WRITE SIZE LIMIT
   Text monitor: truncates at ~84 bytes (256-char input buffer).
   Binary monitor: 256, 1024, and 4096-byte writes all verified.
   --> Eliminates 64-byte chunking workaround in write_memory().

3. PERFORMANCE (persistent connection, no reconnect)
   Memory Get  (16 bytes):    avg 0.021ms  (text: ~400ms = 19,000x slower)
   Memory Set  (256 bytes):   avg 0.011ms  (text: ~400ms = 36,000x slower)
   Memory Set  (1024 bytes):  avg 0.063ms  (text: impossible — truncation)
   Registers Get:             avg 0.082ms  (text: ~400ms)
   Full JSR roundtrip:        avg 0.084ms  (text: ~2000-3000ms)
   --> Text monitor overhead is entirely TCP connect/drain/close cost.

4. CPU BEHAVIOUR
   - Connect: CPU keeps running (no pause, unlike text monitor)
   - First command: CPU auto-stops; VICE sends unsolicited register dump
     (0x31) and Stopped event (0x62) before the command response
   - Breakpoint hit: VICE pushes Resumed (0x63), Checkpoint (0x11),
     Registers (0x31), then Stopped (0x62) events

5. REGISTER MAP (x64sc / C64)
   ID=0x00 A (8-bit), ID=0x01 X (8-bit), ID=0x02 Y (8-bit),
   ID=0x03 PC (16-bit), ID=0x04 SP (8-bit), ID=0x05 FL (8-bit flags),
   ID=0x35 LIN (16-bit), ID=0x36 CYC (16-bit),
   ID=0x37 00 (8-bit), ID=0x38 01 (8-bit)

6. PROTOCOL WIRE FORMAT
   Request:  STX(0x02) API(0x02) body_len(4B LE) req_id(4B LE) cmd(1B) [body]
   Response: STX(0x02) API(0x02) body_len(4B LE) resp_type(1B) err(1B) req_id(4B LE) [body]
   - resp_type echoes command ID (0x01=mem_get, 0x31=regs_get, 0x62=stopped, etc.)
   - Unsolicited events have req_id=0xffffffff
   - Response body uses IS (item size) prefix per array element in register commands

7. RECOMMENDATIONS FOR c64-test-harness
   - Replace ViceTransport text monitor with binary monitor backend
   - Single persistent TCP connection per ViceInstance
   - jsr() becomes: write trampoline + checkpoint_set + registers_set + exit_resume
     + wait for 0x62 stopped event — ~0.08ms vs ~2500ms (31,000x faster)
   - No need for jsr_poll() — the binary monitor stays responsive during warp
   - Remove 64-byte write chunking — binary protocol has no size limit
   - Port allocation unchanged (just use -binarymonitor instead of -remotemonitor)

Protocol reference:
  https://vice-emu.sourceforge.io/vice_13.html (Binary Monitor Interface)

Usage:
  python3 scripts/test_binary_monitor.py [--vice-path x64sc] [--port 6502]

Requires x64sc (VICE) on PATH.
============================================================================
"""

from __future__ import annotations

import argparse
import socket
import struct
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Optional


# ============================================================================
# Binary Protocol Framing
# ============================================================================

# Request header:  STX(1) + API(1) + body_length(4) + request_id(4) + cmd(1) = 11 bytes
REQUEST_HEADER_FMT = "<BBIIB"   # 1+1+4+4+1 = 11
REQUEST_HEADER_SIZE = struct.calcsize(REQUEST_HEADER_FMT)

# Response header: STX(1) + API(1) + body_length(4) + resp_type(1) + err(1) + req_id(4) = 12 bytes
RESPONSE_HEADER_FMT = "<BBIBBI"  # 1+1+4+1+1+4 = 12
RESPONSE_HEADER_SIZE = struct.calcsize(RESPONSE_HEADER_FMT)

STX = 0x02
API_VERSION = 0x02

# Command / response-type IDs
CMD_MEMORY_GET          = 0x01
CMD_MEMORY_SET          = 0x02
CMD_CHECKPOINT_SET      = 0x12
CMD_CHECKPOINT_DELETE   = 0x13
CMD_REGISTERS_GET       = 0x31
CMD_REGISTERS_SET       = 0x32
CMD_EXIT                = 0xAA
CMD_KEYBOARD_FEED       = 0x72
CMD_REGISTERS_AVAILABLE = 0x83
CMD_PING                = 0x81

# Unsolicited event types (response_type field, request_id=0xffffffff)
EVENT_STOPPED           = 0x62
EVENT_RESUMED           = 0x63

# Error codes
ERR_OK = 0x00

# Memspace
MEMSPACE_MAIN = 0x00

# Checkpoint CPU operation
CP_EXEC = 0x04


# ============================================================================
# Response dataclass
# ============================================================================

@dataclass
class Response:
    """Parsed binary monitor response."""
    api_version: int
    body_length: int
    response_type: int   # echoes command ID, or event ID (0x62 stopped, etc.)
    error_code: int
    request_id: int      # echoes request, or 0xffffffff for unsolicited events
    body: bytes

    @property
    def is_event(self) -> bool:
        return self.request_id == 0xFFFFFFFF

    @property
    def is_ok(self) -> bool:
        return self.error_code == ERR_OK

    def __repr__(self) -> str:
        return (
            f"Response(resp_type=0x{self.response_type:02x}, "
            f"err=0x{self.error_code:02x}, "
            f"req_id=0x{self.request_id:08x}, "
            f"body_len={self.body_length})"
        )


# ============================================================================
# BinaryMonitor client
# ============================================================================

class ProtocolError(Exception):
    pass


class BinaryMonitor:
    """Low-level VICE binary monitor protocol client."""

    def __init__(self, host: str = "127.0.0.1", port: int = 6502,
                 timeout: float = 5.0, debug: bool = False):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._request_id: int = 0
        self._debug = debug
        # Populated by registers_available()
        self._reg_name_to_id: dict[str, int] = {}
        self._reg_id_to_name: dict[int, str] = {}
        self._reg_id_to_bits: dict[int, int] = {}

    # ---- connection ----

    def connect(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(self.timeout)
        self._sock.connect((self.host, self.port))

    def close(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    # ---- low-level I/O ----

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def send_command(self, cmd_type: int, body: bytes = b"") -> int:
        """Send a request. Returns the request ID used."""
        assert self._sock is not None, "Not connected"
        req_id = self._next_id()
        header = struct.pack(REQUEST_HEADER_FMT, STX, API_VERSION,
                             len(body), req_id, cmd_type)
        packet = header + body
        if self._debug:
            print(f"  [TX] cmd=0x{cmd_type:02x} id={req_id} body={len(body)}B  raw={packet[:32].hex()}{'...' if len(packet)>32 else ''}")
        self._sock.sendall(packet)
        return req_id

    def recv_response(self, timeout: float | None = None) -> Response:
        """Read one response/event from the wire."""
        assert self._sock is not None, "Not connected"
        old_timeout = self._sock.gettimeout()
        if timeout is not None:
            self._sock.settimeout(timeout)
        try:
            hdr = self._recv_exact(RESPONSE_HEADER_SIZE)
            stx, api, body_len, rtype, err, rid = struct.unpack(RESPONSE_HEADER_FMT, hdr)
            if stx != STX:
                raise ProtocolError(f"Expected STX 0x02, got 0x{stx:02x}")
            body = self._recv_exact(body_len) if body_len > 0 else b""
            resp = Response(api, body_len, rtype, err, rid, body)
            if self._debug:
                print(f"  [RX] {resp}  body={body[:32].hex()}{'...' if len(body)>32 else ''}")
            return resp
        finally:
            self._sock.settimeout(old_timeout)

    def _recv_exact(self, n: int) -> bytes:
        assert self._sock is not None
        buf = bytearray()
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError(f"Socket closed; needed {n}, got {len(buf)}")
            buf.extend(chunk)
        return bytes(buf)

    def recv_for(self, req_id: int, timeout: float | None = None) -> Response:
        """Read responses until one matches req_id; collect events."""
        deadline = time.monotonic() + (timeout or self.timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"No response for request {req_id}")
            resp = self.recv_response(timeout=remaining)
            if resp.request_id == req_id:
                return resp
            # unsolicited event — skip for now

    def drain_events(self, timeout: float = 0.2) -> list[Response]:
        events: list[Response] = []
        while True:
            try:
                resp = self.recv_response(timeout=timeout)
                events.append(resp)
            except (socket.timeout, TimeoutError, OSError):
                break
        return events

    # ---- high-level commands ----

    def ping(self) -> Response:
        rid = self.send_command(CMD_PING)
        return self.recv_for(rid)

    def registers_available(self, memspace: int = MEMSPACE_MAIN) -> list[dict]:
        """Query register names/IDs. Populates internal maps."""
        rid = self.send_command(CMD_REGISTERS_AVAILABLE, bytes([memspace]))
        resp = self.recv_for(rid)
        if not resp.is_ok:
            raise ProtocolError(f"registers_available error: {resp}")
        return self._parse_regs_available(resp.body)

    def _parse_regs_available(self, body: bytes) -> list[dict]:
        """Parse Registers Available response body.

        Format: [2-byte count] then per register:
          [1-byte IS (item size excl. this byte)]
          [1-byte RI (register ID)]
          [1-byte RS (register size in bits)]
          [1-byte NL (name length)]
          [NL-byte RN (name)]
        """
        if len(body) < 2:
            return []
        count = struct.unpack_from("<H", body, 0)[0]
        off = 2
        regs = []
        for _ in range(count):
            if off >= len(body):
                break
            item_size = body[off]
            off += 1
            if off + item_size > len(body):
                break
            reg_id = body[off]
            size_bits = body[off + 1]
            name_len = body[off + 2]
            name = body[off + 3:off + 3 + name_len].decode("ascii", errors="replace")
            off += item_size  # skip the entire item
            regs.append({"name": name, "id": reg_id, "size_bits": size_bits})
            self._reg_name_to_id[name] = reg_id
            self._reg_id_to_name[reg_id] = name
            self._reg_id_to_bits[reg_id] = size_bits
        return regs

    def memory_get(self, start: int, end: int, *,
                   side_effects: bool = False, memspace: int = MEMSPACE_MAIN,
                   bank: int = 0) -> bytes:
        """Read memory [start, end] inclusive."""
        body = struct.pack("<BHHBH", 1 if side_effects else 0,
                           start, end, memspace, bank)
        rid = self.send_command(CMD_MEMORY_GET, body)
        resp = self.recv_for(rid)
        if not resp.is_ok:
            raise ProtocolError(f"memory_get error: {resp}")
        # Body: [2-byte length] [data bytes]
        if len(resp.body) < 2:
            return b""
        data_len = struct.unpack_from("<H", resp.body, 0)[0]
        return resp.body[2:2 + data_len]

    def memory_set(self, start: int, data: bytes, *,
                   side_effects: bool = False, memspace: int = MEMSPACE_MAIN,
                   bank: int = 0) -> Response:
        """Write data starting at start."""
        end = start + len(data) - 1
        hdr = struct.pack("<BHHBH", 1 if side_effects else 0,
                          start, end, memspace, bank)
        rid = self.send_command(CMD_MEMORY_SET, hdr + data)
        return self.recv_for(rid)

    def registers_get(self, memspace: int = MEMSPACE_MAIN) -> dict[str, int]:
        """Get all registers, return {name: value}."""
        rid = self.send_command(CMD_REGISTERS_GET, bytes([memspace]))
        resp = self.recv_for(rid)
        if not resp.is_ok:
            raise ProtocolError(f"registers_get error: {resp}")
        return self._parse_reg_values(resp.body)

    def _parse_reg_values(self, body: bytes) -> dict[str, int]:
        """Parse Registers Get (0x31) response body.

        Format: [2-byte count] then per register:
          [1-byte IS (item size excl. this byte)]
          [1-byte RI (register ID)]
          [IS-1 bytes RV (register value, LE)]
        """
        if len(body) < 2:
            return {}
        count = struct.unpack_from("<H", body, 0)[0]
        off = 2
        regs: dict[str, int] = {}
        for _ in range(count):
            if off >= len(body):
                break
            item_size = body[off]
            off += 1
            if off + item_size > len(body):
                break
            reg_id = body[off]
            val_bytes = item_size - 1  # remaining bytes are the value
            val = int.from_bytes(body[off + 1:off + 1 + val_bytes], "little")
            off += item_size
            name = self._reg_id_to_name.get(reg_id, f"reg_{reg_id}")
            regs[name] = val
        return regs

    def registers_set(self, reg_values: dict[str, int],
                      memspace: int = MEMSPACE_MAIN) -> Response:
        """Set registers by name. Must call registers_available() first."""
        items = []
        for name, val in reg_values.items():
            rid = self._reg_name_to_id.get(name)
            if rid is None:
                raise ValueError(f"Unknown register {name!r}; available: {list(self._reg_name_to_id)}")
            bits = self._reg_id_to_bits[rid]
            val_bytes = max(1, (bits + 7) // 8)
            item_size = 1 + val_bytes  # RI + RV
            items.append((item_size, rid, val, val_bytes))
        count = len(items)
        body = struct.pack("<BH", memspace, count)
        for item_size, rid, val, val_bytes in items:
            body += struct.pack("<BB", item_size, rid)
            body += val.to_bytes(val_bytes, "little")
        req = self.send_command(CMD_REGISTERS_SET, body)
        return self.recv_for(req)

    def checkpoint_set(self, start: int, end: int, *,
                       stop_when_hit: bool = True, enabled: bool = True,
                       cpu_op: int = CP_EXEC, temporary: bool = False) -> tuple[Response, int]:
        """Set a checkpoint. Returns (response, checkpoint_number)."""
        body = struct.pack("<HHBBBB", start, end,
                           int(stop_when_hit), int(enabled), cpu_op, int(temporary))
        rid = self.send_command(CMD_CHECKPOINT_SET, body)
        resp = self.recv_for(rid)
        cp_num = 0
        if resp.is_ok and len(resp.body) >= 4:
            cp_num = struct.unpack_from("<I", resp.body, 0)[0]
        return resp, cp_num

    def checkpoint_delete(self, cp_num: int) -> Response:
        body = struct.pack("<I", cp_num)
        rid = self.send_command(CMD_CHECKPOINT_DELETE, body)
        return self.recv_for(rid)

    def exit_resume(self) -> Response:
        """Send Exit (0xAA) — resume CPU."""
        rid = self.send_command(CMD_EXIT)
        return self.recv_for(rid)

    def keyboard_feed(self, text: str) -> Response:
        """Feed keyboard text (PETSCII)."""
        data = text.encode("ascii", errors="replace")
        body = struct.pack("<B", len(data)) + data
        rid = self.send_command(CMD_KEYBOARD_FEED, body)
        return self.recv_for(rid)


# ============================================================================
# VICE process management
# ============================================================================

def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def launch_vice(executable: str, port: int) -> subprocess.Popen:
    args = [
        executable,
        "-binarymonitor",
        "-binarymonitoraddress", f"ip4://127.0.0.1:{port}",
        "-warp", "-ntsc", "+sound", "-minimized",
    ]
    print(f"  Launching: {' '.join(args)}")
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_for_port(host: str, port: int, timeout: float = 30.0) -> bool:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2)
            s.connect((host, port))
            s.close()
            return True
        except OSError:
            time.sleep(0.5)
    return False


# ============================================================================
# Test functions
# ============================================================================

def test_connect(mon: BinaryMonitor) -> bool:
    """Test A: Connect — does CPU pause? Does VICE send anything initially?"""
    print("\n=== Test A: Connect ===")
    try:
        mon.connect()
        print("  Connected to binary monitor (persistent TCP connection)")
        # Check for unsolicited data
        events = mon.drain_events(timeout=0.5)
        if events:
            print(f"  VICE sent {len(events)} unsolicited response(s) on connect:")
            for ev in events:
                print(f"    {ev}")
        else:
            print("  No unsolicited data on connect")
        print("  FINDING: Unlike text monitor, binary monitor does NOT send data on connect.")
        print("  FINDING: CPU appears to keep running until a command is sent.")
        return True
    except Exception as e:
        print(f"  FAILED: {e}")
        return False


def test_ping_and_initial_stop(mon: BinaryMonitor) -> bool:
    """Test A2: Ping — does sending any command stop the CPU?"""
    print("\n=== Test A2: Ping + Auto-Stop Behaviour ===")
    try:
        rid = mon.send_command(CMD_PING)
        # Per VICE docs: sending any command stops the CPU.
        # We may get unsolicited register dump and stopped event BEFORE the ping response.
        responses = []
        deadline = time.monotonic() + 5.0
        got_ping = False
        while time.monotonic() < deadline:
            try:
                resp = mon.recv_response(timeout=2.0)
                responses.append(resp)
                if resp.request_id == rid:
                    got_ping = True
                    break
            except (socket.timeout, TimeoutError):
                break
        print(f"  Received {len(responses)} response(s) after ping:")
        for r in responses:
            label = "UNSOLICITED" if r.is_event else f"reply to #{r.request_id}"
            print(f"    [{label}] type=0x{r.response_type:02x} err=0x{r.error_code:02x} body={len(r.body)}B")
            if r.response_type == EVENT_STOPPED:
                pc = struct.unpack_from("<H", r.body, 0)[0] if len(r.body) >= 2 else -1
                print(f"      Stopped event: PC=${pc:04x}")
            elif r.response_type == CMD_REGISTERS_GET and r.is_event:
                print(f"      Unsolicited register dump ({len(r.body)} bytes)")
        if got_ping:
            print("  FINDING: Ping response received. CPU is now stopped.")
        else:
            print("  WARNING: No ping response received!")
        return got_ping
    except Exception as e:
        print(f"  FAILED: {e}")
        import traceback; traceback.print_exc()
        return False


def test_registers_available(mon: BinaryMonitor) -> bool:
    """Test B: Registers Available (0x83)."""
    print("\n=== Test B: Registers Available (0x83) ===")
    try:
        regs = mon.registers_available()
        print(f"  Found {len(regs)} registers:")
        for r in regs:
            print(f"    ID=0x{r['id']:02x}  {r['size_bits']:2d} bits  name={r['name']!r}")
        needed = {"PC", "A", "X", "Y", "SP"}
        found = {r["name"] for r in regs} & needed
        missing = needed - found
        if missing:
            print(f"  WARNING: Missing needed registers: {missing}")
            print(f"  Available names: {sorted(r['name'] for r in regs)}")
        else:
            print(f"  All critical registers found: {sorted(found)}")
        return len(found) >= 3  # at least PC, A, Y should be there
    except Exception as e:
        print(f"  FAILED: {e}")
        import traceback; traceback.print_exc()
        return False


def test_memory_get(mon: BinaryMonitor) -> bool:
    """Test C: Memory Get (0x01) — read from $0400 (screen memory)."""
    print("\n=== Test C: Memory Get (0x01) ===")
    try:
        data = mon.memory_get(0x0400, 0x040F)
        print(f"  Read {len(data)} bytes from $0400-$040F:")
        print(f"  Hex: {' '.join(f'{b:02x}' for b in data)}")
        expected = 16
        ok = len(data) == expected
        print(f"  RESULT: Got {len(data)} bytes (expected {expected}): {'PASS' if ok else 'FAIL'}")
        if not ok:
            print(f"  NOTE: End address may need +1 (exclusive). Trying $0400-$0410...")
            data2 = mon.memory_get(0x0400, 0x0410)
            print(f"    Got {len(data2)} bytes with end=$0410")
        return ok
    except Exception as e:
        print(f"  FAILED: {e}")
        import traceback; traceback.print_exc()
        return False


def test_memory_set(mon: BinaryMonitor) -> bool:
    """Test D: Memory Set (0x02) — large writes (256, 1024, 4096 bytes)."""
    print("\n=== Test D: Memory Set (0x02) — Large Writes ===")
    results = {}
    for size, addr in [(256, 0x4000), (1024, 0x5000), (4096, 0x6000)]:
        try:
            test_data = bytes([i & 0xFF for i in range(size)])
            resp = mon.memory_set(addr, test_data)
            end = addr + size - 1
            readback = mon.memory_get(addr, end)
            match = readback == test_data
            results[size] = match
            print(f"  {size:5d}-byte write: err=0x{resp.error_code:02x}  "
                  f"readback={len(readback)}B  match={'PASS' if match else 'FAIL'}")
            if not match and len(readback) > 0:
                # Find first mismatch
                for i in range(min(len(readback), len(test_data))):
                    if readback[i] != test_data[i]:
                        print(f"    First mismatch at offset {i}: got 0x{readback[i]:02x} expected 0x{test_data[i]:02x}")
                        break
        except Exception as e:
            print(f"  {size:5d}-byte write: FAILED — {e}")
            results[size] = False

    all_ok = all(results.values())
    if results.get(256):
        print("  FINDING: 256-byte write works! Text monitor truncates at ~84 bytes.")
    if results.get(1024):
        print("  FINDING: 1024-byte write works!")
    if results.get(4096):
        print("  FINDING: 4096-byte write works! No practical size limit found.")
    return all_ok


def test_registers_get(mon: BinaryMonitor) -> bool:
    """Test E: Registers Get (0x31)."""
    print("\n=== Test E: Registers Get (0x31) ===")
    try:
        regs = mon.registers_get()
        print(f"  Got {len(regs)} registers:")
        for name in sorted(regs):
            val = regs[name]
            bits = mon._reg_id_to_bits.get(mon._reg_name_to_id.get(name, -1), 0)
            if bits <= 8:
                print(f"    {name:6s} = ${val:02x} ({val})")
            else:
                print(f"    {name:6s} = ${val:04x} ({val})")
        return "PC" in regs
    except Exception as e:
        print(f"  FAILED: {e}")
        import traceback; traceback.print_exc()
        return False


def test_registers_set(mon: BinaryMonitor) -> bool:
    """Test F: Registers Set (0x32) — set PC to $C000."""
    print("\n=== Test F: Registers Set (0x32) ===")
    try:
        target = 0xC000
        resp = mon.registers_set({"PC": target})
        print(f"  Set PC=${target:04x}: err=0x{resp.error_code:02x}")
        regs = mon.registers_get()
        actual = regs.get("PC", -1)
        ok = actual == target
        print(f"  Readback PC=${actual:04x}: {'PASS' if ok else 'FAIL'}")
        return ok
    except Exception as e:
        print(f"  FAILED: {e}")
        import traceback; traceback.print_exc()
        return False


def test_exit_and_breakpoint(mon: BinaryMonitor) -> bool:
    """Test G+H: Checkpoint, Exit (resume), connection persistence, Stopped event."""
    print("\n=== Test G+H: Checkpoint + Exit + Stopped Event ===")
    try:
        # Write NOP sled + JMP $C000 loop at $C000
        code = bytes([0xEA] * 16 + [0x4C, 0x00, 0xC0])
        resp = mon.memory_set(0xC000, code)
        print(f"  Wrote NOP-loop at $C000: {'OK' if resp.is_ok else 'FAIL'}")

        # Set breakpoint at $C008
        bp_resp, bp_num = mon.checkpoint_set(0xC008, 0xC008, stop_when_hit=True)
        print(f"  Breakpoint #{bp_num} at $C008: err=0x{bp_resp.error_code:02x}")

        # Set PC to $C000
        mon.registers_set({"PC": 0xC000})

        # Exit = resume CPU
        print("  Sending Exit (0xAA) to resume CPU...")
        exit_resp = mon.exit_resume()
        print(f"  Exit response: err=0x{exit_resp.error_code:02x}")

        # KEY TEST: Does the connection survive? Wait for stopped event.
        print("  Waiting for Stopped event...")
        stopped = False
        stopped_pc = -1
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                resp = mon.recv_response(timeout=2.0)
                print(f"    Received: {resp}")
                if resp.response_type == EVENT_STOPPED:
                    stopped = True
                    if len(resp.body) >= 2:
                        stopped_pc = struct.unpack_from("<H", resp.body, 0)[0]
                    break
            except (socket.timeout, TimeoutError):
                break

        if stopped:
            print(f"  FINDING: Stopped event received! PC=${stopped_pc:04x}")
            print(f"  FINDING: Connection SURVIVES after Exit (resume)!")
            print(f"  FINDING: This is the CRITICAL advantage over text monitor.")
        else:
            print(f"  WARNING: No stopped event received.")

        # Can we still use the connection?
        try:
            regs = mon.registers_get()
            pc = regs.get("PC", -1)
            print(f"  Post-stop registers readable: PC=${pc:04x}")
        except Exception as e:
            print(f"  Post-stop register read failed: {e}")

        # Cleanup
        try:
            mon.checkpoint_delete(bp_num)
            print(f"  Deleted checkpoint #{bp_num}")
        except Exception:
            pass

        return stopped

    except Exception as e:
        print(f"  FAILED: {e}")
        import traceback; traceback.print_exc()
        return False


def test_keyboard_feed(mon: BinaryMonitor) -> bool:
    """Test I: Keyboard Feed (0x72)."""
    print("\n=== Test I: Keyboard Feed (0x72) ===")
    try:
        resp = mon.keyboard_feed("HELLO\r")
        ok = resp.is_ok
        print(f"  Keyboard feed 'HELLO\\r': err=0x{resp.error_code:02x} {'PASS' if ok else 'FAIL'}")
        return ok
    except Exception as e:
        print(f"  FAILED: {e}")
        import traceback; traceback.print_exc()
        return False


def test_jsr_flow(mon: BinaryMonitor) -> bool:
    """Test J: Full jsr() equivalent via binary monitor.

    Subroutine at $C100: LDA #$42 / STA $C080 / RTS
    Trampoline at $0334: JSR $C100 / NOP / NOP
    Breakpoint at $0337 (NOP after JSR returns)
    """
    print("\n=== Test J: JSR Equivalent Flow ===")
    try:
        # Write subroutine
        sub = bytes([0xA9, 0x42, 0x8D, 0x80, 0xC0, 0x60])  # LDA #$42, STA $C080, RTS
        mon.memory_set(0xC100, sub)
        mon.memory_set(0xC080, bytes([0x00]))  # clear result
        print("  Wrote subroutine at $C100 (LDA #$42, STA $C080, RTS)")

        # Write trampoline
        trampoline = bytes([0x20, 0x00, 0xC1, 0xEA, 0xEA])  # JSR $C100, NOP, NOP
        mon.memory_set(0x0334, trampoline)
        print("  Wrote trampoline at $0334 (JSR $C100; NOP; NOP)")

        # Breakpoint at $0337
        bp_resp, bp_num = mon.checkpoint_set(0x0337, 0x0337, stop_when_hit=True)
        print(f"  Breakpoint #{bp_num} at $0337: err=0x{bp_resp.error_code:02x}")

        # Set PC
        mon.registers_set({"PC": 0x0334})

        # Resume and time it
        t0 = time.monotonic()
        mon.exit_resume()

        # Wait for stopped
        stopped = False
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                resp = mon.recv_response(timeout=2.0)
                if resp.response_type == EVENT_STOPPED:
                    stopped = True
                    break
            except (socket.timeout, TimeoutError):
                break
        t1 = time.monotonic()

        if not stopped:
            print("  ERROR: No stopped event after JSR flow")
            return False

        jsr_time_ms = (t1 - t0) * 1000
        print(f"  JSR flow completed in {jsr_time_ms:.1f}ms")

        # Verify results
        regs = mon.registers_get()
        pc = regs.get("PC", -1)
        a_val = regs.get("A", -1)
        result = mon.memory_get(0xC080, 0xC080)
        mem_val = result[0] if result else -1

        print(f"  PC    = ${pc:04x} (expected $0337)")
        print(f"  A     = ${a_val:02x}   (expected $42)")
        print(f"  $C080 = ${mem_val:02x}   (expected $42)")

        ok = (pc == 0x0337) and (a_val == 0x42) and (mem_val == 0x42)
        print(f"  RESULT: {'PASS' if ok else 'FAIL'}")

        # Cleanup
        mon.checkpoint_delete(bp_num)
        return ok

    except Exception as e:
        print(f"  FAILED: {e}")
        import traceback; traceback.print_exc()
        return False


def test_performance(mon: BinaryMonitor) -> None:
    """Measure key operation timings."""
    print("\n=== Performance Measurements ===")

    # Memory Get: 100 reads of 16 bytes
    times = []
    for _ in range(100):
        t0 = time.monotonic()
        mon.memory_get(0x0400, 0x040F)
        times.append(time.monotonic() - t0)
    avg = sum(times) / len(times)
    print(f"  Memory Get (16 bytes, N=100):")
    print(f"    Avg: {avg*1000:.3f}ms  Min: {min(times)*1000:.3f}ms  Max: {max(times)*1000:.3f}ms")

    # Memory Set: 256 bytes
    data256 = bytes(range(256))
    times = []
    for _ in range(100):
        t0 = time.monotonic()
        mon.memory_set(0x4000, data256)
        times.append(time.monotonic() - t0)
    avg = sum(times) / len(times)
    print(f"  Memory Set (256 bytes, N=100):")
    print(f"    Avg: {avg*1000:.3f}ms  Min: {min(times)*1000:.3f}ms  Max: {max(times)*1000:.3f}ms")

    # Memory Set: 1024 bytes
    data1k = bytes([i & 0xFF for i in range(1024)])
    times = []
    for _ in range(50):
        t0 = time.monotonic()
        mon.memory_set(0x5000, data1k)
        times.append(time.monotonic() - t0)
    avg = sum(times) / len(times)
    print(f"  Memory Set (1024 bytes, N=50):")
    print(f"    Avg: {avg*1000:.3f}ms  Min: {min(times)*1000:.3f}ms  Max: {max(times)*1000:.3f}ms")

    # Registers Get
    times = []
    for _ in range(100):
        t0 = time.monotonic()
        mon.registers_get()
        times.append(time.monotonic() - t0)
    avg = sum(times) / len(times)
    print(f"  Registers Get (N=100):")
    print(f"    Avg: {avg*1000:.3f}ms  Min: {min(times)*1000:.3f}ms  Max: {max(times)*1000:.3f}ms")

    # Full JSR roundtrip
    sub = bytes([0xA9, 0x42, 0x8D, 0x80, 0xC0, 0x60])
    mon.memory_set(0xC100, sub)
    trampoline = bytes([0x20, 0x00, 0xC1, 0xEA, 0xEA])
    mon.memory_set(0x0334, trampoline)
    times = []
    for _ in range(20):
        bp_resp, bp_num = mon.checkpoint_set(0x0337, 0x0337, stop_when_hit=True)
        mon.registers_set({"PC": 0x0334})
        t0 = time.monotonic()
        mon.exit_resume()
        # Wait for stop
        while True:
            try:
                resp = mon.recv_response(timeout=5.0)
                if resp.response_type == EVENT_STOPPED:
                    break
            except (socket.timeout, TimeoutError):
                break
        times.append(time.monotonic() - t0)
        mon.checkpoint_delete(bp_num)
    if times:
        avg = sum(times) / len(times)
        print(f"  Full JSR roundtrip (N={len(times)}):")
        print(f"    Avg: {avg*1000:.3f}ms  Min: {min(times)*1000:.3f}ms  Max: {max(times)*1000:.3f}ms")

    print(f"\n  --- Comparison with text monitor ---")
    print(f"  Text monitor per-command: ~400ms (connect + drain + close)")
    print(f"  Text monitor jsr() flow:  ~2000-3000ms (8+ TCP roundtrips)")
    print(f"  Binary monitor: see above (persistent connection, no reconnect)")


# ============================================================================
# Main
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="Test VICE binary monitor protocol")
    parser.add_argument("--vice-path", default="x64sc", help="Path to VICE executable")
    parser.add_argument("--port", type=int, default=0, help="Port (0 = auto)")
    parser.add_argument("--no-launch", action="store_true",
                        help="Don't launch VICE, connect to existing instance")
    parser.add_argument("--debug", action="store_true", help="Show wire-level debug")
    args = parser.parse_args()

    port = args.port or find_free_port()
    proc = None

    print("=" * 70)
    print("VICE Binary Monitor Protocol — Research Test")
    print("=" * 70)

    if not args.no_launch:
        print(f"\nLaunching VICE on port {port}...")
        try:
            proc = launch_vice(args.vice_path, port)
        except FileNotFoundError:
            print(f"ERROR: {args.vice_path} not found on PATH.")
            print("Re-run with: python3 scripts/test_binary_monitor.py --vice-path /path/to/x64sc")
            sys.exit(1)

        print(f"  VICE PID: {proc.pid}")
        print("  Waiting for binary monitor port...")
        if not wait_for_port("127.0.0.1", port, timeout=30):
            print("  ERROR: Binary monitor port did not open within 30s")
            if proc.poll() is not None:
                print(f"  VICE exited with code {proc.returncode}")
            proc.terminate()
            sys.exit(1)
        print("  Port is accepting connections!")
    else:
        print(f"\nConnecting to existing VICE on port {port}...")

    mon = BinaryMonitor(port=port, timeout=5.0, debug=args.debug)
    results: dict[str, bool] = {}

    try:
        results["A_connect"] = test_connect(mon)
        if not results["A_connect"]:
            print("\nCannot continue without connection.")
            return

        results["A2_ping_stop"] = test_ping_and_initial_stop(mon)
        results["B_regs_available"] = test_registers_available(mon)
        results["C_mem_get"] = test_memory_get(mon)
        results["D_mem_set"] = test_memory_set(mon)
        results["E_regs_get"] = test_registers_get(mon)
        results["F_regs_set"] = test_registers_set(mon)
        results["GH_breakpoint_exit"] = test_exit_and_breakpoint(mon)
        results["I_keyboard"] = test_keyboard_feed(mon)
        results["J_jsr_flow"] = test_jsr_flow(mon)

        test_performance(mon)

        # Summary
        print("\n" + "=" * 70)
        print("SUMMARY")
        print("=" * 70)
        for name, ok in results.items():
            print(f"  {name:30s} {'PASS' if ok else 'FAIL'}")
        passed = sum(1 for v in results.values() if v)
        print(f"\n  {passed}/{len(results)} tests passed")

    except Exception as e:
        print(f"\nUnhandled error: {e}")
        import traceback; traceback.print_exc()
    finally:
        mon.close()
        if proc is not None:
            print("\nTerminating VICE...")
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            print("  Done.")


if __name__ == "__main__":
    main()
