"""Unattended ESP32-module flash of an Ultimate 64 Elite over its telnet menu.

    python -m c64_test_harness.tools.flash_esp --host <ETHERNET address> \\
        --esp raw_u64.esp --platform u64e --esp-image-tool /path/to/esp_image.py \\
        --expect-ident "<ident>" --wifi-ip <module Wi-Fi address> --confirm-module-flash

Steps: run ``esp_image.py check --platform P file`` (an external tool, given
by ``--esp-image-tool`` or the ``ESP_IMAGE_TOOL`` environment variable; the
harness does not carry a copy), take the device's ``DeviceLock``, upload to
``/Temp``, run ``Ultimate64RemoteMenu.flash_esp`` (which verifies Wi-Fi,
ping and ident afterwards), delete the upload, print one result line.
Exit 0 only when the whole verdict is OK; 1 on a failed or unverified flash
(the menu's ENTER was sent); 2 on a refusal before it was.

``--host`` must be the Ethernet address: the flash stops Wi-Fi.
"""

from __future__ import annotations

import argparse
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


def check_image(tool: Path, platform_name: str, image: Path) -> tuple[bool, str]:
    """Run ``esp_image.py check``; return (ok, its output)."""
    r = subprocess.run([sys.executable, str(tool), "check", "--platform", platform_name,
                        str(image)], capture_output=True, text=True, timeout=60)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def main(argv: list[str] | None = None, *, menu_factory=None, ping=None,
         put=None, delete=None, lock_factory=None, checker=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", required=True, help="the device's ETHERNET address")
    ap.add_argument("--esp", type=Path, required=True)
    ap.add_argument("--platform", choices=("u64e", "c64u"), required=True)
    ap.add_argument("--esp-image-tool", type=Path,
                    default=Path(os.environ["ESP_IMAGE_TOOL"]) if os.environ.get("ESP_IMAGE_TOOL")
                    else None, help="path of esp_image.py (or env ESP_IMAGE_TOOL)")
    ap.add_argument("--expect-ident")
    ap.add_argument("--wifi-ip", help="the module's Wi-Fi address (pinged afterwards)")
    ap.add_argument("--dest", default=None, help="device path (default /Temp/<name>)")
    ap.add_argument("--lock-timeout", type=float, default=600.0)
    ap.add_argument("--timeout", type=float, default=300.0, help="wait for the result popup")
    ap.add_argument("--verify-timeout", type=float, default=90.0)
    ap.add_argument("--confirm-module-flash", action="store_true")
    a = ap.parse_args(argv)

    from ..backends.device_lock import DeviceLock
    from ..backends.ultimate64_client import Ultimate64UnsafeOperationError
    from ..backends.ultimate64_remote_menu import (
        EspFlashError, Ultimate64RemoteMenu, Ultimate64RemoteMenuError)
    from ..backends.ultimate64_storage import storage_delete_file, storage_put_file

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

    dest = a.dest or f"/Temp/{a.esp.name}"
    put = put or storage_put_file
    delete = delete or storage_delete_file
    lock = (lock_factory or DeviceLock)(a.host)
    lock.acquire_or_raise(timeout=a.lock_timeout)
    try:
        menu = (menu_factory or Ultimate64RemoteMenu)(a.host)
        try:
            put(a.host, dest, a.esp, overwrite=True)
            res = menu.flash_esp(dest, expect_ident=a.expect_ident,
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
            print(f"FAILED {type(exc).__name__}: {exc} (enter_sent={exc.enter_sent})")
            return 1 if exc.enter_sent else 2
        finally:
            try:
                delete(a.host, dest, missing_ok=True)
            except Exception as exc:  # the flash verdict matters more
                print(f"note: could not delete {dest}: {exc}", file=sys.stderr)
        print(res.summary())
        return 0 if res.ok else 1
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
