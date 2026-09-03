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

    def write_memory(
        self,
        addr: int,
        data: bytes | list[int],
        *,
        override: str | None = None,
    ) -> None:
        """Write ``data`` bytes starting at ``addr``.

        Backends MAY enforce a ``MemoryPolicy`` against the write before
        any byte crosses the wire.  Pass ``override="<reason>"`` to
        bypass the policy for a single call; the bypass is logged at
        WARNING level so the use stays visible.  Backends without policy
        support ignore the kwarg.
        """
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

    def inject_joystick(self, port: int, value: int) -> None:
        """Inject joystick state.  ``port`` is 1 or 2; ``value`` is the
        joystick byte, bits 0-4 = up/down/left/right/fire.

        Convention: ``value`` is **active-high** — bit set means the
        direction/button is pressed (the VICE ``JOYPORT_SET`` polarity).
        Backends whose hardware is active-low (the U64's CIA1 data
        ports) convert internally; callers always pass active-high.

        Persistence differs by backend: VICE holds the injected state
        until the next ``inject_joystick`` call; the U64 write is
        one-shot — the KERNAL keyboard scan rewrites the CIA ports at
        ~60 Hz, so sustained input needs periodic re-injection (or a
        paused machine).
        """
        ...

    def read_framebuffer(self) -> dict:
        """Return raw framebuffer bytes plus geometry. Backend-specific layout — see backend docs."""
        ...

    def read_palette(self) -> list[tuple[int, int, int]]:
        """Return the active VIC palette as RGB triples."""
        ...

    def resume(self) -> None:
        """Resume execution, exiting the monitor if one holds the machine.

        The backends differ here and the difference is load-bearing.  On
        VICE **every** binary-monitor command halts the 6510, and it stays
        halted until this is called -- so a caller that reads memory or
        the screen and then expects time to pass must resume, or it is
        timing a stopped machine.  On the Ultimate 64 memory access is
        DMA-backed and the machine runs throughout, so a resume is not
        needed; it is still a real request to the device, which costs a
        round trip and will clear a pause the caller set deliberately.

        The cross-backend screen waiters resume on every exit path for
        this reason; :meth:`ScreenGrid.from_transport` deliberately does
        not, because it is a snapshot primitive rather than a waiter.
        """
        ...

    def set_speed(self, multiplier: int | None) -> None:
        """Set the host-side CPU-speed multiplier.

        Backend-agnostic CPU-speed control.  Semantics:

        * ``multiplier=1`` — run at native 1 MHz (warp off on VICE,
          turbo off on U64).
        * ``multiplier=2|4|8|...`` — run at the requested discrete
          multiplier where the backend supports it (U64 turbo enums).
          Backends that do not support discrete speeds raise
          ``NotImplementedError`` for these values.
        * ``multiplier=None`` — run at the backend's max available
          speed (warp on VICE, max turbo on U64).

        :raises NotImplementedError: backend cannot honour the request.
        :raises ValueError: integer is not in the backend's supported set.
        """
        ...

    def get_speed(self) -> int | None:
        """Return the current CPU-speed multiplier.

        Returns ``1`` when the backend is running at native 1× speed.
        Returns the integer multiplier when running at a known turbo
        step.  Returns ``None`` when the backend is running faster
        than native but the exact multiplier is not known (e.g. VICE
        warp mode, which is "run as fast as possible").
        """
        ...

    def reset(self, scope: str = "cpu", *, drive: str | int | None = None) -> None:
        """Reset the machine.  ``scope`` selects which part:

        * ``"cpu"`` — soft 6510-only reset.  VICE ``reset(type=0)``,
          U64 ``client.reset()``.
        * ``"machine"`` — full machine reset including FPGA-equivalent
          state.  VICE ``reset(type=1)``, U64 ``client.reboot()``.
        * ``"drive"`` — per-drive reset.  ``drive`` is required; on
          VICE it is a 0..3 index (mapped to VICE drive 8..11), on
          U64 it is the slot string ``"a"`` or ``"b"``.

        :raises ValueError: unknown scope or missing/invalid ``drive``.
        """
        ...

    def close(self) -> None:
        """Release resources / close connection."""
        ...
