"""Tests for PSID/RSID parser (c64_test_harness.sid)."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from c64_test_harness.sid import (
    SidError,
    SidFile,
    SidFormatError,
    build_test_psid,
)


def _manual_psid_v1(
    *,
    load_addr: int = 0x1000,
    init_addr: int = 0x1000,
    play_addr: int = 0x1003,
    songs: int = 1,
    start_song: int = 1,
    speed: int = 0,
    name: bytes = b"V1SONG",
    author: bytes = b"SOMEONE",
    released: bytes = b"1985",
    data: bytes = b"\x60\x60",
) -> bytes:
    """Build a PSID v1 file manually (for tests)."""
    header = bytearray(0x76)
    header[0:4] = b"PSID"
    struct.pack_into(">H", header, 4, 1)
    struct.pack_into(">H", header, 6, 0x0076)
    struct.pack_into(">H", header, 8, load_addr)
    struct.pack_into(">H", header, 10, init_addr)
    struct.pack_into(">H", header, 12, play_addr)
    struct.pack_into(">H", header, 14, songs)
    struct.pack_into(">H", header, 16, start_song)
    struct.pack_into(">I", header, 18, speed)
    header[22:22 + len(name)] = name
    header[54:54 + len(author)] = author
    header[86:86 + len(released)] = released
    return bytes(header) + data


# ---------------------------------------------------------------------------
# Basic parsing
# ---------------------------------------------------------------------------


def test_parse_build_test_psid_v2():
    raw = build_test_psid(load_addr=0x1000, name="HELLO", author="ME", released="2026")
    sid = SidFile.from_bytes(raw)
    assert sid.format == "PSID"
    assert sid.version == 2
    assert sid.data_offset == 0x7C
    assert sid.load_addr == 0x1000
    assert sid.name == "HELLO"
    assert sid.author == "ME"
    assert sid.released == "2026"
    assert sid.songs == 1
    assert sid.start_song == 1


def test_parse_manual_psid_v1():
    raw = _manual_psid_v1(load_addr=0x2000, name=b"CLASSIC")
    sid = SidFile.from_bytes(raw)
    assert sid.format == "PSID"
    assert sid.version == 1
    assert sid.data_offset == 0x76
    assert sid.load_addr == 0x2000
    assert sid.name == "CLASSIC"
    # v2+ fields default to zero on v1.
    assert sid.flags == 0
    assert sid.start_page == 0
    assert sid.page_length == 0
    assert sid.second_sid_address == 0
    assert sid.third_sid_address == 0


def test_rsid_magic_accepted():
    raw = bytearray(build_test_psid())
    raw[0:4] = b"RSID"
    sid = SidFile.from_bytes(bytes(raw))
    assert sid.format == "RSID"


# ---------------------------------------------------------------------------
# Validation / error cases
# ---------------------------------------------------------------------------


def test_invalid_magic_raises():
    raw = bytearray(build_test_psid())
    raw[0:4] = b"XXXX"
    with pytest.raises(SidFormatError):
        SidFile.from_bytes(bytes(raw))


def test_truncated_file_raises():
    raw = build_test_psid()
    with pytest.raises(SidFormatError):
        SidFile.from_bytes(raw[:10])


def test_empty_file_raises():
    with pytest.raises(SidFormatError):
        SidFile.from_bytes(b"")


def test_invalid_version_5_raises():
    raw = bytearray(build_test_psid())
    struct.pack_into(">H", raw, 4, 5)
    with pytest.raises(SidFormatError):
        SidFile.from_bytes(bytes(raw))


def test_invalid_version_0_raises():
    raw = bytearray(build_test_psid())
    struct.pack_into(">H", raw, 4, 0)
    with pytest.raises(SidFormatError):
        SidFile.from_bytes(bytes(raw))


def test_wrong_data_offset_raises():
    raw = bytearray(build_test_psid(version=2))
    # v2 should have 0x7C; set to 0x76 to trigger mismatch.
    struct.pack_into(">H", raw, 6, 0x0076)
    with pytest.raises(SidFormatError):
        SidFile.from_bytes(bytes(raw))


def test_sid_error_is_base_class():
    assert issubclass(SidFormatError, SidError)


# ---------------------------------------------------------------------------
# Text field decoding
# ---------------------------------------------------------------------------


def test_iso8859_decoding():
    raw = build_test_psid(name="Bjørk", author="Sögur", released="Éclair")
    sid = SidFile.from_bytes(raw)
    assert sid.name == "Bjørk"
    assert sid.author == "Sögur"
    assert sid.released == "Éclair"


def test_text_strips_null_and_whitespace():
    raw = build_test_psid(name="Hello   ")
    sid = SidFile.from_bytes(raw)
    assert sid.name == "Hello"


# ---------------------------------------------------------------------------
# Load-address handling
# ---------------------------------------------------------------------------


def test_effective_load_addr_direct():
    raw = build_test_psid(load_addr=0x4000)
    sid = SidFile.from_bytes(raw)
    assert sid.load_addr == 0x4000
    assert sid.effective_load_addr == 0x4000


def test_effective_load_addr_embedded():
    # Build a PSID with load_addr=0 by rewriting the header and inserting
    # an LE prefix at the start of the data area.
    raw = bytearray(build_test_psid(load_addr=0x1000))
    struct.pack_into(">H", raw, 8, 0)  # clear header load addr
    # Prepend LE address 0x2345 to the data area.
    data_offset = 0x7C
    new_raw = bytes(raw[:data_offset]) + b"\x45\x23" + bytes(raw[data_offset:])
    sid = SidFile.from_bytes(new_raw)
    assert sid.load_addr == 0
    assert sid.effective_load_addr == 0x2345


def test_c64_data_strips_embedded_prefix():
    raw = bytearray(build_test_psid(load_addr=0x1000))
    struct.pack_into(">H", raw, 8, 0)
    data_offset = 0x7C
    payload = b"\x45\x23" + b"\xAA\xBB\xCC"
    new_raw = bytes(raw[:data_offset]) + payload
    sid = SidFile.from_bytes(new_raw)
    assert sid.c64_data == b"\xAA\xBB\xCC"


def test_c64_data_direct_load_addr():
    raw = build_test_psid(load_addr=0x1000, init_code=b"", play_code=b"")
    sid = SidFile.from_bytes(raw)
    # init routine = \x60, play routine = \x60 → data = \x60\x60
    assert sid.c64_data == b"\x60\x60"


# ---------------------------------------------------------------------------
# Speed bitfield
# ---------------------------------------------------------------------------


def test_song_is_60hz_bit_zero():
    raw = bytearray(build_test_psid())
    struct.pack_into(">I", raw, 18, 0b0001)
    sid = SidFile.from_bytes(bytes(raw))
    assert sid.song_is_60hz(0) is True
    assert sid.song_is_60hz(1) is False


def test_song_is_60hz_bit_one():
    raw = bytearray(build_test_psid())
    struct.pack_into(">I", raw, 18, 0b0010)
    sid = SidFile.from_bytes(bytes(raw))
    assert sid.song_is_60hz(0) is False
    assert sid.song_is_60hz(1) is True


def test_song_is_60hz_wraps_past_32():
    raw = bytearray(build_test_psid())
    struct.pack_into(">I", raw, 18, 0b0001)  # bit 0 set
    sid = SidFile.from_bytes(bytes(raw))
    assert sid.song_is_60hz(32) is True  # 32 % 32 == 0
    assert sid.song_is_60hz(33) is False


# ---------------------------------------------------------------------------
# build_test_psid round-trip
# ---------------------------------------------------------------------------


def test_build_test_psid_addresses():
    init_code = b"\xA9\x00\x8D\x20\xD0"  # LDA #0 / STA $D020
    play_code = b"\xEE\x20\xD0"          # INC $D020
    raw = build_test_psid(
        load_addr=0x3000, init_code=init_code, play_code=play_code
    )
    sid = SidFile.from_bytes(raw)
    assert sid.load_addr == 0x3000
    assert sid.init_addr == 0x3000
    # play_addr = load_addr + len(init_code) + 1 (the RTS byte)
    assert sid.play_addr == 0x3000 + len(init_code) + 1


def test_build_test_psid_payload_layout():
    init_code = b"\xA9\x01"
    play_code = b"\xA9\x02"
    raw = build_test_psid(
        load_addr=0x1000, init_code=init_code, play_code=play_code
    )
    sid = SidFile.from_bytes(raw)
    expected = init_code + b"\x60" + play_code + b"\x60"
    assert sid.c64_data == expected


def test_build_test_psid_v1():
    raw = build_test_psid(version=1)
    sid = SidFile.from_bytes(raw)
    assert sid.version == 1
    assert sid.data_offset == 0x76
    assert sid.flags == 0


def test_build_test_psid_zero_load_addr_rejected():
    with pytest.raises(ValueError):
        build_test_psid(load_addr=0)


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------


def test_load_from_disk(tmp_path: Path):
    raw = build_test_psid(name="DISKTEST")
    sid_path = tmp_path / "test.sid"
    sid_path.write_bytes(raw)

    sid = SidFile.load(sid_path)
    assert sid.name == "DISKTEST"
    assert sid.raw == raw


def test_load_accepts_str_path(tmp_path: Path):
    raw = build_test_psid()
    sid_path = tmp_path / "test.sid"
    sid_path.write_bytes(raw)
    sid = SidFile.load(str(sid_path))
    assert sid.format == "PSID"


# ---------------------------------------------------------------------------
# Raw preservation
# ---------------------------------------------------------------------------


def test_raw_bytes_preserved_exactly():
    raw = build_test_psid(name="PRESERVE")
    sid = SidFile.from_bytes(raw)
    assert sid.raw is raw or sid.raw == raw
    assert bytes(sid.raw) == raw


def test_v2_fields_populated():
    raw = bytearray(build_test_psid(version=2))
    # set flags = 0x001A, startPage=0x20, pageLength=0x10, 2nd=0x42, 3rd=0x46
    struct.pack_into(">H", raw, 118, 0x001A)
    raw[120] = 0x20
    raw[121] = 0x10
    raw[122] = 0x42
    raw[123] = 0x46
    sid = SidFile.from_bytes(bytes(raw))
    assert sid.flags == 0x001A
    assert sid.start_page == 0x20
    assert sid.page_length == 0x10
    assert sid.second_sid_address == 0x42
    assert sid.third_sid_address == 0x46
