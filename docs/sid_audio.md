# SID measurement and audio capture

Three failure modes in this area produce **plausible data instead of an
error**. None of them raises, and all three were found by a downstream
SID-measurement project building against this harness (issues #193, #195,
#196). This page is the reference for what is true and what to do.

Source citations marked `S` are VICE 3.10 (`~/Documents/vice-src/3.10`)
or the Ultimate 3.15 pre-release firmware tree
(`~/Documents/1541u-315preview`).

---

## 1. `sound=False` disables SID *emulation*, not just audio output

`ViceConfig.sound` defaults to `False`, which emits VICE's `+sound`. That
stops the sound core being clocked, and reSID with it. Reads of
`$D400-$D41F` then come from the sound-off fallback:

| Register | Value with sound off | Source |
|---|---|---|
| `$D41B` (OSC3) | `maincpu_clk % 256` | S `sid.c:137`, `sid.c:279` |
| `$D41C` (ENV3) | `maincpu_clk % 256` | same |
| `$D419` / `$D41A` (paddles) | `0xff` | S `sid.c:134`, `sid.c:277` |
| everything else | `0` | S `sid.c:139`, `sid.c:281` |

A sampling loop reading OSC3 therefore gets a clean ramp at its own
stride. It does not look broken; it looks like a working oscillator.
Measured downstream against an analytic oracle (a sawtooth swept over
phase 0..7, where OSC3 is exactly `phase + 3`):

```
sound=False      : [203, 27, 109, 193, 23, 111, 201, 37]   <- maincpu_clk % 256
sounddev="dummy" : [0, 0, 0, 0, ...]                        <- frozen
sound=True/wav   : [3, 4, 5, 6, 7, 8, 9, 10]                <- correct
```

### `sounddev="dummy"` is a *different* broken mode

Sound is enabled, so the core runs — but nothing drains the buffer, the
SID stops advancing, and reads return real state frozen at an arbitrary
past moment. There is no ramp to notice it by, and a freeze-and-read
health check cannot distinguish it from a healthy SID holding `TEST`.
Detecting it needs a *released* oscillator and an assertion that the
value moves.

|                            | freeze-and-read probe | moving-oscillator ramp |
|----------------------------|-----------------------|------------------------|
| `sound=False`              | CLOCK (caught)        | WRONG                  |
| `sound=True` (default dev) | "answering"           | RAMP                   |
| `sound=True -soundvolume 0`| "answering"           | RAMP                   |
| `sounddev="dummy"`         | "answering" (MISSED)  | FROZEN                 |
| `sounddev="wav"`           | "answering"           | RAMP                   |

### What to use

```python
from c64_test_harness import headless_sid_config

cfg = headless_sid_config()          # sound on, sounddev="wav", -soundvolume 0
```

A file writer always drains, so this is the only healthy option that does
not depend on the host having a working audio device. `-soundvolume 0`
keeps a test run from screeching and is safe **for register-domain
measurement**: the volume is applied *after* reSID has been clocked
(S `sound.c:1432` generates, `sound.c:1441-1449` attenuates).

It is **not** safe for audio-domain measurement: at volume 0 `amp` is 0
and VICE `memset`s the sample buffer to zero (S `sound.c:1446`) before it
reaches the play *or* record device, so the WAV is silence.
`render_wav()` refuses `soundvolume=0` for that reason.

The default stays `False`. Turning it on globally would open a host audio
device on every launch, which is not what a headless harness should do —
so the defence is the warning, the predicate
(`sid_emulation_enabled(cfg)`), and this page.

---

## 2. VICE discards audio under warp — `-soundrecdev` does not rescue it

`-soundwarpmode 1` (which the harness always passes) keeps an *enabled*
sound core generating samples under warp, so **register**-read
experiments work warped. It does not get the audio to a device:

- `sound_flush()` zeroes the buffer outright when warp is on and no
  record device is configured (S `sound.c:1528`).
- With a record device configured that early return is skipped — but the
  loop that writes to the play *and* record devices is
  `while (!warp_mode_enabled)` (S `sound.c:1573-1613`), so under warp it
  never executes, and `snddata.bufptr -= nr` drops the samples anyway
  (with `Sound buffer overflow (cycle based)` in the log, S
  `sound.c:1407`).

So the fix is **warp off**, full stop. Issue #196's suggestion that a
configured `-soundrecdev` is an alternative is wrong; that is the one
correction this harness makes to the reported findings.

`render_wav()` sets `warp=False` and refuses a `-warp` smuggled in
through `extra_args`, since VICE takes the last setting of a resource and
`extra_args` is appended last.

---

## 3. The U64 audio stream is not 48000 Hz

`DEFAULT_SAMPLE_RATE` is 48000 — the nominal figure, kept for API
stability. The device's NTSC stream is clock-derived, so the true figures
are exact rationals:

```
Fc    = 315e6/88   = 3579545.4545... Hz
phi2  = Fc * 2/7   = 11250000/11    = 1022727.2727... Hz
audio = Fc * 3/224 =  2109375/44    =   47940.3409... Hz
```

48000 against that is **1244 ppm**, about **75 ms of slip per minute** —
fatal within a single measurement block, not a drift to correct
afterwards.

`Fc` cancels out of the ratio:

```
phi2 : audio = (2/7) / (3/224) = 64 : 3   exactly
```

Three audio samples span exactly 64 phi2 cycles regardless of what the
crystal actually runs at, so crystal error and drift cancel. Two things
follow:

- **Coherent capture** — size blocks in multiples of 64 cycles and each
  holds a whole number of samples. `coherent_block_cycles(samples)`
  computes it and refuses a sample count that is not a multiple of 3.
- **Equivalent-time reconstruction** — stepping the start offset over
  1..63 cycles across repeats places the sampling instants at 64
  sub-sample phases, an effective 3068182 Hz (exactly 3 per phi2 cycle),
  resolving repeatable trajectories far below one audio sample.

Do the arithmetic in `Fraction`, not floats: `1022727.14 / 47940.0` gives
21.333482 against a true 21.333333, a 7 ppm error that can fail a lock
test for reasons unrelated to the hardware.

```python
from c64_test_harness import U64_NTSC_AUDIO_RATE_HZ, AudioCapture

cap = AudioCapture(sample_rate=U64_NTSC_AUDIO_RATE_HZ)
```

The WAV header still has to be an integer; passing the exact rate makes
it 47940 (7 ppm) instead of 48000 (1244 ppm), and
`CaptureResult.sample_rate_exact` carries the rational.

**PAL does not lock**, structurally rather than by a different constant:
PAL's phi2 divides 17734472 while its colour carrier is 17734475/4 —
different base integers, and the ratio does not reduce. No PAL audio
constant is published here because the U64's PAL stream rate has not been
measured on this bench.

### A dropped packet destroys the time base

`AudioCapture` counts gaps but does **not** pad them: the capture is the
concatenation of the payloads that arrived. After a drop, sample index no
longer maps to time and every downstream alignment is off by an unknown
amount. The WAV is well-formed either way, so nothing downstream notices.

```python
result = cap.stop(wav_path="run.wav")
assert result.time_base_intact          # packets_dropped == 0
```

---

## 4. Capturing around a run you drive yourself

`capture_sid_u64()` owns the whole run: it hands a `.sid` to the
firmware's player and resets the C64 in its `finally`. That reset
destroys a program reached through a host handshake. Two escapes:

```python
from c64_test_harness import capture_u64_audio

with capture_u64_audio(client, "run.wav") as captured:
    target.jsr(0xC000)
    target.wait_for_text("DONE")
result = captured[0]
```

`capture_u64_audio()` brings the UDP receiver and the device's audio
stream up and down around an arbitrary block and touches nothing else —
no player, no reset. `capture_sid_u64(..., reset_after=False)` is the
narrower escape when the firmware player *is* wanted but the reset is
not (the tune then keeps playing).

---

## 5. Remapping SIDs safely

`Auto Address Mirroring` ships **Enabled** (firmware
`u64_config.cc:411`, `def` column 1) with all four slots decoded at
`$D400`. When enabled, `auto_mirror()` (`u64_config.cc:857-858`,
implementation at `:2378-2430`) clears decode mask bits A5..A9 wherever
the in-range slots agree on that bit — filling `$D400-$D7FF` with
mirrors.

**Giving the slots distinct base addresses is not sufficient.** With
mirroring on, an address *no* slot occupies is answered by a widened
decode of one that does, so a two-chip comparison run reads the same chip
twice and looks entirely correct doing it. (Four slots at
`$D400/$D420/$D440/$D460` still get A7..A9 widened, because all four
agree on those bits.)

The safe recipe, in order:

1. Snapshot the whole `SID Addressing` category, not just the slots being
   moved.
2. Set `Auto Address Mirroring` to `Disabled` **and assert the
   read-back**.
3. Give *every* slot a base of its own, including the ones not under
   test — a second real SID left sharing a decode is written by every
   measurement aimed at the first.
4. Restore **per item** on every exit path.

Step 4 is per-item deliberately: `set_config_items()` issues one PUT per
item in insertion order and does not catch per-item failures, so a single
rejection aborts the batch and strands the rest of the user's settings in
the test's configuration. `restore_config_items()` attempts every item
and raises `Ultimate64RestoreError` at the end. (The *write* paths keep
`set_config_items` on purpose — `set_reu()` relies on a rejected
`Cartridge` write aborting before the REU is half-enabled.)

All of it is packaged:

```python
from c64_test_harness import isolated_sid_addressing, SidSlot

with isolated_sid_addressing(
    client, {SidSlot.SOCKET1: "$D400", SidSlot.SOCKET2: "$D420"}
) as addresses:
    ...   # addresses is the full four-slot map now in effect
```

`others="unmapped"` is the strongest isolation (everything not named goes
to `Unmapped`, whose firmware offset `0x01` is odd where every real
decode is even, so it can never alias). `others="leave"` touches nothing
else and only checks the result.

`snapshot_state()` / `restore_state()` now cover the `SID Addressing`
category too, so a run that used the helpers above and crashed still gets
the category put back by the surrounding state restore.

---

## Test coverage

| File | Covers |
|---|---|
| `tests/test_vice_sound_sid.py` | §1, §2 — predicates, launch warnings, `-soundvolume`, `render_wav` guards |
| `tests/test_u64_audio_rates.py` | §3, §4 — exact rates, coherent blocks, WAV header, drop semantics, `reset_after`, `capture_u64_audio` |
| `tests/test_sid_isolation.py` | §5 — mirroring read-back, allocation, restore ordering |

All three are offline: no x64sc is spawned and no device is touched.
