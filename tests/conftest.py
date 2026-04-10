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


# ---------------------------------------------------------------------------
# Bridge networking fixtures (two VICE instances on a Linux bridge)
# ---------------------------------------------------------------------------

# Default MACs and IPs used by the bridge_vice_pair fixture
BRIDGE_MAC_A = bytes.fromhex("02C640000001")
BRIDGE_MAC_B = bytes.fromhex("02C640000002")
BRIDGE_IP_A = bytes([10, 0, 65, 2])
BRIDGE_IP_B = bytes([10, 0, 65, 3])


def _bridge_wait_ready(transport: BinaryViceTransport, timeout: float = 30.0) -> None:
    from c64_test_harness.screen import ScreenGrid
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            transport.resume()
            time.sleep(2.0)
            grid = ScreenGrid.from_transport(transport)
            if "READY" in grid.continuous_text().upper():
                return
        except Exception:
            time.sleep(1.0)
    raise AssertionError("BASIC READY prompt not found within timeout")


def _bridge_init_cs8900a(transport: BinaryViceTransport, scratch: int, code: int) -> None:
    """Initialise CS8900a for promiscuous RX + SerTxON/SerRxON.

    *code* is the load address used for the helper routines (e.g. 0xC000).
    *scratch* is a 2-byte scratch area for reading LineCTL (e.g. 0xC1E0).
    """
    from c64_test_harness.bridge_ping import (
        cs8900a_read_linectl_code,
        cs8900a_rxctl_code,
        cs8900a_write_linectl_code,
    )
    from c64_test_harness.execute import jsr, load_code
    from c64_test_harness.memory import read_bytes

    load_code(transport, code, cs8900a_rxctl_code())
    jsr(transport, code, timeout=5.0)
    load_code(transport, code, cs8900a_read_linectl_code(scratch))
    jsr(transport, code, timeout=5.0)
    linectl = read_bytes(transport, scratch, 2)
    load_code(transport, code, cs8900a_write_linectl_code(linectl[0] | 0xC0, linectl[1]))
    jsr(transport, code, timeout=5.0)


@pytest.fixture(scope="module")
def bridge_vice_pair():
    """Launch two VICE instances with RR-Net ethernet on a Linux bridge.

    Yields ``(transport_a, transport_b)`` -- both connected, at BASIC
    READY, CS8900a initialised, and with unique MACs programmed
    (``BRIDGE_MAC_A`` on tap-c64-0, ``BRIDGE_MAC_B`` on tap-c64-1).

    Skipped automatically if ``x64sc`` is not on PATH or if the
    ``tap-c64-0`` / ``tap-c64-1`` interfaces are not present.  Run
    ``sudo scripts/setup-bridge-tap.sh`` first to create them.

    Use this fixture for any test that needs two bridged C64 instances
    sharing a layer-2 segment.  See ``docs/bridge_networking.md`` for
    the full pattern.
    """
    import os
    if shutil.which("x64sc") is None:
        pytest.skip("x64sc not found on PATH")
    if not os.path.isdir("/sys/class/net/tap-c64-0"):
        pytest.skip("tap-c64-0 not found (run scripts/setup-bridge-tap.sh)")
    if not os.path.isdir("/sys/class/net/tap-c64-1"):
        pytest.skip("tap-c64-1 not found (run scripts/setup-bridge-tap.sh)")

    from c64_test_harness.ethernet import set_cs8900a_mac

    code = 0xC000
    scratch = 0xC1E0

    allocator = PortAllocator(port_range_start=6560, port_range_end=6580)
    port_a = allocator.allocate()
    port_b = allocator.allocate()
    res_a = allocator.take_socket(port_a)
    if res_a is not None:
        res_a.close()
    res_b = allocator.take_socket(port_b)
    if res_b is not None:
        res_b.close()

    config_a = ViceConfig(
        port=port_a, warp=False, sound=False,
        ethernet=True, ethernet_mode="rrnet",
        ethernet_interface="tap-c64-0",
        ethernet_driver="tuntap",
    )
    config_b = ViceConfig(
        port=port_b, warp=False, sound=False,
        ethernet=True, ethernet_mode="rrnet",
        ethernet_interface="tap-c64-1",
        ethernet_driver="tuntap",
    )

    vice_a = ViceProcess(config_a)
    vice_b = ViceProcess(config_b)

    try:
        vice_a.start()
        vice_b.start()
        transport_a = connect_binary_transport(port_a, proc=vice_a)
        transport_b = connect_binary_transport(port_b, proc=vice_b)
        try:
            _bridge_wait_ready(transport_a)
            _bridge_wait_ready(transport_b)
            _bridge_init_cs8900a(transport_a, scratch, code)
            _bridge_init_cs8900a(transport_b, scratch, code)
            set_cs8900a_mac(transport_a, BRIDGE_MAC_A)
            set_cs8900a_mac(transport_b, BRIDGE_MAC_B)
            yield transport_a, transport_b
        finally:
            transport_a.close()
            transport_b.close()
    finally:
        vice_a.stop()
        vice_b.stop()
        allocator.release(port_a)
        allocator.release(port_b)
