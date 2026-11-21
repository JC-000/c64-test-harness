"""Tests for PrgFile — PRG parsing and memory verification."""

import tempfile
from pathlib import Path

import pytest

from c64_test_harness.memory import ShortReadError
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

    def test_empty_read_fails_verification(self, sample_prg):
        """Regression: zip() truncation made a failed (empty) read
        verify as intact with 0 diffs."""
        path, _, _ = sample_prg
        prg = PrgFile.from_file(path)
        t = MockTransport()
        t.read_memory = lambda addr, length: b""
        ok, diffs = prg.verify_region(t, 0x0801, 32)
        assert ok is False
        assert diffs == 32  # every missing byte counts as a diff

    def test_short_read_fails_verification(self, sample_prg):
        """A truncated read never verifies as intact, even if the bytes
        that did arrive all match."""
        path, _, data = sample_prg
        prg = PrgFile.from_file(path)
        t = MockTransport()

        def short_read(addr, length):
            offset = addr - 0x0801
            return data[offset : offset + length - 8]  # 8 bytes missing

        t.read_memory = short_read
        ok, diffs = prg.verify_region(t, 0x0801, 32)
        assert ok is False
        assert diffs == 8


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


# --- verify_load (#573) -------------------------------------------------------
#
# A KERNAL LOAD leaves the end pointer EAL ($AE/$AF) one past the last byte it
# stored. On an Ultimate a REST readmem during the LOAD halts the 6510 while the
# CIA1 timer runs on, the KERNAL takes a false EOI and the LOAD ends early with
# ST=$40 and no error (#573, GideonZ/1541ultimate#1120). ST=$40 is also what a
# complete LOAD leaves, and a repeated LOAD of the same file leaves the earlier
# copy's tail in RAM, so only the end pointer separates the two.


class _Ram:
    """A 64 KiB RAM image with the read_memory a transport has."""

    def __init__(self) -> None:
        self.ram = bytearray(0x10000)
        self.reads: list[tuple[int, int]] = []

    def put(self, addr: int, data: bytes) -> None:
        self.ram[addr : addr + len(data)] = data

    def kernal_state(self, end: int, st: int = 0x40) -> None:
        self.ram[0xAE] = end & 0xFF
        self.ram[0xAF] = end >> 8
        self.ram[0x90] = st

    def read_memory(self, addr: int, length: int) -> bytes:
        self.reads.append((addr, length))
        return bytes(self.ram[addr : addr + length])


@pytest.fixture
def load_prg():
    """The #1120 measurement's shape: 2000 bytes at $4000, complete end $47D0."""
    data = bytes((i * 7 + 3) & 0xFF for i in range(2000))
    return PrgFile(0x4000, data)


class TestVerifyLoad:
    def test_complete_load(self, load_prg):
        t = _Ram()
        t.put(0x4000, load_prg.data)
        t.kernal_state(0x47D0)
        r = load_prg.verify_load(t)
        assert r.complete and not r.short
        assert (r.start, r.expected_end, r.end_pointer) == (0x4000, 0x47D0, 0x47D0)
        assert r.content_diffs == 0

    def test_short_load_over_an_earlier_copy_is_caught_by_the_end_pointer(self, load_prg):
        # The earlier LOAD of the same file left all 2000 bytes in RAM, so the
        # content matches; ST=$40 as on a complete load. Only EAL says short.
        t = _Ram()
        t.put(0x4000, load_prg.data)
        t.kernal_state(0x4652)
        r = load_prg.verify_load(t)
        assert r.content_diffs == 0
        assert r.status == 0x40
        assert r.end_pointer == 0x4652
        assert r.short and not r.complete

    def test_short_load_into_clear_ram(self, load_prg):
        t = _Ram()
        t.put(0x4000, load_prg.data[:5])
        t.kernal_state(0x4005)
        r = load_prg.verify_load(t)
        assert r.short and not r.complete
        assert r.content_diffs > 0

    def test_end_pointer_past_the_file_is_not_complete_and_not_short(self, load_prg):
        t = _Ram()
        t.put(0x4000, load_prg.data)
        t.kernal_state(0x47D1)
        r = load_prg.verify_load(t)
        assert not r.complete and not r.short

    def test_right_end_wrong_bytes_is_not_complete(self, load_prg):
        t = _Ram()
        bad = bytearray(load_prg.data)
        bad[100] ^= 0xFF
        bad[1999] ^= 0xFF
        t.put(0x4000, bytes(bad))
        t.kernal_state(0x47D0)
        r = load_prg.verify_load(t)
        assert not r.complete and not r.short
        assert r.content_diffs == 2

    def test_relocated_load_checks_the_start_given(self, load_prg):
        # LOAD"X",8 (secondary address 0) loads at the BASIC start, not the header.
        t = _Ram()
        t.put(0x0801, load_prg.data)
        t.kernal_state(0x0801 + 2000)
        r = load_prg.verify_load(t, start=0x0801)
        assert r.complete
        assert (r.start, r.expected_end) == (0x0801, 0x0FD1)
        assert any(a == 0x0801 for a, _ in t.reads)
        assert not any(0x4000 <= a < 0x47D0 for a, _ in t.reads)

    def test_status_is_reported_as_read(self, load_prg):
        t = _Ram()
        t.put(0x4000, load_prg.data)
        t.kernal_state(0x47D0, st=0x42)
        assert load_prg.verify_load(t).status == 0x42

    def test_short_chunked_read_raises(self, load_prg):
        # 2000 bytes go through read_bytes_chunked, which retries a short
        # chunk once and then raises rather than return a misaligned result.
        t = _Ram()
        t.put(0x4000, load_prg.data)
        t.kernal_state(0x47D0)
        real = t.read_memory
        t.read_memory = lambda a, n: real(a, n)[:-1] if a >= 0x4000 else real(a, n)
        with pytest.raises(ShortReadError):
            load_prg.verify_load(t)

    def test_short_end_pointer_read_raises_not_reports_short(self, load_prg):
        # A failed pointer read must not come back as a "short" LOAD.
        t = _Ram()
        t.put(0x4000, load_prg.data)
        t.kernal_state(0x47D0)
        real = t.read_memory
        t.read_memory = lambda a, n: real(a, n)[:1] if a == 0xAE else real(a, n)
        with pytest.raises(ShortReadError):
            load_prg.verify_load(t)

    def test_short_single_read_is_never_complete(self):
        # 16 bytes is one read_memory call; a read that comes back short must
        # count the missing bytes, not zip them away.
        prg = PrgFile(0x4000, bytes(range(1, 17)))
        t = _Ram()
        t.put(0x4000, prg.data)
        t.kernal_state(0x4010)
        real = t.read_memory
        t.read_memory = lambda a, n: real(a, n)[:-3] if a == 0x4000 else real(a, n)
        r = prg.verify_load(t)
        assert not r.complete
        assert r.content_diffs == 3

    def test_load_past_ffff_is_refused_before_any_read(self, load_prg):
        t = _Ram()
        with pytest.raises(ValueError, match="FFFF"):
            load_prg.verify_load(t, start=0xF900)
        assert t.reads == []

    def test_bool_start_is_refused_before_any_read(self, load_prg):
        t = _Ram()
        with pytest.raises(ValueError):
            load_prg.verify_load(t, start=True)
        assert t.reads == []

    @pytest.mark.parametrize("start", [-1, 0x10000])
    def test_start_outside_the_address_space_is_refused_before_any_read(self, start):
        prg = PrgFile(0x4000, b"\x01")
        t = _Ram()
        with pytest.raises(ValueError, match="outside"):
            prg.verify_load(t, start=start)
        assert t.reads == []

    @pytest.mark.parametrize("start,length", [(0xFE00, 512), (0xFFFF, 1)])
    def test_load_ending_at_ffff_wraps_the_pointer_and_is_complete(self, start, length):
        # The KERNAL stores, then INC $AE / BNE / INC $AF: after a byte at
        # $FFFF the 16-bit pointer reads $0000.
        prg = PrgFile(start, bytes((i * 5 + 1) & 0xFF for i in range(length)))
        t = _Ram()
        t.put(start, prg.data)
        t.kernal_state(0x0000)
        r = prg.verify_load(t)
        assert r.expected_end == 0x10000
        assert r.complete and not r.short

    def test_load_short_of_ffff_is_short(self):
        prg = PrgFile(0xFE00, bytes(512))
        t = _Ram()
        t.kernal_state(0xFF00)
        r = prg.verify_load(t)
        assert r.short and not r.complete

    def test_short_status_read_raises(self, load_prg):
        t = _Ram()
        t.put(0x4000, load_prg.data)
        t.kernal_state(0x47D0)
        real = t.read_memory
        t.read_memory = lambda a, n: b"" if a == 0x90 else real(a, n)
        with pytest.raises(ShortReadError):
            load_prg.verify_load(t)

    def test_result_is_immutable(self, load_prg):
        import dataclasses

        t = _Ram()
        t.put(0x4000, load_prg.data)
        t.kernal_state(0x4652)
        r = load_prg.verify_load(t)
        with pytest.raises(dataclasses.FrozenInstanceError):
            r.end_pointer = 0x47D0
