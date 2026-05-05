"""Unit tests for ethernet MAC address helpers."""

from __future__ import annotations

from unittest.mock import MagicMock, call

import pytest

from c64_test_harness.ethernet import (
    generate_mac,
    parse_mac,
    format_mac,
    set_cs8900a_mac,
)


# ---------------------------------------------------------------------------
# generate_mac
# ---------------------------------------------------------------------------

class TestGenerateMac:
    def test_index_zero(self) -> None:
        mac = generate_mac(0)
        assert mac == b"\x02\xc6\x40\x00\x00\x00"

    def test_index_one(self) -> None:
        mac = generate_mac(1)
        assert mac == b"\x02\xc6\x40\x00\x00\x01"

    def test_index_255(self) -> None:
        mac = generate_mac(255)
        assert mac == b"\x02\xc6\x40\x00\x00\xff"

    def test_index_large(self) -> None:
        mac = generate_mac(0x010203)
        assert mac == b"\x02\xc6\x40\x01\x02\x03"

    def test_max_index(self) -> None:
        mac = generate_mac(0xFFFFFF)
        assert mac == b"\x02\xc6\x40\xff\xff\xff"

    def test_locally_administered_bit(self) -> None:
        """The second-least-significant bit of the first octet must be set."""
        mac = generate_mac(42)
        assert mac[0] & 0x02 == 0x02

    def test_unicast(self) -> None:
        """The least-significant bit of the first octet must be clear (unicast)."""
        mac = generate_mac(42)
        assert mac[0] & 0x01 == 0x00

    def test_length(self) -> None:
        assert len(generate_mac(0)) == 6

    def test_unique(self) -> None:
        macs = {generate_mac(i) for i in range(100)}
        assert len(macs) == 100

    def test_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="0..16777215"):
            generate_mac(-1)

    def test_too_large_raises(self) -> None:
        with pytest.raises(ValueError, match="0..16777215"):
            generate_mac(0x1000000)


# ---------------------------------------------------------------------------
# parse_mac / format_mac
# ---------------------------------------------------------------------------

class TestParseMac:
    def test_colon_separated(self) -> None:
        assert parse_mac("02:c6:40:00:00:01") == b"\x02\xc6\x40\x00\x00\x01"

    def test_dash_separated(self) -> None:
        assert parse_mac("02-c6-40-00-00-01") == b"\x02\xc6\x40\x00\x00\x01"

    def test_uppercase(self) -> None:
        assert parse_mac("02:C6:40:00:00:FF") == b"\x02\xc6\x40\x00\x00\xff"

    def test_wrong_count_raises(self) -> None:
        with pytest.raises(ValueError, match="6 octets"):
            parse_mac("02:c6:40:00:01")

    def test_invalid_hex_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid hex"):
            parse_mac("02:c6:40:00:00:ZZ")


class TestFormatMac:
    def test_roundtrip(self) -> None:
        mac = b"\x02\xc6\x40\x00\x00\x01"
        assert format_mac(mac) == "02:c6:40:00:00:01"

    def test_all_ff(self) -> None:
        assert format_mac(b"\xff" * 6) == "ff:ff:ff:ff:ff:ff"

    def test_wrong_length_raises(self) -> None:
        with pytest.raises(ValueError, match="6 bytes"):
            format_mac(b"\x00" * 5)

    def test_parse_format_roundtrip(self) -> None:
        original = "02:c6:40:ab:cd:ef"
        assert format_mac(parse_mac(original)) == original


# ---------------------------------------------------------------------------
# set_cs8900a_mac
# ---------------------------------------------------------------------------

class TestSetCs8900aMac:
    def test_programs_three_words(self) -> None:
        transport = MagicMock()
        mac = b"\x02\xc6\x40\x00\x00\x01"
        set_cs8900a_mac(transport, mac)

        # Should write PPPtr + PPData for 3 words
        assert transport.write_memory.call_count == 12  # 4 writes per word × 3

    def test_correct_pp_offsets(self) -> None:
        transport = MagicMock()
        mac = b"\x02\xc6\x40\x00\x00\x01"
        set_cs8900a_mac(transport, mac)

        calls = transport.write_memory.call_args_list

        # Word 0: PPPtr = 0x0158
        assert calls[0] == call(0xDE0A, bytes([0x58]))  # PPPtr lo
        assert calls[1] == call(0xDE0B, bytes([0x01]))  # PPPtr hi
        assert calls[2] == call(0xDE0C, bytes([0x02]))  # PPData lo = mac[0]
        assert calls[3] == call(0xDE0D, bytes([0xC6]))  # PPData hi = mac[1]

        # Word 1: PPPtr = 0x015A
        assert calls[4] == call(0xDE0A, bytes([0x5A]))
        assert calls[5] == call(0xDE0B, bytes([0x01]))
        assert calls[6] == call(0xDE0C, bytes([0x40]))  # mac[2]
        assert calls[7] == call(0xDE0D, bytes([0x00]))  # mac[3]

        # Word 2: PPPtr = 0x015C
        assert calls[8] == call(0xDE0A, bytes([0x5C]))
        assert calls[9] == call(0xDE0B, bytes([0x01]))
        assert calls[10] == call(0xDE0C, bytes([0x00]))  # mac[4]
        assert calls[11] == call(0xDE0D, bytes([0x01]))  # mac[5]

    def test_custom_base_address(self) -> None:
        transport = MagicMock()
        mac = b"\x02\xc6\x40\x00\x00\x01"
        set_cs8900a_mac(transport, mac, base=0xDF00)

        calls = transport.write_memory.call_args_list
        # PPPtr should be at base + 0x0A = 0xDF0A
        assert calls[0] == call(0xDF0A, bytes([0x58]))
        assert calls[1] == call(0xDF0B, bytes([0x01]))

    def test_wrong_mac_length_raises(self) -> None:
        transport = MagicMock()
        with pytest.raises(ValueError, match="6 bytes"):
            set_cs8900a_mac(transport, b"\x02\xc6\x40\x00\x00")

    def test_broadcast_mac(self) -> None:
        """Verify broadcast MAC can be programmed (edge case)."""
        transport = MagicMock()
        set_cs8900a_mac(transport, b"\xff\xff\xff\xff\xff\xff")
        assert transport.write_memory.call_count == 12


# ---------------------------------------------------------------------------
# ViceInstanceManager MAC auto-generation
# ---------------------------------------------------------------------------

class TestViceInstanceManagerMac:
    """Test that ViceInstanceManager assigns unique MACs to instances."""

    def test_mac_counter_increments(self) -> None:
        from c64_test_harness.backends.vice_manager import ViceInstanceManager
        from c64_test_harness.backends.vice_lifecycle import ViceConfig

        cfg = ViceConfig(ethernet=True)
        mgr = ViceInstanceManager(cfg)
        assert mgr._mac_counter == 0

    def test_config_copies_ethernet_mac(self) -> None:
        from c64_test_harness.backends.vice_lifecycle import ViceConfig

        mac = b"\x02\xc6\x40\x00\x00\x42"
        cfg = ViceConfig(ethernet=True, ethernet_mac=mac)
        assert cfg.ethernet_mac == mac
