#!/usr/bin/env python3
"""TCP echo test via UCI on the Ultimate 64.

Starts a TCP echo server on this host, then injects a 6502 routine
on the U64 that:
  1. Opens a TCP connection to the test host
  2. Writes "HELLO UCI" through the socket
  3. Reads the echo response
  4. Closes the socket
  5. Stores results in C64 memory for readback

Usage:
    U64_HOST=192.168.1.81 python3 scripts/test_uci_tcp_echo.py

Note on CPU speed:
    This script hand-writes its own 6502 routine (not via the uci_network
    builders) and is NOT turbo-safe. It runs the U64 at stock 1 MHz so
    the FPGA behind $DF1C-$DF1F naturally settles between accesses. For
    UCI code that works at U64 turbo speeds (4/8/16/24/48 MHz), use the
    builders in c64_test_harness.uci_network with turbo_safe=True --
    see docs/uci_networking.md.
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from c64_test_harness.backends.device_lock import DeviceLock
from c64_test_harness.backends.ultimate64 import Ultimate64Transport
from c64_test_harness.backends.ultimate64_client import Ultimate64Client
from c64_test_harness.uci_network import enable_uci, disable_uci

# ---------------------------------------------------------------------------
# UCI registers and constants (same as uci_network.py)
# ---------------------------------------------------------------------------
UCI_STATUS   = 0xDF1C
UCI_CONTROL  = 0xDF1C
UCI_CMD_DATA = 0xDF1D
UCI_RESP     = 0xDF1E
UCI_STAT     = 0xDF1F

MASK_STATE_BUSY = 0x31
BIT_DATA_AV     = 0x80
BIT_STAT_AV     = 0x40
BIT_ERROR       = 0x08
BIT_CMD_BUSY    = 0x01

CTL_PUSH_CMD  = 0x01
CTL_NEXT_DATA = 0x02
CTL_ABORT     = 0x04
CTL_CLR_ERR   = 0x08

TARGET_NETWORK      = 0x03
CMD_TCP_CONNECT     = 0x07
CMD_SOCKET_CLOSE    = 0x09
CMD_SOCKET_READ     = 0x10
CMD_SOCKET_WRITE    = 0x11

# Memory layout
# Code can be up to ~600 bytes ($C000-$C257), so results must be above that.
# Using $C300 for results, $C400 for hostname, $C440 for write data.
CODE_ADDR       = 0xC000
RESULT_BASE     = 0xC300   # results area (must not overlap code)
HOST_STR_ADDR   = 0xC400   # hostname string (null-terminated)
WRITE_DATA_ADDR = 0xC440   # data to write

# Result offsets
OFF_CONNECT_ERR = 0x00  # connect error flag
OFF_SOCKET_ID   = 0x01  # socket ID from connect
OFF_WRITE_ERR   = 0x02  # write error flag
OFF_READ_ERR    = 0x03  # read error flag
OFF_READ_LEN    = 0x04  # response data length (1 byte)
OFF_READ_DATA   = 0x05  # response data (up to 64 bytes)
OFF_CLOSE_ERR   = 0x45  # close error flag
OFF_RETRY_LEFT  = 0x46  # retries remaining when read completed
OFF_UCI_SNAP_RD = 0x47  # UCI status snapshot after read push+wait
OFF_PROGRESS    = 0x7E  # progress step
OFF_SENTINEL    = 0x7F  # completion sentinel
OFF_STATUS_STR  = 0x80  # last status string (up to 64 bytes)
OFF_STATUS_LEN  = 0xC0  # status string length

SENTINEL_VALUE = 0x42
ECHO_PORT      = 7777
TEST_STRING    = b"HELLO UCI"


def _build_echo_routine(host: str, port: int, data: bytes) -> tuple[bytes, bytes, bytes]:
    """Build the 6502 routine + host string + write data.

    Returns (code_bytes, host_bytes, data_bytes).
    """
    code = bytearray()

    def emit(*bs):
        code.extend(bs)

    def abs_lo_hi(addr):
        return (addr & 0xFF, (addr >> 8) & 0xFF)

    def emit_sta(addr):
        lo, hi = abs_lo_hi(addr)
        emit(0x8D, lo, hi)

    def emit_lda_imm(val):
        emit(0xA9, val)

    def emit_lda_abs(addr):
        lo, hi = abs_lo_hi(addr)
        emit(0xAD, lo, hi)

    def emit_sta_ctl(val):
        emit_lda_imm(val)
        emit_sta(UCI_CONTROL)

    def emit_write_cmd(val):
        emit_lda_imm(val)
        emit_sta(UCI_CMD_DATA)

    def emit_wait_idle():
        pos = len(code)
        emit_lda_abs(UCI_STATUS)
        emit(0x29, MASK_STATE_BUSY)  # AND
        emit(0xD0, 0)  # BNE
        code[-1] = (pos - len(code)) & 0xFF

    def emit_wait_not_busy():
        pos = len(code)
        emit_lda_abs(UCI_STATUS)
        emit(0x29, BIT_CMD_BUSY)
        emit(0xD0, 0)
        code[-1] = (pos - len(code)) & 0xFF

    def emit_progress(step):
        emit_lda_imm(step)
        emit_sta(RESULT_BASE + OFF_PROGRESS)

    def emit_check_error(err_addr):
        """Check error bit; if set, store $FF at err_addr, clear error.
        Returns position of BEQ to patch if we want to skip more."""
        emit_lda_abs(UCI_STATUS)
        emit(0x29, BIT_ERROR)
        beq_pos = len(code)
        emit(0xF0, 0x00)  # BEQ skip
        # error path
        emit_lda_imm(0xFF)
        emit_sta(err_addr)
        emit_sta_ctl(CTL_CLR_ERR)
        # patch BEQ
        code[beq_pos + 1] = (len(code) - (beq_pos + 2)) & 0xFF

    def emit_read_status():
        """Read status string into RESULT_BASE+OFF_STATUS_STR, length into OFF_STATUS_LEN.

        IMPORTANT: Must write CTL_NEXT_DATA after each byte read from $DF1F,
        otherwise STAT_AV stays set and the loop never terminates.
        """
        status_addr = RESULT_BASE + OFF_STATUS_STR
        slen_addr = RESULT_BASE + OFF_STATUS_LEN
        emit(0xA0, 0x00)  # LDY #0
        pos = len(code)
        emit_lda_abs(UCI_STATUS)
        emit(0x29, BIT_STAT_AV)
        # BEQ done — skip: LDA(3) + STA abs,Y(3) + INY(1) + LDA+STA ctl(5) + JMP(3) = 15
        emit(0xF0, 15)
        emit_lda_abs(UCI_STAT)
        lo, hi = abs_lo_hi(status_addr)
        emit(0x99, lo, hi)  # STA abs,Y
        emit(0xC8)  # INY
        emit_sta_ctl(CTL_NEXT_DATA)  # acknowledge this byte
        # JMP back to loop
        loop_addr = CODE_ADDR + pos
        emit(0x4C, loop_addr & 0xFF, (loop_addr >> 8) & 0xFF)
        # done: store length
        lo, hi = abs_lo_hi(slen_addr)
        emit(0x8C, lo, hi)  # STY

    def emit_drain_resp():
        """Drain remaining response data."""
        pos = len(code)
        emit_lda_abs(UCI_STATUS)
        emit(0x29, BIT_DATA_AV)
        emit(0xF0, 0x0B)  # BEQ done
        emit_lda_abs(UCI_RESP)
        emit_sta_ctl(CTL_NEXT_DATA)
        loop_addr = CODE_ADDR + pos
        emit(0x4C, loop_addr & 0xFF, (loop_addr >> 8) & 0xFF)

    def emit_acknowledge():
        emit_sta_ctl(CTL_NEXT_DATA)
        emit_wait_idle()

    # === Clear result area ===
    emit_lda_imm(0x00)
    for off in [OFF_CONNECT_ERR, OFF_SOCKET_ID, OFF_WRITE_ERR,
                OFF_READ_ERR, OFF_READ_LEN, OFF_CLOSE_ERR,
                OFF_RETRY_LEFT, OFF_UCI_SNAP_RD,
                OFF_PROGRESS, OFF_SENTINEL, OFF_STATUS_LEN]:
        emit_sta(RESULT_BASE + off)

    # === Step 0: Abort pending UCI state ===
    emit_sta_ctl(CTL_ABORT)
    emit(0xA2, 0x20)  # LDX #$20
    emit(0xCA)         # DEX
    emit(0xD0, 0xFD)   # BNE -3

    # === Step 1: TCP_CONNECT ===
    emit_progress(0x01)
    emit_wait_idle()

    # Write target + command
    emit_write_cmd(TARGET_NETWORK)
    emit_write_cmd(CMD_TCP_CONNECT)

    # Write port (little-endian)
    emit_write_cmd(port & 0xFF)
    emit_write_cmd((port >> 8) & 0xFF)

    # Write hostname bytes from HOST_STR_ADDR until null
    emit(0xA0, 0x00)  # LDY #0
    host_loop = len(code)
    lo, hi = abs_lo_hi(HOST_STR_ADDR)
    emit(0xB9, lo, hi)  # LDA abs,Y
    emit(0xF0, 0x06)    # BEQ done_host (skip 6: STA(3)+INY(1)+BNE(2))
    emit_sta(UCI_CMD_DATA)
    emit(0xC8)           # INY
    offset_back = (host_loop - (len(code) + 2)) & 0xFF
    emit(0xD0, offset_back)  # BNE host_loop
    # Write null terminator
    emit_lda_imm(0x00)
    emit_sta(UCI_CMD_DATA)

    # Push command
    emit_sta_ctl(CTL_PUSH_CMD)
    emit_wait_not_busy()

    emit_progress(0x02)
    emit_check_error(RESULT_BASE + OFF_CONNECT_ERR)

    # Read response — socket ID is first byte
    emit_lda_abs(UCI_STATUS)
    emit(0x29, BIT_DATA_AV)
    emit(0xF0, 0x06)  # BEQ no_data (skip 6: LDA(3)+STA(3))
    emit_lda_abs(UCI_RESP)
    emit_sta(RESULT_BASE + OFF_SOCKET_ID)

    emit_drain_resp()
    emit_read_status()
    emit_acknowledge()

    emit_progress(0x03)

    # Brief delay for TCP connection to settle
    emit(0xA2, 0xFF)  # LDX #$FF
    emit(0xA0, 0x10)  # LDY #$10
    delay_outer = len(code)
    emit(0xCA)         # DEX
    emit(0xD0, 0xFD)   # BNE inner
    emit(0x88)         # DEY
    offset_back2 = (delay_outer - (len(code) + 2)) & 0xFF
    emit(0xD0, offset_back2)  # BNE outer

    # === Step 2: SOCKET_WRITE ===
    emit_progress(0x10)
    emit_wait_idle()

    emit_write_cmd(TARGET_NETWORK)
    emit_write_cmd(CMD_SOCKET_WRITE)

    # Socket ID from result
    emit_lda_abs(RESULT_BASE + OFF_SOCKET_ID)
    emit_sta(UCI_CMD_DATA)

    # Write data bytes from WRITE_DATA_ADDR using Y index, X as counter
    data_len = len(data)
    emit(0xA2, data_len)  # LDX #len
    emit(0xA0, 0x00)      # LDY #0
    write_loop = len(code)
    emit(0x8A)             # TXA
    emit(0xF0, 0x00)      # BEQ done_write (placeholder)
    beq_done_write = len(code) - 1
    lo, hi = abs_lo_hi(WRITE_DATA_ADDR)
    emit(0xB9, lo, hi)    # LDA abs,Y
    emit_sta(UCI_CMD_DATA)
    emit(0xC8)             # INY
    emit(0xCA)             # DEX
    offset_back3 = (write_loop - (len(code) + 2)) & 0xFF
    emit(0xD0, offset_back3)  # BNE write_loop
    # Patch BEQ
    code[beq_done_write] = (len(code) - (beq_done_write + 1)) & 0xFF

    emit_sta_ctl(CTL_PUSH_CMD)
    emit_wait_not_busy()

    emit_progress(0x11)
    emit_check_error(RESULT_BASE + OFF_WRITE_ERR)

    emit_drain_resp()
    emit_read_status()
    emit_acknowledge()

    emit_progress(0x12)

    # Longer delay for echo server to respond (~500ms at 1MHz)
    # Triple nested loop: Z(stored in ZP $FB) * Y * X
    emit_lda_imm(0x08)        # outer counter
    emit(0x85, 0xFB)          # STA $FB
    delay2_zp = len(code)
    emit(0xA0, 0xFF)          # LDY #$FF
    delay2_outer = len(code)
    emit(0xA2, 0xFF)          # LDX #$FF
    delay2_inner = len(code)
    emit(0xCA)                # DEX
    emit(0xD0, 0xFD)          # BNE inner
    emit(0x88)                # DEY
    offset_back4 = (delay2_outer - (len(code) + 2)) & 0xFF
    emit(0xD0, offset_back4)  # BNE outer
    emit(0xC6, 0xFB)          # DEC $FB
    offset_back5 = (delay2_zp - (len(code) + 2)) & 0xFF
    emit(0xD0, offset_back5)  # BNE zp loop

    # === Step 3: SOCKET_READ with retry ===
    # Use ZP $FC as retry counter (up to 20 retries)
    emit_lda_imm(20)
    emit(0x85, 0xFC)  # STA $FC

    read_retry_top = len(code)
    emit_progress(0x20)
    emit_wait_idle()

    emit_write_cmd(TARGET_NETWORK)
    emit_write_cmd(CMD_SOCKET_READ)

    # Socket ID
    emit_lda_abs(RESULT_BASE + OFF_SOCKET_ID)
    emit_sta(UCI_CMD_DATA)

    # Max length (LE) — read up to 64 bytes
    emit_write_cmd(0x40)  # 64
    emit_write_cmd(0x00)

    emit_sta_ctl(CTL_PUSH_CMD)
    emit_wait_not_busy()

    emit_progress(0x21)

    # Snapshot UCI status after read push+wait
    emit_lda_abs(UCI_STATUS)
    emit_sta(RESULT_BASE + OFF_UCI_SNAP_RD)

    emit_check_error(RESULT_BASE + OFF_READ_ERR)

    # Read response data into READ_DATA area
    emit(0xA0, 0x00)  # LDY #0
    read_loop = len(code)
    emit_lda_abs(UCI_STATUS)
    emit(0x29, BIT_DATA_AV)
    # BEQ done_read: skip LDA(3)+STA(3)+INY(1)+JMP(3) = 10 bytes
    emit(0xF0, 0x0A)
    emit_lda_abs(UCI_RESP)
    lo, hi = abs_lo_hi(RESULT_BASE + OFF_READ_DATA)
    emit(0x99, lo, hi)  # STA abs,Y
    emit(0xC8)           # INY
    loop_addr = CODE_ADDR + read_loop
    emit(0x4C, loop_addr & 0xFF, (loop_addr >> 8) & 0xFF)
    # done_read: store length
    lo, hi = abs_lo_hi(RESULT_BASE + OFF_READ_LEN)
    emit(0x8C, lo, hi)  # STY

    emit_read_status()
    emit_acknowledge()

    # Store retry counter value for diagnostics
    emit(0xA5, 0xFC)  # LDA $FC
    emit_sta(RESULT_BASE + OFF_RETRY_LEFT)

    # Check if we got data (Y > 0 means we got something)
    emit_lda_abs(RESULT_BASE + OFF_READ_LEN)
    # BNE skip_retry — we got data, proceed to close
    beq_got_data = len(code)
    emit(0xD0, 0x00)  # BNE skip (placeholder)

    # No data yet — decrement retry counter and delay
    emit(0xC6, 0xFC)  # DEC $FC
    # BEQ skip_retry — out of retries, give up
    beq_no_retries = len(code)
    emit(0xF0, 0x00)  # BEQ skip (placeholder)

    # Delay ~100ms before retry
    emit(0xA0, 0x40)  # LDY #$40
    retry_delay_outer = len(code)
    emit(0xA2, 0xFF)  # LDX #$FF
    retry_delay_inner = len(code)
    emit(0xCA)         # DEX
    emit(0xD0, 0xFD)   # BNE inner
    emit(0x88)         # DEY
    offset_retry_delay = (retry_delay_outer - (len(code) + 2)) & 0xFF
    emit(0xD0, offset_retry_delay)  # BNE outer

    # JMP back to retry top
    retry_jmp_addr = CODE_ADDR + read_retry_top
    emit(0x4C, retry_jmp_addr & 0xFF, (retry_jmp_addr >> 8) & 0xFF)

    # Patch the BNE/BEQ forward jumps
    skip_target = len(code)
    code[beq_got_data + 1] = (skip_target - (beq_got_data + 2)) & 0xFF
    code[beq_no_retries + 1] = (skip_target - (beq_no_retries + 2)) & 0xFF

    emit_progress(0x22)

    # === Step 4: SOCKET_CLOSE ===
    emit_progress(0x30)
    emit_wait_idle()

    emit_write_cmd(TARGET_NETWORK)
    emit_write_cmd(CMD_SOCKET_CLOSE)

    # Socket ID
    emit_lda_abs(RESULT_BASE + OFF_SOCKET_ID)
    emit_sta(UCI_CMD_DATA)

    emit_sta_ctl(CTL_PUSH_CMD)
    emit_wait_not_busy()

    emit_progress(0x31)
    emit_check_error(RESULT_BASE + OFF_CLOSE_ERR)

    emit_read_status()
    emit_acknowledge()

    emit_progress(0x32)

    # === Sentinel ===
    emit_lda_imm(SENTINEL_VALUE)
    emit_sta(RESULT_BASE + OFF_SENTINEL)

    # Park CPU
    park_addr = CODE_ADDR + len(code)
    emit(0x4C, park_addr & 0xFF, (park_addr >> 8) & 0xFF)

    host_bytes = host.encode("ascii") + b"\x00"
    return bytes(code), host_bytes, data


def _run_echo_server(host: str, port: int, result: dict):
    """Run a single-shot TCP echo server. Stores result in dict."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.settimeout(30.0)
    try:
        srv.bind((host, port))
        srv.listen(1)
        result["listening"] = True
        conn, addr = srv.accept()
        result["client_addr"] = addr
        data = conn.recv(1024)
        result["received"] = data
        conn.sendall(data)  # echo back
        conn.close()
    except Exception as exc:
        result["error"] = str(exc)
    finally:
        srv.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="UCI TCP echo test")
    parser.add_argument("--host", default=None,
                        help="U64 host (default: $U64_HOST or 192.168.1.81)")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()

    u64_host = args.host or os.environ.get("U64_HOST", "192.168.1.81")
    # Detect our own IP on the same subnet
    test_host_ip = _detect_local_ip(u64_host)

    print(f"UCI TCP Echo Test")
    print(f"  U64 target  : {u64_host}")
    print(f"  Test host   : {test_host_ip}:{ECHO_PORT}")
    print(f"  Test string : {TEST_STRING!r}")
    print("=" * 60)

    # Start echo server in background
    server_result: dict = {}
    server_thread = threading.Thread(
        target=_run_echo_server,
        args=(test_host_ip, ECHO_PORT, server_result),
        daemon=True,
    )
    server_thread.start()

    # Wait for server to be listening
    for _ in range(50):
        if server_result.get("listening"):
            break
        time.sleep(0.05)
    else:
        print("ERROR: Echo server failed to start")
        return 1
    print(f"Echo server listening on {test_host_ip}:{ECHO_PORT}")

    # Acquire device lock
    lock = DeviceLock(u64_host)
    if not lock.acquire(timeout=60.0):
        print("ERROR: Could not acquire device lock")
        return 1

    try:
        client = Ultimate64Client(host=u64_host, timeout=args.timeout)
        transport = Ultimate64Transport(host=u64_host, timeout=args.timeout,
                                        client=client)

        # Enable UCI transiently (not saved to flash)
        print("Enabling UCI (Command Interface)...")
        enable_uci(client)

        print("Resetting machine...")
        client.reset()
        time.sleep(3)

        # Build the routine
        code_bytes, host_bytes, data_bytes = _build_echo_routine(
            test_host_ip, ECHO_PORT, TEST_STRING
        )
        print(f"Routine size: {len(code_bytes)} bytes at ${CODE_ADDR:04X}")

        # Clear result area (chunked for 128-byte PUT limit)
        for i in range(0, 0xC1, 64):
            chunk_len = min(64, 0xC1 - i)
            transport.write_memory(RESULT_BASE + i, bytes(chunk_len))

        # Write code (chunked for firmware limit)
        CHUNK = 64
        for i in range(0, len(code_bytes), CHUNK):
            transport.write_memory(CODE_ADDR + i, code_bytes[i:i + CHUNK])

        # Write hostname string
        transport.write_memory(HOST_STR_ADDR, host_bytes)

        # Write data to send
        transport.write_memory(WRITE_DATA_ADDR, data_bytes)

        # Execute via keyboard buffer
        sys_cmd = b"SYS49152\r"
        transport.write_memory(0x0277, sys_cmd)
        transport.write_memory(0x00C6, bytes([len(sys_cmd)]))

        # Poll for sentinel
        print("Executing UCI TCP echo routine...")
        deadline = time.time() + 20.0
        sentinel = 0
        last_progress = -1
        while time.time() < deadline:
            time.sleep(0.3)
            status = transport.read_memory(RESULT_BASE + OFF_PROGRESS, 2)
            progress = status[0]
            sentinel = status[1]  # OFF_SENTINEL = OFF_PROGRESS + 1
            if progress != last_progress:
                print(f"  Progress: 0x{progress:02X}")
                last_progress = progress
            if sentinel == SENTINEL_VALUE:
                print("  Sentinel set -- routine complete")
                break
        else:
            print(f"TIMEOUT: Sentinel not set (progress=0x{progress:02X})")
            _dump_results(transport)
            return 1

        # Read results
        _dump_results(transport)

        # Wait for server thread
        server_thread.join(timeout=5.0)

        # Verify echo
        print()
        print("=" * 60)
        print("Server side:")
        if "error" in server_result:
            print(f"  ERROR: {server_result['error']}")
        else:
            print(f"  Client addr: {server_result.get('client_addr')}")
            print(f"  Received   : {server_result.get('received')!r}")

        # Read the echoed data from C64 memory
        read_len = transport.read_memory(RESULT_BASE + OFF_READ_LEN, 1)[0]
        if read_len > 0:
            raw = transport.read_memory(RESULT_BASE + OFF_READ_DATA, read_len)
            # UCI SOCKET_READ response: first 2 bytes = actual_len (LE), then data
            if read_len >= 2:
                actual_len = raw[0] | (raw[1] << 8)
                payload = raw[2:2 + actual_len]
            else:
                payload = raw
            print()
            print(f"Echo response raw ({read_len} bytes): {raw.hex()}")
            print(f"  actual_len field : {actual_len if read_len >= 2 else 'N/A'}")
            print(f"  payload          : {payload!r}")
            print()
            if payload == TEST_STRING:
                print("PASS: Echo roundtrip successful!")
                return 0
            else:
                print(f"FAIL: Expected {TEST_STRING!r}, got {payload!r}")
                return 1
        else:
            print()
            print("FAIL: No data received from echo")
            return 1

    finally:
        try:
            print("Disabling UCI (Command Interface)...")
            disable_uci(client)
        except Exception as exc:
            print(f"WARNING: Failed to disable UCI: {exc}")
        lock.release()


def _detect_local_ip(target: str) -> str:
    """Detect our IP address on the same subnet as target."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target, 80))
        return s.getsockname()[0]
    finally:
        s.close()


def _dump_results(transport: Ultimate64Transport) -> None:
    """Read and display results from C64 memory."""
    result = transport.read_memory(RESULT_BASE, 0xC1)

    print()
    print("--- C64 Memory Results ---")
    print(f"  Progress     : 0x{result[OFF_PROGRESS]:02X}")
    print(f"  Sentinel     : 0x{result[OFF_SENTINEL]:02X}")
    print(f"  Connect err  : 0x{result[OFF_CONNECT_ERR]:02X}")
    print(f"  Socket ID    : 0x{result[OFF_SOCKET_ID]:02X}")
    print(f"  Write err    : 0x{result[OFF_WRITE_ERR]:02X}")
    print(f"  Read err     : 0x{result[OFF_READ_ERR]:02X}")
    print(f"  Read len     : {result[OFF_READ_LEN]}")
    print(f"  Close err    : 0x{result[OFF_CLOSE_ERR]:02X}")
    retry_left = result[OFF_RETRY_LEFT]
    uci_snap = result[OFF_UCI_SNAP_RD]
    print(f"  Retries left : {retry_left}")
    print(f"  UCI snap(rd) : 0x{uci_snap:02X} "
          f"(DATA_AV={bool(uci_snap&0x80)} STAT_AV={bool(uci_snap&0x40)} "
          f"STATE={(uci_snap>>4)&3} ERR={bool(uci_snap&0x08)} "
          f"BUSY={bool(uci_snap&0x01)})")

    read_len = result[OFF_READ_LEN]
    if read_len > 0:
        data = result[OFF_READ_DATA:OFF_READ_DATA + min(read_len, 64)]
        print(f"  Read data    : {bytes(data).hex()}")
        try:
            print(f"  Read ascii   : {bytes(data).decode('ascii', errors='replace')!r}")
        except Exception:
            pass

    stat_len = result[OFF_STATUS_LEN]
    if stat_len > 0:
        stat_data = result[OFF_STATUS_STR:OFF_STATUS_STR + min(stat_len, 64)]
        try:
            print(f"  Last status  : {bytes(stat_data).decode('ascii', errors='replace')!r}")
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
