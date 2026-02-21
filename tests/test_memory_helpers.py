"""Tests for memory convenience helpers: chunked reads, LE readers, write_bytes."""

from c64_test_harness.memory import (
    read_bytes,
    read_bytes_chunked,
    write_bytes,
    read_word_le,
    read_dword_le,
    hex_dump,
    _AUTO_CHUNK_THRESHOLD,
    _WRITE_CHUNK_SIZE,
)
from conftest import MockTransport


class TestReadBytesChunked:
    def test_single_chunk(self):
        t = MockTransport()
        t.memory[0x1000] = list(range(64))
        result = read_bytes_chunked(t, 0x1000, 64, chunk_size=128)
        assert result == bytes(range(64))

    def test_multiple_chunks(self):
        t = MockTransport()
        # Fill 300 bytes across multiple base addresses
        full_data = list(range(256)) + list(range(44))
        t.memory = {}
        # MockTransport.read_memory looks up by exact addr, so we need
        # a smarter mock for contiguous reads
        t._contiguous = full_data
        original_read = t.read_memory

        def contiguous_read(addr, length):
            offset = addr - 0x2000
            data = full_data[offset : offset + length]
            return bytes(data + [0] * (length - len(data)))

        t.read_memory = contiguous_read
        result = read_bytes_chunked(t, 0x2000, 300, chunk_size=128)
        assert len(result) == 300
        assert result[:10] == bytes(range(10))
        assert result[128] == 128
        assert result[256] == 0  # wraps in range(44)

    def test_exact_chunk_boundary(self):
        t = MockTransport()
        data = [0xAA] * 256

        def contiguous_read(addr, length):
            offset = addr - 0x4000
            return bytes(data[offset : offset + length])

        t.read_memory = contiguous_read
        result = read_bytes_chunked(t, 0x4000, 256, chunk_size=128)
        assert len(result) == 256
        assert all(b == 0xAA for b in result)


class TestReadBytesAutoChunk:
    def test_small_read_not_chunked(self):
        """Reads <= threshold go through a single transport call."""
        t = MockTransport()
        calls = []
        original = t.read_memory

        def tracking_read(addr, length):
            calls.append((addr, length))
            return original(addr, length)

        t.read_memory = tracking_read
        read_bytes(t, 0x0400, 16)
        assert len(calls) == 1
        assert calls[0] == (0x0400, 16)

    def test_large_read_auto_chunked(self):
        """Reads > threshold are automatically chunked."""
        t = MockTransport()
        calls = []
        data = [0x55] * 512

        def tracking_read(addr, length):
            calls.append((addr, length))
            offset = addr - 0x0400
            return bytes(data[offset : offset + length])

        t.read_memory = tracking_read
        result = read_bytes(t, 0x0400, 512)
        assert len(result) == 512
        # Should have been split into multiple calls
        assert len(calls) > 1


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

    def test_write_bytes_small_not_chunked(self):
        """Writes <= chunk size go through as a single transport call."""
        t = MockTransport()
        data = bytes(range(_WRITE_CHUNK_SIZE))
        write_bytes(t, 0x1000, data)
        assert len(t.written_memory) == 1

    def test_write_bytes_large_auto_chunked(self):
        """Writes > chunk size are split into multiple transport calls."""
        t = MockTransport()
        size = _WRITE_CHUNK_SIZE * 3 + 7
        data = bytes(i & 0xFF for i in range(size))
        write_bytes(t, 0x1000, data)
        # Should be 4 calls: 3 full chunks + 1 partial
        assert len(t.written_memory) == 4
        # Verify addresses are sequential
        assert t.written_memory[0][0] == 0x1000
        assert t.written_memory[1][0] == 0x1000 + _WRITE_CHUNK_SIZE
        assert t.written_memory[2][0] == 0x1000 + _WRITE_CHUNK_SIZE * 2
        assert t.written_memory[3][0] == 0x1000 + _WRITE_CHUNK_SIZE * 3
        # Verify all data is present
        reassembled = []
        for _, chunk in t.written_memory:
            reassembled.extend(chunk)
        assert bytes(reassembled) == data

    def test_write_bytes_exact_chunk_boundary(self):
        """Write exactly 2x chunk size produces exactly 2 calls."""
        t = MockTransport()
        size = _WRITE_CHUNK_SIZE * 2
        data = bytes([0xAA] * size)
        write_bytes(t, 0x3000, data)
        assert len(t.written_memory) == 2
        assert len(t.written_memory[0][1]) == _WRITE_CHUNK_SIZE
        assert len(t.written_memory[1][1]) == _WRITE_CHUNK_SIZE

    def test_write_bytes_list_large_auto_chunked(self):
        """List input also gets chunked for large writes."""
        t = MockTransport()
        data = list(range(100))
        write_bytes(t, 0x4000, data)
        assert len(t.written_memory) > 1
        reassembled = []
        for _, chunk in t.written_memory:
            reassembled.extend(chunk)
        assert reassembled == data


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
