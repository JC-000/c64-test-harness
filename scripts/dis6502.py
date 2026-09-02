"""Minimal 6502/6510 disassembler, all 256 opcodes (illegal ones included).

Used by scripts/vice_keyecho_probe.py to render VICE's CPU history and by
hand on memory dumps.  Every opcode has a length, so a listing never
desynchronises at an illegal instruction -- which is exactly where the
interesting execution was in issue #170 (``87`` = SAX zp, 2 bytes).

    python3 scripts/dis6502.py <rom.bin> <base-hex> <start-end> [<start-end> ...]

Illegal mnemonics follow the usual names: KIL SLO RLA SRE RRA SAX LAX
DCP ISC ANC ALR ARR XAA AHX TAS SHY SHX LAS AXS, plus NOP variants.
"""
from __future__ import annotations

import sys

# mode -> instruction length
_LEN = {"imp": 1, "acc": 1, "imm": 2, "zp": 2, "zpx": 2, "zpy": 2, "rel": 2,
        "abs": 3, "absx": 3, "absy": 3, "ind": 3, "indx": 2, "indy": 2}

M: dict[int, tuple[str, str]] = {}


def _add(mn: str, d: dict[int, str]) -> None:
    for op, mode in d.items():
        M[op] = (mn, mode)


# --- legal -----------------------------------------------------------------
_add("ADC", {0x69: "imm", 0x65: "zp", 0x75: "zpx", 0x6D: "abs", 0x7D: "absx", 0x79: "absy", 0x61: "indx", 0x71: "indy"})
_add("AND", {0x29: "imm", 0x25: "zp", 0x35: "zpx", 0x2D: "abs", 0x3D: "absx", 0x39: "absy", 0x21: "indx", 0x31: "indy"})
_add("ASL", {0x0A: "acc", 0x06: "zp", 0x16: "zpx", 0x0E: "abs", 0x1E: "absx"})
_add("BIT", {0x24: "zp", 0x2C: "abs"})
for _op, _mn in {0x10: "BPL", 0x30: "BMI", 0x50: "BVC", 0x70: "BVS", 0x90: "BCC", 0xB0: "BCS", 0xD0: "BNE", 0xF0: "BEQ"}.items():
    M[_op] = (_mn, "rel")
for _op, _mn in {0x00: "BRK", 0x18: "CLC", 0xD8: "CLD", 0x58: "CLI", 0xB8: "CLV", 0xCA: "DEX", 0x88: "DEY",
                 0xE8: "INX", 0xC8: "INY", 0xEA: "NOP", 0x48: "PHA", 0x08: "PHP", 0x68: "PLA", 0x28: "PLP",
                 0x40: "RTI", 0x60: "RTS", 0x38: "SEC", 0xF8: "SED", 0x78: "SEI", 0xAA: "TAX", 0xA8: "TAY",
                 0xBA: "TSX", 0x8A: "TXA", 0x9A: "TXS", 0x98: "TYA"}.items():
    M[_op] = (_mn, "imp")
_add("CMP", {0xC9: "imm", 0xC5: "zp", 0xD5: "zpx", 0xCD: "abs", 0xDD: "absx", 0xD9: "absy", 0xC1: "indx", 0xD1: "indy"})
_add("CPX", {0xE0: "imm", 0xE4: "zp", 0xEC: "abs"})
_add("CPY", {0xC0: "imm", 0xC4: "zp", 0xCC: "abs"})
_add("DEC", {0xC6: "zp", 0xD6: "zpx", 0xCE: "abs", 0xDE: "absx"})
_add("INC", {0xE6: "zp", 0xF6: "zpx", 0xEE: "abs", 0xFE: "absx"})
_add("EOR", {0x49: "imm", 0x45: "zp", 0x55: "zpx", 0x4D: "abs", 0x5D: "absx", 0x59: "absy", 0x41: "indx", 0x51: "indy"})
_add("JMP", {0x4C: "abs", 0x6C: "ind"})
_add("JSR", {0x20: "abs"})
_add("LDA", {0xA9: "imm", 0xA5: "zp", 0xB5: "zpx", 0xAD: "abs", 0xBD: "absx", 0xB9: "absy", 0xA1: "indx", 0xB1: "indy"})
_add("LDX", {0xA2: "imm", 0xA6: "zp", 0xB6: "zpy", 0xAE: "abs", 0xBE: "absy"})
_add("LDY", {0xA0: "imm", 0xA4: "zp", 0xB4: "zpx", 0xAC: "abs", 0xBC: "absx"})
_add("LSR", {0x4A: "acc", 0x46: "zp", 0x56: "zpx", 0x4E: "abs", 0x5E: "absx"})
_add("ORA", {0x09: "imm", 0x05: "zp", 0x15: "zpx", 0x0D: "abs", 0x1D: "absx", 0x19: "absy", 0x01: "indx", 0x11: "indy"})
_add("ROL", {0x2A: "acc", 0x26: "zp", 0x36: "zpx", 0x2E: "abs", 0x3E: "absx"})
_add("ROR", {0x6A: "acc", 0x66: "zp", 0x76: "zpx", 0x6E: "abs", 0x7E: "absx"})
_add("SBC", {0xE9: "imm", 0xE5: "zp", 0xF5: "zpx", 0xED: "abs", 0xFD: "absx", 0xF9: "absy", 0xE1: "indx", 0xF1: "indy"})
_add("STA", {0x85: "zp", 0x95: "zpx", 0x8D: "abs", 0x9D: "absx", 0x99: "absy", 0x81: "indx", 0x91: "indy"})
_add("STX", {0x86: "zp", 0x96: "zpy", 0x8E: "abs"})
_add("STY", {0x84: "zp", 0x94: "zpx", 0x8C: "abs"})

# --- illegal ---------------------------------------------------------------
for _op in (0x02, 0x12, 0x22, 0x32, 0x42, 0x52, 0x62, 0x72, 0x92, 0xB2, 0xD2, 0xF2):
    M[_op] = ("KIL", "imp")
for _op in (0x1A, 0x3A, 0x5A, 0x7A, 0xDA, 0xFA):
    M[_op] = ("NOP", "imp")
_add("NOP", {0x80: "imm", 0x82: "imm", 0x89: "imm", 0xC2: "imm", 0xE2: "imm",
             0x04: "zp", 0x44: "zp", 0x64: "zp",
             0x14: "zpx", 0x34: "zpx", 0x54: "zpx", 0x74: "zpx", 0xD4: "zpx", 0xF4: "zpx",
             0x0C: "abs", 0x1C: "absx", 0x3C: "absx", 0x5C: "absx", 0x7C: "absx", 0xDC: "absx", 0xFC: "absx"})
# read-modify-write family: columns 3/7/B/F of rows x0 and x1
for _row, _mn in ((0x00, "SLO"), (0x20, "RLA"), (0x40, "SRE"), (0x60, "RRA"), (0xC0, "DCP"), (0xE0, "ISC")):
    _add(_mn, {_row + 0x03: "indx", _row + 0x07: "zp", _row + 0x0F: "abs",
               _row + 0x13: "indy", _row + 0x17: "zpx", _row + 0x1B: "absy", _row + 0x1F: "absx"})
_add("SAX", {0x83: "indx", 0x87: "zp", 0x8F: "abs", 0x97: "zpy"})
_add("LAX", {0xAB: "imm", 0xA3: "indx", 0xA7: "zp", 0xAF: "abs", 0xB3: "indy", 0xB7: "zpy", 0xBF: "absy"})
_add("ANC", {0x0B: "imm", 0x2B: "imm"})
_add("ALR", {0x4B: "imm"})
_add("ARR", {0x6B: "imm"})
_add("XAA", {0x8B: "imm"})
_add("AHX", {0x93: "indy", 0x9F: "absy"})
_add("TAS", {0x9B: "absy"})
_add("SHY", {0x9C: "absx"})
_add("SHX", {0x9E: "absy"})
_add("LAS", {0xBB: "absy"})
_add("AXS", {0xCB: "imm"})
_add("SBC", {0xEB: "imm"})

assert len(M) == 256, f"opcode table has {len(M)} entries"

#: opcode -> byte length, for every opcode
LENGTH: dict[int, int] = {op: _LEN[mode] for op, (_, mode) in M.items()}


def dis(mem: bytes, base: int, start: int, end: int) -> str:
    """Disassemble ``mem`` (which starts at address ``base``) from start to end."""
    pc = start
    out = []
    while pc < end:
        op = mem[pc - base]
        mn, mode = M[op]
        n = _LEN[mode]
        b = mem[pc - base:pc - base + n]
        if len(b) < n:  # truncated tail
            out.append(f"{pc:04X}  {b.hex(' '):<9} .byte")
            break
        if mode == "imp":
            a = ""
        elif mode == "acc":
            a = "A"
        elif mode == "imm":
            a = f"#${b[1]:02X}"
        elif mode in ("zp", "zpx", "zpy"):
            a = f"${b[1]:02X}" + {"zp": "", "zpx": ",X", "zpy": ",Y"}[mode]
        elif mode == "rel":
            a = f"${(pc + 2 + (b[1] - 256 if b[1] > 127 else b[1])) & 0xFFFF:04X}"
        elif mode == "indx":
            a = f"(${b[1]:02X},X)"
        elif mode == "indy":
            a = f"(${b[1]:02X}),Y"
        else:
            w = b[1] | (b[2] << 8)
            a = f"(${w:04X})" if mode == "ind" else f"${w:04X}" + {"abs": "", "absx": ",X", "absy": ",Y"}[mode]
        out.append(f"{pc:04X}  {b.hex(' '):<9} {mn}{' ' + a if a else ''}")
        pc += n
    return "\n".join(out)


if __name__ == "__main__":
    rom = open(sys.argv[1], "rb").read()
    base = int(sys.argv[2], 16)
    for rng in sys.argv[3:]:
        s, e = (int(x, 16) for x in rng.split("-"))
        print(f"--- {rng}")
        print(dis(rom, base, s, e))
