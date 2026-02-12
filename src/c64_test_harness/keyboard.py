"""Keyboard input helpers — send text to a C64 via the transport.

Converts Unicode text to PETSCII and calls ``transport.inject_keys()``
in batches (fixing vicemon.py bug #4: slow per-character TCP).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .encoding.petscii import char_to_petscii

if TYPE_CHECKING:
    from .transport import C64Transport

#: Maximum keys per batch (C64 keyboard buffer is 10 bytes)
KEYBUF_MAX = 10


def send_text(transport: C64Transport, text: str) -> None:
    """Convert *text* to PETSCII and inject in batches of up to 10 keys.

    Each batch goes through ``transport.inject_keys()``, which writes
    to the C64 keyboard buffer.  The transport's natural latency (TCP
    round-trip for VICE, serial delay for hardware) provides implicit
    drain time.
    """
    codes = [char_to_petscii(ch) for ch in text]
    for i in range(0, len(codes), KEYBUF_MAX):
        batch = codes[i : i + KEYBUF_MAX]
        transport.inject_keys(batch)


def send_key(transport: C64Transport, char_or_code: str | int) -> None:
    """Send a single key.  Accepts a character or raw PETSCII code."""
    if isinstance(char_or_code, int):
        code = char_or_code
    else:
        code = char_to_petscii(char_or_code)
    transport.inject_keys([code])
