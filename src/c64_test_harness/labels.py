"""VICE label file parser.

Parses the ``al C:XXXX .name`` format produced by cc65, ACME, Kick
Assembler, and other 6502 toolchains when generating VICE label files.
"""

from __future__ import annotations

import re
from pathlib import Path

_LABEL_RE = re.compile(r"al\s+C:([0-9a-fA-F]+)\s+\.(\S+)")


class Labels:
    """Parsed label file providing bidirectional address ↔ name lookup."""

    def __init__(self) -> None:
        self._by_name: dict[str, int] = {}
        self._by_addr: dict[int, str] = {}

    @classmethod
    def from_file(cls, path: str | Path) -> Labels:
        """Parse a VICE-format label file."""
        labels = cls()
        with open(path) as f:
            for line in f:
                line = line.strip()
                m = _LABEL_RE.match(line)
                if m:
                    addr = int(m.group(1), 16)
                    name = m.group(2)
                    labels._by_name[name] = addr
                    labels._by_addr[addr] = name
        return labels

    def address(self, name: str) -> int | None:
        """Return address for *name*, or ``None`` if not found."""
        return self._by_name.get(name)

    def name(self, addr: int) -> str | None:
        """Return label name for *addr*, or ``None`` if not found."""
        return self._by_addr.get(addr)

    def __getitem__(self, name: str) -> int:
        """Lookup by name, raising ``KeyError`` if not found."""
        return self._by_name[name]

    def __contains__(self, name: str) -> bool:
        return name in self._by_name

    def __len__(self) -> int:
        return len(self._by_name)

    def __repr__(self) -> str:
        return f"Labels({len(self)} entries)"
