#!/usr/bin/env python3
"""Play a chromatic scale (C3-C5) on the Ultimate 64 via SID.

Builds a PSID v2 file with 6502 init+play routines and a 25-note
frequency table covering all semitones from C3 through C5.  Instrument
parameters come from c64-sid-instruments grand-piano optimisation.

Usage:
    python3 scripts/play_chromatic_u64.py [--sid 6581|8580]
"""
from __future__ import annotations

import argparse
import time

from c64_test_harness.sid import SidFile, build_test_psid
from c64_test_harness.sid_player import play_sid
from c64_test_harness.backends.ultimate64 import Ultimate64Transport

LOAD_ADDR = 0x1000

# State variable addresses (past all code + note table)
STATE_INDEX = 0x10F0  # current note index (0..24)
STATE_FRAME = 0x10F1  # frame countdown (25 = 0.5s at 50Hz PAL)

# PAL SID frequency register values: freq_reg = round(freq_hz * 2^24 / 985248)
# Equal temperament, A4 = 440 Hz
NOTES = [
    ("C3",  0x08B4),
    ("C#3", 0x0938),
    ("D3",  0x09C4),
    ("D#3", 0x0A59),
    ("E3",  0x0AF7),
    ("F3",  0x0B9D),
    ("F#3", 0x0C4E),
    ("G3",  0x0D0A),
    ("G#3", 0x0DD0),
    ("A3",  0x0EA2),
    ("A#3", 0x0F81),
    ("B3",  0x106D),
    ("C4",  0x1167),
    ("C#4", 0x1270),
    ("D4",  0x1389),
    ("D#4", 0x14B2),
    ("E4",  0x15ED),
    ("F4",  0x173B),
    ("F#4", 0x189C),
    ("G4",  0x1A13),
    ("G#4", 0x1BA0),
    ("A4",  0x1D45),
    ("A#4", 0x1F02),
    ("B4",  0x20DA),
    ("C5",  0x22CE),
]

NUM_NOTES = len(NOTES)  # 25
FRAMES_PER_NOTE = 25     # ~0.5s at 50Hz PAL

# Grand-piano instrument parameters from c64-sid-instruments
# Source: instruments/grand-piano/{chip}/params.json
INSTRUMENT_PARAMS = {
    "6581": {
        "ad":       0xC6,  # attack=12, decay=6
        "sr":       0x9A,  # sustain=9, release=10
        "filt_lo":  0x02,  # filter cutoff 154, low 3 bits
        "filt_hi":  0x13,  # filter cutoff 154, bits 3-10
        "res_filt": 0xB1,  # resonance=11, filter voice 1
        "mode_vol": 0x1F,  # LP mode + volume=15
    },
    "8580": {
        "ad":       0xC6,  # attack=12, decay=6
        "sr":       0x78,  # sustain=7, release=8
        "filt_lo":  0x02,  # filter cutoff 426, low 3 bits
        "filt_hi":  0x35,  # filter cutoff 426, bits 3-10
        "res_filt": 0x41,  # resonance=4, filter voice 1
        "mode_vol": 0x1F,  # LP mode + volume=15
    },
}


def build_init_code(chip: str = "6581") -> bytes:
    """Init routine: set filter, volume, ADSR, reset state, sawtooth no-gate."""
    p = INSTRUMENT_PARAMS[chip]
    code = bytes([
        0xA9, p["filt_lo"],  0x8D, 0x15, 0xD4,  # LDA #; STA $D415 filter cutoff lo
        0xA9, p["filt_hi"],  0x8D, 0x16, 0xD4,  # LDA #; STA $D416 filter cutoff hi
        0xA9, p["res_filt"], 0x8D, 0x17, 0xD4,  # LDA #; STA $D417 resonance+filt
        0xA9, p["mode_vol"], 0x8D, 0x18, 0xD4,  # LDA #; STA $D418 mode+volume
        0xA9, p["ad"],       0x8D, 0x05, 0xD4,  # LDA #; STA $D405 AD
        0xA9, p["sr"],       0x8D, 0x06, 0xD4,  # LDA #; STA $D406 SR
        0xA9, 0x00, 0x8D, STATE_INDEX & 0xFF, (STATE_INDEX >> 8) & 0xFF,
        0xA9, 0x01, 0x8D, STATE_FRAME & 0xFF, (STATE_FRAME >> 8) & 0xFF,
        0xA9, 0x20, 0x8D, 0x04, 0xD4,           # LDA #$20; STA $D404 saw, gate off
    ])
    assert len(code) == 45
    return code


def build_play_code(play_addr: int) -> tuple[bytes, int]:
    """Play routine: advance one note every FRAMES_PER_NOTE IRQ frames.

    Returns (code_bytes, note_table_addr).
    """
    play_len = 53
    note_table = play_addr + play_len + 1  # +1 for RTS appended by build_test_psid
    nt_lo = note_table & 0xFF
    nt_hi = (note_table >> 8) & 0xFF

    code = bytes([
        0xCE, STATE_FRAME & 0xFF, (STATE_FRAME >> 8) & 0xFF,  # DEC frame_counter
        0xF0, 0x01,                        # BEQ +1 (skip RTS)
        0x60,                               # RTS (not time yet)
        0xAE, STATE_INDEX & 0xFF, (STATE_INDEX >> 8) & 0xFF,  # LDX note_index
        0xE0, NUM_NOTES,                   # CPX #25
        0x90, 0x06,                        # BCC +6 (skip silence)
        0xA9, 0x00,                        # LDA #$00
        0x8D, 0x18, 0xD4,                  # STA $D418  silence
        0x60,                               # RTS
        0x8A,                               # TXA
        0x0A,                               # ASL  (*2 for 16-bit entries)
        0xA8,                               # TAY
        0xB9, nt_lo, nt_hi,               # LDA note_table,Y  (freq lo)
        0x8D, 0x00, 0xD4,                  # STA $D400
        0xC8,                               # INY
        0xB9, nt_lo, nt_hi,               # LDA note_table,Y  (freq hi)
        0x8D, 0x01, 0xD4,                  # STA $D401
        0xA9, 0x20,                        # LDA #$20
        0x8D, 0x04, 0xD4,                  # STA $D404  gate off (retrigger)
        0xA9, 0x21,                        # LDA #$21
        0x8D, 0x04, 0xD4,                  # STA $D404  gate on
        0xEE, STATE_INDEX & 0xFF, (STATE_INDEX >> 8) & 0xFF,  # INC note_index
        0xA9, FRAMES_PER_NOTE,             # LDA #25
        0x8D, STATE_FRAME & 0xFF, (STATE_FRAME >> 8) & 0xFF,  # STA frame_counter
    ])
    assert len(code) == play_len
    return code, note_table


def build_note_table() -> bytes:
    """25 little-endian 16-bit frequency values."""
    buf = bytearray()
    for _, freq in NOTES:
        buf.append(freq & 0xFF)
        buf.append((freq >> 8) & 0xFF)
    return bytes(buf)


def build_chromatic_psid(chip: str = "6581") -> tuple[bytes, dict]:
    """Build the complete PSID and return (psid_bytes, metadata_dict)."""
    init_code = build_init_code(chip)
    play_addr = LOAD_ADDR + len(init_code) + 1  # +1 for init RTS
    play_code, note_table_addr = build_play_code(play_addr)

    psid = bytearray(build_test_psid(
        load_addr=LOAD_ADDR,
        init_code=init_code,
        play_code=play_code,
        name="CHROMATIC",
        author="HARNESS",
        released="2026",
    ))
    note_bytes = build_note_table()
    psid.extend(note_bytes)
    psid = bytes(psid)

    meta = {
        "chip": chip,
        "load_addr": LOAD_ADDR,
        "init_addr": LOAD_ADDR,
        "play_addr": play_addr,
        "note_table_addr": note_table_addr,
        "num_notes": NUM_NOTES,
        "frames_per_note": FRAMES_PER_NOTE,
        "total_frames": NUM_NOTES * FRAMES_PER_NOTE,
        "duration_s": NUM_NOTES * FRAMES_PER_NOTE / 50.0,
        "psid_size": len(psid),
    }
    return psid, meta


def main() -> None:
    parser = argparse.ArgumentParser(description="Play chromatic scale C3-C5 on U64")
    parser.add_argument("--sid", choices=["6581", "8580"], default="6581",
                        help="SID chip model for instrument params (default: 6581)")
    parser.add_argument("--host", default="192.168.1.81",
                        help="Ultimate 64 host (default: 192.168.1.81)")
    parser.add_argument("--save", metavar="PATH",
                        help="Save PSID to file (e.g. /tmp/chromatic.sid)")
    args = parser.parse_args()

    psid, meta = build_chromatic_psid(args.sid)

    print(f"Chromatic scale PSID ({args.sid} grand-piano params)")
    print(f"  PSID size:    {meta['psid_size']} bytes")
    print(f"  load_addr:    ${meta['load_addr']:04X}")
    print(f"  init_addr:    ${meta['init_addr']:04X}")
    print(f"  play_addr:    ${meta['play_addr']:04X}")
    print(f"  note_table:   ${meta['note_table_addr']:04X}")
    print(f"  notes:        {meta['num_notes']} ({NOTES[0][0]} - {NOTES[-1][0]})")
    print(f"  duration:     {meta['duration_s']:.1f}s ({meta['total_frames']} frames)")
    print()

    # Hex dump of first 128 bytes
    print("First 128 bytes:")
    for i in range(0, 128, 16):
        hexs = " ".join(f"{b:02x}" for b in psid[i:i + 16])
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in psid[i:i + 16])
        print(f"  {i:04x}  {hexs:<48}  {asc}")
    print()

    # Note table dump
    nt_bytes = build_note_table()
    print("Note table (lo hi pairs):")
    for idx, (name, freq) in enumerate(NOTES):
        lo = nt_bytes[idx * 2]
        hi = nt_bytes[idx * 2 + 1]
        print(f"  {name:4s}  ${freq:04X}  -> {lo:02x} {hi:02x}")
    print()

    if args.save:
        with open(args.save, "wb") as f:
            f.write(psid)
        print(f"Saved: {args.save}")
        print()

    # Parse and validate
    sf = SidFile.from_bytes(psid)
    assert sf.name == "CHROMATIC"
    assert sf.songs == 1
    assert sf.load_addr == LOAD_ADDR
    assert sf.play_addr == meta["play_addr"]
    print(f"SidFile parse OK: {sf.format} v{sf.version}, "
          f"init=${sf.init_addr:04X} play=${sf.play_addr:04X}")
    print()

    # Play on U64
    print(f"Connecting to Ultimate 64 at {args.host} ...")
    transport = Ultimate64Transport(host=args.host)

    print("Calling play_sid() ...")
    play_sid(transport, sf)
    print(f"Playing chromatic scale for {meta['duration_s']:.1f}s ...")

    time.sleep(meta["duration_s"] + 1.0)

    print()
    print("Resetting device to stop audio...")
    transport._client.reset()
    transport.close()
    print("Done.")


if __name__ == "__main__":
    main()
