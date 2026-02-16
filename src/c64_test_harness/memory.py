"""Memory access convenience wrappers around C64Transport."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .transport import C64Transport

#: Reads larger than this are automatically chunked for reliability.
_AUTO_CHUNK_THRESHOLD = 256


def read_bytes(transport: C64Transport, addr: int, length: int) -> bytes:
    """Read *length* bytes from *addr*.

    Reads larger than 256 bytes are automatically chunked for reliability
    (VICE's text monitor can return incomplete data on very large reads).
    """
    if length > _AUTO_CHUNK_THRESHOLD:
        return read_bytes_chunked(transport, addr, length)
    return transport.read_memory(addr, length)


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
    """Write *data* to *addr*.  Alias for ``transport.write_memory()``."""
    transport.write_memory(addr, data)


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
