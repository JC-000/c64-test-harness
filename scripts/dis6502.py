import sys
# minimal 6502 disassembler: opcode -> (mnemonic, mode)
M = {}
def add(mn, d):
    for op, mode in d.items():
        M[op] = (mn, mode)
add("ADC",{0x69:"imm",0x65:"zp",0x75:"zpx",0x6D:"abs",0x7D:"absx",0x79:"absy",0x61:"indx",0x71:"indy"})
add("AND",{0x29:"imm",0x25:"zp",0x35:"zpx",0x2D:"abs",0x3D:"absx",0x39:"absy",0x21:"indx",0x31:"indy"})
add("ASL",{0x0A:"acc",0x06:"zp",0x16:"zpx",0x0E:"abs",0x1E:"absx"})
add("BIT",{0x24:"zp",0x2C:"abs"})
for op,mn in {0x10:"BPL",0x30:"BMI",0x50:"BVC",0x70:"BVS",0x90:"BCC",0xB0:"BCS",0xD0:"BNE",0xF0:"BEQ"}.items(): M[op]=(mn,"rel")
for op,mn in {0x00:"BRK",0x18:"CLC",0xD8:"CLD",0x58:"CLI",0xB8:"CLV",0xCA:"DEX",0x88:"DEY",0xE8:"INX",0xC8:"INY",0xEA:"NOP",0x48:"PHA",0x08:"PHP",0x68:"PLA",0x28:"PLP",0x40:"RTI",0x60:"RTS",0x38:"SEC",0xF8:"SED",0x78:"SEI",0xAA:"TAX",0xA8:"TAY",0xBA:"TSX",0x8A:"TXA",0x9A:"TXS",0x98:"TYA"}.items(): M[op]=(mn,"imp")
add("CMP",{0xC9:"imm",0xC5:"zp",0xD5:"zpx",0xCD:"abs",0xDD:"absx",0xD9:"absy",0xC1:"indx",0xD1:"indy"})
add("CPX",{0xE0:"imm",0xE4:"zp",0xEC:"abs"}); add("CPY",{0xC0:"imm",0xC4:"zp",0xCC:"abs"})
add("DEC",{0xC6:"zp",0xD6:"zpx",0xCE:"abs",0xDE:"absx"}); add("INC",{0xE6:"zp",0xF6:"zpx",0xEE:"abs",0xFE:"absx"})
add("EOR",{0x49:"imm",0x45:"zp",0x55:"zpx",0x4D:"abs",0x5D:"absx",0x59:"absy",0x41:"indx",0x51:"indy"})
add("JMP",{0x4C:"abs",0x6C:"ind"}); add("JSR",{0x20:"abs"})
add("LDA",{0xA9:"imm",0xA5:"zp",0xB5:"zpx",0xAD:"abs",0xBD:"absx",0xB9:"absy",0xA1:"indx",0xB1:"indy"})
add("LDX",{0xA2:"imm",0xA6:"zp",0xB6:"zpy",0xAE:"abs",0xBE:"absy"}); add("LDY",{0xA0:"imm",0xA4:"zp",0xB4:"zpx",0xAC:"abs",0xBC:"absx"})
add("LSR",{0x4A:"acc",0x46:"zp",0x56:"zpx",0x4E:"abs",0x5E:"absx"})
add("ORA",{0x09:"imm",0x05:"zp",0x15:"zpx",0x0D:"abs",0x1D:"absx",0x19:"absy",0x01:"indx",0x11:"indy"})
add("ROL",{0x2A:"acc",0x26:"zp",0x36:"zpx",0x2E:"abs",0x3E:"absx"}); add("ROR",{0x6A:"acc",0x66:"zp",0x76:"zpx",0x6E:"abs",0x7E:"absx"})
add("SBC",{0xE9:"imm",0xE5:"zp",0xF5:"zpx",0xED:"abs",0xFD:"absx",0xF9:"absy",0xE1:"indx",0xF1:"indy"})
add("STA",{0x85:"zp",0x95:"zpx",0x8D:"abs",0x9D:"absx",0x99:"absy",0x81:"indx",0x91:"indy"})
add("STX",{0x86:"zp",0x96:"zpy",0x8E:"abs"}); add("STY",{0x84:"zp",0x94:"zpx",0x8C:"abs"})
L = {"imp":1,"acc":1,"imm":2,"zp":2,"zpx":2,"zpy":2,"rel":2,"abs":3,"absx":3,"absy":3,"ind":3,"indx":2,"indy":2}

def dis(mem, base, start, end):
    pc = start; out = []
    while pc < end:
        op = mem[pc-base]; mn, mode = M.get(op, ("???","imp")); n = L[mode]
        b = mem[pc-base:pc-base+n]
        if mode == "imp": a = ""
        elif mode == "acc": a = "A"
        elif mode == "imm": a = f"#${b[1]:02X}"
        elif mode in ("zp","zpx","zpy"): a = f"${b[1]:02X}" + {"zp":"","zpx":",X","zpy":",Y"}[mode]
        elif mode == "rel": a = f"${(pc+2+(b[1]-256 if b[1]>127 else b[1]))&0xFFFF:04X}"
        elif mode == "indx": a = f"(${b[1]:02X},X)"
        elif mode == "indy": a = f"(${b[1]:02X}),Y"
        else:
            w = b[1] | (b[2] << 8); a = f"${w:04X}" + {"abs":"","absx":",X","absy":",Y"}.get(mode,"")
            if mode == "ind": a = f"(${w:04X})"
        out.append(f"{pc:04X}  {b.hex(' '):<9} {mn} {a}"); pc += n
    return "\n".join(out)

if __name__ == "__main__":
    rom = open(sys.argv[1], "rb").read(); base = int(sys.argv[2], 16)
    for rng in sys.argv[3:]:
        s, e = (int(x, 16) for x in rng.split("-")); print(f"--- {rng}"); print(dis(rom, base, s, e))
