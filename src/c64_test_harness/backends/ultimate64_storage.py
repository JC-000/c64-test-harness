"""Files on an Ultimate's persistent storage, over the firmware's FTP server.

The firmware's FTP server (``software/network/ftpd.cc``) lists every
volume and media slot at its root. On this bench (2026-10-08) the C64U
listed ``/SD``, ``/Flash`` and ``/Temp``, but its ``/SD`` slot was empty,
so it had no writable volume. The U64E listed ``/USB1``, ``/Flash`` and
``/Temp``. These helpers put test inputs on the removable volumes (a
disk image that a C64-side program loads through UCI DOS, a rig's trust
store) and read, list and delete them.

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
  target any volume. Checked before any connection.
* **Paths are absolute and normalised:** no ``.``/``..``, no empty
  components, no trailing ``/``, no control characters, no backslash, no
  ``%``, no component over :data:`MAX_COMPONENT_LEN` characters, and at
  least a volume plus a name. ``%`` reaches a format string in 1.1.0's
  ``MLST`` (fixed upstream in #713). The firmware treats ``\\`` as a path
  separator and resolves ``..``, so ``/USB1/..\\Temp`` reaches ``/Temp``.
  Checked before any connection.
* **The volume must exist.** Its name is matched case-insensitively
  against the root listing, and the device's own spelling is used. A
  missing volume raises :class:`Ultimate64StorageVolumeError` with the
  volumes that do exist, so a rig can skip cleanly. One example is
  ``/USB1`` on a C64U that has no USB stick, or its ``/SD`` slot with no
  card in it: the root lists that slot, but it cannot be entered.
* **No config writes.** If FTP File Service is off, the call raises
  :class:`Ultimate64StorageError` naming the setting, and leaves it off.
* **Files and directories are told apart with** ``MLST``, which stats
  without mounting. ``CWD`` into a mountable image (``.d64``, ``.d81``,
  ``.t64``...) succeeds on the firmware and opens the image, so it is used
  only to check that a volume root can be entered.
* **A failed upload of a new file is deleted** (best effort) so that a
  retry is not refused. A failed overwrite cannot restore the old file,
  and its error says so.

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
    "WRITE_REFUSED_VOLUMES",
    "StorageEntry",
    "StoragePutResult",
    "Ultimate64StorageError",
    "Ultimate64StorageNotEmptyError",
    "Ultimate64StorageVolumeError",
    "storage_delete_file",
    "storage_get_file",
    "storage_list_dir",
    "storage_mkdir",
    "storage_put_file",
    "storage_rmdir",
    "storage_volumes",
    "storage_writable_volumes",
]

_log = logging.getLogger(__name__)

#: Volumes that put/mkdir/delete/rmdir never touch (compared casefolded).
WRITE_REFUSED_VOLUMES = frozenset({"temp", "flash"})

#: Socket timeout for the FTP session. An 880 KB image over the VPN to
#: the C64U takes a few seconds, so this is generous.
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


def _check_path(path: str, *, write: bool, allow_volume: bool = False) -> list[str]:
    """Validate *path* and return its components. Raises ``ValueError``."""
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
    if write and parts[0].casefold() in WRITE_REFUSED_VOLUMES:
        raise ValueError(
            f"refusing to write {path!r}: /{parts[0]} is not test storage "
            "(/Temp is the firmware RAM disk the harness sweeps; /Flash holds "
            "the firmware's own files)"
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
            "client.set_config_item('Network Settings', 'FTP File Service', "
            "'Enabled'); the storage helpers never write config."
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
    left out. For somewhere writable, use :func:`storage_writable_volumes`.
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
    SHA-256 compared. Either mismatch raises
    :class:`Ultimate64StorageError` and leaves the file in place for
    inspection.
    """
    parts = _check_path(path, write=True)
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
    digest = hashlib.sha256(payload).hexdigest()
    with _session(host, f"storage_put_file({path!r})", port, timeout) as ftp:
        path = _resolve_volume(ftp, host, parts)
        kind = _walk(ftp, path)
        if kind == "dir":
            raise IsADirectoryError(f"{path} on {host} is a directory")
        existing = _size(ftp, path) if kind == "file" else None
        if kind == "file" and not overwrite:
            raise FileExistsError(
                f"{path} already exists on {host} ({existing} B); pass overwrite=True"
            )
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
                fate = "the previous file is gone or truncated; retry with overwrite=True"
            raise Ultimate64StorageError(
                f"storage_put_file({path!r}) on {host} failed: "
                f"{type(exc).__name__}: {exc}; {fate}"
            ) from exc
    _log.info(
        "storage_put_file: wrote %s on %s (%d B, sha256 %s, verified=%s, replaced=%s)",
        path, host, len(payload), digest[:16], verified, existing is not None,
    )
    return StoragePutResult(
        path=path, size=len(payload), sha256=digest,
        verified=verified, replaced=existing is not None,
    )


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
