#!/usr/bin/env python3
"""Volatile application loads on the C64 Ultimate over its P5 JTAG header, and its software power cycle.

Nothing here writes the device's flash. A JTAG load reprograms the FPGA from a .bit and places a raw application
image (``ultimate.bin``, linked at 0x30000) in DRAM; the bootloader in the new bitstream finds the magic at 0xFFF8
and jumps to it. The flashed firmware comes back on the next power cycle. Between two loads no power cycle is
needed: every load starts with JPROGRAM.

Design: docs/design/jtag_recovery.md (branch design/flashing-and-jtag). Adapter: an ESP32 running esp-jtag
(``~/Documents/1541u-tools/esp-jtag``, ``host/esp_recover.py`` is imported, not copied).

Library use (a test script looping over N images)::

    from jtag_load import jtag_load, power_cycle
    for img in images:
        rec = jtag_load("10.43.23.26", img.bit, img.bin, bit_sha256=img.bit_sha, app_sha256=img.bin_sha,
                        expect_git_hash=img.hash, record_path="runs/loads.jsonl")
        run_tests(...)
    power_cycle("10.43.23.26", expect_git_hash=FLASHED_HASH)   # back to the flashed image

Each call takes the device's DeviceLock (``allow_nested=True``, so a caller may hold it across the loop) and then
the adapter's own lock. Safety gates, in order: device bound to an adapter; file SHA-256 equal to the caller's
(files are copied first and the copies are hashed and loaded); the .bit's IDCODE is an XC7A50T; the device (if it
answers) is the bound unique_id and a C64U-class product; the live IDCODE equals the .bit's (checked before the FPGA
is touched); user ID 0xDEAD1541 after the load; then /v1/info must come back with the bound unique_id and, when
given, the expected git hash.

CLI::

    jtag_load.py idcode     HOST                  IDCODE only (pins driven, nothing written)
    jtag_load.py probe      HOST                  IDCODE + user-block ID of the RUNNING bitstream, no load
    jtag_load.py load       HOST --bit F --bin F --bit-sha256 H --bin-sha256 H [--expect-hash G] [--dry-run]
    jtag_load.py power-cycle HOST [--expect-hash G] [--dry-run]
    jtag_load.py extract    APP OUT               DRAM image from a one-record ultimate.app; prints its SHA-256
    jtag_load.py scan-u64e  HOST                  U64E (Cyclone V) chain: IDCODEs, IR length, SLD hub; read-only
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from c64_test_harness.backends.device_lock import DeviceLock, normalize_device_host  # noqa: E402

ESP_JTAG_HOST = Path(os.environ.get("ESP_JTAG_DIR", "~/Documents/1541u-tools/esp-jtag")).expanduser() / "host"
UPSTREAM_TESTS = Path(os.environ.get("C64U_TESTS_DIR", "~/Documents/1541ultimate/tests")).expanduser()

# One kit, wired to one device. Key: the device's /v1/info unique_id (the DeviceLock key is uid-<lowercase>).
# JTAG_PORT overrides the port (the adapter is identified by its esp-jtag handshake, not by the name; CP2102 bridges
# all report serial 0001, so the name says nothing). The adapter lock is keyed by the port actually used.
_PORT = os.environ.get("JTAG_PORT", "/dev/cu.usbserial-0001")
ADAPTERS = {
    "5D2518": {"port": _PORT, "lock": "jtag-adapter-" + os.path.basename(_PORT), "baud": "921600"},
}
PRODUCTS = ("C64 Ultimate", "Ultimate 64-II")   # U64 == 2 builds; the dev app reports "Ultimate 64-II"
XC7A50T = 0x0362C093
EXPECT_USERID = 0xDEAD1541


class JtagLoadError(RuntimeError):
    def __init__(self, stage: str, message: str):
        super().__init__(f"[{stage}] {message}")
        self.stage = stage


def _log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def esp_module():
    """Import esp_recover from the esp-jtag kit."""
    if str(ESP_JTAG_HOST) not in sys.path:
        sys.path.insert(0, str(ESP_JTAG_HOST))
    return importlib.import_module("esp_recover")


def bound_uid(host: str) -> str:
    """The unique_id the host's lock key names, if an adapter is bound to it; else refuse."""
    key = normalize_device_host(host)
    if not key.startswith("uid-"):
        raise JtagLoadError("binding", f"{host} is not in devices.toml, so it has no unique_id to bind an adapter to")
    uid = key[4:].upper()
    if uid not in ADAPTERS:
        raise JtagLoadError("binding", f"no JTAG adapter is bound to device {uid} ({host})")
    return uid


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def pin_files(bit: Path, app: Path, bit_sha256: str, app_sha256: str, workdir: Path) -> tuple[Path, Path]:
    """Copy both files into workdir, hash the COPIES, refuse unless they equal the caller's hashes."""
    out = []
    for src, want, name in ((Path(bit), bit_sha256, "fpga.bit"), (Path(app), app_sha256, "ultimate.bin")):
        if not want or len(want) != 64:
            raise JtagLoadError("hash", f"no SHA-256 given for {src}; every load is pinned")
        dst = Path(workdir) / name
        shutil.copyfile(src, dst)
        got = sha256(dst)
        if got != want.lower():
            raise JtagLoadError("hash", f"{src}: SHA-256 {got} is not the pinned {want.lower()}")
        out.append(dst)
    return out[0], out[1]


def payload_from_app(app: Path, out: Path) -> str:
    """Write the DRAM image of a single-record ``ultimate.app`` (hex2bin records {load, length, start}) to out.

    This is byte for byte what the flash bootloader copies, which ``ultimate.bin`` (objcopy) is not quite: objcopy
    fills alignment gaps with 0x00 where hex2bin writes 0xFF. Refuses anything but one record loaded at 0x30000.
    Returns the SHA-256 of out.
    """
    import struct
    data = Path(app).read_bytes()
    load, length, start = struct.unpack("<3I", data[:12])
    if load != 0x30000 or start != 0x30000 or len(data) != 12 + length:
        raise JtagLoadError("app", f"{app}: not one record at 0x30000 (load {load:#x}, start {start:#x}, "
                            f"length {length:#x}, file {len(data) - 12:#x} after the header)")
    Path(out).write_bytes(data[12:])
    return sha256(Path(out))


def _client(host: str, timeout: float = 5.0):
    from c64_test_harness.backends.ultimate64_client import Ultimate64Client
    return Ultimate64Client(host, password=os.environ.get("U64_PASS"), timeout=timeout)


def try_info(host: str, client_factory=_client) -> dict | None:
    try:
        return client_factory(host).get_info()
    except Exception:
        return None


def check_identity(info: dict, uid: str, products=PRODUCTS) -> None:
    if str(info.get("unique_id", "")).upper() != uid:
        raise JtagLoadError("identity", f"device answers with unique_id {info.get('unique_id')!r}, bound is {uid}")
    if info.get("product") not in products:
        raise JtagLoadError("identity", f"product {info.get('product')!r} is not a C64U-class board")


def check_git_hash(expect_git_hash) -> str:
    h = str(expect_git_hash or "").lower()
    if not re.fullmatch(r"[0-9a-f]{7,40}", h):
        raise JtagLoadError("hash", f"expected git hash {expect_git_hash!r}: give at least 7 hex digits")
    return h


def hash_matches(reported, expect: str) -> bool:
    """/v1/info reports `git rev-parse --short HEAD` (9 digits here); match on the common prefix."""
    r = str(reported or "").lower()
    n = min(len(r), len(expect))
    return n >= 7 and r[:n] == expect[:n]


def wait_info(host: str, uid: str, expect_git_hash: str | None, timeout: float, client_factory=_client,
              sleep=None, clock=None, products=PRODUCTS) -> dict:
    """Poll /v1/info until it answers with the bound unique_id (and the expected hash, if given)."""
    sleep, clock = sleep or time.sleep, clock or time.monotonic
    expect = check_git_hash(expect_git_hash) if expect_git_hash is not None else None
    end = clock() + timeout
    last = None
    while clock() < end:
        info = try_info(host, client_factory)
        if info is not None:
            last = info
            check_identity(info, uid, products)
            if expect is None or hash_matches(info.get("git_commit_hash"), expect):
                return info
        sleep(2.0)
    raise JtagLoadError("network", f"no /v1/info with the expected identity within {timeout:.0f} s; last {last!r}")


def _open(esp, adapter: dict, tck: int = 1_000_000, timeout_ms: int = 10_000):
    ns = argparse.Namespace(port=adapter["port"], baud=adapter.get("baud", "auto"), tck=tck, timeout_ms=timeout_ms)
    link = esp.open_link(ns)
    try:
        ident = str(getattr(link, "info", {}).get("ident", ""))
        if not ident.startswith("esp-jtag"):
            raise JtagLoadError("adapter", f"{adapter['port']} answers as {ident!r}, not esp-jtag")
        return link, esp.open_jtag(link, ns)
    except BaseException:
        link.ser.close()
        raise


class _Locked:
    """DeviceLock(host) then the adapter lock; released in reverse order."""

    def __init__(self, host: str, adapter: dict, lock_timeout: float, lock_factory=DeviceLock):
        self.dev = lock_factory(host, allow_nested=True)
        self.ad = lock_factory(adapter["lock"]) if adapter else None
        self.host, self.timeout = host, lock_timeout

    def __enter__(self):
        if _held_by_lock_shell(self.host):     # locked.py convention: C64U_LOCK_PID holds the DeviceLock
            self.dev = None
        elif not self.dev.acquire(timeout=self.timeout):
            raise JtagLoadError("lock", f"DeviceLock({self.host}) not acquired within {self.timeout:.0f} s")
        if self.ad is not None and not self.ad.acquire(timeout=30):
            if self.dev is not None:
                self.dev.release()
            raise JtagLoadError("lock", "the JTAG adapter is in use by another process")
        return self

    def __exit__(self, *exc):
        if self.ad is not None:
            self.ad.release()
        if self.dev is not None:
            self.dev.release()
        return False


def _held_by_lock_shell(host: str) -> bool:
    """True when C64U_LOCK_PID is set for this host and that process holds its DeviceLock (locked.py)."""
    pid = os.environ.get("C64U_LOCK_PID")
    if not pid:
        return False
    shell_host = os.environ.get("C64U_LOCK_HOST")
    if shell_host and normalize_device_host(shell_host) != normalize_device_host(host):
        return False
    from c64_test_harness.backends.device_lock import device_lock_holder
    h = device_lock_holder(host)
    if h and str(h.get("pid")) == pid:
        return True
    raise JtagLoadError("lock", f"C64U_LOCK_PID={pid} does not hold the DeviceLock for {host} (holder {h})")


TDI_PATTERN = [1, 0, 1, 1, 0, 0, 1, 0, 1, 1, 1, 0, 0, 0, 1, 0, 0, 1]


def bypass_ok(jtag) -> bool:
    """TDI -> TDO through the 1-bit BYPASS register: the pattern must come back one clock late.

    IDCODE needs only TCK/TMS/TDO, so it passes with TDI (P5 pin 9) loose; a user-block read then reads garbage and
    would be misread as "this bitstream has no user block". Nothing but BYPASS is selected; no user logic is touched.
    """
    jtag.reset()
    jtag.write_ir(0x3F, 6)                  # BYPASS on the 7-series (IR all ones)
    jtag.change_state("shift_dr")
    out = jtag.shift_register(TDI_PATTERN + [0])
    jtag.go_idle()
    jtag.reset()
    return list(out[1:]) == TDI_PATTERN


def read_ids(host: str, *, user_block: bool = False, lock_timeout: float = 600, esp=None,
             lock_factory=DeviceLock, log=_log) -> dict:
    """IDCODE (and, with user_block, the user-block ID of the RUNNING bitstream). No load, no memory write.

    The user-block read writes the block's own 5-bit instruction register (selects the ID register) and shifts 32
    bits out of it; it does not reset the CPU or touch DRAM. It is what settles whether a bitstream carries the
    JTAG user block (IR 5/6 memory writes) before any load is attempted with it.
    """
    esp = esp or esp_module()
    adapter = ADAPTERS[bound_uid(host)]
    with _Locked(host, adapter, lock_timeout, lock_factory):
        link, jtag = _open(esp, adapter)
        try:
            rec = esp.Recover(jtag, log=log)
            jtag.reset()
            idcode = rec.read_id()
            jtag.sync()
            out = {"idcode": f"{idcode:08x}", "xilinx": (idcode & 0xFFF) == 0x093}
            if out["xilinx"]:
                out["tdi_ok"] = bypass_ok(jtag)
                jtag.sync()
            if user_block and out.get("tdi_ok"):
                out["user_id"] = f"{rec.user_read_id():08x}"
                jtag.sync()
        finally:
            jtag.close()
            link.ser.close()
    return out


# ---- U64E (Altera Cyclone V) chain scan: read-only, no load, no SVF ----------------------------------------------
SCAN_ONLY = {"601A96"}                      # devices the scan is enabled for (never loaded with this kit)
ALTERA_USER1, ALTERA_USER0, ALTERA_IR_LEN = 0x00E, 0x00C, 10


def ir_length(jtag, flood: int = 64) -> int:
    """Total IR length of the chain: flood Shift-IR with ones, then a single zero; count until it reaches TDO.

    The zero is followed by ones again, so what Update-IR latches is all ones = BYPASS on every device: no
    instruction other than BYPASS is ever selected (on Cyclone V a stray 0x001 would be PULSE_NCONFIG).
    """
    jtag.reset()
    jtag.change_state("shift_ir")
    bits = [1] * flood + [0] + [1] * flood
    out = jtag.shift_register(bits)
    jtag.go_idle()
    jtag.reset()
    for k in range(flood, len(out)):
        if out[k] == 0:
            return k - flood
    return -1


def idcodes(jtag, n: int = 4) -> list:
    """The DR after Test-Logic-Reset is IDCODE (32 bits, LSB 1) or BYPASS (1 bit, 0) per device."""
    jtag.reset()
    v = jtag.read_dr(32 * n)
    jtag.reset()
    out, i = [], 0
    while i < 32 * n:
        if (v >> i) & 1:
            code = (v >> i) & 0xFFFFFFFF
            if code == 0xFFFFFFFF:
                break
            out.append(code)
            i += 32
        else:
            i += 1
    return out


def sld_hub_info(jtag) -> dict:
    """Altera SLD hub enumeration (as Quartus/jtagconfig and OpenOCD's or1k vjtag code do): IR USER1 with a zero
    virtual IR (hub address 0, HUB_INFO), then IR USER0 and eight 4-bit DR reads per 32-bit word, LSB nibble first:
    the hub word, then one word per node. Read-only on the user logic. Single-device chain assumed (checked first)."""
    def word():
        w = 0
        for i in range(8):
            w |= (jtag.read_dr(4) & 0xF) << (4 * i)
        return w
    jtag.reset()
    jtag.write_ir(ALTERA_USER1, ALTERA_IR_LEN)
    jtag.write_dr(0, 64)
    jtag.write_ir(ALTERA_USER0, ALTERA_IR_LEN)
    hub = word()
    info = {"hub_raw": f"{hub:08x}", "vir_width": hub & 0xFF, "manufacturer": f"{(hub >> 8) & 0x7FF:03x}",
            "nodes": (hub >> 19) & 0xFF, "hub_version": (hub >> 27) & 0x1F, "node_list": []}
    if info["manufacturer"] == "06e" and 0 < info["nodes"] <= 16:
        for _ in range(info["nodes"]):
            n = word()
            info["node_list"].append({"raw": f"{n:08x}", "instance": n & 0xFF, "manufacturer": f"{(n >> 8) & 0x7FF:03x}",
                                      "node_id": (n >> 19) & 0xFF, "version": (n >> 27) & 0x1F})
    jtag.reset()
    jtag.write_ir(0x3FF, ALTERA_IR_LEN)    # leave BYPASS selected
    jtag.reset()
    return info


def scan_u64e(host: str, *, lock_timeout: float = 600, esp=None, lock_factory=DeviceLock, log=_log) -> dict:
    """Read-only chain scan of the U64E FPGA over the ESP32 adapter: IDCODEs, IR length, SLD hub and nodes."""
    esp = esp or esp_module()
    key = normalize_device_host(host)
    uid = key[4:].upper() if key.startswith("uid-") else None
    if uid not in SCAN_ONLY:
        raise JtagLoadError("binding", f"{host}: the chain scan is enabled only for {sorted(SCAN_ONLY)}")
    adapter = {"port": _PORT, "lock": "jtag-adapter-" + os.path.basename(_PORT), "baud": "921600"}
    with _Locked(host, adapter, lock_timeout, lock_factory):
        link, jtag = _open(esp, adapter)
        try:
            out = {"idcodes": [f"{c:08x}" for c in idcodes(jtag)], "ir_length": ir_length(jtag)}
            altera = [c for c in out["idcodes"] if c.endswith("0dd")]
            out["altera"] = bool(altera)
            if len(out["idcodes"]) == 1 and altera and out["ir_length"] == ALTERA_IR_LEN:
                out["sld_hub"] = sld_hub_info(jtag)
            jtag.sync()
        finally:
            jtag.close()
            link.ser.close()
    log(f"U64E chain scan: {out}")
    return out


def jtag_load(host: str, bit, app, *, bit_sha256: str, app_sha256: str, expect_git_hash: str,
              lock_timeout: float = 600, net_timeout: float = 180, record_path=None, dry_run: bool = False,
              esp=None, lock_factory=DeviceLock, client_factory=_client, log=_log) -> dict:
    """Load bit + app into the bound C64U over JTAG; return a record of what was loaded and what came back."""
    esp = esp or esp_module()
    uid = bound_uid(host)
    adapter = ADAPTERS[uid]
    expect_git_hash = check_git_hash(expect_git_hash)
    rec = {"t0": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "host": host, "uid": uid, "adapter": adapter["port"],
           "bit": str(bit), "bin": str(app), "bit_sha256": bit_sha256.lower(), "bin_sha256": app_sha256.lower(),
           "expect_git_hash": expect_git_hash, "dry_run": dry_run}
    try:
        with tempfile.TemporaryDirectory(prefix="jtag_load-") as td:
            pbit, papp = pin_files(Path(bit), Path(app), bit_sha256, app_sha256, Path(td))
            want_id = esp.bit_idcode(str(pbit))
            rec["bit_idcode"] = f"{want_id:08x}"
            if not esp.idcode_matches(want_id, XC7A50T):
                raise JtagLoadError("bit", f"the .bit is for IDCODE {want_id:08x}, not the C64U's XC7A50T")
            if not Path(adapter["port"]).exists():
                raise JtagLoadError("adapter", f"{adapter['port']} is not present")
            if dry_run:
                rec["result"] = "dry-run ok"
                return rec
            with _Locked(host, adapter, lock_timeout, lock_factory):
                pre = try_info(host, client_factory)
                rec["pre_info"] = pre
                if pre is not None:
                    check_identity(pre, uid)
                log(f"JTAG load {Path(bit).name} + {Path(app).name} into {uid} ({host})")
                t0 = time.monotonic()
                link, jtag = _open(esp, adapter)
                try:
                    loader = esp.Recover(jtag, log=log)

                    def idcode_ok(idcode):          # before the FPGA is touched
                        rec["idcode"] = f"{idcode:08x}"
                        if not esp.idcode_matches(idcode, want_id):
                            raise JtagLoadError("idcode", f"live IDCODE {idcode:08x} is not the .bit's {want_id:08x}"
                                                " (P5 unwired or the C64U off?); nothing was loaded")
                        if not bypass_ok(jtag):
                            raise JtagLoadError("tdi", "TDI->TDO through BYPASS failed (P5 pin 9 loose?); "
                                                "nothing was loaded")
                    loader.run(str(pbit), str(papp), idcode_ok=idcode_ok)
                    jtag.link.drain()
                    rec["tck_cycles"] = getattr(jtag, "clock_count", None)
                finally:
                    jtag.close()
                    link.ser.close()
                rec["load_s"] = round(time.monotonic() - t0, 1)
                rec["user_id"] = f"{loader.user_id:08x}" if loader.user_id is not None else None
                if loader.user_id != EXPECT_USERID:
                    raise JtagLoadError("userid", f"user ID {rec['user_id']} after the load, expected "
                                        f"{EXPECT_USERID:08x}: the image did not come up. Power cycle the C64U.")
                info = wait_info(host, uid, expect_git_hash, net_timeout, client_factory)
                rec["info"] = info
                rec["up_s"] = round(time.monotonic() - t0, 1)
                rec["result"] = "ok"
                log(f"up: {info.get('git_commit_hash')} {info.get('product')} after {rec['up_s']} s")
                return rec
    except Exception as e:
        rec["result"] = "error"
        rec["error"] = str(e)
        raise
    finally:
        if record_path:
            with open(record_path, "a") as f:
                f.write(json.dumps(rec, default=str) + "\n")


def power_cycle(host: str, *, expect_git_hash: str | None = None, lock_timeout: float = 600,
                down_timeout: float = 60, up_timeout: float = 240, dry_run: bool = False,
                lock_factory=DeviceLock, client_factory=_client, browser_factory=None,
                sleep=None, clock=None, log=_log) -> dict:
    """C64U regulator power cycle from the telnet menu: C64 Machine > Power Cycle (U64 == 2 only).

    rehearse_power_cycle() walks the same path and leaves without ENTER.

    Firmware path: c64_subsys.cc MENU_C64_POWERCYCLE -> wifi_machine_reboot() -> u64ctrl cmd_reboot (regulators
    off, 2 s, on). The FPGA reconfigures from flash, so this is also the way back from a JTAG-loaded image.
    """
    sleep, clock = sleep or time.sleep, clock or time.monotonic
    uid = bound_uid(host)
    rec = {"t0": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "host": host, "uid": uid, "action": "power_cycle"}
    if browser_factory is None:
        browser_factory = _telnet_browser
    if dry_run:                     # no device I/O: binding checked above; the telnet driver must import
        if browser_factory is _telnet_browser:
            _telnet_imports()
        rec["result"] = "dry-run ok"
        return rec
    with _Locked(host, {}, lock_timeout, lock_factory):
        pre = client_factory(host).get_info()
        check_identity(pre, uid)
        rec["pre_info"] = pre
        b = browser_factory(host)
        log(f"telnet menu: C64 Machine > Power Cycle on {uid}")
        activate_task_item(b, "C64 Machine", "Power Cycle")
        end = clock() + down_timeout
        while try_info(host, client_factory) is not None:
            if clock() > end:
                raise JtagLoadError("power", f"REST still answers {down_timeout:.0f} s after Power Cycle")
            sleep(0.5)
        rec["info"] = wait_info(host, uid, expect_git_hash, up_timeout, client_factory, sleep, clock)
        rec["result"] = "ok"
        return rec


def _label(x: str) -> str:
    return x.split("||", 1)[0].strip()


def activate_task_item(b, category: str, item: str, rehearse: bool = False) -> None:
    """Task menu > category > item, with the item's highlight READ BACK before ENTER.

    The upstream choose_overlay_item quick-seeks by first letter, and in "C64 Machine" that lands on "Power OFF"
    first; it does not verify the highlight over telnet. Here the category is chosen with it (a wrong category only
    opens another submenu, which then lacks the item), and the item is reached with select_framed_entry, which walks
    one row at a time and reads the highlighted text back. After the final ENTER only the socket is closed: the
    browser's own close() navigates home with 1 s timeouts per key, which outlasts the REST drop being watched for.
    On any refusal before ENTER the menus are escaped and the browser closed normally.
    """
    done = False
    try:
        cats = b.open_task_menu()
        if category not in [_label(c) for c in cats]:
            raise JtagLoadError("menu", f"task menu has no {category!r}: {cats}")
        before = b.rows()
        b.choose_overlay_item(cats, category)
        items = b.wait_for_overlay(before)
        if item not in [_label(i) for i in items]:
            raise JtagLoadError("menu", f"{category!r} has no {item!r}: {items}")
        _frame, text = b.select_framed_entry(item)
        if _label(text.split("  ")[0]) != item:
            raise JtagLoadError("menu", f"highlight reads {text!r}, not {item!r}")
        if rehearse:                # navigation proven; leave without activating (the finally ESCapes)
            return
        b.press("ENTER")
        done = True
    finally:
        if done:
            try:
                b.backend.close()
            except Exception:
                pass
        else:
            for _ in range(3):
                try:
                    b.press("ESC")
                except Exception:
                    break
            try:
                b.close()
            except Exception:
                pass


def rehearse_power_cycle(host: str, *, lock_timeout: float = 600, lock_factory=DeviceLock, client_factory=_client,
                         browser_factory=None) -> None:
    """Navigate to the highlighted C64 Machine > Power Cycle and ESCape: proves the menu path, presses no ENTER on it."""
    uid = bound_uid(host)
    with _Locked(host, {}, lock_timeout, lock_factory):
        check_identity(client_factory(host).get_info(), uid)
        activate_task_item((browser_factory or _telnet_browser)(host), "C64 Machine", "Power Cycle", rehearse=True)


def _telnet_imports():
    for p in (UPSTREAM_TESTS, UPSTREAM_TESTS / "e2e" / "lib"):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    from backend import MODE_TELNET
    from ui_backend import make_browser
    return MODE_TELNET, make_browser


def _telnet_browser(host: str):
    """The upstream e2e telnet UI driver (tests/e2e/lib), used only under our DeviceLock."""
    mode, make_browser = _telnet_imports()
    return make_browser(mode, host, telnet_width=60)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("idcode", "probe", "load", "power-cycle", "scan-u64e"):
        p = sub.add_parser(name)
        p.add_argument("host")
        p.add_argument("--lock-timeout", type=float, default=600)
        if name in ("load", "power-cycle"):
            p.add_argument("--expect-hash", required=(name == "load"), default=None,
                           help="git_commit_hash /v1/info must report afterwards (7+ hex digits)")
            p.add_argument("--dry-run", action="store_true", help="all checks that need no device I/O")
            p.add_argument("--record", default=None, help="append a JSON line per action here")
        if name == "load":
            p.add_argument("--bit", required=True)
            p.add_argument("--bin", required=True)
            p.add_argument("--bit-sha256", required=True)
            p.add_argument("--bin-sha256", required=True)
            p.add_argument("--net-timeout", type=float, default=180)
    ex = sub.add_parser("extract")
    ex.add_argument("app")
    ex.add_argument("out")
    a = ap.parse_args(argv)
    if a.cmd == "extract":
        print(payload_from_app(Path(a.app), Path(a.out)))
        return 0
    try:
        if a.cmd == "scan-u64e":
            out = scan_u64e(a.host, lock_timeout=a.lock_timeout)
        elif a.cmd in ("idcode", "probe"):
            out = read_ids(a.host, user_block=a.cmd == "probe", lock_timeout=a.lock_timeout)
        elif a.cmd == "load":
            out = jtag_load(a.host, a.bit, a.bin, bit_sha256=a.bit_sha256, app_sha256=a.bin_sha256,
                            expect_git_hash=a.expect_hash, lock_timeout=a.lock_timeout, net_timeout=a.net_timeout,
                            record_path=a.record, dry_run=a.dry_run)
        else:
            out = power_cycle(a.host, expect_git_hash=a.expect_hash, lock_timeout=a.lock_timeout, dry_run=a.dry_run)
            if a.record:
                with open(a.record, "a") as f:
                    f.write(json.dumps(out, default=str) + "\n")
    except Exception as e:          # JtagLoadError, LinkError, upstream Failure, OSError...
        print(f"FAIL {type(e).__name__}: {e}", file=sys.stderr)
        if getattr(a, "record", None) and a.cmd == "power-cycle":
            with open(a.record, "a") as f:
                f.write(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "action": "power_cycle",
                                    "host": a.host, "result": "error", "error": f"{type(e).__name__}: {e}"}) + "\n")
        return 2
    print(json.dumps(out, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
