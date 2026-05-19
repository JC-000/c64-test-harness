"""Cross-backend C64 snapshot — Phase A.

Round-trips just **64 KB RAM + CPU port** between VICE and Ultimate 64,
using VICE's native ``.vsf`` format as the on-disk wire.

Phase A is deliberately seed-only: a restored snapshot loads the RAM
image but does NOT resume at the exact PC/cycle.  CPU register, VIC-II,
SID, CIA, and REU state are out of scope and will follow in later
phases.

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

import logging
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .transport import C64Transport

__all__ = [
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

_TEMPLATE_PATH = Path(__file__).with_name("_vsf_template.vsf")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SnapshotFormatError(ValueError):
    """The .vsf bytes are malformed or not a recognised snapshot."""


# ---------------------------------------------------------------------------
# Snapshot dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Snapshot:
    """Minimum backend-agnostic C64 state — Phase A scope.

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
    """

    ram: bytes
    cpu_port_data: int
    cpu_port_dir: int
    exrom: int = 1
    game: int = 1

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
        """
        if template is None:
            template = _load_template()
        return _replace_c64mem(template, self._build_c64mem_body())

    @classmethod
    def from_vsf(cls, data: bytes) -> Snapshot:
        """Parse a ``.vsf`` and extract RAM + CPU port from ``C64MEM``.

        Other modules are skipped.  Raises :class:`SnapshotFormatError`
        if the file header is bad or the ``C64MEM`` module is missing
        or too short.
        """
        _validate_file_header(data)
        for name, vmajor, vminor, body_start, body_len in _iter_modules(data):
            if name == _C64MEM_MODULE_NAME:
                if body_len < 4 + 65536:
                    raise SnapshotFormatError(
                        f"C64MEM body too short: {body_len} bytes, "
                        f"need >= {4 + 65536}"
                    )
                body = data[body_start : body_start + body_len]
                return cls(
                    cpu_port_data=body[0],
                    cpu_port_dir=body[1],
                    exrom=body[2],
                    game=body[3],
                    ram=bytes(body[4 : 4 + 65536]),
                )
        raise SnapshotFormatError("no C64MEM module found in snapshot")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_c64mem_body(self) -> bytes:
        return (
            bytes([self.cpu_port_data, self.cpu_port_dir, self.exrom, self.game])
            + self.ram
            + b"\x00" * _C64MEM_TRAILER_LEN
        )


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def extract_snapshot(transport: "C64Transport") -> Snapshot:
    """Read RAM + CPU port out of any ``C64Transport``-conforming backend.

    Reads ``$0000-$FFFF`` and the two CPU port registers and packages
    them into a :class:`Snapshot`.  The backend's chunking handles any
    transport-level size limits.
    """
    ram = transport.read_memory(0x0000, 65536)
    if len(ram) != 65536:
        raise RuntimeError(
            f"transport.read_memory(0x0000, 65536) returned {len(ram)} bytes"
        )
    # CPU port registers are at $00 (direction) and $01 (data) — already
    # included in the RAM read, but we mirror them into the dedicated
    # fields so the Snapshot is self-describing.
    return Snapshot(
        ram=ram,
        cpu_port_data=ram[0x01],
        cpu_port_dir=ram[0x00],
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
