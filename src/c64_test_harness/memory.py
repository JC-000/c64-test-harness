"""Memory access convenience wrappers around C64Transport."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .transport import C64Transport


def read_bytes(transport: C64Transport, addr: int, length: int) -> bytes:
    """Read *length* bytes from *addr*.  Alias for ``transport.read_memory()``."""
    return transport.read_memory(addr, length)


def hex_dump(transport: C64Transport, addr: int, length: int) -> str:
    """Return a formatted hex dump of a memory region.

    Output format::

        $0400: 05 18 10 20 0b 05 19 3a 20 37 03 20 06 04 20 03
        $0410: ...
    """
    data = transport.read_memory(addr, length)
    lines = []
    for i in range(0, len(data), 16):
        chunk = data[i : i + 16]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        lines.append(f"${addr + i:04X}: {hex_part}")
    return "\n".join(lines)
