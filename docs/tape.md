# Tape: TAP capture, TAP playback and a cassette-port stimulus

Issue #584. The firmware's tape recorder and TAP player have no REST route,
so the harness drives them through the Telnet Remote Menu and checks the
files over FTP (`storage_*`). The TAP file helpers and the C64-side stimulus
need no device and work on any backend.

## Supported products

The recorder and the player are two separate FPGA options
(`CAPAB_C2N_RECORDER`, `CAPAB_C2N_STREAMER`; generics `g_c2n_recorder` /
`g_c2n_streamer` in the top levels), so capture and playback have different
product lists. All of them mean the cartridge in a real C64.

| Product | Playback (player) | Capture (recorder) | Source |
|---|---|---|---|
| Ultimate-II+, II+L | yes | yes | `u2p_riscv_lattice.vhd`, `u2p_riscv.vhd`, `u2p_nios2_solo.vhd` set both |
| 1541 Ultimate-II, dual-drive image | yes | yes | `ultimate_mb_700a.vhd`, `ultimate_riscv_700a.vhd`: `g_c2n_recorder => g_dual_drive`; built with `g_dual_drive=true` (`target/fpga/mb700dd`, `mb700gm`, `rv700dd`) |
| 1541 Ultimate-II, single-drive image | yes | **no** | same files; built with `g_dual_drive=false` (`target/fpga/mb700`, `rv700au`) |

The single-drive and dual-drive 1541 Ultimate-II builds ship as separate
update images, so which one a cartridge runs depends on the image it was
flashed with.
| Ultimate 64, Ultimate 64 Elite, C64 Ultimate | no | no | see below |

**An Ultimate 64, Ultimate 64 Elite or C64 Ultimate has no tape hardware.**
No top level for them sets those generics (source-read, not traced
exhaustively), and neither of this project's bench devices lists a Tape task
category (bench observation). There the capture and playback calls raise
`TapeUnsupportedError`, which names the product.

From the browser root an idle device and a product without tape hardware
look the same (no `Tape` category), so `tape_status` cannot tell them apart.
`tape_require_supported(host, directory)` can: inside a writable directory a
product with a recorder always lists a capture item. Call it first.

One case still looks like "unsupported" there: a directory the firmware does
not treat as writable (a write-protected card, a volume mounted read-only).
The capture items are then disabled and the menu lists no `Tape` category,
whatever FTP allows. Pass `tap_path=` with a `.tap` already on the device and
the TAP player is asked as well, since its menu items do not depend on
writability: a player without a capture item raises
`TapeCaptureNotOfferedError`, and neither raises `TapeUnsupportedError`.
`TapeCaptureNotOfferedError` has two possible causes that the menus cannot
tell apart: the directory is not writable to the firmware, or the build has a
player and no recorder (a single-drive 1541 Ultimate-II). Playback works in
both.

## Evidence grade

Nothing here has run on a device, and this project's bench has no supported
product: the live module `tests/test_tape_live.py` (gate `TAPE_LIVE=1`) skips
there with the unsupported reason and needs an Ultimate-II+ to run. The
harness itself has never been run against an Ultimate-II+, so its REST
transport, `create_manager(backend="u64")`, the storage helpers and the
lock's address handling are unproven there as well.

The menu names and the recorder's behaviour are read from the 1541ultimate
source at `69a5f83eb` (a working tree on a development branch of the 3.x
line; the tape files were not compared with a release tag). The offline
tests run against a fake built from that reading.
`tests/test_tape_vice_live.py` checks the TAP encoder against a real KERNAL
under VICE and had not been run when this page was written.

## The pieces

| What | Where |
|---|---|
| `tape_require_supported`, `tape_capture_start`, `tape_capture_stop`, `tape_play`, `tape_stop`, `tape_status`, `tape_load_and_verify` | `c64_test_harness.backends.ultimate64_tape` (package-root exports) |
| `build_tap_from_prg`, `parse_tap`, `build_tap`, `decode_tap_blocks`, `tape_test_program` | `c64_test_harness.tape` (package-root exports) |
| `build_tape_pulse_stimulus`, `run_tape_stimulus`, `cassette_buffer_reserved` | `c64_test_harness.tape` (package-root exports) |

## Rules the helpers enforce

- **The `DeviceLock` first.** Every device-touching function checks that this
  process holds the device's lock before any FTP or telnet connection, reads
  included.
- **Never `/Temp`, never `/Flash`.** A capture target goes through the same
  path check as `storage_put_file` before any menu key. A capture grows with
  every pulse for as long as it runs, so stop it in a `finally`, keep it
  short, and use a volume with ample free space. Do not use a capture to fill
  a volume.
- **Never overwrite.** The target name must be free (compared without case).
- **Menu items by name.** The driver reads the menu, finds the label, moves
  the highlight one key at a time and reads it back. It never counts cursor
  steps. The Tape menu's order is not fixed by the source (two subsystems add
  their items), which is one reason.
- **Unknown labels stop the call.** The recording / playing / paused state is
  read from the Tape menu's labels. A menu with any label outside the six in
  the table below raises `RemoteMenuUnexpectedScreenError`; it is never read
  as "idle".
- **A stop that stopped nothing says so.** If `tape_capture_stop` finds no
  `Finish Rec. to TAP` to choose but the capture's file still has a zero
  length field, it raises `TapeCaptureNotStoppedError` instead of reporting a
  status: the recorder writes that field when it closes the file. Do not
  delete the file until `tape_status` reads "not recording". (An empty
  capture that was already finished also has a zero field, so a second stop
  of one raises too.)
- **One ENTER per change.** The ENTER that creates the capture file, starts
  the tape, or pauses/stops it is sent once and its effect read from a fresh
  connection. It is never resent.

## The menu, as read from source

| Where | Item | Source |
|---|---|---|
| task menu (F5; F1 on the framed C64U layout) | `Tape` | `tape_recorder.cc:108`, `tape_controller.cc:76` |
| Tape, not recording | `Sample tape to TAP`, `Capture save to TAP` | `tape_recorder.cc:109-110` |
| Tape, recording | `Finish Rec. to TAP` | `tape_recorder.cc:111` |
| Tape, a tape playing | `Pause Tape Playback` | `tape_controller.cc:77` |
| Tape, a tape paused | `Resume Tape Playback`, `Stop Tape Playback` | `tape_controller.cc:78-79` |
| a `.tap` file's context menu | `Enter (index)`, `Run Tape`, `Write to Tape`, `Start Tape` | `filetype_tap.cc:153-159` |
| name prompt | `Give name for tap file..` (22 characters) | `tape_recorder.cc:335` |
| after Finish | popup `Tape capture stopped.` (Ok) | `tape_recorder.cc:75` |

What follows from the source:

- **A capture goes to the directory the browser is in** when the task menu is
  opened, so the driver browses into the target directory first. The capture
  items are disabled where the path is not writable; the browser root is not,
  so from the root the `Tape` category appears only while something is
  recording or playing.
- **`Capture save to TAP` records the cassette write line**: rising edges,
  timed in phi2 ticks (the C64's 1 MHz clock), and it pulls the sense line
  itself while it records. The motor line is not consulted. `Sample tape to TAP` records the
  read line (a real datasette) and is not driven by the harness.
- **Stop is two steps.** `Stop Tape Playback` is offered only while the tape
  is paused, so `tape_stop` chooses Pause, then Stop.
- **`Start Tape` presses play and types nothing.** The C64 needs its own
  `LOAD`. `Run Tape` resets the machine and types it; the harness does not
  use it.
- **A tape stays loaded** until its last pulse has played or it is stopped.
  The KERNAL stops reading before the file's trailer, so "playing" in
  `tape_status` can stay true after the load has finished. It is not a
  completion signal; the helpers wait by time.

## Capturing

```python
from c64_test_harness import (
    DeviceLock, build_tape_pulse_stimulus, run_tape_stimulus,
    storage_delete_file, tape_capture_start, tape_capture_stop, tape_require_supported,
)

with DeviceLock(host):
    tape_require_supported(host, "/USB1/captures")             # TapeUnsupportedError on a U64
    stim = build_tape_pulse_stimulus(3000, tap_value=0x30)     # 3000 short pulses
    cap = tape_capture_start(host, "/USB1/captures", "run1")   # -> /USB1/captures/run1.tap
    try:
        finished = run_tape_stimulus(target, stim, reset=True)
    finally:
        res = tape_capture_stop(host, cap)
    assert finished and res.status == "ok", res
    near = [p for p in res.tap.pulses() if abs(p - stim.period_cycles) <= 8]
    assert len(near) >= stim.pulses - 1
    storage_delete_file(host, res.path)
```

`tape_capture_stop` returns a `TapeCaptureResult`: `path`, `size`, `status`,
`declared_length` (the TAP header's length field), `data_length`, the popup
the firmware showed, and the parsed file. Statuses: `ok`, `empty` (no pulses),
`length-mismatch` (the header's length field disagrees with the file),
`bad-header`, `missing`, `unread`. With `read_file=False` the header is not
read, so the still-open check below does not run: `unread` does not show that
the file is closed.

A capture name is 1 to 18 letters, digits, `_` or `-`; the firmware adds
`.tap`. Nothing is typed unless an empty name prompt opened, and a popup that
appears over the prompt stops the call before the next key. The directory must exist (`storage_mkdir`).

### The stimulus

`build_tape_pulse_stimulus(pulses, tap_value=0x30)` returns a small program
that writes `pulses` pulses on the cassette write line (`$01` bit 3), each
period exactly `tap_value * 8` CPU cycles. The recorder times rising edges in
phi2 ticks, so **with the CPU at 1 MHz** it stores `tap_value` for each of the
`pulses - 1` intervals between them, plus one interval before the first edge
and one when it is stopped. At any other CPU speed a CPU cycle is not a phi2
tick and the stored values differ. The routine sets `SEI`, turns sprites off and blanks the
screen so the VIC steals no cycles, drives the line low before the first
pulse, and puts `$D011`, `$D015` and `$01` back (if bit 3 was set on entry,
which is not the KERNAL's default, putting it back is one more rising edge).
It does not touch the motor or sense lines, and it keeps its counters inside
itself: no zero page besides `$01`, nothing in the cassette buffer.

`run_tape_stimulus(target, stim)` writes it with `run_prg_via_sys` (a typed
`SYS`, no `jsr()` trampoline), sleeps for the pulse train's length, and only
then reads the routine's done marker.

A KERNAL `SAVE` to device 1 also produces a recording (the recorder holds the
sense line, so the KERNAL sees the keys as pressed). Its pulses are the
KERNAL's own; decode the capture with `decode_tap_blocks`. If you go that way,
see the cassette buffer section below.

## Playing a tape and proving it

```python
from c64_test_harness import (
    build_tap_from_prg, storage_put_file, tape_load_and_verify, tape_test_program,
)

prg = tape_test_program()                       # a valid BASIC program at $0801
storage_put_file(host, "/USB1/captures/known.tap", build_tap_from_prg(prg))
res = tape_load_and_verify(target.transport, host, "/USB1/captures/known.tap", prg)
assert res.loaded, res.check
```

`tape_load_and_verify` first checks, with a dry run, that the device offers
`Start Tape` for the file, and refuses a negative or non-finite `found_pause`
or `margin` (either would end the wait early). Then it zeroes the load area
and `$AE/$AF`, types `LOAD`, starts the tape, sleeps for the tape's playing time plus an allowance, stops
the tape, and then calls `prg.verify_load`. `loaded` needs the end pointer at
the program's end, every byte equal, and no tape error bit in ST (`$3C`).
For a tape the KERNAL sets the end pointer from the tape header before it
reads the data, so the pointer alone proves less than it does for a disk
load; the zeroed area is what makes the byte compare meaningful.

Use a real BASIC program for a load at `$0801` (`tape_test_program`): a plain
`LOAD` relinks the line pointers afterwards and would rewrite arbitrary bytes.

`tape_play` / `tape_stop` are the two halves if you want to drive the C64
side yourself.

## No memory reads during cassette I/O (#573)

On an Ultimate a REST memory or screen read stops the 6510 while the rest of
the machine runs on (#573). For IEC that is measured. For tape it is reasoned
from the same mechanism and not measured: the KERNAL times tape pulses with a
CIA timer, and the recorder times the write line in phi2 ticks, which run on
while the CPU is stopped, so a stop stretches the pulse being read or written.

So: between typing `LOAD` (or starting the stimulus) and the end of the wait,
do not call `read_memory`, `read_bytes`, `wait_for_text`, `wait_for_memory`,
`watch_progress` or anything else that reads. Wait a computed time, then read.
`tape_load_and_verify` and `run_tape_stimulus` do this. The Remote Menu calls
are telnet and read no C64 memory. These sites carry the `#573` tag and
belong to that issue's retirement list.

## The cassette buffer and `MemoryPolicy`

KERNAL tape I/O keeps its header in `$033C-$03FB`. Harness scratch lives
there too: the liveness probe (`$0334-$03B3`), the U64 `jsr()` trampoline
(`$0360`), the VICE REU park (`$03C0`). During a tape `LOAD` or `SAVE`:

- Do not call `jsr()` / `run_subroutine` on a U64, `liveness_probe`,
  `assert_healthy`, `probe_u64(check_write=True)` or the REU extract.
- Start programs with `run_prg_via_sys` (a typed `SYS`).

`cassette_buffer_reserved(transport)` enforces what it can: inside the block
the transport's `MemoryPolicy` has `$033C-$03FB` reserved, so a
`transport.write_memory` into it raises `MemoryPolicyError` before anything
is sent. The previous policy comes back on exit. `tape_load_and_verify` uses
it. It only adds a reserved region; nothing is allowed that was refused
before, and the tape helpers never pass `override=`. If your policy refuses
the helper's own writes (the cleared load area, `$AE/$AF`), the call stops
before `LOAD` is typed. Declare that area safe or clear it yourself.

It cannot stop a write that bypasses the transport: `liveness_probe` and
`probe_u64(check_write=True)` use the raw REST client.

The pulse stimulus does not use the cassette buffer at all.

## Not covered

- The capture write-error path (item 4 of #584) is not implemented.
- `Sample tape to TAP`, `Run Tape`, `Write to Tape`, Pause/Resume as a pair,
  and `.idx` index entries are not driven.
- Motor and sense handling on the C64 side is left to the caller's program.
