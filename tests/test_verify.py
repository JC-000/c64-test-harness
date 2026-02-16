"""Tests for PrgFile — PRG parsing and memory verification."""

import tempfile
from pathlib import Path

import pytest

from c64_test_harness.verify import PrgFile
from conftest import MockTransport


@pytest.fixture
def sample_prg(tmp_path):
    """Create a sample PRG file: load address $0801, 256 bytes of data."""
    data = bytes(range(256))
    prg_content = b"\x01\x08" + data  # load address $0801
    path = tmp_path / "test.prg"
    path.write_bytes(prg_content)
    return path, 0x0801, data


class TestPrgFileParsing:
    def test_load_address(self, sample_prg):
        path, expected_addr, _ = sample_prg
        prg = PrgFile.from_file(path)
        assert prg.load_address == expected_addr

    def test_end_address(self, sample_prg):
        path, load_addr, data = sample_prg
        prg = PrgFile.from_file(path)
        assert prg.end_address == load_addr + len(data)

    def test_data_content(self, sample_prg):
        path, _, expected_data = sample_prg
        prg = PrgFile.from_file(path)
        assert prg.data == expected_data

    def test_too_small_file(self, tmp_path):
        path = tmp_path / "tiny.prg"
        path.write_bytes(b"\x01")
        with pytest.raises(ValueError, match="too small"):
            PrgFile.from_file(path)


class TestBytesAt:
    def test_bytes_at_start(self, sample_prg):
        path, _, _ = sample_prg
        prg = PrgFile.from_file(path)
        result = prg.bytes_at(0x0801, 4)
        assert result == bytes([0, 1, 2, 3])

    def test_bytes_at_offset(self, sample_prg):
        path, _, _ = sample_prg
        prg = PrgFile.from_file(path)
        result = prg.bytes_at(0x0811, 4)
        assert result == bytes([16, 17, 18, 19])

    def test_bytes_at_end(self, sample_prg):
        path, _, _ = sample_prg
        prg = PrgFile.from_file(path)
        result = prg.bytes_at(0x08FD, 4)
        assert result == bytes([252, 253, 254, 255])

    def test_out_of_range_before(self, sample_prg):
        path, _, _ = sample_prg
        prg = PrgFile.from_file(path)
        with pytest.raises(ValueError, match="outside PRG"):
            prg.bytes_at(0x0700, 4)

    def test_out_of_range_after(self, sample_prg):
        path, _, _ = sample_prg
        prg = PrgFile.from_file(path)
        with pytest.raises(ValueError, match="outside PRG"):
            prg.bytes_at(0x0900, 4)


class TestVerifyRegion:
    def test_matching_region(self, sample_prg):
        path, _, data = sample_prg
        prg = PrgFile.from_file(path)
        t = MockTransport()

        def contiguous_read(addr, length):
            offset = addr - 0x0801
            return data[offset : offset + length]

        t.read_memory = contiguous_read
        ok, diffs = prg.verify_region(t, 0x0801, 32)
        assert ok is True
        assert diffs == 0

    def test_mismatched_region(self, sample_prg):
        path, _, data = sample_prg
        prg = PrgFile.from_file(path)
        t = MockTransport()
        corrupted = bytearray(data)
        corrupted[0] = 0xFF
        corrupted[1] = 0xFF

        def contiguous_read(addr, length):
            offset = addr - 0x0801
            return bytes(corrupted[offset : offset + length])

        t.read_memory = contiguous_read
        ok, diffs = prg.verify_region(t, 0x0801, 32)
        assert ok is False
        assert diffs == 2


class TestFirstDiff:
    def test_no_diff(self, sample_prg):
        path, _, data = sample_prg
        prg = PrgFile.from_file(path)
        t = MockTransport()

        def contiguous_read(addr, length):
            offset = addr - 0x0801
            return data[offset : offset + length]

        t.read_memory = contiguous_read
        assert prg.first_diff(t, 0x0801, 32) is None

    def test_finds_first_diff(self, sample_prg):
        path, _, data = sample_prg
        prg = PrgFile.from_file(path)
        t = MockTransport()
        corrupted = bytearray(data)
        corrupted[5] = 0xFF  # byte at $0806

        def contiguous_read(addr, length):
            offset = addr - 0x0801
            return bytes(corrupted[offset : offset + length])

        t.read_memory = contiguous_read
        result = prg.first_diff(t, 0x0801, 32)
        assert result is not None
        offset, expected, actual = result
        assert offset == 5
        assert expected == 5
        assert actual == 0xFF
