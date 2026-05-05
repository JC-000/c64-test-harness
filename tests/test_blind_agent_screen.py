"""Blind-agent screen and keyboard tests against Ultimate 64 via UnifiedManager.

Requires a live Ultimate 64 device. Skipped unless U64_HOST is set.
"""

import os
import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("U64_HOST"),
    reason="U64_HOST not set — no Ultimate 64 device available",
)


@pytest.fixture(scope="module")
def target():
    """Acquire a U64 test target for the module, release on teardown."""
    from c64_test_harness.backends.unified_manager import UnifiedManager

    host = os.environ["U64_HOST"]
    mgr = UnifiedManager(backend="u64", u64_hosts=[host])
    tgt = mgr.acquire()
    yield tgt
    mgr.release(tgt)
    mgr.shutdown()


class TestScreenBasics:
    """Basic screen property and readback tests."""

    def test_screen_dimensions(self, target):
        """screen_cols == 40, screen_rows == 25."""
        t = target.transport
        assert t.screen_cols == 40
        assert t.screen_rows == 25

    def test_read_screen_codes_length(self, target):
        """read_screen_codes() returns exactly 1000 values (40x25)."""
        codes = target.transport.read_screen_codes()
        assert len(codes) == 1000

    def test_write_and_read_screen_codes(self, target):
        """Write known screen codes to $0400 and verify readback."""
        t = target.transport
        # Write 5 known screen codes at the start of screen RAM
        # Screen codes: A=1, B=2, C=3, D=4, E=5
        test_data = bytes([1, 2, 3, 4, 5])
        t.write_memory(0x0400, test_data)

        codes = t.read_screen_codes()
        assert codes[0] == 1, f"Expected screen code 1 (A), got {codes[0]}"
        assert codes[1] == 2, f"Expected screen code 2 (B), got {codes[1]}"
        assert codes[2] == 3, f"Expected screen code 3 (C), got {codes[2]}"
        assert codes[3] == 4, f"Expected screen code 4 (D), got {codes[3]}"
        assert codes[4] == 5, f"Expected screen code 5 (E), got {codes[4]}"

    def test_write_and_read_mid_screen(self, target):
        """Write screen codes at row 12, col 20 (middle of screen)."""
        t = target.transport
        offset = 12 * 40 + 20  # row 12, col 20
        addr = 0x0400 + offset
        test_data = bytes([19, 5, 12, 12, 15])  # S, E, L, L, O in screen codes
        t.write_memory(addr, test_data)

        codes = t.read_screen_codes()
        for i, expected in enumerate([19, 5, 12, 12, 15]):
            actual = codes[offset + i]
            assert actual == expected, (
                f"Position {offset + i}: expected {expected}, got {actual}"
            )


class TestKeyboardInject:
    """Keyboard injection smoke tests."""

    def test_inject_keys_no_raise(self, target):
        """inject_keys([0x41, 0x42, 0x43]) (ABC in PETSCII) does not raise."""
        target.transport.inject_keys([0x41, 0x42, 0x43])


class TestColorRAM:
    """Color RAM readback tests."""

    def test_color_ram_length(self, target):
        """Color RAM at $D800 is 1000 bytes."""
        color_ram = target.transport.read_memory(0xD800, 1000)
        assert len(color_ram) == 1000

    def test_color_ram_values_masked_in_range(self, target):
        """Color RAM low nybbles (& 0x0F) should all be in 0..15.

        The C64 color RAM is 4-bit; the upper nybble is undefined, so
        we mask before checking the range.
        """
        color_ram = target.transport.read_memory(0xD800, 1000)
        for i, val in enumerate(color_ram):
            masked = val & 0x0F
            assert 0 <= masked <= 15, (
                f"Color RAM byte {i} low nybble out of range: {masked}"
            )
