#!/usr/bin/env python3
"""Probe UCI (Ultimate Command Interface) networking on a U64E device.

Injects a 6502 routine via DMA that:
  1. Reads the UCI ID byte ($DF1D) -- expects $C9 if UCI is present
  2. Sends IDENTIFY to network target -- returns identification string
  3. Sends GET_INTERFACE_COUNT -- returns interface count byte
  4. Sends GET_IP_ADDRESS -- returns 12 bytes: IP(4) + Netmask(4) + Gateway(4)

UCI register interface at $DF1C-$DF1F.

Usage:
    U64_HOST=192.168.1.81 python3 scripts/probe_uci_network.py
    python3 scripts/probe_uci_network.py --host 192.168.1.81
"""
from __future__ import annotations

import argparse
import os
import struct
import sys
import time
import urllib.request

# Allow running from the repo root without install
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from c64_test_harness.backends.device_lock import DeviceLock
from c64_test_harness.backends.ultimate64 import Ultimate64Transport
from c64_test_harness.backends.ultimate64_client import Ultimate64Client


# ---------------------------------------------------------------------------
# UCI register addresses
# ---------------------------------------------------------------------------
UCI_STATUS   = 0xDF1C  # read: status bits; write: control
UCI_CONTROL  = 0xDF1C  # write alias
UCI_CMD_DATA = 0xDF1D  # write: command bytes
UCI_ID       = 0xDF1D  # read: identification byte
UCI_RESP     = 0xDF1E  # read: response data
UCI_STAT     = 0xDF1F  # read: status string data

# UCI status bits (read from $DF1C)
#   bit7: DATA_AV    -- response data available
#   bit6: STAT_AV    -- status string available
#   bit5-4: STATE    -- 00=Idle, 10=Busy, 20=LastData, 30=MoreData
#   bit3: ERROR
#   bit2: ABORT_PENDING
#   bit1: DATA_ACC
#   bit0: CMD_BUSY
MASK_STATE_BUSY = 0x31  # state bits (5,4) + cmd_busy (0) -- zero when idle
BIT_DATA_AV     = 0x80
BIT_STAT_AV     = 0x40
BIT_ERROR       = 0x08  # bit3
BIT_CMD_BUSY    = 0x01

# UCI control bits (write to $DF1C)
CTL_PUSH_CMD  = 0x01  # bit0
CTL_NEXT_DATA = 0x02  # bit1 -- acknowledge / next data
CTL_ABORT     = 0x04  # bit2
CTL_CLR_ERR   = 0x08  # bit3

# UCI network target + commands
TARGET_NETWORK      = 0x03
CMD_IDENTIFY        = 0x01
CMD_GET_IFACE_COUNT = 0x02
CMD_GET_IP_ADDRESS  = 0x05

# Expected UCI ID byte
UCI_ID_EXPECTED = 0xC9

# Memory layout for results at RESULT_BASE
RESULT_BASE = 0xC200
#   +$00:       UCI ID byte
#   +$01:       interface count
#   +$02:       error flags (bit0=identify_err, bit1=iface_err, bit2=ip_err)
#   +$03:       identify string length
#   +$04..$43:  identify string (up to 64 bytes)
#   +$44:       IP response length (should be 12)
#   +$45..$54:  IP response bytes: IP(4) + Netmask(4) + Gateway(4)
#   +$80:       completion sentinel

OFF_UCI_ID      = 0x00
OFF_IFACE_COUNT = 0x01
OFF_ERROR_FLAGS = 0x02
OFF_IDENT_LEN   = 0x03
OFF_IDENT_STR   = 0x04
OFF_IP_LEN      = 0x44
OFF_IP_DATA     = 0x45
OFF_UCI_SNAP    = 0x7E  # diagnostic: UCI status at last progress update
OFF_PROGRESS    = 0x7F  # diagnostic: tracks which step the code reached
OFF_SENTINEL    = 0x80

SENTINEL_VALUE = 0x42
CODE_ADDR = 0xC000


def _build_probe_prg() -> bytes:
    """Build a PRG that probes UCI network and stores results at RESULT_BASE.

    The routine:
    0. Aborts any in-progress UCI command (best practice from firmware)
    1. Reads UCI ID byte from $DF1D, stores at RESULT_BASE+0
    2. Sends IDENTIFY (target=0x03, cmd=0x01), stores response string
    3. Sends GET_INTERFACE_COUNT, stores response byte
    4. Sends GET_IP_ADDRESS, stores 12 response bytes
    5. Writes sentinel 0x42
    """
    code = bytearray()

    def emit(*bs):
        code.extend(bs)

    def abs_addr(offset):
        """Return (lo, hi) for RESULT_BASE + offset."""
        a = RESULT_BASE + offset
        return (a & 0xFF, (a >> 8) & 0xFF)

    def emit_sta(offset):
        """STA RESULT_BASE+offset (absolute)."""
        lo, hi = abs_addr(offset)
        emit(0x8D, lo, hi)

    def emit_progress(step):
        """Write step number + UCI status snapshot for diagnostics."""
        emit(0xA9, step)  # LDA #step
        emit_sta(OFF_PROGRESS)
        emit_lda_status()  # snapshot UCI status
        emit_sta(OFF_UCI_SNAP)

    def emit_lda_status():
        """LDA $DF1C."""
        emit(0xAD, UCI_STATUS & 0xFF, (UCI_STATUS >> 8) & 0xFF)

    def emit_sta_control(val):
        """LDA #val / STA $DF1C."""
        emit(0xA9, val)
        emit(0x8D, UCI_CONTROL & 0xFF, (UCI_CONTROL >> 8) & 0xFF)

    def emit_write_cmd_data(val):
        """LDA #val / STA $DF1D."""
        emit(0xA9, val)
        emit(0x8D, UCI_CMD_DATA & 0xFF, (UCI_CMD_DATA >> 8) & 0xFF)

    def emit_wait_idle():
        """Poll $DF1C until state==idle (bits 5,4,0 all zero)."""
        pos = len(code)
        emit_lda_status()
        emit(0x29, MASK_STATE_BUSY)  # AND #$31
        emit(0xD0, 0)  # BNE back -- placeholder
        code[-1] = (pos - len(code)) & 0xFF

    def emit_send_cmd(target, cmd):
        """Write target + cmd to $DF1D, then push command."""
        emit_write_cmd_data(target)
        emit_write_cmd_data(cmd)
        emit_sta_control(CTL_PUSH_CMD)

    def emit_wait_not_busy():
        """Poll $DF1C until CMD_BUSY (bit0) clears."""
        pos = len(code)
        emit_lda_status()
        emit(0x29, BIT_CMD_BUSY)  # AND #$01
        emit(0xD0, 0)  # BNE back -- placeholder
        code[-1] = (pos - len(code)) & 0xFF

    def emit_check_error(error_bit):
        """Check ERROR (bit3); if set, flag it, CLR_ERR, JMP to ack.

        Returns the position of the JMP target bytes (to patch later).
        The BEQ skips 15 bytes: LDA(3) + ORA(2) + STA(3) + LDA+STA(5) + JMP(3) = 16?
        Let's count carefully and use a forward JMP instead of BEQ for safety.
        """
        emit_lda_status()
        emit(0x29, BIT_ERROR)  # AND #$08
        # BEQ skip_error -- we'll compute the exact skip distance
        beq_pos = len(code)
        emit(0xF0, 0x00)  # placeholder

        # Error path: set flag, clear error, JMP to ack
        err_lo, err_hi = abs_addr(OFF_ERROR_FLAGS)
        emit(0xAD, err_lo, err_hi)      # LDA error_flags    (3 bytes)
        emit(0x09, error_bit)            # ORA #bit           (2 bytes)
        emit(0x8D, err_lo, err_hi)      # STA error_flags    (3 bytes)
        emit_sta_control(CTL_CLR_ERR)   # LDA #$08 / STA ctl (5 bytes)
        jmp_pos = len(code)
        emit(0x4C, 0x00, 0x00)          # JMP ack            (3 bytes)

        # Patch BEQ to skip over the error block
        code[beq_pos + 1] = (len(code) - (beq_pos + 2)) & 0xFF
        return jmp_pos

    def emit_drain_resp():
        """Drain all remaining response data bytes, acknowledging each."""
        pos = len(code)
        emit_lda_status()
        emit(0x29, BIT_DATA_AV)
        # BEQ done -- skip: LDA $DF1E (3) + LDA+STA ctl (5) + JMP (3) = 11
        emit(0xF0, 0x0B)
        emit(0xAD, UCI_RESP & 0xFF, (UCI_RESP >> 8) & 0xFF)
        emit_sta_control(CTL_NEXT_DATA)
        a = CODE_ADDR + pos
        emit(0x4C, a & 0xFF, (a >> 8) & 0xFF)

    def emit_drain_status():
        """Drain all status string bytes, acknowledging after each read."""
        pos = len(code)
        emit_lda_status()
        emit(0x29, BIT_STAT_AV)
        # BEQ done -- skip over: LDA $DF1F (3) + LDA+STA ctl (5) + JMP (3) = 11
        emit(0xF0, 0x0B)
        emit(0xAD, UCI_STAT & 0xFF, (UCI_STAT >> 8) & 0xFF)  # read status byte
        emit_sta_control(CTL_NEXT_DATA)  # acknowledge
        a = CODE_ADDR + pos
        emit(0x4C, a & 0xFF, (a >> 8) & 0xFF)  # loop back

    def emit_acknowledge():
        """Wait for UCI to return to idle after draining data/status.

        drain_resp and drain_status include NEXT_DATA acknowledgment
        with each byte read. After they finish, UCI is typically idle.
        Just wait for idle confirmation; no extra NEXT_DATA needed
        (writing NEXT_DATA to an already-idle UCI can cause problems).
        """
        emit_wait_idle()

    def patch_jmp(jmp_pos):
        """Patch a 3-byte JMP placeholder to jump to current position."""
        a = CODE_ADDR + len(code)
        code[jmp_pos + 1] = a & 0xFF
        code[jmp_pos + 2] = (a >> 8) & 0xFF

    # === Code generation starts here ===

    # --- Clear result area ---
    emit(0xA9, 0x00)  # LDA #$00
    for off in [OFF_UCI_ID, OFF_IFACE_COUNT, OFF_ERROR_FLAGS,
                OFF_IDENT_LEN, OFF_IP_LEN, OFF_PROGRESS, OFF_SENTINEL]:
        emit_sta(off)

    # --- Step 0: Abort any in-progress UCI command ---
    emit_sta_control(CTL_ABORT)
    # Brief delay loop for abort to take effect
    emit(0xA2, 0x20)  # LDX #$20
    delay_pos = len(code)
    emit(0xCA)         # DEX
    emit(0xD0, 0xFD)   # BNE -3

    # --- Step 1: Read UCI ID byte ---
    emit_progress(0x01)
    emit(0xAD, UCI_ID & 0xFF, (UCI_ID >> 8) & 0xFF)  # LDA $DF1D
    emit_sta(OFF_UCI_ID)

    # --- Step 2: IDENTIFY (target=0x03, cmd=0x01) ---
    emit_progress(0x02)
    emit_wait_idle()
    emit_progress(0x03)
    emit_send_cmd(TARGET_NETWORK, CMD_IDENTIFY)
    emit_wait_not_busy()
    emit_progress(0x04)
    jmp_ident_err = emit_check_error(0x01)

    # Read identify response string using Y as index
    emit_progress(0x05)
    emit(0xA0, 0x00)  # LDY #$00
    read_ident_loop = len(code)
    emit_lda_status()
    emit(0x29, BIT_DATA_AV)
    # BEQ done_ident_read: skip 11 bytes (3+3+1+2+2) of loop body
    emit(0xF0, 0x0B)
    emit(0xAD, UCI_RESP & 0xFF, (UCI_RESP >> 8) & 0xFF)  # LDA $DF1E
    ident_lo, ident_hi = abs_addr(OFF_IDENT_STR)
    emit(0x99, ident_lo, ident_hi)  # STA abs,Y
    emit(0xC8)         # INY
    emit(0xC0, 0x3F)   # CPY #63
    offset_back = read_ident_loop - (len(code) + 2)
    emit(0x90, offset_back & 0xFF)  # BCC read_ident_loop
    # done_ident_read:
    ident_len_lo, ident_len_hi = abs_addr(OFF_IDENT_LEN)
    emit(0x8C, ident_len_lo, ident_len_hi)  # STY

    emit_progress(0x06)
    emit_drain_status()
    patch_jmp(jmp_ident_err)
    emit_progress(0x07)
    emit_acknowledge()

    # --- Step 3: GET_INTERFACE_COUNT ---
    emit_progress(0x08)
    # (acknowledge above already waits for idle)
    emit_send_cmd(TARGET_NETWORK, CMD_GET_IFACE_COUNT)
    emit_wait_not_busy()
    jmp_iface_err = emit_check_error(0x02)

    # Read one response byte
    emit_lda_status()
    emit(0x29, BIT_DATA_AV)
    emit(0xF0, 0x06)  # BEQ +6 (no data)
    emit(0xAD, UCI_RESP & 0xFF, (UCI_RESP >> 8) & 0xFF)
    iface_lo, iface_hi = abs_addr(OFF_IFACE_COUNT)
    emit(0x8D, iface_lo, iface_hi)

    emit_drain_resp()
    emit_drain_status()
    patch_jmp(jmp_iface_err)
    emit_progress(0x09)
    emit_acknowledge()

    # --- Step 4: GET_IP_ADDRESS (returns 12 raw bytes) ---
    emit_progress(0x0A)
    # (acknowledge above already waits for idle)
    # Send target + cmd + interface index (0 = first interface)
    emit_write_cmd_data(TARGET_NETWORK)
    emit_write_cmd_data(CMD_GET_IP_ADDRESS)
    emit_write_cmd_data(0x00)  # interface index 0
    emit_sta_control(CTL_PUSH_CMD)
    emit_wait_not_busy()
    jmp_ip_err = emit_check_error(0x04)

    # Read response bytes using Y as index
    emit(0xA0, 0x00)  # LDY #$00
    read_ip_loop = len(code)
    emit_lda_status()
    emit(0x29, BIT_DATA_AV)
    # BEQ done_ip_read: skip 11 bytes (3+3+1+2+2) of loop body
    emit(0xF0, 0x0B)
    emit(0xAD, UCI_RESP & 0xFF, (UCI_RESP >> 8) & 0xFF)
    ip_lo, ip_hi = abs_addr(OFF_IP_DATA)
    emit(0x99, ip_lo, ip_hi)  # STA abs,Y
    emit(0xC8)         # INY
    emit(0xC0, 0x10)   # CPY #16 (safety cap, expect 12)
    offset_back = read_ip_loop - (len(code) + 2)
    emit(0x90, offset_back & 0xFF)  # BCC read_ip_loop
    # done_ip_read:
    ip_len_lo, ip_len_hi = abs_addr(OFF_IP_LEN)
    emit(0x8C, ip_len_lo, ip_len_hi)  # STY

    emit_progress(0x0B)
    emit_drain_status()
    patch_jmp(jmp_ip_err)
    emit_progress(0x0C)
    emit_acknowledge()
    emit_progress(0x0D)

    # --- Write sentinel ---
    emit(0xA9, SENTINEL_VALUE)
    emit_sta(OFF_SENTINEL)

    # Infinite loop
    loop_addr = CODE_ADDR + len(code)
    emit(0x4C, loop_addr & 0xFF, (loop_addr >> 8) & 0xFF)

    return struct.pack("<H", CODE_ADDR) + bytes(code)


def _build_basic_sys(addr: int) -> bytes:
    """Build a minimal BASIC PRG: 10 SYS<addr>.

    Returns bytes with the 2-byte load address ($0801) prepended.
    """
    addr_str = str(addr).encode("ascii")
    # BASIC line: next-ptr(2) + line-number(2) + SYS-token + addr-digits + 0x00
    line_body = bytes([0x9E]) + addr_str + bytes([0x00])
    # Next-line pointer = $0801 + 4 (ptrs) + len(line_body)
    next_ptr = 0x0801 + 4 + len(line_body)
    line = struct.pack("<HH", next_ptr, 10) + line_body
    # End-of-program marker (two zero bytes)
    prg = struct.pack("<H", 0x0801) + line + bytes([0x00, 0x00])
    return prg


def _format_ipv4(data: bytes) -> str:
    """Format 4 raw bytes as dotted-decimal IPv4."""
    if len(data) < 4:
        return "(incomplete)"
    return f"{data[0]}.{data[1]}.{data[2]}.{data[3]}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Probe UCI networking on a U64E device via 6502 code injection."
    )
    parser.add_argument("--host", default=None,
                        help="U64 host/IP (default: $U64_HOST or 192.168.1.81)")
    parser.add_argument("--password", default=None,
                        help="Optional API password (default: $U64_PASSWORD)")
    parser.add_argument("--timeout", type=float, default=10.0,
                        help="HTTP request timeout in seconds (default: 10)")
    args = parser.parse_args()

    host = args.host or os.environ.get("U64_HOST", "192.168.1.81")
    password = args.password or os.environ.get("U64_PASSWORD")

    print(f"UCI Network Probe -- target: {host}")
    print("=" * 60)

    # Acquire device lock for cross-process safety
    lock = DeviceLock(host)
    if not lock.acquire(timeout=60.0):
        print("ERROR: Could not acquire device lock (another process holds it)")
        return 1

    try:
        client = Ultimate64Client(host=host, password=password, timeout=args.timeout)
        transport = Ultimate64Transport(host=host, password=password,
                                        timeout=args.timeout, client=client)

        # Save config to flash so UCI enablement persists across resets
        print("Saving config to flash...")
        req = urllib.request.Request(
            f"http://{host}/v1/configs:save_to_flash", method='PUT')
        urllib.request.urlopen(req, timeout=args.timeout)
        time.sleep(0.5)

        # Reset machine so UCI I/O registers ($DF1C-$DF1F) become active
        print("Resetting machine...")
        req = urllib.request.Request(
            f"http://{host}/v1/machine:reset", method='PUT')
        urllib.request.urlopen(req, timeout=args.timeout)
        time.sleep(3)

        # Verify UCI is present before injecting code
        uci_check = transport.read_memory(UCI_ID, 1)
        if uci_check[0] != UCI_ID_EXPECTED:
            print(f"WARNING: $DF1D = 0x{uci_check[0]:02X} after reset "
                  f"(expected 0x{UCI_ID_EXPECTED:02X})")

        # Clear result area via DMA before injecting code
        # Split into two writes to stay within U64 firmware 128-byte PUT limit
        transport.write_memory(RESULT_BASE, bytes(64))
        transport.write_memory(RESULT_BASE + 64, bytes(OFF_SENTINEL + 1 - 64))

        # Build and inject the probe routine via DMA
        prg_data = _build_probe_prg()
        code_bytes = prg_data[2:]  # strip 2-byte load address
        print(f"Injecting UCI probe routine ({len(code_bytes)} bytes "
              f"at ${CODE_ADDR:04X})...")

        # Write code to $C000 via DMA (chunked for firmware limit)
        CHUNK = 64
        for i in range(0, len(code_bytes), CHUNK):
            transport.write_memory(CODE_ADDR + i, code_bytes[i:i + CHUNK])

        # Execute via keyboard buffer instead of run_prg (which resets
        # the machine and wipes the DMA-written code at $C000).
        # Write "SYS49152\r" into the C64 keyboard buffer at $0277
        # and set buffer length at $00C6.
        sys_cmd = b"SYS49152\r"
        transport.write_memory(0x0277, sys_cmd)
        transport.write_memory(0x00C6, bytes([len(sys_cmd)]))

        # Poll for sentinel with timeout
        print("Waiting for probe to complete...")
        deadline = time.time() + 10.0
        sentinel = 0
        while time.time() < deadline:
            time.sleep(0.3)
            data = transport.read_memory(RESULT_BASE + OFF_SENTINEL, 1)
            sentinel = data[0]
            if sentinel == SENTINEL_VALUE:
                break
        else:
            if sentinel != SENTINEL_VALUE:
                print(f"TIMEOUT: Sentinel not set (got 0x{sentinel:02X}, "
                      f"expected 0x{SENTINEL_VALUE:02X})")
                _dump_results(transport)
                print()
                print("VERDICT: UCI networking is NOT AVAILABLE (probe timed out)")
                return 1

        _dump_results(transport)

    finally:
        lock.release()

    return 0


def _dump_results(transport: Ultimate64Transport) -> None:
    """Read and display probe results from C64 memory."""
    result = transport.read_memory(RESULT_BASE, OFF_SENTINEL + 1)

    uci_id = result[OFF_UCI_ID]
    iface_count = result[OFF_IFACE_COUNT]
    error_flags = result[OFF_ERROR_FLAGS]
    ident_len = result[OFF_IDENT_LEN]
    ident_bytes = result[OFF_IDENT_STR:OFF_IDENT_STR + min(ident_len, 63)]
    ip_len = result[OFF_IP_LEN]
    ip_data = result[OFF_IP_DATA:OFF_IP_DATA + min(ip_len, 16)]
    progress = result[OFF_PROGRESS]
    sentinel = result[OFF_SENTINEL]

    uci_snap = result[OFF_UCI_SNAP]

    print()
    print(f"Progress step    : 0x{progress:02X}")
    print(f"UCI status snap  : 0x{uci_snap:02X} "
          f"(DATA_AV={bool(uci_snap&0x80)} STAT_AV={bool(uci_snap&0x40)} "
          f"STATE={(uci_snap>>4)&3} ERR={bool(uci_snap&0x08)} "
          f"BUSY={bool(uci_snap&0x01)})")
    print(f"UCI ID byte      : 0x{uci_id:02X} "
          f"({'OK -- UCI present' if uci_id == UCI_ID_EXPECTED else 'UNEXPECTED'})")

    has_error = False

    # Identify string
    if error_flags & 0x01:
        print("Identify         : ERROR (UCI returned error)")
        has_error = True
    elif ident_len > 0:
        try:
            ident_str = bytes(ident_bytes).decode("ascii", errors="replace")
        except Exception:
            ident_str = repr(bytes(ident_bytes))
        print(f"Identify         : {ident_str}")
    else:
        print("Identify         : (empty response)")

    # Interface count
    if error_flags & 0x02:
        print("Interface count  : ERROR (UCI returned error)")
        has_error = True
    else:
        print(f"Interface count  : {iface_count}")

    # IP address (12 raw bytes: IP + Netmask + Gateway)
    if error_flags & 0x04:
        print("IP address       : ERROR (UCI returned error)")
        has_error = True
    elif ip_len >= 12:
        print(f"IP address       : {_format_ipv4(ip_data[0:4])}")
        print(f"Netmask          : {_format_ipv4(ip_data[4:8])}")
        print(f"Gateway          : {_format_ipv4(ip_data[8:12])}")
    elif ip_len > 0:
        print(f"IP response      : {ip_len} bytes (expected 12): "
              f"{bytes(ip_data).hex()}")
    else:
        print("IP address       : (empty response)")

    print(f"Error flags      : 0x{error_flags:02X}")
    print(f"Sentinel         : 0x{sentinel:02X} "
          f"({'OK' if sentinel == SENTINEL_VALUE else 'MISSING'})")

    print()
    uci_present = (uci_id == UCI_ID_EXPECTED)
    net_ok = not has_error and ip_len >= 12

    if uci_present and net_ok:
        print("VERDICT: UCI networking is AVAILABLE")
    elif uci_present and not net_ok:
        print(f"VERDICT: UCI is present (ID=0x{uci_id:02X}) "
              "but networking query failed")
    else:
        print(f"VERDICT: UCI networking is NOT AVAILABLE "
              f"(ID byte=0x{uci_id:02X}, expected 0x{UCI_ID_EXPECTED:02X})")


if __name__ == "__main__":
    sys.exit(main())
