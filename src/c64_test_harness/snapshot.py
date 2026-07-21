"""Cross-backend C64 snapshot — Phase A + the Phase-B REU layer.

Round-trips **64 KB RAM + CPU port** between VICE and Ultimate 64, using
VICE's native ``.vsf`` format as the on-disk wire, plus an optional
**REU contents** layer carried in the sidecar bundle (see
:meth:`Snapshot.to_bundle`).

The snapshot remains seed-only: a restored snapshot loads the RAM image
but does NOT resume at the exact PC/cycle.  CPU register, VIC-II, SID,
CIA, drive, and cartridge state are still out of scope and will follow
in later phases (see ``docs/snapshot_interop.md``).

REU layer (Phase B)
-------------------

* **Capture** — :func:`extract_reu_contents` stages REU banks through a
  32 KB window at ``$0800-$87FF``: the window's RAM is stashed, an
  REU→C64 transfer is programmed through the REC registers at
  ``$DF00-$DF0A`` per 32 KB bank, the window is read back, and the
  original RAM is restored.  Works through the plain ``C64Transport``
  read/write surface (no REST readback for REU memory exists on either
  U64 generation — an upstream ``GET /v1/machine:reumem`` feature
  request is pending).
* **Restore** — ``restore_snapshot`` routes REU contents through the
  Ultimate 64 transport's managed SocketDMA client
  (:meth:`Ultimate64Transport.socket_dma_reu_write`, REUWRITE opcode
  ``0xFF07``, ~3 s / 16 MB).  There is **no REST fallback**: if the
  device's "Ultimate DMA Service" is unavailable the restore raises,
  it never silently skips.  VICE-side REU restore is not implemented —
  restoring a snapshot with REU contents onto a transport without the
  SocketDMA path raises :class:`SnapshotRestoreError`.

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
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .transport import C64Transport

__all__ = [
    "Snapshot",
    "SnapshotFormatError",
    "SnapshotRestoreError",
    "extract_reu_contents",
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
# REU layer constants
# ---------------------------------------------------------------------------

#: Staging window for REU extract: 32 KB at $0800-$87FF (see
#: docs/snapshot_interop.md "Memory-safety contracts").
_REU_STAGING_BASE = 0x0800
_REU_STAGING_SIZE = 0x8000

#: Hard ceiling of the REU address space (24-bit → 16 MB).
_REU_MAX_BYTES = 16 * 1024 * 1024

#: REC (REU controller) register file at $DF00.
_REC_COMMAND = 0xDF01     # bit7 execute, bit4 FF00-decode disable, bits0-1 type
_REC_C64_BASE = 0xDF02    # $DF02/03 C64 addr LE, $DF04-06 REU addr LE (24-bit),
                          # $DF07/08 length LE, $DF09 IRQ mask, $DF0A addr ctrl
_REC_CMD_REU_TO_C64 = 0x91  # execute now, FF00 trigger disabled, REU→C64
_REC_CMD_C64_TO_REU = 0x90  # reserved: restore goes via SocketDMA REUWRITE

#: Override reason used for every staging-window / REC write.
_REU_STAGING_OVERRIDE = "reu-snapshot-staging"

# ---------------------------------------------------------------------------
# Sidecar bundle file names
# ---------------------------------------------------------------------------

_BUNDLE_VSF_NAME = "snapshot.vsf"
_BUNDLE_MANIFEST_NAME = "manifest.json"
_BUNDLE_REU_NAME = "reu.bin"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SnapshotFormatError(ValueError):
    """The .vsf bytes are malformed or not a recognised snapshot."""


class SnapshotRestoreError(RuntimeError):
    """A snapshot layer cannot be restored onto this transport.

    Raised instead of silently skipping a layer — e.g. REU contents on a
    transport without the SocketDMA REUWRITE path.
    """


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
    reu_size_bytes, reu_contents:
        Optional REU layer.  ``reu_contents`` is the raw expansion-RAM
        dump (up to 16 MB); ``reu_size_bytes`` is the configured REU
        size and defaults to ``len(reu_contents)`` when contents are
        present.  ``None`` (the default) means "not captured" — the
        snapshot round-trips without the layer, and ``.vsf`` emission
        is unaffected (REU bytes travel in the sidecar bundle, not the
        ``.vsf``).
    """

    ram: bytes
    cpu_port_data: int
    cpu_port_dir: int
    exrom: int = 1
    game: int = 1
    reu_size_bytes: int | None = None
    reu_contents: bytes | None = None

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
        self._validate_reu_fields()

    def _validate_reu_fields(self) -> None:
        contents = self.reu_contents
        if contents is not None:
            if not isinstance(contents, (bytes, bytearray)):
                raise TypeError(
                    f"reu_contents must be bytes, not {type(contents).__name__}"
                )
            if isinstance(contents, bytearray):
                contents = bytes(contents)
                object.__setattr__(self, "reu_contents", contents)
            if not 0 < len(contents) <= _REU_MAX_BYTES:
                raise ValueError(
                    f"reu_contents must be 1..{_REU_MAX_BYTES} bytes, "
                    f"got {len(contents)}"
                )
            if self.reu_size_bytes is None:
                object.__setattr__(self, "reu_size_bytes", len(contents))
        size = self.reu_size_bytes
        if size is not None:
            if not isinstance(size, int) or isinstance(size, bool):
                raise ValueError(
                    f"reu_size_bytes must be an int, got {size!r}"
                )
            if not 0 < size <= _REU_MAX_BYTES:
                raise ValueError(
                    f"reu_size_bytes must be 1..{_REU_MAX_BYTES}, got {size}"
                )
            if contents is not None and len(contents) != size:
                raise ValueError(
                    f"reu_contents is {len(contents)} bytes but "
                    f"reu_size_bytes says {size}"
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
    # Sidecar bundle codec
    # ------------------------------------------------------------------

    def to_bundle(self, path: "Path | str") -> Path:
        """Write this snapshot as a sidecar bundle directory at *path*.

        The bundle carries the ``.vsf`` plus the layers ``.vsf`` can't:

        * ``snapshot.vsf`` — the full in-band state (also valid alone).
        * ``manifest.json`` — which sidecar layers are present.
        * ``reu.bin`` — raw REU dump, written only when
          :attr:`reu_contents` is set (REU bytes are *not* embedded in
          the ``.vsf``).

        The directory is created if missing; existing bundle files are
        overwritten.  Returns the bundle path.
        """
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        (path / _BUNDLE_VSF_NAME).write_bytes(self.to_vsf())
        manifest: dict = {"format": 1, "reu": None}
        if self.reu_contents is not None:
            (path / _BUNDLE_REU_NAME).write_bytes(self.reu_contents)
            manifest["reu"] = {
                "file": _BUNDLE_REU_NAME,
                "size_bytes": self.reu_size_bytes,
            }
        (path / _BUNDLE_MANIFEST_NAME).write_text(
            json.dumps(manifest, indent=2) + "\n"
        )
        return path

    @classmethod
    def from_bundle(cls, path: "Path | str") -> Snapshot:
        """Load a snapshot from a sidecar bundle directory.

        Reads ``snapshot.vsf`` for the in-band layers and ``reu.bin``
        (when present) for the REU layer.  A manifest ``size_bytes``
        that disagrees with the actual ``reu.bin`` length raises
        :class:`SnapshotFormatError`.
        """
        path = Path(path)
        vsf_path = path / _BUNDLE_VSF_NAME
        if not vsf_path.is_file():
            raise SnapshotFormatError(
                f"bundle at {path} has no {_BUNDLE_VSF_NAME}"
            )
        snap = cls.from_vsf(vsf_path.read_bytes())

        manifest: dict = {}
        manifest_path = path / _BUNDLE_MANIFEST_NAME
        if manifest_path.is_file():
            try:
                loaded = json.loads(manifest_path.read_text())
            except ValueError as exc:
                raise SnapshotFormatError(
                    f"bundle manifest at {manifest_path} unreadable: {exc}"
                ) from exc
            if isinstance(loaded, dict):
                manifest = loaded

        reu_path = path / _BUNDLE_REU_NAME
        if reu_path.is_file():
            contents = reu_path.read_bytes()
            reu_meta = manifest.get("reu")
            declared = (
                reu_meta.get("size_bytes")
                if isinstance(reu_meta, dict)
                else None
            )
            if isinstance(declared, int) and declared != len(contents):
                raise SnapshotFormatError(
                    f"{_BUNDLE_REU_NAME} is {len(contents)} bytes but the "
                    f"manifest declares {declared}"
                )
            snap = replace(
                snap, reu_contents=contents, reu_size_bytes=len(contents)
            )
        return snap

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


def extract_snapshot(
    transport: "C64Transport",
    *,
    include_reu: bool = False,
    reu_size_bytes: int | None = None,
    reu_settle: float = 0.05,
) -> Snapshot:
    """Read RAM + CPU port out of any ``C64Transport``-conforming backend.

    Reads ``$0000-$FFFF`` and the two CPU port registers and packages
    them into a :class:`Snapshot`.  The backend's chunking handles any
    transport-level size limits.

    With ``include_reu=True`` the REU contents are additionally captured
    via the 32 KB staging window (see :func:`extract_reu_contents` —
    slow: ~30 s / 16 MB at native speed on U64 hardware).  The REU size
    is taken from *reu_size_bytes*, or auto-detected from the device
    config when the transport exposes an Ultimate 64 ``client``.
    *reu_settle* is the per-bank settle delay forwarded to the staging
    extract.
    """
    ram = transport.read_memory(0x0000, 65536)
    if len(ram) != 65536:
        raise RuntimeError(
            f"transport.read_memory(0x0000, 65536) returned {len(ram)} bytes"
        )
    reu_contents: bytes | None = None
    if include_reu:
        if reu_size_bytes is None:
            reu_size_bytes = _detect_reu_size(transport)
        reu_contents = extract_reu_contents(
            transport, reu_size_bytes, settle=reu_settle
        )
    # CPU port registers are at $00 (direction) and $01 (data) — already
    # included in the RAM read, but we mirror them into the dedicated
    # fields so the Snapshot is self-describing.
    return Snapshot(
        ram=ram,
        cpu_port_data=ram[0x01],
        cpu_port_dir=ram[0x00],
        reu_size_bytes=reu_size_bytes if include_reu else None,
        reu_contents=reu_contents,
    )


def restore_snapshot(
    transport: "C64Transport",
    snap: Snapshot,
    *,
    override_memory_policy: bool = True,
    restore_reu: bool = True,
) -> None:
    """Write RAM and CPU port back through the transport.

    The 64 KB of writes will collide with most ``MemoryPolicy`` reserved
    regions; by default each underlying ``write_memory`` carries
    ``override="snapshot-restore"`` so the restore proceeds, and a
    single WARNING is logged at the start so the bulk override stays
    visible.  Pass ``override_memory_policy=False`` to force the writes
    through the policy unchanged — useful when you've engineered a
    policy that explicitly permits the snapshot's footprint.

    When the snapshot carries REU contents they are restored after the
    RAM image: the REU is enabled at the snapshot's size via the
    generation-aware ``set_reu`` helper (when the transport exposes an
    Ultimate 64 ``client``), then the contents go over the transport's
    managed SocketDMA ``REUWRITE`` path.  There is **no REST fallback**
    for REU memory — an unavailable SocketDMA service raises
    ``Ultimate64Error``, and a transport without the SocketDMA path at
    all (e.g. VICE) raises :class:`SnapshotRestoreError`.  Pass
    ``restore_reu=False`` to skip the REU layer explicitly (never
    skipped silently).
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

    if snap.reu_contents is not None and restore_reu:
        _restore_reu(transport, snap)


# ---------------------------------------------------------------------------
# REU layer — staging-window extract + SocketDMA restore
# ---------------------------------------------------------------------------


def extract_reu_contents(
    transport: "C64Transport",
    size_bytes: int,
    *,
    settle: float = 0.05,
    pause: bool = True,
) -> bytes:
    """Read *size_bytes* of REU expansion memory via the staging window.

    Neither U64 generation exposes a REST endpoint for REU readback (the
    upstream ``GET /v1/machine:reumem`` feature request is pending), so
    the extract stages each bank through C64 RAM:

    1. Best-effort CPU pause (``transport.client.pause()`` when
       available; skipped with ``pause=False`` or on backends without
       one — VICE's binary monitor already holds the machine during
       memory commands).
    2. Stash the 32 KB staging window ``$0800-$87FF``.
    3. Per 32 KB bank: program an REU→C64 transfer through the REC
       registers (``$DF02-$DF0A`` then the command at ``$DF01``), wait
       *settle* seconds for the DMA to land, and read the window back.
    4. Restore the original 32 KB and resume the CPU if it was paused.

    All staging/REC writes carry ``override="reu-snapshot-staging"`` so
    a strict :class:`~c64_test_harness.MemoryPolicy` doesn't block them.
    The REC register file itself is clobbered (its state is not part of
    the snapshot).

    Cost: ~30 s / 16 MB at native speed on U64 hardware (turbo helps);
    effectively instant on VICE.
    """
    if not isinstance(size_bytes, int) or isinstance(size_bytes, bool):
        raise ValueError(f"size_bytes must be an int, got {size_bytes!r}")
    if not 0 < size_bytes <= _REU_MAX_BYTES:
        raise ValueError(
            f"size_bytes must be 1..{_REU_MAX_BYTES}, got {size_bytes}"
        )

    paused = _try_pause(transport) if pause else False
    try:
        saved = transport.read_memory(_REU_STAGING_BASE, _REU_STAGING_SIZE)
        try:
            out = bytearray()
            for reu_offset in range(0, size_bytes, _REU_STAGING_SIZE):
                n = min(_REU_STAGING_SIZE, size_bytes - reu_offset)
                _rec_transfer(
                    transport,
                    reu_offset=reu_offset,
                    length=n,
                    command=_REC_CMD_REU_TO_C64,
                )
                if settle > 0:
                    time.sleep(settle)
                bank = transport.read_memory(_REU_STAGING_BASE, n)
                if len(bank) != n:
                    raise RuntimeError(
                        f"staging window read returned {len(bank)} bytes, "
                        f"expected {n} (REU offset {reu_offset:#x})"
                    )
                out += bank
            return bytes(out)
        finally:
            transport.write_memory(
                _REU_STAGING_BASE, saved, override=_REU_STAGING_OVERRIDE
            )
    finally:
        if paused:
            transport.resume()


def _rec_transfer(
    transport: "C64Transport",
    *,
    reu_offset: int,
    length: int,
    command: int,
) -> None:
    """Program one REC transfer: address/length registers, then command.

    The command byte is written last — with bit 4 set (FF00 decode
    disabled) the transfer executes immediately on the command write.
    """
    regs = bytes(
        [
            _REU_STAGING_BASE & 0xFF,          # $DF02 C64 base low
            (_REU_STAGING_BASE >> 8) & 0xFF,   # $DF03 C64 base high
            reu_offset & 0xFF,                 # $DF04 REU addr low
            (reu_offset >> 8) & 0xFF,          # $DF05 REU addr high
            (reu_offset >> 16) & 0xFF,         # $DF06 REU bank
            length & 0xFF,                     # $DF07 length low
            (length >> 8) & 0xFF,              # $DF08 length high
            0x00,                              # $DF09 IRQ mask: none
            0x00,                              # $DF0A addr control: both inc
        ]
    )
    transport.write_memory(_REC_C64_BASE, regs, override=_REU_STAGING_OVERRIDE)
    transport.write_memory(
        _REC_COMMAND, bytes([command]), override=_REU_STAGING_OVERRIDE
    )


def _try_pause(transport: "C64Transport") -> bool:
    """Pause the CPU when the backend offers a way to; report success.

    The ``C64Transport`` protocol has ``resume()`` but no ``pause()``;
    on the Ultimate 64 the pause lives on the REST client.
    """
    client = getattr(transport, "client", None)
    pause = getattr(client, "pause", None)
    if callable(pause):
        pause()
        return True
    return False


def _detect_reu_size(transport: "C64Transport") -> int:
    """Auto-detect the configured REU size from an Ultimate 64 transport."""
    client = getattr(transport, "client", None)
    if client is None:
        raise ValueError(
            "cannot auto-detect the REU size on this transport (no Ultimate "
            "64 client attached) — pass reu_size_bytes= explicitly"
        )
    from .backends.ultimate64_helpers import get_reu_config
    from .backends.ultimate64_schema import _REU_SIZE_BYTES

    enabled, size_str = get_reu_config(client)
    if not enabled:
        raise ValueError(
            "REU is disabled in the device configuration — enable it (e.g. "
            "set_reu(client, True, size=...)) or pass reu_size_bytes= "
            "explicitly"
        )
    size = _REU_SIZE_BYTES.get(size_str)
    if size is None:
        raise ValueError(
            f"device reports unknown REU Size {size_str!r} — pass "
            "reu_size_bytes= explicitly"
        )
    return size


def _restore_reu(transport: "C64Transport", snap: Snapshot) -> None:
    """Restore ``snap.reu_contents`` via the SocketDMA REUWRITE path."""
    assert snap.reu_contents is not None
    reu_writer = getattr(transport, "socket_dma_reu_write", None)
    if not callable(reu_writer):
        raise SnapshotRestoreError(
            "snapshot carries REU contents but this transport has no "
            "SocketDMA REUWRITE path (Ultimate64Transport."
            "socket_dma_reu_write). REU restore has no REST or VICE-monitor "
            "fallback — restore onto an Ultimate64Transport with the "
            "device's 'Ultimate DMA Service' enabled, or pass "
            "restore_reu=False to skip the REU layer explicitly."
        )
    client = getattr(transport, "client", None)
    if client is not None:
        # Generation-aware enable: the C64U has no "REU" Cartridge preset
        # (its Cartridge value mirrors REU state) — set_reu probes and
        # writes only what the firmware accepts.
        from .backends.ultimate64_helpers import set_reu
        from .backends.ultimate64_schema import reu_size_enum

        set_reu(client, True, size=reu_size_enum(snap.reu_size_bytes))
    reu_writer(0, snap.reu_contents)


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
