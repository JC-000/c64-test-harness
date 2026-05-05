"""Tests for c64_test_harness.disk — disk image management via c1541."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from c64_test_harness.disk import (
    DirEntry,
    DiskFormat,
    DiskImage,
    DiskImageError,
    FileType,
)

# Skip entire module if c1541 is not installed
pytestmark = pytest.mark.skipif(
    shutil.which("c1541") is None,
    reason="c1541 not found on PATH",
)


# ======================================================================
# Helpers
# ======================================================================

def _write_host_file(path: Path, data: bytes) -> Path:
    """Write *data* to *path* and return the path."""
    path.write_bytes(data)
    return path


# ======================================================================
# TestDiskImageCreate
# ======================================================================

class TestDiskImageCreate:
    """Creating new disk images."""

    def test_create_d64(self, tmp_path: Path) -> None:
        img = DiskImage.create(tmp_path / "test.d64")
        assert img.path.exists()
        assert img.format is DiskFormat.D64

    def test_create_d71(self, tmp_path: Path) -> None:
        img = DiskImage.create(tmp_path / "test.d71")
        assert img.path.exists()
        assert img.format is DiskFormat.D71

    def test_create_d81(self, tmp_path: Path) -> None:
        img = DiskImage.create(tmp_path / "test.d81")
        assert img.path.exists()
        assert img.format is DiskFormat.D81

    def test_create_custom_name_and_id(self, tmp_path: Path) -> None:
        img = DiskImage.create(tmp_path / "named.d64", name="MYDISK", disk_id="AB")
        assert img.path.exists()
        # The image should have an empty directory
        assert img.list_files() == []

    def test_create_explicit_format_override(self, tmp_path: Path) -> None:
        # Extension says .d64 but we force D71 format
        img = DiskImage.create(tmp_path / "trick.d64", fmt=DiskFormat.D71)
        assert img.format is DiskFormat.D71


# ======================================================================
# TestFormatDetection
# ======================================================================

class TestFormatDetection:
    """DiskImage.detect_format static method."""

    def test_d64(self) -> None:
        assert DiskImage.detect_format("foo.d64") is DiskFormat.D64

    def test_d71(self) -> None:
        assert DiskImage.detect_format("bar.d71") is DiskFormat.D71

    def test_d81(self) -> None:
        assert DiskImage.detect_format("baz.d81") is DiskFormat.D81

    def test_case_insensitive(self) -> None:
        assert DiskImage.detect_format("UPPER.D64") is DiskFormat.D64
        assert DiskImage.detect_format("Mixed.D81") is DiskFormat.D81

    def test_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown disk image extension"):
            DiskImage.detect_format("bad.bin")


# ======================================================================
# TestDriveType
# ======================================================================

class TestDriveType:
    """drive_type property maps format → VICE drive number."""

    def test_d64_drive_type(self, tmp_path: Path) -> None:
        img = DiskImage.create(tmp_path / "t.d64")
        assert img.drive_type == 1541

    def test_d71_drive_type(self, tmp_path: Path) -> None:
        img = DiskImage.create(tmp_path / "t.d71")
        assert img.drive_type == 1571

    def test_d81_drive_type(self, tmp_path: Path) -> None:
        img = DiskImage.create(tmp_path / "t.d81")
        assert img.drive_type == 1581


# ======================================================================
# TestWriteAndRead
# ======================================================================

class TestWriteAndRead:
    """Writing files to and reading files from disk images."""

    def test_write_prg_default(self, tmp_path: Path) -> None:
        img = DiskImage.create(tmp_path / "test.d64")
        host = _write_host_file(tmp_path / "hello.prg", b"\x01\x08hello")
        img.write_file(host, "HELLO")
        assert img.file_exists("HELLO")

    def test_write_seq(self, tmp_path: Path) -> None:
        img = DiskImage.create(tmp_path / "test.d64")
        host = _write_host_file(tmp_path / "data.seq", b"sequential data")
        img.write_file(host, "DATA", file_type=FileType.SEQ)
        entries = img.list_files()
        seq_entries = [e for e in entries if e.file_type is FileType.SEQ]
        assert len(seq_entries) == 1
        assert seq_entries[0].name == "DATA"

    def test_read_to_host(self, tmp_path: Path) -> None:
        img = DiskImage.create(tmp_path / "test.d64")
        original = b"\x01\x08test data 1234"
        host_in = _write_host_file(tmp_path / "in.prg", original)
        img.write_file(host_in, "MYFILE")

        out_path = tmp_path / "out.prg"
        img.read_file("MYFILE", out_path)
        assert out_path.read_bytes() == original

    def test_read_file_bytes(self, tmp_path: Path) -> None:
        img = DiskImage.create(tmp_path / "test.d64")
        original = b"\x00\x10ABCDEFGH"
        host_in = _write_host_file(tmp_path / "in.prg", original)
        img.write_file(host_in, "BYTES")
        assert img.read_file_bytes("BYTES") == original

    def test_read_nonexistent_raises(self, tmp_path: Path) -> None:
        img = DiskImage.create(tmp_path / "test.d64")
        with pytest.raises(DiskImageError):
            img.read_file("NOPE", tmp_path / "out.prg")

    def test_write_nonexistent_host_raises(self, tmp_path: Path) -> None:
        img = DiskImage.create(tmp_path / "test.d64")
        with pytest.raises((DiskImageError, FileNotFoundError)):
            img.write_file(tmp_path / "nope.prg", "GONE")


# ======================================================================
# TestDeleteFile
# ======================================================================

class TestDeleteFile:
    """Deleting files from disk images."""

    def test_delete_existing(self, tmp_path: Path) -> None:
        img = DiskImage.create(tmp_path / "test.d64")
        host = _write_host_file(tmp_path / "f.prg", b"\x01\x08x")
        img.write_file(host, "DELME")
        assert img.file_exists("DELME")
        img.delete_file("DELME")
        assert not img.file_exists("DELME")

    def test_delete_nonexistent(self, tmp_path: Path) -> None:
        img = DiskImage.create(tmp_path / "test.d64")
        # c1541 may or may not error — we just ensure no crash
        try:
            img.delete_file("NOPE")
        except DiskImageError:
            pass  # acceptable


# ======================================================================
# TestListFiles
# ======================================================================

class TestListFiles:
    """Directory listing."""

    def test_empty_disk(self, tmp_path: Path) -> None:
        img = DiskImage.create(tmp_path / "test.d64")
        assert img.list_files() == []

    def test_list_after_writes(self, tmp_path: Path) -> None:
        img = DiskImage.create(tmp_path / "test.d64")
        for name in ("FILE1", "FILE2", "FILE3"):
            host = _write_host_file(tmp_path / f"{name}.prg", b"\x01\x08data")
            img.write_file(host, name)
        entries = img.list_files()
        names = {e.name for e in entries}
        assert names == {"FILE1", "FILE2", "FILE3"}

    def test_dir_entry_fields(self, tmp_path: Path) -> None:
        img = DiskImage.create(tmp_path / "test.d64")
        host = _write_host_file(tmp_path / "f.prg", b"\x01\x08" + b"X" * 200)
        img.write_file(host, "BIGFILE")
        entries = img.list_files()
        assert len(entries) == 1
        e = entries[0]
        assert e.name == "BIGFILE"
        assert e.blocks >= 1
        assert e.file_type is FileType.PRG


# ======================================================================
# TestFileExists
# ======================================================================

class TestFileExists:
    """file_exists convenience method."""

    def test_exists_true(self, tmp_path: Path) -> None:
        img = DiskImage.create(tmp_path / "test.d64")
        host = _write_host_file(tmp_path / "f.prg", b"\x01\x08ok")
        img.write_file(host, "PRESENT")
        assert img.file_exists("PRESENT") is True

    def test_exists_false(self, tmp_path: Path) -> None:
        img = DiskImage.create(tmp_path / "test.d64")
        assert img.file_exists("ABSENT") is False


# ======================================================================
# TestOverwriteFile
# ======================================================================

class TestOverwriteFile:
    """overwrite_file (delete + write)."""

    def test_overwrite_replaces_content(self, tmp_path: Path) -> None:
        img = DiskImage.create(tmp_path / "test.d64")
        v1 = _write_host_file(tmp_path / "v1.prg", b"\x01\x08version1")
        img.write_file(v1, "DATA")

        v2 = _write_host_file(tmp_path / "v2.prg", b"\x01\x08version2")
        img.overwrite_file(v2, "DATA")

        assert img.read_file_bytes("DATA") == b"\x01\x08version2"

    def test_overwrite_new_file(self, tmp_path: Path) -> None:
        img = DiskImage.create(tmp_path / "test.d64")
        host = _write_host_file(tmp_path / "new.prg", b"\x01\x08fresh")
        img.overwrite_file(host, "NEW")
        assert img.file_exists("NEW")
        assert img.read_file_bytes("NEW") == b"\x01\x08fresh"


# ======================================================================
# TestOpenExisting
# ======================================================================

class TestOpenExisting:
    """Opening pre-existing disk images."""

    def test_open_existing(self, tmp_path: Path) -> None:
        created = DiskImage.create(tmp_path / "existing.d64")
        host = _write_host_file(tmp_path / "f.prg", b"\x01\x08x")
        created.write_file(host, "MARKER")

        opened = DiskImage(tmp_path / "existing.d64")
        assert opened.format is DiskFormat.D64
        assert opened.file_exists("MARKER")

    def test_open_nonexistent_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            DiskImage(tmp_path / "ghost.d64")


# ======================================================================
# TestRoundTrip
# ======================================================================

class TestRoundTrip:
    """End-to-end write → read round-trips."""

    @pytest.mark.parametrize("ext,fmt", [
        (".d64", DiskFormat.D64),
        (".d71", DiskFormat.D71),
        (".d81", DiskFormat.D81),
    ])
    def test_prg_round_trip_all_formats(self, tmp_path: Path, ext: str, fmt: DiskFormat) -> None:
        img = DiskImage.create(tmp_path / f"rt{ext}")
        data = b"\x01\x08round trip payload"
        host = _write_host_file(tmp_path / "input.prg", data)
        img.write_file(host, "RTFILE")
        assert img.read_file_bytes("RTFILE") == data

    def test_binary_integrity_all_byte_values(self, tmp_path: Path) -> None:
        """Every possible byte value survives a round-trip."""
        img = DiskImage.create(tmp_path / "binary.d64")
        data = bytes(range(256))
        host = _write_host_file(tmp_path / "allbytes.prg", data)
        img.write_file(host, "ALLBYTES")
        assert img.read_file_bytes("ALLBYTES") == data

    def test_multiple_files(self, tmp_path: Path) -> None:
        img = DiskImage.create(tmp_path / "multi.d64")
        files = {"FILE1": b"\x01\x08aaa", "FILE2": b"\x01\x08bbb", "FILE3": b"\x01\x08ccc"}
        for name, data in files.items():
            host = _write_host_file(tmp_path / f"{name}.prg", data)
            img.write_file(host, name)

        for name, expected in files.items():
            assert img.read_file_bytes(name) == expected
