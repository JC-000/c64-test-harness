#!/usr/bin/env python3
"""Play a C-major scale on the Ultimate 64 Elite at 192.168.1.81 via SID.

Demonstrates the play_sid infrastructure: builds a PSID v2 file in memory
with a 6502 init + play routine + note-frequency table, parses it, then
sends it to the U64's native sid_play REST endpoint.
"""
from __future__ import annotations

import time

from c64_test_harness.sid import SidFile, build_test_psid
from c64_test_harness.sid_player import play_sid
from c64_test_harness.backends.ultimate64 import Ultimate64Transport

LOAD_ADDR = 0x1000

# PAL SID frequency values (0.985 MHz clock), 16-bit little-endian
NOTES = [
    ("C4", 0x1168),
    ("D4", 0x138A),
    ("E4", 0x15EF),
    ("F4", 0x173C),
    ("G4", 0x1A15),
    ("A4", 0x1D47),
    ("B4", 0x20DD),
    ("C5", 0x22D1),
]


def build_init_code() -> bytes:
    """Init routine: set volume, ADSR, reset state, sawtooth no-gate."""
    code = bytes([
        0xA9, 0x0F, 0x8D, 0x18, 0xD4,  # LDA #$0F; STA $D418  volume=15
        0xA9, 0x18, 0x8D, 0x05, 0xD4,  # LDA #$18; STA $D405  AD
        0xA9, 0x89, 0x8D, 0x06, 0xD4,  # LDA #$89; STA $D406  SR
        0xA9, 0x00, 0x8D, 0x60, 0x10,  # LDA #$00; STA $1060  note index
        0xA9, 0x01, 0x8D, 0x61, 0x10,  # LDA #$01; STA $1061  frame counter
        0xA9, 0x20, 0x8D, 0x04, 0xD4,  # LDA #$20; STA $D404  sawtooth, gate off
    ])
    assert len(code) == 30
    return code


def build_play_code(play_addr: int):
    """Play routine advancing one note every 25 IRQ frames (~0.5s at 50Hz).

    Returns (code, note_table_addr).
    """
    play_len = 53
    note_table = play_addr + play_len + 1  # +1 for RTS that build_test_psid appends
    lo = note_table & 0xFF
    hi = (note_table >> 8) & 0xFF

    code = bytes([
        0xCE, 0x61, 0x10,        # DEC $1061
        0xF0, 0x01,              # BEQ +1 (skip RTS)
        0x60,                    # RTS
        0xAE, 0x60, 0x10,        # LDX $1060
        0xE0, 0x08,              # CPX #$08
        0x90, 0x06,              # BCC +6 (skip silence)
        0xA9, 0x00,              # LDA #$00
        0x8D, 0x18, 0xD4,        # STA $D418  volume=0
        0x60,                    # RTS
        0x8A,                    # TXA
        0x0A,                    # ASL
        0xA8,                    # TAY
        0xB9, lo, hi,            # LDA note_table,Y
        0x8D, 0x00, 0xD4,        # STA $D400  freq lo
        0xC8,                    # INY
        0xB9, lo, hi,            # LDA note_table,Y  (Y now +1)
        0x8D, 0x01, 0xD4,        # STA $D401  freq hi
        0xA9, 0x20,              # LDA #$20
        0x8D, 0x04, 0xD4,        # STA $D404  gate off (retrigger)
        0xA9, 0x21,              # LDA #$21
        0x8D, 0x04, 0xD4,        # STA $D404  gate on
        0xEE, 0x60, 0x10,        # INC $1060
        0xA9, 0x19,              # LDA #25
        0x8D, 0x61, 0x10,        # STA $1061
    ])
    assert len(code) == play_len
    return code, note_table


def build_note_table() -> bytes:
    buf = bytearray()
    for _, freq in NOTES:
        buf.append(freq & 0xFF)
        buf.append((freq >> 8) & 0xFF)
    return bytes(buf)


def main() -> None:
    init_code = build_init_code()
    play_addr = LOAD_ADDR + len(init_code) + 1  # +1 for init RTS
    play_code, note_table_addr = build_play_code(play_addr)

    psid = bytearray(build_test_psid(
        load_addr=LOAD_ADDR,
        init_code=init_code,
        play_code=play_code,
        name="SCALE",
        author="HARNESS",
        released="2026",
    ))
    note_bytes = build_note_table()
    psid.extend(note_bytes)
    psid = bytes(psid)

    print(f"PSID size: {len(psid)} bytes")
    print(f"load_addr:   ${LOAD_ADDR:04X}")
    print(f"init_addr:   ${LOAD_ADDR:04X}")
    print(f"play_addr:   ${play_addr:04X}")
    print(f"note_table:  ${note_table_addr:04X}")
    print()
    print("First 128 bytes:")
    for i in range(0, 128, 16):
        hexs = " ".join(f"{b:02x}" for b in psid[i:i+16])
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in psid[i:i+16])
        print(f"  {i:04x}  {hexs:<48}  {asc}")
    print()
    print("Note table bytes (interleaved lo/hi):")
    print("  " + " ".join(f"{b:02x}" for b in note_bytes))
    print()

    sid_path = "/tmp/scale.sid"
    with open(sid_path, "wb") as f:
        f.write(psid)
    print(f"Saved: {sid_path}")

    sf = SidFile.load(sid_path)
    print()
    print("Parsed SidFile:")
    print(f"  format={sf.format} version={sf.version}")
    print(f"  name={sf.name!r}  author={sf.author!r}  released={sf.released!r}")
    print(f"  songs={sf.songs} start_song={sf.start_song} speed={sf.speed}")
    print(f"  load=${sf.load_addr:04X} init=${sf.init_addr:04X} play=${sf.play_addr:04X}")
    print(f"  flags=${sf.flags:04X}")
    assert sf.name == "SCALE"
    assert sf.songs == 1
    assert sf.load_addr == LOAD_ADDR
    assert sf.play_addr == play_addr
    print("  validation OK")
    print()

    print("Connecting to Ultimate 64 at 192.168.1.81 ...")
    transport = Ultimate64Transport(host="192.168.1.81")

    try:
        before = transport.read_memory(LOAD_ADDR, 32)
        print(f"Before sid_play, memory ${LOAD_ADDR:04X}: {before.hex()}")
    except Exception as e:
        print(f"(read_memory before failed: {e})")

    print()
    print("Scale notes to play:")
    for name, freq in NOTES:
        print(f"  {name}  freq=${freq:04X} ({freq})")
    print()

    print("Calling play_sid() ...")
    play_sid(transport, sf)
    print("Playing C-major scale for 5 seconds...")

    time.sleep(0.2)
    try:
        after = transport.read_memory(LOAD_ADDR, len(init_code))
        match = after == init_code
        print(f"After sid_play, memory ${LOAD_ADDR:04X}: {after.hex()}")
        print(f"Matches init_code: {match}")
    except Exception as e:
        print(f"(read_memory after failed: {e})")

    time.sleep(5.3)

    print()
    print("Resetting device to stop audio...")
    transport._client.reset()
    transport.close()
    print("Done.")


if __name__ == "__main__":
    main()
