"""Hardware transport base — extension point for real C64 hardware.

This module provides a documented base class for hardware backends.
Concrete implementations belong in separate packages (e.g.,
``c64-test-harness-ultimate64``).

Hardware backend scenarios:
  - Serial/SSH/telnet to Ultimate 64 cartridge
  - Custom Arduino probe
  - FPGA-based test jig
  - Video capture + OCR → screen codes
"""

from __future__ import annotations


class HardwareTransportBase:
    """Optional base class for hardware backends.

    Provides default screen dimensions.  Subclasses must implement all
    methods of the ``C64Transport`` protocol.

    Example::

        class Ultimate64Transport(HardwareTransportBase):
            def read_memory(self, addr, length):
                return self._serial.read_mem(addr, length)
            # ... etc
    """

    def __init__(self, screen_cols: int = 40, screen_rows: int = 25) -> None:
        self._screen_cols = screen_cols
        self._screen_rows = screen_rows

    @property
    def screen_cols(self) -> int:
        return self._screen_cols

    @property
    def screen_rows(self) -> int:
        return self._screen_rows

    def read_memory(self, addr: int, length: int) -> bytes:
        raise NotImplementedError

    def write_memory(self, addr: int, data: bytes | list[int]) -> None:
        raise NotImplementedError

    def read_screen_codes(self) -> list[int]:
        raise NotImplementedError

    def inject_keys(self, petscii_codes: list[int]) -> None:
        raise NotImplementedError

    def read_registers(self) -> dict[str, int]:
        raise NotImplementedError

    def resume(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass
