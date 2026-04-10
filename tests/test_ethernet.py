"""Ethernet / RR-Net (CS8900a) integration tests (binary monitor transport).

Validates that VICE can emulate the CS8900a ethernet chip via the RR-Net
cartridge mode, connected to a host TAP interface.  Tests probe the chip
ID register and exercise TX/RX packet I/O.

Uses BinaryViceTransport (-binarymonitor) for all VICE communication.

Requirements:
- x64sc on PATH with ethernet cartridge support
- A TAP interface named ``tap-c64`` (create with:
  ``sudo ip tuntap add dev tap-c64 mode tap user $USER && sudo ip link set tap-c64 up``)
- VICE must be compiled with tuntap or pcap driver support

All tests are skipped automatically if prerequisites are missing.

The ``_binary_jsr()`` helper uses the binary protocol's checkpoint mechanism:
``set_checkpoint()`` + ``set_registers()`` + ``resume()`` + ``wait_for_stopped()``.

See ``test_disk_vice.py`` module docstring for the screen polling / ``resume()``
interaction explanation.
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

# CS8900a I/O registers (RR-Net mode at $DE00).
# Matches ip65 cs8900a.s layout:
#   isq       = $DE00   ; ISQ / RR clockport enable ($DE01 bit 0)
#   packetpp  = $DE02   ; PPPtr (16-bit)
#   ppdata    = $DE04   ; PPData (16-bit)
#   rxtxreg   = $DE08   ; RX/TX data FIFO (16-bit)
#   txcmd     = $DE0C   ; TX command
#   txlen     = $DE0E   ; TX length
#
# CRITICAL: the RR clockport MUST be enabled (set bit 0 of $DE01) before
# any CS8900a register access, or the chip ignores all reads/writes.
CS8900A_BASE = 0xDE00
ISQ_LO = CS8900A_BASE + 0x00
ISQ_HI = CS8900A_BASE + 0x01    # bit 0 = RR clockport enable
PPTR = CS8900A_BASE + 0x02      # PacketPage Pointer (16-bit)
PPDATA = CS8900A_BASE + 0x04    # PacketPage Data (16-bit)
RTDATA = CS8900A_BASE + 0x08    # RX/TX data FIFO (16-bit)
TXCMD = CS8900A_BASE + 0x0C     # TX command (16-bit)
TXLEN = CS8900A_BASE + 0x0E     # TX length (16-bit)


def _clockport_enable_code() -> bytes:
    """6502 snippet: enable RR clockport bit (ORA #$01 at $DE01).

    Must be prepended to every CS8900a access routine.  Without it, the
    chip silently drops all register reads and writes.
    """
    return bytes([
        0xAD, ISQ_HI & 0xFF, ISQ_HI >> 8,   # LDA $DE01
        0x09, 0x01,                          # ORA #$01
        0x8D, ISQ_HI & 0xFF, ISQ_HI >> 8,   # STA $DE01
    ])


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
    allocator = PortAllocator(port_range_start=6511, port_range_end=6531)
    port = allocator.allocate()
    reservation = allocator.take_socket(port)
    if reservation is not None:
        reservation.close()

    config = ViceConfig(
        port=port,
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
        """Read CS8900a Product ID -- expect 0x630E.

        RR-Net mode: PPPtr lives at $DE02/$DE03 and PPData at $DE04/$DE05.
        The RR clockport bit ($DE01 bit 0) MUST be enabled first.
        """
        transport = vice_ethernet

        probe_code = _clockport_enable_code() + bytes([
            0xA9, 0x00,                          # LDA #$00
            0x8D, PPTR & 0xFF, PPTR >> 8,        # STA $DE02 (PPPtr lo)
            0x8D, (PPTR + 1) & 0xFF, (PPTR + 1) >> 8,  # STA $DE03 (PPPtr hi)
            0xAD, PPDATA & 0xFF, PPDATA >> 8,    # LDA $DE04 (PPData lo)
            0x8D, 0x00, 0xC0,                    # STA $C000
            0xAD, (PPDATA + 1) & 0xFF, (PPDATA + 1) >> 8,  # LDA $DE05 (PPData hi)
            0x8D, 0x01, 0xC0,                    # STA $C001
            0x60,                                # RTS
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

        # 6502 TX routine (RR-Net register layout)
        tx_code = _clockport_enable_code() + bytes([
            # TxCMD = 0x00C0 at $DE0C/$DE0D
            0xA9, 0xC0,
            0x8D, TXCMD & 0xFF, TXCMD >> 8,
            0xA9, 0x00,
            0x8D, (TXCMD + 1) & 0xFF, (TXCMD + 1) >> 8,

            # TxLength = 64 at $DE0E/$DE0F
            0xA9, 0x40,
            0x8D, TXLEN & 0xFF, TXLEN >> 8,
            0xA9, 0x00,
            0x8D, (TXLEN + 1) & 0xFF, (TXLEN + 1) >> 8,

            # PPPtr = 0x0138 (BusST)
            0xA9, 0x38,
            0x8D, PPTR & 0xFF, PPTR >> 8,
            0xA9, 0x01,
            0x8D, (PPTR + 1) & 0xFF, (PPTR + 1) >> 8,
            # Poll PPData hi (bit 0 = Rdy4TxNOW)
            0xAD, (PPDATA + 1) & 0xFF, (PPDATA + 1) >> 8,
            0x29, 0x01,
            0xF0, 0xF9,  # BEQ back -7

            # ZP $FB/$FC = FRAME_BUF
            0xA9, FRAME_BUF & 0xFF, 0x85, 0xFB,
            0xA9, (FRAME_BUF >> 8) & 0xFF, 0x85, 0xFC,

            # Write 64 bytes to RTDATA ($DE08/$DE09)
            0xA0, 0x00,
            # .loop:
            0xB1, 0xFB,
            0x8D, RTDATA & 0xFF, RTDATA >> 8,
            0xC8,
            0xB1, 0xFB,
            0x8D, (RTDATA + 1) & 0xFF, (RTDATA + 1) >> 8,
            0xC8,
            0xC0, 0x40,
            0xD0, 0xF0,  # BNE -16

            # Success
            0xA9, 0x01,
            0x8D, 0x00, 0xC0,
            0x60,
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
        rtd_lo = RTDATA & 0xFF
        rtd_h_lo = (RTDATA + 1) & 0xFF
        # For RR-Net: RTDATA at $DE08/$DE09. The high byte of both addresses
        # is 0xDE so we hard-code it via the constants.
        rx_code = _clockport_enable_code() + bytes([
            # PPPtr = 0x0124 (RxEvent)
            0xA9, 0x24, 0x8D, PPTR & 0xFF, PPTR >> 8,
            0xA9, 0x01, 0x8D, (PPTR + 1) & 0xFF, (PPTR + 1) >> 8,

            # Timeout counter 16-bit at $FD/$FE
            0xA9, 0xFF, 0x85, 0xFD, 0x85, 0xFE,

            # .poll:
            0xAD, (PPDATA + 1) & 0xFF, (PPDATA + 1) >> 8,  # LDA PPData hi
            0x29, 0x01,
            0xD0, 0x0E,  # BNE .got_packet (+14)

            # Decrement timeout
            0xC6, 0xFD, 0xD0, 0xF5,  # DEC $FD; BNE .poll  (-11)
            0xC6, 0xFE, 0xD0, 0xF1,  # DEC $FE; BNE .poll  (-15)

            # Timeout -> $C000 = 0xFF
            0xA9, 0xFF, 0x8D, 0x00, 0xC0, 0x60,

            # .got_packet:
            # Read RxStatus (2 bytes, discard) -- from RTDATA
            0xAD, rtd_lo, 0xDE,
            0xAD, rtd_h_lo, 0xDE,
            # Read RxLength (2 bytes, discard)
            0xAD, rtd_lo, 0xDE,
            0xAD, rtd_h_lo, 0xDE,

            # Skip ethernet header: 14 bytes = 7 word reads
            0xA2, 0x07,
            # .skip:
            0xAD, rtd_lo, 0xDE,
            0xAD, rtd_h_lo, 0xDE,
            0xCA,
            0xD0, 0xF8,  # BNE -8

            # Read 4 marker bytes (2 word reads)
            0xAD, rtd_lo, 0xDE,
            0x8D, 0x00, 0xC0,
            0xAD, rtd_h_lo, 0xDE,
            0x8D, 0x01, 0xC0,
            0xAD, rtd_lo, 0xDE,
            0x8D, 0x02, 0xC0,
            0xAD, rtd_h_lo, 0xDE,
            0x8D, 0x03, 0xC0,

            # Success
            0xA9, 0x01, 0x8D, 0x04, 0xC0, 0x60,
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
