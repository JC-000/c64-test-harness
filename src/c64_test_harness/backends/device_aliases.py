"""One device, several addresses: the offline alias map and identity records (#519).

A U64E or C64U has two network interfaces. Ethernet and WiFi answer on
different addresses, and a DHCP lease change, a reflash that resets the
config, or a switch between the two moves the address while the device
stays the same. ``DeviceLock`` and the ``/Temp`` ledger key on
:func:`~c64_test_harness.backends.device_lock.normalize_device_host`, so
before this module ``10.43.23.81`` and ``10.43.23.83`` were two queues and
two budgets for one device (#519).

**The alias map** folds every listed spelling into one canonical key,
``uid-<unique_id>``. The id is the one the firmware reports in
``GET /v1/info``; by default ``getProductUniqueId()`` derives it from the
flash chip's serial (``software/system/product.cc``). It survives a
reflash, a DHCP change and a change of interface, and both interfaces
report the same id, which the MAC does not. The map is read from local
configuration only. The lock path must never contact the device (#434):
a probe there can block for seconds and would be aimed at a device that
may already be wedged.

Sources, merged, with the environment winning on conflict:

* the file ``$C64_DEVICE_ALIASES_FILE``, else
  ``${XDG_CONFIG_HOME:-~/.config}/c64-test-harness/devices.toml``.
  It is user-wide, so every project on this machine that uses the
  harness picks it up::

      [devices.601a96]
      hosts = ["10.43.23.81", "10.43.23.83", "u64e.lan"]

* ``C64_DEVICE_ALIASES="601a96=10.43.23.81,10.43.23.83;abcdef=..."``.

A broken configuration is fatal. A spelling listed under two ids, an empty
``hosts`` list, an id that is not a safe filename component, an unknown
key, unreadable TOML, or a file that is present when no TOML parser is
available all raise :class:`DeviceAliasConfigError` before any lock is
tried or device contacted. Silently falling back to per-address keys
would reopen #519 without anyone noticing. A missing file means an empty
map.

**Identity records** are what catch an alias nobody configured. The
client calls :func:`check_device_identity` after it has the lock and a
cached ``unique_id``; that costs no request, because it reuses the probe the
client already made. Each record is a small JSON file in the lock directory
that lists the keys a device has been reached under. If another key for the
same id is held by a live foreign process right now, two lanes are driving
one device. See :func:`check_device_identity` for what happens then.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - exercised on 3.10 only
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]

_log = logging.getLogger(__name__)

__all__ = [
    "ALIASES_ENV",
    "ALIASES_FILE_ENV",
    "CANONICAL_PREFIX",
    "DeviceAliasConfigError",
    "IdentityFinding",
    "aliases_file_path",
    "canonical_key_for_id",
    "check_device_identity",
    "configured_unique_id",
    "fold_alias",
]

ALIASES_ENV = "C64_DEVICE_ALIASES"
ALIASES_FILE_ENV = "C64_DEVICE_ALIASES_FILE"

#: Prefix of every canonical key. It keeps an id from ever reading as a
#: hostname, and lets a ``DeviceLock("uid-601a96")`` name the device directly.
CANONICAL_PREFIX = "uid-"

#: The firmware's own limit on a hand-entered Unique ID is 16 characters
#: (``network_config.cc``); the default is 6 lowercase hex digits. Only
#: characters that are safe in a lockfile name are accepted.
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,15}$")


class DeviceAliasConfigError(ValueError):
    """The device alias configuration cannot be read as a map (#519).

    Deliberately fatal, never a fall-back to per-address keys: a typo that
    silently dropped a group would put two queues back on one device.
    Raised before any lock is tried and before any device is contacted.
    """


def canonical_key_for_id(unique_id: str) -> str:
    """The lock and ledger key for the device whose ``unique_id`` this is."""
    return CANONICAL_PREFIX + str(unique_id).strip().lower()


def aliases_file_path() -> Path:
    """The alias file this process reads (it need not exist)."""
    explicit = os.environ.get(ALIASES_FILE_ENV)
    if explicit:
        return Path(explicit).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "c64-test-harness" / "devices.toml"


def _valid_id(raw: object, where: str) -> str:
    uid = str(raw).strip().lower()
    if not _ID_RE.match(uid):
        raise DeviceAliasConfigError(
            f"{where}: device id {raw!r} is not a unique_id (1-16 of "
            "a-z, 0-9, '-', '_'; the firmware default is 6 hex digits)"
        )
    return uid


def _add(
    out: dict[str, str], spelling: str, uid: str, where: str,
    normalize: Callable[[str], str],
) -> None:
    key = normalize(spelling)
    if not key or key.startswith(CANONICAL_PREFIX):
        raise DeviceAliasConfigError(
            f"{where}: {spelling!r} is not a host spelling"
        )
    prior = out.get(key)
    if prior is not None and prior != uid:
        raise DeviceAliasConfigError(
            f"{where}: {spelling!r} is listed under two devices "
            f"({prior} and {uid}); one address is one device"
        )
    out[key] = uid


def _parse_file(path: Path, normalize: Callable[[str], str]) -> dict[str, str]:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise DeviceAliasConfigError(f"{path}: cannot read ({exc})") from exc
    if tomllib is None:
        raise DeviceAliasConfigError(
            f"{path}: reading device aliases requires Python 3.11+ or the "
            "'tomli' package"
        )
    try:
        doc = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise DeviceAliasConfigError(f"{path}: not valid TOML ({exc})") from exc
    unknown = set(doc) - {"devices"}
    if unknown:
        raise DeviceAliasConfigError(
            f"{path}: unknown top-level key(s) {sorted(unknown)}; expected "
            "[devices.<unique_id>] tables"
        )
    devices = doc.get("devices", {})
    if not isinstance(devices, dict):
        raise DeviceAliasConfigError(f"{path}: 'devices' must be a table")
    out: dict[str, str] = {}
    for raw_id, table in devices.items():
        where = f"{path} [devices.{raw_id}]"
        uid = _valid_id(raw_id, where)
        if not isinstance(table, dict):
            raise DeviceAliasConfigError(f"{where}: must be a table")
        extra = set(table) - {"hosts"}
        if extra:
            raise DeviceAliasConfigError(
                f"{where}: unknown key(s) {sorted(extra)}; expected 'hosts'"
            )
        hosts = table.get("hosts")
        if (
            not isinstance(hosts, list)
            or not hosts
            or not all(isinstance(h, str) and h.strip() for h in hosts)
        ):
            raise DeviceAliasConfigError(
                f"{where}: 'hosts' must be a non-empty list of host strings"
            )
        for h in hosts:
            _add(out, h, uid, where, normalize)
    return out


def _parse_env(value: str, normalize: Callable[[str], str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for group in value.split(";"):
        if not group.strip():
            continue
        where = f"{ALIASES_ENV} group {group.strip()!r}"
        raw_id, sep, hosts = group.partition("=")
        if not sep:
            raise DeviceAliasConfigError(
                f"{where}: expected '<unique_id>=<host>,<host>'"
            )
        uid = _valid_id(raw_id, where)
        spellings = [h for h in (s.strip() for s in hosts.split(",")) if h]
        if not spellings:
            raise DeviceAliasConfigError(f"{where}: lists no hosts")
        for h in spellings:
            _add(out, h, uid, where, normalize)
    return out


_cache_lock = threading.Lock()
_cache: tuple[object, dict[str, str]] | None = None


def _source_signature(path: Path) -> object:
    try:
        st = path.stat()
        file_sig: object = (st.st_ino, st.st_mtime_ns, st.st_size)
    except FileNotFoundError:
        file_sig = None
    except OSError as exc:
        raise DeviceAliasConfigError(f"{path}: cannot stat ({exc})") from exc
    return (os.environ.get(ALIASES_ENV, ""), str(path), file_sig)


def _alias_map(normalize: Callable[[str], str]) -> dict[str, str]:
    """``{normalised spelling: unique_id}``, re-read when a source changes."""
    global _cache
    path = aliases_file_path()
    sig = _source_signature(path)
    with _cache_lock:
        if _cache is not None and _cache[0] == sig:
            return _cache[1]
        merged = _parse_file(path, normalize)
        # The environment wins: a lane can re-point one spelling for a run.
        merged.update(_parse_env(os.environ.get(ALIASES_ENV, ""), normalize))
        _cache = (sig, merged)
        return merged


def fold_alias(key: str, normalize: Callable[[str], str]) -> str:
    """*key* (already normalised) folded to its device's canonical key.

    A spelling that is not listed is returned unchanged, and so is a
    canonical key, so folding is idempotent. *normalize* is the
    spelling-only normaliser used to read the configured hosts the same
    way *key* was produced.
    """
    uid = _alias_map(normalize).get(key)
    return canonical_key_for_id(uid) if uid is not None else key


def configured_unique_id(key: str, normalize: Callable[[str], str]) -> str | None:
    """The id *key* (normalised, or canonical) is configured as, else ``None``."""
    if key.startswith(CANONICAL_PREFIX):
        return key[len(CANONICAL_PREFIX):]
    return _alias_map(normalize).get(key)


# --------------------------------------------------------------------------- #
# Identity records: catching an alias nobody configured                       #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class IdentityFinding:
    """What :func:`check_device_identity` found, for the client to act on.

    *kind* is ``"collision"`` (another key for this device is held by a live
    foreign process now), ``"mismatch"`` (the alias file says this key is a
    different device from the one that answered), or ``"seen"`` (this
    device has been reached under another key before, and nobody holds it
    now).
    """

    kind: str
    message: str


def _identity_path(lock_dir: Path, unique_id: str) -> Path:
    return lock_dir / f"identity-{canonical_key_for_id(unique_id)}.json"


def _read_record(path: Path) -> dict[str, dict]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    keys = data.get("keys") if isinstance(data, dict) else None
    return {k: v for k, v in keys.items() if isinstance(v, dict)} if isinstance(keys, dict) else {}


def _write_record(path: Path, keys: Mapping[str, dict]) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps({"keys": dict(keys)}, sort_keys=True))
        os.replace(tmp, path)
    except OSError as exc:  # a record is a diagnostic aid, never a failure
        _log.debug("device identity record %s not written: %s", path, exc)
        try:
            tmp.unlink()
        except OSError:
            pass


def _forget_key_elsewhere(lock_dir: Path, key: str, keep: Path) -> None:
    """*key* now reaches the device recorded in *keep*: drop it from the rest.

    An address can move to another device (DHCP).  Without this, the old
    device's record would keep listing it, and a lane legitimately holding
    it for the new device would read as a collision to the old device's
    lanes.  Best-effort and unlocked: a lost update costs one stale entry.
    """
    try:
        records = list(lock_dir.glob(f"identity-{CANONICAL_PREFIX}*.json"))
    except OSError:
        return
    for other in records:
        if other == keep:
            continue
        keys = _read_record(other)
        if key in keys:
            del keys[key]
            _write_record(other, keys)


def _alias_hint(unique_id: str, spellings: list[str]) -> str:
    hosts = ", ".join(json.dumps(s) for s in sorted(set(spellings)))
    return (
        f"Declare them as one device in {aliases_file_path()}:\n"
        f"    [devices.{unique_id}]\n    hosts = [{hosts}]\n"
        "(see docs/device_locking.md, \"Multi-interface devices\")."
    )


def check_device_identity(
    key: str,
    host: str,
    unique_id: str | None,
    *,
    lock_dir: Path | None = None,
) -> IdentityFinding | None:
    """Record that *key* reached device *unique_id*; report a split queue.

    *key* is the caller's device key (already folded), *host* the spelling
    it connects to, and *unique_id* the id from the client's cached
    ``GET /v1/info``. Never does network I/O. Call it only while holding
    *key*'s lock: the record says "this key is a way to reach this device",
    and only a holder knows that the answer came from the device it locked.

    Returns ``None`` when there is nothing to report, including when
    *unique_id* is ``None`` (the client never probed, or the device's
    Unique ID config is empty, which omits it from ``/v1/info``).
    """
    if not unique_id:
        return None
    from . import device_lock as _dl

    uid = str(unique_id).strip().lower()
    configured = configured_unique_id(key, _dl._normalize_spelling)
    if configured is not None and configured != uid:
        return IdentityFinding(
            "mismatch",
            f"{host} is configured as device {configured} (key "
            f"{canonical_key_for_id(configured)}) but the device answering "
            f"there reports unique_id {uid}. The lock this process holds "
            "protects a different device from the one it is driving: the "
            "address moved (DHCP, a change of interface), or the device's "
            "Network Settings > Unique ID was set by hand. Fix the alias "
            f"file ({aliases_file_path()}) before continuing.",
        )
    d = lock_dir or _dl._default_lock_dir()
    path = _identity_path(d, uid)
    keys = _read_record(path)
    entry = keys.get(key)
    if entry is None or entry.get("pid") != os.getpid():
        keys[key] = {"host": host, "pid": os.getpid(), "last_seen": time.time()}
        _write_record(path, keys)
        _forget_key_elsewhere(d, key, path)
    # A key that now folds to this one was a spelling the alias map has
    # since joined to it: no longer a separate queue, so not a finding.
    others = {
        k: v for k, v in keys.items()
        if k != key and _dl.normalize_device_host(k) != key
    }
    if not others:
        return None
    held = []
    for other in sorted(others):
        try:
            holder = _dl.DeviceLock.foreign_holder(other, lock_dir=lock_dir)
        except Exception:  # noqa: BLE001 - a lock query must never fail the caller
            holder = None
        if holder is not None:
            held.append((other, holder.get("pid")))
    spellings = [host] + [str(v.get("host", k)) for k, v in others.items()]
    if held:
        names = ", ".join(f"{k} (pid {pid})" for k, pid in held)
        return IdentityFinding(
            "collision",
            f"device {uid} is reached as {key} by this process and is locked "
            f"right now under another key by a different process: {names}. "
            "Two lanes are driving one device with two locks and two /Temp "
            "budgets (#519). " + _alias_hint(uid, spellings),
        )
    return IdentityFinding(
        "seen",
        f"device {uid} has been reached under several keys "
        f"({', '.join(sorted([key, *others]))}); each one is a separate "
        "lock and /Temp budget. " + _alias_hint(uid, spellings),
    )
