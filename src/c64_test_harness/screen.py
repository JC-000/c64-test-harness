"""Backend-agnostic screen operations.

``ScreenGrid`` is an immutable snapshot of the C64 screen that provides
wrap-aware text search (fixing vicemon.py bug #2) and structured data
extraction.

``wait_for_text()`` and ``wait_for_stable()`` poll the transport and
return a ``ScreenGrid`` on success, so callers can immediately extract data.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from .encoding.screen_codes import SCREEN_CODE_TABLE

if TYPE_CHECKING:
    from .transport import C64Transport


@dataclass(frozen=True)
class ScreenGrid:
    """Immutable snapshot of C64 screen memory.

    Holds raw screen codes and provides text conversion with wrap-aware
    search (no newlines in ``continuous_text()``).
    """

    codes: tuple[int, ...]
    cols: int
    rows: int

    @classmethod
    def from_transport(cls, transport: C64Transport) -> ScreenGrid:
        """Capture a screen snapshot from a live transport."""
        raw = transport.read_screen_codes()
        return cls(
            codes=tuple(raw),
            cols=transport.screen_cols,
            rows=transport.screen_rows,
        )

    @classmethod
    def from_codes(cls, codes: list[int] | tuple[int, ...], cols: int = 40, rows: int = 25) -> ScreenGrid:
        """Create a ScreenGrid from raw codes (useful for testing)."""
        return cls(codes=tuple(codes), cols=cols, rows=rows)

    def text_lines(self) -> list[str]:
        """Return one string per screen row."""
        lines = []
        for row in range(self.rows):
            start = row * self.cols
            end = start + self.cols
            row_codes = self.codes[start:end]
            line = "".join(SCREEN_CODE_TABLE[c & 0xFF] for c in row_codes)
            lines.append(line)
        return lines

    def text(self) -> str:
        """Return screen as newline-separated text (25 lines)."""
        return "\n".join(self.text_lines())

    def continuous_text(self) -> str:
        """Return screen as a single string with NO newlines.

        This is the key fix for vicemon.py bug #2: text that wraps across
        40-column rows is searchable as a continuous string.
        """
        return "".join(self.text_lines())

    def has_text(self, needle: str) -> bool:
        """Case-insensitive, wrap-aware text search."""
        return needle.upper() in self.continuous_text().upper()

    def find_text(self, needle: str) -> int:
        """Return position of *needle* in continuous text, or -1 if absent."""
        return self.continuous_text().upper().find(needle.upper())

    def extract_between(self, start_marker: str, end_marker: str) -> str | None:
        """Extract text between two markers in continuous text.

        Returns ``None`` if *start_marker* is not found.  If *end_marker*
        is not found after the start, extracts to end of screen.
        """
        ct = self.continuous_text()
        upper = ct.upper()
        start_idx = upper.find(start_marker.upper())
        if start_idx < 0:
            return None
        content_start = start_idx + len(start_marker)
        end_idx = upper.find(end_marker.upper(), content_start)
        if end_idx < 0:
            return ct[content_start:].rstrip()
        return ct[content_start:end_idx].rstrip()

    def dump(self, label: str = "") -> str:
        """Format screen for debug output with row numbers."""
        prefix = f" [{label}]" if label else ""
        lines = [f"--- Screen dump{prefix} ---"]
        for i, line in enumerate(self.text_lines()):
            lines.append(f"  {i:2d}| {line}")
        lines.append("---")
        return "\n".join(lines)


def wait_for_text(
    transport: C64Transport,
    needle: str,
    timeout: float = 60.0,
    poll_interval: float = 2.0,
    verbose: bool = True,
    on_progress: Callable[[float, str], None] | None = None,
) -> ScreenGrid | None:
    """Wait until *needle* appears on screen (wrap-aware, case-insensitive).

    Returns the matching ``ScreenGrid`` so the caller can immediately
    extract data, or ``None`` on timeout.

    *on_progress* replaces hardcoded ``print()`` — receives elapsed seconds
    and a snippet of the last non-blank screen row.
    """
    needle_upper = needle.upper()
    start = time.monotonic()
    while True:
        elapsed = time.monotonic() - start
        if elapsed >= timeout:
            return None
        try:
            grid = ScreenGrid.from_transport(transport)
            if needle_upper in grid.continuous_text().upper():
                return grid
            if verbose and on_progress is not None:
                lines = grid.text_lines()
                last = ""
                for line in reversed(lines):
                    if line.strip():
                        last = line.strip()[:60]
                        break
                on_progress(elapsed, last)
        except Exception:
            pass
        # The binary monitor pauses the CPU on every memory read.
        # Resume so the program can continue executing before we poll again.
        try:
            transport.resume()
        except Exception:
            pass
        time.sleep(poll_interval)


def wait_for_stable(
    transport: C64Transport,
    timeout: float = 10.0,
    poll_interval: float = 0.5,
    stable_count: int = 3,
) -> ScreenGrid | None:
    """Wait until screen content stops changing.

    Returns the stable ``ScreenGrid``, or ``None`` on timeout.
    """
    prev_text: str | None = None
    count = 0
    start = time.monotonic()
    last_grid: ScreenGrid | None = None
    while True:
        elapsed = time.monotonic() - start
        if elapsed >= timeout:
            return last_grid
        try:
            grid = ScreenGrid.from_transport(transport)
            current = grid.continuous_text()
            last_grid = grid
            if current == prev_text:
                count += 1
                if count >= stable_count:
                    return grid
            else:
                count = 0
                prev_text = current
        except Exception:
            pass
        # Resume the CPU so the program keeps running between polls
        # (the binary monitor pauses on memory reads).
        try:
            transport.resume()
        except Exception:
            pass
        time.sleep(poll_interval)
