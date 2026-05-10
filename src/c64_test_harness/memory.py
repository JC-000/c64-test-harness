"""Memory access convenience wrappers around C64Transport."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .transport import C64Transport

#: Reads larger than this are automatically chunked for reliability.
_AUTO_CHUNK_THRESHOLD = 256

#: VICE's text monitor silently truncates ``>`` (write) commands at ~261
#: characters, which corresponds to 84 data bytes.  Writes larger than
#: this threshold are automatically split into multiple commands.
_WRITE_CHUNK_SIZE = 84


class FlakeyReadError(Exception):
    """Raised by :func:`read_bytes_verified` when consecutive reads disagree.

    Attributes:
        addr: starting address of the read.
        length: number of bytes requested.
        attempts: list of the disagreeing reads in the order they were
            taken.  Inspect to diagnose whether the corruption is
            structured (issue #88-style misrouted-response) or random
            (e.g. truncation).
    """

    def __init__(self, addr: int, length: int, attempts: list[bytes]) -> None:
        self.addr = addr
        self.length = length
        self.attempts = attempts
        super().__init__(
            f"read_bytes_verified: {len(attempts)} consecutive reads of "
            f"{length} bytes at ${addr:04x} disagreed pairwise"
        )


def read_bytes(transport: C64Transport, addr: int, length: int) -> bytes:
    """Read *length* bytes from *addr*.

    Reads larger than 256 bytes are automatically chunked for reliability
    (VICE's text monitor can return incomplete data on very large reads).
    """
    if length > _AUTO_CHUNK_THRESHOLD:
        return read_bytes_chunked(transport, addr, length)
    return transport.read_memory(addr, length)


def read_bytes_verified(
    transport: C64Transport,
    addr: int,
    length: int,
    *,
    max_attempts: int = 2,
) -> bytes:
    """Read *length* bytes at *addr*, repeating until two consecutive reads agree.

    Returns the value once two consecutive reads agree.  Raises
    :class:`FlakeyReadError` if *max_attempts* reads in a row all
    disagree pairwise.

    Intended for downstream tests that suspect issue #88-style flakey
    reads.  The standard :func:`read_bytes` should be used everywhere
    else — this helper doubles the wire traffic per read and is only
    worth the cost when a flake is suspected.
    """
    if max_attempts < 2:
        raise ValueError(
            f"max_attempts must be >= 2 (need two reads to compare); "
            f"got {max_attempts}"
        )
    attempts: list[bytes] = [read_bytes(transport, addr, length)]
    for _ in range(max_attempts - 1):
        nxt = read_bytes(transport, addr, length)
        if nxt == attempts[-1]:
            return nxt
        attempts.append(nxt)
    raise FlakeyReadError(addr, length, attempts)


def read_bytes_chunked(
    transport: C64Transport,
    addr: int,
    length: int,
    chunk_size: int = 128,
) -> bytes:
    """Read a large memory region in chunks for reliability.

    Breaks the read into *chunk_size*-byte pieces, concatenating the
    results.  Useful when reading DER buffers, key material, or any
    region larger than ~128 bytes where a single VICE ``m`` command
    may return incomplete data.
    """
    result = bytearray()
    offset = 0
    while offset < length:
        n = min(chunk_size, length - offset)
        chunk = transport.read_memory(addr + offset, n)
        result.extend(chunk)
        offset += n
    return bytes(result[:length])


def write_bytes(transport: C64Transport, addr: int, data: bytes | list[int]) -> None:
    """Write *data* to *addr*, automatically chunking large writes.

    VICE's text monitor truncates write commands longer than ~261
    characters (84 data bytes).  This function transparently splits
    larger writes into *_WRITE_CHUNK_SIZE*-byte pieces so callers
    never need to worry about the limit.
    """
    if isinstance(data, list):
        data = bytes(data)
    if len(data) <= _WRITE_CHUNK_SIZE:
        transport.write_memory(addr, data)
        return
    offset = 0
    while offset < len(data):
        end = min(offset + _WRITE_CHUNK_SIZE, len(data))
        transport.write_memory(addr + offset, data[offset:end])
        offset = end


def read_word_le(transport: C64Transport, addr: int) -> int:
    """Read a 16-bit little-endian value from *addr*."""
    data = transport.read_memory(addr, 2)
    return data[0] | (data[1] << 8)


def read_dword_le(transport: C64Transport, addr: int) -> int:
    """Read a 32-bit little-endian value from *addr*."""
    data = transport.read_memory(addr, 4)
    return data[0] | (data[1] << 8) | (data[2] << 16) | (data[3] << 24)


def hex_dump(transport: C64Transport, addr: int, length: int) -> str:
    """Return a formatted hex dump of a memory region.

    Output format::

        $0400: 05 18 10 20 0b 05 19 3a 20 37 03 20 06 04 20 03
        $0410: ...
    """
    data = read_bytes(transport, addr, length)
    lines = []
    for i in range(0, len(data), 16):
        chunk = data[i : i + 16]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        lines.append(f"${addr + i:04X}: {hex_part}")
    return "\n".join(lines)
