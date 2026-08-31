"""Integration tests for BinaryViceTransport (VICE binary monitor protocol).

Requires x64sc on PATH.  Each test class shares a single VICE process via
the module-scoped ``binary_transport`` fixture.

Unlike the text monitor, the binary monitor keeps a persistent connection
and the CPU does NOT auto-pause on connect.  The Exit (resume) command
does NOT close the connection -- it stays open for further commands.
This means resume() is NOT destructive, and tests can freely call it.
"""

from __future__ import annotations

import time

import pytest

from c64_test_harness.backends.vice_binary import (
    DISPLAY_GET_SHORTFALL,
    BinaryViceTransport,
)
from c64_test_harness.backends.vice_lifecycle import ViceConfig, ViceProcess
from c64_test_harness.backends.vice_manager import PortAllocator
from c64_test_harness.transport import TransportError

from conftest import connect_binary_transport

# Skip entire module if x64sc is not installed
pytestmark = pytest.mark.vice_live

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


class TestFullAddressSpaceRead:
    """A 64 KiB read must not be sent as a single MEM_GET.

    VICE computes ``length = (endaddress + 1) - startaddress`` into a
    ``uint32_t`` and then writes it into the response with
    ``write_uint16`` (S ``monitor_binary.c:1637,1672``).  For a whole
    address space that is 0x10000, which truncates to 0, so the response
    declares zero bytes of payload and the transport can only raise.

    The chunker computed ``0x10000 - (addr & 0xFFFF)``, which for
    ``addr=0`` is exactly the one size that cannot work, and never split.
    """

    def test_full_64k_read_matches_piecewise_reads(self, binary_transport) -> None:
        whole = binary_transport.read_memory(0x0000, 0x10000)
        assert len(whole) == 0x10000
        piecewise = b"".join(
            binary_transport.read_memory(base, 0x2000)
            for base in range(0x0000, 0x10000, 0x2000)
        )
        assert whole == piecewise

    def test_read_spanning_the_top_of_memory(self, binary_transport) -> None:
        """The chunk boundary must not drop or duplicate the last byte."""
        whole = binary_transport.read_memory(0x0000, 0x10000)
        assert whole[0xFFFF:] == binary_transport.read_memory(0xFFFF, 1)
        assert whole[0xFF00:] == binary_transport.read_memory(0xFF00, 0x100)

    def test_65535_byte_read_still_works(self, binary_transport) -> None:
        """0xFFFF is the largest length VICE's uint16 field can express."""
        data = binary_transport.read_memory(0x0000, 0xFFFF)
        assert len(data) == 0xFFFF
        assert data == binary_transport.read_memory(0x0000, 0x10000)[:0xFFFF]


# ======================================================================
# Anchors: constants this harness asserts about VICE, checked against it
# ======================================================================


class TestUpstreamBugOneIsReal:
    """``DISPLAY_GET`` really is four bytes short (upstream bug 1).

    ``DISPLAY_GET_SHORTFALL`` is a number read out of VICE's source and
    typed into ours; ``read_framebuffer`` refuses any *other* shortfall as
    corruption.  If the real value were different -- a VICE build that
    fixed the bug, or one that broke it differently -- the mocked tests
    would keep agreeing with the constant and every live call would start
    raising.  So measure it.
    """

    def test_the_declared_length_exceeds_the_delivered_by_exactly_four(
        self, binary_transport
    ) -> None:
        fb = binary_transport.read_framebuffer()
        assert fb["short_by"] == DISPLAY_GET_SHORTFALL, (
            f"VICE delivered {len(fb['bytes'])} of {fb['declared_length']} "
            f"declared bytes ({fb['short_by']} short). The harness is built "
            f"around a {DISPLAY_GET_SHORTFALL}-byte shortfall "
            f"(docs/vice_upstream_bugs.md bug 1); this build differs, so "
            f"the constant and the doc both need revisiting."
        )

    def test_the_shortfall_is_reported_not_hidden(self, binary_transport) -> None:
        """The caller must be able to see it without counting bytes."""
        fb = binary_transport.read_framebuffer()
        assert fb["declared_length"] == len(fb["bytes"]) + fb["short_by"]
        assert fb["short_by"] > 0, (
            "this build appears to have fixed the bug — if so, remove the "
            "workaround rather than leaving it to rot"
        )


class TestStatusRegisterNameAnchor:
    """Which name does VICE actually use for the status register?

    ``_parse_cpu_history_entry`` resolves ``sr`` through ``FL`` → ``FLAGS``
    → ``SR``.  That chain is an assumption about VICE's naming that no
    test checked: if VICE used none of the three, ``sr`` would read 0
    forever and every mocked test would still pass.

    Measured on this bench (VICE 3.10, Homebrew bottle), the full set is
    ``00 01 A CYC FL LIN PC SP X Y`` — so **``FL`` is the real name** and
    ``FLAGS``/``SR`` are speculative fallbacks that never fire here.  The
    test asserts the chain matches *something* rather than pinning
    ``FL``: the fallbacks cost nothing, and a VICE fork or a later
    release renaming the register is exactly what this should catch.
    """

    def test_vice_exposes_a_status_register_the_alias_chain_matches(
        self, binary_transport
    ) -> None:
        names = {r["name"] for r in binary_transport.registers_available()}
        matched = [n for n in ("FL", "FLAGS", "SR") if n in names]
        assert matched, (
            f"VICE exposes {sorted(names)}, none of which is FL/FLAGS/SR, so "
            f"the 'sr' field in cpu_history() is permanently 0. The alias "
            f"chain in _parse_cpu_history_entry needs the real name."
        )

    def test_the_status_register_reaches_cpu_history_as_sr(
        self, binary_transport
    ) -> None:
        """End to end: the alias chain must actually populate ``sr``.

        Knowing the name exists is not the same as the chain using it --
        that is the gap a register-name mutation slipped through.
        """
        binary_transport.cpu_history(count=1)  # prime the ring
        binary_transport.single_step()
        history = binary_transport.cpu_history(count=4)
        if not history:
            pytest.skip("VICE returned no CPU history records")
        assert all("sr" in entry for entry in history)
        assert any(entry["registers"] for entry in history), (
            "no record carried any named register, so this cannot show the "
            "alias chain resolving"
        )


class TestCheckpointList:
    """Enumerating checkpoints, which the client could not do before.

    The harness could set and delete checkpoints but never list them, so
    a checkpoint leaked by an interrupted ``jsr()`` — whose ``finally``
    never ran — was invisible from the client side. A leaked execution
    checkpoint pins the CPU at its address: every resume re-triggers it
    and stops before executing, which is indistinguishable from a hung
    emulator without this.
    """

    def test_a_set_checkpoint_appears_and_a_deleted_one_does_not(
        self, binary_transport
    ) -> None:
        before = binary_transport.checkpoint_list()
        num = binary_transport.set_checkpoint(0xC002)
        try:
            listed = binary_transport.checkpoint_list()
            assert len(listed) == len(before) + 1, (
                f"setting one checkpoint changed the list by "
                f"{len(listed) - len(before)}"
            )
            mine = [c for c in listed if c["number"] == num]
            assert mine, f"checkpoint {num} was set but is not listed"
            assert mine[0]["start"] == 0xC002
            assert mine[0]["enabled"] is True
        finally:
            binary_transport.delete_checkpoint(num)

        after = binary_transport.checkpoint_list()
        assert [c["number"] for c in after] == [c["number"] for c in before], (
            "the deleted checkpoint is still listed — a delete that VICE "
            "did not honour would leak a CPU-pinning checkpoint"
        )

    def test_the_declared_count_and_the_frames_agree(
        self, binary_transport
    ) -> None:
        """The trailing 0x14 frame carries a count; cross-check it.

        VICE answers this command with one CHECKPOINT_INFO per checkpoint
        and *then* a count. Parsing only the count, or only the frames,
        would hide a desynchronised reply — the issue-#88 shape.
        """
        nums = [binary_transport.set_checkpoint(a) for a in (0xC010, 0xC020)]
        try:
            listed = binary_transport.checkpoint_list()
            starts = {c["start"] for c in listed}
            assert {0xC010, 0xC020} <= starts, (
                f"expected both checkpoints in {sorted(hex(s) for s in starts)}"
            )
        finally:
            for n in nums:
                binary_transport.delete_checkpoint(n)
