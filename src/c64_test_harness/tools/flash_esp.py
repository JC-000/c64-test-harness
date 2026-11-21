"""Unattended ESP32-module flash of an Ultimate 64 Elite over its telnet menu.

    python -m c64_test_harness.tools.flash_esp --host <ETHERNET address> \\
        --esp raw_u64.esp --platform u64e --esp-image-tool /path/to/esp_image.py \\
        --dest /USB1/espflash/raw_u64.esp --expect-ident "<ident>" \\
        --wifi-ip <module Wi-Fi address> --confirm-module-flash

Steps: run ``esp_image.py check --platform P file`` (an external tool, given
by ``--esp-image-tool`` or the ``ESP_IMAGE_TOOL`` environment variable; the
harness does not carry a copy), take the device's ``DeviceLock``, upload to
the ``--dest`` path (a writable volume, e.g. ``/USB1/espflash/x.esp``; never
``/Temp`` or ``/Flash``; an existing file is refused), read the upload back and compare bytes,
read the module ident with ``read_wifi_ident`` (unless ``--no-ident-probe``), run
``Ultimate64RemoteMenu.flash_esp`` (which verifies Wi-Fi, ping and ident afterwards), delete the
upload (and its directory if empty), print one result line.
Exit 0 only when the whole verdict is OK and proven (the ident changed), or, with
``--allow-same-ident``, OK with a ping reply; 3 when the
flash verified but is unproven (no ident change, e.g. a same-image flash); 1 on a failed or unverified flash
(the menu's ENTER was sent); 2 on a refusal before it was; 2 also when the DeviceLock
budget runs out or cannot be read (``--lock-timeout nan``, a malformed
``U64_DEVICE_LOCK_TIMEOUT``): nothing was touched; 3 is reserved for "flashed, verified, unproven". The
upload is deleted after a normal return, a failure popup, a verification failure or a refusal
before the ENTER; after anything else (an interrupt, no popup, an unexpected popup, a session or
lock lost after the flash) it and its directory are KEPT and the path printed (the firmware may
still be reading the file).

``--host`` must be the Ethernet address: the flash stops Wi-Fi.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from pathlib import Path

from ..backends.ultimate64_remote_menu import system_ping  # noqa: F401  (re-exported)


def check_image(tool: Path, platform_name: str, image: Path) -> tuple[bool, str]:
    """Run ``esp_image.py check``; return (ok, its output)."""
    r = subprocess.run([sys.executable, str(tool), "check", "--platform", platform_name,
                        str(image)], capture_output=True, text=True, timeout=60)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def main(argv: list[str] | None = None, *, menu_factory=None, ping=None,
         put=None, delete=None, rmdir=None, get=None, lock_factory=None, checker=None,
         ident_probe=None, ident_poll=5.0) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", required=True, help="the device's ETHERNET address")
    ap.add_argument("--esp", type=Path, required=True)
    ap.add_argument("--platform", choices=("u64e",), required=True)
    ap.add_argument("--esp-image-tool", type=Path,
                    default=Path(os.environ["ESP_IMAGE_TOOL"]) if os.environ.get("ESP_IMAGE_TOOL")
                    else None, help="path of esp_image.py (or env ESP_IMAGE_TOOL)")
    ap.add_argument("--expect-ident")
    ap.add_argument("--wifi-ip", help="the module's Wi-Fi address (pinged afterwards)")
    ap.add_argument("--dest", required=True,
                    help="device path of the upload, on a writable volume, e.g. "
                         "/USB1/espflash/raw_u64.esp (never /Temp or /Flash)")
    ap.add_argument("--lock-timeout", type=float, default=None,
                    help="DeviceLock wait in seconds (default: U64_DEVICE_LOCK_TIMEOUT, else the "
                         "manager default, 60 s)")
    ap.add_argument("--timeout", type=float, default=300.0, help="wait for the result popup")
    ap.add_argument("--verify-timeout", type=float, default=90.0)
    ap.add_argument("--no-ident-probe", action="store_true",
                    help="do not toggle Wi-Fi to read the ident before/after (result is then unproven)")
    ap.add_argument("--allow-same-ident", action="store_true",
                    help="a flash that leaves the ident unchanged exits 0 instead of 3 (unproven)")
    ap.add_argument("--no-ping", action="store_true",
                    help="skip the ping of the Wi-Fi address; the run is then UNPROVEN (exit 3)")
    ap.add_argument("--confirm-module-flash", action="store_true")
    a = ap.parse_args(argv)

    from ..backends.device_lock import (
        DeviceLock, DeviceLockTimeout, resolve_lock_timeout)
    from ..backends.unified_manager import DEFAULT_LOCK_TIMEOUT
    from ..backends.ultimate64_client import Ultimate64UnsafeOperationError
    from ..backends.ultimate64_remote_menu import (
        ESP_IDENT_MAX, EspFlashError, EspFlashFailedError, EspFlashVerifyError, Ultimate64RemoteMenu,
        Ultimate64RemoteMenuError)
    from ..backends.ultimate64_storage import (
        Ultimate64StorageError, storage_delete_file, storage_get_file, storage_put_file, storage_rmdir)

    if not a.confirm_module_flash:
        print("REFUSED: pass --confirm-module-flash", file=sys.stderr)
        return 2
    if a.wifi_ip and a.host == a.wifi_ip:
        print(f"REFUSED: {a.host} is the Wi-Fi address; use the Ethernet address",
              file=sys.stderr)
        return 2
    if a.esp.suffix.upper() != ".ESP" or not a.esp.is_file():
        print("REFUSED: --esp must be an existing file ending in .esp", file=sys.stderr)
        return 2
    if a.esp_image_tool is None or not a.esp_image_tool.is_file():
        print("REFUSED: no esp_image.py (--esp-image-tool or ESP_IMAGE_TOOL); the image is "
              "not checked, so nothing is flashed", file=sys.stderr)
        return 2
    ok, out = (checker or check_image)(a.esp_image_tool, a.platform, a.esp)
    print(out)
    if not ok:
        print(f"REFUSED: image check for {a.platform} failed", file=sys.stderr)
        return 2

    dest = a.dest
    vol = dest.split("/")[1].casefold() if dest.startswith("/") and len(dest.split("/")) > 2 else ""
    if vol in ("temp", "flash") or not vol:
        print(f"REFUSED: --dest {dest!r}: /Temp is the firmware RAM disk the harness sweeps and "
              "/Flash holds the firmware's own files; the harness storage API refuses both. "
              "Use a directory on a writable volume, e.g. /USB1/espflash/<name>.esp",
              file=sys.stderr)
        return 2
    if not dest.upper().endswith(".ESP"):
        print("REFUSED: --dest must end in .esp", file=sys.stderr)
        return 2
    put = put or storage_put_file
    delete = delete or storage_delete_file
    rmdir = rmdir or storage_rmdir
    parent = dest.rsplit("/", 1)[0]   # e.g. /USB1/espflash; never a volume root here
    get = get or storage_get_file
    if a.expect_ident is not None and len(a.expect_ident) > ESP_IDENT_MAX:
        print(f"REFUSED: --expect-ident is longer than the {ESP_IDENT_MAX} characters the "
              "module reports", file=sys.stderr)
        return 2
    probe = ident_probe
    if probe is None and not a.no_ident_probe:
        from ..backends.ultimate64_firmware_flash import read_wifi_ident

        def probe():  # the menu is bound below; never called inside the flash window
            return read_wifi_ident(a.host, poll_interval_s=ident_poll,
                                   menu_factory=lambda host: menu).ident
    local = a.esp.read_bytes()
    try:
        budget = resolve_lock_timeout(a.lock_timeout, default=DEFAULT_LOCK_TIMEOUT)
    except ValueError as exc:   # --lock-timeout nan, or a malformed U64_DEVICE_LOCK_TIMEOUT
        print(f"REFUSED before the lock, nothing was sent: {exc}", file=sys.stderr)
        return 2
    lock = (lock_factory or DeviceLock)(a.host)
    try:
        lock.acquire_or_raise(timeout=budget)
    except DeviceLockTimeout as exc:
        print(f"refusing to run: {exc}", file=sys.stderr)   # nothing was flashed: 2, not 3
        return 2
    uploaded = False
    keep = False
    try:
        menu = (menu_factory or Ultimate64RemoteMenu)(a.host)
        try:
            print("WARNING: do not touch the U64E's keyboard or menu during the flash window "
                  "(the action has no mutex); Wi-Fi is off for its duration.")
            try:
                put(a.host, dest, a.esp, overwrite=False)   # never replace what we did not create
                uploaded = True
                # The firmware reports "Success!" for a truncated file: read it back.
                back = get(a.host, dest)
            except (Ultimate64StorageError, OSError) as exc:   # FileExistsError is an OSError
                print(f"REFUSED before the ENTER, nothing was flashed: {type(exc).__name__}: "
                      f"{exc}", file=sys.stderr)
                return 2
            if back != local:
                print(f"REFUSED: the file on the device ({len(back)} B) is not the local "
                      f"image ({len(local)} B, sha256 {hashlib.sha256(local).hexdigest()}); "
                      "nothing was flashed", file=sys.stderr)
                return 2
            keep = True   # from here the firmware may be reading the file, whatever happens
            res = menu.flash_esp(dest, ident_probe=probe, allow_same_ident=a.allow_same_ident, expect_ident=a.expect_ident,
                                 confirm_module_flash=True, timeout=a.timeout,
                                 verify_timeout=a.verify_timeout, wifi_ip=a.wifi_ip, ping=False if a.no_ping else ping)
            keep = False   # a normal return: the module is done with the file
        except EspFlashError as exc:
            # a failure popup or a verification failure: the flash has finished
            keep = not isinstance(exc, (EspFlashFailedError, EspFlashVerifyError))
            print(f"FAILED {type(exc).__name__}: {exc}")
            if exc.result is not None:
                print(exc.result.summary())
            return 1
        except Ultimate64UnsafeOperationError as exc:
            keep = False
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 2
        except Ultimate64RemoteMenuError as exc:
            keep = bool(exc.enter_sent)
            if exc.enter_sent:
                print(f"FAILED {type(exc).__name__}: {exc} (the ENTER was sent: the module may "
                      "have been touched; check Wi-Fi and the ident before anything else)")
                return 1
            print(f"REFUSED before the ENTER, nothing was flashed: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            if "which row is selected" in str(exc) or "is not in" in str(exc):
                print("hint: the menu could not find or mark the file's row. A corrupted or "
                      "oversized directory listing (duplicate or garbled names) does this: "
                      f"look at the device's telnet screen for {parent}/ and repair the "
                      "volume or pick another --dest.", file=sys.stderr)
            for row in (exc.screen or ())[:6]:
                print(f"  screen| {row}", file=sys.stderr)
            return 2
        finally:
            if uploaded and keep:
                print(f"the upload is KEPT at {dest} (and its directory): the ENTER may have "
                      "been sent, and the firmware may still be reading the open file. Delete it by hand once "
                      "the module is back.")
            if uploaded and not keep:
                try:
                    delete(a.host, dest, missing_ok=True)
                except Exception as exc:  # the flash verdict matters more
                    print(f"note: could not delete {dest}: {exc}", file=sys.stderr)
            if uploaded and not keep and parent.count("/") >= 2:
                try:
                    rmdir(a.host, parent)   # only succeeds when empty; other files stay
                except Exception as exc:
                    print(f"note: left {parent} in place ({type(exc).__name__})", file=sys.stderr)
        print(res.summary())
        return 1 if not res.ok else (0 if res.proven or (a.allow_same_ident and res.ping_ok is True) else 3)
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
