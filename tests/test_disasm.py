"""A small 6502 disassembler for failure messages.

Its job is to say where the CPU is when a JSR never returns, so the text
must be unambiguous for the instructions the ethernet routines use and
must render the one byte that explains this class of hang: ``00 BRK``.
"""

from __future__ import annotations

from c64_test_harness.disasm import disassemble, instruction_length


def test_instruction_lengths_cover_the_addressing_modes():
    assert instruction_length(0xEA) == 1   # NOP
    assert instruction_length(0xA9) == 2   # LDA #imm
    assert instruction_length(0xAD) == 3   # LDA abs
    assert instruction_length(0xB1) == 2   # LDA (zp),Y
    assert instruction_length(0x20) == 3   # JSR abs
    assert instruction_length(0x60) == 1   # RTS
    assert instruction_length(0x00) == 1   # BRK
    assert instruction_length(0xF0) == 2   # BEQ rel


def test_disassembles_the_clockport_enable_and_a_branch():
    code = bytes([0xAD, 0x01, 0xDE, 0x09, 0x01, 0x8D, 0x01, 0xDE, 0xF0, 0xF9, 0x60])
    lines = disassemble(code, 0xC000)
    assert lines == [
        "C000  AD 01 DE  LDA $DE01",
        "C003  09 01     ORA #$01",
        "C005  8D 01 DE  STA $DE01",
        "C008  F0 F9     BEQ $C003",
        "C00A  60        RTS",
    ]


def test_brk_and_the_bytes_behind_it_are_rendered_not_hidden():
    # What a zeroed first opcode looks like: BRK, then the tail of the old
    # instruction decoded as garbage -- both must be visible.
    lines = disassemble(bytes([0x00, 0x01, 0xDE]), 0xC000)
    assert lines[0] == "C000  00        BRK"
    assert lines[1] == "C001  01 DE     ORA ($DE,X)"


def test_indirect_indexed_and_zero_page_forms():
    lines = disassemble(bytes([0xB1, 0xFB, 0x85, 0xFB, 0xC6, 0xFD, 0xC0, 0x40, 0xA2, 0x07]), 0x1000)
    assert lines == [
        "1000  B1 FB     LDA ($FB),Y",
        "1002  85 FB     STA $FB",
        "1004  C6 FD     DEC $FD",
        "1006  C0 40     CPY #$40",
        "1008  A2 07     LDX #$07",
    ]


def test_truncated_tail_is_rendered_as_bytes_not_an_exception():
    lines = disassemble(bytes([0xEA, 0xAD, 0x01]), 0x2000)
    assert lines[0] == "2000  EA        NOP"
    assert lines[1].startswith("2001  AD 01") and "??" in lines[1]


def test_illegal_opcodes_are_decoded_not_rendered_as_bytes():
    """A 1-byte ``???`` for an illegal opcode desyncs the listing from the
    first illegal instruction on -- exactly where jsr_timeout_report's
    "PC is not an instruction boundary" path is looking.  Every 6510
    opcode has a length; decode it (table ported from issue #170's
    scripts/dis6502.py)."""
    assert disassemble(bytes([0x02]), 0x3000) == ["3000  02        KIL"]
    assert disassemble(bytes([0x87, 0x87]), 0x3000) == ["3000  87 87     SAX $87"]


def test_every_opcode_has_a_length_and_never_desyncs():
    lengths = {op: instruction_length(op) for op in range(256)}
    assert len(lengths) == 256
    assert set(lengths.values()) <= {1, 2, 3}
    for op in range(256):
        lines = disassemble(bytes([op, 0x11, 0x22]) + b"\xEA" * 3, 0x1000)
        consumed = int(lines[1][:4], 16) - 0x1000
        assert consumed == lengths[op], hex(op)


def test_spot_check_a_stream_mixing_legal_and_illegal_opcodes():
    code = bytes.fromhex("22 87 81 4C 6C 60 40 00 78 58 D8 86 AB".replace(" ", ""))
    assert [line[16:] for line in disassemble(code, 0x1000)] == [
        "KIL", "SAX $81", "JMP $606C", "RTI", "BRK", "SEI", "CLI", "CLD", "STX $AB",
    ]
