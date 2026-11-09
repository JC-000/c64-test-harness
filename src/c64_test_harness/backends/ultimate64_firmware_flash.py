"""Firmware flash orchestration for the Ultimate line and the C64U (issue #561).

:func:`flash_firmware` takes an update image from the host to a verified
running firmware: it validates the image on the host, uploads it over FTP,
starts "Run Update" through the Telnet Remote Menu, tells the operator what
to answer and what to press, waits for REST to come back, and checks the
firmware that answers. It returns a :class:`FlashRecord`; nothing is written
to the host's disk.

**What the harness does not do.** It never answers an updater prompt: the
updater draws on the C64 screen with no network stack running, so nothing
of it is visible remotely (#543, design note F10). It never presses a
button and never cuts power. It never retries Run Update.

**Rules, all checked before the step they guard:**

* **One** ``DeviceLock`` **hold for the whole flash.** The caller holds the
  device's lock for the duration; the call refuses before any traffic when
  this process does not hold it, and checks again before the Run Update
  ENTER. Every building block (FTP storage, the Remote Menu, the REST
  client's non-GET requests) checks it as well.
* **Host-side image validation is mandatory.** The firmware never validates
  a ``.u64`` or ``.ue2``: ``FileTypeUpdate::load_format_0`` reads raw
  ``{load, length, start}`` records into memory and jumps to the start
  address with no check (``filetype_u2p.cc``, read at 7b628eb1 = C64U
  1.1.0, 3a1ff9ff and 7b15448a7; only a ``.cfw`` gets ``load_format_1``'s
  checks, and 1.1.0 has none). So, before anything is uploaded or selected:
  the SHA-256 of the local file must equal the caller's record; the file
  extension (local and on the device) must be the product's update
  extension (``getProductUpdateFileExtension``: ``U64`` for the U64 target,
  ``UE2`` for the u64ii target, ``product.cc``); and the record structure
  must parse as the loader will read it (:func:`inspect_update_image`).
* **Image kind is derived from the image.** On the C64U a ``.ue2`` is
  SILENT only if it contains :data:`SILENT_MARKER` and none of
  :data:`PROMPT_STRINGS` (owner, 2026-11-09). The caller's ``kind`` is
  checked against that and refused on a mismatch. A ``kind="official"``
  image must contain the "Reset Configuration?" prompt, because answering
  NO to it is what keeps REST, FTP and telnet on (design note F7, F9). The
  U64E has no SILENT path: ``update_u64.cc`` always prompts and always ends
  in ``turn_off()``.
* **Zero** ``/Temp`` **by construction.** FTP (``STOR`` writes the target
  itself), telnet, and bodyless REST only: ``GET /v1/info``,
  ``PUT /v1/machine:reboot``, and, on the official 1.1.0 path, the
  per-category ``reset_to_default`` PUTs of :func:`apply_factory_baseline`
  (which refuses the network stores itself). Never
  ``set_config_items_batch``.

**Steps** (``FlashRecord.steps`` lists the ones that finished):

1. ``validate``: the image, the ROMs, the kind, the confirmation. No traffic.
2. ``preflight``: bodyless ``GET /v1/info``: product (picks the extension and
   the instruction), ``unique_id``, the running ``git_commit_hash``. On a
   C64U the JTAG recovery check is logged (warn-only; see
   :data:`RECOVERY_CHECK_NOTE`).
3. ``upload``: reuse the target if it already exists with the same SHA-256;
   otherwise ``storage_put_file(verify=True)``. A dry run uploads nothing.
4. ``run_update``: the operator instruction is logged at WARNING, then
   ``Ultimate64RemoteMenu.run_update(confirm_device_powers_off=True)`` must
   report ``started``. A dry run navigates with ``run_update(dry_run=True)``
   (no ENTER) and stops here.
5. ``wait``: poll bodyless ``GET /v1/info`` until REST answers, up to
   ``wait_up_s``. The instruction is logged again at WARNING first.
6. ``verify``: the same ``unique_id``; ``git_commit_hash`` matches the
   image's record.
7. ``reset_categories`` (official C64U images only, after NO): the harness
   resets the non-network categories with :func:`apply_factory_baseline`.
8. ``wifi_ident`` (only with ``expected_wifi_ident``): Disable, read the
   module IDENT, Enable, wait for Link Up. **On the U64E the Disable and
   the Enable each write the WiFi config store to flash** (``network_esp32.cc``
   ``set_enabled``; #543); on the C64U they switch the radio only.
9. ``roms`` (C64U only, and only when ROMs are supplied): ``storage_put_rom``
   for each, ``reboot()``, wait for REST, verify again.

Evidence grades: everything here is source-read or taken from the
2026-11-09 #543/#562 bench records. No real flash has been driven by this
code; the first one is planned for a flash round with the owner present.
"""

from __future__ import annotations

import hashlib
import logging
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Sequence

from .ultimate64_baseline import BaselineReport, apply_factory_baseline
from .ultimate64_client import Ultimate64Client, Ultimate64Error
from .ultimate64_remote_menu import (
    RunUpdateResult,
    Ultimate64RemoteMenu,
    WifiStatus,
)
from .ultimate64_storage import (
    StoragePutResult,
    _check_path,
    _check_rom,
    storage_get_file,
    storage_list_dir,
    storage_put_file,
    storage_put_rom,
)
from .ultimate64_temp_gc import lock_held_for

__all__ = [
    "FIRMWARE_IMAGE_KINDS",
    "FirmwareFlashError",
    "FirmwareFlashRefusedError",
    "FirmwareFlashStepError",
    "FirmwareFlashTimeoutError",
    "FirmwareFlashVerifyError",
    "FirmwareImage",
    "FirmwareImageError",
    "FlashRecord",
    "PROMPT_STRINGS",
    "RomImage",
    "SILENT_MARKER",
    "UpdateImageInfo",
    "flash_firmware",
    "inspect_update_image",
    "operator_instruction",
]

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------

#: ``"silent"``: a C64U bench updater built with ``SILENT 1``; it asks
#: nothing, resets the configuration and reboots by itself.
#: ``"dev"``: a prompting development image; Reset Configuration is YES.
#: ``"official"``: a prompting official image (C64U 1.1.0); Reset
#: Configuration is NO, and the harness resets the non-network categories.
FIRMWARE_IMAGE_KINDS = ("silent", "dev", "official")

#: The C64U bench updater prints this when it forces the module flash
#: (``update_u64ii.cc``, bench build 7b15448a7).
SILENT_MARKER = b"bench: forcing module flash"

#: The updater prompts a SILENT build compiles out (``update_common.h``
#: ``check_flash_disk``/``reset_config``, ``update_u64ii.cc``/``update_u64.cc``
#: ``do_update``; 7b628eb1, 3a1ff9ff, 7b15448a7).
PROMPT_STRINGS = (
    b"Reformat Flash Disk?",
    b"About to update. Continue?",
    b"Reset Configuration? (Recommended)",
)
_RESET_PROMPT = PROMPT_STRINGS[2]

#: ``/v1/info`` ``product`` -> device family. Product names from
#: ``product.cc`` ``product_name[]`` (7b628eb1 with and without
#: ``COMMODORE``; 3a1ff9ff; 7b15448a7). The bench C64U on 7b15448a7 reports
#: ``Ultimate 64-II``; on official 1.1.0 it reports ``C64 Ultimate``.
_PRODUCT_FAMILY = {
    "Ultimate 64": "u64e",
    "Ultimate 64 Elite": "u64e",
    "C64 Ultimate (MK1)": "u64e",
    "Ultimate 64-II": "c64u",
    "C64 Ultimate": "c64u",
}

#: ``getProductUpdateFileExtension()`` (``product.cc``): ``U64 == 1`` ->
#: ``U64``, ``U64 == 2`` -> ``UE2``. ``FileTypeUpdate::test_type`` compares it
#: with the file's extension.
_FAMILY_EXTENSION = {"u64e": ".u64", "c64u": ".ue2"}

#: ``FINGERPRINT`` of ``load_format_1`` (a ``.cfw`` pre-header).
_CFW_FINGERPRINT = 0x4115ADDE

#: Where both updaters are linked: ``target/u64ii/riscv/update/linker.x``
#: ``memory : ORIGIN = 0x03000000, LENGTH = 0x01000000`` (7b628eb1,
#: 3a1ff9ff, 7b15448a7) and ``target/u64/nios2/updater/linker.x``
#: ``avalon2mem_0 : ORIGIN = 0x03000000, LENGTH = 16777216`` (3a1ff9ff,
#: 7b15448a7). A record or start address outside it would be written over
#: the running application or into nothing.
LOAD_WINDOW = (0x03000000, 0x04000000)

#: Why the recovery check before a C64U flash can only warn. Owner,
#: 2026-11-09: warn-only.
RECOVERY_CHECK_NOTE = (
    "JTAG recovery kit not checked: per docs/design/jtag_recovery.md the only "
    "evidence that the kit can recover this device is an IDCODE read through "
    "the ESP32 adapter (esp_recover.py --idcode-only / --link-test), which is "
    "hardware traffic, and no adapter-to-device binding exists in devices.toml. "
    "A /dev/cu.usbserial-* node proves only that some USB serial device is "
    "plugged in. It cannot be determined offline, so this warning is always "
    "given. The kit covers a bad application or FPGA image with the machine "
    "powered; it does not cover a dead ESP32 module (board dark)."
)

_INFO_TIMEOUT = 5.0

# Indirections the tests replace.
_sleep = time.sleep
_monotonic = time.monotonic


# ---------------------------------------------------------------------------
# Inputs and the record
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FirmwareImage:
    """The image to flash, and what it must turn into.

    :param local_path: the update file on the host (``.ue2`` for a C64U,
        ``.u64`` for a U64E).
    :param device_path: where it goes on the device, e.g.
        ``/SD/updates/update.ue2``. Not ``/Temp`` or ``/Flash``.
    :param sha256: the SHA-256 the caller recorded for this image (hex).
    :param expected_git_commit_hash: what ``/v1/info`` ``git_commit_hash``
        must report afterwards; at least 7 hex digits. A prefix match either
        way is accepted (the firmware reports 8).
    :param kind: one of :data:`FIRMWARE_IMAGE_KINDS`. Checked against the
        image.
    :param expected_wifi_ident: the module IDENT expected afterwards, e.g.
        ``ESP32 WiFi Bridge V1.494``, as the WiFi row shows it while
        Disabled. ``None`` skips the check, and with it the Disable/Enable
        cycle (which writes flash on the U64E).
    """

    local_path: Path
    device_path: str
    sha256: str
    expected_git_commit_hash: str
    kind: str
    expected_wifi_ident: str | None = None


@dataclass(frozen=True)
class RomImage:
    """A ROM to install after a C64U flash (``storage_put_rom``).

    Only ever what the caller supplies; the harness never makes one up.
    ``kind``, ``name``, ``overwrite``, ``replace_stock`` and
    ``check_signature`` are passed through; ``sha256`` is checked on the
    host before anything is sent.
    """

    kind: str
    name: str
    local_path: Path
    sha256: str
    overwrite: bool = False
    replace_stock: bool = False
    check_signature: bool = True


@dataclass(frozen=True)
class UpdateImageInfo:
    """What :func:`inspect_update_image` read from an image."""

    size: int
    sha256: str
    #: ``(load, length, start)`` per record, in file order.
    records: tuple[tuple[int, int, int], ...]
    #: Where the loader jumps: the last record's start address.
    start: int
    #: Trailing bytes after the last record (hex2bin's padding, 0-3).
    padding: int
    has_silent_marker: bool
    prompts_present: tuple[str, ...]


@dataclass
class FlashRecord:
    """What a :func:`flash_firmware` call did, step by step.

    Returned on success; carried by every :class:`FirmwareFlashError` as
    ``record`` so the operator can see which step failed (``failed_step``)
    and what had already happened (``steps``). Nothing is written to disk.
    """

    host: str
    dry_run: bool
    image_local_path: str
    image_device_path: str
    image_kind: str
    expected_git_commit_hash: str
    #: Steps that finished, in order.
    steps: list[str] = field(default_factory=list)
    #: The step that raised, or ``None``.
    failed_step: str | None = None
    image: UpdateImageInfo | None = None
    #: ``True`` when the image is a SILENT updater (C64U only).
    image_silent: bool | None = None
    product: str | None = None
    #: ``"c64u"`` or ``"u64e"``.
    family: str | None = None
    unique_id_before: str | None = None
    firmware_version_before: str | None = None
    git_commit_hash_before: str | None = None
    recovery_check: str | None = None
    #: The device path actually used (the device's spelling when reused).
    device_path_used: str | None = None
    upload_reused: bool | None = None
    upload: StoragePutResult | None = None
    #: The answer the operator is told to give to "Reset Configuration?".
    reset_configuration_answer: str | None = None
    operator_instruction: str | None = None
    run_update: RunUpdateResult | None = None
    #: Seconds from the Run Update result to REST answering.
    wait_seconds: float | None = None
    unique_id_after: str | None = None
    firmware_version_after: str | None = None
    git_commit_hash_after: str | None = None
    hash_verified: bool = False
    baseline: BaselineReport | None = None
    wifi_before_check: WifiStatus | None = None
    wifi_ident: str | None = None
    wifi_ident_verified: bool | None = None
    #: The WiFi row after the Enable and the Link Up wait.
    wifi_after: WifiStatus | None = None
    roms: list[StoragePutResult] = field(default_factory=list)
    rebooted: bool = False
    git_commit_hash_after_reboot: str | None = None
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class FirmwareFlashError(Ultimate64Error):
    """A firmware flash was refused or failed. ``record`` is the partial
    :class:`FlashRecord`; ``record.failed_step`` names the step."""

    def __init__(self, message: str, record: FlashRecord) -> None:
        super().__init__(message)
        self.record = record

    @property
    def step(self) -> str | None:
        return self.record.failed_step


class FirmwareImageError(FirmwareFlashError):
    """The image failed host-side validation (hash, extension, records,
    kind). Raised before it is uploaded or selected."""


class FirmwareFlashRefusedError(FirmwareFlashError):
    """A precondition failed (lock, confirmation, product, ROMs on a U64E)."""


class FirmwareFlashTimeoutError(FirmwareFlashError):
    """REST (or the WiFi link) did not come back within the limit."""


class FirmwareFlashVerifyError(FirmwareFlashError):
    """The device came back with the wrong identity, hash or module IDENT."""


class FirmwareFlashStepError(FirmwareFlashError):
    """A building block raised during a step; the original is ``__cause__``."""


# ---------------------------------------------------------------------------
# The image
# ---------------------------------------------------------------------------

def _in_window(addr: int, length: int = 0) -> bool:
    lo, hi = LOAD_WINDOW
    return lo <= addr < hi and length <= hi - addr


def inspect_update_image(data: bytes) -> UpdateImageInfo:
    """Parse a format-0 update image as the application's loader reads it.

    ``load_format_0`` (``filetype_u2p.cc``) reads a 12-byte header
    ``{load, length, start}`` (little-endian ``uint32``, as ``hex2bin -r``
    writes them on the host), then ``length`` bytes to ``load``, until the
    file ends. It does **not** skip ``hex2bin``'s padding to 4 bytes, and
    1.1.0 jumps to the *last header read*'s start (3a1ff9ff to the last
    non-zero one). So an image is accepted only if:

    * it does not start with the ``.cfw`` fingerprint (it would be loaded as
      records at ``0x4115ADDE``);
    * every record is complete, and every record but the last has a length
      that is a multiple of 4 (the loader would read padding as a header);
    * only the last record's padding follows it (0-3 bytes, exactly what
      ``hex2bin`` writes): more would be read as a partial header and, at
      1.1.0, could overwrite the start address;
    * every non-empty record, and the last record's start address, lies in
      :data:`LOAD_WINDOW`, and the start address is not 0.

    Also reports the SILENT marker and which prompt strings the image holds.
    Raises ``ValueError`` naming the first problem.
    """
    size = len(data)
    if size < 12:
        raise ValueError(f"{size} bytes: shorter than one record header")
    if struct.unpack_from("<I", data, 0)[0] == _CFW_FINGERPRINT:
        raise ValueError("starts with the .cfw fingerprint (format 1); a .u64/.ue2 "
                         "is loaded as format 0 and would be misread")
    records: list[tuple[int, int, int]] = []
    pos = 0
    padding = 0
    while True:
        load, length, start = struct.unpack_from("<III", data, pos)
        idx = len(records)
        pos += 12
        if length and not _in_window(load, length):
            raise ValueError(
                f"record {idx}: {length} bytes at 0x{load:08x} fall outside the "
                f"updater's load window 0x{LOAD_WINDOW[0]:08x}-0x{LOAD_WINDOW[1]:08x}")
        if length > size - pos:
            raise ValueError(f"record {idx}: {length} bytes declared, "
                             f"{size - pos} left in the file (truncated)")
        records.append((load, length, start))
        pos += length
        left = size - pos
        if left == 0:
            break
        pad = (-length) % 4
        if left < 12:
            if left != pad:
                raise ValueError(
                    f"{left} trailing bytes after record {idx}; only its padding "
                    f"({pad}) may follow the last record")
            padding = left
            break
        if pad:
            raise ValueError(
                f"record {idx}: length {length} is not a multiple of 4 and another "
                "record follows; the firmware loader does not skip padding")
    last_start = records[-1][2]
    if last_start == 0:
        raise ValueError("the last record carries no start address (1.1.0 would jump to 0)")
    if not _in_window(last_start):
        raise ValueError(f"start address 0x{last_start:08x} is outside the load window")
    return UpdateImageInfo(
        size=size,
        sha256=hashlib.sha256(data).hexdigest(),
        records=tuple(records),
        start=last_start,
        padding=padding,
        has_silent_marker=SILENT_MARKER in data,
        prompts_present=tuple(p.decode() for p in PROMPT_STRINGS if p in data),
    )


def _norm_hex(value: str, what: str, *, min_len: int, max_len: int) -> str:
    v = value.strip().lower() if isinstance(value, str) else ""
    if not (min_len <= len(v) <= max_len) or any(c not in "0123456789abcdef" for c in v):
        raise ValueError(f"{what} must be {min_len}-{max_len} hex digits, not {value!r}")
    return v


def _hash_matches(expected: str, reported: Any) -> bool:
    if not isinstance(reported, str):
        return False
    r = reported.strip().lower()
    if len(r) < 7:
        return False
    return r.startswith(expected) or expected.startswith(r)


def operator_instruction(family: str, kind: str) -> str:
    """The exact instruction for the person at the device.

    The prompts appear on the C64 screen only; the harness cannot see or
    answer them. "Reformat Flash Disk?" is asked only when the flash disk
    already has files (``check_flash_disk``, ``FR_DIR_NOT_EMPTY``); YES
    follows the bench rule that every flash formats flash and resets to
    defaults, and the drive ROMs are rewritten by the updater after it.
    """
    if family == "c64u" and kind == "silent":
        return (
            "C64U SILENT image: the updater asks nothing and resets the configuration. "
            "By source (bench updater 7b15448a7, update_common.h turn_off with SILENT) "
            "it then has the module switch the machine off and on again (off 2 s), with "
            "no button press; REST came back about 113 s after the start on 2026-11-09, "
            "and whether that return involved a button press is not established. Do not "
            "press any key or button. If the machine stays dark, report it before "
            "pressing anything. Any popup on the C64 screen is unexpected: do not answer "
            "it; report it.")
    reset = "YES" if kind == "dev" else "NO"
    lines = [
        f"{'C64U' if family == 'c64u' else 'U64E'} {kind} image: answer the updater "
        "prompts on the C64 screen exactly as follows:",
        "  'Reformat Flash Disk?' (only asked if the flash disk has files): YES",
        "  'About to update. Continue?': YES",
        f"  'Reset Configuration? (Recommended)': {reset}",
        "  an ESP32 'Flashing ESP32 ...' popup, if one waits for OK: OK",
        "  any other prompt: do not answer it; report it.",
    ]
    if reset == "NO":
        lines.append("  (NO keeps REST, FTP and telnet on; the harness then resets the "
                     "non-network categories itself.)")
    lines.append(
        "When the screen says 'Turning OFF machine in 5 seconds', the machine switches "
        "itself off. Then press the power button once to switch it back on.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

class _Run:
    """One flash: the record, the current step, and error wrapping."""

    def __init__(self, record: FlashRecord) -> None:
        self.record = record
        self.step = "validate"

    def begin(self, step: str) -> None:
        self.step = step

    def done(self) -> None:
        self.record.steps.append(self.step)

    def fail(self, cls: type[FirmwareFlashError], message: str) -> FirmwareFlashError:
        self.record.failed_step = self.step
        return cls(f"{self.step}: {message}", self.record)

    def warn(self, message: str) -> None:
        self.record.warnings.append(message)
        _log.warning("flash %s: %s", self.record.host, message)


def _read_bytes(path: Path) -> bytes:
    return Path(path).read_bytes()


def _extension(path: str) -> str:
    return PurePosixPath(path).suffix.lower()


def _info(client: Ultimate64Client) -> dict:
    info = client.get_info()
    if not isinstance(info, dict):
        raise Ultimate64Error(f"/v1/info answered {type(info).__name__}, not an object")
    return info


def _wait_for_rest(client: Ultimate64Client, deadline_s: float, poll_s: float,
                   run: _Run, what: str) -> tuple[dict, float]:
    """Poll bodyless ``GET /v1/info`` until it answers. Returns (info, seconds)."""
    t0 = _monotonic()
    last_log = t0
    last_err: Exception | None = None
    while True:
        try:
            return _info(client), _monotonic() - t0
        except (Ultimate64Error, OSError) as exc:  # down is the expected state
            last_err = exc
        now = _monotonic()
        if now - t0 >= deadline_s:
            raise run.fail(FirmwareFlashTimeoutError,
                           f"REST on {run.record.host} did not answer within {deadline_s:.0f} s "
                           f"{what}; last error: {last_err}")
        if now - last_log >= 60.0:
            last_log = now
            _log.info("flash %s: waiting for REST %s, %.0f s so far",
                      run.record.host, what, now - t0)
        _sleep(poll_s)


def _verify_identity(info: dict, run: _Run, expected_hash: str) -> str:
    rec = run.record
    uid = info.get("unique_id")
    if rec.unique_id_before is not None and uid != rec.unique_id_before:
        raise run.fail(FirmwareFlashVerifyError,
                       f"a different device answers at {rec.host}: unique_id {uid!r}, "
                       f"was {rec.unique_id_before!r}")
    got = info.get("git_commit_hash")
    if not _hash_matches(expected_hash, got):
        raise run.fail(FirmwareFlashVerifyError,
                       f"git_commit_hash is {got!r}, expected {expected_hash!r}; "
                       f"before the flash it was {rec.git_commit_hash_before!r}")
    return got


def _settled_wifi(menu: Ultimate64RemoteMenu, timeout: float, poll: float) -> WifiStatus:
    """The WiFi row once it is in a state its menu can act on."""
    deadline = _monotonic() + timeout
    while True:
        st = menu.wifi_status()
        if st.state in ("link-up", "link-down", "disabled", "no-module") or _monotonic() >= deadline:
            return st
        _sleep(poll)


def _check_wifi_ident(menu: Ultimate64RemoteMenu, expected: str, run: _Run,
                      link_timeout: float, poll: float) -> None:
    rec = run.record
    st = _settled_wifi(menu, link_timeout, poll)
    rec.wifi_before_check = st
    if st.state == "disabled":
        # Already off: read it, and leave it as found.
        rec.wifi_ident = st.ident
        rec.wifi_after = st
    elif st.state in ("link-up", "link-down"):
        off = menu.set_wifi_enabled(False)
        rec.wifi_after = off.after
        try:
            ident = off.after.ident if off.after is not None else None
            if ident is None:
                ident = menu.wifi_status().ident
            rec.wifi_ident = ident
        finally:
            # Put back what this step changed, whatever the read did.
            try:
                menu.set_wifi_enabled(True)
            except Exception:
                run.warn("WiFi was Disabled for the IDENT read and the Enable failed: "
                         "it is left Disabled")
                raise
        deadline = _monotonic() + link_timeout
        while True:
            now_st = menu.wifi_status()
            rec.wifi_after = now_st
            if now_st.state == "link-up":
                break
            if _monotonic() >= deadline:
                raise run.fail(FirmwareFlashTimeoutError,
                               f"WiFi enabled again but not Link Up within {link_timeout:.0f} s "
                               f"(row {now_st.row!r}); module IDENT read {ident!r}")
            _sleep(poll)
    else:
        raise run.fail(FirmwareFlashVerifyError,
                       f"WiFi row is {st.state!r} ({st.row!r}); the module IDENT cannot be read")
    rec.wifi_ident_verified = rec.wifi_ident == expected
    if not rec.wifi_ident_verified:
        raise run.fail(FirmwareFlashVerifyError,
                       f"WiFi module IDENT is {rec.wifi_ident!r}, expected {expected!r}")


def flash_firmware(
    host: str,
    image: FirmwareImage,
    *,
    confirm_device_powers_off: bool = False,
    roms: Sequence[RomImage] = (),
    dry_run: bool = False,
    wait_up_s: float = 900.0,
    poll_interval_s: float = 5.0,
    wifi_link_timeout_s: float = 90.0,
    client_factory: Callable[[str], Ultimate64Client] | None = None,
    menu_factory: Callable[[str], Ultimate64RemoteMenu] | None = None,
) -> FlashRecord:
    """Flash *image* onto the device at *host*, and verify what comes back.

    See the module docstring for the steps and the rules. The caller must
    hold the device's ``DeviceLock`` for the whole call.

    :param confirm_device_powers_off: required for a real run (not for a dry
        run), and passed to ``run_update``. Every updater ends by switching
        the machine off; a U64E, and a C64U on a prompting image, then needs
        its power button pressed.
    :param roms: C64U only. Installed with ``storage_put_rom`` after the
        flash is verified, followed by ``reboot()`` and a second verify.
    :param dry_run: validation, preflight and navigation only: no upload, no
        ENTER, no config, WiFi or ROM change. Navigation needs the image to
        be on the device already; if it is not, it is skipped with a warning.
    :param wait_up_s: how long to wait for REST after Run Update started.
        The U64E took 684 s on 2026-11-09, including the button press.
    :param wifi_link_timeout_s: how long the WiFi row may take to settle
        and, after the Enable, to reach Link Up (#562: it reads Link Down
        first).
    :param client_factory: builds the REST client; for tests.
    :param menu_factory: builds the Remote Menu driver; for tests.
    :raises FirmwareFlashError: (a subclass) carrying the partial record.
    """
    record = FlashRecord(
        host=host, dry_run=dry_run, image_local_path=str(image.local_path),
        image_device_path=image.device_path, image_kind=image.kind,
        expected_git_commit_hash=str(image.expected_git_commit_hash),
    )
    run = _Run(record)
    make_client = client_factory or (lambda h: Ultimate64Client(h, timeout=_INFO_TIMEOUT))
    make_menu = menu_factory or Ultimate64RemoteMenu
    v = _validate(image, roms, dry_run, confirm_device_powers_off, run)
    clients: list[Ultimate64Client] = []
    try:
        return _flash(host, image, roms, v, dry_run, confirm_device_powers_off, wait_up_s,
                      poll_interval_s, wifi_link_timeout_s, make_client, make_menu, run,
                      clients)
    finally:
        if client_factory is None:
            for c in clients:
                try:
                    c.close()
                except Exception as exc:  # pragma: no cover - close is best effort
                    _log.warning("flash %s: client close failed: %s", host, exc)


@dataclass(frozen=True)
class _Validated:
    data: bytes
    info: UpdateImageInfo
    sha: str
    git_hash: str
    rom_payloads: tuple[bytes, ...]


def _validate(image: FirmwareImage, roms: Sequence[RomImage], dry_run: bool,
              confirm: bool, run: _Run) -> _Validated:
    """Step 1: every check that needs no traffic. The lock comes first."""
    host = run.record.host
    if not lock_held_for(host):
        raise run.fail(FirmwareFlashRefusedError,
                       f"this process does not hold {host}'s DeviceLock; hold it for the "
                       "whole flash")
    if not dry_run and not confirm:
        raise run.fail(FirmwareFlashRefusedError,
                       "every updater ends by switching the machine off; pass "
                       "confirm_device_powers_off=True (or dry_run=True)")
    if image.kind not in FIRMWARE_IMAGE_KINDS:
        raise run.fail(FirmwareImageError,
                       f"kind {image.kind!r} is not one of {FIRMWARE_IMAGE_KINDS}")
    try:
        want_sha = _norm_hex(image.sha256, "sha256", min_len=64, max_len=64)
        want_hash = _norm_hex(image.expected_git_commit_hash, "expected_git_commit_hash",
                              min_len=7, max_len=40)
        _check_path(image.device_path, write=True)
    except ValueError as exc:
        raise run.fail(FirmwareImageError, str(exc)) from exc
    if _extension(str(image.local_path)) != _extension(image.device_path):
        raise run.fail(FirmwareImageError,
                       f"local {image.local_path} and device {image.device_path} have "
                       "different extensions")
    if _extension(image.device_path) not in _FAMILY_EXTENSION.values():
        raise run.fail(FirmwareImageError,
                       f"{image.device_path}: not an update image extension "
                       f"({sorted(_FAMILY_EXTENSION.values())})")
    try:
        data = _read_bytes(image.local_path)
    except OSError as exc:
        raise run.fail(FirmwareImageError, f"cannot read {image.local_path}: {exc}") from exc
    got_sha = hashlib.sha256(data).hexdigest()
    if got_sha != want_sha:
        raise run.fail(FirmwareImageError,
                       f"{image.local_path} has SHA-256 {got_sha}, the record says {want_sha}")
    try:
        info_img = inspect_update_image(data)
    except ValueError as exc:
        raise run.fail(FirmwareImageError, f"{image.local_path}: {exc}") from exc
    run.record.image = info_img
    if image.kind == "official" and _RESET_PROMPT.decode() not in info_img.prompts_present:
        raise run.fail(FirmwareImageError,
                       "kind='official' relies on answering NO to 'Reset Configuration?', "
                       "but the image does not contain that prompt")
    payloads: list[bytes] = []
    for rom in roms:
        try:
            payload = _read_bytes(rom.local_path)
            rsha = _norm_hex(rom.sha256, f"ROM {rom.name} sha256", min_len=64, max_len=64)
            if hashlib.sha256(payload).hexdigest() != rsha:
                raise ValueError(f"ROM {rom.local_path} does not match its SHA-256 record")
            _check_rom(rom.kind, rom.name, payload, check_signature=rom.check_signature)
        except (OSError, ValueError, TypeError) as exc:
            raise run.fail(FirmwareImageError, f"ROM {rom.name!r}: {exc}") from exc
        payloads.append(payload)
    run.done()
    return _Validated(data, info_img, want_sha, want_hash, tuple(payloads))


def _flash(host: str, image: FirmwareImage, roms: Sequence[RomImage], v: _Validated,
           dry_run: bool, confirm: bool, wait_up_s: float, poll_interval_s: float,
           wifi_link_timeout_s: float, make_client: Callable[[str], Ultimate64Client],
           make_menu: Callable[[str], Ultimate64RemoteMenu], run: _Run,
           clients: list[Ultimate64Client]) -> FlashRecord:
    record = run.record
    data, info_img, want_sha, want_hash = v.data, v.info, v.sha, v.git_hash
    rom_payloads = v.rom_payloads
    confirm_device_powers_off = confirm
    # -- 2. preflight ------------------------------------------------------------
    run.begin("preflight")
    try:
        client = make_client(host)
        clients.append(client)
        menu = make_menu(host)
        info = _info(client)
    except Exception as exc:
        raise run.fail(FirmwareFlashStepError, f"{type(exc).__name__}: {exc}") from exc
    record.product = info.get("product")
    record.unique_id_before = info.get("unique_id")
    record.firmware_version_before = info.get("firmware_version")
    record.git_commit_hash_before = info.get("git_commit_hash")
    family = _PRODUCT_FAMILY.get(record.product)  # type: ignore[arg-type]
    if family is None:
        raise run.fail(FirmwareFlashRefusedError,
                       f"unknown product {record.product!r}; known: {sorted(_PRODUCT_FAMILY)}")
    record.family = family
    ext = _FAMILY_EXTENSION[family]
    if _extension(image.device_path) != ext:
        raise run.fail(FirmwareImageError,
                       f"{record.product} takes {ext} update images, not "
                       f"{_extension(image.device_path)}")
    # The SILENT gate (owner, 2026-11-09): the marker, and none of the prompts.
    # An image with both is not one the gate knows, so it is refused outright.
    if info_img.has_silent_marker and info_img.prompts_present:
        raise run.fail(FirmwareImageError,
                       "the image has the SILENT bench marker and prompt strings "
                       f"{list(info_img.prompts_present)}; not a recognised image")
    if family == "u64e" and info_img.has_silent_marker:
        raise run.fail(FirmwareImageError, "a SILENT marker in a .u64: the U64E has no SILENT path")
    silent = info_img.has_silent_marker      # prompts and the U64E are refused above
    record.image_silent = silent
    if silent != (image.kind == "silent"):
        raise run.fail(FirmwareImageError,
                       f"kind={image.kind!r}, but the image {'is' if silent else 'is not'} a "
                       "SILENT updater (marker 'bench: forcing module flash' and no prompts)")
    if family == "u64e" and image.kind == "official":
        raise run.fail(FirmwareFlashRefusedError,
                       "kind='official' on a U64E: the Reset Configuration answer for an "
                       "official Ultimate-line image is not decided (owner)")
    if roms and family != "c64u":
        raise run.fail(FirmwareFlashRefusedError, "ROM install after a flash is C64U-only")
    if record.git_commit_hash_before and _hash_matches(want_hash, record.git_commit_hash_before):
        run.warn(f"the device already reports {record.git_commit_hash_before}; the hash "
                 "check after the flash cannot tell a flash from none")
    if family == "c64u":
        record.recovery_check = RECOVERY_CHECK_NOTE
        run.warn(RECOVERY_CHECK_NOTE)
    record.reset_configuration_answer = (
        "automatic (SILENT: always resets)" if silent else ("YES" if image.kind == "dev" else "NO"))
    record.operator_instruction = operator_instruction(family, image.kind)
    run.done()

    # -- 3. upload ---------------------------------------------------------------
    run.begin("upload")
    try:
        target = _find_existing(host, image.device_path, info_img)
        if target is not None:
            record.upload_reused = True
            record.device_path_used = target
            _log.info("flash %s: %s already holds this image; reusing it", host, target)
        elif dry_run:
            record.upload_reused = False
            record.device_path_used = None
        else:
            put = storage_put_file(host, image.device_path, data, verify=True)
            if not put.verified or put.sha256 != want_sha:
                raise run.fail(FirmwareFlashVerifyError,
                               f"upload of {image.device_path} not verified ({put})")
            record.upload = put
            record.upload_reused = False
            record.device_path_used = put.path
    except FirmwareFlashError:
        raise
    except Exception as exc:
        raise run.fail(FirmwareFlashStepError, f"{type(exc).__name__}: {exc}") from exc
    run.done()

    # -- 4. run_update -----------------------------------------------------------
    run.begin("run_update")
    if dry_run:
        _log.info("flash %s (dry run): the operator would be told:\n%s",
                  host, record.operator_instruction)
        if record.device_path_used is None:
            run.warn(f"dry run: {image.device_path} is not on the device, so navigation to "
                     "it was skipped (a real run uploads it first)")
            return record
        try:
            record.run_update = menu.run_update(record.device_path_used, dry_run=True)
        except Exception as exc:
            raise run.fail(FirmwareFlashStepError, f"{type(exc).__name__}: {exc}") from exc
        run.done()
        return record
    if not lock_held_for(host):
        raise run.fail(FirmwareFlashRefusedError, f"{host}'s DeviceLock is no longer held")
    _log.warning("flash %s: OPERATOR, Run Update is about to start on %s.\n%s",
                 host, record.device_path_used, record.operator_instruction)
    try:
        result = menu.run_update(record.device_path_used,
                                 confirm_device_powers_off=confirm_device_powers_off)
    except Exception as exc:
        raise run.fail(FirmwareFlashStepError, f"{type(exc).__name__}: {exc}") from exc
    record.run_update = result
    if not result.started:
        raise run.fail(FirmwareFlashVerifyError,
                       f"run_update returned without the updater starting: {result}")
    run.done()

    # -- 5. wait -----------------------------------------------------------------
    run.begin("wait")
    _log.warning("flash %s: OPERATOR, the updater is running. Waiting up to %.0f s for "
                 "REST.\n%s", host, wait_up_s, record.operator_instruction)
    info, waited = _wait_for_rest(client, wait_up_s, poll_interval_s, run,
                                  "after Run Update")
    record.wait_seconds = waited
    run.done()

    # -- 6. verify ---------------------------------------------------------------
    run.begin("verify")
    record.unique_id_after = info.get("unique_id")
    record.firmware_version_after = info.get("firmware_version")
    record.git_commit_hash_after = _verify_identity(info, run, want_hash)
    record.hash_verified = True
    run.done()

    # -- 7. reset_categories (official C64U, after NO) ------------------------
    if family == "c64u" and image.kind == "official":
        run.begin("reset_categories")
        try:
            record.baseline = apply_factory_baseline(client)
        except Exception as exc:
            raise run.fail(FirmwareFlashStepError, f"{type(exc).__name__}: {exc}") from exc
        run.done()

    # -- 8. wifi_ident -------------------------------------------------------------
    if image.expected_wifi_ident is not None:
        run.begin("wifi_ident")
        try:
            _check_wifi_ident(menu, image.expected_wifi_ident, run,
                              wifi_link_timeout_s, poll_interval_s)
        except FirmwareFlashError:
            raise
        except Exception as exc:
            raise run.fail(FirmwareFlashStepError, f"{type(exc).__name__}: {exc}") from exc
        run.done()

    # -- 9. roms -------------------------------------------------------------------
    if roms:
        run.begin("roms")
        try:
            for rom, payload in zip(roms, rom_payloads):
                record.roms.append(storage_put_rom(
                    host, rom.kind, rom.name, payload, overwrite=rom.overwrite,
                    replace_stock=rom.replace_stock, check_signature=rom.check_signature))
            client.reboot()
            record.rebooted = True
        except Exception as exc:
            raise run.fail(FirmwareFlashStepError, f"{type(exc).__name__}: {exc}") from exc
        info, _ = _wait_for_rest(client, wait_up_s, poll_interval_s, run, "after reboot()")
        record.git_commit_hash_after_reboot = _verify_identity(info, run, want_hash)
        run.done()
    return record


def _find_existing(host: str, device_path: str, img: UpdateImageInfo) -> str | None:
    """The device's path for *device_path* if it already holds this image.

    Lists the parent (any case of the name), compares the size, and only
    then reads the file and compares its SHA-256. A file of the same name
    with other content raises ``FileExistsError``: it is never overwritten.
    """
    parent, _, name = device_path.rpartition("/")
    try:
        entries = storage_list_dir(host, parent)
    except FileNotFoundError:
        return None
    hits = [e for e in entries if e.name.casefold() == name.casefold()]
    if not hits:
        return None
    entry = hits[0]
    found = f"{parent}/{entry.name}"
    if entry.kind != "file":
        raise IsADirectoryError(f"{found} on {host} is a directory")
    if entry.size == img.size:
        got = hashlib.sha256(storage_get_file(host, found)).hexdigest()
        if got == img.sha256:
            return found
    raise FileExistsError(
        f"{found} on {host} exists with other content; it is not overwritten. "
        "Delete it or choose another device_path.")
