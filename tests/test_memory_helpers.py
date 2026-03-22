"""Tests for memory convenience helpers: read/write, LE readers, hex_dump."""

from c64_test_harness.memory import (
    read_bytes,
    write_bytes,
    read_word_le,
    read_dword_le,
    hex_dump,
)
from conftest import MockTransport


class TestWriteBytes:
    def test_write_bytes_delegates(self):
        t = MockTransport()
        write_bytes(t, 0x1000, [0xDE, 0xAD])
        assert len(t.written_memory) == 1
        assert t.written_memory[0] == (0x1000, [0xDE, 0xAD])

    def test_write_bytes_with_bytes_obj(self):
        t = MockTransport()
        write_bytes(t, 0x2000, b"\x01\x02\x03")
        assert t.written_memory[0] == (0x2000, [0x01, 0x02, 0x03])


class TestReadWordLE:
    def test_read_word_le(self):
        t = MockTransport()
        t.memory[0x1000] = [0x34, 0x12]
        assert read_word_le(t, 0x1000) == 0x1234

    def test_read_word_le_zero(self):
        t = MockTransport()
        t.memory[0x1000] = [0x00, 0x00]
        assert read_word_le(t, 0x1000) == 0

    def test_read_word_le_max(self):
        t = MockTransport()
        t.memory[0x1000] = [0xFF, 0xFF]
        assert read_word_le(t, 0x1000) == 0xFFFF


class TestReadDwordLE:
    def test_read_dword_le(self):
        t = MockTransport()
        t.memory[0x1000] = [0x78, 0x56, 0x34, 0x12]
        assert read_dword_le(t, 0x1000) == 0x12345678

    def test_read_dword_le_zero(self):
        t = MockTransport()
        t.memory[0x1000] = [0x00, 0x00, 0x00, 0x00]
        assert read_dword_le(t, 0x1000) == 0

    def test_read_dword_le_max(self):
        t = MockTransport()
        t.memory[0x1000] = [0xFF, 0xFF, 0xFF, 0xFF]
        assert read_dword_le(t, 0x1000) == 0xFFFFFFFF


class TestHexDump:
    def test_hex_dump_format(self):
        t = MockTransport()
        t.memory[0x0400] = [0x05, 0x18, 0x10, 0x20]
        result = hex_dump(t, 0x0400, 4)
        assert result == "$0400: 05 18 10 20"

    def test_hex_dump_multiple_lines(self):
        t = MockTransport()
        data = list(range(32))

        def contiguous_read(addr, length):
            offset = addr - 0x0400
            return bytes(data[offset : offset + length])

        t.read_memory = contiguous_read
        result = hex_dump(t, 0x0400, 32)
        lines = result.strip().split("\n")
        assert len(lines) == 2
        assert lines[0].startswith("$0400:")
        assert lines[1].startswith("$0410:")

    def test_hex_dump_partial_last_line(self):
        t = MockTransport()
        t.memory[0x0400] = [0xAA, 0xBB, 0xCC]
        result = hex_dump(t, 0x0400, 3)
        assert result == "$0400: aa bb cc"
