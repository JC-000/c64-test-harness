"""scripts/dis6502.py must decode every 6502/6510 opcode with the right length.

Motivated by issue #170: the disassembler is what turned VICE's CPU
history and a zero-page dump into a readable chain of events, and its
first version had no illegal opcodes.  ``87`` (SAX zp, 2 bytes) decoded
as a 1-byte ``???``, so a listing desynchronised from the first illegal
instruction on -- exactly where the interesting execution was.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts"))

from dis6502 import LENGTH, dis  # noqa: E402


def one(code: bytes, pc: int = 0x1000) -> str:
    return dis(code, pc, pc, pc + 1)


def length_of(code: bytes) -> int:
    """How many bytes the disassembler consumed for the first instruction."""
    text = dis(code + b"\xea" * 3, 0x1000, 0x1000, 0x1000 + len(code) + 1)
    # the second line starts at 0x1000 + consumed
    second = text.splitlines()[1]
    return int(second[:4], 16) - 0x1000


@pytest.mark.parametrize("code, expected", [
    (bytes([0x22]), "1000  22        KIL"),           # the #170 jam opcode
    (bytes([0x02]), "1000  02        KIL"),
    (bytes([0xF2]), "1000  f2        KIL"),
    (bytes([0x87, 0x87]), "1000  87 87     SAX $87"),  # the #170 zero-page chain
    (bytes([0x81, 0xEB]), "1000  81 eb     STA ($EB,X)"),
])
def test_illegal_and_zero_page_chain_opcodes(code, expected):
    assert one(code) == expected


@pytest.mark.parametrize("opcode, length", [
    (0x22, 1), (0x87, 2), (0x83, 2), (0x8F, 3), (0x97, 2),  # KIL, SAX forms
    (0xA7, 2), (0xBF, 3), (0xEB, 2), (0x1A, 1), (0x0C, 3),  # LAX, SBC imm, NOPs
    (0x1C, 3), (0x04, 2), (0x80, 2), (0xFF, 3), (0xD3, 2),  # NOP absx/zp/imm, ISC, DCP
])
def test_illegal_opcode_lengths(opcode, length):
    assert LENGTH[opcode] == length
    assert length_of(bytes([opcode, 0x11, 0x22])) == length


@pytest.mark.parametrize("code, expected", [
    (bytes([0xEA]), "1000  ea        NOP"),
    (bytes([0x60]), "1000  60        RTS"),
    (bytes([0x00]), "1000  00        BRK"),
    (bytes([0xA9, 0x30]), "1000  a9 30     LDA #$30"),
    (bytes([0x86, 0x86]), "1000  86 86     STX $86"),
    (bytes([0xE6, 0x7A]), "1000  e6 7a     INC $7A"),
    (bytes([0xAD, 0x00, 0x02]), "1000  ad 00 02  LDA $0200"),
    (bytes([0x6C, 0x02, 0xA0]), "1000  6c 02 a0  JMP ($A002)"),
    (bytes([0xD0, 0xF7]), "1000  d0 f7     BNE $0FF9"),
    (bytes([0xB1, 0xD1]), "1000  b1 d1     LDA ($D1),Y"),
])
def test_legal_spot_checks(code, expected):
    assert one(code) == expected


def test_every_opcode_has_a_length_and_never_desyncs():
    assert len(LENGTH) == 256
    assert set(LENGTH.values()) <= {1, 2, 3}
    for op in range(256):
        assert length_of(bytes([op, 0x11, 0x22])) == LENGTH[op], hex(op)
