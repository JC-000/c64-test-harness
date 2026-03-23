"""Shared test fixtures — MockTransport for unit testing without VICE."""

from __future__ import annotations

import shutil
import time

import pytest

from c64_test_harness.backends.vice_binary import BinaryViceTransport
from c64_test_harness.backends.vice_lifecycle import ViceConfig, ViceProcess
from c64_test_harness.backends.vice_manager import PortAllocator
from c64_test_harness.screen import wait_for_text


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


def connect_binary_transport(
    port: int,
    timeout: float = 30.0,
    proc: ViceProcess | None = None,
    **kwargs,
) -> BinaryViceTransport:
    """Connect a BinaryViceTransport with retries.

    Polls until VICE's binary monitor accepts the persistent TCP connection.
    Keeps the first successful connection open — which is the correct
    lifecycle for the binary monitor.
    """
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        if proc is not None and proc._proc is not None and proc._proc.poll() is not None:
            raise RuntimeError("VICE process exited during binary monitor connect")
        try:
            return BinaryViceTransport(port=port, **kwargs)
        except Exception as e:
            last_err = e
            time.sleep(1)
    raise ConnectionError(
        f"Could not connect to VICE binary monitor on port {port} "
        f"within {timeout}s: {last_err}"
    )


@pytest.fixture(scope="module")
def binary_transport():
    """Boot VICE with binary monitor, yield a live BinaryViceTransport."""
    if shutil.which("x64sc") is None:
        pytest.skip("x64sc not found on PATH")

    allocator = PortAllocator(port_range_start=6511, port_range_end=6531)
    port = allocator.allocate()
    reservation = allocator.take_socket(port)
    if reservation is not None:
        reservation.close()

    config = ViceConfig(
        port=port, warp=True, sound=False,
    )

    with ViceProcess(config) as vice:
        transport = connect_binary_transport(port, proc=vice)
        try:
            yield transport
        finally:
            transport.close()
            allocator.release(port)
