#!/usr/bin/env python3
"""Cold power cycle of an Ultimate through a switched outlet (a smart plug with a local HTTP API).

    power_outlet.py cycle HOST [--off-s 10] [--expect-hash G] [--no-graceful] [--c64u-poweroff] [--dry-run]
    power_outlet.py status HOST

Binding, in the local bench bindings file (~/.config/c64-test-harness/bench.toml, see _bench_bindings.py) under
the table of the unique_id that the alias map (devices.toml) folds HOST to:

    [devices.abc123]
    outlet = "http://<plug address>"    # static DHCP lease
    outlet_kind = "shelly-gen2"         # or "tasmota"

Sequence (from the #1042 review: graceful poweroff, then 10 s with the outlet off), all under the device's
DeviceLock (allow_nested, so a driver that holds it can call this):
  1. read /v1/info; if REST answers and its product allows it (see below): PUT /v1/machine:poweroff through the
     harness client (confirm_irrecoverable=True), then wait briefly for REST to go away;
  2. outlet OFF, read back OFF; wait off_s (at least 10 s);
  3. outlet ON, read back ON;
  4. wait for /v1/info with the bound unique_id (and the expected hash, if given).
The outlet is never left OFF by this code: any error after step 2 switches it back ON before raising.

The poweroff decision is made from the /v1/info product read in step 1, under the lock, never from a device id:
  * "Ultimate 64", "Ultimate 64 Elite": the poweroff is sent unless --no-graceful is given, as before this
    decision moved to the product (the #1042 sequence). Basis, by source only: both are U64 == 1 builds that switch
    off through the same power register, and the board revision picks which name it reports. That such a board
    powers up again after a poweroff and an outlet cycle has NOT been measured here. --c64u-poweroff is refused.
  * "C64 Ultimate", "Ultimate 64-II" (the C64U class): poweroff() is unreachable on the C64U (CLAUDE.md,
    hardware-safety rule 7), so the outlet is just cut. After AC returns, its ESP32 module decides whether the
    machine powers up, by its power-on mode (software/u64ctrl/main/power_state.c:130-158: ON, LAST_STATE, else
    OFF). A poweroff stores last-state OFF, so after one the C64U comes back only with power-on mode ON.
    --c64u-poweroff sends it anyway, for a module known to be set to power-on mode ON; graceful=True without it is
    refused. This code never changes that mode.
  * any other product, or none, or REST not answering: the outlet is just cut. An explicit graceful=True or
    --c64u-poweroff is refused, since the poweroff it asks for cannot be sent safely. "C64 Ultimate (MK1)" is in
    this group on purpose: it is a COMMODORE build on the same U64 == 1 hardware, but nothing about its power-up
    after a poweroff is known either way.
Every refusal comes before the outlet is switched. The record keeps "graceful" (poweroff sent or not) and
"c64u_poweroff" (sent to a C64U-class product) as the resolved decision, adds "poweroff" (same as "graceful") and
"product", and puts the arguments under "requested". A dry run reads the plug only, never the Ultimate, so it
cannot make the decision: its resolved keys are null.
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
from _bench_bindings import BenchBindingError, bench_file_path, bench_table  # noqa: E402

MIN_OFF_S = 10.0
#: Products that get the poweroff by default: U64 == 1 builds sharing one power register, by source. Their power-up
#: after a poweroff and an outlet cycle is NOT measured. "C64 Ultimate (MK1)" (same hardware) is left out on purpose.
GRACEFUL_PRODUCTS = frozenset({"Ultimate 64", "Ultimate 64 Elite"})
#: The C64U class (U64 == 2 builds; ultimate64_firmware_flash._PRODUCT_FAMILY "c64u"): an ESP32 power controller
#: stores last-state OFF on a poweroff.
C64U_PRODUCTS = frozenset(jl.PRODUCTS)


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
    """{"outlet": url, "outlet_kind": kind} from bench.toml for this unique_id; refuse if absent, or if the file is
    missing or broken."""
    try:
        tbl = bench_table(uid, path)
    except BenchBindingError as e:
        raise OutletError(str(e)) from None
    if not tbl.get("outlet") or tbl.get("outlet_kind") not in KINDS:
        raise OutletError(f"no outlet bound to device {uid} in {path or bench_file_path()} (need outlet = URL, "
                          f"outlet_kind in {sorted(KINDS)})")
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


def _decide_poweroff(uid: str, product: str | None, *, rest_up: bool, graceful: bool | None, c64u_poweroff: bool,
                    log) -> bool:
    """Whether to send poweroff() before the cut; raises OutletError (before any switching) for a request that
    cannot be honoured safely. graceful=False with c64u_poweroff=True was refused before the lock."""
    if not isinstance(product, str):
        product = None                              # missing or malformed (a set lookup must not raise on a list)
    if not rest_up:
        if graceful or c64u_poweroff:
            raise OutletError(f"{uid} does not answer /v1/info, so no poweroff can be sent and its product is "
                              "unknown; refusing the explicit poweroff request. Without it the outlet alone is cut.")
        log(f"{uid} does not answer /v1/info: no poweroff, cutting the outlet")
        return False
    if product in GRACEFUL_PRODUCTS:
        if c64u_poweroff:
            raise OutletError(f"{uid} is a {product!r}, not a C64U; --c64u-poweroff does not apply")
        return graceful is not False
    if product in C64U_PRODUCTS:
        if graceful and not c64u_poweroff:
            raise OutletError(f"{uid} is a {product!r} (C64U class): poweroff() is unreachable there (hardware-safety "
                              "rule 7), so a graceful cycle is refused; the outlet alone is cut by default. Pass "
                              "c64u_poweroff=True (--c64u-poweroff) only when its power-on mode is ON.")
        if not c64u_poweroff:
            log(f"{uid} is a {product!r} (C64U class): no poweroff (rule 7), cutting the outlet")
        return c64u_poweroff
    if graceful or c64u_poweroff:
        raise OutletError(f"{uid} reports product {product!r}, which is not known to power up again after a "
                          "poweroff and an outlet cycle; refusing the explicit poweroff request. Without it the "
                          "outlet alone is cut.")
    log(f"{uid} reports product {product!r}, not known to come back after a poweroff: no poweroff, cutting the outlet")
    return False


def outlet_cycle(host: str, *, off_s: float = MIN_OFF_S, graceful: bool | None = None, c64u_poweroff: bool = False,
                 expect_git_hash: str | None = None,
                 up_timeout: float = 240, down_timeout: float = 30, lock_timeout: float = 600, dry_run: bool = False,
                 binding: dict | None = None, outlet_factory=None, lock_factory=jl.DeviceLock,
                 client_factory=jl._client, sleep=None, clock=None, log=jl._log) -> dict:
    sleep, clock = sleep or time.sleep, clock or time.monotonic
    if off_s < MIN_OFF_S:
        raise OutletError(f"off time {off_s} s is under the {MIN_OFF_S:.0f} s minimum")
    if c64u_poweroff and graceful is False:
        raise OutletError("graceful=False (--no-graceful) and c64u_poweroff=True (--c64u-poweroff) contradict "
                          "each other; give one")
    uid = _uid(host)
    b = binding or outlet_binding(uid)
    if expect_git_hash is not None:
        expect_git_hash = jl.check_git_hash(expect_git_hash)
    outlet = (outlet_factory or (lambda bb: KINDS[bb["outlet_kind"]](bb["outlet"], host=host)))(b)
    rec = {"t0": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "host": host, "uid": uid, "action": "outlet_cycle",
           "outlet": b["outlet"], "off_s": off_s,
           "requested": {"graceful": graceful, "c64u_poweroff": c64u_poweroff},
           # the resolved decision, made under the lock from the /v1/info product (lane scripts read these keys)
           "graceful": None, "c64u_poweroff": None, "poweroff": None, "product": None}
    if dry_run:
        rec["outlet_on"] = outlet.is_on()             # reads the plug only, never the Ultimate: undecided
        rec["result"] = "dry-run ok"
        return rec
    with jl._Locked(host, {}, lock_timeout, lock_factory):
        pre = jl.try_info(host, client_factory)
        rec["pre_info"] = pre
        if pre is not None and str(pre.get("unique_id", "")).upper() != uid:
            raise OutletError(f"{host} answers as {pre.get('unique_id')}, bound is {uid}: wrong plug mapping?")
        product = None if pre is None else pre.get("product")
        rec["product"] = product
        poweroff = _decide_poweroff(uid, product, rest_up=pre is not None, graceful=graceful,
                                    c64u_poweroff=c64u_poweroff, log=log)
        rec["poweroff"] = rec["graceful"] = poweroff
        rec["c64u_poweroff"] = poweroff and product in C64U_PRODUCTS
        if poweroff:
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
            products = tuple(C64U_PRODUCTS | GRACEFUL_PRODUCTS) + ((product,) if isinstance(product, str) else ())
            rec["info"] = jl.wait_info(host, uid, expect_git_hash, up_timeout, client_factory, sleep, clock,
                                       products=products)   # the board that answered before may answer after
        except jl.JtagLoadError as e:
            if e.stage != "network":                   # it answered, as the wrong board: no power-up question
                raise OutletError(str(e)) from e
            hint = (" The C64U powers up after AC restore by its power-on mode (power_state.c:130-158)"
                    + ("; the poweroff stored last-state OFF, so only mode ON brings it back." if poweroff else ".")
                    if isinstance(product, str) and product in C64U_PRODUCTS else
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
    poweroff = c.add_mutually_exclusive_group()
    poweroff.add_argument("--no-graceful", action="store_true")
    poweroff.add_argument("--c64u-poweroff", action="store_true",
                   help="C64U class only (product 'C64 Ultimate' or 'Ultimate 64-II', read at run time): send "
                        "poweroff before cutting the outlet. Its ESP32 then stores last-state OFF, so the C64U "
                        "powers up again only if its power-on mode is ON; check that first.")
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
