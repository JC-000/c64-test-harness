"""Unit tests for c64_test_harness.tod_timer.

These tests do NOT need a live VICE or Ultimate 64 target; they exercise
the pure 6502 code builders and verify the emitted byte sequences have
the right shape and opcodes.
"""

from __future__ import annotations

import pytest

from c64_test_harness.tod_timer import (
    MAX_DEADLINE_TENTHS,
    ZP_CUR_LO,
    ZP_CUR_HI,
    ZP_DEADLINE_LO,
    ZP_DEADLINE_HI,
    build_poll_with_tod_deadline_code,
    build_tod_read_tenths_code,
    build_tod_start_code,
)


class TestBuildTodStart:
    def test_starts_with_sei_and_ends_with_rts(self) -> None:
        code = build_tod_start_code(0xC000)
        assert code[0] == 0x78, "first byte should be SEI"
        assert code[-1] == 0x60, "last byte should be RTS"
        assert code[-2] == 0x58, "second-to-last byte should be CLI"

    def test_clears_dc0f_bit7(self) -> None:
        code = build_tod_start_code(0xC000)
        # Expect an 'AND #$7F' (0x29 0x7F) somewhere early (clearing bit 7).
        assert b"\x29\x7f" in code, "AND #$7F not found"

    def test_writes_tod_registers_in_order(self) -> None:
        code = build_tod_start_code(0xC000)
        # Expect STA $DC0B, STA $DC0A, STA $DC09, STA $DC08 in that order.
        # STA abs = 0x8D lo hi.
        pat_hr = b"\x8d\x0b\xdc"
        pat_min = b"\x8d\x0a\xdc"
        pat_sec = b"\x8d\x09\xdc"
        pat_tnt = b"\x8d\x08\xdc"
        i_hr = code.find(pat_hr)
        i_min = code.find(pat_min)
        i_sec = code.find(pat_sec)
        i_tnt = code.find(pat_tnt)
        assert i_hr >= 0 and i_min >= 0 and i_sec >= 0 and i_tnt >= 0
        assert i_hr < i_min < i_sec < i_tnt, (
            f"TOD writes must be ordered HR->MIN->SEC->TENTHS, got "
            f"HR={i_hr} MIN={i_min} SEC={i_sec} TNT={i_tnt}"
        )

    def test_length_bounded(self) -> None:
        code = build_tod_start_code(0xC000)
        # Reasonable upper bound: 30 bytes is more than enough.
        assert 10 < len(code) < 40


class TestBuildTodReadTenths:
    def test_latch_then_unlatch_sequence(self) -> None:
        code = build_tod_read_tenths_code(0xC000, 0xC1F0)
        # Read-latch sequence: LDA $DC0B (0xAD 0x0B 0xDC) before
        # LDA $DC0A (0xAD 0x0A 0xDC).
        i_hr = code.find(b"\xad\x0b\xdc")
        i_min = code.find(b"\xad\x0a\xdc")
        i_sec = code.find(b"\xad\x09\xdc")
        i_tnt = code.find(b"\xad\x08\xdc")
        assert i_hr >= 0 and i_min >= 0 and i_sec >= 0 and i_tnt >= 0
        assert i_hr < i_min, "must LDA HR (latch) before MIN"
        assert i_min < i_sec, "must LDA MIN before SEC"
        assert i_sec < i_tnt, "must LDA SEC before TENTHS (unlatch)"

    def test_result_stored_at_result_addr(self) -> None:
        code = build_tod_read_tenths_code(0xC000, 0xC1F0)
        # STA $C1F0 = 0x8D 0xF0 0xC1 ; STA $C1F1 = 0x8D 0xF1 0xC1
        assert b"\x8d\xf0\xc1" in code
        assert b"\x8d\xf1\xc1" in code

    def test_ends_rts(self) -> None:
        code = build_tod_read_tenths_code(0xC000, 0xC1F0)
        assert code[0] == 0x78  # SEI
        # RTS appears before the data tables but may not be the last byte.
        assert 0x60 in code

    def test_length_reasonable(self) -> None:
        code = build_tod_read_tenths_code(0xC000, 0xC1F0)
        # Read routine + BCD convert + two tables (26 bytes) -> well under 200
        assert 40 < len(code) < 300


class TestBuildPollWithTodDeadline:
    def test_deadline_validated(self) -> None:
        snippet = bytes([0xAD, 0x05, 0xDE, 0x29, 0x01])  # LDA $DE05; AND #$01
        with pytest.raises(ValueError):
            build_poll_with_tod_deadline_code(0xC000, snippet, 0xC1F0, 0)
        with pytest.raises(ValueError):
            build_poll_with_tod_deadline_code(0xC000, snippet, 0xC1F0, MAX_DEADLINE_TENTHS + 1)
        # Boundary values must succeed.
        build_poll_with_tod_deadline_code(0xC000, snippet, 0xC1F0, 1)
        build_poll_with_tod_deadline_code(0xC000, snippet, 0xC1F0, MAX_DEADLINE_TENTHS)

    def test_snippet_embedded(self) -> None:
        snippet = bytes([0xAD, 0x05, 0xDE, 0x29, 0x01])
        code = build_poll_with_tod_deadline_code(0xC000, snippet, 0xC1F0, 50)
        assert snippet in code, "peek_check_snippet must be embedded verbatim"

    def test_deadline_stored_at_f2f3(self) -> None:
        snippet = bytes([0xAD, 0x05, 0xDE, 0x29, 0x01])
        code = build_poll_with_tod_deadline_code(0xC000, snippet, 0xC1F0, 50)
        # LDA #50 / STA $F2 -> 0xA9 0x32 0x85 0xF2 at some position.
        assert bytes([0xA9, 50, 0x85, ZP_DEADLINE_LO]) in code
        assert bytes([0xA9, 0, 0x85, ZP_DEADLINE_HI]) in code

    def test_sbc_sequence_present(self) -> None:
        """The 16-bit compare must use SEC followed by SBC $F2 and SBC $F3."""
        snippet = bytes([0xAD, 0x05, 0xDE, 0x29, 0x01])
        code = build_poll_with_tod_deadline_code(0xC000, snippet, 0xC1F0, 50)
        # SEC = 0x38, SBC $F2 zp = 0xE5 0xF2, SBC $F3 zp = 0xE5 0xF3
        assert bytes([0x38, 0xE5, ZP_DEADLINE_LO]) in code
        assert bytes([0xE5, ZP_DEADLINE_HI]) in code

    def test_result_success_and_timeout_stores(self) -> None:
        snippet = bytes([0xAD, 0x05, 0xDE, 0x29, 0x01])
        code = build_poll_with_tod_deadline_code(0xC000, snippet, 0xC1F0, 50)
        # Must write 0x01 and 0xFF to $C1F0 (as success/timeout markers).
        # LDA #$01 / STA $C1F0 -> 0xA9 0x01 0x8D 0xF0 0xC1
        assert bytes([0xA9, 0x01, 0x8D, 0xF0, 0xC1]) in code
        assert bytes([0xA9, 0xFF, 0x8D, 0xF0, 0xC1]) in code

    def test_starts_with_sei_and_starts_tod(self) -> None:
        snippet = bytes([0xAD, 0x05, 0xDE, 0x29, 0x01])
        code = build_poll_with_tod_deadline_code(0xC000, snippet, 0xC1F0, 50)
        assert code[0] == 0x78  # SEI
        # Must include AND #$7F (start-TOD mode select)
        assert b"\x29\x7f" in code

    def test_length_within_page(self) -> None:
        """Full poll loop + tables should comfortably fit in one page
        when assembled at a page-aligned address."""
        snippet = bytes([0xAD, 0x05, 0xDE, 0x29, 0x01])
        code = build_poll_with_tod_deadline_code(0xC000, snippet, 0xC1F0, 50)
        assert len(code) < 256, f"code too long: {len(code)}"

    def test_lookup_tables_patched(self) -> None:
        """The three LDA abs,X placeholders must be patched to non-zero
        addresses (the default placeholder is $0000)."""
        snippet = bytes([0xAD, 0x05, 0xDE, 0x29, 0x01])
        code = build_poll_with_tod_deadline_code(0xC000, snippet, 0xC1F0, 50)
        # Count occurrences of LDA abs,X to an operand >= $C000.
        count = 0
        for i in range(len(code) - 2):
            if code[i] == 0xBD:
                addr = code[i + 1] | (code[i + 2] << 8)
                if addr >= 0xC000:
                    count += 1
        assert count == 3, (
            f"expected 3 patched LDA abs,X instructions, found {count}"
        )


class TestCrossRoutineZpLayout:
    """Sanity check: the documented ZP slots match the emitted opcodes."""

    def test_zp_constants(self) -> None:
        assert ZP_CUR_LO == 0xF0
        assert ZP_CUR_HI == 0xF1
        assert ZP_DEADLINE_LO == 0xF2
        assert ZP_DEADLINE_HI == 0xF3
