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

_TEMPLATE_PATH = Path(__file__).with_name("_vsf_template.vsf")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SnapshotFormatError(ValueError):
    """The .vsf bytes are malformed or not a recognised snapshot."""


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
    drives: tuple[DriveState, ...] = ()

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

        manifest = {
            "version": 1,
            "cpu_port_data": self.cpu_port_data,
            "cpu_port_dir": self.cpu_port_dir,
            "exrom": self.exrom,
            "game": self.game,
            "drives": drive_entries,
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

        return cls(
            ram=base.ram,
            cpu_port_data=base.cpu_port_data,
            cpu_port_dir=base.cpu_port_dir,
            exrom=base.exrom,
            game=base.game,
            drives=tuple(drives),
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


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def extract_snapshot(
    transport: "C64Transport",
    *,
    host_image_paths: "dict[int, str | Path] | None" = None,
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

    The drive-discovery side path is best-effort: on backends that don't
    expose any drive state at all the snapshot comes back with
    ``drives=()`` rather than failing.  See :func:`_extract_drives_vice`
    and :func:`_extract_drives_u64` for the per-backend specifics.
    """
    ram = transport.read_memory(0x0000, 65536)
    if len(ram) != 65536:
        raise RuntimeError(
            f"transport.read_memory(0x0000, 65536) returned {len(ram)} bytes"
        )
    drives = _extract_drives(transport, host_image_paths or {})
    # CPU port registers are at $00 (direction) and $01 (data) — already
    # included in the RAM read, but we mirror them into the dedicated
    # fields so the Snapshot is self-describing.
    return Snapshot(
        ram=ram,
        cpu_port_data=ram[0x01],
        cpu_port_dir=ram[0x00],
        drives=drives,
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

    # Attached-drives sidecar.  Best-effort: warn (not raise) when the
    # target backend can't host a requested drive (e.g. devices 10/11
    # on U64, which only has slots a/b).
    if snap.drives:
        _restore_drives(transport, snap.drives)


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
