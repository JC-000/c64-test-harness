"""Live CS8900a RX FIFO semantics on real silicon (issue #219).

Question: after a *complete* read of a frame (all ``RxLength`` bytes) does
the chip advance to the next frame on its own, or is SkipNow (RxCFG bit
6, PP ``$0102``) still required?  ``_emit_read_frame`` always skips
because it reads a fixed 60 bytes; ip65's ``cs8900a.s`` reads the whole
frame, never skips, and works.

Measured 2026-09-05 on the U64E (fw 3.15 fork) with an external RR-Net
cabled point-to-point to the host NIC, RxCTL = ``$0505`` (RxOKA |
IndividualA only), two host-injected unicast frames (100 B then 80 B,
unique seed byte per frame) queued before each 6510 routine ran, n=3 per
variant, interleaved (scratchpad ``exp219c.py`` / ``exp219d.py``):

===============================================  ==========================
after frame 1 was ...                            the next header read gave
===============================================  ==========================
read completely, header read *immediately*       ``$0000`` / zero body 3/3
read completely, RxEvent polled until set        frame 2, no SkipNow, 3/3
read completely, SkipNow, RxEvent polled         frame 2 (control)      3/3
read partially (20 B), header read immediately   frame 1's *remaining
                                                 data* (not zeros)      3/3
read partially (20 B), SkipNow, RxEvent polled   frame 2 (control)      3/3
===============================================  ==========================

Buffering depth (measured on a clean FIFO, 100-byte frames 50 ms apart,
no reader until all were sent, n=2 per N; scratchpad ``exp219f.py``):
3 queued -> F1 F2 F3 out, RxMISS 0; 4 queued -> F2 F3 F4 (once F3 F4),
RxMISS 1 (2); 5 queued -> F3 F4 F5, RxMISS 2; 8 queued -> F6 F7 F8,
RxMISS 5.  The chip holds up to three such frames and keeps the
**newest**: an unread frame is overwritten by a later arrival and RxMISS
(PP ``$0130``, count in bits 6-15) counts each overwrite.  An earlier
"exactly two, third missed" reading (3/3) came from a leftover half-read
frame holding a slot behind a blind-SkipNow drain, not from the chip.

So: a complete read *does* release the frame -- no SkipNow needed -- but
the next frame's header is presented at RTDATA only once RxEvent's high
byte (PPTR ``$0124``, ``$DE05``) has been read.  That is the CS8900A data
sheet's I/O-mode receive sequence ("Receive Frame Operation": read
RxEvent, then RxStatus and RxLength from the Receive/Transmit Data port,
then the data), and ip65 follows it.  It is not a latency: both frames
were injected 200 ms before the routine ran and RxMISS stayed 0, so frame
2 was already buffered and the only latency possible is the chip's
internal advance after the last data read; the poll that succeeded took
zero iterations (RxEvent was already set), while waiting 100 us / 1.3 ms
/ 10 ms -- two orders beyond an 80 us frame time -- with no RxEvent
access still read ``$00`` (3/3 each).  Reading PP ``$0000``, writing PPTR
alone, reading the RxEvent *low* byte, or reading PP ``$0400`` did not
present it either (3/3 each).  ``$DE00/$DE01`` was read too, but on an
RR-Net that window is the clockport register, not the chip's ISQ, so it
says nothing; the ISQ proper (PP ``$0120``), which the data sheet says
also pops the event queue, is the ``isq`` row below.
A partial read does not advance: the FIFO keeps handing out the rest of
the current frame, so a fixed-length reader (``_emit_read_frame``) must
SkipNow -- which is what it does.  The ``$00``-past-the-end observation
from #210 is this same *between frames, RxEvent not yet read* state.

Gates (all unset -> the module skips cleanly):

* ``RRNET_LIVE=1`` -- master switch.
* ``U64_HOST``     -- the device (no IPs are committed).
* ``RRNET_IFACE``  -- host NIC on the cartridge's link (default ``en4``).

Needs a capture node the process can open (macOS: world-rw ``/dev/bpf*``;
Linux: ``CAP_NET_RAW``).  Sets ``Cartridge Preference = External`` for the
module and restores it.  No elevation marker: nothing here changes host
network state, it only injects and reads frames on the given NIC.
"""

from __future__ import annotations

import os
import platform
import subprocess
import time

import pytest

from c64_test_harness import (
    CARTRIDGE_PREFERENCE_ITEM,
    CARTRIDGE_SETTINGS_CATEGORY,
    create_manager,
)
from c64_test_harness.bridge_ping import (
    CS8900A_RXEVENT_MASK,
    PPDATA_HI,
    PPDATA_LO,
    PPTR_HI,
    PPTR_LO,
    RTDATA_HI,
    RTDATA_LO,
    Asm,
    _emit_clockport_enable,
    _emit_skip_packet,
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

# Named in ultimate64_helpers since issue #221.
CAT = CARTRIDGE_SETTINGS_CATEGORY
ITEM = CARTRIDGE_PREFERENCE_ITEM

C64_MAC = parse_mac("02:c6:40:00:00:01")
RXCTL_IA_ONLY = 0x0505          # RxOKA (bit 8) | IndividualA (bit 10) | regnum 5

CODE = 0x4000
RES = 0x5000                    # result block, layout in _build_variant
BUF1 = 0x5100                   # frame 1 data (<= 256 B)
BUF2 = 0x5200                   # 16 bytes read after the second header
DBUF = 0x5300                   # depth probe: DEPTH_SLOTS x (4 header + 16 data)
DEPTH_SLOTS = 8
LEN1, LEN2, LEN3 = 100, 80, 120
PARTIAL_N = 20
_tag = [0]


def _host_mac(iface: str) -> bytes:
    if platform.system() == "Darwin":
        out = subprocess.run(["ifconfig", iface], capture_output=True, text=True).stdout
        for ln in out.splitlines():
            if "ether " in ln:
                return parse_mac(ln.split()[1])
    else:
        try:
            with open(f"/sys/class/net/{iface}/address") as fh:
                return parse_mac(fh.read().strip())
        except OSError:
            pass
    pytest.skip(f"cannot read the MAC of {iface}")


# --------------------------------------------------------------------------- #
# 6502 builders                                                                #
# --------------------------------------------------------------------------- #

def _pp_sel(a: Asm, off: int) -> None:
    a.emit(0xA9, off & 0xFF, 0x8D, PPTR_LO & 0xFF, PPTR_LO >> 8)
    a.emit(0xA9, off >> 8, 0x8D, PPTR_HI & 0xFF, PPTR_HI >> 8)


def _poll_rxevent(a: Asm, got: str, timeout_lbl: str) -> None:
    """Bounded RxEvent poll (~2.5 s at 1 MHz); A = masked high byte on exit."""
    _tag[0] += 1
    lbl = f"pl{_tag[0]}"
    a.emit(0xA9, 0x00, 0x85, 0xFD, 0x85, 0xFE)
    a.label(lbl)
    _pp_sel(a, 0x0124)
    a.emit(0xAD, PPDATA_HI & 0xFF, PPDATA_HI >> 8)
    a.emit(0x29, CS8900A_RXEVENT_MASK)
    a.branch(0xD0, got)
    a.emit(0xE6, 0xFD)
    a.branch(0xD0, lbl)
    a.emit(0xE6, 0xFE)
    a.branch(0xD0, lbl)
    a.jmp(timeout_lbl)


def _read_header(a: Asm, dst: int) -> None:
    """RxStatus, RxLength -> dst (lo, hi, lo, hi); high half first (#210)."""
    a.emit(0xAD, RTDATA_HI & 0xFF, RTDATA_HI >> 8, 0x8D, (dst + 1) & 0xFF, (dst + 1) >> 8)
    a.emit(0xAD, RTDATA_LO & 0xFF, RTDATA_LO >> 8, 0x8D, dst & 0xFF, dst >> 8)
    a.emit(0xAD, RTDATA_HI & 0xFF, RTDATA_HI >> 8, 0x8D, (dst + 3) & 0xFF, (dst + 3) >> 8)
    a.emit(0xAD, RTDATA_LO & 0xFF, RTDATA_LO >> 8, 0x8D, (dst + 2) & 0xFF, (dst + 2) >> 8)


def _read_body(a: Asm, buf: int, count_zp: int | None, fixed: int | None) -> None:
    _tag[0] += 1
    lbl = f"rd{_tag[0]}"
    a.emit(0xA9, buf & 0xFF, 0x85, 0xFB, 0xA9, buf >> 8, 0x85, 0xFC)
    a.emit(0xA0, 0x00)
    a.label(lbl)
    a.emit(0xAD, RTDATA_LO & 0xFF, RTDATA_LO >> 8, 0x91, 0xFB, 0xC8)
    a.emit(0xAD, RTDATA_HI & 0xFF, RTDATA_HI >> 8, 0x91, 0xFB, 0xC8)
    if count_zp is not None:
        a.emit(0xC4, count_zp)
        a.branch(0x90, lbl)
    else:
        a.emit(0xC0, fixed)
        a.branch(0xD0, lbl)


def _rxmiss(a: Asm) -> None:
    _pp_sel(a, 0x0130)
    a.emit(0xAD, PPDATA_LO & 0xFF, PPDATA_LO >> 8, 0x8D, (RES + 12) & 0xFF, (RES + 12) >> 8)
    a.emit(0xAD, PPDATA_HI & 0xFF, PPDATA_HI >> 8, 0x8D, (RES + 13) & 0xFF, (RES + 13) >> 8)


def _build_variant(variant: str) -> bytes:
    """RES: +0 status ($01 ran / $FE no frame 1), +1 RxEvent1, +2..5 hdr1,
    +6 RxEvent2 ($EE = not polled / $FE = poll timed out), +7..10 hdr2,
    +11 bytes read of frame 1, +12,13 RxMISS raw."""
    _tag[0] = 0
    a = Asm(org=CODE)
    a.emit(0x78)
    _emit_clockport_enable(a)
    a.emit(0xA9, 0xEE, 0x8D, (RES + 6) & 0xFF, (RES + 6) >> 8)
    _poll_rxevent(a, "got1", "to1")
    a.label("got1")
    a.emit(0x8D, (RES + 1) & 0xFF, (RES + 1) >> 8)
    _read_header(a, RES + 2)
    if variant.startswith("complete"):
        a.emit(0x18, 0xAD, (RES + 4) & 0xFF, (RES + 4) >> 8)     # CLC; LDA len lo
        a.emit(0x69, 0x01, 0x29, 0xFE, 0x85, 0xF7)               # round up to even
    else:
        a.emit(0xA9, PARTIAL_N, 0x85, 0xF7)
    a.emit(0xA5, 0xF7, 0x8D, (RES + 11) & 0xFF, (RES + 11) >> 8)
    _read_body(a, BUF1, 0xF7, None)
    if variant == "complete_ev1":            # ONE RxEvent read, high byte only
        _pp_sel(a, 0x0124)
        a.emit(0xAD, PPDATA_HI & 0xFF, PPDATA_HI >> 8)
        a.emit(0x8D, (RES + 6) & 0xFF, (RES + 6) >> 8)
    elif variant == "complete_ev1lo":        # ONE RxEvent read, low byte only
        _pp_sel(a, 0x0124)
        a.emit(0xAD, PPDATA_LO & 0xFF, PPDATA_LO >> 8)
        a.emit(0x8D, (RES + 6) & 0xFF, (RES + 6) >> 8)
    elif variant == "complete_isq":          # ONE ISQ read (PP $0120), both halves
        _pp_sel(a, 0x0120)
        a.emit(0xAD, PPDATA_LO & 0xFF, PPDATA_LO >> 8)
        a.emit(0xAD, PPDATA_HI & 0xFF, PPDATA_HI >> 8)
        a.emit(0x8D, (RES + 6) & 0xFF, (RES + 6) >> 8)
    elif variant == "complete_d8":           # ~10.4 ms of nothing at 1 MHz
        for i in range(8):
            a.emit(0xA0, 0x00)
            a.label(f"dly{i}")
            a.emit(0x88)
            a.branch(0xD0, f"dly{i}")
    if variant.endswith("_skip"):
        _emit_skip_packet(a)
    if variant.endswith("_skip") or variant == "complete_poll":
        _poll_rxevent(a, "got2", "to2")
        a.label("to2")
        a.emit(0xA9, 0xFE, 0x8D, (RES + 6) & 0xFF, (RES + 6) >> 8)
        a.jmp("hdr2")
        a.label("got2")
        a.emit(0x8D, (RES + 6) & 0xFF, (RES + 6) >> 8)
    a.label("hdr2")
    _read_header(a, RES + 7)
    _read_body(a, BUF2, None, 16)
    _rxmiss(a)
    _ack_and_skip(a, 3)                     # leave nothing half-read behind
    a.emit(0xA9, 0x01, 0x8D, RES & 0xFF, RES >> 8)
    a.emit(0x58, 0x60)
    a.label("to1")
    _rxmiss(a)
    a.emit(0xA9, 0xFE, 0x8D, RES & 0xFF, RES >> 8)
    a.emit(0x58, 0x60)
    return a.build()


def _build_depth() -> bytes:
    """DEPTH_SLOTS x (poll RxEvent; header + 16 data -> DBUF slot; SkipNow)."""
    _tag[0] = 0
    a = Asm(org=CODE)
    a.emit(0x78)
    _emit_clockport_enable(a)
    for i in range(DEPTH_SLOTS):
        slot = DBUF + i * 20
        _poll_rxevent(a, f"dg{i}", f"dt{i}")
        a.label(f"dt{i}")
        a.emit(0xA9, 0xFE)
        a.label(f"dg{i}")
        a.emit(0x8D, (RES + 16 + i) & 0xFF, (RES + 16 + i) >> 8)
        _read_header(a, slot)
        _read_body(a, slot + 4, None, 16)
        _emit_skip_packet(a)
    _rxmiss(a)
    a.emit(0xA9, 0x01, 0x8D, RES & 0xFF, RES >> 8)
    a.emit(0x58, 0x60)
    return a.build()


def _ack_and_skip(a: Asm, n: int) -> None:
    """n x (read RxEvent high byte, SkipNow, ~1.3 ms) -- unconditional, but
    each skip is preceded by the RxEvent read that acknowledges the current
    frame, the way ip65's poll/skipframe pair does it.  Blind SkipNows left
    a half-read frame behind on the first live run of this file."""
    for i in range(n):
        _tag[0] += 1
        _pp_sel(a, 0x0124)
        a.emit(0xAD, PPDATA_HI & 0xFF, PPDATA_HI >> 8)
        _emit_skip_packet(a)
        a.emit(0xA0, 0x00)
        a.label(f"dw{_tag[0]}")
        a.emit(0x88)
        a.branch(0xD0, f"dw{_tag[0]}")


def _build_drain() -> bytes:
    """8 x (RxEvent read, SkipNow), then RxEvent hi -> RES+15, 4 RTDATA
    bytes -> RES+16..19 (should be zeros), RxMISS read (clears it)."""
    _tag[0] = 100
    a = Asm(org=CODE)
    a.emit(0x78)
    _emit_clockport_enable(a)
    _ack_and_skip(a, 8)
    _pp_sel(a, 0x0124)
    a.emit(0xAD, PPDATA_HI & 0xFF, PPDATA_HI >> 8)
    a.emit(0x29, CS8900A_RXEVENT_MASK)
    a.emit(0x8D, (RES + 15) & 0xFF, (RES + 15) >> 8)
    _read_header(a, RES + 16)
    _rxmiss(a)
    a.emit(0x58, 0x60)
    return a.build()


# --------------------------------------------------------------------------- #
# Fixture and trial runner                                                     #
# --------------------------------------------------------------------------- #

class _Bench:
    def __init__(self, target, cap, hmac: bytes) -> None:
        self.target, self.cap, self.hmac = target, cap, hmac
        self.t = target.transport
        self.seed = 0
        self.sent: dict[int, tuple[str, int]] = {}

    def frame(self, label: str, length: int) -> bytes:
        self.seed = (self.seed % 255) + 1
        self.sent[self.seed] = (label, length)
        body = bytes(((self.seed + i) & 0xFF) for i in range(length - 14))
        return C64_MAC + self.hmac + b"\x88\xb5" + body

    def classify(self, status: int, length: int, first16: bytes, this: dict[str, int]) -> str:
        if status == 0 and length == 0 and first16 == bytes(16):
            return "ZERO"
        if first16[:14] != C64_MAC + self.hmac + b"\x88\xb5":
            return "OTHER"
        seed = first16[14]
        for lbl, sd in this.items():
            if sd == seed and length == self.sent[sd][1]:
                return lbl
        return "STALE" if seed in self.sent else "OTHER"

    def run(self, code: bytes, frames: list[bytes]) -> bytes:
        t = self.t
        write_bytes(t, RES, bytes(32))
        load_code(t, CODE, _build_drain())
        run_subroutine(self.target, CODE, timeout=10.0, poll_cadence=0.005)
        d = read_bytes(t, RES, 20)
        assert d[15] == 0 and d[16:20] == bytes(4), (
            f"FIFO not empty after the drain: RxEvent hi ${d[15]:02X}, "
            f"header {d[16:20].hex()} -- the previous trial left a frame behind"
        )
        write_bytes(t, RES, bytes(32))
        write_bytes(t, BUF1, bytes(256))
        write_bytes(t, BUF2, bytes(16))
        write_bytes(t, DBUF, bytes(DEPTH_SLOTS * 20))
        load_code(t, CODE, code)
        for f in frames:
            self.cap.send(f)
            time.sleep(0.05)
        time.sleep(0.2)
        run_subroutine(self.target, CODE, timeout=15.0, poll_cadence=0.005)
        return read_bytes(t, RES, 32)

    def trial(self, variant: str) -> dict:
        f1, f2 = self.frame("F1", LEN1), self.frame("F2", LEN2)
        this = {"F1": f1[14], "F2": f2[14]}
        res = self.run(_build_variant(variant), [f1, f2])
        assert res[0] == 0x01, f"{variant}: frame 1 never arrived (status ${res[0]:02X})"
        b1 = read_bytes(self.t, BUF1, 256)
        b2 = read_bytes(self.t, BUF2, 16)
        n1 = res[11]
        st1, ln1 = res[2] | (res[3] << 8), res[4] | (res[5] << 8)
        st2, ln2 = res[7] | (res[8] << 8), res[9] | (res[10] << 8)
        first = self.classify(st1, ln1, b1[:16], this)
        assert first == "F1", (
            f"{variant}: the first frame out was {first}, not F1 "
            f"(RxEvent ${res[1]:02X}, RxStatus ${st1:04X}, RxLength {ln1}, "
            f"first 16 bytes {b1[:16].hex()}; this trial's seeds {this})"
        )
        assert b1[:n1] == f1[:n1], f"{variant}: frame 1 body mismatch"
        nxt = self.classify(st2, ln2, b2, this)
        return {
            "next": nxt, "hdr2": (st2, ln2), "buf2": b2, "f1": f1, "n1": n1,
            "rxevent2": res[6], "rxmiss": (res[12] | (res[13] << 8)) >> 6,
        }


@pytest.fixture(scope="module")
def bench():
    hmac = _host_mac(_IFACE)
    with create_manager(backend="u64", u64_hosts=_HOST, lock_timeout=600.0) as mgr:
        with mgr.instance() as target:
            t = target.transport
            client = t.client
            orig = client.get_config_category(CAT)[CAT][ITEM]
            try:
                client.set_config_item(CAT, ITEM, "External")
                time.sleep(0.5)
                t.reset()
                assert wait_for_text(t, "READY.", timeout=25.0, poll_interval=0.3,
                                     verbose=False) is not None, "no READY."
                time.sleep(6.5)
                # Bring the chip up from the 6510 and prove it is there: the
                # IA read-back is the presence test (ip65's is PP $0000).
                init = (cs8900a_rxctl_inline_code(RXCTL_IA_ONLY)
                        + cs8900a_linectl_or_inline_code()
                        + cs8900a_set_mac_inline_code(C64_MAC))
                a = Asm(org=CODE + len(init))
                for i, off in enumerate((0x0000, 0x0104, 0x0158)):
                    _pp_sel(a, off)
                    a.emit(0xAD, PPDATA_LO & 0xFF, PPDATA_LO >> 8,
                           0x8D, (RES + 2 * i) & 0xFF, (RES + 2 * i) >> 8)
                    a.emit(0xAD, PPDATA_HI & 0xFF, PPDATA_HI >> 8,
                           0x8D, (RES + 2 * i + 1) & 0xFF, (RES + 2 * i + 1) >> 8)
                a.emit(0x60)
                load_code(t, CODE, init + a.build())
                run_subroutine(target, CODE, timeout=10.0, poll_cadence=0.005)
                rb = read_bytes(t, RES, 6)
                if rb[:2] != b"\x0e\x63":
                    pytest.skip("no CS8900a answers on the 6510 (PP $0000 != $630E); "
                                "is an RR-Net fitted?")
                assert rb[2] | (rb[3] << 8) == RXCTL_IA_ONLY, "RxCTL did not take"
                assert rb[4:6] == C64_MAC[:2], "IA did not take"
                try:
                    cap = open_capture(_IFACE)
                except CaptureUnavailable as exc:
                    pytest.skip(str(exc))
                try:
                    time.sleep(0.5)
                    yield _Bench(target, cap, hmac)
                finally:
                    cap.close()
            finally:
                client.set_config_item(CAT, ITEM, orig)


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #

def test_complete_read_then_rxevent_poll_advances_without_skipnow(bench):
    r = bench.trial("complete_poll")
    assert r["rxevent2"] & CS8900A_RXEVENT_MASK, "RxEvent never re-asserted for frame 2"
    assert r["next"] == "F2", (
        f"after a complete read and an RxEvent poll the next header was {r['next']} "
        f"(RxStatus ${r['hdr2'][0]:04X}, RxLength {r['hdr2'][1]}); a complete read "
        "should release the frame by itself (ip65 relies on this)"
    )
    assert r["rxmiss"] == 0


def test_immediately_after_a_complete_read_the_fifo_reads_zero(bench):
    """Without an RxEvent (high byte) read in between, RTDATA is $00 --
    not a latency (10 ms of waiting changes nothing).  This is the #210
    'zeros past the end' state."""
    r = bench.trial("complete")
    assert r["next"] == "ZERO", (
        f"header read straight after a complete read gave {r['next']} "
        f"(RxStatus ${r['hdr2'][0]:04X}, RxLength {r['hdr2'][1]}); measured $0000 3/3"
    )


def test_a_single_rxevent_high_byte_read_presents_the_next_frame(bench):
    """One LDA $DE05 with PPTR=$0124 -- no loop, no SkipNow -- is enough."""
    r = bench.trial("complete_ev1")
    assert r["rxevent2"] & CS8900A_RXEVENT_MASK, "RxEvent high byte read $00"
    assert r["next"] == "F2", f"one RxEvent high-byte read gave {r['next']}"


def test_the_rxevent_low_byte_alone_does_not_present_it(bench):
    r = bench.trial("complete_ev1lo")
    assert r["rxevent2"] == 0x04, f"RxEvent low byte should be the register number, got ${r['rxevent2']:02X}"
    assert r["next"] == "ZERO", f"the low byte alone gave {r['next']}"


def test_ten_milliseconds_of_waiting_does_not_present_it(bench):
    r = bench.trial("complete_d8")
    assert r["next"] == "ZERO", f"after ~10 ms with no RxEvent access: {r['next']}"


def test_isq_read_and_the_next_frame(bench):
    """PP $0120 (ISQ) pops the event queue per the data sheet.  Measured
    live: its high byte reads $00 here and the read does NOT present the
    next frame -- only the RxEvent register read does."""
    r = bench.trial("complete_isq")
    print(f"\nISQ (PP $0120) read after a complete read: hi byte ${r['rxevent2']:02X}, "
          f"next header -> {r['next']} (RxStatus ${r['hdr2'][0]:04X}, RxLength {r['hdr2'][1]})")
    assert r["next"] == "ZERO", f"an ISQ read presented the next frame ({r['next']}); measured ZERO before"


def test_partial_read_without_skipnow_keeps_delivering_frame_one(bench):
    r = bench.trial("partial")
    f1, n1 = r["f1"], r["n1"]
    # the 4 "header" bytes plus 16 data bytes are simply frame 1 bytes n1..n1+19
    assert r["buf2"] == f1[n1 + 4:n1 + 20], (
        f"after a partial read the next bytes were not frame 1's remainder "
        f"(next classified as {r['next']}); a fixed-length reader must SkipNow"
    )
    assert r["next"] != "F2"


def test_skipnow_after_partial_read_yields_frame_two(bench):
    """Positive control: what _emit_read_frame does."""
    r = bench.trial("partial_skip")
    assert r["next"] == "F2", f"SkipNow after a partial read gave {r['next']}"
    assert r["rxmiss"] == 0


def test_skipnow_after_complete_read_is_harmless(bench):
    """The harness's skip after a full-length frame is a no-op-safe extra."""
    r = bench.trial("complete_skip")
    assert r["next"] == "F2", f"SkipNow after a complete read gave {r['next']}"


def test_frames_come_out_in_order_and_rxmiss_counts_the_overflow(bench):
    """Eight 100-byte frames queued with no reader: the chip hands back
    the NEWEST k (k = 3, once 2 in 4-frame trials) in arrival order, the
    older ones are gone, and RxMISS == 8 - k.  Pins the measured
    newest-kept semantics; an oldest-kept FIFO would fail on the first
    slot."""
    n = DEPTH_SLOTS
    frames = [bench.frame(f"F{i + 1}", LEN1) for i in range(n)]
    this = {f"F{i + 1}": f[14] for i, f in enumerate(frames)}
    res = bench.run(_build_depth(), frames)
    slots = read_bytes(bench.t, DBUF, DEPTH_SLOTS * 20)
    seen = []
    for i in range(DEPTH_SLOTS):
        sl = slots[i * 20:(i + 1) * 20]
        seen.append(bench.classify(sl[0] | (sl[1] << 8), sl[2] | (sl[3] << 8), sl[4:20], this))
    rxmiss = (res[12] | (res[13] << 8)) >> 6
    k = next((i for i, x in enumerate(seen) if x == "ZERO"), DEPTH_SLOTS)
    print(f"\nRX depth: {k} of {n} {LEN1}-byte frames came out {seen}, RxMISS {rxmiss}")
    assert 2 <= k <= 3, f"{k} frames buffered; 3 (once 2) measured on a clean FIFO: {seen}"
    assert seen[:k] == [f"F{i + 1}" for i in range(n - k, n)], (
        f"expected the newest {k} in order, got {seen}"
    )
    assert all(x == "ZERO" for x in seen[k:]), f"gap in the sequence: {seen}"
    assert rxmiss == n - k, f"RxMISS {rxmiss} does not account for the {n - k} overwritten frame(s)"
