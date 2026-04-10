"""Ethernet MAC address helpers for CS8900a (RR-Net) on VICE.

VICE does not expose a command-line flag for setting the CS8900a MAC
address, so the Individual Address (IA) must be programmed at runtime
via the chip's PacketPage registers (PP offsets 0x0158-0x015D).

This module provides:

* ``generate_mac`` — deterministic locally-administered MAC from an index
* ``parse_mac``  — colon-hex string → 6-byte ``bytes``
* ``format_mac`` — 6-byte ``bytes`` → colon-hex string
* ``set_cs8900a_mac`` — program the CS8900a IA registers through a
  connected ``C64Transport``

The MAC is written via the RR-Net PPPtr ($DE02) / PPData ($DE04) I/O
registers.  Before any register access we must set the RR clockport
enable bit ($DE01 bit 0), otherwise the chip silently drops the writes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .transport import C64Transport

# CS8900a PacketPage offsets for the Individual Address (6 bytes, 3 words)
_IA_PP_OFFSET = 0x0158

# I/O register offsets relative to the CS8900a base address (RR-Net layout)
_ISQ_HI = 0x01       # bit 0 = RR clockport enable
_PPTR_LO = 0x02
_PPTR_HI = 0x03
_PPDATA_LO = 0x04
_PPDATA_HI = 0x05

# Locally-administered OUI prefix: 02:c6:40 (C64-themed)
_MAC_PREFIX = b"\x02\xc6\x40"


def generate_mac(index: int) -> bytes:
    """Generate a unique locally-administered MAC address for *index*.

    Returns a 6-byte MAC of the form ``02:c6:40:00:00:xx`` where the
    last three bytes encode *index* (0-16777215).

    The ``02`` prefix sets the locally-administered bit, avoiding
    conflicts with IEEE-assigned OUIs.

    Raises ``ValueError`` if *index* is out of range.
    """
    if index < 0 or index > 0xFFFFFF:
        raise ValueError(f"MAC index must be 0..16777215, got {index}")
    suffix = index.to_bytes(3, "big")
    return _MAC_PREFIX + suffix


def parse_mac(mac_str: str) -> bytes:
    """Parse a colon-separated hex MAC string to 6 bytes.

    Accepts ``"02:c6:40:00:00:01"`` or ``"02-c6-40-00-00-01"``.

    Raises ``ValueError`` on malformed input.
    """
    sep = "-" if "-" in mac_str else ":"
    parts = mac_str.split(sep)
    if len(parts) != 6:
        raise ValueError(f"MAC address must have 6 octets, got {len(parts)}")
    try:
        octets = bytes(int(p, 16) for p in parts)
    except ValueError as exc:
        raise ValueError(f"Invalid hex octet in MAC: {mac_str}") from exc
    return octets


def format_mac(mac: bytes) -> str:
    """Format a 6-byte MAC address as a colon-separated hex string."""
    if len(mac) != 6:
        raise ValueError(f"MAC must be 6 bytes, got {len(mac)}")
    return ":".join(f"{b:02x}" for b in mac)


def set_cs8900a_mac(
    transport: C64Transport,
    mac: bytes,
    base: int = 0xDE00,
) -> None:
    """Program the CS8900a Individual Address registers.

    Writes the 6-byte *mac* to PacketPage offsets 0x0158-0x015D by
    setting PPPtr and writing PPData one word at a time.

    *base* is the CS8900a I/O base address (default 0xDE00 for RR-Net
    mode).  The RR clockport enable bit ($base+1, bit 0) is set before
    the first register access.

    The transport must be connected and the CPU should be stopped
    (normal state after binary monitor connect).
    """
    if len(mac) != 6:
        raise ValueError(f"MAC must be 6 bytes, got {len(mac)}")

    isq_hi = base + _ISQ_HI
    pptr_lo = base + _PPTR_LO
    pptr_hi = base + _PPTR_HI
    ppdata_lo = base + _PPDATA_LO
    ppdata_hi = base + _PPDATA_HI

    # Enable RR clockport: set bit 0 of $DE01 (read-modify-write).
    cur = transport.read_memory(isq_hi, 1)
    transport.write_memory(isq_hi, bytes([cur[0] | 0x01]))

    # Write 3 words (6 bytes) to IA registers at PP 0x0158-0x015D
    for i in range(3):
        pp_offset = _IA_PP_OFFSET + (i * 2)
        # Set PPPtr to the target PP offset
        transport.write_memory(pptr_lo, bytes([pp_offset & 0xFF]))
        transport.write_memory(pptr_hi, bytes([pp_offset >> 8]))
        # Write the MAC word to PPData (little-endian: low byte first)
        transport.write_memory(ppdata_lo, bytes([mac[i * 2]]))
        transport.write_memory(ppdata_hi, bytes([mac[i * 2 + 1]]))
