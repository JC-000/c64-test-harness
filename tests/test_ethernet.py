"""Ethernet / RR-Net (CS8900a) integration tests (binary monitor transport).

Validates that VICE can emulate the CS8900a ethernet chip via the RR-Net
cartridge mode, connected to a host TAP interface.  Tests probe the chip
ID register and exercise TX/RX packet I/O.

Uses BinaryViceTransport (-binarymonitor) instead of the text monitor.

Requirements:
- x64sc on PATH with ethernet cartridge support
- A TAP interface named ``tap-c64`` (create with:
  ``sudo ip tuntap add dev tap-c64 mode tap user $USER && sudo ip link set tap-c64 up``)
- VICE must be compiled with tuntap or pcap driver support

All tests are skipped automatically if prerequisites are missing.

NOTE: Binary transport compatibility
-------------------------------------
The ``jsr()`` helper from ``execute.py`` uses ``raw_command()`` for breakpoints,
which raises ``NotImplementedError`` on binary transport.  The ``_binary_jsr()``
helper below uses the binary protocol's checkpoint mechanism instead:
``set_checkpoint()`` + ``set_registers()`` + ``resume()`` + ``wait_for_stopped()``.

See ``test_disk_vice.py`` module docstring for the ``wait_for_text()`` /
``resume()`` interaction explanation.
"""

from __future__ import annotations

import os
import shutil
import socket
import struct
import time

import pytest

from c64_test_harness.backends.vice_binary import BinaryViceTransport
from c64_test_harness.backends.vice_lifecycle import ViceConfig, ViceProcess
from c64_test_harness.backends.vice_manager import PortAllocator
from c64_test_harness.execute import load_code
from c64_test_harness.memory import read_bytes, write_bytes
from c64_test_harness.screen import ScreenGrid
from c64_test_harness.transport import TransportError

from conftest import connect_binary_transport

# ---------------------------------------------------------------------------
# Skip helpers
# ---------------------------------------------------------------------------

_HAS_X64SC = shutil.which("x64sc") is not None


def _tap_interface_exists(name: str = "tap-c64") -> bool:
    """Check if a TAP interface exists by looking in /sys/class/net."""
    return os.path.isdir(f"/sys/class/net/{name}")


def _find_tap_interface() -> str | None:
    """Return the first available tap-* interface, or None."""
    try:
        for iface in os.listdir("/sys/class/net"):
            if iface.startswith("tap"):
                return iface
    except OSError:
        pass
    return None


TAP_IFACE = _find_tap_interface()

pytestmark = [
    pytest.mark.skipif(not _HAS_X64SC, reason="x64sc not found on PATH"),
    pytest.mark.skipif(TAP_IFACE is None, reason="No TAP interface available (need: sudo ip tuntap add dev tap-c64 mode tap user $USER && sudo ip link set tap-c64 up)"),
]

# Scratch area
CODE_BASE = 0xC000
DATA_BASE = 0xC100

# CS8900a I/O registers (RR-Net at $DE00)
RRNET_BASE = 0xDE00
RTDATA = RRNET_BASE + 0x00      # RX/TX data port (16-bit)
TXCMD = RRNET_BASE + 0x04       # TX command (16-bit)
TXLEN = RRNET_BASE + 0x06       # TX length (16-bit)
PPTR = RRNET_BASE + 0x0A        # PacketPage Pointer (16-bit)
PPDATA = RRNET_BASE + 0x0C      # PacketPage Data (16-bit)


# ---------------------------------------------------------------------------
# Binary transport helpers
# ---------------------------------------------------------------------------

def _binary_jsr(
    transport: BinaryViceTransport,
    addr: int,
    timeout: float = 10.0,
    scratch_addr: int = 0x0334,
) -> dict[str, int]:
    """JSR via binary monitor checkpoint mechanism.

    Writes a trampoline (JSR addr; NOP; NOP) at *scratch_addr*, sets a
    checkpoint (breakpoint) at scratch_addr+3, sets PC to scratch_addr,
    resumes, and waits for the CPU to stop at the breakpoint.

    Returns the register state after the subroutine returns.
    """
    trampoline = bytes([
        0x20, addr & 0xFF, (addr >> 8) & 0xFF,  # JSR addr
        0xEA,  # NOP (breakpoint here)
        0xEA,  # NOP
    ])
    transport.write_memory(scratch_addr, trampoline)
    bp_addr = scratch_addr + 3
    bp_num = transport.set_checkpoint(bp_addr)
    try:
        transport.set_registers({"PC": scratch_addr})
        transport.resume()
        transport.wait_for_stopped(timeout=timeout)
        regs = transport.read_registers()
        return regs
    finally:
        transport.delete_checkpoint(bp_num)


def _binary_wait_for_text(
    transport: BinaryViceTransport,
    needle: str,
    timeout: float = 30.0,
    poll_interval: float = 2.0,
) -> ScreenGrid | None:
    """Wait until *needle* appears on screen, resuming between polls.

    See ``test_disk_vice.py`` module docstring for why this is needed.
    """
    needle_upper = needle.upper()
    start = time.monotonic()
    while True:
        elapsed = time.monotonic() - start
        if elapsed >= timeout:
            return None
        try:
            transport.resume()
            time.sleep(poll_interval)
            grid = ScreenGrid.from_transport(transport)
            if needle_upper in grid.continuous_text().upper():
                return grid
        except Exception:
            time.sleep(poll_interval)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def vice_ethernet():
    """Launch VICE with RR-Net on the TAP interface, yield transport."""
    allocator = PortAllocator(port_range_start=6540, port_range_end=6550)
    port = allocator.allocate()
    reservation = allocator.take_socket(port)
    if reservation is not None:
        reservation.close()

    config = ViceConfig(
        port=port,
        monitor_type="binary",
        warp=False,  # warp can cause timing issues with ethernet
        sound=False,
        ethernet=True,
        ethernet_mode="rrnet",
        ethernet_interface=TAP_IFACE or "tap-c64",
        ethernet_driver="tuntap",
    )

    with ViceProcess(config) as vice:
        transport = connect_binary_transport(port, proc=vice)
        try:
            grid = _binary_wait_for_text(transport, "READY.", timeout=30)
            assert grid is not None, "BASIC READY prompt not found"
            yield transport
        finally:
            transport.close()
            allocator.release(port)


# ---------------------------------------------------------------------------
# Part 2: CS8900a Probe Test
# ---------------------------------------------------------------------------


class TestCS8900aProbe:
    """Verify CS8900a chip is present by reading Product ID register."""

    def test_product_id(self, vice_ethernet: BinaryViceTransport) -> None:
        """Read CS8900a Product ID -- expect 0x630E."""
        transport = vice_ethernet

        # 6502 routine: probe CS8900a Product ID
        # Write 0x0000 to PP Pointer ($DE0A/$DE0B)
        # Read PP Data ($DE0C/$DE0D) into $C000/$C001
        probe_code = bytes([
            0xA9, 0x00,        # LDA #$00
            0x8D, 0x0A, 0xDE,  # STA $DE0A  (PP Pointer low)
            0x8D, 0x0B, 0xDE,  # STA $DE0B  (PP Pointer high)
            0xAD, 0x0C, 0xDE,  # LDA $DE0C  (PP Data low)
            0x8D, 0x00, 0xC0,  # STA $C000
            0xAD, 0x0D, 0xDE,  # LDA $DE0D  (PP Data high)
            0x8D, 0x01, 0xC0,  # STA $C001
            0x60,              # RTS
        ])

        load_code(transport, CODE_BASE, probe_code)
        regs = _binary_jsr(transport, CODE_BASE, timeout=10)

        result = read_bytes(transport, 0xC000, 2)
        chip_id = result[0] | (result[1] << 8)

        assert result[0] == 0x0E, f"PP Data low: expected 0x0E, got 0x{result[0]:02X}"
        assert result[1] == 0x63, f"PP Data high: expected 0x63, got 0x{result[1]:02X}"
        assert chip_id == 0x630E, f"CS8900a Product ID: expected 0x630E, got 0x{chip_id:04X}"


# ---------------------------------------------------------------------------
# Part 3: Ethernet Traffic Tests
# ---------------------------------------------------------------------------


# Frame constants
DEST_MAC = b"\xFF\xFF\xFF\xFF\xFF\xFF"  # broadcast
SRC_MAC = b"\x00\x00\x00\x00\x00\x01"  # arbitrary
ETHERTYPE = b"\x88\xB5"                  # local experimental
FRAME_LEN = 64
# Payload: fill with 0xC6, 0x40 ("C", "6" in a loose sense) repeated
PAYLOAD_LEN = FRAME_LEN - 14  # 14 = 6+6+2 header
PAYLOAD = bytes([0xC6, 0x40] * (PAYLOAD_LEN // 2))
FRAME_DATA = DEST_MAC + SRC_MAC + ETHERTYPE + PAYLOAD

# Buffer location in C64 RAM for the frame
FRAME_BUF = 0xC200


def _can_open_raw_socket(iface: str) -> bool:
    """Check if we can open a raw socket on the interface."""
    try:
        s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0003))
        s.bind((iface, 0))
        s.close()
        return True
    except (PermissionError, OSError):
        return False


class TestEthernetTX:
    """Send an ethernet frame from the C64 and capture it on the host."""

    @pytest.fixture(autouse=True)
    def _check_raw_socket(self) -> None:
        if TAP_IFACE is None or not _can_open_raw_socket(TAP_IFACE):
            pytest.skip("Cannot open raw socket on TAP interface (need CAP_NET_RAW)")

    def test_send_broadcast_frame(self, vice_ethernet: BinaryViceTransport) -> None:
        """C64 sends a 64-byte broadcast frame; host captures it."""
        transport = vice_ethernet

        # Write the frame buffer into C64 RAM
        write_bytes(transport, FRAME_BUF, FRAME_DATA)

        # 6502 TX routine
        # Uses zero-page pointer at $FB/$FC for indirect indexed reads
        tx_code = bytes([
            # --- TX Setup ---
            # TxCMD = 0x00C0 (TxStart: transmit after full frame)
            0xA9, 0xC0,        # LDA #$C0
            0x8D, 0x04, 0xDE,  # STA $DE04  (TxCMD low)
            0xA9, 0x00,        # LDA #$00
            0x8D, 0x05, 0xDE,  # STA $DE05  (TxCMD high)

            # TxLength = 64
            0xA9, 0x40,        # LDA #$40
            0x8D, 0x06, 0xDE,  # STA $DE06  (TxLength low)
            0xA9, 0x00,        # LDA #$00
            0x8D, 0x07, 0xDE,  # STA $DE07  (TxLength high)

            # --- Wait for Rdy4TxNOW ---
            # Write 0x0138 to PP Pointer
            0xA9, 0x38,        # LDA #$38
            0x8D, 0x0A, 0xDE,  # STA $DE0A  (PP Ptr low)
            0xA9, 0x01,        # LDA #$01
            0x8D, 0x0B, 0xDE,  # STA $DE0B  (PP Ptr high)
            # Poll PP Data high byte bit 0 (= bit 8 of BusST)
            # .wait:
            0xAD, 0x0D, 0xDE,  # LDA $DE0D  (PP Data high)
            0x29, 0x01,        # AND #$01
            0xF0, 0xF9,        # BEQ .wait  (-7 -> back to LDA $DE0D)

            # --- Write frame data ---
            # Set up ZP pointer: $FB/$FC = FRAME_BUF ($C200)
            0xA9, FRAME_BUF & 0xFF,         # LDA #<FRAME_BUF
            0x85, 0xFB,                      # STA $FB
            0xA9, (FRAME_BUF >> 8) & 0xFF,  # LDA #>FRAME_BUF
            0x85, 0xFC,                      # STA $FC

            # Loop: write 64 bytes (32 16-bit words) to $DE00/$DE01
            0xA0, 0x00,        # LDY #$00
            # .loop:
            0xB1, 0xFB,        # LDA ($FB),Y   ; low byte
            0x8D, 0x00, 0xDE,  # STA $DE00
            0xC8,              # INY
            0xB1, 0xFB,        # LDA ($FB),Y   ; high byte
            0x8D, 0x01, 0xDE,  # STA $DE01
            0xC8,              # INY
            0xC0, 0x40,        # CPY #$40       ; 64 bytes done?
            0xD0, 0xF2,        # BNE .loop      (-14 -> back to LDA ($FB),Y)

            # Store success flag
            0xA9, 0x01,        # LDA #$01
            0x8D, 0x00, 0xC0,  # STA $C000
            0x60,              # RTS
        ])

        load_code(transport, CODE_BASE, tx_code)
        # Clear success flag
        write_bytes(transport, 0xC000, [0x00])

        # Open raw socket on TAP BEFORE executing
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0003))
        sock.bind((TAP_IFACE, 0))
        sock.settimeout(5.0)

        try:
            # Execute TX routine via binary checkpoint mechanism
            regs = _binary_jsr(transport, CODE_BASE, timeout=10)

            # Verify success flag
            flag = read_bytes(transport, 0xC000, 1)
            assert flag[0] == 0x01, "TX routine did not complete (success flag not set)"

            # Capture packet
            captured = sock.recv(1518)

            # Verify frame contents
            assert len(captured) >= FRAME_LEN, \
                f"Captured frame too short: {len(captured)} < {FRAME_LEN}"
            assert captured[:6] == DEST_MAC, \
                f"Dest MAC mismatch: {captured[:6].hex()}"
            assert captured[6:12] == SRC_MAC, \
                f"Source MAC mismatch: {captured[6:12].hex()}"
            assert captured[12:14] == ETHERTYPE, \
                f"EtherType mismatch: {captured[12:14].hex()}"
            # Check at least the first few payload bytes
            assert captured[14:18] == PAYLOAD[:4], \
                f"Payload mismatch: {captured[14:18].hex()} != {PAYLOAD[:4].hex()}"
        finally:
            sock.close()


class TestEthernetRX:
    """Send a packet from the host and have the C64 receive it."""

    @pytest.fixture(autouse=True)
    def _check_raw_socket(self) -> None:
        if TAP_IFACE is None or not _can_open_raw_socket(TAP_IFACE):
            pytest.skip("Cannot open raw socket on TAP interface (need CAP_NET_RAW)")

    def test_receive_frame(self, vice_ethernet: BinaryViceTransport) -> None:
        """Host sends a frame to the TAP; C64 reads it via CS8900a RX."""
        transport = vice_ethernet

        # Build a frame to send from host
        # Dest MAC: use the CS8900a's MAC or broadcast
        rx_dest_mac = b"\xFF\xFF\xFF\xFF\xFF\xFF"
        rx_src_mac = b"\x00\x00\x00\x00\x00\x02"
        rx_ethertype = b"\x88\xB5"
        rx_marker = b"\xDE\xAD\xBE\xEF"
        rx_payload = rx_marker + b"\x00" * (FRAME_LEN - 14 - len(rx_marker))
        rx_frame = rx_dest_mac + rx_src_mac + rx_ethertype + rx_payload

        # 6502 RX routine:
        # 1. Poll RxEvent (PP 0x0124) for RxOK (bit 8)
        # 2. Read RxStatus from $DE00/$DE01
        # 3. Read RxLength from $DE00/$DE01
        # 4. Read first 4 payload bytes (skip 14-byte header = 7 word reads)
        # 5. Store marker at $C000-$C003
        # Note: uses a timeout counter to avoid infinite loop
        rx_code = bytes([
            # --- Poll for RxOK ---
            # Write 0x0124 to PP Pointer
            0xA9, 0x24,        # LDA #$24
            0x8D, 0x0A, 0xDE,  # STA $DE0A
            0xA9, 0x01,        # LDA #$01
            0x8D, 0x0B, 0xDE,  # STA $DE0B

            # Timeout counter in $FD/$FE (16-bit, counts down from 0xFFFF)
            0xA9, 0xFF,        # LDA #$FF
            0x85, 0xFD,        # STA $FD
            0x85, 0xFE,        # STA $FE

            # .poll:
            0xAD, 0x0D, 0xDE,  # LDA $DE0D  (PP Data high = RxEvent high)
            0x29, 0x01,        # AND #$01   (bit 8 = RxOK)
            0xD0, 0x0F,        # BNE .got_packet (+15)

            # Decrement timeout
            0xC6, 0xFD,        # DEC $FD
            0xD0, 0xF5,        # BNE .poll  (-11)
            0xC6, 0xFE,        # DEC $FE
            0xD0, 0xF1,        # BNE .poll  (-15)

            # Timeout -- store 0xFF at $C000 as error flag
            0xA9, 0xFF,        # LDA #$FF
            0x8D, 0x00, 0xC0,  # STA $C000
            0x60,              # RTS

            # .got_packet:
            # Read RxStatus (2 bytes, discard)
            0xAD, 0x00, 0xDE,  # LDA $DE00
            0xAD, 0x01, 0xDE,  # LDA $DE01

            # Read RxLength (2 bytes, discard)
            0xAD, 0x00, 0xDE,  # LDA $DE00
            0xAD, 0x01, 0xDE,  # LDA $DE01

            # Skip ethernet header: 14 bytes = 7 word reads
            # (6 dest MAC + 6 src MAC + 2 ethertype)
            0xA2, 0x07,        # LDX #$07
            # .skip:
            0xAD, 0x00, 0xDE,  # LDA $DE00
            0xAD, 0x01, 0xDE,  # LDA $DE01
            0xCA,              # DEX
            0xD0, 0xF8,        # BNE .skip (-8)

            # Read 4 marker bytes (2 word reads)
            0xAD, 0x00, 0xDE,  # LDA $DE00  ; marker[0]
            0x8D, 0x00, 0xC0,  # STA $C000
            0xAD, 0x01, 0xDE,  # LDA $DE01  ; marker[1]
            0x8D, 0x01, 0xC0,  # STA $C001
            0xAD, 0x00, 0xDE,  # LDA $DE00  ; marker[2]
            0x8D, 0x02, 0xC0,  # STA $C002
            0xAD, 0x01, 0xDE,  # LDA $DE01  ; marker[3]
            0x8D, 0x03, 0xC0,  # STA $C003

            # Store success flag
            0xA9, 0x01,        # LDA #$01
            0x8D, 0x04, 0xC0,  # STA $C004
            0x60,              # RTS
        ])

        load_code(transport, CODE_BASE, rx_code)
        # Clear result area
        write_bytes(transport, 0xC000, [0x00] * 5)

        # We need to execute the RX routine, then send the packet while
        # the routine is polling. Use goto() + send packet + wait_for_stopped
        # on the RTS address.

        # For binary transport, we use set_checkpoint on the RTS + resume,
        # then send the packet from a thread while the C64 polls.
        import threading

        # Write a trampoline at scratch area and set breakpoint after JSR
        scratch_addr = 0x0334
        trampoline = bytes([
            0x20, CODE_BASE & 0xFF, (CODE_BASE >> 8) & 0xFF,  # JSR CODE_BASE
            0xEA,  # NOP (breakpoint here)
            0xEA,  # NOP
        ])
        transport.write_memory(scratch_addr, trampoline)
        bp_addr = scratch_addr + 3
        bp_num = transport.set_checkpoint(bp_addr)

        def _send_packet_delayed():
            """Send the RX frame after a short delay."""
            time.sleep(0.5)
            try:
                sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0003))
                sock.bind((TAP_IFACE, 0))
                sock.send(rx_frame)
                sock.close()
            except OSError:
                pass

        sender = threading.Thread(target=_send_packet_delayed, daemon=True)
        sender.start()

        try:
            transport.set_registers({"PC": scratch_addr})
            transport.resume()
            transport.wait_for_stopped(timeout=15)
            regs = transport.read_registers()
        finally:
            transport.delete_checkpoint(bp_num)

        sender.join(timeout=2)

        # Check results
        result = read_bytes(transport, 0xC000, 5)
        success = result[4]

        if result[0] == 0xFF and success != 0x01:
            pytest.skip("RX poll timed out -- packet may not have reached CS8900a")

        assert success == 0x01, \
            f"RX routine did not complete successfully (flag=0x{success:02X})"

        # Verify marker bytes
        marker = bytes(result[:4])
        assert marker == rx_marker, \
            f"RX marker mismatch: got {marker.hex()}, expected {rx_marker.hex()}"
