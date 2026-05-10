"""C64Transport Protocol — the central abstraction for talking to a C64.

Defines the interface that all backends (VICE emulator, hardware) must
implement.  Uses ``typing.Protocol`` for structural subtyping — backends
don't need to inherit from anything, they just need to have the right methods.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class TransportError(Exception):
    """Base exception for all transport-related errors."""


class ConnectionError(TransportError):
    """Could not connect to the C64 backend."""


class TimeoutError(TransportError):
    """Operation timed out waiting for the backend."""


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class C64Transport(Protocol):
    """What you can do with a C64, regardless of backend.

    A backend only needs to implement this protocol to get all screen
    matching, keyboard helpers, and the test framework for free.
    """

    @property
    def screen_cols(self) -> int:
        """Number of screen columns (typically 40)."""
        ...

    @property
    def screen_rows(self) -> int:
        """Number of screen rows (typically 25)."""
        ...

    def read_memory(self, addr: int, length: int) -> bytes:
        """Read ``length`` bytes starting at ``addr``.

        Returns a ``bytes`` object of exactly ``length`` bytes.
        """
        ...

    def write_memory(self, addr: int, data: bytes | list[int]) -> None:
        """Write ``data`` bytes starting at ``addr``."""
        ...

    def read_screen_codes(self) -> list[int]:
        """Read raw screen code bytes (cols * rows values).

        Returns screen codes, NOT PETSCII and NOT ASCII.  The screen
        module handles conversion to text.
        """
        ...

    def inject_keys(self, petscii_codes: list[int]) -> None:
        """Inject PETSCII key codes into the keyboard buffer.

        The backend handles batching if needed (C64 buffer is 10 keys max).
        """
        ...

    def read_registers(self) -> dict[str, int]:
        """Read CPU registers.  Returns dict with keys like PC, A, X, Y, SP."""
        ...

    def inject_joystick(self, port: int, value: int) -> None:
        """Inject joystick state. port=1 or 2, value is the joystick byte (bits 0-4 = up/down/left/right/fire)."""
        ...

    def read_framebuffer(self) -> dict:
        """Return raw framebuffer bytes plus geometry. Backend-specific layout — see backend docs."""
        ...

    def read_palette(self) -> list[tuple[int, int, int]]:
        """Return the active VIC palette as RGB triples."""
        ...

    def resume(self) -> None:
        """Resume execution (exit monitor / un-pause)."""
        ...

    def close(self) -> None:
        """Release resources / close connection."""
        ...
