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

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ._address import refuse_bool_address

if TYPE_CHECKING:
    from .transport import C64Transport

# KERNAL zero page after a LOAD: EAL, the end pointer (one past the last byte
# stored), and ST, the I/O status.
_KERNAL_EAL = 0x00AE
_KERNAL_ST = 0x0090


@dataclass(frozen=True)
class LoadCheck:
    """What :meth:`PrgFile.verify_load` read after a KERNAL LOAD.

    ``start`` is where the payload was expected, ``expected_end`` is
    ``start + len(payload)``, ``end_pointer`` is the KERNAL's EAL (``$AE/$AF``),
    ``status`` is ST (``$90``) as read, and ``content_diffs`` counts payload
    bytes that differ from RAM at ``start`` (a short read counts every
    missing byte).
    """

    start: int
    expected_end: int
    end_pointer: int
    status: int
    content_diffs: int

    @property
    def complete(self) -> bool:
        """The LOAD stored the whole payload: end pointer and bytes both match."""
        return self.end_pointer == self.expected_end and self.content_diffs == 0

    @property
    def short(self) -> bool:
        """The LOAD stopped before the end of the payload."""
        return self.end_pointer < self.expected_end


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

        A read that returns the wrong number of bytes (e.g. a failed or
        truncated transport read) counts every missing byte as a diff —
        it never verifies as intact.
        """
        from .memory import read_bytes  # avoid circular import

        expected = self.bytes_at(addr, length)
        actual = read_bytes(transport, addr, length)
        if len(actual) != length:
            # zip() would silently truncate the comparison and a failed
            # (empty) read would "verify" as intact.  Count the length
            # mismatch as diffs on top of any differing overlap bytes.
            diffs = sum(1 for a, b in zip(actual, expected) if a != b)
            diffs += abs(length - len(actual))
            return (False, diffs)
        diffs = sum(1 for a, b in zip(actual, expected) if a != b)
        return (diffs == 0, diffs)

    def verify_load(
        self,
        transport: C64Transport,
        *,
        start: int | None = None,
    ) -> LoadCheck:
        """Check that a KERNAL LOAD of this file stored all of it.

        Call it after the LOAD has finished. It reads the KERNAL's end
        pointer EAL (``$AE/$AF``), ST (``$90``) and the payload at *start*
        (default: the file's own load address; pass ``start=0x0801``, or
        whatever ``$2B/$2C`` holds, for ``LOAD"NAME",8``, which loads at the
        BASIC start).

        **Use the result's** ``complete`` **, not ST or the bytes alone.** On
        an Ultimate a REST memory read during the LOAD halts the 6510 while
        the CIA timers run on, and the KERNAL can take a false EOI: the LOAD
        ends early with ST=$40 and no error (#573, upstream
        GideonZ/1541ultimate#1120). ST=$40 is also what a complete LOAD
        leaves, and a repeated LOAD of the same file leaves the earlier
        copy's tail in RAM, so neither ST nor a content compare can tell a
        short LOAD from a complete one. The end pointer can. ``short`` says
        the pointer stopped before the end.

        This makes one read for EAL, one for ST and one per 128 bytes of
        payload (chunked above 256 bytes, :func:`~.memory.read_bytes`). Do not
        call it while the C64 is still doing IEC I/O on an Ultimate, for the
        same reason. On VICE it leaves the CPU halted, like
        :meth:`verify_region`.

        :raises ValueError: *start* is a bool, outside ``0..0xFFFF``, or the
            payload would run past ``$FFFF``; raised before any read.
        :raises ~.memory.ShortReadError: the EAL or ST read, or a chunk of a
            payload over 256 bytes, came back short twice.
        """
        from .memory import read_bytes, read_bytes_chunked  # avoid circular import

        if start is None:
            start = self._load_address
        refuse_bool_address(start, "verify_load start")
        if not 0 <= start <= 0xFFFF:
            raise ValueError(f"verify_load start {start!r} is outside $0000-$FFFF")
        expected_end = start + len(self._data)
        if expected_end > 0x10000:
            raise ValueError(
                f"a {len(self._data)}-byte payload at ${start:04X} runs past $FFFF"
            )

        # Chunked reads: a short reply is retried once, then ShortReadError,
        # so a failed pointer read is never mistaken for a short LOAD.
        eal = read_bytes_chunked(transport, _KERNAL_EAL, 2)
        end_pointer = eal[0] | (eal[1] << 8)
        status = read_bytes_chunked(transport, _KERNAL_ST, 1)[0]
        actual = read_bytes(transport, start, len(self._data))
        diffs = sum(1 for a, b in zip(actual, self._data) if a != b)
        diffs += abs(len(self._data) - len(actual))
        return LoadCheck(start, expected_end, end_pointer, status, diffs)

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
