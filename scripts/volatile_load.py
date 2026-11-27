#!/usr/bin/env python3
"""One volatile (no-flash) firmware load for both Ultimate products, above ram_run_u64.py, jtag_load.py and
blaster_load_u64.py (#585).

    volatile_load(host, image)        load a volatile image; the backend follows the device's product
    volatile_restore(host)            back to the baseline image configured in the local bench.toml
    with volatile_session(host, image, baseline_first=True) as s:   # one DeviceLock across all of it
        ...                           # the caller's rows; harness helpers pass their lock checks here

Backends (this file adds no device I/O of its own beyond a bodyless /v1/info read; each backend keeps its gates):
  * "ram_run"   "Ultimate 64" / "Ultimate 64 Elite": ram_run_u64.ram_run (stage the RAM-run .u64 on a storage
                volume, Run Update, delete the staged file). Needs the network.
  * "jtag_cold" "C64 Ultimate" / "Ultimate 64-II": jtag_load.jtag_load, always cold (the CPU is held in reset
                from the FPGA load until the upload is complete). This API has no warm variant and passes
                cold=True whatever JTAG_LOAD_WARM says. Works on a box that does not answer on the network.
  * "u64_jtag_reload"  a U64-line box that does not answer: blaster_load_u64.blaster_load (the application ELF
                over JTAG through a USB-Blaster route, with that script's own binding and gates), called with
                dark=True. The image must name the ELF (jtag_image, jtag_sha256). The callable is replaceable
                (U64_JTAG_RELOAD below).
The backend is chosen under the lock from the /v1/info product. A box that does not answer (three polls) is "dark":
the backend then follows the image's product family, which the device's bench.toml table must enable, and the
device's identity (unique_id, product) is confirmed on /v1/info after the load.

Pre-flight, before any lock, client, upload or JTAG pin is driven. An image is one of exactly two shapes:
  * U64 line: a RAM-run file, as ram_run_u64.check_ramrun_image accepts it;
  * C64U class: a one-record ``ultimate.app`` at 0x30000 (what jtag_load.payload_from_app accepts; the DRAM image
    is extracted from it here) plus the FPGA .bit with its own SHA-256. A raw ``ultimate.bin`` carries nothing to
    check and is not accepted here.
Everything else is refused: update images of either product, .cfw files, garbage. So is any file that holds an
updater prompt string or the SILENT marker (ultimate64_firmware_flash.PROMPT_STRINGS / SILENT_MARKER), whatever
its shape: an updater that is started writes flash. The image's family must equal the device's.
What this refuses is update images as they are built: by structure, and by the updater's strings. It cannot tell
what an application does: a hand-made RAM-run wrapper around an updater that carries none of those strings would
pass.

The image must also BE the build the caller names. Both formats carry the application's System Info line,
"Git hash:   <hash>" (system_info.cc: "Git hash:   " APP_VERSION_HASH "\\n"), where the hash is the build's
`git rev-parse --short HEAD` (target/common/rules.mk), the same macro /v1/info reports as git_commit_hash. That line
is read from the file (embedded_git_hashes). An image with no such line, or with two different ones, is refused; so
is a caller's hash that does not begin with every digit the image carries (a caller's hash shorter than the image's
is refused too: fewer digits would leave part of it uncompared). After the load, /v1/info must report exactly the
image's own hash.

Adapter safety (C64U class): the adapter is the one bound to the device in bench.toml (jtag_load.adapter_binding;
a jtag_scan_only device is refused), a JTAG_USB_LOCATION that names another location is refused here (jtag_load
alone lets it override the table), and jtag_load's live IDCODE gate still runs before the FPGA is touched.

After a load the running firmware must report the image's expected git hash with the bound unique_id and a product
of the same family, else the call fails. What the result claims about a restart ("restart_proof"):
  * "hash-change"        the reported hash before the load differed from the expected one and now equals it;
  * "jtag-reconfigured"  the FPGA was reloaded cold over JTAG and the user ID read back (jtag_cold). This holds
                         for a same-hash load as well;
  * "no-answer-before", "rest-drop"  (u64_jtag_reload) blaster_load's own proofs, carried through as it reports
                         them, as is its "hash-change": the box did not answer before the load and answers now;
                         or REST went away after the download although the hash was the same;
  * "none"               nothing was loaded (see "loaded"), or the backend gave no evidence.
A reload that blaster_load ends as "ok-unproven" (the device reported the image's hash before the load as well, and
REST never dropped; measured once on an Ultimate 64 Elite, 2026-11-27: a same-hash reload showed no REST outage)
is NOT ok here: the call fails with the stage "unproven" (CLI exit 3), the result keeps what was read, and no proof
is made up for it. The same goes for a reload record with no proof, or one this file does not know.
A same-hash RAM-run cannot be proven started (#570), so ram_run refuses it; volatile_restore on a U64-line device
that already reports the baseline hash therefore loads nothing and says so (loaded False, restart_proof "none"):
that the running image IS the baseline file is then not proven, only that the hashes agree.

``jtag_timeout`` below MIN_JTAG_TIMEOUT_S is refused. It is the budget handed to the U64 JTAG reload (blaster_load's
download_timeout, the deadline around its one nios2-download) and applies to nothing else: it does not reach
jtag_load.py. The floor stays at 300 s and is also the default. One measured hash-changing reload came up in about
22 s (n=1), so 300 s is more than ten times what a good download took; the floor is not there to fit the usual case
but to keep a caller from cutting a download that is slow for a reason nobody has seen yet, since a cut download
leaves the Nios paused and the box dark until a power cycle, while a hung one only costs the wait. One run does not
justify a lower floor, and blaster_load's own default is the same 300 s. This file puts no deadline around the cold esp-jtag load, but
jtag_load.py runs it with the kit's link timeout of 10 s per adapter transaction, and after a link error once the
FPGA is programmed it retries the whole load once and then leaves the box dark. So "no timeout under 300 s around a
JTAG operation" holds for the deadlines this file sets, not for that per-transaction one.

On the RAM-run path the staged file is uploaded without overwriting: a file already on the volume under the staging
name stops the load, and is neither replaced nor deleted.

Baseline, in the device's table of the local bench.toml (never in a tracked file, never in devices.toml):

    volatile_baseline_image = "<path>"          # RAM-run .u64 (U64 line) or one-record ultimate.app (C64U class)
    volatile_baseline_sha256 = "<64 hex>"
    volatile_baseline_git_hash = "<7-40 hex>"   # what /v1/info must report once it runs
    volatile_baseline_bit = "<path>"            # C64U class: the FPGA bitstream ...
    volatile_baseline_bit_sha256 = "<64 hex>"   # ... and its SHA-256
    volatile_baseline_jtag_image = "<path>"     # U64 line, optional: what the U64 JTAG reload loads into a dark box
    volatile_baseline_jtag_sha256 = "<64 hex>"

CLI (a failure exits 2, a reload whose restart is not proven exits 3; --dry-run prints the plan and takes no lock, opens no client, storage, menu or adapter):

    volatile_load.py load    HOST IMAGE --sha256 H --expect-hash G [--bit F --bit-sha256 H]
                             [--jtag-image F --jtag-sha256 H] [--baseline-first] [--dry-run] [--record F]
    volatile_load.py restore HOST [--dry-run] [--record F]
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import jtag_load as jl  # noqa: E402  (puts the harness src on sys.path)
import ram_run_u64 as rr  # noqa: E402

#: No timeout under this is accepted around a JTAG operation (an interrupted load leaves the box dark).
MIN_JTAG_TIMEOUT_S = 300.0

FAMILY_U64, FAMILY_C64U = "u64", "c64u"
#: /v1/info product -> family. "C64 Ultimate (MK1)" is in neither on purpose: no volatile path was reviewed for it.
PRODUCT_FAMILY = {**{p: FAMILY_U64 for p in rr.PRODUCTS}, **{p: FAMILY_C64U for p in jl.PRODUCTS}}

BACKEND_RAM_RUN, BACKEND_JTAG_COLD, BACKEND_U64_JTAG = "ram_run", "jtag_cold", "u64_jtag_reload"

#: Restart proofs a U64 JTAG reload record may carry; anything else, or result "ok-unproven", is unproven here.
RELOAD_PROOFS = ("hash-change", "no-answer-before", "rest-drop")


def blaster_reload(host: str, image_path, *, sha256, expect_git_hash, timeout, lock_timeout, lock_factory,
                   client_factory, dry_run, sleep, clock, log, **blaster_options) -> dict:
    """The U64-line JTAG reload: blaster_load_u64.blaster_load on a box that need not answer before the load
    (dark=True; if it does answer, blaster_load still checks its identity). *timeout* becomes its download_timeout.
    *blaster_options* go to blaster_load unchanged (its runner, for tests)."""
    import blaster_load_u64 as bl
    return bl.blaster_load(host, image_path, sha256=sha256, expect_git_hash=expect_git_hash, dark=True,
                           dry_run=dry_run, lock_timeout=lock_timeout, download_timeout=timeout,
                           lock_factory=lock_factory, client_factory=client_factory, sleep=sleep, clock=clock,
                           log=log, **blaster_options)


#: The U64-line JTAG reload used for a dark box. Any callable
#:     reload(host, image_path, *, sha256, expect_git_hash, timeout, lock_timeout, lock_factory, client_factory,
#:            dry_run, sleep, clock, log) -> dict
#: that enforces its own adapter binding, loads *image_path* without writing flash into a box that may not answer
#: before the load, and returns a record with "info" (the /v1/info read afterwards), "result" and "restart_proof"
#: (one of RELOAD_PROOFS). *timeout* is the budget of its JTAG operation, never below MIN_JTAG_TIMEOUT_S. None: a
#: dark U64-line box is refused.
U64_JTAG_RELOAD = blaster_reload

DARK_POLLS = 3


class VolatileLoadError(jl.JtagLoadError):
    """A volatile load or restore was refused or failed. ``stage`` as JtagLoadError; ``result`` is the
    VolatileResult with ok False (backend None when it failed before one was chosen)."""

    def __init__(self, stage: str, text: str, result: "VolatileResult"):
        RuntimeError.__init__(self, text)
        self.stage, self.result = stage, result


@dataclasses.dataclass(frozen=True)
class VolatileImage:
    """A volatile image and its pins. *path*: the RAM-run .u64 (U64 line) or the one-record ultimate.app (C64U
    class). *git_hash*: the build it is; it must begin with every digit of the hash the file carries, which is what
    /v1/info must report once it runs. *bit*/*bit_sha256*: the C64U FPGA bitstream.
    *jtag_image*/*jtag_sha256*: U64 line only, what the U64 JTAG reload loads when the box is dark."""
    path: Path
    sha256: str
    git_hash: str
    bit: Path | None = None
    bit_sha256: str | None = None
    jtag_image: Path | None = None
    jtag_sha256: str | None = None


@dataclasses.dataclass
class VolatileResult:
    ok: bool
    action: str                         # "load" | "restore"
    host: str
    backend: str | None = None          # BACKEND_*; None when nothing was chosen
    family: str | None = None
    product: str | None = None          # as /v1/info reported it after the load (before it, when nothing loaded)
    expect_git_hash: str | None = None  # the caller's
    image_git_hash: str | None = None   # read from the image file; the caller's hash begins with all of it
    git_hash: str | None = None         # reported by the running firmware
    loaded: bool = False                # a load was performed and verified
    dark_before: bool | None = None     # the box did not answer before the load
    hash_changed: bool | None = None    # None: the hash before is unknown
    restart_proof: str = "none"
    timings: dict = dataclasses.field(default_factory=dict)     # seconds per step
    notes: list = dataclasses.field(default_factory=list)
    record: dict | None = None          # the backend's own record
    error: str | None = None


def check_jtag_timeout(value) -> float:
    """*value* as seconds, refused (JtagLoadError "timeout") unless it is a number of at least MIN_JTAG_TIMEOUT_S."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = math.nan
    if isinstance(value, bool) or math.isnan(v) or v < MIN_JTAG_TIMEOUT_S:
        raise jl.JtagLoadError("timeout", f"jtag_timeout {value!r}: no timeout under {MIN_JTAG_TIMEOUT_S:.0f} s goes "
                               "around a JTAG operation (an interrupted load leaves the device dark)")
    return v


def _uid(host: str) -> str:
    key = jl.normalize_device_host(host)
    if not key.startswith("uid-"):
        raise jl.JtagLoadError("binding", f"{host} is not in devices.toml, so it has no unique_id to bind to")
    return key[4:].upper()


def baseline_image(uid: str) -> VolatileImage:
    """The baseline of device *uid* from its bench.toml table; refused (JtagLoadError "baseline") without the three
    required keys."""
    t = jl.device_table(uid)
    need = ("volatile_baseline_image", "volatile_baseline_sha256", "volatile_baseline_git_hash")
    missing = [k for k in need if k not in t]
    if missing:
        raise jl.JtagLoadError("baseline", f"device {uid} has no volatile baseline (its bench.toml table lacks "
                               f"{missing})")

    def path(key):
        return Path(t[key]).expanduser() if key in t else None
    return VolatileImage(path("volatile_baseline_image"), t["volatile_baseline_sha256"],
                         t["volatile_baseline_git_hash"], path("volatile_baseline_bit"),
                         t.get("volatile_baseline_bit_sha256"), path("volatile_baseline_jtag_image"),
                         t.get("volatile_baseline_jtag_sha256"))


def _updater_marks(data: bytes) -> list[str]:
    from c64_test_harness.backends.ultimate64_firmware_flash import PROMPT_STRINGS, SILENT_MARKER
    return [m.decode() for m in (*PROMPT_STRINGS, SILENT_MARKER) if m in data]


_GIT_HASH_LINE = re.compile(rb"Git hash:   ([0-9a-fA-F]{7,40})\n")


def embedded_git_hashes(data: bytes) -> list[str]:
    """The distinct build hashes *data* carries, lower case: the application's System Info line, "Git hash:   "
    APP_VERSION_HASH "\\n" (system_info.cc), whose hash is the string /v1/info reports as git_commit_hash."""
    return sorted({m.group(1).decode().lower() for m in _GIT_HASH_LINE.finditer(data)})


def image_git_hash(data: bytes, path, expect: str) -> str:
    """The one hash *data* carries, refused (JtagLoadError) unless *expect* names that build: "image" when the file
    carries no hash or two different ones, "hash" when *expect* does not begin with every digit the image carries."""
    found = embedded_git_hashes(data)
    if len(found) != 1:
        raise jl.JtagLoadError("image", f"{path} carries {'no' if not found else 'several'} 'Git hash:' line"
                               f"{'' if not found else 's ' + str(found)}: which build it is cannot be read from the "
                               "file, so it is not loaded")
    if not expect.startswith(found[0]):
        raise jl.JtagLoadError("hash", f"{path} is build {found[0]}, not the expected {expect} (the expected hash "
                               f"must begin with all {len(found[0])} digits the image carries)")
    return found[0]


@dataclasses.dataclass
class _Prepared:
    image: VolatileImage
    family: str
    expect: str                     # the caller's, normalised
    embedded: str                   # the image's own; expect begins with it
    copy: Path                      # the pinned copy the backend is given
    app_bin: Path | None = None     # C64U class: the extracted DRAM image ...
    app_sha256: str | None = None   # ... and its SHA-256


def prepare_image(image: VolatileImage, workdir: Path, tag: str = "image") -> _Prepared:
    """Pin *image* and decide which product family it is a volatile image for; refuse everything else.

    The file is read once; what is checked is what the backend gets (a copy in *workdir*). Raises JtagLoadError
    ("hash" for a pin, "image" for the shape) and touches no device."""
    expect = jl.check_git_hash(image.git_hash)
    try:
        data = Path(image.path).read_bytes()
    except OSError as e:
        raise jl.JtagLoadError("image", f"{image.path}: cannot read it ({e})") from None
    got = hashlib.sha256(data).hexdigest()
    if not image.sha256 or got != str(image.sha256).lower():
        raise jl.JtagLoadError("hash", f"{image.path}: SHA-256 {got} is not the pinned {image.sha256}")
    marks = _updater_marks(data)
    if marks:
        raise jl.JtagLoadError("image", f"{image.path} holds updater text {marks}: an updater that is started "
                               "writes flash; this call loads volatile images only")
    workdir = Path(workdir)
    try:
        rr.check_ramrun_image(data)
        as_u64 = None
    except jl.JtagLoadError as e:
        as_u64 = str(e)
    if as_u64 is None:
        if image.bit is not None or image.bit_sha256:
            raise jl.JtagLoadError("image", f"{image.path} is a U64 RAM-run image; a .bit belongs to a C64 Ultimate "
                                   "load only")
        copy = workdir / f"{tag}.u64"
        copy.write_bytes(data)
        return _Prepared(image, FAMILY_U64, expect, image_git_hash(data, image.path, expect), copy)
    copy = workdir / f"{tag}.app"
    copy.write_bytes(data)
    app_bin = workdir / f"{tag}.bin"
    try:
        if len(data) <= 12:
            raise jl.JtagLoadError("app", f"{len(data)} bytes: no record")
        app_sha = jl.payload_from_app(copy, app_bin)
    except jl.JtagLoadError as e:
        raise jl.JtagLoadError("image", f"{image.path} is not a volatile image for any supported product. As a U64 "
                               f"RAM-run file: {as_u64}. As a C64 Ultimate RAM application (one ultimate.app record "
                               f"at 0x30000): {e}") from None
    if image.bit is None or not image.bit_sha256:
        raise jl.JtagLoadError("image", f"{image.path} is a C64 Ultimate RAM application; its load needs the FPGA "
                               ".bit and the .bit's SHA-256")
    return _Prepared(image, FAMILY_C64U, expect, image_git_hash(data, image.path, expect), copy, app_bin, app_sha)


def check_binding(host: str, uid: str, family: str) -> None:
    """Refuse unless the device's bench.toml table enables the backend of *family*; for the C64U class also refuse
    an environment override that names another adapter than the bound one. No device I/O."""
    if family == FAMILY_U64:
        rr.bound_u64(host)
        return
    jl.adapter_binding(uid)                 # refuses a missing binding and a jtag_scan_only device
    bound = jl.device_table(uid)["jtag_adapter_location"]
    env = os.environ.get("JTAG_USB_LOCATION")
    if env and env != bound:
        raise jl.JtagLoadError("adapter", f"JTAG_USB_LOCATION={env} is not the adapter bound to device {uid} in "
                               "bench.toml; only the bound adapter may load it")


def _probe(host: str, client_factory, sleep) -> dict | None:
    """/v1/info, or None when DARK_POLLS reads in a row got no answer."""
    for n in range(DARK_POLLS):
        info = jl.try_info(host, client_factory)
        if info is not None:
            return info
        if n + 1 < DARK_POLLS:
            sleep(2.0)
    return None


def _check_device(info: dict, uid: str, family: str, when: str) -> None:
    if str(info.get("unique_id", "")).upper() != uid:
        raise jl.JtagLoadError("identity", f"{when}: device answers with unique_id {info.get('unique_id')!r}, bound "
                               f"is {uid}")
    product = info.get("product")
    if product not in PRODUCT_FAMILY:
        raise jl.JtagLoadError("identity", f"{when}: product {product!r} has no volatile-load backend (known: "
                               f"{sorted(PRODUCT_FAMILY)})")
    if PRODUCT_FAMILY[product] != family:
        raise jl.JtagLoadError("image", f"{when}: the device is a {product!r}, and the image is a volatile image "
                               f"for the {family} family")


@dataclasses.dataclass
class _Opts:
    lock_timeout: float = 600
    net_timeout: float = 180
    jtag_timeout: float = MIN_JTAG_TIMEOUT_S
    volume: str = "USB1"
    lock_factory: object = jl.DeviceLock
    client_factory: object = jl._client
    esp: object = None
    storage: object = None
    menu_factory: object = None
    ram_run: object = rr.ram_run
    jtag_load: object = jl.jtag_load
    u64_jtag_reload: object = None
    sleep: object = None
    clock: object = None
    log: object = jl._log


def _opts(kw: dict) -> _Opts:
    """The shared keyword options. There is deliberately no ``cold`` (or any other load-mode) option."""
    unknown = set(kw) - {f.name for f in dataclasses.fields(_Opts)}
    if unknown:
        raise TypeError(f"unexpected keyword argument(s) {sorted(unknown)}")
    o = _Opts(**kw)
    o.jtag_timeout = check_jtag_timeout(o.jtag_timeout)
    o.sleep, o.clock = o.sleep or time.sleep, o.clock or time.monotonic
    if o.u64_jtag_reload is None:
        o.u64_jtag_reload = U64_JTAG_RELOAD
    return o


def _backend_for(family: str, dark: bool, prep: _Prepared, o: _Opts) -> str:
    if family == FAMILY_C64U:
        return BACKEND_JTAG_COLD
    if not dark:
        return BACKEND_RAM_RUN
    if o.u64_jtag_reload is None:
        raise jl.JtagLoadError("backend", "the device does not answer on the network, a RAM-run needs it, and no "
                               "U64 JTAG reload is set (U64_JTAG_RELOAD is None); power cycle it to return to the "
                               "flashed image")
    if prep.image.jtag_image is None or not prep.image.jtag_sha256:
        raise jl.JtagLoadError("backend", "the device does not answer on the network and the image names no "
                               "jtag_image / jtag_sha256 for the U64 JTAG reload")
    return BACKEND_U64_JTAG


class _StageOnly:
    """The storage ram_run is given: its two calls, with the upload never overwriting. ram_run stages with
    overwrite=True and deletes the staged name afterwards, so a file that was already there under that name would
    be replaced and then removed. Here the upload refuses an existing file before anything is written, and ram_run
    (which uploads before its try/finally) then deletes nothing."""

    def __init__(self, storage):
        self._storage = storage

    def storage_put_file(self, host, path, data, overwrite=False):
        try:
            return self._storage.storage_put_file(host, path, data, overwrite=False)
        except FileExistsError:
            raise jl.JtagLoadError("storage", f"{path} already exists on the device: it is not this run's staged "
                                   "image, so it is neither overwritten nor deleted; remove or rename it") from None

    def storage_delete_file(self, host, path, missing_ok=False):
        return self._storage.storage_delete_file(host, path, missing_ok=missing_ok)


def _run_backend(backend: str, host: str, prep: _Prepared, o: _Opts, dry_run: bool = False) -> dict:
    if backend == BACKEND_RAM_RUN:
        storage = o.storage
        if storage is None:
            from c64_test_harness.backends import ultimate64_storage as storage
        kw = dict(sha256=prep.image.sha256, expect_git_hash=prep.embedded, volume=o.volume,
                  lock_timeout=o.lock_timeout, dry_run=dry_run, lock_factory=o.lock_factory,
                  client_factory=o.client_factory, storage=_StageOnly(storage), sleep=o.sleep, clock=o.clock,
                  log=o.log)
        if o.menu_factory is not None:
            kw["menu_factory"] = o.menu_factory
        return o.ram_run(host, prep.copy, **kw)
    if backend == BACKEND_JTAG_COLD:
        # cold=True always: a warm load on the C64 Ultimate runs the flashed app first and leaves stale I-cache
        # lines (false "does not boot"). No caller option reaches this argument.
        return o.jtag_load(host, prep.image.bit, prep.app_bin, bit_sha256=prep.image.bit_sha256,
                           app_sha256=prep.app_sha256, expect_git_hash=prep.embedded, lock_timeout=o.lock_timeout,
                           net_timeout=o.net_timeout, dry_run=dry_run, esp=o.esp, lock_factory=o.lock_factory,
                           client_factory=o.client_factory, log=o.log, cold=True)
    return o.u64_jtag_reload(host, prep.image.jtag_image, sha256=prep.image.jtag_sha256,
                             expect_git_hash=prep.embedded, timeout=o.jtag_timeout, lock_timeout=o.lock_timeout,
                             lock_factory=o.lock_factory, client_factory=o.client_factory, dry_run=dry_run,
                             sleep=o.sleep, clock=o.clock, log=o.log)


def _reports(info: dict | None, embedded: str) -> bool:
    """Whether /v1/info reports exactly the image's own hash (the firmware reports the string its image carries)."""
    return info is not None and str(info.get("git_commit_hash") or "").strip().lower() == embedded


def _hash_known(info: dict | None) -> bool:
    """Whether /v1/info reported a hash at all (7+ hex digits). Firmware that does not report the key gives none."""
    return info is not None and re.fullmatch(r"[0-9a-fA-F]{7,40}", str(info.get("git_commit_hash") or "").strip()) \
        is not None


def _hash_changed(pre: dict | None, embedded: str):
    """True/False when the device reported a hash before the load; None when what ran before is unknown."""
    return (not _reports(pre, embedded)) if _hash_known(pre) else None


def _verify(res: VolatileResult, rec: dict, uid: str, family: str, expect: str, pre: dict | None) -> None:
    """The running firmware must be the bound device, of the image's family, reporting the image's own hash
    (*expect*); then say what is proven about the restart."""
    info = rec.get("info") if isinstance(rec, dict) else None
    if not isinstance(info, dict):
        raise jl.JtagLoadError("verify", "the backend returned no /v1/info after the load")
    res.product, res.git_hash = info.get("product"), info.get("git_commit_hash")
    _check_device(info, uid, family, "after the load")
    if not _reports(info, expect):
        raise jl.JtagLoadError("verify", f"the running firmware reports git hash {res.git_hash!r}, the image's is "
                               f"{expect}")
    res.hash_changed = _hash_changed(pre, expect)
    if res.backend == BACKEND_JTAG_COLD:
        if rec.get("cold") is not True or rec.get("user_id") != f"{jl.EXPECT_USERID:08x}":
            raise jl.JtagLoadError("verify", f"the JTAG load does not record a cold load with the user ID read back "
                                   f"(cold {rec.get('cold')!r}, user_id {rec.get('user_id')!r})")
        res.restart_proof = "jtag-reconfigured"
    elif res.backend == BACKEND_U64_JTAG:
        # The reload saw the box itself, later than this file's own reads: its proof is carried through, and its
        # "ok-unproven" stays unproven. Nothing is derived here from "it was dark when I looked".
        proof = rec.get("restart_proof")
        if rec.get("result") != "ok" or proof not in RELOAD_PROOFS:
            raise jl.JtagLoadError("unproven", f"the JTAG reload came back as {rec.get('result')!r} with restart "
                                   f"proof {proof!r}: the device answers with {expect}, but that it restarted is "
                                   f"not proven ({rec.get('unproven') or 'no proof in the record'}; #570)")
        res.restart_proof = proof
    elif res.hash_changed:
        res.restart_proof = "hash-change"
    else:
        raise jl.JtagLoadError("verify", f"the device did not report another hash than {expect} before the load "
                               f"(it reported {(pre or {}).get('git_commit_hash')!r}), and this backend gives no "
                               "other evidence of a restart (#570)")


def _load(host: str, image: VolatileImage, action: str, o: _Opts, *, skip_if_running: bool = False,
          dry_run: bool = False):
    res = VolatileResult(ok=False, action=action, host=host)
    t0 = o.clock()
    marks = {"start": t0}

    def step(name):
        now = o.clock()
        res.timings[name + "_s"] = round(now - marks["start"], 3)
        marks["start"] = now
    try:
        uid = _uid(host)
        with tempfile.TemporaryDirectory(prefix="volatile_load-") as td:
            prep = prepare_image(image, Path(td))
            res.family, res.expect_git_hash, res.image_git_hash = prep.family, prep.expect, prep.embedded
            check_binding(host, uid, prep.family)
            step("preflight")
            if dry_run:
                answering = _backend_for(prep.family, False, prep, o)
                try:
                    dark = _backend_for(prep.family, True, prep, o)
                except jl.JtagLoadError as e:
                    dark = f"refused: {e}"
                rec = _run_backend(answering, host, prep, o, dry_run=True)
                return {"action": action, "host": host, "uid": uid, "family": prep.family,
                        "image": str(image.path), "sha256": str(image.sha256).lower(), "expect_git_hash": prep.expect,
                        "image_git_hash": prep.embedded,
                        "backend_if_answering": answering, "backend_if_dark": dark, "jtag_timeout": o.jtag_timeout,
                        "backend_dry_run": rec, "result": "dry-run ok"}
            with jl._Locked(host, {}, o.lock_timeout, o.lock_factory):
                step("lock")
                pre = _probe(host, o.client_factory, o.sleep)
                res.dark_before = pre is None
                if pre is not None:
                    res.product, res.git_hash = pre.get("product"), pre.get("git_commit_hash")
                    _check_device(pre, uid, prep.family, "before the load")
                res.backend = _backend_for(prep.family, pre is None, prep, o)
                step("identify")
                if skip_if_running and res.backend == BACKEND_RAM_RUN and _reports(pre, prep.embedded):
                    res.ok, res.hash_changed = True, False
                    res.notes.append(f"already reporting {prep.embedded}: nothing was loaded. A same-hash RAM-run cannot "
                                     "be proven started (#570), so that the running image is this file is not proven")
                    res.timings["total_s"] = round(o.clock() - t0, 3)
                    return res
                if res.backend == BACKEND_RAM_RUN and not _hash_known(pre):
                    raise jl.JtagLoadError("run", f"the device reports no git hash before the load "
                                           f"({pre.get('git_commit_hash')!r}), so a change to {prep.embedded} could "
                                           "not be shown and a RAM-run has no other evidence of a restart (#570); "
                                           "nothing was staged")
                res.record = _run_backend(res.backend, host, prep, o)
                step("load")
                _verify(res, res.record, uid, prep.family, prep.embedded, pre)
                step("verify")
        res.ok = res.loaded = True
        res.timings["total_s"] = round(o.clock() - t0, 3)
        return res
    except Exception as e:
        stage = e.stage if isinstance(e, jl.JtagLoadError) else "backend"
        text = str(e) if isinstance(e, jl.JtagLoadError) else f"[backend] {type(e).__name__}: {e}"
        res.ok, res.loaded, res.error = False, False, text
        res.timings["total_s"] = round(o.clock() - t0, 3)
        raise VolatileLoadError(stage, text, res) from e


def volatile_load(host: str, image: VolatileImage, *, dry_run: bool = False, **options):
    """Load *image* into *host* without writing flash; return a VolatileResult (ok True) or raise
    VolatileLoadError (its ``result`` has ok False). With dry_run: the plan as a dict, nothing touched.

    Options (all keyword): lock_timeout, net_timeout, jtag_timeout (at least MIN_JTAG_TIMEOUT_S), volume, and the
    injected lock_factory, client_factory, esp, storage, menu_factory, ram_run, jtag_load, u64_jtag_reload, sleep,
    clock, log. There is no load-mode option: the C64 Ultimate load is always cold."""
    return _load(host, image, "load", _opts(options), dry_run=dry_run)


def volatile_restore(host: str, *, force: bool = False, dry_run: bool = False, **options):
    """Load the device's bench.toml baseline image, verified by hash. Options as volatile_load.

    On a U64-line device that answers and already reports the baseline hash nothing is loaded (the result says so:
    loaded False, restart_proof "none"); with force=True the load is attempted, and ram_run refuses it as unprovable
    (#570). A C64 Ultimate is always reloaded: its cold JTAG load proves itself."""
    o = _opts(options)
    try:
        image = baseline_image(_uid(host))
    except jl.JtagLoadError as e:
        raise VolatileLoadError(e.stage, str(e), VolatileResult(ok=False, action="restore", host=host,
                                                               error=str(e))) from e
    return _load(host, image, "restore", o, skip_if_running=not force, dry_run=dry_run)


class VolatileSession:
    """What volatile_session yields. ``load``: the result of the image's load. ``baseline_first`` / ``restore``:
    those results, when they ran. ``restore_error``: why the restore failed. ``restore_skipped``: why none was
    started (an interrupt). ``lock_in_process``: False when a verified lock shell holds the DeviceLock. The storage
    helpers, the Remote Menu driver and the bench scripts accept that hold (#582); Ultimate64Client's /Temp hygiene on
    a leak-prone device, flash_firmware and save_config_item_to_flash still need the lock held by this process."""

    def __init__(self, host: str, options: dict):
        self.host, self._options = host, options
        self.baseline_first = self.load = self.restore = self.restore_error = self.restore_skipped = None
        self.lock_in_process = False
        self.loads: list[VolatileResult] = []

    def load_image(self, image: VolatileImage) -> VolatileResult:
        """Load another image inside the same hold (image A, rows, image B, rows, restore)."""
        r = volatile_load(self.host, image, **self._options)
        self.loads.append(r)
        return r


@contextlib.contextmanager
def volatile_session(host: str, image: VolatileImage, *, baseline_first: bool = False, restore: bool = True,
                     **options):
    """Hold the device's DeviceLock across [baseline first,] the load of *image*, the body and the restore.

    The lock is taken in this process (allow_nested, so the backends and the body's harness helpers join it), or
    left to a verified lock shell. baseline_first=True loads the bench.toml baseline before *image*, which proves
    the recovery path while the device still runs something known; if that fails, *image* is never loaded. With
    restore=True (default) the baseline is restored on the way out, also when the load or the body raised: the
    body's exception then propagates and a restore failure is kept in ``restore_error`` (and logged), never raised
    over it. A restore failure after a clean body is raised. A KeyboardInterrupt or SystemExit starts no restore
    (``restore_skipped`` says so): a load of minutes behind a Ctrl-C would invite the second Ctrl-C that cuts a JTAG
    load half-way.

    Both images and the binding are checked before the lock is taken."""
    o = _opts(dict(options))
    uid = _uid(host)
    with tempfile.TemporaryDirectory(prefix="volatile_load-") as td:
        prep = prepare_image(image, Path(td))
        check_binding(host, uid, prep.family)
        if restore or baseline_first:
            base = prepare_image(baseline_image(uid), Path(td), "baseline")
            if base.family != prep.family:
                raise jl.JtagLoadError("baseline", f"the baseline is a {base.family} image and the image to load is "
                                       f"a {prep.family} image")
    s = VolatileSession(host, dict(options))
    with jl._Locked(host, {}, o.lock_timeout, o.lock_factory) as held:
        s.lock_in_process = held.dev is not None
        if baseline_first:
            s.baseline_first = volatile_restore(host, force=True, **options)
        body_exc = None
        try:
            s.load = volatile_load(host, image, **options)
            s.loads.append(s.load)
            yield s
        except Exception as e:
            body_exc = e
            raise
        except BaseException as e:      # KeyboardInterrupt, SystemExit: the operator said stop
            if restore:
                s.restore_skipped = (f"{type(e).__name__}: no restore was started; the device runs what was loaded "
                                     "last, or nothing. Run volatile_restore when ready")
                o.log(f"volatile_session: {s.restore_skipped}")
            restore = False
            raise
        finally:
            if restore:
                try:
                    s.restore = volatile_restore(host, **options)
                except Exception as e:
                    s.restore_error = e
                    o.log(f"volatile_session: the restore to the baseline FAILED: {e}")
                    if body_exc is None:
                        raise


def _as_json(out) -> dict:
    return dataclasses.asdict(out) if dataclasses.is_dataclass(out) else out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("load", "restore"):
        p = sub.add_parser(name)
        p.add_argument("host")
        if name == "load":
            p.add_argument("image")
            p.add_argument("--sha256", required=True)
            p.add_argument("--expect-hash", required=True)
            p.add_argument("--bit", default=None)
            p.add_argument("--bit-sha256", default=None)
            p.add_argument("--jtag-image", default=None)
            p.add_argument("--jtag-sha256", default=None)
            p.add_argument("--baseline-first", action="store_true",
                           help="load the bench.toml baseline first, to prove the way back")
        p.add_argument("--volume", default="USB1")
        p.add_argument("--lock-timeout", type=float, default=600)
        p.add_argument("--net-timeout", type=float, default=180)
        p.add_argument("--jtag-timeout", type=float, default=MIN_JTAG_TIMEOUT_S,
                       help=f"budget of the U64 JTAG reload only (a C64 Ultimate load takes none); under "
                            f"{MIN_JTAG_TIMEOUT_S:.0f} s is refused")
        p.add_argument("--dry-run", action="store_true", help="print the plan; nothing is locked or touched")
        p.add_argument("--record", default=None, help="append a JSON line per action here")
    a = ap.parse_args(argv)
    opts = dict(volume=a.volume, lock_timeout=a.lock_timeout, net_timeout=a.net_timeout, jtag_timeout=a.jtag_timeout)
    outs = []
    try:
        if a.cmd == "load":
            image = VolatileImage(Path(a.image), a.sha256, a.expect_hash, Path(a.bit) if a.bit else None,
                                  a.bit_sha256, Path(a.jtag_image) if a.jtag_image else None, a.jtag_sha256)
            if a.baseline_first and not a.dry_run:      # one DeviceLock across both loads
                with volatile_session(a.host, image, baseline_first=True, restore=False, **opts) as s:
                    outs += [s.baseline_first, s.load]
            else:
                if a.baseline_first:
                    outs.append(volatile_restore(a.host, force=True, dry_run=True, **opts))
                outs.append(volatile_load(a.host, image, dry_run=a.dry_run, **opts))
        else:
            outs.append(volatile_restore(a.host, dry_run=a.dry_run, **opts))
    except Exception as e:          # VolatileLoadError (with its result), JtagLoadError from the option checks
        print(f"FAIL {type(e).__name__}: {e}", file=sys.stderr)
        outs.append(_as_json(getattr(e, "result", None)) or {"action": a.cmd, "host": a.host, "ok": False,
                                                              "error": f"{type(e).__name__}: {e}"})
        rc = 3 if getattr(e, "stage", None) == "unproven" else 2
    else:
        rc = 0
    outs = [_as_json(x) for x in outs]
    if a.record:
        with open(a.record, "a") as f:
            for x in outs:
                f.write(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **x}, default=str) + "\n")
    print(json.dumps(outs, indent=1, default=str))
    return rc


if __name__ == "__main__":
    sys.exit(main())
