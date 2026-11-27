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
    jtag_blaster_ssh = "<ssh destination>"   # blaster_load_u64.py: where jtagd serves the USB-Blaster ...
    jtag_blaster_cable = "USB-Blaster"       # ... and text its one cable's name in jtagconfig must contain (both)
    jtag_blaster_ssh_port = 2200             # optional: the ssh port,
    jtag_blaster_ssh_identity = "<key file>" # the ssh key,
    jtag_blaster_env = ". <env script>"      # shell text run before every command there (PATH for the tools),
    jtag_blaster_jtagd_start = "<command>"   # and the command that starts jtagd there when it is not running

    volatile_baseline_image = "<path>"    # volatile_load.py: the image volatile_restore returns to (a RAM-run
                                          # .u64 for the U64 line, a one-record ultimate.app for the C64U class) ...
    volatile_baseline_sha256 = "<64 hex>" # ... its SHA-256 ...
    volatile_baseline_git_hash = "<hex>"  # ... and the git hash /v1/info reports when it runs (7-40 hex digits)
    volatile_baseline_bit = "<path>"      # C64U class: the baseline's FPGA bitstream ...
    volatile_baseline_bit_sha256 = "<64 hex>"   # ... and its SHA-256
    volatile_baseline_jtag_image = "<path>"     # U64 line, optional: what a U64 JTAG reload loads into a dark box
    volatile_baseline_jtag_sha256 = "<64 hex>"  # ... and its SHA-256

The jtag_blaster_* keys are a separate capability from jtag_adapter_* / jtag_scan_only (the esp-jtag adapter of
jtag_load.py): neither set of keys enables, or lifts a refusal of, the other script.

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

def _is_hex(v, lo: int, hi: int) -> bool:
    return isinstance(v, str) and re.fullmatch(r"[0-9a-fA-F]{%d,%d}" % (lo, hi), v) is not None


#: key -> (check, what it must be)
_CHECKS = {
    "ram_run": (lambda v: isinstance(v, bool), "true or false"),
    "jtag_adapter_location": (lambda v: isinstance(v, str) and bool(v.strip()), "a non-empty string"),
    "jtag_adapter_baud": (lambda v: isinstance(v, int) and not isinstance(v, bool) and v > 0, "a positive integer"),
    "jtag_scan_only": (lambda v: isinstance(v, bool), "true or false"),
    "outlet": (lambda v: isinstance(v, str) and bool(v.strip()), "a non-empty string"),
    "outlet_kind": (lambda v: isinstance(v, str) and bool(v.strip()), "a non-empty string"),
    # An ssh destination is passed to ssh and scp as one argument: no option look-alike, nothing a shell would split.
    "jtag_blaster_ssh": (lambda v: isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_.@\[\]:-]+", v) is not None
                         and not v.startswith("-"), "an ssh destination (user@host or a Host alias)"),
    "jtag_blaster_ssh_port": (lambda v: isinstance(v, int) and not isinstance(v, bool) and 0 < v < 65536,
                              "a port number"),
    "jtag_blaster_ssh_identity": (lambda v: isinstance(v, str) and bool(v.strip()) and not v.startswith("-"),
                                  "a key file path"),
    "jtag_blaster_cable": (lambda v: isinstance(v, str) and bool(v.strip()), "a non-empty string"),
    "jtag_blaster_env": (lambda v: isinstance(v, str) and bool(v.strip()), "a non-empty string"),
    "jtag_blaster_jtagd_start": (lambda v: isinstance(v, str) and bool(v.strip()), "a non-empty string"),
    "volatile_baseline_image": (lambda v: isinstance(v, str) and bool(v.strip()), "a non-empty string"),
    "volatile_baseline_sha256": (lambda v: _is_hex(v, 64, 64), "64 hex digits"),
    "volatile_baseline_git_hash": (lambda v: _is_hex(v, 7, 40), "7 to 40 hex digits"),
    "volatile_baseline_bit": (lambda v: isinstance(v, str) and bool(v.strip()), "a non-empty string"),
    "volatile_baseline_bit_sha256": (lambda v: _is_hex(v, 64, 64), "64 hex digits"),
    "volatile_baseline_jtag_image": (lambda v: isinstance(v, str) and bool(v.strip()), "a non-empty string"),
    "volatile_baseline_jtag_sha256": (lambda v: _is_hex(v, 64, 64), "64 hex digits"),
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
