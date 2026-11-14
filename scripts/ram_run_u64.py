#!/usr/bin/env python3
"""Run an Ultimate 64 (Elite) application from RAM, without writing flash: the "Run Update" path.

The image is a RAM-run ``.u64`` (1541ultimate branch ``jtagv/u64-ramrun``: ``tools/make_ramrun.py`` around the app's
``ultimate.app`` plus ``ramrun_stub.S``). "Run Update" on a ``.u64`` (``filetype_u2p.cc`` load_format_0) copies each
record to its load address and jumps; the stub silences the running app's DMA, copies the app to 0x0 and starts it.
Nothing is written to flash. The flashed app comes back only with a power cycle (the U64E has no software one).

First, before any lock, client or upload: the file must be a RAM-run image (check_ramrun_image). "Run Update" on a
plain update.u64 flashes the device; on 2026-11-13 a plain image passed here by mistake flashed a U64E.

Steps, all under the device's DeviceLock: identity check (bound unique_id, product "Ultimate 64 Elite"); pinned
SHA-256 of the image; FTP put to a USB volume through the harness storage helpers (they refuse /Temp); telnet menu:
select the file, context menu, "Run Update"; REST must drop and come back with the expected git hash; the file is
deleted again.

    ram_run_u64.py run HOST IMAGE --sha256 H --expect-hash G [--volume USB1] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import jtag_load as jl  # noqa: E402

BOUND = {"601A96"}                       # devices this was reviewed for
PRODUCTS = ("Ultimate 64 Elite", "Ultimate 64")


def bound_u64(host: str) -> str:
    key = jl.normalize_device_host(host)
    uid = key[4:].upper() if key.startswith("uid-") else None
    if uid not in BOUND:
        raise jl.JtagLoadError("binding", f"{host} is not a device this RAM-run path is enabled for")
    return uid


# tools/make_ramrun.py (1541ultimate jtagv/u64-ramrun 30cd96ff9): the stub at __updater_start, the app staged 0x1000
# above it, and "RAMR" as the first word of the stub's ramrun_params block (ramrun_stub.S:68-73), which make_ramrun.py
# finds with rfind and fills with source, destination, length, entry.
RAMRUN_STUB_ADDR = 0x03000000
RAMRUN_STAGE_ADDR = 0x03001000
RAMRUN_MAGIC = 0x524D4152


def check_ramrun_image(data: bytes) -> None:
    """Refuse anything but a RAM-run file: "Run Update" on a plain update.u64 FLASHES the device.

    The records are parsed by the harness's own format-0 reader (``inspect_update_image``, ultimate64_firmware_flash.py),
    which also enforces the loader's rules: complete records, word-aligned lengths between records, and after the last
    one only its hex2bin padding (0-3 bytes). On top of that, exactly what make_ramrun.py writes: two records, the
    application at the staging address with start 0, then the stub at its own address with start = that address; the
    stub's ramrun_params block ("RAMR" magic, then source, destination, length, entry) names the staged record.
    """
    from c64_test_harness.backends.ultimate64_firmware_flash import inspect_update_image
    try:
        recs = inspect_update_image(data).records
    except ValueError as e:
        raise jl.JtagLoadError("image", f"not a RAM-run file: {e}") from None
    shape = [(hex(load), length, hex(start)) for load, length, start in recs]
    if (len(recs) != 2 or recs[0][0] != RAMRUN_STAGE_ADDR or recs[0][2] != 0
            or recs[1][0] != RAMRUN_STUB_ADDR or recs[1][2] != RAMRUN_STUB_ADDR):
        raise jl.JtagLoadError("image", f"not a RAM-run file (expected the app at {RAMRUN_STAGE_ADDR:#x} with start 0, "
                               f"then the stub at {RAMRUN_STUB_ADDR:#x} starting there; got {shape}): a plain update "
                               "image would flash")
    off = 12 + recs[0][1] + 12
    payload = data[off:off + recs[1][1]]
    pos = payload.rfind(struct.pack("<I", RAMRUN_MAGIC))
    if pos < 0 or pos + 20 > len(payload):
        raise jl.JtagLoadError("image", "not a RAM-run file: the stub record has no RAMR parameter block")
    source, _dest, length, entry = struct.unpack_from("<IIII", payload, pos + 4)
    if source != recs[0][0] or length != recs[0][1] or not entry:
        raise jl.JtagLoadError("image", f"not a RAM-run file: the stub's parameters (source {source:#x}, length "
                               f"{length}, entry {entry:#x}) do not describe the staged record {shape[0]}")


def ram_run(host: str, image, *, sha256: str, expect_git_hash: str, volume: str = "USB1", stem: str | None = None,
            lock_timeout: float = 600, down_timeout: float = 60, up_timeout: float = 180, dry_run: bool = False,
            lock_factory=jl.DeviceLock, client_factory=jl._client, browser_factory=None, storage=None,
            sleep=None, clock=None, log=jl._log) -> dict:
    sleep, clock = sleep or time.sleep, clock or time.monotonic
    uid = bound_u64(host)
    data = Path(image).read_bytes()
    check_ramrun_image(data)
    got = jl.hashlib.sha256(data).hexdigest()
    if not sha256 or got != sha256.lower():
        raise jl.JtagLoadError("hash", f"{image}: SHA-256 {got} is not the pinned {sha256}")
    expect_git_hash = jl.check_git_hash(expect_git_hash)
    stem = stem or ("rr" + expect_git_hash[:6])
    if not stem.isalnum() or len(stem) > 8:
        raise jl.JtagLoadError("name", f"stem {stem!r}: use up to 8 letters/digits (the browser selects by prefix)")
    remote = f"/{volume}/{stem}.u64"
    rec = {"t0": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "host": host, "uid": uid, "image": str(image),
           "sha256": got, "remote": remote, "expect_git_hash": expect_git_hash, "action": "ram_run"}
    if browser_factory is None:
        browser_factory = jl._telnet_browser
    if storage is None:
        from c64_test_harness.backends import ultimate64_storage as storage
    if dry_run:
        if browser_factory is jl._telnet_browser:
            jl._telnet_imports()
        rec["result"] = "dry-run ok"
        return rec
    with jl._Locked(host, {}, lock_timeout, lock_factory):
        pre = client_factory(host).get_info()
        jl.check_identity(pre, uid, PRODUCTS)
        rec["pre_info"] = pre
        storage.storage_put_file(host, remote, data, overwrite=True)
        try:
            b = browser_factory(host)
            done = False
            try:
                jl.clear_message_popups(b)
                b.go_to_directory(volume)
                b.select_entry(stem)
                if stem.lower() not in b.selected_text().lower():
                    raise jl.JtagLoadError("menu", f"selected {b.selected_text()!r}, not {stem}")
                labels = b.open_context_menu()
                if "Run Update" not in [jl._label(x) for x in labels]:
                    raise jl.JtagLoadError("menu", f"no 'Run Update' in the context menu: {labels}")
                _frame, text = b.select_framed_entry("Run Update")
                if jl._label(text.split("  ")[0]) != "Run Update":
                    raise jl.JtagLoadError("menu", f"highlight reads {text!r}, not 'Run Update'")
                log(f"telnet menu: Run Update on {remote}")
                b.press("ENTER")
                done = True
            finally:
                if done:                    # only the socket: Browser.close() navigates home with 1 s key timeouts
                    try:
                        b.backend.close()
                    except Exception:
                        pass
                else:
                    for _ in range(2):
                        try:
                            b.press("ESC")
                        except Exception:
                            break
                    try:
                        b.close()
                    except Exception:
                        pass
            # Proof of a restart: the hash changes, or (same hash) REST drops and returns.
            same = jl.hash_matches(pre.get("git_commit_hash"), jl.check_git_hash(expect_git_hash))
            end = clock() + down_timeout
            dropped, misses = False, 0
            while clock() < end:
                info = jl.try_info(host, client_factory)
                if info is None:
                    misses += 1
                    if misses >= 3:         # sustained, not one missed poll while the old app reads the file
                        dropped = True
                        break
                    sleep(1.0)
                    continue
                misses = 0
                if not same and jl.hash_matches(info.get("git_commit_hash"), expect_git_hash.lower()):
                    break
                sleep(0.25)
            rec["rest_drop_seen"] = dropped
            if same and not dropped:
                raise jl.JtagLoadError("run", f"same hash before and after and no REST drop within "
                                       f"{down_timeout:.0f} s: no proof that the RAM image started")
            rec["info"] = jl.wait_info(host, uid, expect_git_hash, up_timeout, client_factory, sleep, clock, PRODUCTS)
        finally:
            try:
                storage.storage_delete_file(host, remote, missing_ok=True)
            except Exception as e:          # a device that did not come back cannot be cleaned up; say so
                rec["cleanup_error"] = repr(e)
        rec["result"] = "ok"
        return rec


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("host")
    p.add_argument("image")
    p.add_argument("--sha256", required=True)
    p.add_argument("--expect-hash", required=True)
    p.add_argument("--volume", default="USB1")
    p.add_argument("--stem", default=None)
    p.add_argument("--lock-timeout", type=float, default=600)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--record", default=None)
    a = ap.parse_args(argv)
    try:
        out = ram_run(a.host, a.image, sha256=a.sha256, expect_git_hash=a.expect_hash, volume=a.volume, stem=a.stem,
                      lock_timeout=a.lock_timeout, dry_run=a.dry_run)
    except Exception as e:          # JtagLoadError, upstream Failure, storage and socket errors
        print(f"FAIL {type(e).__name__}: {e}", file=sys.stderr)
        if a.record:
            with open(a.record, "a") as f:
                f.write(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "action": "ram_run", "host": a.host,
                                    "image": a.image, "result": "error", "error": f"{type(e).__name__}: {e}"}) + "\n")
        return 2
    if a.record:
        with open(a.record, "a") as f:
            f.write(json.dumps(out, default=str) + "\n")
    print(json.dumps(out, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
