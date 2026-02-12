"""Debug utilities — screen dump formatting."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .screen import ScreenGrid

if TYPE_CHECKING:
    from .transport import C64Transport


def dump_screen(transport: C64Transport, label: str = "") -> str:
    """Capture screen and return formatted debug dump.

    Also prints to stdout for immediate visibility during test runs.
    """
    try:
        grid = ScreenGrid.from_transport(transport)
        output = grid.dump(label)
    except Exception as e:
        output = f"(screen read failed: {e})"
    print(output)
    return output
