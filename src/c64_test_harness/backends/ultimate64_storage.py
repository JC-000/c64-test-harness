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
* **Writes never go to** ``/Temp`` **or** ``/Flash``: ``put``, ``mkdir``
  and ``delete`` refuse them. ``/Temp`` is the firmware's RAM disk,
  emptied at power-on and swept by the harness's ``/Temp`` hygiene.
  ``/Flash`` holds the firmware's own configuration and ROMs. Reads may
  target any volume. Checked before any connection.
* **Paths are absolute and normalised:** no ``.``/``..``, no empty
  components, no trailing ``/``, no control characters, and at least a
  volume plus a name. Checked before any connection.
* **The volume must exist.** Its name is matched case-insensitively
  against the root listing, and the device's own spelling is used. A
  missing volume raises :class:`Ultimate64StorageVolumeError` with the
  volumes that do exist, so a rig can skip cleanly. One example is
  ``/USB1`` on a C64U that has no USB stick, or its ``/SD`` slot with no
  card in it: the root lists that slot, but it cannot be entered.
* **No config writes.** If FTP File Service is off, the call raises
  :class:`Ultimate64StorageError` naming the setting, and leaves it off.

FTP ``STOR`` writes straight to the target volume (``ftpd.cc``
``cmd_stor``). It is not a REST body, so it creates no ``/Temp``
attachment on any firmware. ``/Temp`` hygiene for REST uploads is
separate and automatic; see ``ultimate64_temp_gc``.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
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
    "StoragePutResult",
    "Ultimate64StorageError",
    "Ultimate64StorageVolumeError",
    "storage_delete_file",
    "storage_get_file",
    "storage_mkdir",
    "storage_put_file",
    "storage_volumes",
]

_log = logging.getLogger(__name__)

#: Volumes that put/mkdir/delete never touch (compared casefolded).
WRITE_REFUSED_VOLUMES = frozenset({"temp", "flash"})

#: Socket timeout for the FTP session. An 880 KB image over the VPN to
#: the C64U takes a few seconds, so this is generous.
DEFAULT_STORAGE_FTP_TIMEOUT = 60.0

#: OSError subclasses raised on purpose here. ftplib's ``all_errors``
#: includes OSError, so they are re-raised before that clause can wrap them.
_PASS_THROUGH = (FileExistsError, FileNotFoundError, IsADirectoryError)


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


def _check_path(path: str, *, write: bool) -> list[str]:
    """Validate *path* and return its components. Raises ``ValueError``."""
    if not isinstance(path, str) or not path:
        raise ValueError("storage path must be a non-empty string")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in path):
        raise ValueError(f"storage path contains a control character: {path!r}")
    if not path.startswith("/"):
        raise ValueError(f"storage path must be absolute: {path!r}")
    if path.endswith("/"):
        raise ValueError(f"storage path must not end in '/': {path!r}")
    parts = path[1:].split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"storage path must be normalised (no '', '.', '..'): {path!r}")
    if len(parts) < 2:
        raise ValueError(f"storage path must name a volume and an entry: {path!r}")
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
    usable = [n for n in names if _is_dir(ftp, "/" + n)]
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
    except Ultimate64Error:
        raise
    except _FTP_ALL_ERRORS as exc:
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
        "is listed but has no medium mounted"
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


def _is_dir(ftp: FTP, path: str) -> bool:
    """Whether *path* is a directory, tested by whether ``CWD`` into it succeeds."""
    try:
        ftp.cwd(path)
    except error_perm:
        return False
    ftp.cwd("/")
    return True


def _make_parents(ftp: FTP, path: str, *, include_self: bool) -> None:
    parts = path[1:].split("/")
    stop = len(parts) + 1 if include_self else len(parts)
    for depth in range(2, stop):
        sub = "/" + "/".join(parts[:depth])
        if _is_dir(ftp, sub):
            continue
        if _size(ftp, sub) is not None:
            raise FileExistsError(f"{sub} exists and is not a directory")
        ftp.mkd(sub)


def storage_volumes(
    host: str, *, port: int = DEFAULT_FTP_PORT, timeout: float = DEFAULT_STORAGE_FTP_TIMEOUT
) -> list[str]:
    """The usable volume names at the device's FTP root, e.g. ``['Flash', 'Temp', 'USB1']``.

    An empty media slot (listed at the root, but ``CWD`` into it fails) is
    left out. Filter out ``/Temp`` and ``/Flash`` yourself
    (``WRITE_REFUSED_VOLUMES``) to find somewhere writable.
    """
    with _session(host, "storage_volumes", port, timeout) as ftp:
        return _list_root(ftp)[0]


def storage_get_file(
    host: str, path: str, *, port: int = DEFAULT_FTP_PORT,
    timeout: float = DEFAULT_STORAGE_FTP_TIMEOUT,
) -> bytes:
    """Read the file at *path*. Any volume may be read.

    :raises FileNotFoundError: no such file.
    :raises IsADirectoryError: *path* is a directory.
    """
    parts = _check_path(path, write=False)
    with _session(host, f"storage_get_file({path!r})", port, timeout) as ftp:
        path = _resolve_volume(ftp, host, parts)
        if _is_dir(ftp, path):
            raise IsADirectoryError(f"{path} on {host} is a directory")
        if _size(ftp, path) is None:
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
    *path* is always refused (``IsADirectoryError``).

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
    digest = hashlib.sha256(payload).hexdigest()
    with _session(host, f"storage_put_file({path!r})", port, timeout) as ftp:
        path = _resolve_volume(ftp, host, parts)
        if _is_dir(ftp, path):
            raise IsADirectoryError(f"{path} on {host} is a directory")
        existing = _size(ftp, path)
        if existing is not None and not overwrite:
            raise FileExistsError(
                f"{path} already exists on {host} ({existing} B); pass overwrite=True"
            )
        _make_parents(ftp, path, include_self=False)
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

    :raises FileExistsError: it exists and *exist_ok* is false, or a
        file sits where a directory is needed.
    """
    parts = _check_path(path, write=True)
    with _session(host, f"storage_mkdir({path!r})", port, timeout) as ftp:
        path = _resolve_volume(ftp, host, parts)
        if _is_dir(ftp, path):
            if exist_ok:
                return False
            raise FileExistsError(f"{path} already exists on {host}")
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
    """
    parts = _check_path(path, write=True)
    with _session(host, f"storage_delete_file({path!r})", port, timeout) as ftp:
        path = _resolve_volume(ftp, host, parts)
        if _is_dir(ftp, path):
            raise IsADirectoryError(f"{path} on {host} is a directory")
        if _size(ftp, path) is None:
            if missing_ok:
                return False
            raise FileNotFoundError(f"{path} does not exist on {host}")
        ftp.delete(path)
    _log.info("storage_delete_file: deleted %s on %s", path, host)
    return True
