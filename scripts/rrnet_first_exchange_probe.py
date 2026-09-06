#!/usr/bin/env python3
"""Issue #222 reproducer: why does the first exchange of an RR-Net session miss?

Paired arms on a U64 with an external RR-Net cabled point-to-point to the
host NIC (RRNET_IFACE, default en4; host 10.0.66.1/24, C64 10.0.66.201,
MAC 02:c6:40:00:00:01).  Every trial = one "first ping" plus a second ping
1 s later, with:

* LineST (PP 0x0134) + RxMISS (PP 0x0130) read ON THE 6510 microseconds
  before the ping routine transmits (a prefix that falls into the library
  routine), and again right after it returns;
* a host thread sampling ``ifconfig <iface>`` status every 100 ms and
  logging every transition (does BIOCPROMISC / reset / init bounce the
  link?);
* a host BpfCapture counting echo request / reply for the trial's seq;
* ARP-first in every trial (#212 excluded): separate build_tx_code ARP
  routine + 0.3 s (the #218 recipe) or, with ARP=inline, the library's
  arp_frame_buf= in the same routine.

Arms (interleaved, order rotated each round; ROUNDS=6 ARMS=FL,FE,FW,SL,SE):

* FL  fresh session: PUT External, reset, READY, settle, chip init, 0.5 s,
  capture opened, 0.5 s, ping.  (the original recipe)
* FE  fresh session, capture opened, 5 s, ping.
* FW  fresh session, 5 s after init, capture opened, 0.5 s, ping.
* SL  steady: chip left up, no reset; capture re-opened, 0.5 s, ping.
* SE  steady; capture re-opened, 5 s, ping.

Measured 2026-09-05 on the U64E (fw 3.15 fork): FL 6/6, FE 3/6, FW 3/6,
SL 6/6, SE 6/6; every miss had LinkOK, both frames on the wire, no link
transition and RxMISS +1 -- stale frames in the chip's RX queue (the
host's DHCP DISCOVER broadcasts) make the chip discard the reply.  The fix
is drain_first=True on the ping builders; the live test is
tests/test_first_exchange_live.py.  Needs the DeviceLock (create_manager
takes it); Cartridge Preference is set to External and restored.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

os.environ.setdefault("C64_BACKEND", "u64")
HOST = os.environ.setdefault("U64_HOST", "10.43.23.81")
os.environ.setdefault("U64_UNLOCKED_CLIENT_WARNING", "0")

from c64_test_harness import create_manager  # noqa: E402
from c64_test_harness.bridge_ping import (  # noqa: E402
    CS8900A_RXCTL_VALUE_IP65, PPDATA_HI, PPDATA_LO, PPTR_HI, PPTR_LO,
    build_arp_request_frame, build_echo_request_frame, build_ping_and_wait_tod_code,
    build_tx_code, cs8900a_linectl_or_inline_code, cs8900a_rxctl_inline_code,
    cs8900a_set_mac_inline_code, _clockport_enable_bytes,
)
from c64_test_harness.capture import BpfCapture  # noqa: E402
from c64_test_harness.ethernet import parse_mac  # noqa: E402
from c64_test_harness.execute import load_code, run_subroutine  # noqa: E402
from c64_test_harness.memory import read_bytes, write_bytes  # noqa: E402
from c64_test_harness.screen import wait_for_text  # noqa: E402

CAT = "C64 and Cartridge Settings"
ITEM = "Cartridge Preference"
IFACE = os.environ.get("RRNET_IFACE", "en4")
CODE, TX_BUF, ARP_BUF, RX_BUF, RESULT, STAT = 0x4000, 0x5000, 0x5080, 0x5100, 0x5300, 0x5310
C64_MAC = parse_mac("02:c6:40:00:00:01")
C64_IP = bytes([10, 0, 66, 201])
HOST_IP = bytes([10, 0, 66, 1])
PREFIX_LEN = 0  # filled by build_prefix
T0 = time.monotonic()


def rel() -> str:
    return f"{time.monotonic() - T0:8.2f}"


def now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]


def host_mac() -> bytes:
    out = subprocess.run(["ifconfig", IFACE], capture_output=True, text=True).stdout
    for ln in out.splitlines():
        if "ether " in ln:
            return parse_mac(ln.split()[1])
    raise SystemExit(f"no ether on {IFACE}")


def link_status() -> str:
    out = subprocess.run(["ifconfig", IFACE], capture_output=True, text=True).stdout
    for ln in out.splitlines():
        if "status:" in ln:
            return ln.split(":", 1)[1].strip()
    return "?"


def arp_state(ip: str) -> str:
    out = subprocess.run(["arp", "-n", ip], capture_output=True, text=True).stdout.strip()
    if "no entry" in out:
        return "none"
    if "incomplete" in out:
        return "incomplete"
    parts = out.split()
    try:
        return parts[parts.index("at") + 1]
    except (ValueError, IndexError):
        return out


def pp_read_bytes(off: int, dst: int) -> bytes:
    return bytes([
        0xA9, off & 0xFF, 0x8D, PPTR_LO & 0xFF, PPTR_LO >> 8,
        0xA9, off >> 8, 0x8D, PPTR_HI & 0xFF, PPTR_HI >> 8,
        0xAD, PPDATA_LO & 0xFF, PPDATA_LO >> 8, 0x8D, dst & 0xFF, dst >> 8,
        0xAD, PPDATA_HI & 0xFF, PPDATA_HI >> 8, 0x8D, (dst + 1) & 0xFF, (dst + 1) >> 8,
    ])


def build_prefix(ping_addr: int) -> bytes:
    """clockport on; LineST -> STAT, RxMISS -> STAT+2; JMP ping routine."""
    return (_clockport_enable_bytes() + pp_read_bytes(0x0134, STAT)
            + pp_read_bytes(0x0130, STAT + 2)
            + bytes([0x4C, ping_addr & 0xFF, ping_addr >> 8]))


def build_stat_read() -> bytes:
    """clockport on; LineST -> STAT+4, RxMISS -> STAT+6; RTS (post-trial)."""
    return (_clockport_enable_bytes() + pp_read_bytes(0x0134, STAT + 4)
            + pp_read_bytes(0x0130, STAT + 6) + b"\x60")


def w16(b: bytes, i: int) -> int:
    return b[i] | (b[i + 1] << 8)


class LinkWatch(threading.Thread):
    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.stop = threading.Event()
        self.events: list[tuple[float, str]] = []
        self.last = link_status()
        self.events.append((time.monotonic() - T0, self.last))

    def run(self) -> None:
        while not self.stop.is_set():
            s = link_status()
            if s != self.last:
                self.events.append((time.monotonic() - T0, s))
                print(f"[{now()} {rel()}] LINK {self.last} -> {s}", flush=True)
                self.last = s
            time.sleep(0.1)

    def since(self, t_rel: float) -> list[tuple[float, str]]:
        return [(t, s) for t, s in self.events if t >= t_rel]


def main() -> int:
    hmac = host_mac()
    rounds = int(os.environ.get("ROUNDS", "6"))
    arms = os.environ.get("ARMS", "FL,FE,FW,SL,SE").split(",")
    arp_mode = os.environ.get("ARP", "separate")
    deadline_tenths = int(os.environ.get("DEADLINE_TENTHS", "20"))
    settle = float(os.environ.get("SETTLE", "6.5"))
    init = (cs8900a_rxctl_inline_code(CS8900A_RXCTL_VALUE_IP65)
            + cs8900a_linectl_or_inline_code()
            + cs8900a_set_mac_inline_code(C64_MAC) + b"\x60")
    prefix_len = len(build_prefix(0))
    ping_addr = CODE + prefix_len
    rows = []
    seq = 0
    watch = LinkWatch()
    watch.start()
    print(f"[{now()}] host {IFACE} {hmac.hex(':')} link={watch.last} arp .201={arp_state('10.0.66.201')} "
          f"arms={arms} rounds={rounds} arp={arp_mode} deadline={deadline_tenths / 10}s")

    def fresh_session(t, client) -> None:
        t_put = time.monotonic() - T0
        client.set_config_item(CAT, ITEM, "External")
        time.sleep(0.5)
        print(f"[{now()} {rel()}] PUT External done; reset", flush=True)
        t.reset()
        assert wait_for_text(t, "READY.", timeout=25.0, poll_interval=0.3, verbose=False)
        print(f"[{now()} {rel()}] READY.; settle {settle}s", flush=True)
        time.sleep(settle)
        load_code(t, CODE, init)
        run_subroutine(target, CODE, timeout=10.0, poll_cadence=0.005)
        print(f"[{now()} {rel()}] chip init done (link={watch.last})", flush=True)

    def one_ping(t, cap, seen, label: str) -> dict:
        nonlocal seq
        seq += 1
        req = build_echo_request_frame(src_mac=C64_MAC, dst_mac=hmac, src_ip=C64_IP,
                                       dst_ip=HOST_IP, identifier=0x1234, sequence=seq)
        arp = build_arp_request_frame(C64_MAC, C64_IP, HOST_IP)
        if arp_mode == "separate":
            write_bytes(t, TX_BUF, arp)
            write_bytes(t, RESULT, b"\x00")
            load_code(t, CODE, build_tx_code(load_addr=CODE, frame_buf=TX_BUF,
                                             frame_len=len(arp), result_addr=RESULT))
            run_subroutine(target, CODE, timeout=10.0, poll_cadence=0.002)
            time.sleep(0.3)
            arp_kw = {}
        else:
            write_bytes(t, ARP_BUF, arp)
            arp_kw = {"arp_frame_buf": ARP_BUF}
        write_bytes(t, TX_BUF, req.frame)
        write_bytes(t, RX_BUF, bytes(64))
        write_bytes(t, RESULT, b"\x00")
        write_bytes(t, STAT, bytes(8))
        code = build_prefix(ping_addr) + build_ping_and_wait_tod_code(
            load_addr=ping_addr, tx_frame_buf=TX_BUF, tx_frame_len=len(req.frame),
            rx_buf=RX_BUF, result_addr=RESULT, identifier=req.identifier,
            sequence=req.sequence, deadline_tenths=deadline_tenths, **arp_kw)
        load_code(t, CODE, code)
        n0 = len(seen)
        before = arp_state("10.0.66.201")
        t_rel = time.monotonic() - T0
        t0 = time.monotonic()
        run_subroutine(target, CODE, timeout=20.0, poll_cadence=0.002)
        dt = time.monotonic() - t0
        res = read_bytes(t, RESULT, 1)[0]
        load_code(t, CODE, build_stat_read())
        run_subroutine(target, CODE, timeout=10.0, poll_cadence=0.005)
        st = read_bytes(t, STAT, 8)
        time.sleep(0.3)
        win = [f for _, f in seen[n0:]]

        def is_icmp(f, typ):
            return (len(f) > 42 and f[12:14] == b"\x08\x00" and f[23] == 1 and f[34] == typ
                    and f[38:40] == bytes([0x12, 0x34]) and f[40:42] == bytes([seq >> 8, seq & 0xFF]))
        reqs = sum(1 for f in win if is_icmp(f, 8) and f[6:12] == C64_MAC)
        reps = sum(1 for f in win if is_icmp(f, 0) and f[0:6] == C64_MAC)
        arp_reps = sum(1 for f in win if f[12:14] == b"\x08\x06" and len(f) >= 42
                       and f[21] == 2 and f[0:6] == C64_MAC)
        others = len(win) - reqs - reps - arp_reps
        linkst = w16(st, 0)
        r = dict(label=label, seq=seq, res=res, ok=res == 0x01, dt=dt, reqs=reqs, reps=reps,
                 arp_reps=arp_reps, others=others, linest=linkst, linkok=bool(linkst & 0x80),
                 rxmiss=w16(st, 2) >> 6, linest_after=w16(st, 4), rxmiss_after=w16(st, 6) >> 6,
                 arp_before=before, host_link=watch.last, t_rel=t_rel)
        print(f"[{now()} {rel()}] {label} seq={seq} result={res:02X} {'MATCH' if r['ok'] else 'MISS '} "
              f"LineST=${linkst:04X}{'(LinkOK)' if r['linkok'] else '(NOLINK)'} RxMISS={r['rxmiss']} "
              f"after: LineST=${r['linest_after']:04X} RxMISS={r['rxmiss_after']} "
              f"wire: req={reqs} rep={reps} arp-rep={arp_reps} other={others} host-link={watch.last} "
              f"({dt:.2f}s)", flush=True)
        return r

    with create_manager(backend="u64", u64_hosts=HOST, lock_timeout=600.0) as mgr:
        with mgr.instance() as target:
            t = target.transport
            client = t.client
            orig = client.get_config_value(CAT, ITEM)
            print(f"[{now()}] {ITEM} was {orig!r}")
            try:
                # bring the chip up once so the S arms have a steady state to start from
                fresh_session(t, client)
                time.sleep(2.0)
                for r in range(1, rounds + 1):
                    order = arms[r % len(arms):] + arms[:r % len(arms)]
                    for arm in order:
                        print(f"\n[{now()} {rel()}] ---- round {r} arm {arm} (link={watch.last})", flush=True)
                        t_arm = time.monotonic() - T0
                        if arm[0] == "F":
                            fresh_session(t, client)
                            time.sleep(5.0 if arm == "FW" else 0.5)
                        seen: list[tuple[float, bytes]] = []
                        stop = threading.Event()
                        t_cap = time.monotonic() - T0
                        with BpfCapture(IFACE) as cap:
                            print(f"[{now()} {rel()}] capture open on {cap.node} (link={watch.last})", flush=True)

                            def sniff():
                                while not stop.is_set():
                                    try:
                                        seen.append((time.monotonic(), cap.recv(0.2)))
                                    except Exception:
                                        pass
                            threading.Thread(target=sniff, daemon=True).start()
                            time.sleep(5.0 if arm[1] == "E" else 0.5)
                            r1 = one_ping(t, cap, seen, f"r{r} {arm} ping1")
                            time.sleep(1.0)
                            r2 = one_ping(t, cap, seen, f"r{r} {arm} ping2")
                            stop.set()
                        links = watch.since(t_arm)
                        rows.append((r, arm, r1, r2, links, t_cap))
            finally:
                watch.stop.set()
                client.set_config_item(CAT, ITEM, orig)
                print(f"[{now()}] restored {ITEM} = {orig!r}")

    print("\n| round | arm | ping1 | LineST@TX | RxMISS | wire req/rep/arp-rep/other | s | ping2 | LineST@TX2 | link events during arm |")
    print("|" + "---|" * 10)
    tot = {}
    for r, arm, r1, r2, links, t_cap in rows:
        ev = "; ".join(f"{t - t_cap:+.1f}s {s}" for t, s in links) or "-"
        print(f"| {r} | {arm} | {'MATCH' if r1['ok'] else 'MISS'} | ${r1['linest']:04X} | {r1['rxmiss']} | "
              f"{r1['reqs']}/{r1['reps']}/{r1['arp_reps']}/{r1['others']} | {r1['dt']:.2f} | "
              f"{'MATCH' if r2['ok'] else 'MISS'} | ${r2['linest']:04X} | {ev} |")
        tot.setdefault(arm, [0, 0, 0])
        tot[arm][2] += 1
        tot[arm][0] += int(r1["ok"])
        tot[arm][1] += int(r2["ok"])
    print("\nsummary (ping1 / ping2 matches per arm):",
          {k: f"{v[0]}/{v[2]} , {v[1]}/{v[2]}" for k, v in tot.items()})
    print("link events (rel s):", [(round(t, 1), s) for t, s in watch.events])
    return 0


if __name__ == "__main__":
    sys.exit(main())
