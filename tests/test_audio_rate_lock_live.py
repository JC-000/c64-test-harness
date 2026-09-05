"""``U64_NTSC_AUDIO_RATE_HZ`` measured through the ``64:3`` identity.

Issue #205, following up #195 / PR #201.  ``2109375/44`` Hz shipped as
the reporter's measurement because the rate is generated in the FPGA
and nothing on this side could check it.  The identity is its own
instrument: **three audio samples span exactly 64 phi2 cycles**
whatever the crystal does, so the ratio can be measured without an
accurate host clock, and crystal error cancels out of it.

Method (all on the 6510; the host touches nothing on the wire during
the window, because a REST read is a DMA that halts the 6510):

1. Voice 1 sounds a 3.7 kHz pulse at sustain 15 with the master volume
   at 0.  ``SEI``; the screen is blanked and two frames are waited out
   so no badline can steal a cycle.
2. CIA2 timers A and B are chained as a 32-bit phi2 counter and
   started; the master volume goes 0 -> 15 (**tone on**); a
   cycle-counted loop runs; the volume goes 15 -> 0 (**tone off**); the
   timers stop and their count goes to RAM.  The timer count is the
   measured cycle count; the loop's predicted count is a cross-check
   that any DMA stall or badline would break (they agree to 11 cycles,
   the fixed instruction overhead between the timer stores and the
   volume stores).
3. ``AudioCapture`` records the stream across the window to a scratch
   path.  Tone edges are located by **first difference** -- the pulse
   swings ~10000 counts between consecutive samples, while the DC step
   the tone-off leaves decays through the output high-pass at ~80
   counts/sample and would put an amplitude threshold ~150 samples late.
4. ``samples * 64 == cycles * 3`` within tolerance, per window and as
   the slope across windows of two lengths (a fixed edge offset cancels
   in the slope).  A capture with ``packets_dropped != 0`` has no time
   base and is retried, never analysed.

Discrimination: at 48000 Hz the 60 s window would hold ~3580 more
samples than 64:3 predicts (1244 ppm); the per-window residual from the
edge detector is ~25 samples, and the slope's is a few.

Measured 2026-09-05 on the U64E (fw 3.15, NTSC, 1 MHz): see the
docstring on ``U64_NTSC_AUDIO_RATE_HZ`` for the numbers.

NTSC only: the device's ``System Mode`` is checked and a PAL device
skips.  No PAL constant is published (it does not reduce to a small
ratio, and it is unmeasured).

Gate: ``AUDIO_RATE_LIVE=1``.  Host: ``U64_HOST`` (default ``10.43.23.81``).
"""
from __future__ import annotations

import os
import socket
import struct
import time
import wave
from fractions import Fraction
from pathlib import Path

import pytest

from c64_test_harness import create_manager, send_text, wait_for_text
from c64_test_harness.backends.u64_audio_capture import (
    NTSC_PHI2_HZ,
    PHI2_CYCLES_PER_AUDIO_SAMPLE,
    U64_NTSC_AUDIO_RATE_HZ,
    AudioCapture,
)
from c64_test_harness.backends.ultimate64_helpers import (
    CAT_U64_SPECIFIC,
    check_measurement_environment,
)

_HOST = os.environ.get("U64_HOST", "10.43.23.81")

pytestmark = pytest.mark.skipif(
    os.environ.get("AUDIO_RATE_LIVE") != "1",
    reason="AUDIO_RATE_LIVE=1 not set -- live audio rate-lock test disabled",
)

#: Clear of every HARNESS_SCRATCH span and of the $0360 trampoline.
CODE_ADDR = 0xC900
RESULT_ADDR = 0xCA00     # TA lo, TA hi, TB lo, TB hi, done flag; counter at +8
DONE = 0x42
#: A port of its own, so a neighbour on the default 11001 is not disturbed.
CAPTURE_PORT = 11021

#: Cycles per pass of the outer loop, and the fixed cost around it --
#: see ``_build`` for the count.
_OUTER_PASS = 329226
#: Fixed cycles between the timer start/stop stores and the volume
#: stores (LDA #imm + STA abs on each side), the loop count's offset
#: from the CIA count.
_TIMER_TO_TONE = 12

#: Window lengths as outer-loop counts: ~10 s, ~60 s, ~10 s, interleaved
#: so a drift over the run would show as a difference between the two
#: short windows.
WINDOWS = (31, 187, 31)
#: Per-window tolerance against 64:3.  The edge detector's fixed offset
#: is ~25 samples: 50 ppm on the short window, 8 ppm on the long one.
PER_WINDOW_PPM = 150.0
#: Slope tolerance: the fixed offset cancels, leaving detector noise.
SLOPE_PPM = 30.0
#: How far the nominal 48000 Hz hypothesis sits from 64:3.
NOMINAL_48K_PPM = 1244.0
#: Retries for a capture that dropped packets (no time base).
CAPTURE_ATTEMPTS = 3


class _Asm:
    """Just enough 6502 to write the probe with checked branches."""

    def __init__(self, org: int) -> None:
        self.org = org
        self.out = bytearray()
        self.labels: dict[str, int] = {}
        self.fix: list[tuple[int, str]] = []

    @property
    def pc(self) -> int:
        return self.org + len(self.out)

    def label(self, name: str) -> None:
        self.labels[name] = self.pc

    def emit(self, *b: int) -> None:
        self.out += bytes(b)

    def lda_imm(self, v: int) -> None: self.emit(0xA9, v)
    def ldx_imm(self, v: int) -> None: self.emit(0xA2, v)
    def ldy_imm(self, v: int) -> None: self.emit(0xA0, v)
    def sta(self, a: int) -> None: self.emit(0x8D, a & 0xFF, a >> 8)
    def lda(self, a: int) -> None: self.emit(0xAD, a & 0xFF, a >> 8)
    def and_imm(self, v: int) -> None: self.emit(0x29, v)
    def ora_imm(self, v: int) -> None: self.emit(0x09, v)
    def dec(self, a: int) -> None: self.emit(0xCE, a & 0xFF, a >> 8)
    def dex(self) -> None: self.emit(0xCA)
    def dey(self) -> None: self.emit(0x88)
    def sei(self) -> None: self.emit(0x78)
    def cli(self) -> None: self.emit(0x58)
    def rts(self) -> None: self.emit(0x60)

    def bne(self, label: str) -> None:
        self.fix.append((len(self.out) + 1, label))
        self.emit(0xD0, 0x00)

    def link(self) -> bytes:
        for pos, label in self.fix:
            src = self.org + pos + 1
            dst = self.labels[label]
            off = dst - src
            assert -128 <= off <= 127, (label, off)
            # A taken branch across a page costs one more cycle; the
            # budget assumes none does.
            assert (src & 0xFF00) == (dst & 0xFF00), (label, src, dst)
            self.out[pos] = off & 0xFF
        return bytes(self.out)


def _build(outer: int) -> tuple[bytes, int]:
    """The probe routine and its predicted tone-on-to-tone-off cycles."""
    a = _Asm(CODE_ADDR)
    cnt = RESULT_ADDR + 8
    a.sei()
    a.lda_imm(0x7F); a.sta(0xDD0D); a.lda(0xDD0D)          # CIA2 IRQs off, ack
    a.lda_imm(0x00); a.sta(0xDD0E); a.sta(0xDD0F)          # timers stopped
    a.lda_imm(0x00); a.sta(0xD418)                         # volume 0
    a.lda_imm(0x00); a.sta(0xD400); a.lda_imm(0xF0); a.sta(0xD401)   # 3745 Hz
    a.lda_imm(0x00); a.sta(0xD402); a.lda_imm(0x08); a.sta(0xD403)   # PW 50 %
    a.lda_imm(0x00); a.sta(0xD405); a.lda_imm(0xF0); a.sta(0xD406)   # A0 D0 S15 R0
    a.lda_imm(0x41); a.sta(0xD404)                         # pulse, gate on
    a.lda(0xD011); a.and_imm(0xEF); a.sta(0xD011)          # DEN off
    a.ldy_imm(0x00)                                        # > 2 frames
    a.label("w1"); a.ldx_imm(0x00)
    a.label("w2"); a.dex(); a.bne("w2"); a.dey(); a.bne("w1")
    a.lda_imm(0xFF)
    a.sta(0xDD04); a.sta(0xDD05); a.sta(0xDD06); a.sta(0xDD07)  # latches $FFFF
    a.lda_imm(0x50); a.sta(0xDD0F)      # TB: force load, counts TA underflow
    a.lda_imm(0x51); a.sta(0xDD0F)      # TB: start
    a.lda_imm(0x11); a.sta(0xDD0E)      # TA: force load + start
    a.lda_imm(0x0F); a.sta(0xD418)      # TONE ON
    a.lda_imm(outer); a.sta(cnt)
    a.label("outer"); a.ldy_imm(0x00)
    a.label("mid");   a.ldx_imm(0x00)
    a.label("inner"); a.dex(); a.bne("inner")
    a.dey(); a.bne("mid")
    a.dec(cnt); a.bne("outer")
    a.lda_imm(0x00); a.sta(0xD418)      # TONE OFF
    a.lda_imm(0x00); a.sta(0xDD0E)      # stop TA
    a.sta(0xDD0F)                       # stop TB
    a.lda(0xDD04); a.sta(RESULT_ADDR + 0)
    a.lda(0xDD05); a.sta(RESULT_ADDR + 1)
    a.lda(0xDD06); a.sta(RESULT_ADDR + 2)
    a.lda(0xDD07); a.sta(RESULT_ADDR + 3)
    a.lda(0xD011); a.ora_imm(0x10); a.sta(0xD011)          # DEN on
    a.lda_imm(0x40); a.sta(0xD404)                         # gate off
    a.cli()
    a.lda_imm(DONE); a.sta(RESULT_ADDR + 4)
    a.rts()
    code = a.link()
    # Store-to-store, TONE ON to TONE OFF.  Inner: 255*(DEX 2 + BNE 3)
    # + (2 + 2) = 1279, +LDX 2 = 1281 per mid; +DEY 2 + BNE 3 = 1286 per
    # mid, last mid 1285 -> 255*1286 + 1285 + LDY 2 = 329217 per outer;
    # +DEC 6 + BNE 3 = 329226, last outer 329225.  Around it: LDA #n 2
    # + STA cnt 4 before, LDA #0 2 + STA $D418 4 after.
    predicted = 6 + outer * _OUTER_PASS - 1 + 6
    return code, predicted


def _cia_cycles(raw: bytes) -> int:
    ta = raw[0] | (raw[1] << 8)
    tb = raw[2] | (raw[3] << 8)
    return (0xFFFF - ta) + (0xFFFF - tb) * 65536


def _tone_edges(wav_path: Path, frac: float = 0.25) -> tuple[int, int]:
    """(first, last) sample index of the tone, by first difference."""
    with wave.open(str(wav_path), "rb") as wf:
        n = wf.getnframes()
        ch = wf.getnchannels()
        raw = wf.readframes(n)
    left = struct.unpack(f"<{n * ch}h", raw)[0::ch]
    diffs = [abs(b - a) for a, b in zip(left, left[1:])]
    thr = int(max(diffs) * frac)
    assert thr > 1000, f"no tone in the capture (peak step {max(diffs)})"
    first = next(i for i, d in enumerate(diffs) if d > thr) + 1
    last = len(diffs) - 1 - next(i for i, d in enumerate(reversed(diffs)) if d > thr)
    return first, last


def _local_ip(remote: str) -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((remote, 80))
        return s.getsockname()[0]


@pytest.fixture(scope="module")
def target():
    with create_manager(
        backend="u64", u64_hosts=_HOST, lock_timeout=600.0
    ) as mgr:
        with mgr.instance() as tgt:
            client = tgt.client
            check_measurement_environment(client)
            mode = client.get_config_category(CAT_U64_SPECIFIC)[
                CAT_U64_SPECIFIC
            ]["System Mode"]
            if mode != "NTSC":
                pytest.skip(
                    f"System Mode is {mode!r}; only the NTSC rate is "
                    f"claimed and no PAL constant is published"
                )
            client.reset()
            time.sleep(3.0)
            assert wait_for_text(
                tgt.transport, "READY.", timeout=20.0, poll_interval=0.5,
                verbose=False,
            ) is not None, "C64 never reached READY after reset"
            try:
                yield tgt
            finally:
                try:
                    client.stream_audio_stop()
                except Exception:  # noqa: BLE001 -- best effort on teardown
                    pass


def _measure(target, outer: int, wav_dir: Path, tag: str) -> dict:
    """One window: returns cycles, samples and the capture bookkeeping."""
    client = target.client
    t = target.transport
    code, predicted = _build(outer)
    dest = f"{_local_ip(_HOST)}:{CAPTURE_PORT}"
    for attempt in range(CAPTURE_ATTEMPTS):
        t.write_memory(RESULT_ADDR, bytes(16))
        t.write_memory(CODE_ADDR, code)
        cap = AudioCapture(
            port=CAPTURE_PORT, sample_rate=U64_NTSC_AUDIO_RATE_HZ,
            recv_buf_size=8 << 20,
        )
        cap.start()
        client.stream_audio_start(dest)
        time.sleep(1.0)
        send_text(t, f"SYS {CODE_ADDR}\r")
        # Hands off the wire until the window is over.
        time.sleep(predicted / float(NTSC_PHI2_HZ) + 2.0)
        deadline = time.monotonic() + 20.0
        while t.read_memory(RESULT_ADDR + 4, 1) != bytes([DONE]):
            assert time.monotonic() < deadline, "probe routine never finished"
            time.sleep(0.1)
        time.sleep(0.5)
        client.stream_audio_stop()
        result = cap.stop(wav_path=wav_dir / f"{tag}-attempt{attempt}.wav")
        if not result.time_base_intact:
            continue
        cycles = _cia_cycles(t.read_memory(RESULT_ADDR, 4))
        assert abs(cycles - predicted - _TIMER_TO_TONE) <= 4, (
            f"CIA counted {cycles} phi2 cycles, loop predicts {predicted} "
            f"(+{_TIMER_TO_TONE} fixed): a badline or a DMA stall reached "
            f"the window"
        )
        first, last = _tone_edges(result.wav_path)
        return {
            "outer": outer, "cycles": cycles, "samples": last - first,
            "packets": result.packets_received, "attempt": attempt,
        }
    pytest.fail(
        f"{CAPTURE_ATTEMPTS} captures in a row dropped packets; the "
        f"sample index is not a clock on this network right now"
    )


def _ppm(samples: int, cycles: int) -> float:
    return (float(Fraction(samples * 64, cycles * 3)) - 1) * 1e6


def test_audio_rate_locks_64_to_3(target, tmp_path: Path) -> None:
    assert PHI2_CYCLES_PER_AUDIO_SAMPLE == Fraction(64, 3)
    points = [
        _measure(target, outer, tmp_path, f"w{i}-{outer}")
        for i, outer in enumerate(WINDOWS)
    ]
    lines = []
    for p in points:
        p["ppm"] = _ppm(p["samples"], p["cycles"])
        seconds = p["cycles"] / float(NTSC_PHI2_HZ)
        p["ppm_vs_48000"] = (p["samples"] / seconds / 48000 - 1) * 1e6
        lines.append(
            f"outer={p['outer']:3d} cycles={p['cycles']:9d} samples={p['samples']:8d} "
            f"64:3 -> {p['cycles'] * 3 / 64:10.1f} ({p['ppm']:+7.1f} ppm) "
            f"48000 -> {seconds * 48000:10.1f} ({p['ppm_vs_48000']:+7.1f} ppm) "
            f"packets={p['packets']} attempt={p['attempt']}"
        )
    print("\n".join(lines))

    for p in points:
        assert abs(p["ppm"]) <= PER_WINDOW_PPM, (
            f"window {p['outer']}: samples*64/(cycles*3) is {p['ppm']:+.1f} ppm "
            f"from 1 (tolerance {PER_WINDOW_PPM})\n" + "\n".join(lines)
        )
        assert abs(p["ppm_vs_48000"]) > NOMINAL_48K_PPM / 2, (
            f"window {p['outer']}: the nominal 48000 Hz is NOT rejected "
            f"({p['ppm_vs_48000']:+.1f} ppm)\n" + "\n".join(lines)
        )

    # Slope between the long window and the mean of the two short ones:
    # the detector's fixed edge offset cancels here.
    long_ = next(p for p in points if p["outer"] == max(WINDOWS))
    shorts = [p for p in points if p["outer"] == min(WINDOWS)]
    d_samples = long_["samples"] - sum(p["samples"] for p in shorts) / len(shorts)
    d_cycles = long_["cycles"] - sum(p["cycles"] for p in shorts) / len(shorts)
    slope_ppm = (d_samples * 64 / (d_cycles * 3) - 1) * 1e6
    print(f"slope: {d_samples:.1f} samples over {d_cycles:.1f} cycles -> "
          f"{slope_ppm:+.2f} ppm from 64:3")
    assert abs(slope_ppm) <= SLOPE_PPM, (
        f"slope across window lengths is {slope_ppm:+.1f} ppm from 64:3 "
        f"(tolerance {SLOPE_PPM})\n" + "\n".join(lines)
    )
    # The two short windows agree with each other (no drift over the run).
    assert abs(shorts[0]["samples"] - shorts[1]["samples"]) <= 40, shorts
