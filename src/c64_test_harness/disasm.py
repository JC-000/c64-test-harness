"""A small 6502 disassembler for failure messages.

When a JSR into a test routine never returns, the useful fact is *where*
the CPU is and *what it is executing there*.  This renders a window of
memory as one line per instruction in the classic monitor layout::

    C000  AD 01 DE  LDA $DE01
    C003  09 01     ORA #$01
    C008  F0 F9     BEQ $C003

Only the documented opcodes are decoded; anything else is rendered as its
byte and ``???`` so a corrupted routine (a zeroed opcode is ``00 BRK``)
is visible rather than hidden.  No cycle counts, no labels -- this is a
diagnostic, not a tool.
"""

from __future__ import annotations

__all__ = ["disassemble", "instruction_length"]

# opcode -> (mnemonic, addressing mode)
_OPS: dict[int, tuple[str, str]] = {}


def _add(mn: str, modes: dict[int, str]) -> None:
    for op, mode in modes.items():
        _OPS[op] = (mn, mode)


_add("ADC", {0x69: "imm", 0x65: "zp", 0x75: "zpx", 0x6D: "abs", 0x7D: "absx", 0x79: "absy", 0x61: "indx", 0x71: "indy"})
_add("AND", {0x29: "imm", 0x25: "zp", 0x35: "zpx", 0x2D: "abs", 0x3D: "absx", 0x39: "absy", 0x21: "indx", 0x31: "indy"})
_add("ASL", {0x0A: "acc", 0x06: "zp", 0x16: "zpx", 0x0E: "abs", 0x1E: "absx"})
_add("BIT", {0x24: "zp", 0x2C: "abs"})
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
for _op, _mn in {0x10: "BPL", 0x30: "BMI", 0x50: "BVC", 0x70: "BVS",
                 0x90: "BCC", 0xB0: "BCS", 0xD0: "BNE", 0xF0: "BEQ"}.items():
    _OPS[_op] = (_mn, "rel")
for _op, _mn in {0x00: "BRK", 0x18: "CLC", 0xD8: "CLD", 0x58: "CLI", 0xB8: "CLV",
                 0xCA: "DEX", 0x88: "DEY", 0xE8: "INX", 0xC8: "INY", 0xEA: "NOP",
                 0x48: "PHA", 0x08: "PHP", 0x68: "PLA", 0x28: "PLP", 0x40: "RTI",
                 0x60: "RTS", 0x38: "SEC", 0xF8: "SED", 0x78: "SEI", 0xAA: "TAX",
                 0xA8: "TAY", 0xBA: "TSX", 0x8A: "TXA", 0x9A: "TXS", 0x98: "TYA"}.items():
    _OPS[_op] = (_mn, "imp")

_LEN = {"imp": 1, "acc": 1, "imm": 2, "zp": 2, "zpx": 2, "zpy": 2, "rel": 2,
        "abs": 3, "absx": 3, "absy": 3, "ind": 3, "indx": 2, "indy": 2}


def instruction_length(opcode: int) -> int:
    """Byte length of the instruction starting with *opcode* (1 for undocumented)."""
    return _LEN[_OPS.get(opcode, ("???", "imp"))[1]]


def _operand(mode: str, b: bytes, pc: int) -> str:
    if mode == "imp":
        return ""
    if mode == "acc":
        return "A"
    if mode == "imm":
        return f"#${b[1]:02X}"
    if mode in ("zp", "zpx", "zpy"):
        return f"${b[1]:02X}" + {"zp": "", "zpx": ",X", "zpy": ",Y"}[mode]
    if mode == "rel":
        off = b[1] - 256 if b[1] > 127 else b[1]
        return f"${(pc + 2 + off) & 0xFFFF:04X}"
    if mode == "indx":
        return f"(${b[1]:02X},X)"
    if mode == "indy":
        return f"(${b[1]:02X}),Y"
    w = b[1] | (b[2] << 8)
    if mode == "ind":
        return f"(${w:04X})"
    return f"${w:04X}" + {"abs": "", "absx": ",X", "absy": ",Y"}[mode]


def disassemble(mem: bytes, base: int) -> list[str]:
    """One line per instruction for *mem*, which starts at address *base*.

    A trailing instruction cut short by the end of *mem* is rendered with
    the bytes present and ``??`` for the missing operand.
    """
    out: list[str] = []
    i, n = 0, len(mem)
    while i < n:
        pc = (base + i) & 0xFFFF
        op = mem[i]
        mn, mode = _OPS.get(op, ("???", "imp"))
        length = _LEN[mode]
        b = bytes(mem[i:i + length])
        if len(b) < length:
            shown = b.hex(" ").upper() + " ??" * (length - len(b))
            out.append(f"{pc:04X}  {shown:<9} {mn} ??")
            break
        text = f"{mn} {_operand(mode, b, pc)}".rstrip()
        out.append(f"{pc:04X}  {b.hex(' ').upper():<9} {text}")
        i += length
    return out
