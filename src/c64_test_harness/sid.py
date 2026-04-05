"""PSID/RSID file parser for SID music files.

Parses the PSID/RSID header format (v1 through v4) used to distribute
Commodore 64 SID music. The parsed ``SidFile`` retains the original
file bytes so backends can re-serialize the file (e.g. for Ultimate 64
replay) without re-synthesizing the header.

Reference: HVSC PSIDv2NG specification.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path


class SidError(Exception):
    """Base exception for SID file errors."""


class SidFormatError(SidError):
    """Raised when a PSID/RSID file is malformed or has invalid fields."""


_PSID_MAGIC = b"PSID"
_RSID_MAGIC = b"RSID"
_V1_DATA_OFFSET = 0x0076
_V2_DATA_OFFSET = 0x007C
_VALID_VERSIONS = (1, 2, 3, 4)


def _decode_text(raw_bytes: bytes) -> str:
    """Decode a fixed-width ISO-8859-1 text field.

    Strips the trailing null terminator and any trailing whitespace.
    """
    return raw_bytes.decode("iso-8859-1").rstrip("\x00").rstrip()


@dataclass(frozen=True)
class SidFile:
    """A parsed PSID or RSID file.

    The original file bytes are retained in ``raw`` so backends can
    forward the file verbatim to hardware replay endpoints that expect
    a complete PSID/RSID blob.
    """

    raw: bytes
    format: str
    version: int
    data_offset: int
    load_addr: int
    init_addr: int
    play_addr: int
    songs: int
    start_song: int
    speed: int
    name: str
    author: str
    released: str
    flags: int
    start_page: int
    page_length: int
    second_sid_address: int
    third_sid_address: int

    @property
    def c64_data(self) -> bytes:
        """Return just the 6502 code/data, stripping any embedded load address prefix."""
        if self.load_addr == 0:
            return self.raw[self.data_offset + 2:]
        return self.raw[self.data_offset:]

    @property
    def effective_load_addr(self) -> int:
        """Resolved load address.

        If the header ``load_addr`` is 0, the real load address is stored
        as a little-endian 16-bit prefix at the start of the data area.
        """
        if self.load_addr != 0:
            return self.load_addr
        lo = self.raw[self.data_offset]
        hi = self.raw[self.data_offset + 1]
        return lo | (hi << 8)

    def song_is_60hz(self, song_index: int) -> bool:
        """Return True if the given 0-based song index plays at 60 Hz (CIA).

        Bit N (0..31) of the 32-bit ``speed`` bitfield selects the timing
        for song N+1. For songs beyond 32 the bit index wraps.
        """
        bit = song_index % 32
        return bool(self.speed & (1 << bit))

    @classmethod
    def from_bytes(cls, raw: bytes) -> "SidFile":
        """Parse a PSID/RSID file from raw bytes.

        Raises:
            SidFormatError: if the magic, version, offset, or overall
                length is invalid.
        """
        if len(raw) < 8:
            raise SidFormatError(
                f"file too short for header ({len(raw)} bytes)"
            )

        magic = raw[0:4]
        if magic == _PSID_MAGIC:
            fmt = "PSID"
        elif magic == _RSID_MAGIC:
            fmt = "RSID"
        else:
            raise SidFormatError(
                f"invalid magic: {magic!r} (expected b'PSID' or b'RSID')"
            )

        version = struct.unpack(">H", raw[4:6])[0]
        if version not in _VALID_VERSIONS:
            raise SidFormatError(
                f"invalid version: {version} (expected 1, 2, 3, or 4)"
            )

        data_offset = struct.unpack(">H", raw[6:8])[0]
        expected_offset = _V1_DATA_OFFSET if version == 1 else _V2_DATA_OFFSET
        if data_offset != expected_offset:
            raise SidFormatError(
                f"dataOffset 0x{data_offset:04X} does not match version {version} "
                f"(expected 0x{expected_offset:04X})"
            )

        # Need at least dataOffset + 2 bytes so an embedded load address fits.
        if len(raw) < data_offset + 2:
            raise SidFormatError(
                f"file truncated: {len(raw)} bytes, need at least {data_offset + 2}"
            )

        load_addr = struct.unpack(">H", raw[8:10])[0]
        init_addr = struct.unpack(">H", raw[10:12])[0]
        play_addr = struct.unpack(">H", raw[12:14])[0]
        songs = struct.unpack(">H", raw[14:16])[0]
        start_song = struct.unpack(">H", raw[16:18])[0]
        speed = struct.unpack(">I", raw[18:22])[0]

        name = _decode_text(raw[22:54])
        author = _decode_text(raw[54:86])
        released = _decode_text(raw[86:118])

        if version >= 2:
            flags = struct.unpack(">H", raw[118:120])[0]
            start_page = raw[120]
            page_length = raw[121]
            second_sid_address = raw[122]
            third_sid_address = raw[123]
        else:
            flags = 0
            start_page = 0
            page_length = 0
            second_sid_address = 0
            third_sid_address = 0

        return cls(
            raw=raw,
            format=fmt,
            version=version,
            data_offset=data_offset,
            load_addr=load_addr,
            init_addr=init_addr,
            play_addr=play_addr,
            songs=songs,
            start_song=start_song,
            speed=speed,
            name=name,
            author=author,
            released=released,
            flags=flags,
            start_page=start_page,
            page_length=page_length,
            second_sid_address=second_sid_address,
            third_sid_address=third_sid_address,
        )

    @classmethod
    def load(cls, path: str | Path) -> "SidFile":
        """Read a PSID/RSID file from disk and parse it."""
        data = Path(path).read_bytes()
        return cls.from_bytes(data)


def _encode_text(text: str, width: int = 32) -> bytes:
    """Encode a string as a fixed-width, null-terminated ISO-8859-1 field."""
    encoded = text.encode("iso-8859-1", errors="replace")
    if len(encoded) >= width:
        # Leave room for at least one null terminator.
        encoded = encoded[: width - 1]
    return encoded + b"\x00" * (width - len(encoded))


def build_test_psid(
    load_addr: int = 0x1000,
    init_code: bytes = b"",
    play_code: bytes = b"",
    name: str = "TEST",
    author: str = "HARNESS",
    released: str = "2026",
    version: int = 2,
    songs: int = 1,
) -> bytes:
    """Synthesize a minimal valid PSID v2 file for testing.

    The generated memory layout at ``load_addr`` is::

        load_addr + 0:                       init_code bytes + RTS ($60)
        load_addr + len(init_code) + 1:      play_code bytes + RTS ($60)

    ``init_addr`` is set to ``load_addr`` and ``play_addr`` is set to
    ``load_addr + len(init_code) + 1`` (the byte immediately after the
    init routine's RTS).

    Args:
        load_addr: Non-zero 16-bit C64 address where data should load.
            The PSID header's loadAddress field is set directly (no
            embedded load-address prefix).
        init_code: Bytes for the init routine, excluding the trailing RTS.
        play_code: Bytes for the play routine, excluding the trailing RTS.
        name, author, released: Text fields (truncated to 31 chars).
        version: PSID version, 1 or >=2.
        songs: Number of songs declared in the header.

    Returns:
        PSID file bytes parseable by :meth:`SidFile.from_bytes`.
    """
    if load_addr == 0:
        raise ValueError("load_addr must be non-zero for build_test_psid")
    if version not in _VALID_VERSIONS:
        raise ValueError(f"invalid version: {version}")

    init_routine = init_code + b"\x60"
    play_routine = play_code + b"\x60"
    init_addr = load_addr
    play_addr = load_addr + len(init_routine)

    data = init_routine + play_routine

    data_offset = _V1_DATA_OFFSET if version == 1 else _V2_DATA_OFFSET

    header = bytearray(data_offset)
    header[0:4] = _PSID_MAGIC
    struct.pack_into(">H", header, 4, version)
    struct.pack_into(">H", header, 6, data_offset)
    struct.pack_into(">H", header, 8, load_addr)
    struct.pack_into(">H", header, 10, init_addr)
    struct.pack_into(">H", header, 12, play_addr)
    struct.pack_into(">H", header, 14, songs)
    struct.pack_into(">H", header, 16, 1)  # startSong = 1
    struct.pack_into(">I", header, 18, 0)  # speed = 0 (all songs 50Hz)
    header[22:54] = _encode_text(name)
    header[54:86] = _encode_text(author)
    header[86:118] = _encode_text(released)

    if version >= 2:
        struct.pack_into(">H", header, 118, 0)  # flags
        header[120] = 0  # startPage
        header[121] = 0  # pageLength
        header[122] = 0  # secondSidAddress
        header[123] = 0  # thirdSidAddress

    return bytes(header) + data
