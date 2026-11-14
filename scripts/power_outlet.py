#!/usr/bin/env python3
"""Cold power cycle of an Ultimate through a switched outlet (a smart plug with a local HTTP API).

    power_outlet.py cycle HOST [--off-s 10] [--expect-hash G] [--no-graceful] [--c64u-poweroff] [--dry-run]
    power_outlet.py status HOST

Binding, in ~/.config/c64-test-harness/devices.toml under the device's table:

    [devices.5d2518]
    hosts = [...]
    outlet = "http://<plug address>"    # static DHCP lease
    outlet_kind = "shelly-gen2"         # or "tasmota"

Sequence (from the #1042 review: graceful poweroff, then 10 s with the outlet off), all under the device's
DeviceLock (allow_nested, so a driver that holds it can call this):
  1. if REST answers: PUT /v1/machine:poweroff through the harness client (confirm_irrecoverable=True), then wait
     briefly for REST to go away. Not on a C64U unless --c64u-poweroff is given (see below);
  2. outlet OFF, read back OFF; wait off_s (at least 10 s);
  3. outlet ON, read back ON;
  4. wait for /v1/info with the bound unique_id (and the expected hash, if given).
The outlet is never left OFF by this code: any error after step 2 switches it back ON before raising.

C64U: poweroff() is unreachable on the C64U (CLAUDE.md, hardware-safety rule 7), so step 1 is skipped there and
the outlet is just cut. After AC returns, its ESP32 module decides whether the machine powers up, by its power-on
mode (software/u64ctrl/main/power_state.c:130-158: ON, LAST_STATE, else OFF). A poweroff stores last-state OFF, so
after one the C64U comes back only with power-on mode ON. --c64u-poweroff sends it anyway, for a module known to be
set to power-on mode ON; graceful=True on a C64U without it is refused. This code never changes that mode.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import jtag_load as jl  # noqa: E402  (harness src on sys.path, DeviceLock, try_info, wait_info, check_git_hash)
from _u64_host import require_device_lock  # noqa: E402

MIN_OFF_S = 10.0
C64U_IDS = {"5D2518"}


class OutletError(RuntimeError):
    pass


def _is_status_url(url: str) -> bool:
    """Shelly Gen2 ``/rpc/Switch.GetStatus`` (``id`` only) or Tasmota ``/cm?cmnd=Power`` with no argument."""
    u = urllib.parse.urlsplit(url)
    q = urllib.parse.parse_qs(u.query, keep_blank_values=True)
    if u.path.endswith("/rpc/Switch.GetStatus"):
        return set(q) <= {"id"}
    if u.path.endswith("/cm"):
        return q == {"cmnd": ["Power"]}
    return False


def _read_plug(url: str, timeout: float) -> dict:
    """GET a plug status URL. Reads the plug only; never switches power (the lock scan excludes it by name).

    Anything but a status endpoint is refused: a Shelly Gen2 Switch.Set is a GET as well."""
    if not _is_status_url(url):
        raise OutletError(f"{url} is not a plug status URL; switching goes through _switch, under the DeviceLock")
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _switch(host: str, url: str, timeout: float) -> dict:
    """GET a plug switch URL: this cuts or restores the power of *host*, so its DeviceLock must be held."""
    require_device_lock(host, normalize=jl.normalize_device_host)
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


class _Plug:
    """Builds the URLs; reads go through _read_plug, switching through _switch (which needs *host*'s lock).
    *fetch* replaces both, for tests."""

    def __init__(self, base: str, timeout: float = 5.0, fetch=None, host: str | None = None):
        self.base, self.timeout, self.fetch, self.host = base.rstrip("/"), timeout, fetch, host

    def _get(self, url: str) -> dict:
        return self.fetch(url) if self.fetch else _read_plug(url, self.timeout)

    def set(self, on: bool) -> None:
        url = self.switch_url(on)
        if self.fetch:
            self.fetch(url)
        else:
            _switch(self.host, url, self.timeout)


class ShellyGen2(_Plug):
    """Shelly Plus/Pro (Gen2+) RPC over HTTP GET: Switch.Set / Switch.GetStatus, id 0."""

    def switch_url(self, on: bool) -> str:
        return f"{self.base}/rpc/Switch.Set?id=0&on={'true' if on else 'false'}"

    def is_on(self) -> bool:
        return bool(self._get(f"{self.base}/rpc/Switch.GetStatus?id=0")["output"])


class Tasmota(_Plug):
    """Tasmota HTTP commands: /cm?cmnd=Power%20On|Off and /cm?cmnd=Power -> {"POWER": "ON"|"OFF"}."""

    def switch_url(self, on: bool) -> str:
        return f"{self.base}/cm?cmnd=Power%20{'On' if on else 'Off'}"

    def is_on(self) -> bool:
        return str(self._get(f"{self.base}/cm?cmnd=Power").get("POWER", "")).upper() == "ON"


KINDS = {"shelly-gen2": ShellyGen2, "tasmota": Tasmota}


def outlet_binding(uid: str, path: Path | None = None) -> dict:
    """{"outlet": url, "outlet_kind": kind} from devices.toml for this unique_id; refuse if absent."""
    import tomllib
    from c64_test_harness.backends.device_aliases import aliases_file_path
    p = path or aliases_file_path()
    try:
        data = tomllib.loads(Path(p).read_text())
    except FileNotFoundError:
        data = {}
    tbl = data.get("devices", {}).get(uid.lower()) or data.get("devices", {}).get(uid.upper()) or {}
    if not tbl.get("outlet") or tbl.get("outlet_kind") not in KINDS:
        raise OutletError(f"no outlet bound to device {uid} in {p} (need outlet = URL, outlet_kind in {sorted(KINDS)})")
    return {"outlet": tbl["outlet"], "outlet_kind": tbl["outlet_kind"]}


def _uid(host: str) -> str:
    key = jl.normalize_device_host(host)
    if not key.startswith("uid-"):
        raise OutletError(f"{host} is not in devices.toml, so it has no outlet binding")
    return key[4:].upper()


def _set_checked(outlet, on: bool, sleep, tries: int = 3) -> None:
    for _ in range(tries):
        outlet.set(on)
        sleep(0.5)
        if outlet.is_on() == on:
            return
    raise OutletError(f"outlet does not read back {'ON' if on else 'OFF'}")


def outlet_cycle(host: str, *, off_s: float = MIN_OFF_S, graceful: bool | None = None, c64u_poweroff: bool = False,
                 expect_git_hash: str | None = None,
                 up_timeout: float = 240, down_timeout: float = 30, lock_timeout: float = 600, dry_run: bool = False,
                 binding: dict | None = None, outlet_factory=None, lock_factory=jl.DeviceLock,
                 client_factory=jl._client, sleep=None, clock=None, log=jl._log) -> dict:
    sleep, clock = sleep or time.sleep, clock or time.monotonic
    if off_s < MIN_OFF_S:
        raise OutletError(f"off time {off_s} s is under the {MIN_OFF_S:.0f} s minimum")
    uid = _uid(host)
    if uid in C64U_IDS:
        if graceful and not c64u_poweroff:
            raise OutletError(f"{uid} is a C64U: poweroff() is unreachable there (hardware-safety rule 7), so a "
                              "graceful cycle is refused; the outlet alone is cut by default. Pass "
                              "c64u_poweroff=True (--c64u-poweroff) only when its power-on mode is ON.")
        graceful = c64u_poweroff and graceful is not False
    elif c64u_poweroff:
        raise OutletError(f"{uid} is not a C64U; --c64u-poweroff does not apply")
    else:
        graceful = graceful is not False
    b = binding or outlet_binding(uid)
    if expect_git_hash is not None:
        expect_git_hash = jl.check_git_hash(expect_git_hash)
    outlet = (outlet_factory or (lambda bb: KINDS[bb["outlet_kind"]](bb["outlet"], host=host)))(b)
    rec = {"t0": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "host": host, "uid": uid, "action": "outlet_cycle",
           "outlet": b["outlet"], "off_s": off_s, "graceful": graceful,
           "c64u_poweroff": graceful and uid in C64U_IDS}
    if dry_run:
        rec["outlet_on"] = outlet.is_on()             # reads the plug only, never the Ultimate
        rec["result"] = "dry-run ok"
        return rec
    with jl._Locked(host, {}, lock_timeout, lock_factory):
        pre = jl.try_info(host, client_factory)
        rec["pre_info"] = pre
        if pre is not None and str(pre.get("unique_id", "")).upper() != uid:
            raise OutletError(f"{host} answers as {pre.get('unique_id')}, bound is {uid}: wrong plug mapping?")
        if graceful and pre is not None:
            try:
                client_factory(host).poweroff(confirm_irrecoverable=True)
                log(f"graceful poweroff sent to {uid}")
            except Exception as e:                     # a dead app is exactly when the outlet is needed
                log(f"graceful poweroff failed ({type(e).__name__}: {e}); cutting power anyway")
            end = clock() + down_timeout
            while jl.try_info(host, client_factory) is not None and clock() < end:
                sleep(1.0)
        switched_off = False
        try:
            log(f"outlet {b['outlet']} OFF for {off_s:.0f} s")
            switched_off = True
            _set_checked(outlet, False, sleep)
            sleep(off_s)
            _set_checked(outlet, True, sleep)
            switched_off = False
            log("outlet ON")
        finally:
            if switched_off:                           # never leave the device without power
                try:
                    outlet.set(True)
                except Exception as e:
                    log(f"COULD NOT SWITCH THE OUTLET BACK ON: {e}")
        try:
            rec["info"] = jl.wait_info(host, uid, expect_git_hash, up_timeout, client_factory, sleep, clock,
                                       products=jl.PRODUCTS + ("Ultimate 64 Elite", "Ultimate 64"))
        except jl.JtagLoadError as e:
            hint = (" The C64U powers up after AC restore by its power-on mode (power_state.c:130-158)"
                    + ("; the poweroff stored last-state OFF, so only mode ON brings it back." if graceful else ".")
                    if uid in C64U_IDS else
                    " Check whether this device powers up by itself when AC returns.")
            raise OutletError(f"{e}.{hint}") from e
        rec["result"] = "ok"
        return rec


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("cycle")
    c.add_argument("host")
    c.add_argument("--off-s", type=float, default=MIN_OFF_S)
    c.add_argument("--expect-hash", default=None)
    c.add_argument("--no-graceful", action="store_true")
    c.add_argument("--c64u-poweroff", action="store_true",
                   help="C64U only: send poweroff before cutting the outlet. Its ESP32 then stores last-state OFF, "
                        "so the C64U powers up again only if its power-on mode is ON; check that first.")
    c.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("status")
    s.add_argument("host")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "status":
            uid = _uid(a.host)
            b = outlet_binding(uid)
            out = {"uid": uid, **b, "on": KINDS[b["outlet_kind"]](b["outlet"]).is_on()}
        else:
            out = outlet_cycle(a.host, off_s=a.off_s, graceful=False if a.no_graceful else None,
                               c64u_poweroff=a.c64u_poweroff, expect_git_hash=a.expect_hash, dry_run=a.dry_run)
    except Exception as e:
        print(f"FAIL {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    print(json.dumps(out, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
