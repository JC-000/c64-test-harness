#!/usr/bin/env python3
"""Read-only reconnaissance probe for Ultimate 64 / Ultimate II REST API.

Queries the device's HTTP API (v1) and prints a structured summary:
  - Device identity (product, firmware, fpga, core, hostname, unique_id)
  - All 19 configuration categories
  - Key enums (CPU Speed turbo table, REU Size, SID addressing)
  - Drive enumeration

All requests are strictly GET. No mutation, no reset, no reboot.

Usage:
    python3 scripts/probe_u64.py --host 192.168.1.81
    python3 scripts/probe_u64.py --host 192.168.1.81 --password secret
    python3 scripts/probe_u64.py --host 192.168.1.81 --raw-dir /tmp/u64_dump

Zero external deps (urllib only).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


DEFAULT_HOST = "192.168.1.81"
TIMEOUT = 8.0

# Authoritative list of config categories exposed by /v1/configs
CATEGORIES = [
    "Audio Mixer",
    "SID Sockets Configuration",
    "UltiSID Configuration",
    "SID Addressing",
    "U64 Specific Settings",
    "C64 and Cartridge Settings",
    "Clock Settings",
    "SoftIEC Drive Settings",
    "Printer Settings",
    "Network Settings",
    "Ethernet Settings",
    "WiFi settings",
    "Tape Settings",
    "LED Strip Settings",
    "Drive A Settings",
    "Drive B Settings",
    "Data Streams",
    "Modem Settings",
    "User Interface Settings",
]


def _slug(name: str) -> str:
    return name.lower().replace(" ", "_")


def _get(host: str, path: str, password: str | None) -> tuple[int, bytes]:
    """HTTP GET. Returns (status, body_bytes). Never raises on HTTP errors."""
    url = f"http://{host}{path}"
    req = urllib.request.Request(url, method="GET")
    if password:
        req.add_header("X-Password", password)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() if e.fp else b""


def _get_json(host: str, path: str, password: str | None) -> Any:
    status, body = _get(host, path, password)
    if status != 200:
        return {"_http_status": status, "_body": body.decode("utf-8", "replace")}
    try:
        return json.loads(body.decode("utf-8"))
    except json.JSONDecodeError:
        return {"_parse_error": True, "_body": body.decode("utf-8", "replace")}


def _dump_raw(raw_dir: str, slug: str, data: Any) -> None:
    os.makedirs(raw_dir, exist_ok=True)
    with open(os.path.join(raw_dir, f"{slug}.json"), "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def _fetch_category(host: str, category: str, password: str | None) -> Any:
    enc = urllib.parse.quote(category, safe="")
    return _get_json(host, f"/v1/configs/{enc}", password)


def _fetch_item(host: str, category: str, item: str, password: str | None) -> Any:
    enc_cat = urllib.parse.quote(category, safe="")
    enc_item = urllib.parse.quote(item, safe="")
    return _get_json(host, f"/v1/configs/{enc_cat}/{enc_item}", password)


def _unwrap(payload: Any, category: str) -> dict:
    """Return the inner dict for a category, stripped of the category wrapper."""
    if not isinstance(payload, dict):
        return {}
    inner = payload.get(category)
    return inner if isinstance(inner, dict) else {}


def print_header(text: str) -> None:
    print()
    print("=" * 72)
    print(text)
    print("=" * 72)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only probe of an Ultimate 64 / Ultimate II REST API."
    )
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"Device host/IP (default: {DEFAULT_HOST})")
    parser.add_argument("--password", default=None,
                        help="Optional password; sent as X-Password header.")
    parser.add_argument("--raw-dir", default=None,
                        help="If set, dump every raw JSON response into this dir.")
    args = parser.parse_args()

    host = args.host
    pw = args.password
    raw_dir = args.raw_dir

    print_header(f"Ultimate device probe: http://{host}")

    version = _get_json(host, "/v1/version", pw)
    info = _get_json(host, "/v1/info", pw)
    configs = _get_json(host, "/v1/configs", pw)
    drives = _get_json(host, "/v1/drives", pw)

    if raw_dir:
        _dump_raw(raw_dir, "_version", version)
        _dump_raw(raw_dir, "_info", info)
        _dump_raw(raw_dir, "_configs", configs)
        _dump_raw(raw_dir, "_drives", drives)

    # Identity
    print(f"API version      : {version.get('version', '?')}")
    print(f"product          : {info.get('product', '?')}")
    print(f"firmware_version : {info.get('firmware_version', '?')}")
    print(f"fpga_version     : {info.get('fpga_version', '?')}")
    print(f"core_version     : {info.get('core_version', '?')}")
    print(f"hostname         : {info.get('hostname', '?')}")
    print(f"unique_id        : {info.get('unique_id', '?')}")

    # Categories
    cats = configs.get("categories", []) if isinstance(configs, dict) else []
    print(f"\nconfig categories ({len(cats)}):")
    for c in cats:
        print(f"  - {c}")

    # Fetch every category
    category_data: dict[str, dict] = {}
    for cat in CATEGORIES:
        data = _fetch_category(host, cat, pw)
        if raw_dir:
            _dump_raw(raw_dir, _slug(cat), data)
        category_data[cat] = _unwrap(data, cat)

    # U64 Specific: CPU Speed enum (authoritative turbo table)
    print_header("U64 Specific Settings  ->  CPU Speed (turbo table)")
    cpu_item = _fetch_item(host, "U64 Specific Settings", "CPU Speed", pw)
    if raw_dir:
        _dump_raw(raw_dir, "_item_cpu_speed", cpu_item)
    cs = _unwrap(cpu_item, "U64 Specific Settings").get("CPU Speed", {})
    print(f"current : {cs.get('current')!r}")
    print(f"default : {cs.get('default')!r}")
    print(f"values  : {cs.get('values')}")
    print("Interpretation: CPU speed multiplier in MHz (approx). Values are")
    print("strings, right-aligned to width 2. Max on this device is 48 MHz.")

    # C64 and Cartridge Settings
    print_header("C64 and Cartridge Settings  ->  Cartridge / REU Size")
    cart_item = _fetch_item(host, "C64 and Cartridge Settings", "Cartridge", pw)
    reu_item = _fetch_item(host, "C64 and Cartridge Settings", "REU Size", pw)
    if raw_dir:
        _dump_raw(raw_dir, "_item_cartridge", cart_item)
        _dump_raw(raw_dir, "_item_reu_size", reu_item)
    cart = _unwrap(cart_item, "C64 and Cartridge Settings").get("Cartridge", {})
    reu = _unwrap(reu_item, "C64 and Cartridge Settings").get("REU Size", {})
    print(f"Cartridge presets : {cart.get('presets')}")
    print(f"Cartridge current : {cart.get('current')!r}")
    print(f"REU Size current  : {reu.get('current')!r}")
    print(f"REU Size default  : {reu.get('default')!r}")
    print(f"REU Size values   : {reu.get('values')}")
    cc = category_data.get("C64 and Cartridge Settings", {})
    print(f"RAM Expansion Unit: {cc.get('RAM Expansion Unit')!r} (master enable)")

    # SID config
    print_header("SID Config (Sockets / UltiSID / Addressing)")
    sk = category_data.get("SID Sockets Configuration", {})
    us = category_data.get("UltiSID Configuration", {})
    sa = category_data.get("SID Addressing", {})
    print("Sockets:")
    for k, v in sk.items():
        print(f"  {k:32} = {v!r}")
    print("UltiSID:")
    for k, v in us.items():
        print(f"  {k:32} = {v!r}")
    print("Addressing:")
    for k, v in sa.items():
        print(f"  {k:32} = {v!r}")

    # Drives
    print_header("Drive Enumeration  (/v1/drives)")
    if isinstance(drives, dict):
        for entry in drives.get("drives", []):
            for slot, spec in entry.items():
                print(f"slot {slot}:")
                for k, v in spec.items():
                    print(f"  {k:18} = {v!r}")

    da = category_data.get("Drive A Settings", {})
    db = category_data.get("Drive B Settings", {})
    print("\nDrive A Settings:")
    for k, v in da.items():
        print(f"  {k:26} = {v!r}")
    print("Drive B Settings:")
    for k, v in db.items():
        print(f"  {k:26} = {v!r}")

    # Brief summary of remaining categories
    print_header("Remaining categories (current values)")
    remaining = [
        "Audio Mixer", "Clock Settings", "SoftIEC Drive Settings",
        "Printer Settings", "Network Settings", "Ethernet Settings",
        "WiFi settings", "Tape Settings", "LED Strip Settings",
        "Data Streams", "Modem Settings", "User Interface Settings",
    ]
    for cat in remaining:
        d = category_data.get(cat, {})
        print(f"\n[{cat}]  ({len(d)} items)")
        for k, v in d.items():
            print(f"  {k:36} = {v!r}")

    print_header("Probe complete (read-only)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
