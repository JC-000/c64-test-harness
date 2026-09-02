"""Reproduce the mode-2 keyboard/screen failure (issue #170) and capture it.

Replays ``tests/test_vice_core.py``'s TestScreen -> TestKeyboard order in
one process against a FRESH VICE per cycle, using the *production*
``_restore_basic`` / ``_wait_for_text_binary`` helpers through a
recording proxy, so what runs here is what pytest runs.

What the proxy records that the test suite cannot see:

* at every PC redirect (``set_registers``): where the 6510 was paused
  (PC, SP, the stack above SP) -- the state ``_restore_basic`` inherits
  when it jumps into the KERNAL idle loop at $E5CD without touching SP;
* at every ``$C6`` write: the paused PC and the old ``$C6`` value, to
  catch a write landing while LP2 ($E5B4-$E5C9) is mid-copy;
* around the blank+feed pair: LIN/CYC before and after, to prove the
  monitor pause held across the writes.

On failure it dumps the production failure report plus the full zero
page, the keyboard buffer, the vector table, the stack, the redirect log
of the failing cycle *and* of the previous (passing) cycle, and VICE's
CPU history disassembled, which shows how the PC got where it is.

    python3 scripts/vice_keyecho_probe.py 45              # baseline
    python3 scripts/vice_keyecho_probe.py 45 --restore e5cd-stub
    python3 scripts/vice_keyecho_probe.py 45 --stop-at-first

``--restore`` picks how BASIC is re-entered:

    suite  (default)  the suite's ``_restore_basic`` as it stands
    e5cd-stub         the pre-#170 restore: CLI; JMP $E5CD at $CF00 with
                      SP untouched -- kept so the mechanism stays
                      reproducible (the RTS into zero page needs the
                      monitor pause to land inside the IRQ handler)

Run under host load to reproduce (three concurrent load generators):

    for j in 1 2 3; do
      (for k in $(seq 1 200); do
         pytest tests/test_vice_warp.py -q -p no:cacheprovider >/dev/null 2>&1
       done) &
    done
"""
from __future__ import annotations

import argparse
import collections
import contextlib
import io
import os
import socket
import sys
import time

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))
sys.path.insert(0, os.path.join(_REPO, "tests"))

import c64_test_harness  # noqa: E402
from c64_test_harness.backends.vice_lifecycle import ViceConfig, ViceProcess  # noqa: E402
from c64_test_harness.debug import dump_screen  # noqa: E402
from c64_test_harness.keyboard import send_key, send_text  # noqa: E402
from c64_test_harness.screen import ScreenGrid, wait_for_stable  # noqa: E402
from conftest import connect_binary_transport  # noqa: E402

import test_vice_core as tc  # noqa: E402

KEYCOUNT, KEYBUF = 0x00C6, 0x0277

# KERNAL regions that classify where the 6510 was paused.
_REGIONS = [
    ("idle-loop $E5CD-$E5D5", 0xE5CD, 0xE5D6),
    ("LP2 $E5B4-$E5C9 (buffer shift)", 0xE5B4, 0xE5CA),
    ("key-fetch $E5D6-$E631", 0xE5D6, 0xE632),
    ("CHRIN-from-screen $E632-$E6B5", 0xE632, 0xE6B6),
    ("IRQ entry $FF48-$FF5A", 0xFF48, 0xFF5B),
    ("IRQ handler $EA31-$EA86", 0xEA31, 0xEA87),
    ("SCNKEY $EA87-$EB78", 0xEA87, 0xEB79),
    ("UDTIM $F69B-$F6BB", 0xF69B, 0xF6BC),
    ("cursor $EA13-$EA30", 0xEA13, 0xEA31),
    ("stub $CF00", 0xCF00, 0xCF04),
]


def region(pc: int) -> str:
    for name, lo, hi in _REGIONS:
        if lo <= pc < hi:
            return name
    if 0xA000 <= pc < 0xC000:
        return "BASIC ROM"
    if pc >= 0xE000:
        return "KERNAL ROM (other)"
    return "RAM  <-- not ROM"


class Recorder:
    """Transparent proxy that logs redirects and $C6 writes.

    The CPU is already paused when these commands arrive (every monitor
    command pauses it), so the extra register/stack reads cost the C64
    zero cycles and do not perturb what they measure.
    """

    def __init__(self, transport):
        self._t = transport
        self.log: list[str] = []
        self.feeds = 0

    def __getattr__(self, name):
        return getattr(self._t, name)

    def _stack(self, regs) -> str:
        sp = regs.get("SP", 0xFF)
        if sp >= 0xFF:
            return "(empty)"
        return self._t.read_memory(0x0100 + sp + 1, 0xFF - sp).hex(" ")

    def set_registers(self, regs):
        r = self._t.read_registers()
        self.log.append(
            f"redirect PC:={regs.get('PC', -1):#06x}  paused at "
            f"PC={r['PC']:#06x} SP={r['SP']:#04x} A={r['A']:#04x} "
            f"X={r['X']:#04x} Y={r['Y']:#04x} FL={r.get('FL', -1):#04x} "
            f"[{region(r['PC'])}]  stack={self._stack(r)}"
        )
        self._t.set_registers(regs)

    def write_memory(self, addr, data, **kw):
        if addr == KEYCOUNT:
            r = self._t.read_registers()
            old = self._t.read_memory(KEYCOUNT, 1)[0]
            self.log.append(
                f"$C6:={bytes(data)[0]} (was {old})  paused at "
                f"PC={r['PC']:#06x} SP={r['SP']:#04x} [{region(r['PC'])}]"
                + ("  <-- INSIDE LP2" if 0xE5B4 <= r["PC"] < 0xE5CA else "")
            )
        self._t.write_memory(addr, data, **kw)

    def inject_keys(self, codes):
        self._t.inject_keys(codes)
        self.feeds += 1  # _send_and_recv raised if VICE did not ack


# ---------------------------------------------------------------------------
# restore variants
# ---------------------------------------------------------------------------

def _restore_tail(transport):
    """Everything ``_restore_basic`` does after the redirect, verbatim."""
    transport.write_memory(0x00C6, b"\x00")
    transport.resume()
    wait_for_stable(transport, timeout=5.0, poll_interval=0.15, stable_count=2)
    before = transport.read_registers()
    transport.write_memory(tc._SCREEN_RAM, b"\x20" * tc._SCREEN_CELLS)
    transport.write_memory(0x0277, bytes([0x93, 0x3A, 0x0D]))
    transport.write_memory(0x00C6, b"\x03")
    after = transport.read_registers()
    if (before["LIN"], before["CYC"], before["PC"]) != (after["LIN"], after["CYC"], after["PC"]):
        transport.log.append(
            f"*** PAUSE DID NOT HOLD across blank+feed: {before} -> {after}"
        )
    grid = tc._wait_for_text_binary(transport, "READY.", timeout=10.0, poll_interval=0.2)
    assert grid is not None, (
        "BASIC did not print READY. after the blank\n"
        + tc._machine_failure_report(transport, "READY.")
    )


def restore_suite(transport):
    """The suite's own restore, byte for byte."""
    tc._restore_basic(transport)


def restore_e5cd_stub(transport):
    """The restore as it was before issue #170: straight into $E5CD.

    $E5CD is inside CHRIN's frame, so this only works when SP already
    points at that frame.  Same tail as the suite afterwards, so the only
    difference under test is whether SP is inherited or rebuilt.
    """
    transport.write_memory(0xCF00, bytes([0x58, 0x4C, 0xCD, 0xE5]))
    transport.set_registers({"PC": 0xCF00})
    transport.resume()
    time.sleep(0.5)
    _restore_tail(transport)


RESTORES = {"suite": restore_suite, "e5cd-stub": restore_e5cd_stub}


# ---------------------------------------------------------------------------
# the replayed sequence
# ---------------------------------------------------------------------------

def _wait(t, needle, timeout=15.0):
    return tc._wait_for_text_binary(t, needle, timeout=timeout) is not None


def cycle(t: Recorder, restore) -> str | None:
    """Return the name of the first failing step, or None."""
    def step(name):
        t.log.append(f"--- {name}")

    step("TestScreen::test_screen_grid_reads_real_screen")
    restore(t)
    ScreenGrid.from_transport(t)

    step("TestScreen::test_wait_for_text_after_print")
    restore(t)
    tc._assert_needle_absent(t, "42")
    send_text(t, "PRINT 6*7\r")
    t.resume()
    if not _wait(t, "42"):
        return "TestScreen::test_wait_for_text_after_print ('42')"

    step("TestScreen::test_wait_for_stable_on_idle")
    restore(t)
    ScreenGrid.from_transport(t)

    step("TestScreen::test_dump_screen_contains_ready")
    restore(t)
    with contextlib.redirect_stdout(io.StringIO()):  # it prints, too
        dump_screen(t, "t")

    step("TestKeyboard fixture")
    restore(t)
    ran, pcs = tc._stub_was_executed(t)
    if not ran:
        return f"TestKeyboard fixture (stub never left: {[hex(p) for p in pcs]})"
    if not ScreenGrid.from_transport(t).has_text("READY."):
        return "TestKeyboard fixture (no READY.)"

    step("TestKeyboard::test_send_text_basic_command")
    tc._assert_needle_absent(t, "5")
    send_text(t, "PRINT 2+3\r")
    t.resume()
    if not _wait(t, "5"):
        return "TestKeyboard::test_send_text_basic_command ('5')"

    step("TestKeyboard::test_send_key_single_chars")
    restore(t)
    tc._assert_needle_absent(t, "7")
    for ch in "PRINT 3+4\r":
        send_key(t, ch)
    t.resume()
    if not _wait(t, "7"):
        return "TestKeyboard::test_send_key_single_chars ('7')"

    step("TestKeyboard::test_send_text_long_batching")
    restore(t)
    tc._assert_needle_absent(t, "20")
    send_text(t, 'PRINT LEN("ABCDEFGHIJKLMNOPQRST")\r')
    t.resume()
    if not _wait(t, "20"):
        return "TestKeyboard::test_send_text_long_batching ('20')"
    return None


# ---------------------------------------------------------------------------
# failure capture
# ---------------------------------------------------------------------------

def _hexdump(t, addr, n, per=16):
    data = t.read_memory(addr, n)
    return "\n".join(
        f"  ${addr + i:04x}: {data[i:i + per].hex(' ')}"
        for i in range(0, n, per)
    )


def _disasm(entry) -> str:
    """One CPU-history record as a line of assembly."""
    from dis6502 import dis  # scripts/dis6502.py
    ins = entry["instruction"]
    try:
        text = dis(bytes(ins), entry["pc"], entry["pc"], entry["pc"] + 1)
    except Exception:
        text = f"{entry['pc']:04X}  {bytes(ins).hex(' ')}"
    return (f"{text:<32} A={entry['a']:02x} X={entry['x']:02x} "
            f"Y={entry['y']:02x} SP={entry['sp']:02x} P={entry['sr']:02x}")


def capture(t: Recorder, step: str, prev_log: list[str]) -> str:
    out = [f"*** FAILED step: {step}", "", "== production failure report =="]
    out.append(tc._machine_failure_report(t, step))
    r = t.read_registers()
    out += ["", f"== registers == {r}   [{region(r['PC'])}]"]
    sp = r["SP"]
    out.append(f"== stack above SP (${0x0100 + sp + 1:04x}-$01ff) == "
               + (t.read_memory(0x0100 + sp + 1, 0xFF - sp).hex(" ") if sp < 0xFF else "(empty)"))
    out.append("== $01F0-$01FF ==\n" + _hexdump(t, 0x01F0, 16))
    out.append("== zero page ==\n" + _hexdump(t, 0x0000, 256))
    out.append(f"== keyboard == $C6={t.read_memory(KEYCOUNT, 1)[0]} "
               f"$0277-$0280={t.read_memory(KEYBUF, 10).hex(' ')}")
    out.append("== $0300-$0333 (BASIC + KERNAL vectors) ==\n" + _hexdump(t, 0x0300, 52))
    out.append(f"== $0334-$033B == {t.read_memory(0x0334, 8).hex(' ')}   "
               f"$0360 = {t.read_memory(0x0360, 1).hex()}")
    out.append(f"== $CF00-$CF0F == {t.read_memory(0xCF00, 16).hex(' ')}")
    out.append(f"== keyboard feeds acknowledged this cycle == {t.feeds}")
    kinds = collections.Counter(resp.response_type for _, resp in t._event_queue)
    out.append(f"== transport event queue == {len(t._event_queue)} entries: "
               + ", ".join(f"{k:#04x}x{v}" for k, v in sorted(kinds.items())))
    try:
        hist = t.cpu_history(4096)
        out.append(f"== CPU history: {len(hist)} records, last 120 ==")
        for e in hist[-120:]:
            out.append("  " + _disasm(e))
        # first departure from ROM, scanning backwards
        for i in range(len(hist) - 1, 0, -1):
            if hist[i]["pc"] < 0xA000 and hist[i - 1]["pc"] >= 0xA000:
                out.append(f"== last ROM->RAM transition at record {i} ==")
                for e in hist[max(0, i - 24):i + 4]:
                    out.append("  " + _disasm(e))
                break
    except Exception as e:
        out.append(f"== CPU history unavailable: {type(e).__name__}: {e}")
    out += ["", "== redirect / $C6 log, FAILING cycle =="] + ["  " + l for l in t.log]
    out += ["", "== redirect / $C6 log, PREVIOUS (passing) cycle =="] + ["  " + l for l in prev_log]
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cycles", nargs="?", type=int, default=40)
    ap.add_argument("--restore", choices=sorted(RESTORES), default="suite")
    ap.add_argument("--stop-at-first", action="store_true")
    ap.add_argument("--verbose", action="store_true", help="print the redirect log of passing cycles too")
    args = ap.parse_args()
    restore = RESTORES[args.restore]

    print(f"import path: {c64_test_harness.__file__}", flush=True)
    print(f"restore variant: {args.restore}", flush=True)

    failures: list[tuple[int, str]] = []
    prev_log: list[str] = []
    for i in range(1, args.cycles + 1):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        proc = ViceProcess(ViceConfig(port=port, warp=True, sound=False))
        proc.start()
        try:
            raw = connect_binary_transport(port, proc=proc, timeout=30)
            t = Recorder(raw)
            try:
                try:
                    step = cycle(t, restore)
                except AssertionError as e:
                    step = f"assertion: {str(e).splitlines()[0]}"
                if step:
                    failures.append((i, step))
                    print(f"\n*** cycle {i} FAILED: {step}", flush=True)
                    print(capture(t, step, prev_log), flush=True)
                    if args.stop_at_first:
                        return
                elif args.verbose:
                    print(f"  cycle {i}: ok", flush=True)
                    for l in t.log:
                        print("     ", l, flush=True)
                elif i % 5 == 0:
                    print(f"  cycle {i}: ok", flush=True)
                prev_log = t.log
            finally:
                raw.close()
        finally:
            proc.stop()
    print(f"\n=== {len(failures)} of {args.cycles} cycles failed "
          f"(restore={args.restore}) ===", flush=True)
    for i, step in failures:
        print(f"  cycle {i}: {step}", flush=True)


if __name__ == "__main__":
    main()
