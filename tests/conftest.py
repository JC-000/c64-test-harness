"""Shared test fixtures — MockTransport for unit testing without VICE."""

from __future__ import annotations

import pytest


class MockTransport:
    """In-memory C64Transport for testing screen/keyboard/memory modules.

    Set ``screen_codes`` to control what ``read_screen_codes()`` returns.
    Set ``memory`` dict to control ``read_memory()`` responses.
    Inspect ``written_memory`` and ``injected_keys`` to verify writes.
    """

    def __init__(
        self,
        screen_codes: list[int] | None = None,
        cols: int = 40,
        rows: int = 25,
    ) -> None:
        self._cols = cols
        self._rows = rows
        total = cols * rows
        self._screen_codes = screen_codes if screen_codes is not None else [32] * total
        self.memory: dict[int, list[int]] = {}
        self.written_memory: list[tuple[int, list[int]]] = []
        self.injected_keys: list[list[int]] = []
        self._registers: dict[str, int] = {"PC": 0x0800, "A": 0, "X": 0, "Y": 0, "SP": 0xFF}
        self._raw_commands: list[str] = []

    @property
    def screen_cols(self) -> int:
        return self._cols

    @property
    def screen_rows(self) -> int:
        return self._rows

    @property
    def screen_codes(self) -> list[int]:
        return self._screen_codes

    @screen_codes.setter
    def screen_codes(self, codes: list[int]) -> None:
        self._screen_codes = codes

    def read_memory(self, addr: int, length: int) -> bytes:
        if addr in self.memory:
            data = self.memory[addr][:length]
            return bytes(data + [0] * (length - len(data)))
        return bytes(length)

    def write_memory(self, addr: int, data: bytes | list[int]) -> None:
        self.written_memory.append((addr, list(data)))

    def read_screen_codes(self) -> list[int]:
        return list(self._screen_codes)

    def inject_keys(self, petscii_codes: list[int]) -> None:
        self.injected_keys.append(list(petscii_codes))

    def read_registers(self) -> dict[str, int]:
        return dict(self._registers)

    def resume(self) -> None:
        pass

    def raw_command(self, cmd: str) -> str:
        self._raw_commands.append(cmd)
        return ""

    def close(self) -> None:
        pass


@pytest.fixture
def mock_transport():
    """Create a MockTransport with a blank screen (all spaces)."""
    return MockTransport()


@pytest.fixture
def labels_path():
    """Path to the real labels.txt fixture file."""
    import pathlib
    return pathlib.Path(__file__).parent / "fixtures" / "labels.txt"
