"""Unattended ESP32-module flash of an Ultimate 64 Elite over its telnet menu.

    python -m c64_test_harness.tools.flash_esp --host <ETHERNET address> \\
        --esp raw_u64.esp --platform u64e --esp-image-tool /path/to/esp_image.py \\
        --expect-ident "<ident>" --wifi-ip <module Wi-Fi address> --confirm-module-flash

Steps: run ``esp_image.py check --platform P file`` (an external tool, given
by ``--esp-image-tool`` or the ``ESP_IMAGE_TOOL`` environment variable; the
harness does not carry a copy), take the device's ``DeviceLock``, upload to
the ``--dest`` path (a writable volume, e.g. ``/USB1/espflash/x.esp``; never
``/Temp`` or ``/Flash``), run ``Ultimate64RemoteMenu.flash_esp`` (which verifies Wi-Fi,
ping and ident afterwards), delete the upload (and its directory if empty), print one result line.
Exit 0 only when the whole verdict is OK and proven (the ident changed); 3 when the
flash verified but is unproven (no ident change, e.g. a same-image flash); 1 on a failed or unverified flash
(the menu's ENTER was sent); 2 on a refusal before it was.

``--host`` must be the Ethernet address: the flash stops Wi-Fi.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from pathlib import Path


def system_ping(ip: str, timeout: float = 2.0) -> bool:
    """One ICMP echo through the system ``ping``."""
    # macOS -W is milliseconds, Linux seconds.
    wait = str(int(timeout * 1000)) if sys.platform == "darwin" else str(max(1, int(timeout)))
    try:
        r = subprocess.run(["ping", "-c", "1", "-W", wait, ip], capture_output=True,
                           timeout=timeout + 3)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def read_ident(menu, *, link_timeout: float = 90.0, poll: float = 2.0) -> str | None:
    """Read the module ident: the WiFi row shows it only while Wi-Fi is Disabled.

    Over the Ethernet session: if Wi-Fi is on, Disable it, read the row, Enable
    it and wait for Link Up again (only when it was Link Up before). Call it
    BEFORE and AFTER the flash window, never inside it. On the U64E each toggle
    writes the Wi-Fi config to flash. A minimal copy of the shape of harness
    PR #569's ``read_wifi_ident``; collapse onto that once it merges.
    """
    import time

    from ..backends.ultimate64_remote_menu import Ultimate64RemoteMenuError

    st = menu.wifi_status()
    if st.state == "disabled":
        return st.ident
    if st.state not in ("link-up", "link-down"):
        raise Ultimate64RemoteMenuError(f"WiFi row is {st.row!r}: no ident to read, nothing sent")
    ident = None
    err: BaseException | None = None
    try:
        menu.set_wifi_enabled(False)
        ident = menu.wifi_status().ident
    except Ultimate64RemoteMenuError as exc:
        err = exc
        if exc.enter_sent is False:
            raise
    menu.set_wifi_enabled(True)
    want = ("link-up",) if st.state == "link-up" else ("link-up", "link-down")
    end = time.monotonic() + link_timeout
    while menu.wifi_status().state not in want:
        if time.monotonic() >= end:
            raise Ultimate64RemoteMenuError(
                f"Wi-Fi did not return to {want[0]} within {link_timeout:.0f} s after reading the ident")
        time.sleep(poll)
    if err is not None:
        raise err
    return ident


def check_image(tool: Path, platform_name: str, image: Path) -> tuple[bool, str]:
    """Run ``esp_image.py check``; return (ok, its output)."""
    r = subprocess.run([sys.executable, str(tool), "check", "--platform", platform_name,
                        str(image)], capture_output=True, text=True, timeout=60)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def main(argv: list[str] | None = None, *, menu_factory=None, ping=None,
         put=None, delete=None, rmdir=None, get=None, lock_factory=None, checker=None,
         ident_probe=None) -> int:
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
    ap.add_argument("--lock-timeout", type=float, default=600.0)
    ap.add_argument("--timeout", type=float, default=300.0, help="wait for the result popup")
    ap.add_argument("--verify-timeout", type=float, default=90.0)
    ap.add_argument("--no-ident-probe", action="store_true",
                    help="do not toggle Wi-Fi to read the ident before/after (result is then unproven)")
    ap.add_argument("--allow-same-ident", action="store_true",
                    help="a flash that leaves the ident unchanged exits 0 instead of 3 (unproven)")
    ap.add_argument("--confirm-module-flash", action="store_true")
    a = ap.parse_args(argv)

    from ..backends.device_lock import DeviceLock
    from ..backends.ultimate64_client import Ultimate64UnsafeOperationError
    from ..backends.ultimate64_remote_menu import (
        EspFlashError, Ultimate64RemoteMenu, Ultimate64RemoteMenuError)
    from ..backends.ultimate64_storage import (
        storage_delete_file, storage_get_file, storage_put_file, storage_rmdir)

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
    probe = ident_probe
    if probe is None and not a.no_ident_probe:
        if a.expect_ident is not None and len(a.expect_ident) > 31:
            print("REFUSED: --expect-ident is longer than the 31 characters the module reports",
                  file=sys.stderr)
            return 2
        probe = lambda: read_ident(menu)  # noqa: E731  (menu is bound below)
    local = a.esp.read_bytes()
    lock = (lock_factory or DeviceLock)(a.host)
    lock.acquire_or_raise(timeout=a.lock_timeout)
    try:
        menu = (menu_factory or Ultimate64RemoteMenu)(a.host)
        try:
            print("WARNING: do not touch the U64E's keyboard or menu during the flash window "
                  "(the action has no mutex); Wi-Fi is off for its duration.")
            put(a.host, dest, a.esp, overwrite=True)
            # The firmware reports "Success!" for a truncated file: read it back.
            back = get(a.host, dest)
            if back != local:
                print(f"REFUSED: the file on the device ({len(back)} B) is not the local "
                      f"image ({len(local)} B, sha256 {hashlib.sha256(local).hexdigest()}); "
                      "nothing was flashed", file=sys.stderr)
                return 2
            res = menu.flash_esp(dest, ident_probe=probe, allow_same_ident=a.allow_same_ident, expect_ident=a.expect_ident,
                                 confirm_module_flash=True, timeout=a.timeout,
                                 verify_timeout=a.verify_timeout, wifi_ip=a.wifi_ip, ping=ping)
        except EspFlashError as exc:
            print(f"FAILED {type(exc).__name__}: {exc}")
            if exc.result is not None:
                print(exc.result.summary())
            return 1
        except Ultimate64UnsafeOperationError as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 2
        except Ultimate64RemoteMenuError as exc:
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
            try:
                delete(a.host, dest, missing_ok=True)
            except Exception as exc:  # the flash verdict matters more
                print(f"note: could not delete {dest}: {exc}", file=sys.stderr)
            if parent.count("/") >= 2:
                try:
                    rmdir(a.host, parent)   # only succeeds when empty; other files stay
                except Exception as exc:
                    print(f"note: left {parent} in place ({type(exc).__name__})", file=sys.stderr)
        print(res.summary())
        return 1 if not res.ok else (0 if res.proven or a.allow_same_ident else 3)
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
