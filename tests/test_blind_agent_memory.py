"""Memory tests against an Ultimate 64 device via UnifiedManager.

Requires U64_HOST environment variable to be set (e.g. U64_HOST=192.168.1.81).
"""

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("U64_HOST"),
    reason="U64_HOST not set",
)

from c64_test_harness.backends.unified_manager import UnifiedManager


@pytest.fixture(scope="module")
def manager():
    mgr = UnifiedManager(backend="u64", u64_hosts=[os.environ["U64_HOST"]])
    yield mgr
    mgr.shutdown()


@pytest.fixture(scope="module")
def transport(manager):
    target = manager.acquire()
    yield target.transport
    manager.release(target)


class TestMemoryRoundTrip:
    """Memory read/write round-trip tests."""

    def test_write_read_256_bytes(self, transport):
        """Write 0x00-0xFF to $C000 in 128-byte chunks, read back, verify."""
        data = bytes(range(256))
        # U64 firmware limits writes to 128 bytes per request
        transport.write_memory(0xC000, data[:128])
        transport.write_memory(0xC080, data[128:])
        result = transport.read_memory(0xC000, 256)
        assert result == data, "256-byte round-trip at $C000 failed"

    def test_pattern_c100(self, transport):
        """Write a known pattern to $C100-$C1FF in chunks, verify round-trip."""
        pattern = bytes([0xDE, 0xAD, 0xBE, 0xEF] * 64)
        # Write in 128-byte chunks to stay within firmware limit
        transport.write_memory(0xC100, pattern[:128])
        transport.write_memory(0xC180, pattern[128:])
        result = transport.read_memory(0xC100, 256)
        assert result == pattern, "Pattern round-trip at $C100 failed"

    def test_rom_area_nonzero(self, transport):
        """Read 16 bytes from ROM area ($E000), verify non-zero."""
        rom = transport.read_memory(0xE000, 16)
        assert len(rom) == 16
        assert any(b != 0 for b in rom), "ROM area at $E000 should not be all zeros"

    def test_screen_memory_length(self, transport):
        """Read 1000 bytes of screen memory ($0400), verify length."""
        screen = transport.read_memory(0x0400, 1000)
        assert len(screen) == 1000, f"Expected 1000 bytes, got {len(screen)}"

    def test_petscii_screen_codes(self, transport):
        """Write 'AGENT1' as PETSCII screen codes to $0428, read back."""
        # PETSCII screen codes: A=1, G=7, E=5, N=14, T=20, 1=49
        agent1_screen = bytes([1, 7, 5, 14, 20, 0x31])
        transport.write_memory(0x0428, agent1_screen)
        result = transport.read_memory(0x0428, 6)
        assert result == agent1_screen, (
            f"PETSCII screen code round-trip failed: "
            f"expected {agent1_screen.hex()}, got {result.hex()}"
        )
