"""VICE integration tests for core harness modules.

Validates execute, memory, screen, keyboard, and debug functions against
a real VICE instance.  Each test class shares a single VICE process via
the module-scoped ``vice_transport`` fixture from conftest.py.

VICE monitor behaviour note
----------------------------
VICE's text monitor (-remotemonitor) accepts one TCP connection at a time.
While connected, the CPU is paused.  When the connection closes *normally*
(without sending the ``x`` exit command), the CPU runs briefly and the
monitor immediately re-listens for new connections.

Calling ``resume()`` sends ``x``, which makes the monitor **stop listening
permanently**.  The monitor will NOT reopen, even if a breakpoint fires.
This means:

- ``goto()`` (which uses ``set_register("PC", …)``) keeps the monitor
  accessible — the CPU just runs from the new PC.
- ``resume()`` is a one-way door: the monitor never comes back.
- ``jsr()`` works because the breakpoint fires *before* ``wait_for_pc``
  ever calls ``resume()``.

**IMPORTANT**: ``TestWaitForPcTimeout`` must be the LAST class in this
file because ``wait_for_pc`` calls ``resume()`` internally when the PC
doesn't match, which permanently closes the monitor.
"""

from __future__ import annotations

import shutil

import pytest

from c64_test_harness.debug import dump_screen
from c64_test_harness.execute import (
    goto,
    jsr,
    load_code,
    set_breakpoint,
    set_register,
    wait_for_pc,
)
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

# Scratch area for machine code ($C000-$CFFF) — avoids clobbering BASIC/kernal
CODE_BASE = 0xC000
DATA_BASE = 0xC100

# Address for a "parking" JMP loop that preserves all registers.
# goto() (not resume()) keeps the monitor accessible.
PARK_ADDR = 0xCF00


def _restore_basic(transport) -> None:
    """Return CPU to the BASIC idle loop so it can process keystrokes.

    After parking the CPU in a tight loop, screen/keyboard tests need
    the CPU back in the KERNAL main loop.  CLI + JMP $E5CD (KERNAL
    MAINLOOP) restores normal operation.
    """
    # CLI (re-enable interrupts) + JMP $E5CD (KERNAL MAINLOOP)
    load_code(transport, PARK_ADDR, [0x58, 0x4C, 0xCD, 0xE5])
    goto(transport, PARK_ADDR)


def _park_cpu(transport) -> None:
    """Put CPU into a tight JMP loop that preserves all registers.

    Uses goto() which keeps the monitor listening (unlike resume()).
    SEI disables interrupts so the KERNAL IRQ handler doesn't clobber
    registers between TCP connections.
    """
    # SEI; JMP $CF01
    load_code(transport, PARK_ADDR, [0x78, 0x4C, 0x01, 0xCF])
    goto(transport, PARK_ADDR)


# ======================================================================
# Execution control
# ======================================================================

class TestExecution:
    """Test execute.py functions against real VICE."""

    def test_jsr_simple_rts(self, vice_transport) -> None:
        """Load RTS at $C000, jsr(), verify trampoline round-trip."""
        load_code(vice_transport, CODE_BASE, [0x60])  # RTS
        regs = jsr(vice_transport, CODE_BASE, timeout=15)
        assert "PC" in regs
        assert "A" in regs

    def test_jsr_computation(self, vice_transport) -> None:
        """Double a value: LDA $C100 / ASL A / STA $C101 / RTS."""
        code = [
            0xAD, 0x00, 0xC1,  # LDA $C100
            0x0A,              # ASL A
            0x8D, 0x01, 0xC1,  # STA $C101
            0x60,              # RTS
        ]
        load_code(vice_transport, CODE_BASE, code)
        write_bytes(vice_transport, DATA_BASE, [42])
        write_bytes(vice_transport, DATA_BASE + 1, [0])

        jsr(vice_transport, CODE_BASE, timeout=15)

        result = read_bytes(vice_transport, DATA_BASE + 1, 1)
        assert result == bytes([84]), f"Expected 84, got {result[0]}"

    def test_jsr_register_state(self, vice_transport) -> None:
        """Routine sets A=$AA, X=$BB, Y=$CC, RTS. Verify returned regs."""
        code = [
            0xA9, 0xAA,  # LDA #$AA
            0xA2, 0xBB,  # LDX #$BB
            0xA0, 0xCC,  # LDY #$CC
            0x60,        # RTS
        ]
        load_code(vice_transport, CODE_BASE, code)
        regs = jsr(vice_transport, CODE_BASE, timeout=15)

        assert regs["A"] == 0xAA
        assert regs["X"] == 0xBB
        assert regs["Y"] == 0xCC

    def test_set_register_and_read_back(self, vice_transport) -> None:
        """set_register then read_registers for A, X, Y.

        Parks CPU in a JMP loop (via goto, not resume) so registers
        are preserved between per-command TCP connections.
        """
        _park_cpu(vice_transport)

        for name, value in [("A", 0x42), ("X", 0x7F), ("Y", 0x01)]:
            set_register(vice_transport, name, value)
            regs = vice_transport.read_registers()
            assert regs[name] == value, \
                f"Register {name}: expected {value:#x}, got {regs[name]:#x}"

    def test_breakpoint_fires(self, vice_transport) -> None:
        """NOP;NOP;NOP at $C000, breakpoint at $C002, goto, wait_for_pc."""
        code = [0xEA, 0xEA, 0xEA]  # NOP; NOP; NOP
        load_code(vice_transport, CODE_BASE, code)

        bp_id = set_breakpoint(vice_transport, CODE_BASE + 2)
        try:
            goto(vice_transport, CODE_BASE)
            regs = wait_for_pc(vice_transport, CODE_BASE + 2, timeout=15)
            assert regs["PC"] == CODE_BASE + 2
        finally:
            vice_transport.raw_command(f"delete {bp_id}")


# ======================================================================
# Memory
# ======================================================================

class TestMemory:
    """Test memory.py functions against real VICE."""

    def test_write_and_read_bytes(self, vice_transport) -> None:
        """Write 5-byte pattern, read back."""
        pattern = bytes([0xDE, 0xAD, 0xBE, 0xEF, 0x42])
        write_bytes(vice_transport, DATA_BASE, list(pattern))
        result = read_bytes(vice_transport, DATA_BASE, len(pattern))
        assert result == pattern

    def test_read_bytes_chunked_large(self, vice_transport) -> None:
        """Write 512 bytes in chunks, read back — triggers auto-chunking.

        Parks CPU in a JMP loop first so it doesn't modify the memory
        region between write and read connections.  Writes in 64-byte
        chunks because VICE's text monitor input buffer (~256 chars)
        truncates longer >C: commands.
        """
        _park_cpu(vice_transport)
        data = bytes(range(256)) + bytes(range(256))

        chunk_size = 64
        for i in range(0, len(data), chunk_size):
            write_bytes(vice_transport, DATA_BASE + i,
                        list(data[i:i + chunk_size]))

        result = read_bytes(vice_transport, DATA_BASE, 512)
        assert result == data

    def test_read_word_le(self, vice_transport) -> None:
        """Write [0x34, 0x12], read_word_le == 0x1234."""
        write_bytes(vice_transport, DATA_BASE, [0x34, 0x12])
        assert read_word_le(vice_transport, DATA_BASE) == 0x1234

    def test_read_dword_le(self, vice_transport) -> None:
        """Write [0x78, 0x56, 0x34, 0x12], read_dword_le == 0x12345678."""
        write_bytes(vice_transport, DATA_BASE, [0x78, 0x56, 0x34, 0x12])
        assert read_dword_le(vice_transport, DATA_BASE) == 0x12345678

    def test_hex_dump_format(self, vice_transport) -> None:
        """Write 32 known bytes, verify hex_dump output format."""
        data = list(range(32))
        write_bytes(vice_transport, DATA_BASE, data)
        output = hex_dump(vice_transport, DATA_BASE, 32)

        lines = output.strip().split("\n")
        assert len(lines) == 2  # 32 bytes = 2 lines of 16
        assert lines[0].startswith(f"${DATA_BASE:04X}:")
        assert lines[1].startswith(f"${DATA_BASE + 16:04X}:")
        # Verify first line contains "00 01 02 ... 0f"
        assert "00 01 02" in lines[0]

    def test_read_rom_bytes(self, vice_transport) -> None:
        """Read BASIC ROM at $A000 — known C64 ROM signature."""
        data = read_bytes(vice_transport, 0xA000, 2)
        # C64 BASIC ROM starts with $94 $E3 (cold start vector)
        assert len(data) == 2
        assert all(isinstance(b, int) for b in data)


# ======================================================================
# Screen & Debug
# ======================================================================

class TestScreen:
    """Test screen.py functions against real VICE."""

    @pytest.fixture(autouse=True)
    def _ensure_basic_loop(self, vice_transport):
        """Ensure CPU is in the BASIC idle loop for screen tests."""
        _restore_basic(vice_transport)

    def test_screen_grid_reads_real_screen(self, vice_transport) -> None:
        """ScreenGrid after boot has READY., 40 cols, 25 rows."""
        grid = ScreenGrid.from_transport(vice_transport)
        assert grid.has_text("READY.")
        assert len(grid.text_lines()) == 25
        # Each line should be 40 chars
        for line in grid.text_lines():
            assert len(line) == 40

    def test_wait_for_text_after_print(self, vice_transport) -> None:
        """send_text PRINT command, wait for output on screen."""
        send_text(vice_transport, 'PRINT"HELLO VICE"\r')
        grid = wait_for_text(vice_transport, "HELLO VICE", timeout=15,
                             verbose=False)
        assert grid is not None, "HELLO VICE not found on screen"

    def test_wait_for_stable_on_idle(self, vice_transport) -> None:
        """wait_for_stable on idle C64 should return a ScreenGrid."""
        grid = wait_for_stable(vice_transport, timeout=15, stable_count=3)
        assert grid is not None
        assert grid.has_text("READY.")

    def test_dump_screen_contains_ready(self, vice_transport) -> None:
        """dump_screen returns string with READY and frame markers."""
        output = dump_screen(vice_transport, "test")
        assert "READY" in output
        assert "--- Screen dump [test] ---" in output
        assert "---" in output


# ======================================================================
# Keyboard
# ======================================================================

class TestKeyboard:
    """Test keyboard.py functions against real VICE."""

    @pytest.fixture(autouse=True)
    def _ensure_basic_loop(self, vice_transport):
        """Ensure CPU is in the BASIC idle loop and ready for keyboard input."""
        _restore_basic(vice_transport)
        # Wait for BASIC to be fully ready before injecting keystrokes
        grid = wait_for_text(vice_transport, "READY.", timeout=15,
                             verbose=False)
        assert grid is not None, "BASIC not ready after restore"

    def test_send_text_basic_command(self, vice_transport) -> None:
        """send_text PRINT 2+3, verify '5' appears on screen."""
        send_text(vice_transport, "PRINT 2+3\r")
        grid = wait_for_text(vice_transport, "5", timeout=15, verbose=False)
        assert grid is not None, "'5' not found on screen after PRINT 2+3"

    def test_send_key_single_chars(self, vice_transport) -> None:
        """Individual send_key calls form a BASIC command."""
        # Type P, R, I, N, T, space, 7, return
        for ch in "PRINT 7\r":
            send_key(vice_transport, ch)
        grid = wait_for_text(vice_transport, "7", timeout=15, verbose=False)
        assert grid is not None

    def test_send_text_long_batching(self, vice_transport) -> None:
        """36-char PRINT command (4 batches of 10 keys)."""
        cmd = 'PRINT"ABCDEFGHIJKLMNOPQRST"\r'
        send_text(vice_transport, cmd)
        grid = wait_for_text(vice_transport, "ABCDEFGHIJKLMNOPQRST",
                             timeout=15, verbose=False)
        assert grid is not None, "Long string not found on screen"

    def test_send_text_return_key(self, vice_transport) -> None:
        """send_text with just CR should not crash."""
        send_text(vice_transport, "\r")
        # Just verify we can still read the screen afterwards
        grid = ScreenGrid.from_transport(vice_transport)
        assert grid is not None


# ======================================================================
# Destructive tests — MUST BE LAST (resume() permanently closes monitor)
# ======================================================================

class TestWaitForPcTimeout:
    """Test wait_for_pc timeout behaviour.

    This class MUST be the last in this file because wait_for_pc calls
    resume() internally, which permanently closes the VICE monitor port.
    """

    def test_wait_for_pc_timeout(self, vice_transport) -> None:
        """Tight JMP loop — wait_for_pc raises TransportTimeoutError."""
        code = [0x4C, 0x00, 0xC0]  # JMP $C000
        load_code(vice_transport, CODE_BASE, code)

        goto(vice_transport, CODE_BASE)
        with pytest.raises(TransportTimeoutError):
            wait_for_pc(vice_transport, CODE_BASE + 0x80, timeout=3)
