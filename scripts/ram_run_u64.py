#!/usr/bin/env python3
"""Run an Ultimate 64 (Elite) application from RAM, without writing flash: the "Run Update" path.

The image is a RAM-run ``.u64`` (1541ultimate branch ``jtagv/u64-ramrun``: ``tools/make_ramrun.py`` around the app's
``ultimate.app`` plus ``ramrun_stub.S``). "Run Update" on a ``.u64`` (``filetype_u2p.cc`` load_format_0) copies each
record to its load address and jumps; the stub silences the running app's DMA, copies the app to 0x0 and starts it.
Nothing is written to flash. The flashed app comes back only with a power cycle (the U64E has no software one).

Steps, all under the device's DeviceLock: identity check (bound unique_id, product "Ultimate 64 Elite"); pinned
SHA-256 of the image; FTP put to a USB volume through the harness storage helpers (they refuse /Temp); telnet menu:
select the file, context menu, "Run Update"; REST must drop and come back with the expected git hash; the file is
deleted again.

    ram_run_u64.py run HOST IMAGE --sha256 H --expect-hash G [--volume USB1] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
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


def ram_run(host: str, image, *, sha256: str, expect_git_hash: str, volume: str = "USB1", stem: str | None = None,
            lock_timeout: float = 600, down_timeout: float = 60, up_timeout: float = 180, dry_run: bool = False,
            lock_factory=jl.DeviceLock, client_factory=jl._client, browser_factory=None, storage=None,
            sleep=None, clock=None, log=jl._log) -> dict:
    sleep, clock = sleep or time.sleep, clock or time.monotonic
    uid = bound_u64(host)
    data = Path(image).read_bytes()
    got = jl.hashlib.sha256(data).hexdigest()
    if not sha256 or got != sha256.lower():
        raise jl.JtagLoadError("hash", f"{image}: SHA-256 {got} is not the pinned {sha256}")
    if not expect_git_hash:
        raise jl.JtagLoadError("hash", "an expected git hash is required: it is the only proof the RAM image runs")
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
            try:
                b.go_to_directory(volume)
                b.select_entry(stem)
                if stem.lower() not in b.selected_text().lower():
                    raise jl.JtagLoadError("menu", f"selected {b.selected_text()!r}, not {stem}")
                labels = b.open_context_menu()
                if "Run Update" not in [x.split("||", 1)[0].strip() for x in labels]:
                    b.press("ESC")
                    raise jl.JtagLoadError("menu", f"no 'Run Update' in the context menu: {labels}")
                log(f"telnet menu: Run Update on {remote}")
                b.choose_overlay_item(labels, "Run Update")
            finally:
                try:
                    b.close()
                except Exception:
                    pass
            end = clock() + down_timeout
            while jl.try_info(host, client_factory) is not None:
                if clock() > end:
                    raise jl.JtagLoadError("run", f"REST still answers {down_timeout:.0f} s after Run Update")
                sleep(0.5)
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
    except jl.JtagLoadError as e:
        print(f"FAIL {e}", file=sys.stderr)
        return 2
    if a.record:
        with open(a.record, "a") as f:
            f.write(json.dumps(out, default=str) + "\n")
    print(json.dumps(out, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
