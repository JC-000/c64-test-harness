"""Live: the first exchange of a session on real RR-Net silicon (issue #222).

What was reported: in 2/2 sessions the very first ping timed out on the
6510 while the host's echo reply was on the wire; not the ARP effect,
not the link, not "too soon after init".

What it is (U64E fw 3.15 fork ``4011c97c``, external RR-Net cabled
point-to-point to a macOS USB NIC, 1 MHz, ``DeviceLock`` held,
2026-09-05, scratchpad ``exp222.py`` / ``exp222b.py``): **frames that
arrived while nobody was reading sit in the CS8900a's RX queue, and an
exchange started on top of them loses its reply -- the chip counts it
in RxMISS (+1, "no receive buffer") and never presents it.**  On this
bench the stale frames are the host's own periodic 342-byte UDP
broadcasts (RxCTL accepts broadcast).

Run 1, five arms x 6 rounds, interleaved, every trial with LineST read on
the 6510 microseconds before the TX, a host link watch at 100 ms and a
second ping 1 s later:

======================================================  =======  =======
arm                                                     ping 1   ping 2
======================================================  =======  =======
FL  fresh (PUT External, reset, READY, 6.5 s, init), capture
    opened 0.5 s before, ping ~1 s after init            6/6      5/6
FE  fresh, capture opened, 5 s idle, ping                 3/6      6/6
FW  fresh, 5 s idle, capture opened, 0.5 s, ping          3/6      6/6
SL  steady chip, capture re-opened 0.5 s before           6/6      6/6
SE  steady chip, capture re-opened 5 s before             6/6      6/6
======================================================  =======  =======

Every one of the 7 misses: LinkOK set (LineST ``$1294``) at TX, echo
request AND reply on the wire, ARP entry present, no ``en4`` link
transition in the whole run (so the promiscuous-mode / link-bounce
candidate is out), and RxMISS exactly +1 on the chip (0 in all 30
matches).  Run 2, FE vs FE+drain (``_emit_drain_rx`` on the 6510
immediately before the exchange), 6 rounds interleaved: **FE 3/6, FE+drain
6/6**; every FE miss had frames queued when the exchange started (RxMISS
+1 during the reset window, or 1-3 host broadcasts logged during the
idle), every FE match had an empty queue; the drain skipped 0-1 frames.
The REST ``reset()`` does not reset the chip (RxCTL/LineCTL/IA survive
it), which is why a "fresh session" still carries the old queue.

The rule this pins: **drain the chip's RX queue before the first
exchange** (``drain_first=True`` on the ping builders), and read RxMISS
when an exchange misses -- +1 means the chip discarded the reply.

Gates (all unset -> the module skips cleanly):

* ``RRNET_LIVE=1`` -- master switch.
* ``U64_HOST``     -- the device (no IPs are committed).
* ``RRNET_IFACE``  -- host NIC on the cartridge's link (default ``en4``);
  its IP is read from ``ifconfig`` (macOS) or ``ip`` (Linux).

Needs a capture node the process can open.  Sets ``Cartridge
Preference = External`` and restores it.  Never: ``save_config_to_flash``,
``poweroff``, ``reboot``.
"""
from __future__ import annotations

import os
import platform
import re
import subprocess
import time

import pytest

from c64_test_harness import create_manager
from c64_test_harness.bridge_ping import (
    CS8900A_RXCTL_VALUE_IP65,
    PPDATA_HI,
    PPDATA_LO,
    PPTR_HI,
    PPTR_LO,
    _clockport_enable_bytes,
    build_arp_request_frame,
    build_echo_request_frame,
    build_ping_and_wait_tod_code,
    cs8900a_linectl_or_inline_code,
    cs8900a_rxctl_inline_code,
    cs8900a_set_mac_inline_code,
)
from c64_test_harness.capture import CaptureUnavailable, open_capture
from c64_test_harness.ethernet import parse_mac
from c64_test_harness.execute import load_code, run_subroutine
from c64_test_harness.memory import read_bytes, write_bytes
from c64_test_harness.screen import wait_for_text

_LIVE = os.environ.get("RRNET_LIVE")
_HOST = os.environ.get("U64_HOST")
_IFACE = os.environ.get("RRNET_IFACE", "en4")

pytestmark = [
    pytest.mark.skipif(not _LIVE, reason="RRNET_LIVE not set"),
    pytest.mark.skipif(not _HOST, reason="U64_HOST not set"),
]

CAT = "C64 and Cartridge Settings"
ITEM = "Cartridge Preference"
C64_MAC = parse_mac("02:c6:40:00:00:01")
CODE, TX_BUF, ARP_BUF, RX_BUF, RESULT, STAT = 0x4000, 0x5000, 0x5080, 0x5100, 0x5300, 0x5310
STALE_FRAMES = 3
ROUNDS = 3
_seq = [0x2200]


def _host_addr(iface: str) -> tuple[bytes, bytes]:
    if platform.system() == "Darwin":
        out = subprocess.run(["ifconfig", iface], capture_output=True, text=True).stdout
        mac = re.search(r"ether ([0-9a-f:]{17})", out)
        ip = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
    else:
        out = subprocess.run(["ip", "addr", "show", iface], capture_output=True, text=True).stdout
        mac = re.search(r"link/ether ([0-9a-f:]{17})", out)
        ip = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
    if not mac or not ip:
        pytest.skip(f"cannot read the MAC/IPv4 of {iface}")
    return parse_mac(mac.group(1)), bytes(int(x) for x in ip.group(1).split("."))


def _pp_read(off: int, dst: int) -> bytes:
    return bytes([
        0xA9, off & 0xFF, 0x8D, PPTR_LO & 0xFF, PPTR_LO >> 8,
        0xA9, off >> 8, 0x8D, PPTR_HI & 0xFF, PPTR_HI >> 8,
        0xAD, PPDATA_LO & 0xFF, PPDATA_LO >> 8, 0x8D, dst & 0xFF, dst >> 8,
        0xAD, PPDATA_HI & 0xFF, PPDATA_HI >> 8, 0x8D, (dst + 1) & 0xFF, (dst + 1) >> 8,
    ])


def _stat(target) -> tuple[int, int]:
    """(LineST, RxMISS count) read on the 6510; RxMISS is read-to-clear."""
    t = target.transport
    write_bytes(t, STAT, bytes(4))
    load_code(t, CODE, _clockport_enable_bytes() + _pp_read(0x0134, STAT) + _pp_read(0x0130, STAT + 2) + b"\x60")
    run_subroutine(target, CODE, timeout=10.0, poll_cadence=0.005)
    raw = read_bytes(t, STAT, 4)
    return raw[0] | (raw[1] << 8), (raw[2] | (raw[3] << 8)) >> 6


def _stale_broadcast(host_mac: bytes, host_ip: bytes, n: int) -> bytes:
    """What the bench host sends on its own: a 342-byte UDP broadcast."""
    f = bytearray(b"\xff" * 6 + host_mac + b"\x08\x00")
    f += bytes([0x45, 0, 0x01, 0x48, 0, n, 0, 0, 64, 17, 0, 0]) + host_ip + b"\xff" * 4
    f += bytes([0, 68, 0, 67, 0x01, 0x34, 0, 0])
    return bytes(f) + bytes(342 - len(f))


@pytest.fixture(scope="module")
def session():
    host_mac, host_ip = _host_addr(_IFACE)
    try:
        cap = open_capture(_IFACE)
    except CaptureUnavailable as exc:
        pytest.skip(f"no capture on {_IFACE}: {exc}")
    with create_manager(backend="u64", u64_hosts=_HOST, lock_timeout=600.0) as mgr:
        with mgr.instance() as target:
            t = target.transport
            client = t.client
            orig = client.get_config_value(CAT, ITEM)
            try:
                client.set_config_item(CAT, ITEM, "External")
                time.sleep(0.5)
                t.reset()
                time.sleep(1.0)
                assert wait_for_text(t, "READY.", timeout=25.0, poll_interval=0.3, verbose=False)
                time.sleep(6.5)
                load_code(t, CODE, cs8900a_rxctl_inline_code(CS8900A_RXCTL_VALUE_IP65)
                          + cs8900a_linectl_or_inline_code()
                          + cs8900a_set_mac_inline_code(C64_MAC) + b"\x60")
                run_subroutine(target, CODE, timeout=10.0, poll_cadence=0.005)
                linest, _ = _stat(target)
                if not linest & 0x80:
                    pytest.skip(f"no 10BASE-T link on the cartridge (LineST ${linest:04X})")
                yield target, cap, host_mac, host_ip
            finally:
                cap.close()
                client.set_config_item(CAT, ITEM, orig)


def _exchange(session, *, drain_first: bool, stale: int) -> tuple[bool, int]:
    """Inject *stale* host broadcasts, wait, then ARP + ping in one routine.

    Returns (matched, RxMISS counted during the exchange)."""
    target, cap, host_mac, host_ip = session
    t = target.transport
    for i in range(stale):
        cap.send(_stale_broadcast(host_mac, host_ip, i))
        time.sleep(0.05)
    time.sleep(0.5)
    _stat(target)          # clear RxMISS: three 342-byte frames already overflow the chip
    _seq[0] += 1
    c64_ip = bytes([host_ip[0], host_ip[1], host_ip[2], 201])
    echo = build_echo_request_frame(src_mac=C64_MAC, dst_mac=host_mac, src_ip=c64_ip,
                                    dst_ip=host_ip, identifier=0x2222, sequence=_seq[0])
    write_bytes(t, TX_BUF, echo.frame)
    write_bytes(t, ARP_BUF, build_arp_request_frame(C64_MAC, c64_ip, host_ip))
    write_bytes(t, RX_BUF, bytes(64))
    write_bytes(t, RESULT, b"\x00")
    load_code(t, CODE, build_ping_and_wait_tod_code(
        CODE, TX_BUF, len(echo.frame), RX_BUF, RESULT, echo.identifier, echo.sequence,
        deadline_tenths=20, arp_frame_buf=ARP_BUF, drain_first=drain_first))
    run_subroutine(target, CODE, timeout=20.0, poll_cadence=0.002)
    matched = read_bytes(t, RESULT, 1) == b"\x01"
    _, rxmiss = _stat(target)
    return matched, rxmiss


def test_drain_first_matches_with_stale_frames_queued(session) -> None:
    """The rule: with stale frames in the chip's queue, a ``drain_first``
    exchange matches, every time, with nothing discarded by the chip."""
    results = [_exchange(session, drain_first=True, stale=STALE_FRAMES) for _ in range(ROUNDS)]
    assert all(m for m, _ in results), f"drain_first exchanges: {results}"
    assert all(r == 0 for _, r in results), f"RxMISS with drain_first: {results}"


def test_a_miss_without_drain_is_the_chip_discarding_the_reply(session) -> None:
    """The mechanism: with three 342-byte frames queued and no drain the
    exchange misses, and the chip has counted the discarded reply
    (RxMISS >= 1).  Measured 100% so far with a non-empty queue (3/3
    here on 2026-09-05 with RxMISS 2 each, 3/3 in exp222b, 6/12 in exp222
    where the queue state was only partly known); a match here would be
    new data, and a miss whose RxMISS is 0 would refute the mechanism."""
    results = [_exchange(session, drain_first=False, stale=STALE_FRAMES) for _ in range(ROUNDS)]
    misses = [r for m, r in results if not m]
    print(f"\nno-drain with {STALE_FRAMES} stale frames: {len(misses)}/{ROUNDS} missed; "
          f"RxMISS per trial={[r for _, r in results]}")
    assert len(misses) == ROUNDS, f"an exchange over a full queue matched: {results}"
    assert all(r >= 1 for r in misses), f"a miss the chip did not count: {results}"


def test_empty_queue_matches_without_drain(session) -> None:
    """The control: nothing queued, no drain, the exchange matches."""
    _exchange(session, drain_first=True, stale=0)      # leaves the queue empty
    results = [_exchange(session, drain_first=False, stale=0) for _ in range(ROUNDS)]
    assert all(m for m, _ in results), f"empty-queue exchanges: {results}"
