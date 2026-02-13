"""Disk image management for c64-test-harness.

Create, read, write, and manipulate CBM disk images (D64/D71/D81)
using VICE's ``c1541`` command-line tool.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class DiskImageError(Exception):
    """Raised when a c1541 operation fails."""


class DiskFormat(Enum):
    """Supported CBM disk image formats."""

    D64 = "d64"
    D71 = "d71"
    D81 = "d81"


class FileType(Enum):
    """CBM file types."""

    PRG = "prg"
    SEQ = "seq"


@dataclass
class DirEntry:
    """A single directory entry on a CBM disk image."""

    name: str
    blocks: int
    file_type: FileType


class DiskImage:
    """Manage a CBM disk image via VICE's c1541 utility.

    Parameters
    ----------
    path
        Path to an existing disk image file.
    fmt
        Disk format.  If *None*, auto-detected from the file extension.

    Raises
    ------
    FileNotFoundError
        If *path* does not exist.
    ValueError
        If the format cannot be detected from the extension.
    DiskImageError
        If c1541 is not found on the system.
    """

    def __init__(self, path: str | os.PathLike[str], fmt: DiskFormat | None = None) -> None:
        self._path = Path(path)
        if not self._path.exists():
            raise FileNotFoundError(f"Disk image not found: {self._path}")
        self._format = fmt or self.detect_format(self._path)
        self._c1541 = self.find_c1541()

    # ------------------------------------------------------------------
    # Class method: create a new, formatted disk image
    # ------------------------------------------------------------------

    @classmethod
    def create(
        cls,
        path: str | os.PathLike[str],
        name: str = "TEST",
        disk_id: str = "00",
        fmt: DiskFormat | None = None,
    ) -> DiskImage:
        """Create and format a new disk image.

        Parameters
        ----------
        path
            Where to write the image file.
        name
            Disk name (up to 16 characters).
        disk_id
            Two-character disk ID.
        fmt
            Disk format.  Detected from extension if *None*.
        """
        p = Path(path)
        resolved_fmt = fmt or cls.detect_format(p)
        c1541 = cls.find_c1541()

        format_flag = {
            DiskFormat.D64: "d64",
            DiskFormat.D71: "d71",
            DiskFormat.D81: "d81",
        }[resolved_fmt]

        result = subprocess.run(
            [c1541, "-format", f"{name},{disk_id}", format_flag, str(p)],
            capture_output=True,
        )
        if result.returncode != 0:
            output = (result.stderr or result.stdout or b"").decode("latin-1")
            raise DiskImageError(f"c1541 format failed (rc={result.returncode}): {output}")
        return cls(p, resolved_fmt)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def path(self) -> Path:
        """Absolute path to the disk image file."""
        return self._path

    @property
    def format(self) -> DiskFormat:
        """Disk format (D64, D71, D81)."""
        return self._format

    @property
    def drive_type(self) -> int:
        """VICE drive-type number matching this format."""
        return {
            DiskFormat.D64: 1541,
            DiskFormat.D71: 1571,
            DiskFormat.D81: 1581,
        }[self._format]

    # ------------------------------------------------------------------
    # File operations
    # ------------------------------------------------------------------

    def write_file(
        self,
        host_path: str | os.PathLike[str],
        c64_name: str,
        file_type: FileType = FileType.PRG,
    ) -> None:
        """Write a host file into the disk image.

        Parameters
        ----------
        host_path
            Path to the file on the host filesystem.
        c64_name
            Name to give the file on the CBM disk (max 16 chars).
        file_type
            PRG (default) or SEQ.
        """
        suffix = ",s" if file_type is FileType.SEQ else ""
        self._run_c1541(f"-write", str(Path(host_path)), f"{c64_name}{suffix}")

    def read_file(self, c64_name: str, host_path: str | os.PathLike[str]) -> None:
        """Read a file from the disk image to a host path.

        Parameters
        ----------
        c64_name
            Name of the file on the CBM disk.
        host_path
            Where to write the extracted file on the host.
        """
        self._run_c1541("-read", c64_name, str(Path(host_path)))

    def read_file_bytes(self, c64_name: str) -> bytes:
        """Read a file from the disk image and return its contents as bytes.

        Parameters
        ----------
        c64_name
            Name of the file on the CBM disk.
        """
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp_path = tmp.name
        try:
            self.read_file(c64_name, tmp_path)
            return Path(tmp_path).read_bytes()
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def delete_file(self, c64_name: str) -> None:
        """Delete a file from the disk image.

        Parameters
        ----------
        c64_name
            Name of the file to delete.
        """
        self._run_c1541("-delete", c64_name)

    def overwrite_file(
        self,
        host_path: str | os.PathLike[str],
        c64_name: str,
        file_type: FileType = FileType.PRG,
    ) -> None:
        """Overwrite (delete + write) a file on the disk image.

        CBM DOS ``@0:`` overwrite is historically unreliable, so this
        method deletes the old file first, then writes the new one.
        """
        if self.file_exists(c64_name):
            self.delete_file(c64_name)
        self.write_file(host_path, c64_name, file_type)

    def list_files(self) -> list[DirEntry]:
        """Return the directory listing of the disk image."""
        result = subprocess.run(
            [self._c1541, str(self._path), "-list"],
            capture_output=True,
        )
        if result.returncode != 0:
            output = (result.stderr or result.stdout or b"").decode("latin-1")
            raise DiskImageError(f"c1541 list failed (rc={result.returncode}): {output}")

        entries: list[DirEntry] = []
        for line in result.stdout.decode("latin-1").splitlines():
            m = re.match(r'^\s*(\d+)\s+"(.+?)"\s+(\w+)\s*$', line)
            if m:
                blocks = int(m.group(1))
                name = m.group(2).rstrip()
                raw_type = m.group(3).lower().strip()
                try:
                    ft = FileType(raw_type)
                except ValueError:
                    continue
                entries.append(DirEntry(name=name, blocks=blocks, file_type=ft))
        return entries

    def file_exists(self, c64_name: str) -> bool:
        """Check whether a file exists on the disk image."""
        return any(e.name == c64_name for e in self.list_files())

    # ------------------------------------------------------------------
    # Static helpers
    # ------------------------------------------------------------------

    @staticmethod
    def detect_format(path: str | os.PathLike[str]) -> DiskFormat:
        """Detect disk format from a file extension.

        Raises
        ------
        ValueError
            If the extension is not recognised.
        """
        ext = Path(path).suffix.lower()
        mapping = {
            ".d64": DiskFormat.D64,
            ".d71": DiskFormat.D71,
            ".d81": DiskFormat.D81,
        }
        if ext not in mapping:
            raise ValueError(f"Unknown disk image extension: {ext!r}")
        return mapping[ext]

    @staticmethod
    def find_c1541() -> str:
        """Locate the ``c1541`` binary.

        Raises
        ------
        DiskImageError
            If c1541 cannot be found on ``$PATH``.
        """
        path = shutil.which("c1541")
        if path is None:
            raise DiskImageError("c1541 not found on PATH")
        return path

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run_c1541(self, *commands: str) -> str:
        """Run c1541 in batch mode against this image.

        Parameters
        ----------
        *commands
            Command-line arguments passed after the image path.

        Returns
        -------
        str
            Combined stdout + stderr from c1541.

        Raises
        ------
        DiskImageError
            If c1541 returns a non-zero exit code or prints an error.
        """
        args = [self._c1541, str(self._path)] + list(commands)
        result = subprocess.run(args, capture_output=True)
        output = (result.stdout or b"").decode("latin-1") + (result.stderr or b"").decode("latin-1")
        if result.returncode != 0:
            raise DiskImageError(f"c1541 failed (rc={result.returncode}): {output}")
        return output
