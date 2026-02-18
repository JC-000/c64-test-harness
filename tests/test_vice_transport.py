"""VICE integration tests for ViceTransport protocol layer.

Validates the raw transport interface: register parsing, TCP command
round-trips, chunked memory access, key injection, and error paths.
Uses the module-scoped ``vice_transport`` fixture from conftest.py.

NOTE: ``test_resume_closes_monitor`` must be the LAST test because
``resume()`` permanently closes the VICE monitor port.
"""

from __future__ import annotations

import shutil

import pytest

from c64_test_harness.backends.vice import ViceTransport
from c64_test_harness.memory import write_bytes
from c64_test_harness.transport import ConnectionError as TransportConnectionError

# Skip entire module if x64sc is not installed
pytestmark = pytest.mark.skipif(
    shutil.which("x64sc") is None, reason="x64sc not found on PATH"
)


class TestViceTransportBasic:
    """Test ViceTransport methods that keep the monitor accessible."""

    def test_read_registers_all_keys(self, vice_transport) -> None:
        """read_registers returns PC, A, X, Y, SP — all ints in valid range."""
        regs = vice_transport.read_registers()
        for key in ("PC", "A", "X", "Y", "SP"):
            assert key in regs, f"Missing register key: {key}"
            assert isinstance(regs[key], int)
        assert 0 <= regs["PC"] <= 0xFFFF
        assert 0 <= regs["A"] <= 0xFF
        assert 0 <= regs["X"] <= 0xFF
        assert 0 <= regs["Y"] <= 0xFF
        assert 0 <= regs["SP"] <= 0xFF

    def test_raw_command_response(self, vice_transport) -> None:
        """raw_command('r') returns string with '.;' prefix (register dump)."""
        resp = vice_transport.raw_command("r")
        assert ".;" in resp, f"Unexpected register response: {resp!r}"

    def test_write_read_at_chunk_boundary(self, vice_transport) -> None:
        """Write/read 256 bytes (exact boundary) then verify.

        Writes in 64-byte chunks because VICE's text monitor input buffer
        (~256 chars) truncates longer >C: commands.
        """
        addr = 0xC000
        data_256 = bytes(range(256))

        chunk_size = 64
        for i in range(0, len(data_256), chunk_size):
            write_bytes(vice_transport, addr + i,
                        list(data_256[i:i + chunk_size]))

        result = vice_transport.read_memory(addr, 256)
        assert result == data_256

    def test_read_screen_codes_length(self, vice_transport) -> None:
        """read_screen_codes returns exactly 1000 ints, all 0-255."""
        codes = vice_transport.read_screen_codes()
        assert len(codes) == 1000
        assert all(0 <= c <= 255 for c in codes)

    def test_inject_keys_buffer_write(self, vice_transport) -> None:
        """inject_keys writes to keyboard buffer without error."""
        vice_transport.inject_keys([0x41, 0x42])  # 'A', 'B' in PETSCII
        # The buffer may already have been consumed by BASIC.
        # The full end-to-end path is tested in TestKeyboard.

    def test_connection_error_on_bad_port(self) -> None:
        """ViceTransport on unreachable port raises on read_registers."""
        bad = ViceTransport(port=1, timeout=1.0)
        with pytest.raises(TransportConnectionError):
            bad.read_registers()


class TestViceTransportResume:
    """Test resume() behaviour.

    This class MUST be last because resume() permanently closes the
    VICE monitor port.
    """

    def test_resume_closes_monitor(self, vice_transport) -> None:
        """After resume(), the monitor port stops accepting connections.

        This confirms the VICE text monitor behaviour: sending 'x'
        (exit) permanently closes the monitor until a breakpoint fires.
        """
        vice_transport.resume()

        # Monitor should be unreachable now
        with pytest.raises(TransportConnectionError):
            vice_transport.read_registers()
