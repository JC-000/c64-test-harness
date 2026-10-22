"""Files on an Ultimate's persistent storage, over the firmware's FTP server.

The firmware's FTP server (``software/network/ftpd.cc``) lists every
volume and media slot at its root. Which of them are usable depends on
the media present at the time, so read :func:`storage_volumes` /
:func:`storage_writable_volumes` at run time rather than relying on a
recorded reading; a volume either one lists is one the helpers will try,
not one proven to accept writes. These helpers put test inputs on the
removable volumes (a disk image that a C64-side program loads through
UCI DOS, a rig's trust store) and read, list and delete them.

Rules, all enforced before the path is touched:

* **The caller must hold the device's** ``DeviceLock`` (any lock dir), on
  every firmware grade, for every call, reads included. Storage is device
  state, and the lock is the unit of exclusion (docs/device_locking.md).
  This is checked before any connection.
* **Writes never go to** ``/Temp`` **or** ``/Flash``: ``put``, ``mkdir``,
  ``delete`` and ``rmdir`` refuse them, along with names starting with ``.``,
  which no listing would show. ``/Temp`` is the firmware's RAM disk,
  emptied at power-on and swept by the harness's ``/Temp`` hygiene.
  ``/Flash`` holds the firmware's own configuration and ROMs. Reads may
  target any volume. Checked before any connection. The one exception is
  :func:`storage_put_rom`, which writes a single ROM image of a size the
  firmware loads to ``/Flash/roms/<name>`` and nowhere else in ``/Flash``
  (owner, 2026-10-20). It replaces a firmware stock ROM file only when the
  caller passes ``replace_stock=True``, and it deletes only a new file of
  its own whose upload failed. :func:`storage_delete_rom` deletes a single
  non-stock file in ``/Flash/roms``.
* **Paths are absolute and normalised:** no ``.``/``..``, no empty
  components, no trailing ``/``, no control characters, no backslash, no
  ``%``, no ``*`` or ``?``, on writes no component ending in ``.`` or a
  space, no component over :data:`MAX_COMPONENT_LEN` characters, and at
  least a volume plus a name. ``%`` reaches a format string in 1.1.0's
  ``MLST`` (fixed upstream in #713). The firmware treats ``\\`` as a path
  separator and resolves ``..``, so ``/USB1/..\\Temp`` reaches ``/Temp``.
  ``MLST``/``SIZE``/``DELE`` match each component as a case-insensitive
  pattern (``*``, ``?``), and FatFS strips a trailing ``.`` or space when it
  creates a file, so either spelling would reach a file that the existence
  checks never saw. Checked before any connection.
* **The volume must exist.** Its name is matched case-insensitively
  against the root listing, and the device's own spelling is used. A
  missing volume raises :class:`Ultimate64StorageVolumeError` with the
  volumes that do exist, so a rig can skip cleanly. One example is
  ``/USB1`` on a C64U that has no USB stick, or its ``/SD`` slot with no
  card in it: the root lists that slot, but it cannot be entered.
* **No config writes.** If FTP File Service is off, the call raises
  :class:`Ultimate64StorageError` naming the setting, and leaves it off.
  Turning it on is the caller's decision:
  :func:`~c64_test_harness.backends.ultimate64_helpers.enable_ftp_service`.
* **Files and directories are told apart with** ``MLST``, which stats
  without mounting. ``CWD`` into a mountable image (``.d64``, ``.d81``,
  ``.t64``...) succeeds on the firmware and opens the image, so it is used
  only to check that a volume root can be entered.
* **A failed upload of a new file is deleted** (best effort) so that a
  retry is not refused. Before an overwrite the old bytes are read, and a
  failed overwrite puts them back and verifies them. The error says
  whether that worked.

The lock is keyed the way ``DeviceLock`` keys it: addresses listed together
in ``devices.toml`` fold to one key. Unlike the REST client, these helpers
make no identity probe, so an *unlisted* second address of a device whose
lock another lane holds is not detected (#519). The FTP login reuses
``$U64_TEMP_GC_FTP_USER`` and ``$U64_TEMP_GC_FTP_PASSWORD`` (default
anonymous), the same credentials the ``/Temp`` GC uses.

FTP ``STOR`` writes straight to the target volume (``ftpd.cc``
``cmd_stor``). It is not a REST body, so it creates no ``/Temp``
attachment (source-read at 1.1.0 and 3a1ff9ff). ``/Temp`` hygiene for REST uploads is
separate and automatic; see ``ultimate64_temp_gc``.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from ftplib import FTP, all_errors as _FTP_ALL_ERRORS, error_perm
from pathlib import Path
from typing import Iterator

from .ultimate64_client import Ultimate64Error
from .ultimate64_temp_gc import (
    DEFAULT_FTP_PASSWORD,
    DEFAULT_FTP_PORT,
    DEFAULT_FTP_USER,
    FTP_PASSWORD_ENV,
    FTP_USER_ENV,
    lock_held_for,
)

__all__ = [
    "ROM_DIRECTORY",
    "ROM_SIZES",
    "STOCK_ROM_NAMES",
    "WRITE_REFUSED_VOLUMES",
    "StorageEntry",
    "StoragePutResult",
    "Ultimate64StorageError",
    "Ultimate64StorageNotEmptyError",
    "Ultimate64StorageVolumeError",
    "storage_delete_file",
    "storage_delete_rom",
    "storage_get_file",
    "storage_list_dir",
    "storage_mkdir",
    "storage_put_file",
    "storage_put_rom",
    "storage_rmdir",
    "storage_volumes",
    "storage_writable_volumes",
]

_log = logging.getLogger(__name__)

#: Volumes that put/mkdir/delete/rmdir never touch (compared casefolded).
#: :func:`storage_put_rom` is the one exception, and only for
#: :data:`ROM_DIRECTORY`.
WRITE_REFUSED_VOLUMES = frozenset({"temp", "flash"})

#: The firmware's ROM directory: ``ROMS_DIRECTORY "/flash/roms"`` (c64.h:12,
#: 1.1.0 and master). Every ROM the C64 and the drives load is read from here
#: by name (c64.cc:811/824/829, c1541.cc:920).
ROM_DIRECTORY = "/Flash/roms"

#: The sizes each ROM kind may have, from the firmware's own filters: the
#: ``Kernal ROM``/``Basic ROM`` presets list only 8192-byte files and ``Char
#: ROM`` only 4096-byte ones (c64.cc:1419/1448/1477 at 1.1.0), and the drive
#: ROM presets 16384 or 32768 (c1541.cc:1220). A 16 KiB drive ROM is mirrored
#: to fill 32 KiB; any other size fails with SSRET_INVALID_DRIVE_ROM
#: (c1541.cc:928-933).
ROM_SIZES: dict[str, tuple[int, ...]] = {
    "kernal": (8192,),
    "basic": (8192,),
    "chars": (4096,),
    "drive": (16384, 32768),
}

#: Longest ROM file name per kind: the config item that selects it holds 30
#: characters for the C64 ROMs (c64.cc:84-86) and 32 for the drive ROMs
#: (c1541.cc:50-52). ``ConfigItem::setString`` truncates a longer value and
#: marks it with ``*`` (config.cc:846-853), so a longer name could never be
#: selected.
_ROM_NAME_MAX = {"kernal": 30, "basic": 30, "chars": 30, "drive": 32}

#: ``(offset, bytes)`` each C64 ROM kind starts with: the checks the firmware
#: menu's "Set as ... ROM" makes before it asks "Are you sure?"
#: (filetype_bin.cc:164, :177, :190 at 1.1.0, little-endian words). The drive
#: ROMs have none there.
_ROM_SIGNATURES = {
    "kernal": (0, bytes.fromhex("8556200f")),
    "basic": (4, b"CBMBASIC"),
    "chars": (0, bytes.fromhex("3c666e6e")),
}

#: Files the firmware itself puts in :data:`ROM_DIRECTORY` (compared
#: casefolded). The updaters write the drive ROMs and sound banks
#: (update_u64ii.cc:144-149 for the C64U, update_u64.cc:93-98 for the
#: U64E), the U64E's updater also copies kernal/basic/chars out of flash
#: (update_u64.cc:83-91), and on the C64U dev line these three are
#: the defaults of the ROM config items (c64.cc:84-86 at master cc4af99e). These are
#: what recovery needs, and the C64U cannot be reflashed remotely, so
#: :func:`storage_put_rom` replaces one only with ``replace_stock=True``.
STOCK_ROM_NAMES = frozenset({
    "kernal.bin", "basic.bin", "chars.bin",
    "1541.rom", "1571.rom", "1581.rom",
    "snds1541.bin", "snds1571.bin", "snds1581.bin",
})

#: Socket timeout for the FTP session. An 880 KB image to the C64U took a
#: few seconds over the VPN (written 2026-10-08, before it moved to the bench), so
#: this is generous.
DEFAULT_STORAGE_FTP_TIMEOUT = 60.0

#: OSError subclasses raised on purpose here. ftplib's ``all_errors``
#: includes OSError, so they are re-raised before that clause can wrap them.
_PASS_THROUGH = (FileExistsError, FileNotFoundError, IsADirectoryError, NotADirectoryError)

#: Longest path component accepted. 1.1.0's ``cmd_mlst`` formats the name
#: and size into a 200-byte buffer; a 63-character LFN plus a 10-digit size
#: fills it exactly (#526 review), so components stay well under that.
MAX_COMPONENT_LEN = 60


class Ultimate64StorageError(Ultimate64Error):
    """A storage call was refused (no lock) or failed (FTP unreachable,
    transfer error, written file did not read back as sent)."""


class Ultimate64StorageVolumeError(Ultimate64StorageError):
    """The path's volume is not present on the device.

    ``available`` lists the volume names the device does show.
    """

    def __init__(self, message: str, available: list[str]) -> None:
        super().__init__(message)
        self.available = list(available)


class Ultimate64StorageNotEmptyError(Ultimate64StorageError):
    """:func:`storage_rmdir` refused a directory that still has entries.

    ``entries`` lists their names.
    """

    def __init__(self, message: str, entries: list[str]) -> None:
        super().__init__(message)
        self.entries = list(entries)


@dataclass(frozen=True)
class StorageEntry:
    """One entry of :func:`storage_list_dir`."""

    name: str
    #: ``"dir"`` or ``"file"``. A disk image is a ``"file"``.
    kind: str
    size: int


@dataclass(frozen=True)
class StoragePutResult:
    """What :func:`storage_put_file` wrote."""

    path: str
    size: int
    sha256: str
    #: ``True`` when the file was read back and its hash matched.
    verified: bool
    #: ``True`` when an existing file was overwritten (``overwrite=True``).
    replaced: bool


def _is_rom_path(parts: list[str]) -> bool:
    """Whether *parts* is exactly ``Flash/roms/<name>``, whatever the case."""
    return (
        len(parts) == 3
        and parts[0].casefold() == "flash"
        and parts[1].casefold() == "roms"
    )


_ASCII_FOLD = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def _ascii_fold(name: str) -> str:
    """*name* with A-Z lowered and nothing else changed.

    This is how the firmware's pattern_match, which MLST, SIZE and DELE use,
    compares names: ASCII only (pattern.cc:78 at 1.1.0). ``str.casefold``
    would turn ``'ß'`` into ``"ss"``. FatFS itself folds more: ff_wtoupper
    for long names (ff.c:1917) and CP437 ExCvt for short ones (ff.c:2797).
    That gap is why the ROM helpers refuse non-ASCII names.
    """
    return name.translate(_ASCII_FOLD)


def _check_path(
    path: str, *, write: bool, allow_volume: bool = False, allow_rom_dir: bool = False
) -> list[str]:
    """Validate *path* and return its components. Raises ``ValueError``.

    *allow_rom_dir* exempts exactly ``/Flash/roms/<name>`` from the
    ``/Flash`` write refusal, for :func:`storage_put_rom` only.
    """
    if not isinstance(path, str) or not path:
        raise ValueError("storage path must be a non-empty string")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in path):
        raise ValueError(f"storage path contains a control character: {path!r}")
    if "\\" in path:
        # The firmware's Path::cd treats '\\' as a separator and resolves
        # '..' (path.cc at 1.1.0 and 3a1ff9ff), so '/USB1/..\\Temp' reaches
        # /Temp: measured on the U64E, 2026-10-08 (SIZE and MLST of it).
        raise ValueError(f"storage path must not contain a backslash: {path!r}")
    if "%" in path:
        # 1.1.0's cmd_mlst sprintfs the entry's name and then passes the
        # result to send_msg as its *format*, so a '%' in a name reaches
        # vsprintf with no arguments (ftpd.cc; fixed upstream in #713,
        # which 3a1ff9ff carries and 1.1.0 does not). Source-read; never
        # probed on a device.
        raise ValueError(f"storage path must not contain '%': {path!r}")
    if "*" in path or "?" in path:
        # MLST, SIZE, DELE and CWD resolve every component with
        # pattern_match, which honours '*' and '?' and ignores case
        # (file_system.cc:106 -> pattern.cc:78 at 1.1.0 and master), so
        # "/SD/*" names whichever entry matches first.
        raise ValueError(f"storage path must not contain '*' or '?': {path!r}")
    if not path.startswith("/"):
        raise ValueError(f"storage path must be absolute: {path!r}")
    if path.endswith("/"):
        raise ValueError(f"storage path must not end in '/': {path!r}")
    parts = path[1:].split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"storage path must be normalised (no '', '.', '..'): {path!r}")
    if len(parts) < (1 if allow_volume else 2):
        raise ValueError(f"storage path must name a volume and an entry: {path!r}")
    if any(len(p) > MAX_COMPONENT_LEN for p in parts):
        raise ValueError(
            f"storage path component longer than {MAX_COMPONENT_LEN} characters: {path!r}"
        )
    if write and any(p.startswith(".") for p in parts):
        # FileManager::get_directory (1.1.0 and 3a1ff9ff) leaves '.'-names
        # out of every listing, so the harness must not create entries that
        # storage_list_dir cannot show and storage_rmdir cannot see.
        raise ValueError(f"refusing to write a name starting with '.': {path!r}")
    if write and any(p[-1] in ". " for p in parts):
        # FatFS create_name snips trailing spaces and dots (ff.c:2903 at
        # 1.1.0), so STOR "x.bin." truncates and rewrites x.bin, while MLST
        # of "x.bin." (pattern_match, literal dot) reports it missing: every
        # existence guard would pass.
        raise ValueError(f"refusing to write a name ending in '.' or ' ': {path!r}")
    if (
        write
        and parts[0].casefold() in WRITE_REFUSED_VOLUMES
        and not (allow_rom_dir and _is_rom_path(parts))
    ):
        raise ValueError(
            f"refusing to write {path!r}: /{parts[0]} is not test storage "
            "(/Temp is the firmware RAM disk the harness sweeps; /Flash holds "
            "the firmware's own files; storage_put_rom writes a single file "
            "directly in /Flash/roms and nothing else)"
        )
    return parts


def _list_root(ftp: FTP) -> tuple[list[str], list[str]]:
    """``(usable, empty)`` volume names at the root.

    The root also lists empty media slots: on the C64U (fw 1.1.0,
    2026-10-08) ``/SD`` was listed with no card in it. ``CWD /SD`` and
    ``SIZE /SD`` answered 550, and ``MKD`` under it answered 553. A slot
    whose ``CWD`` fails is reported as empty, not usable.
    """
    names = sorted({n.strip("/").rsplit("/", 1)[-1] for n in ftp.nlst("/") if n.strip("/")})
    usable = [n for n in names if _enterable(ftp, "/" + n)]
    return usable, [n for n in names if n not in usable]


@contextmanager
def _session(host: str, operation: str, port: int, timeout: float) -> Iterator[FTP]:
    """A logged-in binary-mode FTP session, opened only by a lock holder.

    Errors raised inside are mapped: the ``_PASS_THROUGH`` OSErrors and
    our own errors propagate unchanged; any other ftplib/socket error
    becomes :class:`Ultimate64StorageError`.
    """
    if not lock_held_for(host):
        raise Ultimate64StorageError(
            f"refusing {operation} on {host}: this process does not hold the "
            f"device's DeviceLock. Take it first, e.g. "
            f"DeviceLock({host!r}).acquire_or_raise(timeout=...), or run inside "
            "create_manager(backend='u64')."
        )
    ftp = FTP()
    # FatFS is built with FF_CODE_PAGE 437 and FF_LFN_UNICODE 0 (ffconf.h at
    # 1.1.0 and 3a1ff9ff), so names go on the wire as CP437 bytes. ftplib's
    # default strict UTF-8 would raise on a name such as b"\x9abung".
    ftp.encoding = "cp437"
    try:
        ftp.connect(host, port, timeout=timeout)
        ftp.login(
            os.environ.get(FTP_USER_ENV, DEFAULT_FTP_USER),
            os.environ.get(FTP_PASSWORD_ENV, DEFAULT_FTP_PASSWORD),
        )
    except _FTP_ALL_ERRORS as exc:
        try:
            ftp.close()
        except Exception:  # noqa: BLE001 - best effort
            pass
        raise Ultimate64StorageError(
            f"{operation}: cannot reach the FTP server on {host}:{port} "
            f"({type(exc).__name__}: {exc}). If FTP File Service is disabled "
            "(the 1.1.0 default after power-on), enable it with "
            "c64_test_harness.enable_ftp_service(client) while holding this "
            "DeviceLock (it persists until power-on and is never saved to "
            "flash); the storage helpers never write config themselves."
        ) from exc
    try:
        ftp.voidcmd("TYPE I")
        yield ftp
    except _PASS_THROUGH:
        raise
    except (UnicodeError, *_FTP_ALL_ERRORS) as exc:
        raise Ultimate64StorageError(
            f"{operation} on {host} failed: {type(exc).__name__}: {exc}"
        ) from exc
    finally:
        try:
            ftp.quit()
        except Exception:  # noqa: BLE001 - best effort
            ftp.close()


def _resolve_volume(ftp: FTP, host: str, parts: list[str]) -> str:
    """Return the path with the device's spelling of its volume, or raise."""
    available, empty = _list_root(ftp)
    for name in available:
        if name.casefold() == parts[0].casefold():
            return "/" + "/".join([name] + parts[1:])
    state = (
        "is listed but cannot be entered (no medium, or a filesystem the firmware cannot read)"
        if any(n.casefold() == parts[0].casefold() for n in empty)
        else "is not a volume"
    )
    raise Ultimate64StorageVolumeError(
        f"/{parts[0]} {state} on {host}; usable volumes: "
        + (", ".join("/" + n for n in available) or "(none)"),
        available,
    )


def _size(ftp: FTP, path: str) -> int | None:
    """``SIZE`` of *path*, or ``None`` when the server answers 550."""
    try:
        size = ftp.size(path)
    except error_perm as exc:
        if str(exc).startswith("550"):
            return None
        raise
    return int(size) if size is not None else None


def _enterable(ftp: FTP, path: str) -> bool:
    """Whether ``CWD`` into *path* succeeds. Use this only for volume roots.

    On the firmware, ``CWD`` into a mountable image file (.d64, .d81, .t64,
    and so on) succeeds and opens the image: ``vfs_chdir`` ->
    ``find_mount_point``. So it cannot tell a file from a directory; that is
    :func:`_kind`'s job. A volume root is never such a file.
    """
    try:
        ftp.cwd(path)
    except error_perm:
        return False
    ftp.cwd("/")
    return True


def _kind(ftp: FTP, path: str) -> str | None:
    """``"dir"``, ``"file"`` or ``None`` (missing), from ``MLST``.

    ``cmd_mlst`` stats without mounting and answers 501 for a missing path
    (ftpd.cc at 1.1.0 and 3a1ff9ff). On the U64E (2026-10-08) a ``.d64``
    read ``type=file``.
    """
    try:
        reply = ftp.sendcmd(f"MLST {path}")
    except error_perm as exc:
        if str(exc).startswith(("501", "550")):
            return None
        raise
    m = re.search(r"^\s*type=(\w+)", reply, re.MULTILINE)
    if m is None:
        raise Ultimate64StorageError(f"MLST {path}: no type fact in {reply!r}")
    return "dir" if m.group(1) in ("dir", "cdir", "pdir") else "file"


def _walk(ftp: FTP, path: str) -> str | None:
    """Kind of *path*'s leaf, classified top-down (``"dir"``/``"file"``/``None``).

    Each ancestor below the volume is checked with ``MLST`` before
    anything deeper. A missing ancestor means the leaf is missing, and an
    ancestor that is a file raises ``NotADirectoryError``. So nothing is
    ever stat'ed *through* a disk image: ``vfs_stat`` would mount it
    (``find_pathentry`` -> ``find_mount_point``), and on 1.1.0 a later
    ``DELE`` of that image leaves a stale mount (#526 review).
    """
    parts = path[1:].split("/")
    for depth in range(2, len(parts)):
        sub = "/" + "/".join(parts[:depth])
        kind = _kind(ftp, sub)
        if kind is None:
            return None
        if kind == "file":
            raise NotADirectoryError(f"{sub} is a file, not a directory")
    return _kind(ftp, path)


def _make_parents(ftp: FTP, path: str, *, include_self: bool) -> None:
    parts = path[1:].split("/")
    stop = len(parts) + 1 if include_self else len(parts)
    for depth in range(2, stop):
        sub = "/" + "/".join(parts[:depth])
        kind = _kind(ftp, sub)
        if kind == "dir":
            continue
        if kind == "file":
            raise NotADirectoryError(f"{sub} is a file, not a directory")
        ftp.mkd(sub)


def storage_volumes(
    host: str, *, port: int = DEFAULT_FTP_PORT, timeout: float = DEFAULT_STORAGE_FTP_TIMEOUT
) -> list[str]:
    """The usable volume names at the device's FTP root, e.g. ``['Flash', 'Temp', 'USB1']``.

    An empty media slot (listed at the root, but ``CWD`` into it fails) is
    left out. To leave out the volumes the helpers refuse to write, use
    :func:`storage_writable_volumes`.
    """
    with _session(host, "storage_volumes", port, timeout) as ftp:
        return _list_root(ftp)[0]


def storage_writable_volumes(
    host: str, *, port: int = DEFAULT_FTP_PORT, timeout: float = DEFAULT_STORAGE_FTP_TIMEOUT
) -> list[str]:
    """:func:`storage_volumes` without ``/Temp`` and ``/Flash``, whatever their case.

    The device spells them ``Temp`` and ``Flash``, while
    :data:`WRITE_REFUSED_VOLUMES` is casefolded, so filtering with a plain
    ``in`` keeps both. Use this instead.

    **A listed volume is one the helpers will try to write, not one that
    accepts writes.** Nothing is written to find out: a volume here was
    entered with ``CWD`` and is not refused by name. The C64U (1.1.0,
    2026-10-09) listed ``SD`` here with a microSD card inserted, and
    ``storage_mkdir('/SD/amiga64')`` failed with ``553`` until the card was
    unlocked and reformatted MBR + FAT32. 1.1.0's
    ``MKD`` answers ``553`` whenever ``vfs_mkdir`` fails: ``FR_EXIST`` or
    any other failure from the lookup in ``FileManager::create_dir``, or
    any ``FRESULT`` from ``f_mkdir``. The reply does not say which.
    """
    return [
        v for v in storage_volumes(host, port=port, timeout=timeout)
        if v.casefold() not in WRITE_REFUSED_VOLUMES
    ]


def storage_list_dir(
    host: str, path: str, *, port: int = DEFAULT_FTP_PORT,
    timeout: float = DEFAULT_STORAGE_FTP_TIMEOUT,
) -> list[StorageEntry]:
    """The entries of directory *path*, sorted by name. Any volume may be listed.

    *path* may be a volume root such as ``/USB1``. The listing comes from
    ``MLSD``, whose 1.1.0 code passes each name as a ``%s`` argument, so
    names containing ``%`` are safe here (unlike in ``MLST``). An entry
    whose own name contains ``%`` is listed, but the other helpers refuse
    to address it.

    **Hidden entries are omitted.** ``FileManager::get_directory`` skips
    names starting with ``.`` and files with the hidden attribute (1.1.0
    and 3a1ff9ff), so an empty result does not prove the directory is
    empty. A login user of ``dirs``/``into`` (container mode, set by
    ``$U64_TEMP_GC_FTP_USER``) would list disk images as directories; the
    default anonymous login lists them as files.

    :raises FileNotFoundError: no such directory.
    :raises NotADirectoryError: *path*, or an ancestor, is a file. A disk
        image counts as a file and is never opened as a directory.
    """
    parts = _check_path(path, write=False, allow_volume=True)
    with _session(host, f"storage_list_dir({path!r})", port, timeout) as ftp:
        path = _resolve_volume(ftp, host, parts)
        kind = _walk(ftp, path)
        if kind is None:
            raise FileNotFoundError(f"{path} does not exist on {host}")
        if kind == "file":
            raise NotADirectoryError(f"{path} on {host} is a file")
        entries = []
        for name, facts in ftp.mlsd(path):
            if name in (".", "..") or facts.get("type") in ("cdir", "pdir"):
                continue
            entries.append(StorageEntry(
                name=name,
                kind="dir" if facts.get("type") == "dir" else "file",
                size=int(facts.get("size", 0) or 0),
            ))
        return sorted(entries, key=lambda e: e.name)


def storage_get_file(
    host: str, path: str, *, port: int = DEFAULT_FTP_PORT,
    timeout: float = DEFAULT_STORAGE_FTP_TIMEOUT,
) -> bytes:
    """Read the file at *path*. Any volume may be read.

    :raises FileNotFoundError: no such file.
    :raises IsADirectoryError: *path* is a directory.
    :raises NotADirectoryError: an ancestor is a file (such as a disk image).
    """
    parts = _check_path(path, write=False)
    with _session(host, f"storage_get_file({path!r})", port, timeout) as ftp:
        path = _resolve_volume(ftp, host, parts)
        kind = _walk(ftp, path)
        if kind == "dir":
            raise IsADirectoryError(f"{path} on {host} is a directory")
        if kind is None:
            raise FileNotFoundError(f"{path} does not exist on {host}")
        buf = io.BytesIO()
        ftp.retrbinary(f"RETR {path}", buf.write)
        return buf.getvalue()


def storage_put_file(
    host: str,
    path: str,
    data: bytes | bytearray | Path,
    *,
    overwrite: bool = False,
    verify: bool = True,
    port: int = DEFAULT_FTP_PORT,
    timeout: float = DEFAULT_STORAGE_FTP_TIMEOUT,
) -> StoragePutResult:
    """Write *data* to *path*, creating missing parent directories.

    *data* is ``bytes``, or a ``Path`` to a local file. A ``str`` is
    refused, because it could be either. An existing file is refused with
    ``FileExistsError`` unless *overwrite* is true, and a directory at
    *path* is always refused (``IsADirectoryError``), and so is an
    ancestor that is a file (``NotADirectoryError``).

    After the ``STOR`` the server's ``SIZE`` must equal ``len(data)``.
    With *verify* (the default) the file is also read back and its
    SHA-256 compared. Either mismatch, or a failed transfer, raises
    :class:`Ultimate64StorageError`. A new file is then deleted. An
    overwritten one gets its previous bytes back, which are read before the
    ``STOR`` and verified after the restore. The error says which happened.

    **Residuals of the existence check.** The *overwrite* refusal relies on
    ``MLST``, which matches names with ASCII-only case folding (pattern.cc:78
    at 1.1.0), while the ``STOR`` reaches FatFS's ``f_open``. Two spellings
    get past it and rewrite an existing file. One is an 8.3 short-name alias
    of a long name (``LONGNA~1.D64``). The other is a non-ASCII name whose
    case differs from the file on the device: FatFS upcases long names with
    ff_wtoupper (ff.c:1917) and short names through CP437 (ff.c:2797), so
    ``übung.d64`` reaches ``Übung.d64``. Non-ASCII is refused only for ROM
    names. Here it stays allowed, because the session speaks CP437 on
    purpose and existing callers may use such names. Spell a name exactly as
    the device lists it.
    """
    parts = _check_path(path, write=True)
    payload = _payload(data)
    with _session(host, f"storage_put_file({path!r})", port, timeout) as ftp:
        path = _resolve_volume(ftp, host, parts)
        result = _store(
            ftp, host, path, payload, _walk(ftp, path),
            overwrite=overwrite, verify=verify, operation=f"storage_put_file({path!r})",
        )
    return result


def _check_rom_name(name: str) -> None:
    """Refuse a ROM name that is not a str or not ASCII, before any connection.

    MLST finds an existing entry with pattern_match, which folds ASCII only
    (pattern.cc:78 at 1.1.0), but f_open compares long names with
    ff_wtoupper (ff.c:1917) and short names through the CP437 table
    (ff.c:2797, :2942). So with ``JIFFY\u00c4.bin`` on the device,
    ``jiffy\u00e4.bin`` stats as missing and STOR then rewrites the
    existing file. ASCII names fold the same way on both paths.
    """
    if not isinstance(name, str):
        raise TypeError(f"ROM name must be a str, not {type(name).__name__}")
    if not name.isascii():
        raise ValueError(
            f"ROM name must be ASCII: {name!r} (the firmware's MLST and FatFS "
            "fold non-ASCII case differently, so existence checks would miss)"
        )


def _check_rom(kind: str, name: str, payload: bytes, *, check_signature: bool) -> None:
    """Refuse a ROM the firmware would not list or load. Raises ``ValueError``."""
    if kind not in ROM_SIZES:
        raise ValueError(f"unknown ROM kind {kind!r}; expected one of {sorted(ROM_SIZES)}")
    _check_rom_name(name)
    if len(name) > _ROM_NAME_MAX[kind]:
        raise ValueError(
            f"ROM name {name!r} is {len(name)} characters; the {kind} ROM config "
            f"item holds {_ROM_NAME_MAX[kind]}, so it could never be selected"
        )
    if len(payload) not in ROM_SIZES[kind]:
        raise ValueError(
            f"a {kind} ROM must be "
            + " or ".join(str(n) for n in ROM_SIZES[kind])
            + f" bytes, not {len(payload)}: the firmware neither lists nor loads it"
        )
    if check_signature and kind in _ROM_SIGNATURES:
        offset, magic = _ROM_SIGNATURES[kind]
        if payload[offset:offset + len(magic)] != magic:
            raise ValueError(
                f"this does not start like a {kind} ROM (bytes {offset}-"
                f"{offset + len(magic) - 1} are {payload[offset:offset + len(magic)].hex()}, "
                f"expected {magic.hex()}, the check the firmware menu makes); pass "
                "check_signature=False if it is one"
            )


def storage_put_rom(
    host: str,
    kind: str,
    name: str,
    data: bytes | bytearray | Path,
    *,
    overwrite: bool = False,
    replace_stock: bool = False,
    check_signature: bool = True,
    port: int = DEFAULT_FTP_PORT,
    timeout: float = DEFAULT_STORAGE_FTP_TIMEOUT,
) -> StoragePutResult:
    """Write a ROM image to :data:`ROM_DIRECTORY` as *name*, and read it back.

    The one write the helpers make under ``/Flash``. *kind* is
    ``"kernal"``, ``"basic"``, ``"chars"`` or ``"drive"`` (a 1541, 1571 or
    1581 ROM), and sets the sizes allowed (:data:`ROM_SIZES`) and the
    longest name.

    **Selection.** The firmware loads the file that the ``C64 and Cartridge
    Settings`` items ``Kernal ROM``, ``Basic ROM`` and ``Char ROM``, or the
    ``Drive A Settings`` items ``ROM for 1541 mode`` / ``1571`` / ``1581``,
    name. A new name is not selected until one of those items names it. A
    stock name can already be selected, because the item's default names
    it: the drive items default to ``1541.rom``/``1571.rom``/``1581.rom``
    (c1541.cc:50-52 at 1.1.0), and on the C64U dev line the C64 items
    default to ``kernal.bin``/``basic.bin``/``chars.bin`` (c64.cc:84-86 at
    master cc4af99e). So writing a missing stock file can select it. The
    C64 ROMs are loaded again on ``client.reboot()`` (c64.cc:915), not on
    ``machine:reset``.

    **Fallbacks.** A missing ROM file makes the firmware fall back to its
    built-in kernal and character ROM, but on the C64U dev line not to a
    built-in BASIC (c64.cc:1153 at master cc4af99e). A drive ROM has no
    built-in fallback: a missing or wrong-sized one fails ``set_drive_type``
    (c1541.cc:920-933) and the drive is powered off (c1541.cc:200-202).
    Drive images also get no signature check.

    Refused before any connection: an unknown *kind*, a size that kind cannot
    have, a C64 ROM that does not start the way the firmware menu expects
    (unless *check_signature* is false), and a name that is not one path
    component, breaks the storage name rules (``*``, ``?`` and a trailing
    ``.`` or space included), or is too long for its config item.

    *name* must be ASCII. MLST matches names ASCII-casefolded, but FatFS
    upcases non-ASCII too (ff.c:1917, :2797), so for a non-ASCII name the
    two disagree about whether a file exists. On the device, the name is
    matched ASCII-casefolded against the ``MLSD`` listing, which is how
    MLST matches, and an existing file keeps its spelling. An existing file needs *overwrite*.
    One of :data:`STOCK_ROM_NAMES` also needs *replace_stock*, because
    those are what recovery needs. **Residual:** an 8.3 short-name alias of
    a long non-stock name (``JIFFYD~1.BIN``) is not in the listing, so the
    *overwrite* guard does not see the file it reaches. The stock names are
    already 8.3 and have no other alias. Before an overwrite the old bytes
    are read. If the write or its read-back fails, they are put back and
    verified, and the error says whether that worked. The new file is
    always read back and its SHA-256 compared. A new file that fails is
    deleted.

    **Accumulation.** ``/Flash`` is the same flash FAT that holds the saved
    configuration (``CFG_FILEPATH "/flash/config"``, config.cc:34, written at
    :374-375), so ROMs that pile up there take its space. Remove what you
    upload with :func:`storage_delete_rom`.

    **No /Temp cost, by source:** ``STOR`` opens the target itself
    (ftpd.cc:538 -> ``vfs_open`` vfs.cc:32 -> ``f_open``, at 1.1.0). It is not
    a REST body, so no attachment is created.

    :raises FileExistsError: an existing file without *overwrite*, or a stock
        file without *replace_stock*.
    :raises IsADirectoryError: *name* is a directory.
    :raises NotADirectoryError: ``/Flash/roms`` is a file.
    """
    payload = _payload(data)
    _check_rom(kind, name, payload, check_signature=check_signature)
    parts = _check_path(f"{ROM_DIRECTORY}/{name}", write=True, allow_rom_dir=True)
    with _session(host, f"storage_put_rom({kind!r}, {name!r})", port, timeout) as ftp:
        path = _resolve_volume(ftp, host, parts)
        rom_dir = path.rsplit("/", 1)[0]
        # A file at /Flash/roms leaves this alone; _walk below raises
        # NotADirectoryError for it.
        if _walk(ftp, rom_dir) == "dir":
            for entry, _facts in ftp.mlsd(rom_dir):
                if _ascii_fold(entry) == _ascii_fold(name):
                    path = f"{rom_dir}/{entry}"
                    break
        kind_now = _walk(ftp, path)
        if (
            kind_now == "file"
            and _ascii_fold(path.rsplit("/", 1)[1]) in STOCK_ROM_NAMES
            and not replace_stock
        ):
            raise FileExistsError(
                f"{path} on {host} is a firmware stock ROM file, which recovery "
                "needs; pass overwrite=True, replace_stock=True to replace it"
            )
        result = _store(
            ftp, host, path, payload, kind_now, overwrite=overwrite, verify=True,
            operation=f"storage_put_rom({kind!r}, {path!r})",
        )
    return result


def storage_delete_rom(
    host: str, name: str, *, missing_ok: bool = False, port: int = DEFAULT_FTP_PORT,
    timeout: float = DEFAULT_STORAGE_FTP_TIMEOUT,
) -> bool:
    """Delete the ROM image *name* from :data:`ROM_DIRECTORY`. Stock files are refused.

    The one delete the helpers make under ``/Flash``, so that what
    :func:`storage_put_rom` uploads can be removed again. *name* follows the
    same rules as there, and one of :data:`STOCK_ROM_NAMES` raises
    ``ValueError`` before any connection, whatever its case. On the device
    *name* must be ASCII and must equal, ASCII-casefolded, an entry the
    ``MLSD`` listing of ``/Flash/roms`` shows. That entry is deleted by its own spelling, so
    ``DELE`` never matches by pattern, and an entry the listing does not
    show (an 8.3 alias such as ``JIFFYD~1.BIN``) is never reached.

    Returns ``True`` if a file was deleted, or ``False`` if it was absent and
    *missing_ok* is true. If a config item still names the file, the
    firmware falls back as :func:`storage_put_rom` describes.

    :raises FileNotFoundError: absent and *missing_ok* is false.
    :raises IsADirectoryError: *name* is a directory.
    :raises Ultimate64StorageError: the listing still shows the file after
        ``DELE``.
    """
    _check_rom_name(name)
    if _ascii_fold(name) in STOCK_ROM_NAMES:
        raise ValueError(
            f"refusing to delete {name!r}: it is a firmware stock ROM file, which recovery needs"
        )
    parts = _check_path(f"{ROM_DIRECTORY}/{name}", write=True, allow_rom_dir=True)
    with _session(host, f"storage_delete_rom({name!r})", port, timeout) as ftp:
        path = _resolve_volume(ftp, host, parts)
        rom_dir = path.rsplit("/", 1)[0]
        found = None
        if _walk(ftp, rom_dir) == "dir":
            for entry, facts in ftp.mlsd(rom_dir):
                if _ascii_fold(entry) == _ascii_fold(name):
                    found = (entry, facts.get("type"))
                    break
        if found is None:
            if missing_ok:
                return False
            raise FileNotFoundError(f"{rom_dir}/{name} does not exist on {host}")
        entry, entry_type = found
        path = f"{rom_dir}/{entry}"
        if entry_type in ("dir", "cdir", "pdir"):
            raise IsADirectoryError(f"{path} on {host} is a directory")
        ftp.delete(path)
        left = [e for e, _f in ftp.mlsd(rom_dir) if _ascii_fold(e) == _ascii_fold(entry)]
        if left:
            raise Ultimate64StorageError(
                f"storage_delete_rom: DELE {path} on {host} was answered, but "
                f"{rom_dir} still lists {left[0]!r}"
            )
    _log.info("storage_delete_rom: deleted %s on %s", path, host)
    return True


def _payload(data: bytes | bytearray | Path) -> bytes:
    """The bytes to upload. Raises ``TypeError``/``ValueError`` before any connection."""
    if isinstance(data, Path):
        payload = data.read_bytes()
    elif isinstance(data, (bytes, bytearray)):
        payload = bytes(data)
    else:
        raise TypeError("data must be bytes or a pathlib.Path to a local file")
    if not payload:
        # 3a1ff9ff's receivefile opens the file only on the first data block,
        # so a zero-byte STOR creates nothing (measured on the U64E, 2026-10-08).
        raise ValueError("refusing an empty payload: the U64E firmware creates no file for it")
    return payload


def _store(
    ftp: FTP, host: str, path: str, payload: bytes, kind: str | None, *,
    overwrite: bool, verify: bool, operation: str,
) -> StoragePutResult:
    """``STOR`` *payload* at the resolved *path*, whose current kind is *kind*."""
    digest = hashlib.sha256(payload).hexdigest()
    if kind == "dir":
        raise IsADirectoryError(f"{path} on {host} is a directory")
    existing = _size(ftp, path) if kind == "file" else None
    if kind == "file" and not overwrite:
        raise FileExistsError(
            f"{path} already exists on {host} ({existing} B); pass overwrite=True"
        )
    original = None
    if kind == "file":
        # "wb" opens with FA_CREATE_ALWAYS (vfs.cc:32 at 1.1.0), which
        # truncates before the first byte arrives, so keep the old bytes to
        # put back if the write fails.
        buf = io.BytesIO()
        ftp.retrbinary(f"RETR {path}", buf.write)
        original = buf.getvalue()
    _make_parents(ftp, path, include_self=False)
    try:
        ftp.storbinary(f"STOR {path}", io.BytesIO(payload))
        written = _size(ftp, path)
        if written != len(payload):
            raise Ultimate64StorageError(
                f"{path} on {host}: server reports size {written}, sent {len(payload)}"
            )
        verified = False
        if verify:
            buf = io.BytesIO()
            ftp.retrbinary(f"RETR {path}", buf.write)
            back = hashlib.sha256(buf.getvalue()).hexdigest()
            if back != digest:
                raise Ultimate64StorageError(
                    f"{path} on {host}: read back sha256 {back[:16]}..., "
                    f"sent {digest[:16]}..."
                )
            verified = True
    except (Ultimate64StorageError, *_FTP_ALL_ERRORS) as exc:
        # The firmware opens the target before (1.1.0) or on (3a1ff9ff)
        # the first data block and answers 226 even after a dropped
        # transfer, so a failure can leave a truncated file behind.
        if kind is None:
            try:
                ftp.delete(path)
                fate = "the partial file was removed"
            except _FTP_ALL_ERRORS as del_exc:
                fate = f"removing the partial file also failed ({del_exc}); retry with overwrite=True"
        else:
            fate = _restore(ftp, host, path, original)
        raise Ultimate64StorageError(
            f"{operation} on {host} failed: "
            f"{type(exc).__name__}: {exc}; {fate}"
        ) from exc
    _log.info(
        "%s: wrote %s on %s (%d B, sha256 %s, verified=%s, replaced=%s)",
        operation.split("(", 1)[0], path, host, len(payload), digest[:16], verified, existing is not None,
    )
    return StoragePutResult(
        path=path, size=len(payload), sha256=digest,
        verified=verified, replaced=existing is not None,
    )


def _restore(ftp: FTP, host: str, path: str, original: bytes) -> str:
    """Put *original* back at *path* after a failed overwrite; say what is there now."""
    try:
        ftp.storbinary(f"STOR {path}", io.BytesIO(original))
        buf = io.BytesIO()
        ftp.retrbinary(f"RETR {path}", buf.write)
        if buf.getvalue() != original:
            raise Ultimate64StorageError("the restored file read back differently")
    except (Ultimate64StorageError, *_FTP_ALL_ERRORS) as exc:
        return (
            f"putting it back also failed ({type(exc).__name__}: {exc}); the previous "
            "file is gone or truncated; retry with overwrite=True"
        )
    return "the previous file was put back and verified; the device holds it unchanged"


def storage_mkdir(
    host: str, path: str, *, exist_ok: bool = True, port: int = DEFAULT_FTP_PORT,
    timeout: float = DEFAULT_STORAGE_FTP_TIMEOUT,
) -> bool:
    """Create the directory *path* and any missing parents.

    Returns ``True`` if it was created, or ``False`` if it already
    existed and *exist_ok* is true.

    :raises FileExistsError: it exists and *exist_ok* is false, or it
        is a file.
    :raises NotADirectoryError: an ancestor is a file (such as a disk image).
    """
    parts = _check_path(path, write=True)
    with _session(host, f"storage_mkdir({path!r})", port, timeout) as ftp:
        path = _resolve_volume(ftp, host, parts)
        kind = _walk(ftp, path)
        if kind == "dir":
            if exist_ok:
                return False
            raise FileExistsError(f"{path} already exists on {host}")
        if kind == "file":
            raise FileExistsError(f"{path} exists on {host} and is a file")
        _make_parents(ftp, path, include_self=True)
    _log.info("storage_mkdir: created %s on %s", path, host)
    return True


def storage_delete_file(
    host: str, path: str, *, missing_ok: bool = False, port: int = DEFAULT_FTP_PORT,
    timeout: float = DEFAULT_STORAGE_FTP_TIMEOUT,
) -> bool:
    """Delete the file at *path*. Directories are refused.

    Returns ``True`` if a file was deleted, or ``False`` if it was absent
    and *missing_ok* is true.

    :raises FileNotFoundError: absent and *missing_ok* is false.
    :raises IsADirectoryError: *path* is a directory.
    :raises NotADirectoryError: an ancestor is a file (such as a disk image).
    """
    parts = _check_path(path, write=True)
    with _session(host, f"storage_delete_file({path!r})", port, timeout) as ftp:
        path = _resolve_volume(ftp, host, parts)
        kind = _walk(ftp, path)
        if kind == "dir":
            raise IsADirectoryError(f"{path} on {host} is a directory")
        if kind is None:
            if missing_ok:
                return False
            raise FileNotFoundError(f"{path} does not exist on {host}")
        ftp.delete(path)
    _log.info("storage_delete_file: deleted %s on %s", path, host)
    return True


def storage_rmdir(
    host: str, path: str, *, missing_ok: bool = False, port: int = DEFAULT_FTP_PORT,
    timeout: float = DEFAULT_STORAGE_FTP_TIMEOUT,
) -> bool:
    """Remove the empty directory *path*. Volume roots are refused.

    Returns ``True`` if it was removed, or ``False`` if it was absent and
    *missing_ok* is true. A directory that still has entries is refused
    with :class:`Ultimate64StorageNotEmptyError`. Visible entries are
    caught before ``RMD`` is sent (``.entries`` names them). Hidden ones
    (see :func:`storage_list_dir`) make ``RMD`` answer 550, which raises
    the same error with empty ``.entries``.

    :raises FileNotFoundError: absent and *missing_ok* is false.
    :raises NotADirectoryError: *path*, or an ancestor, is a file.
    """
    parts = _check_path(path, write=True)
    with _session(host, f"storage_rmdir({path!r})", port, timeout) as ftp:
        path = _resolve_volume(ftp, host, parts)
        kind = _walk(ftp, path)
        if kind is None:
            if missing_ok:
                return False
            raise FileNotFoundError(f"{path} does not exist on {host}")
        if kind == "file":
            raise NotADirectoryError(f"{path} on {host} is a file")
        left = [n for n, f in ftp.mlsd(path) if n not in (".", "..")]
        if left:
            raise Ultimate64StorageNotEmptyError(
                f"{path} on {host} is not empty ({len(left)} entries)", sorted(left)
            )
        try:
            ftp.rmd(path)
        except error_perm as exc:
            if not str(exc).startswith("550"):
                raise
            # f_unlink refuses a non-empty directory. Entries the listing
            # omits ('.'-names and hidden-attribute files, such as a Mac's
            # .DS_Store or ._*) still count, so the pre-check above passed.
            raise Ultimate64StorageNotEmptyError(
                f"{path} on {host}: RMD answered {exc}. The directory most likely "
                "still holds hidden entries that MLSD does not list (names "
                "starting with '.', or files with the hidden attribute, such as "
                ".DS_Store), or the directory is read-only or the volume "
                "write-protected; remove them by another route",
                [],
            ) from exc
    _log.info("storage_rmdir: removed %s on %s", path, host)
    return True
