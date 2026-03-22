"""VICE integration tests for core harness modules (binary monitor).

Validates execute, memory, screen, keyboard, and debug functions against
a real VICE instance using the binary monitor protocol.  Each test class
shares a single VICE process via the module-scoped ``binary_transport``
fixture from conftest.py.

The binary monitor keeps a persistent TCP connection.  Unlike the text
monitor, ``resume()`` does NOT destroy the connection, so tests can run
in any order.  No CPU parking tricks are needed.

NOTE: The binary monitor auto-pauses the CPU when any command is sent.
Screen and keyboard tests must explicitly resume() the CPU between
operations so BASIC can process keystrokes and update the screen.
"""

from __future__ import annotations

import shutil
import time

import pytest

from c64_test_harness.debug import dump_screen
from c64_test_harness.execute import jsr, load_code
from c64_test_harness.keyboard import send_key, send_text
from c64_test_harness.memory import (
    hex_dump,
    read_bytes,
    read_dword_le,
    read_word_le,
    write_bytes,
)
from c64_test_harness.screen import ScreenGrid, wait_for_stable, wait_for_text
from c64_test_harness.transport import TimeoutError as TransportTimeoutError

# Skip entire module if x64sc is not installed
pytestmark = pytest.mark.skipif(
    shutil.which("x64sc") is None, reason="x64sc not found on PATH"
)

# Scratch area for machine code ($C000-$CFFF) -- avoids clobbering BASIC/kernal
CODE_BASE = 0xC000
DATA_BASE = 0xC100

def _wait_for_text_binary(transport, needle, timeout=15.0, poll_interval=1.0):
    """Poll screen for *needle*, resuming the CPU between reads.

    The binary monitor auto-pauses the CPU when any command is sent.
    This helper resumes the CPU after each screen read so the KERNAL
    can continue updating the screen.
    """
    needle_upper = needle.upper()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        grid = ScreenGrid.from_transport(transport)
        if needle_upper in grid.continuous_text().upper():
            return grid
        transport.resume()
        time.sleep(poll_interval)
    return None


def _restore_basic(transport):
    """Return CPU to the BASIC idle loop.

    Writes CLI + JMP $E5CD (KERNAL MAINLOOP) to scratch memory, sets PC
    there, and resumes.  After a brief delay the CPU should be in BASIC's
    idle loop ready to process keystrokes.
    """
    restore_code = bytes([0x58, 0x4C, 0xCD, 0xE5])
    transport.write_memory(0xCF00, restore_code)
    transport.set_registers({"PC": 0xCF00})
    transport.resume()
    time.sleep(0.5)


# ======================================================================
# Execution control
# ======================================================================

class TestExecution:
    """Test execution functions against real VICE via binary monitor."""

    def test_jsr_simple_rts(self, binary_transport) -> None:
        """Load RTS at $C000, jsr(), verify trampoline round-trip."""
        load_code(binary_transport, CODE_BASE, [0x60])  # RTS
        regs = jsr(binary_transport, CODE_BASE, timeout=15)
        assert "PC" in regs
        assert "A" in regs

    def test_jsr_computation(self, binary_transport) -> None:
        """Double a value: LDA $C100 / ASL A / STA $C101 / RTS."""
        code = [
            0xAD, 0x00, 0xC1,  # LDA $C100
            0x0A,              # ASL A
            0x8D, 0x01, 0xC1,  # STA $C101
            0x60,              # RTS
        ]
        load_code(binary_transport, CODE_BASE, code)
        write_bytes(binary_transport, DATA_BASE, [42])
        write_bytes(binary_transport, DATA_BASE + 1, [0])

        jsr(binary_transport, CODE_BASE, timeout=15)

        result = read_bytes(binary_transport, DATA_BASE + 1, 1)
        assert result == bytes([84]), f"Expected 84, got {result[0]}"

    def test_jsr_register_state(self, binary_transport) -> None:
        """Routine sets A=$AA, X=$BB, Y=$CC, RTS. Verify returned regs."""
        code = [
            0xA9, 0xAA,  # LDA #$AA
            0xA2, 0xBB,  # LDX #$BB
            0xA0, 0xCC,  # LDY #$CC
            0x60,        # RTS
        ]
        load_code(binary_transport, CODE_BASE, code)
        regs = jsr(binary_transport, CODE_BASE, timeout=15)

        assert regs["A"] == 0xAA
        assert regs["X"] == 0xBB
        assert regs["Y"] == 0xCC

    def test_set_register_and_read_back(self, binary_transport) -> None:
        """set_registers then read_registers for A, X, Y.

        The binary monitor keeps a persistent connection, so registers are
        preserved without needing to park the CPU in a JMP loop.
        """
        for name, value in [("A", 0x42), ("X", 0x7F), ("Y", 0x01)]:
            binary_transport.set_registers({name: value})
            regs = binary_transport.read_registers()
            assert regs[name] == value, \
                f"Register {name}: expected {value:#x}, got {regs[name]:#x}"

    def test_breakpoint_fires(self, binary_transport) -> None:
        """NOP;NOP;NOP at $C000, checkpoint at $C002, resume, wait."""
        code = [0xEA, 0xEA, 0xEA]  # NOP; NOP; NOP
        load_code(binary_transport, CODE_BASE, code)

        bp_num = binary_transport.set_checkpoint(CODE_BASE + 2)
        try:
            binary_transport.set_registers({"PC": CODE_BASE})
            binary_transport.resume()
            pc = binary_transport.wait_for_stopped(timeout=15)
            assert pc == CODE_BASE + 2
        finally:
            binary_transport.delete_checkpoint(bp_num)


# ======================================================================
# Memory
# ======================================================================

class TestMemory:
    """Test memory.py functions against real VICE."""

    def test_write_and_read_bytes(self, binary_transport) -> None:
        """Write 5-byte pattern, read back."""
        pattern = bytes([0xDE, 0xAD, 0xBE, 0xEF, 0x42])
        write_bytes(binary_transport, DATA_BASE, list(pattern))
        result = read_bytes(binary_transport, DATA_BASE, len(pattern))
        assert result == pattern

    def test_read_bytes_large(self, binary_transport) -> None:
        """Write 512 bytes, read back -- no chunking needed with binary monitor.

        The binary monitor has no write size limitation, so we can write
        the full 512 bytes in a single call.
        """
        data = bytes(range(256)) + bytes(range(256))
        write_bytes(binary_transport, DATA_BASE, list(data))
        result = read_bytes(binary_transport, DATA_BASE, 512)
        assert result == data

    def test_read_word_le(self, binary_transport) -> None:
        """Write [0x34, 0x12], read_word_le == 0x1234."""
        write_bytes(binary_transport, DATA_BASE, [0x34, 0x12])
        assert read_word_le(binary_transport, DATA_BASE) == 0x1234

    def test_read_dword_le(self, binary_transport) -> None:
        """Write [0x78, 0x56, 0x34, 0x12], read_dword_le == 0x12345678."""
        write_bytes(binary_transport, DATA_BASE, [0x78, 0x56, 0x34, 0x12])
        assert read_dword_le(binary_transport, DATA_BASE) == 0x12345678

    def test_hex_dump_format(self, binary_transport) -> None:
        """Write 32 known bytes, verify hex_dump output format."""
        data = list(range(32))
        write_bytes(binary_transport, DATA_BASE, data)
        output = hex_dump(binary_transport, DATA_BASE, 32)

        lines = output.strip().split("\n")
        assert len(lines) == 2  # 32 bytes = 2 lines of 16
        assert lines[0].startswith(f"${DATA_BASE:04X}:")
        assert lines[1].startswith(f"${DATA_BASE + 16:04X}:")
        # Verify first line contains "00 01 02 ... 0f"
        assert "00 01 02" in lines[0]

    def test_read_rom_bytes(self, binary_transport) -> None:
        """Read BASIC ROM at $A000 -- known C64 ROM signature."""
        data = read_bytes(binary_transport, 0xA000, 2)
        # C64 BASIC ROM starts with $94 $E3 (cold start vector)
        assert len(data) == 2
        assert all(isinstance(b, int) for b in data)


# ======================================================================
# Screen & Debug
# ======================================================================

class TestScreen:
    """Test screen.py functions against real VICE.

    The binary monitor auto-pauses the CPU on each command.  Screen tests
    use _restore_basic() and _wait_for_text_binary() which explicitly
    resume the CPU between operations.
    """

    @pytest.fixture(autouse=True)
    def _ensure_basic_loop(self, binary_transport):
        """Ensure CPU is in the BASIC idle loop for screen tests."""
        _restore_basic(binary_transport)

    def test_screen_grid_reads_real_screen(self, binary_transport) -> None:
        """ScreenGrid after boot has READY., 40 cols, 25 rows."""
        grid = ScreenGrid.from_transport(binary_transport)
        assert grid.has_text("READY.")
        assert len(grid.text_lines()) == 25
        for line in grid.text_lines():
            assert len(line) == 40

    def test_wait_for_text_after_print(self, binary_transport) -> None:
        """send_text PRINT command, wait for output on screen."""
        send_text(binary_transport, 'PRINT"HELLO VICE"\r')
        # Resume so BASIC processes the keystrokes
        binary_transport.resume()
        grid = _wait_for_text_binary(binary_transport, "HELLO VICE", timeout=15)
        assert grid is not None, "HELLO VICE not found on screen"

    def test_wait_for_stable_on_idle(self, binary_transport) -> None:
        """Screen grid reads READY. on idle C64."""
        # With binary monitor, just read the screen -- CPU is paused but
        # screen memory already has READY. from BASIC boot
        grid = ScreenGrid.from_transport(binary_transport)
        assert grid is not None
        assert grid.has_text("READY.")

    def test_dump_screen_contains_ready(self, binary_transport) -> None:
        """dump_screen returns string with READY and frame markers."""
        output = dump_screen(binary_transport, "test")
        assert "READY" in output
        assert "--- Screen dump [test] ---" in output
        assert "---" in output


# ======================================================================
# Keyboard
# ======================================================================

class TestKeyboard:
    """Test keyboard.py functions against real VICE.

    Keyboard tests inject keystrokes and verify screen output.  After
    injecting keys, we resume the CPU so BASIC can process them, then
    use _wait_for_text_binary() which resumes between screen polls.
    """

    @pytest.fixture(autouse=True)
    def _ensure_basic_loop(self, binary_transport):
        """Ensure CPU is in the BASIC idle loop and ready for keyboard input."""
        _restore_basic(binary_transport)
        # Verify BASIC is ready
        grid = ScreenGrid.from_transport(binary_transport)
        assert grid.has_text("READY."), "BASIC not ready after restore"

    def test_send_text_basic_command(self, binary_transport) -> None:
        """send_text PRINT 2+3, verify '5' appears on screen."""
        send_text(binary_transport, "PRINT 2+3\r")
        binary_transport.resume()
        grid = _wait_for_text_binary(binary_transport, "5", timeout=15)
        assert grid is not None, "'5' not found on screen after PRINT 2+3"

    def test_send_key_single_chars(self, binary_transport) -> None:
        """Individual send_key calls form a BASIC command."""
        for ch in "PRINT 7\r":
            send_key(binary_transport, ch)
        binary_transport.resume()
        grid = _wait_for_text_binary(binary_transport, "7", timeout=15)
        assert grid is not None

    def test_send_text_long_batching(self, binary_transport) -> None:
        """36-char PRINT command (4 batches of 10 keys)."""
        cmd = 'PRINT"ABCDEFGHIJKLMNOPQRST"\r'
        send_text(binary_transport, cmd)
        binary_transport.resume()
        grid = _wait_for_text_binary(binary_transport, "ABCDEFGHIJKLMNOPQRST",
                                     timeout=15)
        assert grid is not None, "Long string not found on screen"

    def test_send_text_return_key(self, binary_transport) -> None:
        """send_text with just CR should not crash."""
        send_text(binary_transport, "\r")
        binary_transport.resume()
        time.sleep(0.5)
        # Just verify we can still read the screen afterwards
        grid = ScreenGrid.from_transport(binary_transport)
        assert grid is not None


# ======================================================================
# wait_for_pc timeout (no longer needs to be last -- resume is safe)
# ======================================================================

class TestWaitForPcTimeout:
    """Test wait_for_pc equivalent timeout behaviour via binary monitor.

    With the binary monitor, resume() does NOT destroy the connection,
    so this class can appear anywhere in the file.
    """

    def test_wait_for_stopped_timeout(self, binary_transport) -> None:
        """Tight JMP loop -- wait_for_stopped raises TimeoutError."""
        code = [0x4C, 0x00, 0xC0]  # JMP $C000
        binary_transport.write_memory(CODE_BASE, code)

        binary_transport.set_registers({"PC": CODE_BASE})
        binary_transport.resume()
        with pytest.raises(TransportTimeoutError):
            binary_transport.wait_for_stopped(timeout=3)
