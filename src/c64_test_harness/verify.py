"""PRG binary verification — compare C64 runtime memory against a PRG file.

Useful for detecting code/data corruption caused by self-modifying code,
memory overlaps, DMA, or runtime initialisation bugs.

Usage::

    from c64_test_harness import PrgFile, read_bytes

    prg = PrgFile.from_file("build/mygame.prg")
    ok, diffs = prg.verify_region(transport, labels["sha256_k"], 256)
    assert ok, f"{diffs} bytes differ"
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .transport import C64Transport


class PrgFile:
    """Parsed C64 PRG file (2-byte load address header + binary payload).

    A ``.prg`` file starts with a 16-bit little-endian load address,
    followed by the raw binary data that gets loaded at that address.
    """

    def __init__(self, load_address: int, data: bytes) -> None:
        self._load_address = load_address
        self._data = data

    @classmethod
    def from_file(cls, path: str | Path) -> PrgFile:
        """Parse a PRG file from disk."""
        raw = Path(path).read_bytes()
        if len(raw) < 2:
            raise ValueError(f"PRG file too small: {len(raw)} bytes")
        load_address = raw[0] | (raw[1] << 8)
        return cls(load_address, raw[2:])

    @property
    def load_address(self) -> int:
        """The 16-bit C64 address where this PRG loads."""
        return self._load_address

    @property
    def end_address(self) -> int:
        """One past the last byte covered by this PRG."""
        return self._load_address + len(self._data)

    @property
    def data(self) -> bytes:
        """Raw binary payload (excluding the 2-byte load address header)."""
        return self._data

    def bytes_at(self, addr: int, length: int) -> bytes:
        """Return *length* bytes from the PRG at C64 address *addr*.

        Raises ``ValueError`` if the requested region is outside the PRG.
        """
        offset = addr - self._load_address
        if offset < 0 or offset + length > len(self._data):
            raise ValueError(
                f"Region ${addr:04X}-${addr + length - 1:04X} outside PRG "
                f"(${self._load_address:04X}-${self.end_address - 1:04X})"
            )
        return self._data[offset : offset + length]

    def verify_region(
        self,
        transport: C64Transport,
        addr: int,
        length: int,
    ) -> tuple[bool, int]:
        """Compare a PRG region with live C64 memory.

        Returns ``(match, diff_count)`` where *match* is ``True`` if all
        bytes are identical, and *diff_count* is the number of differing bytes.
        """
        from .memory import read_bytes  # avoid circular import

        expected = self.bytes_at(addr, length)
        actual = read_bytes(transport, addr, length)
        diffs = sum(1 for a, b in zip(actual, expected) if a != b)
        return (diffs == 0, diffs)

    def first_diff(
        self,
        transport: C64Transport,
        addr: int,
        length: int,
    ) -> tuple[int, int, int] | None:
        """Find the first differing byte between PRG and live memory.

        Returns ``(offset, expected_byte, actual_byte)`` or ``None`` if
        all bytes match.  *offset* is relative to *addr*.
        """
        from .memory import read_bytes

        expected = self.bytes_at(addr, length)
        actual = read_bytes(transport, addr, length)
        for i, (e, a) in enumerate(zip(expected, actual)):
            if e != a:
                return (i, e, a)
        return None
