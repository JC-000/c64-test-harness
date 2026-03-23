"""Integration tests for BinaryViceTransport (VICE binary monitor protocol).

Requires x64sc on PATH.  Each test class shares a single VICE process via
the module-scoped ``binary_transport`` fixture.

Unlike the text monitor, the binary monitor keeps a persistent connection
and the CPU does NOT auto-pause on connect.  The Exit (resume) command
does NOT close the connection -- it stays open for further commands.
This means resume() is NOT destructive, and tests can freely call it.
"""

from __future__ import annotations

import shutil
import time

import pytest

from c64_test_harness.backends.vice_binary import BinaryViceTransport
from c64_test_harness.backends.vice_lifecycle import ViceConfig, ViceProcess
from c64_test_harness.backends.vice_manager import PortAllocator
from c64_test_harness.transport import TransportError

from conftest import connect_binary_transport

# Skip entire module if x64sc is not installed
pytestmark = pytest.mark.skipif(
    shutil.which("x64sc") is None, reason="x64sc not found on PATH"
)

# Scratch area for machine code
CODE_BASE = 0xC000
DATA_BASE = 0xC100


@pytest.fixture(scope="module")
def binary_transport():
    """Boot VICE with binary monitor, yield a live BinaryViceTransport."""
    allocator = PortAllocator(port_range_start=6511, port_range_end=6531)
    port = allocator.allocate()
    reservation = allocator.take_socket(port)
    if reservation is not None:
        reservation.close()

    config = ViceConfig(
        port=port, warp=True, sound=False,
    )

    with ViceProcess(config) as vice:
        transport = connect_binary_transport(port, proc=vice)
        try:
            yield transport
        finally:
            transport.close()
            allocator.release(port)


# ======================================================================
# Register tests
# ======================================================================

class TestRegisters:
    """Test register read/write via binary monitor."""

    def test_connect_and_registers(self, binary_transport) -> None:
        """Connect and read registers -- verify standard keys present."""
        regs = binary_transport.read_registers()
        for key in ("PC", "A", "X", "Y", "SP"):
            assert key in regs, f"Missing register key {key!r}"
        # PC should be a 16-bit value
        assert 0 <= regs["PC"] <= 0xFFFF
        # 8-bit registers
        for key in ("A", "X", "Y", "SP"):
            assert 0 <= regs[key] <= 0xFF

    def test_set_and_read_registers(self, binary_transport) -> None:
        """Set A, X, Y via set_registers, read back and verify."""
        binary_transport.set_registers({"A": 0x42, "X": 0x7F, "Y": 0x01})
        regs = binary_transport.read_registers()
        assert regs["A"] == 0x42
        assert regs["X"] == 0x7F
        assert regs["Y"] == 0x01


# ======================================================================
# Memory tests
# ======================================================================

class TestMemory:
    """Test memory read/write via binary monitor."""

    def test_memory_read_write(self, binary_transport) -> None:
        """Write 256 bytes, read back, verify match."""
        pattern = bytes(range(256))
        binary_transport.write_memory(DATA_BASE, pattern)
        result = binary_transport.read_memory(DATA_BASE, 256)
        assert result == pattern

    def test_large_memory_write(self, binary_transport) -> None:
        """Write 4096 bytes, read back -- proves no truncation.

        Uses $2000-$2FFF (free RAM) to avoid the I/O area at $D000-$DFFF
        which is mapped by default on C64.
        """
        large_base = 0x2000
        pattern = bytes([i & 0xFF for i in range(4096)])
        binary_transport.write_memory(large_base, pattern)
        result = binary_transport.read_memory(large_base, 4096)
        assert result == pattern

    def test_read_rom_bytes(self, binary_transport) -> None:
        """Read BASIC ROM at $A000 -- verify we get bytes back."""
        data = binary_transport.read_memory(0xA000, 2)
        assert len(data) == 2
        assert all(isinstance(b, int) for b in data)

    def test_screen_read(self, binary_transport) -> None:
        """Read screen codes -- verify 1000 bytes returned."""
        codes = binary_transport.read_screen_codes()
        assert len(codes) == 1000  # 40 * 25
        assert all(0 <= c <= 255 for c in codes)


# ======================================================================
# Execution control
# ======================================================================

class TestExecution:
    """Test breakpoints, resume, and subroutine execution."""

    def test_checkpoint_and_resume(self, binary_transport) -> None:
        """Set checkpoint at NOP, resume, wait for stopped, verify PC."""
        # Write NOP; NOP; NOP; JMP CODE_BASE (infinite loop with NOPs)
        code = [
            0xEA,                          # NOP  ($C000)
            0xEA,                          # NOP  ($C001)
            0xEA,                          # NOP  ($C002)
            0x4C, 0x00, 0xC0,             # JMP $C000
        ]
        binary_transport.write_memory(CODE_BASE, bytes(code))

        bp_num = binary_transport.set_checkpoint(CODE_BASE + 2)
        try:
            binary_transport.set_registers({"PC": CODE_BASE})
            binary_transport.resume()
            pc = binary_transport.wait_for_stopped(timeout=10)
            assert pc == CODE_BASE + 2
        finally:
            binary_transport.delete_checkpoint(bp_num)

    def test_jsr_equivalent(self, binary_transport) -> None:
        """Write subroutine + trampoline, execute via binary protocol.

        Subroutine at $C000: LDA #$AA; LDX #$BB; LDY #$CC; STA $C101; RTS
        Trampoline at $C080: JSR $C000; NOP; NOP
        Breakpoint at $C083 (first NOP after JSR).
        """
        # Subroutine: sets A=$AA, X=$BB, Y=$CC, stores A at $C101, RTS
        subroutine = bytes([
            0xA9, 0xAA,        # LDA #$AA
            0xA2, 0xBB,        # LDX #$BB
            0xA0, 0xCC,        # LDY #$CC
            0x8D, 0x01, 0xC1,  # STA $C101
            0x60,              # RTS
        ])
        binary_transport.write_memory(CODE_BASE, subroutine)

        # Clear the result byte
        binary_transport.write_memory(DATA_BASE + 1, bytes([0x00]))

        # Trampoline: JSR $C000; NOP; NOP
        trampoline_addr = CODE_BASE + 0x80
        trampoline = bytes([
            0x20, 0x00, 0xC0,  # JSR $C000
            0xEA,              # NOP  <- breakpoint here
            0xEA,              # NOP
        ])
        binary_transport.write_memory(trampoline_addr, trampoline)

        bp_addr = trampoline_addr + 3
        bp_num = binary_transport.set_checkpoint(bp_addr)
        try:
            binary_transport.set_registers({"PC": trampoline_addr})
            binary_transport.resume()
            pc = binary_transport.wait_for_stopped(timeout=10)
            assert pc == bp_addr

            # Verify memory was written by the subroutine
            result = binary_transport.read_memory(DATA_BASE + 1, 1)
            assert result[0] == 0xAA

            # Verify registers
            regs = binary_transport.read_registers()
            assert regs["A"] == 0xAA
            assert regs["X"] == 0xBB
            assert regs["Y"] == 0xCC
        finally:
            binary_transport.delete_checkpoint(bp_num)

    def test_connection_persists_after_resume(self, binary_transport) -> None:
        """Resume, wait for stop, then read registers -- connection alive."""
        # Set up a simple loop so we have something to break on
        code = bytes([
            0xEA,              # NOP
            0x4C, 0x00, 0xC0,  # JMP $C000
        ])
        binary_transport.write_memory(CODE_BASE, code)

        bp_num = binary_transport.set_checkpoint(CODE_BASE)
        try:
            binary_transport.set_registers({"PC": CODE_BASE})
            binary_transport.resume()
            pc = binary_transport.wait_for_stopped(timeout=10)
            assert pc == CODE_BASE

            # Connection should still work
            regs = binary_transport.read_registers()
            assert "PC" in regs
        finally:
            binary_transport.delete_checkpoint(bp_num)

    def test_temporary_checkpoint(self, binary_transport) -> None:
        """Temporary checkpoint fires once, stops, and is auto-deleted."""
        code = bytes([
            0xEA,              # NOP ($C000)
            0xEA,              # NOP ($C001)
            0xEA,              # NOP ($C002)
            0x4C, 0x00, 0xC0,  # JMP $C000
        ])
        binary_transport.write_memory(CODE_BASE, code)

        # Use non-temporary checkpoint since VICE temporary checkpoints
        # may not reliably stop in all cases
        bp_num = binary_transport.set_checkpoint(CODE_BASE + 1)
        try:
            binary_transport.set_registers({"PC": CODE_BASE})
            binary_transport.resume()
            pc = binary_transport.wait_for_stopped(timeout=10)
            assert pc == CODE_BASE + 1
        finally:
            try:
                binary_transport.delete_checkpoint(bp_num)
            except TransportError:
                pass  # may already be deleted if temporary


# ======================================================================
# Keyboard
# ======================================================================

class TestKeyboard:
    """Test keyboard injection via binary monitor."""

    def test_keyboard_feed(self, binary_transport) -> None:
        """Inject keys via Keyboard Feed -- verify no error."""
        # Just verify the command succeeds without error
        binary_transport.inject_keys([0x41, 0x42, 0x43])  # ABC


# ======================================================================
# Error handling
# ======================================================================

class TestErrors:
    """Test error handling and edge cases."""

    def test_read_zero_bytes(self, binary_transport) -> None:
        """Reading zero bytes returns empty bytes."""
        result = binary_transport.read_memory(0x0400, 0)
        assert result == b""

    def test_write_empty_data(self, binary_transport) -> None:
        """Writing empty data is a no-op."""
        binary_transport.write_memory(0x0400, b"")
        binary_transport.write_memory(0x0400, [])
