#!/usr/bin/env python3
"""Validate ICMP ping: C64 -> tap-c64 -> host NAT -> 1.1.1.1 -> reply.

Launches VICE with CS8900a ethernet emulation (TFE mode) on a TAP
interface, configures the chip, performs ARP resolution for the gateway,
sends an ICMP echo request to 1.1.1.1, and verifies the echo reply.

Prerequisites:
    sudo ./scripts/setup-tap-networking.sh

Usage:
    python3 scripts/validate_ping.py

Key implementation notes:
- Uses TFE mode (not RR-Net) for standard CS8900a register layout at $DE00.
- Combined TX+RX routines keep the CPU running between send and receive,
  because VICE's CS8900a emulation only processes TAP frames while the
  CPU is executing (binary monitor pauses halt I/O processing).
- SEI/CLI bracket the TX+RX code to prevent KERNAL IRQ from corrupting
  zero-page counters used by the RX polling loop.
- Result addresses ($C0E0/$C0F0) are placed past the generated code to
  avoid overwriting the running routine.
"""

from __future__ import annotations

import os
import struct
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from c64_test_harness.backends.vice_binary import BinaryViceTransport
from c64_test_harness.backends.vice_lifecycle import ViceConfig, ViceProcess
from c64_test_harness.backends.vice_manager import PortAllocator
from c64_test_harness.execute import load_code
from c64_test_harness.memory import read_bytes, write_bytes

# ---------------------------------------------------------------------------
# Memory layout — result/meta addresses MUST NOT overlap generated code
# (combined TX+RX routines can reach ~185 bytes starting at CODE).
# ---------------------------------------------------------------------------
CODE = 0xC000  # 6502 routines (up to ~200 bytes)
RX_META = 0xC0E0  # RxStatus(2) + RxLength(2)
RESULT = 0xC0F0  # 1-byte result flag (0x01=ok, 0xFF=timeout)
FRAME_BUF = 0xC100  # TX frame buffer (up to 128 bytes)
RX_BUF = 0xC300  # RX frame buffer (up to 160 bytes)

# ---------------------------------------------------------------------------
# Network config
# ---------------------------------------------------------------------------
C64_MAC = b"\x02\x64\x65\x76\x00\x02"  # locally-administered
C64_IP = b"\x0A\x00\x41\x02"  # 10.0.65.2
GW_IP = b"\x0A\x00\x41\x01"  # 10.0.65.1
TARGET_IP = b"\x01\x01\x01\x01"  # 1.1.1.1
BCAST_MAC = b"\xFF" * 6
TAP_IFACE = "tap-c64"


# ---------------------------------------------------------------------------
# Mini 6502 assembler (branch fixups only)
# ---------------------------------------------------------------------------
class Asm:
    """Tiny 6502 assembler with automatic branch offset calculation."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.labels: dict[str, int] = {}
        self._fix: list[tuple[int, str]] = []

    @property
    def pos(self) -> int:
        return len(self.buf)

    def label(self, name: str) -> None:
        self.labels[name] = self.pos

    def emit(self, *data: int) -> None:
        self.buf.extend(data)

    def branch(self, opcode: int, target: str) -> None:
        self.buf.append(opcode)
        self._fix.append((self.pos, target))
        self.buf.append(0)

    def build(self) -> bytes:
        for fix_pos, label in self._fix:
            target = self.labels[label]
            disp = target - (fix_pos + 1)
            if not (-128 <= disp <= 127):
                raise ValueError(f"Branch to '{label}' out of range: {disp}")
            self.buf[fix_pos] = disp & 0xFF
        return bytes(self.buf)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _inet_cksum(data: bytes) -> int:
    """Internet checksum (RFC 1071)."""
    if len(data) % 2:
        data += b"\x00"
    s = sum((data[i] << 8) | data[i + 1] for i in range(0, len(data), 2))
    while s > 0xFFFF:
        s = (s & 0xFFFF) + (s >> 16)
    return (~s) & 0xFFFF


def _jsr(t: BinaryViceTransport, addr: int, timeout: float = 10.0) -> None:
    """Execute a subroutine at *addr* via trampoline + checkpoint."""
    trampoline = bytes([
        0x20, addr & 0xFF, (addr >> 8) & 0xFF,  # JSR addr
        0xEA, 0xEA,  # NOP; NOP  <- breakpoint
    ])
    t.write_memory(0x0334, trampoline)
    bp = t.set_checkpoint(0x0337)
    try:
        t.set_registers({"PC": 0x0334})
        t.resume()
        t.wait_for_stopped(timeout=timeout)
    finally:
        t.delete_checkpoint(bp)


def pp_write(t: BinaryViceTransport, pp_addr: int, val: int) -> None:
    """Write a 16-bit value to a CS8900a PacketPage register."""
    code = bytes([
        0xA9, pp_addr & 0xFF, 0x8D, 0x0A, 0xDE,
        0xA9, (pp_addr >> 8) & 0xFF, 0x8D, 0x0B, 0xDE,
        0xA9, val & 0xFF, 0x8D, 0x0C, 0xDE,
        0xA9, (val >> 8) & 0xFF, 0x8D, 0x0D, 0xDE,
        0x60,
    ])
    load_code(t, CODE, code)
    _jsr(t, CODE)


def pp_read(t: BinaryViceTransport, pp_addr: int) -> int:
    """Read a 16-bit CS8900a PacketPage register."""
    code = bytes([
        0xA9, pp_addr & 0xFF, 0x8D, 0x0A, 0xDE,
        0xA9, (pp_addr >> 8) & 0xFF, 0x8D, 0x0B, 0xDE,
        0xAD, 0x0C, 0xDE, 0x8D, 0x81, 0xC0,
        0xAD, 0x0D, 0xDE, 0x8D, 0x82, 0xC0,
        0x60,
    ])
    load_code(t, CODE, code)
    _jsr(t, CODE)
    r = read_bytes(t, 0xC081, 2)
    return r[0] | (r[1] << 8)


# ---------------------------------------------------------------------------
# 6502 code generators
# ---------------------------------------------------------------------------

def _make_tx_then_rx(frame_len: int, rx_words: int = 80) -> bytes:
    """6502 routine: TX from FRAME_BUF then immediately RX into RX_BUF.

    SEI at entry prevents KERNAL IRQ from corrupting ZP counters.
    """
    a = Asm()
    a.emit(0x78)  # SEI
    # -- TX setup --
    a.emit(0xA9, 0xC0, 0x8D, 0x04, 0xDE)  # TxCMD = 0x00C0
    a.emit(0xA9, 0x00, 0x8D, 0x05, 0xDE)
    a.emit(0xA9, frame_len & 0xFF, 0x8D, 0x06, 0xDE)  # TxLen
    a.emit(0xA9, (frame_len >> 8) & 0xFF, 0x8D, 0x07, 0xDE)
    # Wait Rdy4TxNOW (PP 0x0138 bit 8)
    a.emit(0xA9, 0x38, 0x8D, 0x0A, 0xDE)
    a.emit(0xA9, 0x01, 0x8D, 0x0B, 0xDE)
    a.label("txw")
    a.emit(0xAD, 0x0D, 0xDE, 0x29, 0x01)
    a.branch(0xF0, "txw")  # BEQ txw
    # Write frame data via ZP pointer
    a.emit(0xA9, FRAME_BUF & 0xFF, 0x85, 0xFB)
    a.emit(0xA9, (FRAME_BUF >> 8) & 0xFF, 0x85, 0xFC)
    a.emit(0xA0, 0x00)
    a.label("txlp")
    a.emit(0xB1, 0xFB, 0x8D, 0x00, 0xDE, 0xC8)  # lo byte
    a.emit(0xB1, 0xFB, 0x8D, 0x01, 0xDE, 0xC8)  # hi byte
    a.emit(0xC0, frame_len & 0xFF)
    a.branch(0xD0, "txlp")

    # -- RX poll (CPU stays running — no binary-monitor pause) --
    a.emit(0xA9, 0x24, 0x8D, 0x0A, 0xDE)  # PPTR = 0x0124 (RxEvent)
    a.emit(0xA9, 0x01, 0x8D, 0x0B, 0xDE)
    a.emit(0xA9, 0xFF, 0x85, 0xFD)  # inner counter
    a.emit(0xA9, 0xFF, 0x85, 0xFE)  # middle counter
    a.emit(0xA9, 0x10, 0x85, 0xFF)  # outer counter
    a.label("rxp")
    a.emit(0xAD, 0x0D, 0xDE, 0x29, 0x01)
    a.branch(0xD0, "rxg")
    a.emit(0xC6, 0xFD)
    a.branch(0xD0, "rxp")
    a.emit(0xA9, 0xFF, 0x85, 0xFD)
    a.emit(0xC6, 0xFE)
    a.branch(0xD0, "rxp")
    a.emit(0xA9, 0xFF, 0x85, 0xFE)
    a.emit(0xC6, 0xFF)
    a.branch(0xD0, "rxp")
    # Timeout
    a.emit(0xA9, 0xFF, 0x8D, RESULT & 0xFF, (RESULT >> 8) & 0xFF,
           0x58, 0x60)  # CLI; RTS

    a.label("rxg")
    # RxStatus + RxLength -> RX_META
    a.emit(0xAD, 0x00, 0xDE, 0x8D, RX_META & 0xFF, (RX_META >> 8) & 0xFF)
    a.emit(0xAD, 0x01, 0xDE, 0x8D, (RX_META + 1) & 0xFF,
           ((RX_META + 1) >> 8) & 0xFF)
    a.emit(0xAD, 0x00, 0xDE, 0x8D, (RX_META + 2) & 0xFF,
           ((RX_META + 2) >> 8) & 0xFF)
    a.emit(0xAD, 0x01, 0xDE, 0x8D, (RX_META + 3) & 0xFF,
           ((RX_META + 3) >> 8) & 0xFF)
    # Read frame data
    a.emit(0xA9, RX_BUF & 0xFF, 0x85, 0xFB)
    a.emit(0xA9, (RX_BUF >> 8) & 0xFF, 0x85, 0xFC)
    a.emit(0xA0, 0x00)
    a.emit(0xA2, rx_words & 0xFF)
    a.label("rxrd")
    a.emit(0xAD, 0x00, 0xDE, 0x91, 0xFB, 0xC8)
    a.emit(0xAD, 0x01, 0xDE, 0x91, 0xFB, 0xC8)
    a.emit(0xCA)
    a.branch(0xD0, "rxrd")
    # Success
    a.emit(0xA9, 0x01, 0x8D, RESULT & 0xFF, (RESULT >> 8) & 0xFF,
           0x58, 0x60)  # CLI; RTS
    return a.build()


def _make_rx_only(rx_words: int = 80) -> bytes:
    """6502 routine: poll for next RX frame and read into RX_BUF."""
    a = Asm()
    a.emit(0x78)  # SEI
    a.emit(0xA9, 0x24, 0x8D, 0x0A, 0xDE)
    a.emit(0xA9, 0x01, 0x8D, 0x0B, 0xDE)
    a.emit(0xA9, 0xFF, 0x85, 0xFD, 0x85, 0xFE)
    a.emit(0xA9, 0x10, 0x85, 0xFF)
    a.label("p")
    a.emit(0xAD, 0x0D, 0xDE, 0x29, 0x01)
    a.branch(0xD0, "g")
    a.emit(0xC6, 0xFD)
    a.branch(0xD0, "p")
    a.emit(0xA9, 0xFF, 0x85, 0xFD)
    a.emit(0xC6, 0xFE)
    a.branch(0xD0, "p")
    a.emit(0xA9, 0xFF, 0x85, 0xFE)
    a.emit(0xC6, 0xFF)
    a.branch(0xD0, "p")
    a.emit(0xA9, 0xFF, 0x8D, RESULT & 0xFF, (RESULT >> 8) & 0xFF,
           0x58, 0x60)
    a.label("g")
    a.emit(0xAD, 0x00, 0xDE, 0x8D, RX_META & 0xFF, (RX_META >> 8) & 0xFF)
    a.emit(0xAD, 0x01, 0xDE, 0x8D, (RX_META + 1) & 0xFF,
           ((RX_META + 1) >> 8) & 0xFF)
    a.emit(0xAD, 0x00, 0xDE, 0x8D, (RX_META + 2) & 0xFF,
           ((RX_META + 2) >> 8) & 0xFF)
    a.emit(0xAD, 0x01, 0xDE, 0x8D, (RX_META + 3) & 0xFF,
           ((RX_META + 3) >> 8) & 0xFF)
    a.emit(0xA9, RX_BUF & 0xFF, 0x85, 0xFB)
    a.emit(0xA9, (RX_BUF >> 8) & 0xFF, 0x85, 0xFC)
    a.emit(0xA0, 0x00, 0xA2, rx_words & 0xFF)
    a.label("r")
    a.emit(0xAD, 0x00, 0xDE, 0x91, 0xFB, 0xC8)
    a.emit(0xAD, 0x01, 0xDE, 0x91, 0xFB, 0xC8)
    a.emit(0xCA)
    a.branch(0xD0, "r")
    a.emit(0xA9, 0x01, 0x8D, RESULT & 0xFF, (RESULT >> 8) & 0xFF,
           0x58, 0x60)
    return a.build()


# ---------------------------------------------------------------------------
# High-level TX/RX
# ---------------------------------------------------------------------------

def _read_rx_result(t: BinaryViceTransport) -> bytes | None:
    """Read frame from RX_BUF after a successful RX routine."""
    flag = read_bytes(t, RESULT, 1)[0]
    if flag != 0x01:
        return None
    meta = read_bytes(t, RX_META, 4)
    rx_len = meta[2] | (meta[3] << 8)
    if rx_len == 0 or rx_len > 1518:
        rx_len = 160
    return bytes(read_bytes(t, RX_BUF, min(rx_len, 160)))


def tx_rx(t: BinaryViceTransport, frame: bytes, timeout: float = 60.0) -> bytes | None:
    """TX a frame and RX the first response (single CPU-continuous routine)."""
    if len(frame) % 2:
        frame += b"\x00"
    write_bytes(t, FRAME_BUF, frame)
    code = _make_tx_then_rx(len(frame))
    load_code(t, CODE, code)
    write_bytes(t, RESULT, [0x00])
    write_bytes(t, RX_META, [0x00] * 4)
    _jsr(t, CODE, timeout=timeout)
    return _read_rx_result(t)


def rx_only(t: BinaryViceTransport, timeout: float = 30.0) -> bytes | None:
    """RX the next queued frame from the CS8900a."""
    code = _make_rx_only()
    load_code(t, CODE, code)
    write_bytes(t, RESULT, [0x00])
    write_bytes(t, RX_META, [0x00] * 4)
    _jsr(t, CODE, timeout=timeout)
    return _read_rx_result(t)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    if not os.path.isdir(f"/sys/class/net/{TAP_IFACE}"):
        print(f"ERROR: TAP interface '{TAP_IFACE}' not found.")
        print("Run:  sudo ./scripts/setup-tap-networking.sh")
        return 1

    allocator = PortAllocator(port_range_start=6580, port_range_end=6620)
    port = allocator.allocate()
    res = allocator.take_socket(port)
    if res is not None:
        res.close()

    config = ViceConfig(
        port=port, warp=False, sound=False,
        ethernet=True, ethernet_mode="tfe",
        ethernet_interface=TAP_IFACE, ethernet_driver="tuntap",
    )

    with ViceProcess(config) as vice:
        time.sleep(4)
        if vice._proc.poll() is not None:
            print("ERROR: VICE exited early")
            return 1
        t = BinaryViceTransport(port=port)
        try:
            print(f"VICE running on port {port} (TFE mode)")

            # --- Probe CS8900a ---
            chip_id = pp_read(t, 0x0000)
            print(f"CS8900a Product ID: 0x{chip_id:04X}", end="")
            if chip_id != 0x630E:
                print(" - UNEXPECTED")
                return 1
            print(" - OK")

            # --- Set MAC ---
            pp_write(t, 0x0158, C64_MAC[0] | (C64_MAC[1] << 8))
            pp_write(t, 0x015A, C64_MAC[2] | (C64_MAC[3] << 8))
            pp_write(t, 0x015C, C64_MAC[4] | (C64_MAC[5] << 8))
            mac_w = [pp_read(t, a) for a in (0x0158, 0x015A, 0x015C)]
            mac_b = bytes([
                mac_w[0] & 0xFF, mac_w[0] >> 8,
                mac_w[1] & 0xFF, mac_w[1] >> 8,
                mac_w[2] & 0xFF, mac_w[2] >> 8,
            ])
            print(f"MAC: {mac_b.hex(':')}")

            # --- Configure RxCTL + LineCTL ---
            pp_write(t, 0x0104, 0x00D8)  # promiscuous + RxOK
            linectl = pp_read(t, 0x0112)
            pp_write(t, 0x0112, linectl | 0x00C0)  # SerRxON + SerTxON

            # --- ARP ---
            print("\n--- ARP: resolve gateway ---")
            arp = BCAST_MAC + C64_MAC + b"\x08\x06"
            arp += struct.pack("!HHBBH", 1, 0x0800, 6, 4, 1)
            arp += C64_MAC + C64_IP + b"\x00" * 6 + GW_IP

            gw_mac = None
            frame = tx_rx(t, arp, timeout=30)
            for i in range(10):
                if frame is None:
                    print(f"  Frame {i + 1}: RX timeout")
                    break
                etype = frame[12:14].hex() if len(frame) >= 14 else "??"
                print(f"  Frame {i + 1}: {len(frame)} bytes, "
                      f"ethertype=0x{etype}")
                if (len(frame) >= 42 and frame[12:14] == b"\x08\x06"
                        and (frame[20] << 8 | frame[21]) == 2):
                    gw_mac = frame[22:28]
                    print(f"  ARP reply! Gateway MAC = {gw_mac.hex(':')}")
                    break
                frame = rx_only(t, timeout=15)

            if gw_mac is None:
                print("FAIL: no ARP reply received")
                return 1

            # --- ICMP ---
            print(f"\n--- ICMP ping to 1.1.1.1 via "
                  f"{gw_mac.hex(':')} ---")
            payload = bytes(range(32))
            icmp_hdr = struct.pack("!BBHHH", 8, 0, 0, 0xC640, 1) + payload
            icmp_ck = _inet_cksum(icmp_hdr)
            icmp_data = (struct.pack("!BBHHH", 8, 0, icmp_ck, 0xC640, 1)
                         + payload)
            total_len = 20 + len(icmp_data)
            ip_h = struct.pack(
                "!BBHHHBBH4s4s",
                0x45, 0, total_len, 1, 0x4000, 64, 1, 0, C64_IP, TARGET_IP,
            )
            ip_h = struct.pack(
                "!BBHHHBBH4s4s",
                0x45, 0, total_len, 1, 0x4000, 64, 1,
                _inet_cksum(ip_h), C64_IP, TARGET_IP,
            )
            ping = gw_mac + C64_MAC + b"\x08\x00" + ip_h + icmp_data

            frame = tx_rx(t, ping, timeout=60)
            for i in range(15):
                if frame is None:
                    print(f"  Frame {i + 1}: RX timeout")
                    break
                etype = ((frame[12] << 8) | frame[13]
                         if len(frame) >= 14 else 0)
                if (etype == 0x0800 and len(frame) >= 34
                        and frame[23] == 1):  # IPv4 + ICMP
                    ihl = (frame[14] & 0x0F) * 4
                    off = 14 + ihl
                    if len(frame) >= off + 8:
                        icmp_type = frame[off]
                        icmp_id = (frame[off + 4] << 8) | frame[off + 5]
                        icmp_seq = (frame[off + 6] << 8) | frame[off + 7]
                        src = frame[26:30]
                        print(
                            f"  Frame {i + 1}: ICMP type={icmp_type} "
                            f"id=0x{icmp_id:04X} seq={icmp_seq} "
                            f"from={'.'.join(str(b) for b in src)}"
                        )
                        if icmp_type == 0 and icmp_id == 0xC640:
                            print("\n*** SUCCESS: C64 pinged 1.1.1.1 and "
                                  "received a reply! ***")
                            return 0
                else:
                    print(f"  Frame {i + 1}: {len(frame)} bytes, "
                          f"ethertype=0x{etype:04X} (skipping)")
                frame = rx_only(t, timeout=15)

            print("\nFAIL: no ICMP echo reply received")
            return 1

        finally:
            t.close()
            allocator.release(port)

    return 0


if __name__ == "__main__":
    sys.exit(main())
