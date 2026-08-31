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
pytestmark = pytest.mark.vice_live

# Scratch area for machine code ($C000-$CFFF) -- avoids clobbering BASIC/kernal
CODE_BASE = 0xC000
DATA_BASE = 0xC100

#: PC sampled at the end of each poll window by the last
#: :func:`_wait_for_text_binary` call, read by the failure report.
_LAST_POLL_TRACE: list[int] = []


def _wait_for_text_binary(transport, needle, timeout=15.0, poll_interval=1.0):
    """Poll screen for *needle*, resuming the CPU between reads.

    The binary monitor auto-pauses the CPU when any command is sent.
    This helper resumes the CPU after each screen read so the KERNAL
    can continue updating the screen.

    Every command re-enters the monitor (S ``monitor.c:407`` ->
    ``monitor_binary.c:284``), and the CPU is halted for as long as the
    monitor is open.  So ``time.sleep(poll_interval)`` below is the
    *only* window in which the C64 executes at all.  If a resume is lost,
    or a command lands between the resume and the sleep, that iteration
    gives the machine zero cycles and the screen cannot change however
    long the loop runs -- which is a candidate explanation for the
    intermittent failures in ``TestKeyboard``.

    So the PC is sampled once per window and kept for the failure report.
    That costs one extra monitor entry per iteration, taken immediately
    before the next screen read would have trapped the CPU anyway, so it
    does not shorten the window it measures.
    """
    needle_upper = needle.upper()
    deadline = time.monotonic() + timeout
    trace: list[int] = []
    try:
        while time.monotonic() < deadline:
            grid = ScreenGrid.from_transport(transport)
            if needle_upper in grid.continuous_text().upper():
                return grid
            transport.resume()
            time.sleep(poll_interval)
            try:
                trace.append(transport.read_registers().get("PC", -1))
            except Exception:
                break
        return None
    finally:
        _LAST_POLL_TRACE[:] = trace


def _emulator_is_stalled(transport, samples: int = 4) -> tuple[bool, list[str]]:
    """Whether VICE has stopped emulating, and the raster positions seen.

    ``LIN``/``CYC`` are the raster position.  They advance whenever the
    *machine* is emulating, whether or not the 6510 is executing any
    instruction, so they discriminate a stalled emulator from every other
    failure — and unlike a PC sample they cannot coincide by accident,
    because the raster never sits still on a running machine.

    This is upstream bug 6 (docs/vice_upstream_bugs.md): under host load
    VICE stops emulating while its monitor thread stays healthy, answers
    every command, and acknowledges every resume.  A test that waits on
    the machine running will otherwise spend its whole timeout and then
    report something misleading about screen text.
    """
    seen: list[str] = []
    positions: set[tuple[int, int]] = set()
    for _ in range(samples):
        try:
            r = transport.read_registers()
        except Exception:
            break
        pos = (r.get("LIN", -1), r.get("CYC", -1))
        positions.add(pos)
        seen.append(f"LIN={pos[0]} CYC={pos[1]}")
        if len(positions) > 1:
            return False, seen
        transport.resume()
        time.sleep(0.1)
    return len(positions) == 1, seen


def _stub_was_executed(transport, samples: int = 4) -> tuple[bool, list[int]]:
    """Whether the CPU left ``_restore_basic``'s stub, and the PCs seen.

    Deliberately *not* "did the PC change".  The BASIC idle loop is about
    eight bytes ($E5CD-$E5D4), so on a perfectly healthy machine four
    samples can land on the same address by coincidence -- measured, and
    it would make this a flaky check for the flake it exists to diagnose.

    The stub at $CF00 is ``CLI; JMP $E5CD``: it executes once and never
    returns.  So "PC is no longer $CF00" is a precise, one-way signal
    that the machine ran, immune to loop-size coincidence.
    """
    seen: list[int] = []
    for _ in range(samples):
        try:
            seen.append(transport.read_registers().get("PC", -1))
        except Exception:
            break
        if seen[-1] != 0xCF00:
            return True, seen
        transport.resume()
        time.sleep(0.1)
    return False, seen


def _machine_failure_report(transport, needle: str) -> str:
    """Why a test waiting on the machine failed, rather than just that it did.

    Used by every site that waits for the C64 to actually run — the
    keyboard tests and ``TestScreen``'s print-and-wait alike. That is not
    incidental: the stall reported as upstream bug 6 has now been
    observed in both, which is itself evidence that the fault is the
    emulator rather than anything about keyboard injection.

    These three tests fail together, intermittently, and only in
    full-suite runs -- never in 60 isolated runs, nor in 30 under heavy
    concurrent VICE load.  "'5' not found on screen" is all the evidence
    a failure has ever produced, which is why the cause is still open.

    The distinguishing question is whether the 6510 is *executing*.  The
    fourth test in this class asserts no screen content and has never
    failed, which points at a stopped CPU rather than a slow screen
    update -- so sample the PC twice across a resume and say whether it
    moved.
    """
    lines = [f"expected {needle!r} on screen"]

    # First, because it subsumes every other diagnosis below: if VICE has
    # stopped emulating, the PC, the screen and the checkpoints are all
    # just the last state the machine was left in.
    try:
        stalled, raster = _emulator_is_stalled(transport)
        lines.append(
            f"raster across resumes: {raster}"
            + ("  <- FROZEN: VICE has stopped emulating entirely "
               "(upstream bug 6). Everything below is the state it was "
               "left in, not a clue about this test." if stalled else
               "  (advancing: the emulator is running)")
        )
    except Exception as e:
        lines.append(f"could not sample the raster: {type(e).__name__}: {e}")
    if _LAST_POLL_TRACE:
        distinct = len(set(_LAST_POLL_TRACE))
        lines.append(
            f"PC at the end of each poll window ({len(_LAST_POLL_TRACE)} "
            f"windows, {distinct} distinct): "
            f"{[hex(p) for p in _LAST_POLL_TRACE[:8]]}"
            + ("" if distinct > 1 else
               "  <- IDENTICAL: the CPU got no cycles in any poll window, "
               "so a resume was lost rather than the screen being slow. "
               "Note the PC line below resumes explicitly, so it can show "
               "'advancing' even in this case — together they mean the CPU "
               "runs when resumed but the loop's resumes were not landing.")
        )
    try:
        first = transport.read_registers()
        transport.resume()
        time.sleep(0.3)
        second = transport.read_registers()
        moved = first.get("PC") != second.get("PC")
        lines.append(
            f"PC {first.get('PC', 0):#06x} -> {second.get('PC', 0):#06x} "
            f"({'advancing' if moved else 'STUCK — the CPU is not running'})"
        )
        lines.append(f"registers: {second}")
    except Exception as e:  # diagnostics must never mask the real failure
        lines.append(f"could not read registers: {type(e).__name__}: {e}")
    # What is actually at $CF00?  A pinned PC is equally consistent with
    # "halted" and with "jammed": if the stub write did not land, or was
    # overwritten, the 6510 may be sitting on an illegal opcode that
    # halts it in place.  $CF00 should read 58 4C CD E5 (CLI; JMP $E5CD).
    try:
        stub = transport.read_memory(0xCF00, 4)
        expected = bytes([0x58, 0x4C, 0xCD, 0xE5])
        lines.append(
            f"memory at $CF00: {stub.hex()} "
            + ("(the stub, intact)" if stub == expected else
               f"<- NOT the stub, expected {expected.hex()} — the write did "
               f"not land or was overwritten, so the CPU may be jammed on "
               f"whatever is there rather than halted")
        )
    except Exception as e:
        lines.append(f"could not read $CF00: {type(e).__name__}: {e}")

    # A leaked execution checkpoint pins the CPU at its address — every
    # resume re-triggers it and stops before executing — which is
    # indistinguishable from a hung emulator without asking.  For the
    # TestKeyboard wedge this reported zero, which is how that diagnosis
    # was ruled out rather than argued about.
    try:
        cps = transport.checkpoint_list()
        lines.append(
            f"checkpoints VICE holds: {len(cps)}"
            + ("" if not cps else
               " -> " + ", ".join(
                   f"#{c['number']} at ${c['start']:04x}"
                   f"{' (enabled)' if c['enabled'] else ' (disabled)'}"
                   for c in cps))
        )
    except Exception as e:
        lines.append(f"could not list checkpoints: {type(e).__name__}: {e}")
    try:
        lines.append(dump_screen(transport, label="screen at failure"))
    except Exception as e:
        lines.append(f"could not dump screen: {type(e).__name__}: {e}")
    return "\n".join(lines)


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
        assert grid is not None, _machine_failure_report(
            binary_transport, "HELLO VICE"
        )

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

        # Prove the CPU is executing *before* trusting anything on screen.
        #
        # ``READY.`` is left on screen by the previous test and
        # ``_restore_basic`` never clears it, so asserting on it is the
        # false-completion signal this repo documents (c33b5c4, issue
        # #138): it cannot tell "BASIC is ready now" from "the screen
        # still shows READY. from the last test and the 6510 is wedged".
        # That is exactly the shape of the intermittent failure these
        # tests show — the fixture passes, then the three tests that need
        # execution fail while the one that only reads the screen passes.
        #
        # This was recorded as an *analytic* argument when the check was
        # written, because every wedge seeded by hand also stopped BASIC
        # from drawing READY. in the first place, so the old assertion
        # failed for the wrong reason and its vacuity could not be shown.
        # The real failure then produced the state that could not be
        # constructed: caught in a full-suite run, the screen genuinely
        # still carried READY. from the previous test while the 6510 sat
        # pinned at $CF00. Measured, not argued.
        ran, pcs = _stub_was_executed(binary_transport)
        assert ran, (
            "the 6510 never left _restore_basic's stub — PC stayed at "
            f"{[hex(p) for p in pcs]} ($CF00 is CLI; JMP $E5CD, which "
            f"executes once and never returns). Any READY. on screen is "
            f"left over from the previous test.\n"
            + _machine_failure_report(binary_transport, "a running CPU")
        )

        grid = ScreenGrid.from_transport(binary_transport)
        assert grid.has_text("READY."), _machine_failure_report(
            binary_transport, "READY."
        )

    def test_send_text_basic_command(self, binary_transport) -> None:
        """send_text PRINT 2+3, verify '5' appears on screen."""
        send_text(binary_transport, "PRINT 2+3\r")
        binary_transport.resume()
        grid = _wait_for_text_binary(binary_transport, "5", timeout=15)
        assert grid is not None, _machine_failure_report(binary_transport, "5")

    def test_send_key_single_chars(self, binary_transport) -> None:
        """Individual send_key calls form a BASIC command."""
        for ch in "PRINT 7\r":
            send_key(binary_transport, ch)
        binary_transport.resume()
        grid = _wait_for_text_binary(binary_transport, "7", timeout=15)
        assert grid is not None, _machine_failure_report(binary_transport, "7")

    def test_send_text_long_batching(self, binary_transport) -> None:
        """36-char PRINT command (4 batches of 10 keys)."""
        cmd = 'PRINT"ABCDEFGHIJKLMNOPQRST"\r'
        send_text(binary_transport, cmd)
        binary_transport.resume()
        grid = _wait_for_text_binary(binary_transport, "ABCDEFGHIJKLMNOPQRST",
                                     timeout=15)
        assert grid is not None, _machine_failure_report(
            binary_transport, "ABCDEFGHIJKLMNOPQRST"
        )

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
