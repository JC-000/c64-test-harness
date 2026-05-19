"""Cross-backend C64 snapshot — Phases A + B + C.

Round-trips RAM, CPU port, mounted disk images, and now REU contents
between VICE and Ultimate 64, using VICE's native ``.vsf`` format as the
on-disk wire (with optional sidecar files for drives and a Phase C
``reu.bin`` bundle file).

Phase A is deliberately seed-only: a restored snapshot loads the RAM
image but does NOT resume at the exact PC/cycle.  CPU register, VIC-II,
SID, and CIA state are out of scope and will follow in later phases.

Phase C — REU
-------------

The optional ``Snapshot.reu_contents`` field carries up to 16 MB of REU
bytes (sizes match the device's enum: 128 KB, 256 KB, ..., 16 MB).
On the U64 side restore pushes the bytes through
:class:`~c64_test_harness.backends.u64_socket_dma.SocketDMAClient` —
specifically its 0xFF07 REUWRITE opcode — which is fast (a few seconds
even for the full 16 MB) because it's a persistent TCP socket on port
64.  Extract is slower: the U64 REST API has no REU readback endpoint,
so :func:`extract_snapshot` uses a DMA-via-staging window at
``$0800–$87FF``, with the CPU paused first, programming the REU
registers at ``$DF02–$DF09`` to copy 32 KB banks from REU into the
window, then reading them back via ``read_memory``.  Staging-window
contents are stashed and restored under a ``try/finally`` so even a
partial failure leaves the original bytes intact.  All staging writes
carry ``override="reu-snapshot-staging"`` so :class:`MemoryPolicy`
doesn't block them.

Design notes
------------

* ``Snapshot`` is a frozen dataclass holding the minimum useful state:
  64 KB RAM, the two CPU port bytes ($00 direction, $01 data), and the
  EXROM/GAME cartridge-control lines (defaulted to ``1`` — no cart).

* :func:`extract_snapshot` reads state out of any
  :class:`~c64_test_harness.transport.C64Transport`-conforming backend
  by issuing read_memory calls — the same code path for VICE and U64.
  This is simpler and more uniform than parsing a VICE-emitted ``.vsf``
  on the VICE side.

* :func:`restore_snapshot` writes RAM and CPU port back through
  ``write_memory``.  Because the natural path of 64 KB writes will
  collide with most :class:`~c64_test_harness.MemoryPolicy` reserved
  regions, a single WARNING is logged at the start of the restore and
  each underlying ``write_memory`` call carries
  ``override="snapshot-restore"``.

* :meth:`Snapshot.to_vsf` emits a complete VICE ``.vsf`` by taking a
  bundled reference template (captured from VICE 3.10 at the BASIC READY
  prompt) and overwriting the ``C64MEM`` module body with this
  snapshot's RAM + CPU port.  All other modules (MAINCPU, CIA1, CIA2,
  SID, VIC-II, GLUE, drives, joyports, ...) are preserved verbatim from
  the template.  This is required because empirically VICE 3.10 refuses
  to load a snapshot missing any of its expected ~30 modules — see the
  spike notes in commit history.

* :meth:`Snapshot.from_vsf` parses any well-formed ``.vsf`` by scanning
  for the ``C64MEM`` module and extracting the prefix bytes and RAM.
  Other modules are skipped via their length field.

VSF byte layout (relevant subset, all multi-byte fields little-endian)
----------------------------------------------------------------------

File header (58 bytes for VICE 3.5+):

* 0x00, 19 bytes: ``b"VICE Snapshot File\\x1A"``
* 0x13, 1 byte: format major (``2``)
* 0x14, 1 byte: format minor (``0``)
* 0x15, 16 bytes: machine name zero-padded (``"C64SC"`` for x64sc)
* 0x25, 13 bytes: ``b"VICE Version\\x1A"``
* 0x32, 4 bytes: release version (major, minor, micro, 0)
* 0x36, 4 bytes: SVN revision (u32)

Module header (22 bytes):

* 16 bytes: module name zero-padded
* 1 byte:  VMAJOR
* 1 byte:  VMINOR
* 4 bytes: total module size including this header (u32)

``C64MEM`` module body (VMINOR=1, the layout VICE 3.10 requires):

* 1 byte:  CPU port data register ($01)
* 1 byte:  CPU port direction register ($00)
* 1 byte:  EXROM line (1 = high / not asserted = no cart)
* 1 byte:  GAME line   (1 = high / not asserted = no cart)
* 65536 bytes: RAM image
* 15 bytes: VICE-internal CPU-port delayed-bit state (zero is accepted)

→ body = 65555 bytes, module = 65577 bytes.
"""

from __future__ import annotations

import json
import logging
import struct
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .transport import C64Transport

__all__ = [
    "CpuRegisters",
    "DriveState",
    "Snapshot",
    "SnapshotFormatError",
    "extract_snapshot",
    "restore_snapshot",
]

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# VSF format constants
# ---------------------------------------------------------------------------

_VSF_MAGIC = b"VICE Snapshot File\x1A"
assert len(_VSF_MAGIC) == 19
_VSF_VERSION_TAG = b"VICE Version\x1A"
assert len(_VSF_VERSION_TAG) == 13

_VSF_FORMAT_MAJOR = 2
_VSF_FORMAT_MINOR = 0
_VSF_MACHINE_NAME = b"C64SC"  # what x64sc writes; required for compatibility
_VSF_MACHINE_FIELD_LEN = 16
_VSF_RELEASE_DEFAULT = (3, 10, 0, 0)  # plausible VICE 3.x triple + reserved 0
_VSF_FILE_HEADER_LEN = 19 + 1 + 1 + 16 + 13 + 4 + 4  # = 58 bytes

_MODULE_NAME_LEN = 16
_MODULE_HEADER_LEN = _MODULE_NAME_LEN + 1 + 1 + 4  # = 22

_C64MEM_MODULE_NAME = b"C64MEM"
_C64MEM_VMAJOR = 0
_C64MEM_VMINOR = 1  # VICE 3.5+ layout — what 3.10 requires
_C64MEM_TRAILER_LEN = 15  # VICE-internal CPU-port delayed-bit state
_C64MEM_BODY_LEN = 4 + 65536 + _C64MEM_TRAILER_LEN  # = 65555

# I/O register-bank sizes.
_CIA_REGS_LEN = 16  # $DC00-$DC0F / $DD00-$DD0F register file
_VIC_REGS_LEN = 47  # $D000-$D02E register file
_SID_REGS_LEN = 32  # $D400-$D41F register file
# REU1764 module — VICE writes this for the RAM Expansion Unit.
# Body layout (empirically captured from VICE 3.10 at BASIC READY with
# REU enabled, no transfers performed):
#
#   bytes 0..2:   REU size in KB, 24-bit LE  (e.g. 128 / 16384)
#   byte  3:      reserved / unused (zero)
#   bytes 4..14:  11 registers $DF00..$DF0A
#   bytes 15..19: 5 bytes VICE-internal DMA state (FFs in the idle snapshot)
#   bytes 20..N:  N bytes REU contents (N = size_KB * 1024)
#
# Total preamble length = 20.
_REU_MODULE_NAME = b"REU1764"
_REU_VMAJOR = 0
_REU_VMINOR = 0
_REU_PREAMBLE_LEN = 20

# Idle-REU control register snapshot — what VICE writes when REU is
# enabled but has performed no transfers since boot.  Reproduces the
# observed preamble bytes 4..19 from a clean VICE 3.10 capture:
#
#   80 00 00 00  00 10 00 00 00 00 f8 ff ff 1f 3f  ff ff ff ff ff
#   |-- size --| |---- 11 DF regs (DF00..DF0A) ---| |- internal -|
#
# Bytes 4..14 (11) = DF00..DF0A; bytes 15..19 (5) = internal DMA state.
_REU_IDLE_DF_REGS = bytes([
    0x00, 0x10, 0x00, 0x00, 0x00, 0x00, 0xF8, 0xFF, 0xFF, 0x1F, 0x3F,
])
_REU_IDLE_INTERNAL = bytes([0xFF, 0xFF, 0xFF, 0xFF, 0xFF])
assert len(_REU_IDLE_DF_REGS) == 11
assert len(_REU_IDLE_INTERNAL) == 5

# REU size enum — matches the U64 schema (REU_SIZE_VALUES).  Used to
# validate ``Snapshot.reu_size_bytes`` so the snapshot is restorable
# onto either backend.
_REU_SIZE_BYTES: tuple[int, ...] = (
    128 * 1024,
    256 * 1024,
    512 * 1024,
    1 * 1024 * 1024,
    2 * 1024 * 1024,
    4 * 1024 * 1024,
    8 * 1024 * 1024,
    16 * 1024 * 1024,
)

# Staging-window for the U64 REU→host extract path.  32 KB window at
# $0800–$87FF.  Stays inside C64 RAM and clear of the harness's own
# scratch regions (see docs/memory_safety.md).
_REU_STAGING_ADDR = 0x0800
_REU_STAGING_LEN = 0x8000  # 32 KB per bank read

# REU command byte — DMA REU→C64, execute now, no autoload.
# Bit layout: bit7=execute, bit6=autoload, bit5=ff00, bits1-0=direction
#   0b1001_0001 = $91  (execute, no autoload, REU→C64)
_REU_CMD_REU_TO_C64 = 0x91

_TEMPLATE_PATH = Path(__file__).with_name("_vsf_template.vsf")

# I/O register-bank slice locations inside each VICE 3.10 module body.
# Verified empirically (May 2026, x64sc 3.10 build on macOS) by writing
# a distinctive byte pattern to the corresponding $D000-$D02E /
# $D400-$D41F / $DC00-$DC0F / $DD00-$DD0F memory window through the
# binary monitor and inspecting the dumped .vsf — see commit history
# for the probe script.  Module module-body layouts:
#
#  * CIA1 v2.5 (77 bytes body): first 16 bytes are the register file
#    readback of $DC00-$DC0F.  Remaining bytes are timer latches /
#    TOD alarm / IRQ mask internals — left as template.
#  * CIA2 v2.5 (77 bytes body): same layout as CIA1, at $DD00-$DD0F.
#  * SID  v1.5 (36 bytes body): bytes 0..3 are an engine-state prefix
#    (always `00 00 01 01` at the BASIC READY template), bytes 4..35
#    are the SID register file ($D400-$D41F).
#  * VIC-II v1.3 (109207 bytes body): byte 0 is a single-byte prefix
#    (irq-condition flag), bytes 1..47 are the VIC register file
#    ($D000-$D02E).  The remaining ~109 KB is sequencer internals
#    (DMA caches, sprite shift registers, pixel pipeline) and is left
#    as template — VICE re-derives most of it during the next frame.
#
# Each entry: (module_name, body_offset, length).
_REGISTER_MODULE_SLICES: tuple[tuple[bytes, int, int], ...] = (
    (b"CIA1", 0, _CIA_REGS_LEN),
    (b"CIA2", 0, _CIA_REGS_LEN),
    (b"SID", 4, _SID_REGS_LEN),
    (b"VIC-II", 1, _VIC_REGS_LEN),
)


# MAINCPU module register-block layout, empirically derived from the
# bundled VICE 3.10 template (vmajor=1, vminor=4):
#
#   bytes  0..3  clock counter low DWORD
#   bytes  4..7  additional clock / reserved (treated opaquely; preserved verbatim)
#   byte   8     A
#   byte   9     X
#   byte   10    Y
#   byte   11    SP
#   bytes  12..13  PC (little-endian)
#   byte   14    P (processor status flags)
#   bytes  15..   last opcode info + remaining CPU state (preserved verbatim)
#
# The cross-check is that for the template (captured at the BASIC READY
# prompt) the bytes at offset 8..14 decode to A=0,X=0,Y=$0A,SP=$F3,
# PC=$E5D1 (BASIC main input loop), P=$22 (Z+unused) — see the offline
# round-trip test.
_MAINCPU_MODULE_NAME = b"MAINCPU"
_MAINCPU_REG_OFFSET = 8  # byte offset of A within the module body


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SnapshotFormatError(ValueError):
    """The .vsf bytes are malformed or not a recognised snapshot."""


# ---------------------------------------------------------------------------
# CpuRegisters — 6510 register state — Phase D
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CpuRegisters:
    """6510 CPU register state captured in / restored from a snapshot.

    Attributes
    ----------
    pc:
        Program counter — 16-bit address ``0..0xFFFF``.
    a, x, y:
        Accumulator and the two index registers — 8-bit ``0..0xFF``.
    sp:
        Stack pointer — 8-bit ``0..0xFF`` (it actually addresses
        ``$0100 + sp`` on the hardware, but the register itself is one
        byte).
    p:
        Processor status flags — 8-bit ``0..0xFF`` (bit 7 = N, bit 6 = V,
        bit 5 = unused/always-1, bit 4 = B, bit 3 = D, bit 2 = I,
        bit 1 = Z, bit 0 = C).
    """

    pc: int
    a: int
    x: int
    y: int
    sp: int
    p: int

    def __post_init__(self) -> None:
        if not isinstance(self.pc, int) or not 0 <= self.pc <= 0xFFFF:
            raise ValueError(
                f"pc must be a 16-bit int 0..65535, got {self.pc!r}"
            )
        for name in ("a", "x", "y", "sp", "p"):
            v = getattr(self, name)
            if not isinstance(v, int) or not 0 <= v <= 0xFF:
                raise ValueError(
                    f"{name} must be an 8-bit int 0..255, got {v!r}"
                )


# ---------------------------------------------------------------------------
# DriveState — sidecar disk image + drive configuration
# ---------------------------------------------------------------------------


_VALID_DEVICES = (8, 9, 10, 11)
_VALID_DRIVE_TYPES = ("1541", "1571", "1581")
_VALID_IMAGE_FORMATS = ("d64", "d71", "d81", "g64")
_VALID_MODES = ("readwrite", "readonly", "unlinked")

# drive_type → set of legal image_format values.  Cross-check at validation
# time so users can't accidentally pair a 1581 with a .d64.  ``g64`` is a
# raw-GCR variant of the 1541 disk format, so it's allowed for 1541 only.
_DRIVE_TYPE_FORMATS: dict[str, tuple[str, ...]] = {
    "1541": ("d64", "g64"),
    "1571": ("d71",),
    "1581": ("d81",),
}

# U64 has two physical drive slots; firmware convention maps "a" → CBM
# device 8, "b" → device 9.  Devices 10/11 have no U64 home.
_U64_DEVICE_TO_SLOT: dict[int, str] = {8: "a", 9: "b"}


@dataclass(frozen=True)
class DriveState:
    """Mounted-image + drive-config state for one CBM disk drive.

    Attributes
    ----------
    device:
        CBM device number — one of 8, 9, 10, 11.
    drive_type:
        Drive model — ``"1541"``, ``"1571"``, or ``"1581"``.
    image:
        Raw image file bytes (``.d64`` / ``.d71`` / ``.d81`` / ``.g64``).
        May be empty (``b""``) when the snapshot was extracted from a
        backend that cannot read mounted image bytes back (e.g. U64
        without a host-side path); in that case the snapshot must be
        side-loaded with image bytes before it can be restored.
    image_format:
        File-format suffix — ``"d64"``, ``"d71"``, ``"d81"``, ``"g64"``.
        Must be compatible with ``drive_type`` (e.g. 1581 ↔ d81).
    mode:
        Mount mode — ``"readwrite"``, ``"readonly"``, or ``"unlinked"``.
        Default ``"readwrite"``.  Maps to the U64
        ``PUT /v1/drives/<slot>:mount`` ``mode`` field; for VICE,
        ``"readonly"`` translates to ``read_only=True`` on
        :meth:`attach_drive`.
    """

    device: int
    drive_type: str
    image: bytes
    image_format: str
    mode: str = "readwrite"

    def __post_init__(self) -> None:
        if self.device not in _VALID_DEVICES:
            raise ValueError(
                f"device must be one of {_VALID_DEVICES}, got {self.device!r}"
            )
        if self.drive_type not in _VALID_DRIVE_TYPES:
            raise ValueError(
                f"drive_type must be one of {_VALID_DRIVE_TYPES}, "
                f"got {self.drive_type!r}"
            )
        if self.image_format not in _VALID_IMAGE_FORMATS:
            raise ValueError(
                f"image_format must be one of {_VALID_IMAGE_FORMATS}, "
                f"got {self.image_format!r}"
            )
        if self.mode not in _VALID_MODES:
            raise ValueError(
                f"mode must be one of {_VALID_MODES}, got {self.mode!r}"
            )
        allowed = _DRIVE_TYPE_FORMATS[self.drive_type]
        if self.image_format not in allowed:
            raise ValueError(
                f"image_format {self.image_format!r} not compatible with "
                f"drive_type {self.drive_type!r} (expected one of {allowed})"
            )
        if not isinstance(self.image, (bytes, bytearray)):
            raise TypeError(
                f"image must be bytes, not {type(self.image).__name__}"
            )
        if isinstance(self.image, bytearray):
            object.__setattr__(self, "image", bytes(self.image))


# ---------------------------------------------------------------------------
# Snapshot dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Snapshot:
    """Minimum backend-agnostic C64 state — Phase A + B scope.

    Attributes
    ----------
    ram:
        Exactly 65536 bytes of system RAM ($0000-$FFFF as seen with all
        banks disabled).
    cpu_port_data:
        CPU port data register ($01), one byte.
    cpu_port_dir:
        CPU port direction register ($00), one byte.
    exrom, game:
        Cartridge control lines from the C64MEM module.  Default to
        ``1`` (high / not asserted / no cart attached).
    drives:
        Tuple of :class:`DriveState` capturing mounted images.  Empty
        by default — Phase B sidecar.
    cia1_regs, cia2_regs:
        Raw 16-byte register files of CIA1 ($DC00-$DC0F) and CIA2
        ($DD00-$DD0F) at the moment of snapshot.  Empty (``b""``) means
        "not captured" — restore skips the corresponding writes.
    vic_regs:
        Raw 47-byte VIC-II register file ($D000-$D02E).  Empty means
        not captured.
    sid_regs:
        Raw 32-byte SID register file ($D400-$D41F).  Empty means not
        captured.  On real-hardware backends this is sourced from a
        host-side shadow (see :attr:`Ultimate64Transport.sid_shadow`)
        because 28 of the 32 SID registers are write-only on
        6581/8580/UltiSID.
    """

    ram: bytes
    cpu_port_data: int
    cpu_port_dir: int
    exrom: int = 1
    game: int = 1
    drives: tuple[DriveState, ...] = ()
    cia1_regs: bytes = b""
    cia2_regs: bytes = b""
    vic_regs: bytes = b""
    sid_regs: bytes = b""
    # Phase C — RAM Expansion Unit (REU) contents.  ``reu_size_bytes``
    # of 0 means the snapshot carries no REU; otherwise the size must
    # be one of :data:`_REU_SIZE_BYTES` and ``reu_contents`` must hold
    # exactly that many bytes.
    reu_size_bytes: int = 0
    reu_contents: bytes = b""
    cpu_registers: CpuRegisters | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.ram, (bytes, bytearray)):
            raise TypeError(f"ram must be bytes, not {type(self.ram).__name__}")
        if len(self.ram) != 65536:
            raise ValueError(
                f"ram must be exactly 65536 bytes, got {len(self.ram)}"
            )
        for name in ("cpu_port_data", "cpu_port_dir", "exrom", "game"):
            v = getattr(self, name)
            if not isinstance(v, int) or not 0 <= v <= 0xFF:
                raise ValueError(f"{name} must be a byte 0..255, got {v!r}")
        # Coerce ram to immutable bytes if a bytearray slipped in.
        if isinstance(self.ram, bytearray):
            object.__setattr__(self, "ram", bytes(self.ram))
        # Accept list of DriveState too; normalise to a tuple for immutability.
        if not isinstance(self.drives, tuple):
            if not all(isinstance(d, DriveState) for d in self.drives):
                raise TypeError(
                    "drives must be a sequence of DriveState instances"
                )
            object.__setattr__(self, "drives", tuple(self.drives))
        # No two DriveState entries may share the same device number.
        seen: set[int] = set()
        for d in self.drives:
            if not isinstance(d, DriveState):
                raise TypeError(
                    f"drives must contain DriveState, got {type(d).__name__}"
                )
            if d.device in seen:
                raise ValueError(
                    f"duplicate DriveState for device {d.device}"
                )
            seen.add(d.device)
        # cpu_registers is optional; when present it must be a CpuRegisters.
        if self.cpu_registers is not None and not isinstance(
            self.cpu_registers, CpuRegisters
        ):
            raise TypeError(
                "cpu_registers must be a CpuRegisters or None, got "
                f"{type(self.cpu_registers).__name__}"
            )

        # I/O register banks — each is either empty (not captured) or
        # exactly the expected length.  Normalise bytearray → bytes.
        for name, expected in (
            ("cia1_regs", _CIA_REGS_LEN),
            ("cia2_regs", _CIA_REGS_LEN),
            ("vic_regs", _VIC_REGS_LEN),
            ("sid_regs", _SID_REGS_LEN),
        ):
            v = getattr(self, name)
            if not isinstance(v, (bytes, bytearray)):
                raise TypeError(
                    f"{name} must be bytes, not {type(v).__name__}"
                )
            if len(v) not in (0, expected):
                raise ValueError(
                    f"{name} must be empty or exactly {expected} bytes, "
                    f"got {len(v)}"
                )
            if isinstance(v, bytearray):
                object.__setattr__(self, name, bytes(v))
        # ----- REU validation (Phase C) -----
        if not isinstance(self.reu_size_bytes, int) or self.reu_size_bytes < 0:
            raise ValueError(
                f"reu_size_bytes must be a non-negative int, "
                f"got {self.reu_size_bytes!r}"
            )
        if not isinstance(self.reu_contents, (bytes, bytearray)):
            raise TypeError(
                f"reu_contents must be bytes, "
                f"not {type(self.reu_contents).__name__}"
            )
        if isinstance(self.reu_contents, bytearray):
            object.__setattr__(self, "reu_contents", bytes(self.reu_contents))
        if self.reu_size_bytes == 0:
            if len(self.reu_contents) != 0:
                raise ValueError(
                    f"reu_size_bytes=0 but reu_contents has "
                    f"{len(self.reu_contents)} bytes; pass empty bytes when "
                    "no REU is present"
                )
        else:
            if self.reu_size_bytes not in _REU_SIZE_BYTES:
                raise ValueError(
                    f"reu_size_bytes={self.reu_size_bytes} is not one of the "
                    f"REU enum sizes {_REU_SIZE_BYTES}"
                )
            if len(self.reu_contents) != self.reu_size_bytes:
                raise ValueError(
                    f"reu_contents has {len(self.reu_contents)} bytes but "
                    f"reu_size_bytes is {self.reu_size_bytes}"
                )

    # ------------------------------------------------------------------
    # VSF codec
    # ------------------------------------------------------------------

    def to_vsf(self, *, template: bytes | None = None) -> bytes:
        """Serialise this snapshot to ``.vsf`` bytes consumable by VICE.

        The result wraps a bundled reference template (or *template*, if
        provided) whose ``C64MEM`` module is replaced with this
        snapshot's contents.  All other modules in the template are
        preserved verbatim — VICE 3.10 refuses snapshots that don't
        carry the full module set.

        When ``cia1_regs`` / ``cia2_regs`` / ``vic_regs`` / ``sid_regs``
        are non-empty, the corresponding bytes inside the CIA1, CIA2,
        VIC-II, and SID modules are also patched to carry this
        snapshot's I/O register state — the remaining bytes of those
        modules (sequencer state, TOD alarm latches, etc.) stay as
        template values.  See :data:`_REGISTER_MODULE_SLICES` for the
        verified offsets.
        When :attr:`reu_contents` is non-empty, a ``REU1764`` module is
        injected immediately after ``C64MEM`` (the position VICE emits
        when REU is enabled).  The 11-byte ``$DF00–$DF0A`` register
        block in the module preamble is sourced from this snapshot's
        RAM image (those addresses are memory-mapped) when REU appears
        enabled, otherwise a captured idle-state preamble is used.
        When ``cpu_registers`` is set, the MAINCPU module is patched
        in-place: only the register block (A, X, Y, SP, PC, P at body
        offset 8..14) is rewritten; clock counter and last-opcode info
        are preserved from the template so VICE's internal CPU state
        stays self-consistent.
        """
        if template is None:
            template = _load_template()
        out = _replace_c64mem(template, self._build_c64mem_body())
        # Patch each I/O register module in turn.  Skip fields that are
        # empty — those snapshots came from extract_snapshot(
        # include_registers=False) and the template values stand.
        for module_name, offset, length in _REGISTER_MODULE_SLICES:
            payload = self._regs_for_module(module_name)
            if payload:
                out = _patch_module_prefix(out, module_name, offset, payload)
        # Inject the REU1764 module immediately after C64MEM when the
        # snapshot carries REU contents.  See _inject_reu_module for
        # the byte-level placement.
        if self.reu_contents:
            out = _inject_reu_module(
                out,
                reu_contents=self.reu_contents,
                control_regs=self._reu_control_regs(),
            )
        # Patch MAINCPU when cpu_registers is set — only the register
        # block is touched; clock counter and trailing state are preserved.
        if self.cpu_registers is not None:
            out = _patch_maincpu_registers(out, self.cpu_registers)
        return out

    def _regs_for_module(self, module_name: bytes) -> bytes:
        """Look up this snapshot's register bytes for *module_name*."""
        if module_name == b"CIA1":
            return self.cia1_regs
        if module_name == b"CIA2":
            return self.cia2_regs
        if module_name == b"SID":
            return self.sid_regs
        if module_name == b"VIC-II":
            return self.vic_regs
        return b""

    @classmethod
    def from_vsf(cls, data: bytes) -> Snapshot:
        """Parse a ``.vsf`` and extract RAM + CPU port + I/O regs + REU + CPU regs.

        Walks the module list once, picks up ``C64MEM`` (required), the
        four I/O register modules (CIA1 / CIA2 / VIC-II / SID —
        best-effort: missing or shorter modules leave the corresponding
        field empty), an optional ``REU1764`` module, and the
        ``MAINCPU`` register block when present (missing MAINCPU →
        ``cpu_registers=None``).

        If a ``REU1764`` module is present its body is also parsed: the
        size is taken from the preamble's first 24-bit LE field
        (kilobytes), then validated against the byte count of the
        body's REU bytes.  Missing REU module → ``reu_size_bytes=0``.

        Raises :class:`SnapshotFormatError` if the file header is bad
        or the ``C64MEM`` module is missing or too short.
        """
        _validate_file_header(data)
        c64mem_fields: dict | None = None
        regs: dict[bytes, bytes] = {}
        reu_size_bytes = 0
        reu_contents = b""
        cpu_regs: CpuRegisters | None = None
        slice_by_name = {
            name: (off, length) for name, off, length in _REGISTER_MODULE_SLICES
        }
        for name, _vmajor, _vminor, body_start, body_len in _iter_modules(data):
            if name == _C64MEM_MODULE_NAME:
                if body_len < 4 + 65536:
                    raise SnapshotFormatError(
                        f"C64MEM body too short: {body_len} bytes, "
                        f"need >= {4 + 65536}"
                    )
                body = data[body_start : body_start + body_len]
                c64mem_fields = dict(
                    cpu_port_data=body[0],
                    cpu_port_dir=body[1],
                    exrom=body[2],
                    game=body[3],
                    ram=bytes(body[4 : 4 + 65536]),
                )
            elif name in slice_by_name:
                off, length = slice_by_name[name]
                if body_len >= off + length:
                    regs[name] = bytes(
                        data[body_start + off : body_start + off + length]
                    )
            elif name == _REU_MODULE_NAME:
                reu_size_bytes, reu_contents = _parse_reu_module(
                    data[body_start : body_start + body_len]
                )
            elif name == _MAINCPU_MODULE_NAME:
                needed = _MAINCPU_REG_OFFSET + 7
                if body_len >= needed:
                    rb = data[body_start + _MAINCPU_REG_OFFSET :
                              body_start + _MAINCPU_REG_OFFSET + 7]
                    cpu_regs = CpuRegisters(
                        a=rb[0],
                        x=rb[1],
                        y=rb[2],
                        sp=rb[3],
                        pc=rb[4] | (rb[5] << 8),
                        p=rb[6],
                    )
        if c64mem_fields is None:
            raise SnapshotFormatError("no C64MEM module found in snapshot")
        return cls(
            **c64mem_fields,
            cia1_regs=regs.get(b"CIA1", b""),
            cia2_regs=regs.get(b"CIA2", b""),
            vic_regs=regs.get(b"VIC-II", b""),
            sid_regs=regs.get(b"SID", b""),
            reu_size_bytes=reu_size_bytes,
            reu_contents=reu_contents,
            cpu_registers=cpu_regs,
        )

    # ------------------------------------------------------------------
    # Sidecar bundle (directory) codec — Phase B
    # ------------------------------------------------------------------

    def to_bundle(self, directory: str | Path) -> Path:
        """Serialise this snapshot to a directory bundle.

        Writes three kinds of file under *directory*:

        * ``snapshot.vsf`` — the ``.vsf`` bytes from :meth:`to_vsf`.
        * ``drive<N>.<image_format>`` — one file per :class:`DriveState`,
          containing the raw image bytes (skipped when ``image`` is
          empty).
        * ``manifest.json`` — describes each drive entry so the bundle
          can be re-hydrated by :meth:`from_bundle`.

        The format is deliberately plain JSON + raw files (no zip) so a
        human can inspect and modify it.  A future enhancement may add
        an opt-in zip wrapper.

        Returns *directory* as a :class:`Path`.
        """
        out = Path(directory)
        out.mkdir(parents=True, exist_ok=True)
        (out / "snapshot.vsf").write_bytes(self.to_vsf())

        drive_entries: list[dict] = []
        for d in self.drives:
            image_file: str | None = None
            if d.image:
                image_file = f"drive{d.device}.{d.image_format}"
                (out / image_file).write_bytes(d.image)
            drive_entries.append({
                "device": d.device,
                "drive_type": d.drive_type,
                "image_format": d.image_format,
                "mode": d.mode,
                "image_file": image_file,
            })

        # Phase C — write REU sidecar when present.  Empty REU → no
        # file written, manifest field is 0.
        if self.reu_contents:
            (out / "reu.bin").write_bytes(self.reu_contents)

        manifest = {
            "version": 1,
            "cpu_port_data": self.cpu_port_data,
            "cpu_port_dir": self.cpu_port_dir,
            "exrom": self.exrom,
            "game": self.game,
            "drives": drive_entries,
            "reu_size_bytes": self.reu_size_bytes,
        }
        # I/O register banks — serialise as hex for human readability.
        # Each field is short (16-47 bytes) so hex bloat is irrelevant.
        # Omit empty fields entirely (compact manifest for Phase A / B
        # snapshots that did not capture I/O state).
        for key, payload in (
            ("cia1_regs", self.cia1_regs),
            ("cia2_regs", self.cia2_regs),
            ("vic_regs", self.vic_regs),
            ("sid_regs", self.sid_regs),
        ):
            if payload:
                manifest[key] = payload.hex()
        if self.cpu_registers is not None:
            manifest["cpu_registers"] = {
                "pc": self.cpu_registers.pc,
                "a": self.cpu_registers.a,
                "x": self.cpu_registers.x,
                "y": self.cpu_registers.y,
                "sp": self.cpu_registers.sp,
                "p": self.cpu_registers.p,
            }
        (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
        return out

    @classmethod
    def from_bundle(cls, directory: str | Path) -> Snapshot:
        """Inverse of :meth:`to_bundle`.

        Reads ``snapshot.vsf``, ``manifest.json``, and the per-drive
        image files under *directory* and returns a fully-populated
        :class:`Snapshot`.  Raises :class:`SnapshotFormatError` if a
        required file is missing or the manifest is malformed.
        """
        src = Path(directory)
        try:
            vsf_bytes = (src / "snapshot.vsf").read_bytes()
        except FileNotFoundError as exc:
            raise SnapshotFormatError(
                f"bundle at {src} has no snapshot.vsf"
            ) from exc
        try:
            manifest_text = (src / "manifest.json").read_text()
        except FileNotFoundError as exc:
            raise SnapshotFormatError(
                f"bundle at {src} has no manifest.json"
            ) from exc
        try:
            manifest = json.loads(manifest_text)
        except json.JSONDecodeError as exc:
            raise SnapshotFormatError(
                f"manifest.json malformed: {exc}"
            ) from exc

        # RAM + CPU port come from the .vsf (single source of truth).
        base = cls.from_vsf(vsf_bytes)

        drives: list[DriveState] = []
        for entry in manifest.get("drives", ()):
            image_file = entry.get("image_file")
            if image_file:
                image_path = src / image_file
                try:
                    image_bytes = image_path.read_bytes()
                except FileNotFoundError as exc:
                    raise SnapshotFormatError(
                        f"manifest references missing image file: {image_file}"
                    ) from exc
            else:
                image_bytes = b""
            drives.append(DriveState(
                device=entry["device"],
                drive_type=entry["drive_type"],
                image=image_bytes,
                image_format=entry["image_format"],
                mode=entry.get("mode", "readwrite"),
            ))

        # I/O register banks: manifest values win when present (they are
        # the human-editable source); fall back to whatever from_vsf
        # picked up from the embedded .vsf modules.
        def _hex_field(key: str, default: bytes) -> bytes:
            raw = manifest.get(key)
            if raw is None:
                return default
            if not isinstance(raw, str):
                raise SnapshotFormatError(
                    f"manifest {key!r} must be a hex string, got {type(raw).__name__}"
                )
            try:
                return bytes.fromhex(raw)
            except ValueError as exc:
                raise SnapshotFormatError(
                    f"manifest {key!r} is not valid hex: {exc}"
                ) from exc
        # Phase C — REU sidecar.  Prefer ``reu.bin`` (the canonical
        # sidecar) over whatever the .vsf might carry, so callers can
        # edit the bundle in place without re-serialising the .vsf.
        reu_size_bytes = int(manifest.get("reu_size_bytes", 0) or 0)
        reu_contents: bytes = b""
        reu_path = src / "reu.bin"
        if reu_path.is_file():
            reu_contents = reu_path.read_bytes()
            if reu_size_bytes == 0:
                # Manifest didn't declare a size but the file exists — trust
                # the file length and let __post_init__ validate.
                reu_size_bytes = len(reu_contents)
        elif reu_size_bytes > 0:
            # Manifest says REU is present but no sidecar file.  Fall
            # back to whatever bytes the .vsf already carried.
            reu_contents = base.reu_contents
            if not reu_contents:
                raise SnapshotFormatError(
                    f"manifest reu_size_bytes={reu_size_bytes} but no "
                    "reu.bin sidecar and the .vsf carries no REU bytes"
                )
        cpu_registers: CpuRegisters | None = None
        regs_entry = manifest.get("cpu_registers")
        if isinstance(regs_entry, dict):
            try:
                cpu_registers = CpuRegisters(
                    pc=int(regs_entry["pc"]),
                    a=int(regs_entry["a"]),
                    x=int(regs_entry["x"]),
                    y=int(regs_entry["y"]),
                    sp=int(regs_entry["sp"]),
                    p=int(regs_entry["p"]),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise SnapshotFormatError(
                    f"manifest.cpu_registers malformed: {exc}"
                ) from exc
        elif base.cpu_registers is not None:
            # The bundled .vsf carried a MAINCPU register block.  Prefer
            # the explicit manifest entry when present, otherwise inherit
            # from the .vsf so a bundle without an explicit registers
            # entry still round-trips correctly.
            cpu_registers = base.cpu_registers

        return cls(
            ram=base.ram,
            cpu_port_data=base.cpu_port_data,
            cpu_port_dir=base.cpu_port_dir,
            exrom=base.exrom,
            game=base.game,
            drives=tuple(drives),
            cia1_regs=_hex_field("cia1_regs", base.cia1_regs),
            cia2_regs=_hex_field("cia2_regs", base.cia2_regs),
            vic_regs=_hex_field("vic_regs", base.vic_regs),
            sid_regs=_hex_field("sid_regs", base.sid_regs),
            reu_size_bytes=reu_size_bytes,
            reu_contents=reu_contents,
            cpu_registers=cpu_registers,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_c64mem_body(self) -> bytes:
        return (
            bytes([self.cpu_port_data, self.cpu_port_dir, self.exrom, self.game])
            + self.ram
            + b"\x00" * _C64MEM_TRAILER_LEN
        )

    def _reu_control_regs(self) -> bytes:
        """Return the 11-byte $DF00..$DF0A register block for the REU module.

        Pulled from this snapshot's RAM image (those addresses are
        memory-mapped, so the bytes are already there) when the RAM
        carries plausibly-valid REU state.  Otherwise the idle-state
        default captured from a clean VICE 3.10 REU snapshot is used.

        The "looks like real REU regs" heuristic: at least one of the
        11 bytes is neither ``$00`` (zero-initialised host RAM) nor
        ``$FF`` (unmapped bus floating).  A pure all-zero or all-FF
        block is treated as "no REU register state captured" and the
        idle defaults are substituted, which matches what VICE writes
        when REU is enabled but has performed no transfers.
        """
        regs = bytes(self.ram[0xDF00 : 0xDF00 + 11])
        looks_real = any(b not in (0x00, 0xFF) for b in regs)
        if looks_real:
            return regs
        return _REU_IDLE_DF_REGS


# ---------------------------------------------------------------------------
# CPU-register snoop / restore — Phase D 6510 routines
# ---------------------------------------------------------------------------
#
# Both routines live at the harness scratch base $0334 in the cassette-buffer
# region ($0334-$03FB).  The snoop reserves $0350-$0354 for the readback
# buffer (5 bytes: A, X, Y, P, SP); the routine itself uses $0334-$0346
# (19 bytes).  The restorer fits in $0334-$0343 (16 bytes) — no readback
# buffer needed.

_SNOOP_ADDR = 0x0334
_SNOOP_SAVE_ADDR = 0x0350         # A, X, Y, P, SP — 5 bytes
_SNOOP_LENGTH = 19                # the snoop routine itself
_RESTORE_ADDR = 0x0334
_RESTORE_LENGTH = 16


def _build_snoop_routine(save_addr: int = _SNOOP_SAVE_ADDR) -> bytes:
    """Return the 19-byte snoop routine that records A, X, Y, P, SP.

    Layout when ``save_addr == $0350`` (the default) — assembled at
    :data:`_SNOOP_ADDR` (``$0334``):

    ::

        $0334  8D 50 03   STA $0350     ; save A first (before PHP/PLA clobbers it)
        $0337  8E 51 03   STX $0351     ; save X
        $033A  8C 52 03   STY $0352     ; save Y
        $033D  08         PHP           ; push P onto stack (SP -= 1)
        $033E  68         PLA           ; pop P-byte into A    (SP += 1, net SP unchanged)
        $033F  8D 53 03   STA $0353     ; save P
        $0342  BA         TSX           ; X <- SP (the SP we want — see below)
        $0343  8E 54 03   STX $0354     ; save SP
        $0346  60         RTS           ; return to caller

    PHP/PLA SP arithmetic — PHP decrements SP by 1 (after the cycle that
    stores P at $0100+SP), PLA increments SP by 1 (before the cycle that
    reads from $0100+SP).  Net effect on SP is zero.  Therefore the TSX
    at $0342 captures the same SP value the caller had when they entered
    the snoop — exactly what we want in the snapshot.

    Caveat: the caller's A, X, and Y are saved before any other side
    effect; P is captured via PHP, which reflects the flags as of the
    STA/STX/STY (none of those touch the flags except the bus cycle so P
    is unchanged from caller-entry).  Specifically, none of STA, STX,
    STY, PHP, PLA modify N or Z based on the operand — they're all
    flag-neutral except PLP — so the captured P is the caller-entry P.
    """
    lo_a = save_addr & 0xFF
    hi_a = (save_addr >> 8) & 0xFF
    lo_x = (save_addr + 1) & 0xFF
    hi_x = ((save_addr + 1) >> 8) & 0xFF
    lo_y = (save_addr + 2) & 0xFF
    hi_y = ((save_addr + 2) >> 8) & 0xFF
    lo_p = (save_addr + 3) & 0xFF
    hi_p = ((save_addr + 3) >> 8) & 0xFF
    lo_s = (save_addr + 4) & 0xFF
    hi_s = ((save_addr + 4) >> 8) & 0xFF
    return bytes([
        0x8D, lo_a, hi_a,   # STA save+0
        0x8E, lo_x, hi_x,   # STX save+1
        0x8C, lo_y, hi_y,   # STY save+2
        0x08,               # PHP
        0x68,               # PLA
        0x8D, lo_p, hi_p,   # STA save+3
        0xBA,               # TSX
        0x8E, lo_s, hi_s,   # STX save+4
        0x60,               # RTS
    ])


def _build_restore_routine(regs: CpuRegisters) -> bytes:
    """Return the 16-byte restorer that pokes registers and jumps to PC.

    Layout (assembled at :data:`_RESTORE_ADDR`):

    ::

        $0334  A2 SP      LDX #SP_VAL       ; load target SP into X
        $0336  9A         TXS               ; SP <- X
        $0337  A9 P       LDA #P_VAL        ; load target P into A
        $0339  48         PHA               ; push P_VAL onto stack (SP -= 1)
        $033A  A2 X       LDX #X_VAL        ; restore X (overwrites the SP we used)
        $033C  A0 Y       LDY #Y_VAL        ; restore Y
        $033E  A9 A       LDA #A_VAL        ; restore A
        $0340  28         PLP               ; pop P from stack (SP += 1, P_VAL -> P)
        $0341  4C lo hi   JMP PC_VAL        ; jump to target PC

    After the LDA #A_VAL the registers other than P are at their target
    values; PLP then restores P (so any flag changes from the loads are
    overwritten) and PLP increments SP back to the target SP.  JMP doesn't
    touch any register or flag — the dispatched code starts with exactly
    the requested machine state.

    Stack arithmetic: TXS sets SP to SP_VAL; PHA decrements to SP_VAL-1
    (stores P at $0100 + SP_VAL - 1); PLP reads from $0100 + SP_VAL and
    increments SP back to SP_VAL.  Net effect: SP is the requested
    SP_VAL when JMP executes.  The byte at $0100 + SP_VAL - 1 was
    temporarily clobbered, but the target program treats that as below
    the stack pointer (free space) anyway.

    PLP-followed-by-JMP is intentional — there's a NMOS 6502 quirk where
    a pending IRQ is recognised on the cycle following PLP, but for
    snapshot restore that's the same behaviour the saved program would
    have seen at its own next instruction.
    """
    return bytes([
        0xA2, regs.sp,            # LDX #SP_VAL
        0x9A,                     # TXS
        0xA9, regs.p,             # LDA #P_VAL
        0x48,                     # PHA
        0xA2, regs.x,             # LDX #X_VAL
        0xA0, regs.y,             # LDY #Y_VAL
        0xA9, regs.a,             # LDA #A_VAL
        0x28,                     # PLP
        0x4C, regs.pc & 0xFF, (regs.pc >> 8) & 0xFF,  # JMP PC_VAL
    ])


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def extract_snapshot(
    transport: "C64Transport",
    *,
    host_image_paths: "dict[int, str | Path] | None" = None,
    include_registers: bool = True,
    include_reu: bool = False,
    reu_turbo: bool = False,
    include_cpu_registers: bool = True,
    known_pc: int | None = None,
) -> Snapshot:
    """Read RAM + CPU port out of any ``C64Transport``-conforming backend.

    Reads ``$0000-$FFFF`` and the two CPU port registers and packages
    them into a :class:`Snapshot`.  The backend's chunking handles any
    transport-level size limits.

    Parameters
    ----------
    transport:
        Any object implementing the :class:`C64Transport` protocol.
    host_image_paths:
        Optional ``{device: path_to_image_file}`` mapping.  Used for two
        purposes: (1) as a fallback when the backend can't tell us where
        a mounted image lives (true for U64 — REST has no ``:get_image``
        endpoint, and for VICE when ``resource_get`` doesn't expose the
        path) and (2) as an authoritative source the caller knows: if
        they just attached ``foo.d64`` to drive 8 themselves, they pass
        ``{8: "foo.d64"}`` and the snapshot picks up the image bytes
        verbatim from disk.
    include_registers:
        When ``True`` (default), also read the CIA1, CIA2, VIC-II, and
        SID register files into the snapshot.  SID extraction uses the
        host-side ``sid_shadow`` attribute when the transport exposes
        it (Ultimate 64) and falls back to ``read_memory(0xD400, 32)``
        otherwise (VICE — which returns last-written values).  Set to
        ``False`` to emit a Phase A / Phase B-disk-only snapshot with
        the four register fields left empty.
    include_cpu_registers:
        When ``True`` (the default) also capture A, X, Y, SP, PC, P into
        :attr:`Snapshot.cpu_registers`.  On VICE this uses the binary
        monitor's :meth:`read_registers` directly.  On Ultimate 64 this
        sideloads a small snoop routine at ``$0334`` (see
        :func:`_build_snoop_routine`), triggers it, and reads the saved
        register block back out of ``$0350-$0354``.  Set ``False`` to
        skip the snoop (e.g. when the device is in a state where running
        the snoop would interfere with the test).
    known_pc:
        Optional caller-supplied PC.  Ignored on VICE (the monitor knows
        the running PC).  On Ultimate 64 the REST API has no way to
        read back an arbitrary running PC, so the captured PC is set
        either to *known_pc* (preferred — caller passes the address
        their last ``SYS``/JSR landed at) or, when ``None``, to the
        snoop's own entry address :data:`_SNOOP_ADDR`.  See
        :func:`_extract_cpu_registers_u64` for the full discussion.

    The drive-discovery side path is best-effort: on backends that don't
    expose any drive state at all the snapshot comes back with
    ``drives=()`` rather than failing.  See :func:`_extract_drives_vice`
    and :func:`_extract_drives_u64` for the per-backend specifics.

    *include_reu* (default ``False``) opts in to REU capture.  The
    operation is slow and intrusive even with the staging-window
    optimisation, so it stays off by default.  When ``True``, the REU
    contents are read into ``Snapshot.reu_contents``.  If the transport
    reports REU as disabled (via ``get_reu_config`` on the U64), the
    REU is emitted empty.

    *reu_turbo* (default ``False``) — when ``True`` and the backend
    supports it, the U64 CPU is switched to 48 MHz turbo for the
    duration of the extract loop and the original speed restored after.
    No-op on backends without a turbo facility.
    """
    ram = transport.read_memory(0x0000, 65536)
    if len(ram) != 65536:
        raise RuntimeError(
            f"transport.read_memory(0x0000, 65536) returned {len(ram)} bytes"
        )
    drives = _extract_drives(transport, host_image_paths or {})

    # I/O register banks.  When skipped, the four fields stay empty
    # bytes — preserves the Phase A/B-disk snapshot shape.
    cia1_regs = cia2_regs = vic_regs = sid_regs = b""
    if include_registers:
        cia1_regs = bytes(transport.read_memory(0xDC00, _CIA_REGS_LEN))
        cia2_regs = bytes(transport.read_memory(0xDD00, _CIA_REGS_LEN))
        vic_regs = bytes(transport.read_memory(0xD000, _VIC_REGS_LEN))
        # SID extract asymmetry: U64 hardware can't read back the
        # write-only SID registers, so we use the transport's
        # host-side shadow.  VICE returns last-written values from
        # read_memory($D400, 32) directly.
        shadow = getattr(transport, "sid_shadow", None)
        if isinstance(shadow, (bytes, bytearray)) and len(shadow) == _SID_REGS_LEN:
            sid_regs = bytes(shadow)
        else:
            sid_regs = bytes(transport.read_memory(0xD400, _SID_REGS_LEN))

    reu_size_bytes = 0
    reu_contents = b""
    if include_reu:
        reu_size_bytes, reu_contents = _extract_reu(
            transport, reu_turbo=reu_turbo,
        )
    cpu_registers: CpuRegisters | None = None
    if include_cpu_registers:
        cpu_registers = _extract_cpu_registers(
            transport, known_pc=known_pc,
        )
    # CPU port registers are at $00 (direction) and $01 (data) — already
    # included in the RAM read, but we mirror them into the dedicated
    # fields so the Snapshot is self-describing.
    return Snapshot(
        ram=ram,
        cpu_port_data=ram[0x01],
        cpu_port_dir=ram[0x00],
        drives=drives,
        cia1_regs=cia1_regs,
        cia2_regs=cia2_regs,
        vic_regs=vic_regs,
        sid_regs=sid_regs,
        reu_size_bytes=reu_size_bytes,
        reu_contents=reu_contents,
        cpu_registers=cpu_registers,
    )


def restore_snapshot(
    transport: "C64Transport",
    snap: Snapshot,
    *,
    override_memory_policy: bool = True,
) -> None:
    """Write RAM and CPU port back through the transport.

    The 64 KB of writes will collide with most ``MemoryPolicy`` reserved
    regions; by default each underlying ``write_memory`` carries
    ``override="snapshot-restore"`` so the restore proceeds, and a
    single WARNING is logged at the start so the bulk override stays
    visible.  Pass ``override_memory_policy=False`` to force the writes
    through the policy unchanged — useful when you've engineered a
    policy that explicitly permits the snapshot's footprint.
    """
    override: str | None = "snapshot-restore" if override_memory_policy else None
    policy = getattr(transport, "memory_policy", None)
    if override_memory_policy and policy is not None:
        try:
            reserved = getattr(policy, "reserved_regions", ())
            n = len(reserved)
        except Exception:
            n = 0
        if n:
            _log.warning(
                "Snapshot restore bypassing MemoryPolicy reserved regions: %d",
                n,
            )

    # Write the 64 KB image in one call — backend chunks as needed.
    transport.write_memory(0x0000, snap.ram, override=override)
    # Then force the CPU port data/direction to the snapshot values.
    # (Already covered by the RAM write above, but explicit is safer in
    # case the backend treats those addresses specially.)
    transport.write_memory(0x0000, bytes([snap.cpu_port_dir]), override=override)
    transport.write_memory(0x0001, bytes([snap.cpu_port_data]), override=override)

    # I/O register banks.  Each non-empty field becomes a single
    # write_memory call at the canonical base address.  Order: CIA1,
    # CIA2, SID, then VIC-II last.  VIC-II is intentionally last
    # because the VIC sequencer latches several values from neighbouring
    # I/O state (raster IRQ enable interacts with $D019/$D01A, etc.) —
    # writing it after the CIAs are settled produces the most consistent
    # restore.  A caller wanting finer-grained ordering can split the
    # restore by skipping fields and writing them manually.
    if snap.cia1_regs:
        transport.write_memory(0xDC00, snap.cia1_regs, override=override)
    if snap.cia2_regs:
        transport.write_memory(0xDD00, snap.cia2_regs, override=override)
    if snap.sid_regs:
        # SID writes go to the wire on both backends; on U64 the same
        # call also primes the host-side sid_shadow via write_memory's
        # built-in shadow update.
        transport.write_memory(0xD400, snap.sid_regs, override=override)
    if snap.vic_regs:
        transport.write_memory(0xD000, snap.vic_regs, override=override)

    # Attached-drives sidecar.  Best-effort: warn (not raise) when the
    # target backend can't host a requested drive (e.g. devices 10/11
    # on U64, which only has slots a/b).
    if snap.drives:
        _restore_drives(transport, snap.drives)

    # REU sidecar (Phase C).  Routes to the SocketDMA fast path on U64,
    # or to a no-op on VICE since the .vsf restore already loaded the
    # REU module via undump.  Empty REU → nothing to do.
    if snap.reu_contents:
        _restore_reu(transport, snap)
    # CPU registers — must happen LAST.  The U64 path triggers via
    # BASIC SYS, which would otherwise be clobbered by the RAM bulk
    # write above.  On VICE the order doesn't matter for correctness,
    # but matching the U64 ordering keeps the two paths identical.
    if snap.cpu_registers is not None:
        _restore_cpu_registers(transport, snap.cpu_registers)


# ---------------------------------------------------------------------------
# CPU-register extract/restore — per-backend helpers
# ---------------------------------------------------------------------------


def _extract_cpu_registers(
    transport: "C64Transport",
    *,
    known_pc: int | None,
) -> CpuRegisters | None:
    """Dispatch to the right per-backend register collector.

    Backend selection is by duck-typing: a U64 transport exposes a
    ``client`` attribute with ``run_prg`` / ``send_text``; a VICE binary
    transport exposes ``read_registers`` returning the running register
    values.  Returns ``None`` on truly unknown transports rather than
    failing — the snapshot field is optional.
    """
    if hasattr(transport, "client") and hasattr(transport.client, "run_prg"):
        return _extract_cpu_registers_u64(transport, known_pc=known_pc)
    if hasattr(transport, "read_registers"):
        try:
            regs = transport.read_registers()
        except NotImplementedError:
            # U64 transport raises NotImplementedError here; but the
            # branch above should have caught it.  Belt-and-braces.
            return None
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "read_registers() failed on %s: %s — emitting "
                "cpu_registers=None", type(transport).__name__, exc,
            )
            return None
        return _build_cpu_registers_from_dict(regs)
    return None


def _build_cpu_registers_from_dict(
    regs: dict[str, int],
) -> CpuRegisters | None:
    """Coerce a VICE-binary-monitor register dict to :class:`CpuRegisters`.

    The dict is expected to carry keys ``PC``, ``A``, ``X``, ``Y``, ``SP``
    plus one of ``P`` / ``FL`` / ``STATUS`` for the flags register
    (different VICE builds expose it under different names).  Missing
    keys cause this helper to return ``None``.
    """
    norm = {k.upper(): v for k, v in regs.items()}
    flags_key = next(
        (k for k in ("P", "FL", "STATUS", "PS") if k in norm),
        None,
    )
    try:
        return CpuRegisters(
            pc=norm["PC"] & 0xFFFF,
            a=norm["A"] & 0xFF,
            x=norm["X"] & 0xFF,
            y=norm["Y"] & 0xFF,
            sp=norm["SP"] & 0xFF,
            p=(norm[flags_key] & 0xFF) if flags_key is not None else 0x20,
        )
    except KeyError as exc:
        _log.warning(
            "register dict missing key %s — emitting cpu_registers=None "
            "(present keys: %s)", exc, sorted(norm),
        )
        return None


def _extract_cpu_registers_u64(
    transport: "C64Transport",
    *,
    known_pc: int | None,
) -> CpuRegisters | None:
    """Snoop A, X, Y, SP, P via a sideloaded routine and read it back.

    Sequence:

    1. Save the 19 bytes currently at ``$0334-$0346`` and the 5 bytes
       at ``$0350-$0354`` so we can restore the cassette buffer after
       the snoop fires.
    2. Write the snoop routine to ``$0334`` (with
       ``override="snapshot-snoop"`` so the call goes through the
       harness's MemoryPolicy).
    3. Trigger the snoop via ``client.send_text("SYS 820")`` — BASIC's
       JSR pushes a return PC and lands at ``$0334``.  The RTS at the
       tail brings BASIC back to the READY prompt cleanly.
    4. Read ``$0350-$0354`` to recover A, X, Y, P, SP.
    5. Restore the original 19 + 5 bytes.

    PC handling — see :func:`extract_snapshot` for the design discussion.
    """
    client = transport.client  # type: ignore[attr-defined]
    if not hasattr(client, "send_text"):
        _log.warning(
            "U64 client lacks send_text(); skipping CPU-register snoop",
        )
        return None
    # 1. Save what's currently there.
    routine_save = transport.read_memory(_SNOOP_ADDR, _SNOOP_LENGTH)
    regs_buf_save = transport.read_memory(_SNOOP_SAVE_ADDR, 5)
    # 2. Sideload the snoop routine.
    snoop = _build_snoop_routine()
    transport.write_memory(_SNOOP_ADDR, snoop, override="snapshot-snoop")
    # 3. Trigger.  Convert _SNOOP_ADDR to decimal for BASIC.
    try:
        client.send_text(f"SYS {_SNOOP_ADDR}", finish_with_return=True)
    except TypeError:
        # Older send_text signatures don't accept finish_with_return.
        client.send_text(f"SYS {_SNOOP_ADDR}\r")
    # 4. Read back A, X, Y, P, SP.
    rb = transport.read_memory(_SNOOP_SAVE_ADDR, 5)
    if len(rb) != 5:
        raise RuntimeError(
            f"read_memory({_SNOOP_SAVE_ADDR:#04x}, 5) returned {len(rb)} bytes"
        )
    # 5. Restore original bytes (best effort — log if the restore fails).
    try:
        transport.write_memory(
            _SNOOP_ADDR, routine_save, override="snapshot-snoop",
        )
        transport.write_memory(
            _SNOOP_SAVE_ADDR, regs_buf_save, override="snapshot-snoop",
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "post-snoop restore of $%04X..$%04X failed: %s",
            _SNOOP_ADDR, _SNOOP_SAVE_ADDR + 4, exc,
        )
    a, x, y, p, sp = rb[0], rb[1], rb[2], rb[3], rb[4]
    pc = known_pc if known_pc is not None else _SNOOP_ADDR
    return CpuRegisters(pc=pc, a=a, x=x, y=y, sp=sp, p=p)


def _restore_cpu_registers(
    transport: "C64Transport",
    regs: CpuRegisters,
) -> None:
    """Push *regs* into the live machine via the right per-backend path.

    On VICE, calls :meth:`set_registers` directly.  On U64, sideloads a
    16-byte restorer at ``$0334`` and triggers it via BASIC ``SYS``.
    """
    if hasattr(transport, "client") and hasattr(transport.client, "run_prg"):
        _restore_cpu_registers_u64(transport, regs)
        return
    set_regs = getattr(transport, "set_registers", None)
    if set_regs is None:
        _log.warning(
            "transport %r has no set_registers; skipping CPU-register restore",
            type(transport).__name__,
        )
        return
    set_regs({
        "PC": regs.pc,
        "A": regs.a,
        "X": regs.x,
        "Y": regs.y,
        "SP": regs.sp,
        "FL": regs.p,
    })


def _restore_cpu_registers_u64(
    transport: "C64Transport",
    regs: CpuRegisters,
) -> None:
    """Sideload the restorer routine at ``$0334`` and trigger via BASIC.

    See :func:`_build_restore_routine` for the byte layout.
    """
    routine = _build_restore_routine(regs)
    transport.write_memory(
        _RESTORE_ADDR, routine, override="snapshot-restore",
    )
    client = transport.client  # type: ignore[attr-defined]
    if not hasattr(client, "send_text"):
        _log.warning(
            "U64 client lacks send_text(); cannot trigger restorer at $%04X",
            _RESTORE_ADDR,
        )
        return
    try:
        client.send_text(f"SYS {_RESTORE_ADDR}", finish_with_return=True)
    except TypeError:
        client.send_text(f"SYS {_RESTORE_ADDR}\r")


# ---------------------------------------------------------------------------
# Drive-state extract/restore — per-backend helpers
# ---------------------------------------------------------------------------


def _extract_drives(
    transport: "C64Transport",
    host_image_paths: "dict[int, str | Path]",
) -> tuple[DriveState, ...]:
    """Dispatch to the right per-backend drive-state collector.

    Backend selection is by duck-typing (``transport.client`` for U64,
    ``transport.resource_get`` for VICE binary monitor) — keeps this
    file free of hard imports from ``backends/``.
    """
    # U64: has a `client` attribute exposing Ultimate64Client.list_drives.
    if hasattr(transport, "client") and hasattr(transport.client, "list_drives"):
        return _extract_drives_u64(transport, host_image_paths)
    # VICE binary monitor: has resource_get / resource_set.
    if hasattr(transport, "resource_get"):
        return _extract_drives_vice(transport, host_image_paths)
    # Mocks / unknown backends: emit only what the caller explicitly
    # declared via host_image_paths.  Falling silent here is OK — the
    # Phase A tests have no drives, and the new tests exercise the
    # per-backend paths via their own mocks.
    return _extract_drives_from_paths(host_image_paths)


def _extract_drives_vice(
    transport: "C64Transport",
    host_image_paths: "dict[int, str | Path]",
) -> tuple[DriveState, ...]:
    """Collect drive state from a VICE-binary-monitor transport.

    Uses VICE resources ``Drive<N>Type`` to discover which devices have
    a drive attached, and tries ``Drive<N>Image`` / ``DriveImage<N>`` /
    ``FileSystemDevice<N>`` for the image path before falling back to
    *host_image_paths*.  Resource lookup failures are tolerated — VICE
    builds vary, and the host_image_paths fallback covers gaps.
    """
    _resource_get = transport.resource_get  # type: ignore[attr-defined]
    drives: list[DriveState] = []
    # Map VICE drive_type resource int → DriveState drive_type string.
    type_int_map = {1541: "1541", 1570: "1541", 1571: "1571", 1581: "1581"}
    for device in _VALID_DEVICES:
        try:
            dt_int = _resource_get(f"Drive{device}Type")
        except Exception as exc:  # noqa: BLE001 — best-effort probe
            _log.debug("resource_get Drive%dType failed: %s", device, exc)
            continue
        if not isinstance(dt_int, int) or dt_int == 0:
            continue
        drive_type = type_int_map.get(dt_int)
        if drive_type is None:
            _log.warning(
                "VICE Drive%dType=%r is not a recognised CBM drive model; skipping",
                device, dt_int,
            )
            continue

        # Try several common VICE resource names for the mounted image
        # path; tolerate any that fail.  VICE's binary monitor docs are
        # uneven on which resources are exposed, so probe.
        image_path: Path | None = None
        for name in (
            f"Drive{device}Image",
            f"DriveImage{device}",
            f"FileSystemDevice{device}",
        ):
            try:
                val = _resource_get(name)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(val, str) and val:
                p = Path(val)
                if p.is_file():
                    image_path = p
                    break
        # Caller-supplied path wins over resource_get; this is the
        # robust path for tests that just attached a known image.
        if device in host_image_paths:
            image_path = Path(host_image_paths[device])
        if image_path is None:
            _log.warning(
                "VICE drive %d type=%s detected but no image path "
                "available — emitting empty image bytes; caller must "
                "supply host_image_paths to capture the image",
                device, drive_type,
            )
            # Without an image, emit a DriveState with empty bytes; the
            # drive_type is still useful for restore-time configuration.
            # Pick the canonical format for the drive_type.
            fmt = _DRIVE_TYPE_FORMATS[drive_type][0]
            drives.append(DriveState(
                device=device,
                drive_type=drive_type,
                image=b"",
                image_format=fmt,
            ))
            continue
        image_bytes = image_path.read_bytes()
        fmt = _guess_image_format(image_path, drive_type)
        drives.append(DriveState(
            device=device,
            drive_type=drive_type,
            image=image_bytes,
            image_format=fmt,
        ))
    return tuple(drives)


def _extract_drives_u64(
    transport: "C64Transport",
    host_image_paths: "dict[int, str | Path]",
) -> tuple[DriveState, ...]:
    """Collect drive state from an Ultimate 64 transport.

    The U64 REST API has no ``:get_image`` endpoint — image bytes can't
    be read back out of the device.  Slot configuration (drive type,
    enabled, etc.) is read from ``GET /v1/drives``; image bytes come
    from *host_image_paths* when supplied, or are emitted as ``b""``
    so the slot config is at least round-tripped.
    """
    client = transport.client  # type: ignore[attr-defined]
    try:
        listing = client.list_drives()
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "U64 list_drives() failed (%s); emitting drives from "
            "host_image_paths only", exc,
        )
        return _extract_drives_from_paths(host_image_paths)

    slot_to_device = {"a": 8, "b": 9}
    drives: list[DriveState] = []
    for entry in listing.get("drives", ()):
        if not isinstance(entry, dict):
            continue
        for slot, slot_info in entry.items():
            if slot not in slot_to_device:
                continue
            device = slot_to_device[slot]
            if not isinstance(slot_info, dict):
                continue
            if slot_info.get("enabled") is False:
                continue
            # The firmware may surface the drive type via different
            # keys depending on version; check a few.
            raw_type = (
                slot_info.get("bus_id_mode")
                or slot_info.get("drive_type")
                or slot_info.get("type")
                or slot_info.get("mode")
            )
            drive_type: str | None = None
            if isinstance(raw_type, str):
                for candidate in _VALID_DRIVE_TYPES:
                    if candidate in raw_type:
                        drive_type = candidate
                        break
            if drive_type is None:
                # Default to 1541 — the firmware's default and the
                # most common physical pairing.
                drive_type = "1541"

            host_path = host_image_paths.get(device)
            if host_path is not None:
                hp = Path(host_path)
                image_bytes = hp.read_bytes()
                image_format = _guess_image_format(hp, drive_type)
            else:
                _log.warning(
                    "U64 slot %s (device %d) has no readable image — "
                    "REST cannot dump mounted bytes; emitting empty "
                    "DriveState (caller must side-load image at "
                    "restore time)", slot, device,
                )
                image_bytes = b""
                image_format = _DRIVE_TYPE_FORMATS[drive_type][0]
            drives.append(DriveState(
                device=device,
                drive_type=drive_type,
                image=image_bytes,
                image_format=image_format,
            ))
    # Allow the caller to attach drives at 10/11 too even if the U64
    # didn't surface them — useful when authoring portable snapshots.
    for device, host_path in host_image_paths.items():
        if any(d.device == device for d in drives):
            continue
        hp = Path(host_path)
        # Best guess for the drive type from the extension.
        suffix = hp.suffix.lstrip(".").lower()
        for dt, formats in _DRIVE_TYPE_FORMATS.items():
            if suffix in formats:
                drives.append(DriveState(
                    device=device,
                    drive_type=dt,
                    image=hp.read_bytes(),
                    image_format=suffix,
                ))
                break
    return tuple(drives)


def _extract_drives_from_paths(
    host_image_paths: "dict[int, str | Path]",
) -> tuple[DriveState, ...]:
    """Build DriveState entries purely from a caller-supplied dict.

    Used when the backend doesn't expose any drive-listing API.  Drive
    type is inferred from the file extension via :data:`_DRIVE_TYPE_FORMATS`.
    """
    out: list[DriveState] = []
    for device, host_path in host_image_paths.items():
        hp = Path(host_path)
        suffix = hp.suffix.lstrip(".").lower()
        drive_type: str | None = None
        for dt, formats in _DRIVE_TYPE_FORMATS.items():
            if suffix in formats:
                drive_type = dt
                break
        if drive_type is None:
            _log.warning(
                "cannot infer drive_type for %s; skipping device %d", hp, device,
            )
            continue
        out.append(DriveState(
            device=device,
            drive_type=drive_type,
            image=hp.read_bytes(),
            image_format=suffix,
        ))
    return tuple(out)


def _guess_image_format(path: Path, drive_type: str) -> str:
    """Return a valid image_format for ``path`` paired with ``drive_type``.

    Prefers the file extension if it matches; otherwise falls back to
    the drive_type's canonical format (so a 1541 with no extension
    defaults to ``d64``).  Always returns a value valid against
    :data:`_DRIVE_TYPE_FORMATS`.
    """
    suffix = path.suffix.lstrip(".").lower()
    allowed = _DRIVE_TYPE_FORMATS[drive_type]
    if suffix in allowed:
        return suffix
    return allowed[0]


def _restore_drives(
    transport: "C64Transport",
    drives: tuple[DriveState, ...],
) -> None:
    """Dispatch to the per-backend drive-restore implementation."""
    if hasattr(transport, "client") and hasattr(transport.client, "mount_disk"):
        _restore_drives_u64(transport, drives)
    elif hasattr(transport, "attach_drive"):
        _restore_drives_vice(transport, drives)
    else:
        _log.warning(
            "transport %r has no recognised drive-attach API; "
            "skipping drive sidecar restore", type(transport).__name__,
        )


def _restore_drives_vice(
    transport: "C64Transport",
    drives: tuple[DriveState, ...],
) -> None:
    """Attach each DriveState image to the VICE binary-monitor transport.

    Image bytes are written to per-snapshot temp files (one per drive)
    so :meth:`attach_drive` (which takes a path) can consume them.  The
    temp files outlive this call by design — VICE keeps the file
    descriptor open until ``detach`` or process exit.  The caller is
    responsible for cleaning the directory when the test is done; the
    paths are logged at INFO so they're discoverable.
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="c64-snapshot-drives-"))
    _log.info("VICE drive-restore staging directory: %s", tmpdir)
    for d in drives:
        if not d.image:
            _log.warning(
                "DriveState device=%d has empty image — skipping "
                "VICE attach (snapshot must be side-loaded with image bytes)",
                d.device,
            )
            continue
        path = tmpdir / f"drive{d.device}.{d.image_format}"
        path.write_bytes(d.image)
        # First set the drive type so VICE configures the right model
        # before the attach.  Resource value matches drive_type string.
        try:
            transport.resource_set(  # type: ignore[attr-defined]
                f"Drive{d.device}Type", int(d.drive_type),
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "resource_set Drive%dType=%s failed: %s — continuing",
                d.device, d.drive_type, exc,
            )
        transport.attach_drive(  # type: ignore[attr-defined]
            d.device, str(path), read_only=(d.mode == "readonly"),
        )


def _restore_drives_u64(
    transport: "C64Transport",
    drives: tuple[DriveState, ...],
) -> None:
    """Mount each DriveState onto the Ultimate 64 via REST.

    Only devices 8 and 9 can be hosted (mapped to slots ``a`` and
    ``b``).  Devices 10 and 11 are logged at WARNING and skipped —
    this is the asymmetry the maintainer asked us to surface rather
    than fail on.
    """
    client = transport.client  # type: ignore[attr-defined]
    for d in drives:
        slot = _U64_DEVICE_TO_SLOT.get(d.device)
        if slot is None:
            _log.warning(
                "Snapshot drive device=%d cannot be hosted on Ultimate 64 "
                "(only slots a/b → devices 8/9 are physical); skipping",
                d.device,
            )
            continue
        if not d.image:
            _log.warning(
                "DriveState device=%d has empty image — skipping "
                "U64 mount (snapshot must be side-loaded with image bytes)",
                d.device,
            )
            continue
        # Ensure the drive type matches what's mounted.
        try:
            client.drive_set_mode(slot, d.drive_type)
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "U64 drive_set_mode(%s, %s) failed: %s — continuing",
                slot, d.drive_type, exc,
            )
        try:
            client.drive_on(slot)
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "U64 drive_on(%s) failed: %s — continuing", slot, exc,
            )
        client.mount_disk(slot, d.image, d.image_format, d.mode)


# ---------------------------------------------------------------------------
# REU extract/restore — per-backend helpers (Phase C)
# ---------------------------------------------------------------------------


def _extract_reu(
    transport: "C64Transport",
    *,
    reu_turbo: bool,
) -> tuple[int, bytes]:
    """Dispatch to the right REU-extract path for the transport.

    Returns ``(size_bytes, contents)``.  Returns ``(0, b"")`` silently
    when the transport has no REU (e.g. U64 with REU disabled, or a
    backend that doesn't expose any REU surface).
    """
    # U64: REST + SocketDMA workaround.
    if hasattr(transport, "client") and hasattr(transport, "read_memory"):
        return _extract_reu_u64(transport, reu_turbo=reu_turbo)
    # VICE: delegate to dump_snapshot if the transport exposes it; the
    # caller's normal extract path already pulled RAM, so to keep the
    # behaviour symmetric we read $DF00..$DF0A and try resource_get.
    if hasattr(transport, "resource_get"):
        return _extract_reu_vice(transport)
    return 0, b""


def _extract_reu_u64(
    transport: "C64Transport",
    *,
    reu_turbo: bool,
) -> tuple[int, bytes]:
    """Capture REU bytes from an Ultimate 64 transport.

    No native REST readback exists.  The workaround pauses the CPU,
    stashes a 32 KB staging window at ``$0800–$87FF``, then loops over
    each bank: programs the REU registers to copy from REU→C64 into
    the window, ``read_memory``s the window, and appends to the output.
    Always restores the staging window's original bytes via try/finally.
    """
    client = transport.client  # type: ignore[attr-defined]

    # Probe REU state via the helper.  If REU is disabled, return empty.
    try:
        from .backends.ultimate64_helpers import get_reu_config
        from .backends.ultimate64_schema import _REU_SIZE_BYTES as _SCHEMA_SIZES
        enabled, size_str = get_reu_config(client)
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "U64 get_reu_config probe failed (%s); emitting empty REU",
            exc,
        )
        return 0, b""
    if not enabled:
        _log.debug("U64 REU disabled per get_reu_config; emitting empty REU")
        return 0, b""
    if size_str not in _SCHEMA_SIZES:
        _log.warning(
            "U64 REU size %r not recognised in schema; emitting empty REU",
            size_str,
        )
        return 0, b""
    size_bytes = _SCHEMA_SIZES[size_str]

    # Optional 48 MHz turbo for the duration of the extract.
    prior_turbo: tuple[bool, int] | None = None
    if reu_turbo:
        try:
            from .backends.ultimate64_helpers import set_turbo_mhz
            set_turbo_mhz(client, 48)
            prior_turbo = (True, 1)  # restore to native 1 MHz off afterward
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "U64 set_turbo_mhz(48) failed (%s); continuing at native speed",
                exc,
            )

    # Pause the CPU so the C64 can't race with our staging window writes.
    paused = False
    try:
        try:
            client.pause()
            paused = True
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "U64 pause() failed (%s); REU extract may race with running code",
                exc,
            )

        # Stash the staging window so we can restore it after the loop.
        original_window = transport.read_memory(
            _REU_STAGING_ADDR, _REU_STAGING_LEN,
        )

        try:
            collected = bytearray(size_bytes)
            num_banks = size_bytes // _REU_STAGING_LEN
            for bank in range(num_banks):
                reu_offset = bank * _REU_STAGING_LEN
                _program_reu_transfer_to_c64(
                    transport,
                    c64_addr=_REU_STAGING_ADDR,
                    reu_addr=reu_offset,
                    length=_REU_STAGING_LEN,
                )
                window = transport.read_memory(
                    _REU_STAGING_ADDR, _REU_STAGING_LEN,
                )
                if len(window) != _REU_STAGING_LEN:
                    raise RuntimeError(
                        f"REU extract: read_memory returned "
                        f"{len(window)} bytes, expected {_REU_STAGING_LEN}"
                    )
                collected[reu_offset : reu_offset + _REU_STAGING_LEN] = window
            return size_bytes, bytes(collected)
        finally:
            # Always restore the staging window — even on partial failure.
            try:
                transport.write_memory(
                    _REU_STAGING_ADDR, original_window,
                    override="reu-snapshot-staging",
                )
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "REU staging restore failed (%s); $0800-$87FF may be "
                    "corrupted", exc,
                )
    finally:
        if paused:
            try:
                client.resume()
            except Exception as exc:  # noqa: BLE001
                _log.warning("U64 resume() failed after REU extract: %s", exc)
        if prior_turbo is not None:
            try:
                from .backends.ultimate64_helpers import set_turbo_mhz
                set_turbo_mhz(client, None)
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "U64 set_turbo_mhz(None) failed (%s); device may "
                    "still be in turbo", exc,
                )


def _program_reu_transfer_to_c64(
    transport: "C64Transport",
    *,
    c64_addr: int,
    reu_addr: int,
    length: int,
) -> None:
    """Program $DF02..$DF0A and trigger a REU→C64 transfer.

    Sequence (per the 1764/1750 manual):

    * $DF02-03: C64 address (16-bit LE)
    * $DF04-06: REU address (24-bit LE)
    * $DF07-08: length in bytes (16-bit LE; cap at $8000 here)
    * $DF09:    interrupt mask = 0
    * $DF0A:    address control = 0 (both addrs increment)
    * $DF01:    command = 0x91 (REU→C64, execute now, no autoload)
    """
    if length > 0x8000:
        raise ValueError(
            f"REU transfer length {length:#x} exceeds 0x8000 cap"
        )
    setup = bytes([
        c64_addr & 0xFF, (c64_addr >> 8) & 0xFF,         # DF02-03
        reu_addr & 0xFF, (reu_addr >> 8) & 0xFF, (reu_addr >> 16) & 0xFF,  # DF04-06
        length & 0xFF, (length >> 8) & 0xFF,             # DF07-08
        0x00,                                            # DF09 int mask
        0x00,                                            # DF0A addr control
    ])
    transport.write_memory(
        0xDF02, setup, override="reu-snapshot-staging",
    )
    transport.write_memory(
        0xDF01, bytes([_REU_CMD_REU_TO_C64]),
        override="reu-snapshot-staging",
    )


def _extract_reu_vice(
    transport: "C64Transport",
) -> tuple[int, bytes]:
    """Capture REU state from a VICE binary-monitor transport.

    Uses the ``REU`` and ``REUsize`` resources to discover whether REU
    is enabled and how big it is.  When enabled, programs the REU
    registers via the same staging-window dance as the U64 path — VICE
    handles those memory-mapped writes identically to real hardware.
    """
    try:
        enabled = transport.resource_get("REU")  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        _log.debug("VICE resource_get REU failed: %s", exc)
        return 0, b""
    if not enabled:
        return 0, b""
    try:
        size_kb = transport.resource_get("REUsize")  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "VICE REU enabled but REUsize resource failed (%s); "
            "emitting empty REU", exc,
        )
        return 0, b""
    if not isinstance(size_kb, int):
        return 0, b""
    size_bytes = size_kb * 1024
    if size_bytes not in _REU_SIZE_BYTES:
        _log.warning(
            "VICE REUsize=%d KB is not a recognised REU enum size; "
            "emitting empty REU", size_kb,
        )
        return 0, b""

    # Stash + loop + restore — same logic as U64 path, minus the
    # turbo/pause facilities (VICE binary monitor pauses the CPU
    # implicitly when the monitor is stopped, but here we don't
    # depend on it; the writes are atomic at the C64-cycle level).
    original_window = transport.read_memory(
        _REU_STAGING_ADDR, _REU_STAGING_LEN,
    )
    try:
        collected = bytearray(size_bytes)
        num_banks = size_bytes // _REU_STAGING_LEN
        for bank in range(num_banks):
            reu_offset = bank * _REU_STAGING_LEN
            _program_reu_transfer_to_c64(
                transport,
                c64_addr=_REU_STAGING_ADDR,
                reu_addr=reu_offset,
                length=_REU_STAGING_LEN,
            )
            window = transport.read_memory(
                _REU_STAGING_ADDR, _REU_STAGING_LEN,
            )
            if len(window) != _REU_STAGING_LEN:
                raise RuntimeError(
                    f"VICE REU extract: read_memory returned "
                    f"{len(window)} bytes, expected {_REU_STAGING_LEN}"
                )
            collected[reu_offset : reu_offset + _REU_STAGING_LEN] = window
        return size_bytes, bytes(collected)
    finally:
        try:
            transport.write_memory(
                _REU_STAGING_ADDR, original_window,
                override="reu-snapshot-staging",
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "VICE REU staging restore failed (%s); $0800-$87FF may be "
                "corrupted", exc,
            )


def _restore_reu(
    transport: "C64Transport",
    snap: "Snapshot",
) -> None:
    """Push ``snap.reu_contents`` to the right backend.

    On the U64 we prefer the :class:`SocketDMAClient` ``REUWRITE``
    fast path (one TCP socket on port 64; ~3 s for 16 MB).  On VICE
    the .vsf already carries the REU module so the standard
    ``undump_snapshot`` flow handles the bytes — there's nothing to do
    here unless the caller bypassed that path.
    """
    # U64: SocketDMA REUWRITE.
    if hasattr(transport, "client") and hasattr(transport.client, "host"):
        _restore_reu_u64(transport, snap)
        return
    # VICE: undump_snapshot route is canonical; nothing extra to do.
    if hasattr(transport, "undump_snapshot") or hasattr(transport, "resource_get"):
        _log.debug(
            "VICE REU restore is handled by .vsf undump; skipping explicit push",
        )
        return
    _log.warning(
        "transport %r has no recognised REU-restore API; skipping",
        type(transport).__name__,
    )


def _restore_reu_u64(
    transport: "C64Transport",
    snap: "Snapshot",
) -> None:
    """Restore REU contents to the Ultimate 64.

    Configures the cartridge preset to ``REU`` and the size to match
    ``snap.reu_size_bytes`` (so the device's REU map is actually
    enabled), then opens a :class:`SocketDMAClient` and pushes the
    bytes in 64 KB chunks via REUWRITE.
    """
    client = transport.client  # type: ignore[attr-defined]
    try:
        from .backends.ultimate64_helpers import set_reu
        from .backends.ultimate64_schema import reu_size_enum
        # set_reu interprets int as MB, so pass the enum string explicitly.
        size_str = reu_size_enum(snap.reu_size_bytes)
        set_reu(client, enabled=True, size=size_str)
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "U64 set_reu(enabled=True, size=%d) failed (%s); REU restore "
            "may target an unconfigured device", snap.reu_size_bytes, exc,
        )

    try:
        from .backends.u64_socket_dma import SocketDMAClient
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "SocketDMAClient unavailable (%s); skipping REU restore", exc,
        )
        return

    chunk_size = 64 * 1024
    host = getattr(client, "host", None)
    password = getattr(client, "password", None)
    if host is None:
        _log.warning(
            "U64 client has no .host attribute; skipping REU restore",
        )
        return
    with SocketDMAClient(host=host, password=password, port=64) as dma:
        for offset in range(0, len(snap.reu_contents), chunk_size):
            chunk = snap.reu_contents[offset : offset + chunk_size]
            dma.reu_write(offset, chunk)


# ---------------------------------------------------------------------------
# VSF helpers (file-level)
# ---------------------------------------------------------------------------


def _load_template() -> bytes:
    try:
        return _TEMPLATE_PATH.read_bytes()
    except FileNotFoundError as exc:
        raise SnapshotFormatError(
            f"bundled .vsf template not found at {_TEMPLATE_PATH}; "
            "the c64-test-harness install is missing the snapshot fixture"
        ) from exc


def _validate_file_header(data: bytes) -> None:
    if len(data) < _VSF_FILE_HEADER_LEN:
        raise SnapshotFormatError(
            f".vsf shorter than file header: {len(data)} < {_VSF_FILE_HEADER_LEN}"
        )
    if data[:19] != _VSF_MAGIC:
        raise SnapshotFormatError(
            f".vsf magic mismatch: got {data[:19]!r}, want {_VSF_MAGIC!r}"
        )
    major = data[0x13]
    if major != _VSF_FORMAT_MAJOR:
        raise SnapshotFormatError(
            f".vsf format major {major} unsupported; expected {_VSF_FORMAT_MAJOR}"
        )
    # Minor is informational only; accept anything VICE accepts.
    if data[0x25 : 0x25 + len(_VSF_VERSION_TAG)] != _VSF_VERSION_TAG:
        raise SnapshotFormatError(
            ".vsf missing or malformed VICE Version sub-header"
        )


def _iter_modules(data: bytes):
    """Yield ``(name_bytes_stripped, vmajor, vminor, body_start, body_len)``
    for each module in *data*.  Stops cleanly on truncation.
    """
    offset = _VSF_FILE_HEADER_LEN
    n = len(data)
    while offset + _MODULE_HEADER_LEN <= n:
        name_field = data[offset : offset + _MODULE_NAME_LEN]
        name = name_field.rstrip(b"\x00")
        vmajor = data[offset + 16]
        vminor = data[offset + 17]
        mod_size = struct.unpack_from("<I", data, offset + 18)[0]
        if mod_size < _MODULE_HEADER_LEN or offset + mod_size > n:
            break
        body_start = offset + _MODULE_HEADER_LEN
        body_len = mod_size - _MODULE_HEADER_LEN
        yield name, vmajor, vminor, body_start, body_len
        offset += mod_size


def _patch_module_prefix(
    template: bytes,
    module_name: bytes,
    offset: int,
    payload: bytes,
) -> bytes:
    """Patch *payload* into a slice of *module_name*'s body in *template*.

    Walks the .vsf modules, finds the one whose name matches
    *module_name*, and overwrites bytes ``[offset : offset + len(payload)]``
    of that module's body.  Module headers and all other modules are
    preserved verbatim — the file's total length is unchanged.

    Returns the patched ``.vsf`` bytes.  Raises
    :class:`SnapshotFormatError` if the module is missing or its body
    is too short to hold the slice.
    """
    if not payload:
        return template
    for name, _vmaj, _vmin, body_start, body_len in _iter_modules(template):
        if name == module_name:
            if body_len < offset + len(payload):
                raise SnapshotFormatError(
                    f"{module_name!r} body is {body_len} bytes; "
                    f"cannot patch {len(payload)} bytes at offset {offset}"
                )
            patch_start = body_start + offset
            patch_end = patch_start + len(payload)
            return template[:patch_start] + payload + template[patch_end:]
    raise SnapshotFormatError(
        f"template has no {module_name!r} module to patch"
    )
def _parse_reu_module(body: bytes) -> tuple[int, bytes]:
    """Parse a ``REU1764`` module body into ``(size_bytes, contents)``.

    The first 3 bytes of the preamble are the REU size in KB (24-bit
    LE).  Bytes after the 20-byte preamble are the REU contents.
    Raises :class:`SnapshotFormatError` on shape mismatches.
    """
    if len(body) < _REU_PREAMBLE_LEN:
        raise SnapshotFormatError(
            f"REU1764 module body too short: {len(body)} bytes, "
            f"need >= {_REU_PREAMBLE_LEN}"
        )
    size_kb = int.from_bytes(body[0:3], "little")
    size_bytes = size_kb * 1024
    contents = bytes(body[_REU_PREAMBLE_LEN:])
    if len(contents) != size_bytes:
        raise SnapshotFormatError(
            f"REU1764 body length {len(contents)} does not match "
            f"preamble size {size_kb} KB ({size_bytes} bytes)"
        )
    if size_bytes not in _REU_SIZE_BYTES:
        raise SnapshotFormatError(
            f"REU1764 preamble size {size_kb} KB is not a recognised "
            f"REU enum size; expected one of {_REU_SIZE_BYTES}"
        )
    return size_bytes, contents


def _build_reu_module(reu_contents: bytes, control_regs: bytes) -> bytes:
    """Build a complete ``REU1764`` module (header + body) for emission.

    *reu_contents* must be one of the REU enum sizes.  *control_regs*
    is the 11-byte $DF00..$DF0A snapshot; the rest of the preamble is
    filled with the idle-state defaults captured from VICE 3.10.
    """
    size_bytes = len(reu_contents)
    if size_bytes not in _REU_SIZE_BYTES:
        raise ValueError(
            f"reu_contents must be one of REU enum sizes "
            f"{_REU_SIZE_BYTES}, got {size_bytes}"
        )
    if len(control_regs) != 11:
        raise ValueError(
            f"control_regs must be 11 bytes ($DF00..$DF0A), "
            f"got {len(control_regs)}"
        )
    size_kb = size_bytes // 1024
    preamble = (
        size_kb.to_bytes(3, "little")
        + b"\x00"               # reserved byte 3
        + bytes(control_regs)   # bytes 4..14: DF00..DF0A
        + _REU_IDLE_INTERNAL    # bytes 15..19: 5-byte internal state
    )
    assert len(preamble) == _REU_PREAMBLE_LEN
    body = preamble + reu_contents
    mod_size = _MODULE_HEADER_LEN + len(body)
    header = (
        _REU_MODULE_NAME
        + b"\x00" * (_MODULE_NAME_LEN - len(_REU_MODULE_NAME))
        + bytes([_REU_VMAJOR, _REU_VMINOR])
        + struct.pack("<I", mod_size)
    )
    return header + body


def _inject_reu_module(
    vsf_bytes: bytes,
    *,
    reu_contents: bytes,
    control_regs: bytes,
) -> bytes:
    """Insert a ``REU1764`` module into *vsf_bytes* immediately after C64MEM.

    This is the position VICE 3.10 emits it.  If a ``REU1764`` module is
    already present it is replaced in place.
    """
    _validate_file_header(vsf_bytes)
    reu_module = _build_reu_module(reu_contents, control_regs)

    # Walk modules to find C64MEM (insertion anchor) and any existing REU.
    c64mem_end: int | None = None
    existing_reu_span: tuple[int, int] | None = None
    offset = _VSF_FILE_HEADER_LEN
    n = len(vsf_bytes)
    while offset + _MODULE_HEADER_LEN <= n:
        name_field = vsf_bytes[offset : offset + _MODULE_NAME_LEN]
        name = name_field.rstrip(b"\x00")
        mod_size = struct.unpack_from("<I", vsf_bytes, offset + 18)[0]
        if mod_size < _MODULE_HEADER_LEN or offset + mod_size > n:
            break
        if name == _C64MEM_MODULE_NAME:
            c64mem_end = offset + mod_size
        elif name == _REU_MODULE_NAME:
            existing_reu_span = (offset, offset + mod_size)
        offset += mod_size

    if c64mem_end is None:
        raise SnapshotFormatError(
            "cannot inject REU1764: template has no C64MEM module"
        )

    if existing_reu_span is not None:
        start, end = existing_reu_span
        return vsf_bytes[:start] + reu_module + vsf_bytes[end:]
    return vsf_bytes[:c64mem_end] + reu_module + vsf_bytes[c64mem_end:]


def _patch_maincpu_registers(template: bytes, regs: CpuRegisters) -> bytes:
    """Patch the MAINCPU module's register block with *regs*.

    Only the 7-byte register block (A, X, Y, SP, PC_lo, PC_hi, P) at
    body-relative offset :data:`_MAINCPU_REG_OFFSET` is rewritten — clock
    counter, last-opcode info, and trailing CPU state are preserved
    verbatim from the template.
    """
    rb = bytes([
        regs.a,
        regs.x,
        regs.y,
        regs.sp,
        regs.pc & 0xFF,
        (regs.pc >> 8) & 0xFF,
        regs.p,
    ])
    return _patch_module_prefix(
        template, _MAINCPU_MODULE_NAME, _MAINCPU_REG_OFFSET, rb,
    )



def _replace_c64mem(template: bytes, new_body: bytes) -> bytes:
    """Return *template* with its ``C64MEM`` module body replaced.

    Preserves all other modules and the file header verbatim.
    """
    _validate_file_header(template)
    if len(new_body) != _C64MEM_BODY_LEN:
        raise ValueError(
            f"C64MEM body must be {_C64MEM_BODY_LEN} bytes, got {len(new_body)}"
        )
    for name, _vmaj, _vmin, body_start, body_len in _iter_modules(template):
        if name == _C64MEM_MODULE_NAME:
            # Rebuild the module header so VMAJOR/VMINOR/size match the
            # new body (in case the template uses a different VMINOR or
            # body size than what we emit).
            mod_size = _MODULE_HEADER_LEN + len(new_body)
            mod_header = (
                _C64MEM_MODULE_NAME
                + b"\x00" * (_MODULE_NAME_LEN - len(_C64MEM_MODULE_NAME))
                + bytes([_C64MEM_VMAJOR, _C64MEM_VMINOR])
                + struct.pack("<I", mod_size)
            )
            mod_start = body_start - _MODULE_HEADER_LEN
            mod_end = body_start + body_len
            return template[:mod_start] + mod_header + new_body + template[mod_end:]
    raise SnapshotFormatError("template has no C64MEM module to replace")


# ---------------------------------------------------------------------------
# Reserved for future helpers (no public surface).
# ---------------------------------------------------------------------------


def _build_file_header(
    *,
    minor: int = _VSF_FORMAT_MINOR,
    machine: bytes = _VSF_MACHINE_NAME,
    release: tuple[int, int, int, int] = _VSF_RELEASE_DEFAULT,
    svn: int = 0,
) -> bytes:
    """Build a standalone 58-byte VSF file header.

    Currently unused by :meth:`Snapshot.to_vsf` (which wraps a bundled
    template), but kept here as the canonical reference for the byte
    layout — invoked by tests and useful as Phase B/D scaffolding when
    we eventually emit snapshots from scratch.
    """
    if len(machine) > _VSF_MACHINE_FIELD_LEN:
        raise ValueError(f"machine name {machine!r} too long")
    field_machine = machine + b"\x00" * (_VSF_MACHINE_FIELD_LEN - len(machine))
    return (
        _VSF_MAGIC
        + bytes([_VSF_FORMAT_MAJOR, minor])
        + field_machine
        + _VSF_VERSION_TAG
        + bytes(release)
        + struct.pack("<I", svn)
    )
