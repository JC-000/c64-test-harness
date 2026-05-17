#!/usr/bin/env python3
"""Example: implementing a custom hardware backend.

This shows how to create a backend for, e.g., an Ultimate 64 cartridge
accessed over serial.  The backend only needs to implement the methods
of C64Transport — it gets all screen matching, keyboard helpers, and
the test framework for free.

Unlike the other ``examples/*.py`` files (which spawn VICE via
``ViceInstanceManager``), this example deliberately uses the low-level
``HardwareTransportBase`` API: its purpose is to demonstrate how a new
backend is *implemented*, not how an existing one is consumed. There is
no VICE process here to manage. Once the subclass is wired up to real
hardware, callers use it directly (see the usage comment at the bottom).
"""

from c64_test_harness import HardwareTransportBase, ScreenGrid, wait_for_text


class MyHardwareTransport(HardwareTransportBase):
    """Example hardware backend (not functional — for illustration only)."""

    def __init__(self, serial_port: str = "/dev/ttyUSB0"):
        super().__init__()
        self._port = serial_port
        # self._serial = serial.Serial(serial_port, 115200)

    def read_memory(self, addr: int, length: int) -> bytes:
        # Send read command to hardware probe
        # self._serial.write(f"R {addr:04X} {length}\n".encode())
        # return self._serial.read(length)
        raise NotImplementedError("Connect real hardware here")

    def write_memory(self, addr: int, data: bytes | list[int]) -> None:
        raise NotImplementedError("Connect real hardware here")

    def read_screen_codes(self) -> list[int]:
        # Read screen memory from $0400
        data = self.read_memory(0x0400, self.screen_cols * self.screen_rows)
        return list(data)

    def inject_keys(self, petscii_codes: list[int]) -> None:
        # Write to C64 keyboard buffer
        self.write_memory(0x0277, petscii_codes)
        self.write_memory(0x00C6, [len(petscii_codes)])

    def read_registers(self) -> dict[str, int]:
        raise NotImplementedError

    def resume(self) -> None:
        raise NotImplementedError


# Once implemented, use it exactly like BinaryViceTransport:
#
#   transport = MyHardwareTransport("/dev/ttyUSB0")
#   grid = wait_for_text(transport, "READY.")
#   print(grid.text())
