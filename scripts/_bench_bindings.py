"""Per-device bindings of the bench scripts, read from a local file that is never committed.

A device's unique_id is serial-derived and this repo is public, so which device a script may drive, and through
which adapter or outlet, lives in local configuration:

    ${C64_BENCH_BINDINGS_FILE:-${XDG_CONFIG_HOME:-~/.config}/c64-test-harness/bench.toml}

    [devices.<unique_id>]                 # the GET /v1/info unique_id, the id devices.toml folds the host to
    ram_run = true                        # ram_run_u64.py may RAM-run this device
    jtag_adapter_location = "<USB loc>"   # jtag_load.py: the esp-jtag adapter's pyserial .location ...
    jtag_adapter_baud = 921600            # ... and its baud rate (both, for a load or a scan)
    jtag_scan_only = true                 # jtag_load.py scan-u64e only: the device is never loaded
    outlet = "http://<plug address>"      # power_outlet.py: the switched outlet ...
    outlet_kind = "shelly-gen2"           # ... and its API ("shelly-gen2" or "tasmota")

It is a separate file from the alias map (devices.toml), which every harness version reads and which refuses any key
but ``hosts``. Only these scripts read this one. A host is resolved to its unique_id through the alias map, as
before; a device with no table, or a table without a script's keys, is refused by that script.

Refused here, with the path in the message (BenchBindingError): a missing or unreadable file, invalid TOML, a
top-level key other than ``devices``, a table id that is not a unique_id, an unknown key (a typo must not pass
silently), and a value of the wrong type.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

BENCH_FILE_ENV = "C64_BENCH_BINDINGS_FILE"

#: key -> (check, what it must be)
_CHECKS = {
    "ram_run": (lambda v: isinstance(v, bool), "true or false"),
    "jtag_adapter_location": (lambda v: isinstance(v, str) and bool(v.strip()), "a non-empty string"),
    "jtag_adapter_baud": (lambda v: isinstance(v, int) and not isinstance(v, bool) and v > 0, "a positive integer"),
    "jtag_scan_only": (lambda v: isinstance(v, bool), "true or false"),
    "outlet": (lambda v: isinstance(v, str) and bool(v.strip()), "a non-empty string"),
    "outlet_kind": (lambda v: isinstance(v, str) and bool(v.strip()), "a non-empty string"),
}
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,15}$")     # as device_aliases._ID_RE


class BenchBindingError(RuntimeError):
    pass


def bench_file_path() -> Path:
    """The bindings file this process reads (it need not exist)."""
    explicit = os.environ.get(BENCH_FILE_ENV)
    if explicit:
        return Path(explicit).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "c64-test-harness" / "bench.toml"


def load_bench_file(path: Path | None = None) -> dict[str, dict]:
    """{unique_id (lower case): table}, every table validated; BenchBindingError on anything malformed."""
    import tomllib
    p = Path(path) if path is not None else bench_file_path()
    try:
        doc = tomllib.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise BenchBindingError(f"{p} does not exist: the bench bindings (which device each script may drive) are "
                                f"local configuration; create it, or point ${BENCH_FILE_ENV} at it") from None
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise BenchBindingError(f"{p}: cannot read it as TOML ({e})") from None
    unknown = set(doc) - {"devices"}
    if unknown:
        raise BenchBindingError(f"{p}: unknown top-level key(s) {sorted(unknown)}; expected [devices.<unique_id>]")
    devices = doc.get("devices", {})
    if not isinstance(devices, dict):
        raise BenchBindingError(f"{p}: 'devices' must be a table")
    out: dict[str, dict] = {}
    for raw_id, table in devices.items():
        where = f"{p} [devices.{raw_id}]"
        uid = str(raw_id).strip().lower()
        if not _ID_RE.match(uid):
            raise BenchBindingError(f"{where}: {raw_id!r} is not a unique_id")
        if uid in out:
            raise BenchBindingError(f"{where}: device {uid} has two tables")
        if not isinstance(table, dict):
            raise BenchBindingError(f"{where}: must be a table")
        extra = set(table) - set(_CHECKS)
        if extra:
            raise BenchBindingError(f"{where}: unknown key(s) {sorted(extra)}; known: {sorted(_CHECKS)}")
        for key, value in table.items():
            check, what = _CHECKS[key]
            if not check(value):
                raise BenchBindingError(f"{where}: {key} must be {what}, not {value!r}")
        out[uid] = dict(table)
    return out


def bench_table(uid: str, path: Path | None = None) -> dict:
    """The bindings of device *uid* (any case); {} when the file has no table for it."""
    return load_bench_file(path).get(str(uid).strip().lower(), {})
