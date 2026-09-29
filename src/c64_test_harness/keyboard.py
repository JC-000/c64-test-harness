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

    Each batch goes through ``transport.inject_keys()``.  Draining is
    the backend's job, not latency's:

    * On VICE the Keyboard Feed command queues the keys in the
      emulator's own feed buffer and -- like every binary-monitor
      command -- leaves the 6510 halted.  The KERNAL sees the keys only
      once something resumes the machine (a screen waiter,
      :func:`~.memory.wait_for_memory`, or ``transport.resume()``); a
      ``time.sleep`` after this call drains nothing (issue #514).
    * On the Ultimate 64 ``inject_keys`` waits for the KERNAL buffer to
      drain before each batch; the machine runs throughout.
    """
    codes = [char_to_petscii(ch) for ch in text]
    for i in range(0, len(codes), KEYBUF_MAX):
        batch = codes[i : i + KEYBUF_MAX]
        transport.inject_keys(batch)


def send_key(transport: C64Transport, char_or_code: str | int) -> None:
    """Send a single key.  Accepts a character or raw PETSCII code.

    On VICE the key reaches the program only after a resume; see
    :func:`send_text`.
    """
    if isinstance(char_or_code, int):
        code = char_or_code
    else:
        code = char_to_petscii(char_or_code)
    transport.inject_keys([code])
