# Upstream VICE 3.10 bugs found by this harness

> **Recorded for internal reference only. None of these has been reported
> upstream — there is no VICE issue, mailing-list post, or PR for any of
> them, and none should be assumed to exist.** The purpose of this file is
> that we never re-derive these, and that the next person who trips over
> one finds it already diagnosed.

Every entry below was reproduced on this bench and read back to the VICE
3.10 source. `S` citations are into that source tree.

**Environment for all reproductions**

| | |
|---|---|
| OS | macOS 26.6.2, arm64 (Apple Silicon) |
| VICE | 3.10, Homebrew bottle — `/opt/homebrew/bin/x64sc` |
| VICE | 3.10, local build with `--enable-ethernet` — `~/.local/opt/vice-3.10-ethernet/bin/x64sc` |
| source | `~/Documents/vice-src/3.10/` (see `docs/vice_build_provenance.md`) |

Unless a bug says otherwise, it reproduces identically on **both** builds,
which rules out a packaging fault in the bottle.

---

## 1. Framebuffer response overruns its heap buffer by 4 bytes

**Security weight — this one is a heap overflow, not just a wrong answer.**

`monitor_binary.c:1273` sizes the response buffer:

```c
uint32_t info_length = 13;                              /* :1236 */
buffer_length = screenshot.debug_width * screenshot.debug_height * depth / 8;
response_length = 4 + info_length + buffer_length;      /* :1273 */
response = lib_malloc(response_length);
```

The `4 +` accounts for the `write_uint32(info_length, …)` at `:1278`. But
the code then also writes a **second** 4-byte field — the display-buffer
length at `:1297`:

```c
response_cursor = write_uint32(buffer_length, response_cursor);   /* :1297 */
```

So the bytes actually written are `4 + info_length + 4 + buffer_length`
while only `4 + info_length + buffer_length` were allocated. Every
`DISPLAY_GET` writes 4 bytes past the end of a heap allocation whose size
is derived from attacker-influenceable geometry.

**Observed:** the delivered image is 4 bytes short of the declared
`buffer_length` — measured during this audit as 157,248 declared against
157,244 returned (4 pixels missing at 8bpp). No error, no log line.

**Harness mitigation: the shortfall is now measured and surfaced.**
`BinaryViceTransport.read_framebuffer()` compares `len(pixels)` against
the declared `buffer_length` and returns the difference to the caller:

| key | meaning |
|---|---|
| `bytes` | the pixel bytes that actually arrived |
| `declared_length` | what the response claimed |
| `short_by` | `declared_length - len(bytes)` |

A shortfall of exactly `DISPLAY_GET_SHORTFALL` (4) is this bug: the call
succeeds, `short_by` reports it, and a warning is logged **once per
transport** (not per frame — a capture loop would otherwise bury the
message in copies of itself). Any *other* shortfall raises
`TransportError`, because that is a truncated or desynchronised response
rather than this documented bug.

It deliberately does **not** raise on the known case. The bug fires on
every single call, and `read_framebuffer` is part of the cross-backend
`C64Transport` protocol, so raising would make the VICE backend fail
where the Ultimate 64 backend succeeds — trading a silent wrong answer
for a loud wrong behaviour.

Callers that need exact geometry should size from `debug_rect`/`bpp` and
check `short_by`, rather than trusting `len(bytes)`.

*Verified against a live VICE*, not just asserted: the constant 4 is read
out of VICE's source and typed into ours, so
`tests/test_vice_binary.py::TestUpstreamBugOneIsReal` measures the real
shortfall and fails if this build differs — including if a future VICE
fixes the bug, at which point the workaround should be removed rather
than left to rot.

---

## 2. NULL `rawnet_arch_driver` dereferenced during command-line parsing

`rawnetarch.c:245-251`:

```c
void rawnet_arch_pre_reset(void)
{
    ...
    rawnet_arch_driver->pre_reset();      /* :251 — no NULL check */
}
```

`rawnet_arch_driver` is NULL whenever the driver resolves to `"none"`,
which is the default for a macOS process that is not root:
`set_ethernet_driver()` admits `pcap` only when
`archdep_rawnet_capability()` holds, and that function is `geteuid() == 0`
plus a Linux-only `CAP_NET_RAW` branch.

The sibling function twenty lines below **does** check, which makes this
look like a straightforward omission rather than an invariant:

```c
int rawnet_arch_activate(const char *interface_name)
{
    ...
    if (rawnet_arch_driver == NULL) {     /* :270 — the check that is missing above */
        return -1;
    }
```

**Observed:** SIGSEGV during command-line parsing — before the binary
monitor socket opens, with **zero log output**. Exit code **-11** (139
via a shell). It does not degrade to "ethernet present but no traffic";
it dies. Two routes reach it, both on both builds:

* **The `-ethernetcart` / `-rrnet` / `-tfe` CLI flags**, passed
  unelevated. Measured `rc=139` on **6 of 6** flag × build combinations
  as uid 501 during phase 0 of this audit, and reproduced independently
  by a second agent — same three flags, same two binaries, 6/6 again.
  (Measurements attributed, not re-run here; this bench avoids launching
  those flags unelevated by standing instruction.)
* **An `-addconfig` rc containing `ETHERNETCART_ACTIVE=1`**, launched
  unelevated — the route reproduced directly here, script below.

All three flags are genuinely registered: S `ethernetcart.c:434-451`.
`-tfe` and `-rrnet` are `CALL_FUNCTION` entries whose handlers end in
`resources_set_int("ETHERNETCART_ACTIVE", 1)`, so they activate the cart
exactly as the rc does and arrive at the same dereference.

> A comment in `vice_lifecycle.py` used to claim these flags "appear in
> `-help` but are rejected at parse time (`Option '-ethernetcart' not
> valid`)". That was false, and it is now corrected in place. The
> confusion is worth knowing: `-ethernetiodriver pcap` on a bare command
> line **is** rejected that way (exit 255, `Argument 'pcap' not valid`),
> because its value set is only populated once the cart is active. A
> rejection of the *driver* option was generalised into a rejection of
> the *cart* options.

**Harness mitigation: yes.** `plan_vice_launch()`
(`src/c64_test_harness/backends/vice_elevation.py`) refuses to spawn an
ethernet launch it cannot elevate, raising `ViceElevationRequiredError`
with the exact command and a NOPASSWD line naming that binary.
`VICE_ETHERNET_ALLOW_UNELEVATED=1` opts out.

---

## 3. `ui_error()` called from console mode, where no UI exists

`resources.c:1241-1295`, `check_resource_file_version()`:

```c
if (strcmp(tag, VERSION) != 0) {
    log_warning(LOG_DEFAULT, "Config file version mismatch ...");
    ui_error("WARNING: Configuration file version mismatch ...");   /* :1279 */
    err = 0;
}
...
if (err) {
    log_warning(LOG_DEFAULT, "No version tag found in config file.");
    ui_error("WARNING: No version tag found in configuration file ...");  /* :1291 */
}
```

Under `-console`, `main.c:385` skips `ui_init_with_args()` entirely, so
the GTK3 `ui_error()` touches state that was never constructed.

**Observed:** `x64sc -console` SIGSEGVs — exit **-11** (139) — whenever
the config file's `ConfigVersion` is absent, empty, or does not match the
running VICE. stderr fills with
`Gtk-CRITICAL **: _gtk_style_provider_private_get_settings: assertion 'GTK_IS_STYLE_PROVIDER_PRIVATE (provider)' failed`
before the fault. A windowed launch survives the same file, so this is
specific to console mode.

The practical trigger is mundane: **a vicerc left behind by an older
VICE, or any hand-written one lacking a `[Version]` header.** A vicerc
written by the same VICE version is fine.

Which doors reach the check:

| door | version-checked? |
|---|---|
| user vicerc (`~/.config/vice/vicerc`) | yes |
| portable vicerc beside the binary | yes |
| `-config <file>` | yes |
| `-addconfig <file>` | **no** — `resources_load` only version-checks when its argument is NULL (`resources.c:1376`) |

**Harness mitigation: partial.** `ViceProcess` passes `-default`
(`ViceConfig.load_user_config=False`, the default), which closes the
first two doors — both reach the check via `resources_load(NULL)` at
`main.c:390`, gated on `loadconfig`. It does **not** close
`-config <file>`, which the harness never emits but a caller could add
through `extra_args`. `-addconfig` never reaches the check, so the
ethernet rc path is unaffected.

---

## 4. `strcmp(NULL, VERSION)` on an empty `ConfigVersion` value

Same function, a separate fault reached earlier. `resources.c:1273-1276`:

```c
char *tag = strtok(buf, "=");
if (strcmp(tag, "ConfigVersion") == 0) {
    tag = strtok(NULL, "=");          /* returns NULL when nothing follows '=' */
    if (strcmp(tag, VERSION) != 0) {  /* :1276 — strcmp(NULL, ...) */
```

A line reading exactly `ConfigVersion=` makes `strtok` return NULL, and
the NULL goes straight into `strcmp`.

**Observed:** SIGSEGV, exit **-11**, and — diagnostically useful —
**without** the `Gtk-CRITICAL` preamble that bug 3 always shows, because
the fault happens before `ui_error()` is ever reached. That absence is
how the two were told apart.

This is independent of the UI: it would fault in a windowed build too.
It is listed separately from bug 3 because fixing `ui_error()` would not
fix this.

**Harness mitigation:** the same `-default` as bug 3, and with the same
gap for `-config`.

---

## 5. (Minor) `x64sc --version` is broken

**Observed on both builds:**

```
$ x64sc --version
Error - failed to retrieve executable path, falling back to getcwd() + argv[0]
Error - argv[0] is NULL, giving up.
```

No version is printed and the exit status is unhelpful. Likely the same
init-order cluster that produces the other startup problems here.

**Harness mitigation: yes, incidentally.** Nothing in the harness parses
`--version`. `vice_features()`
(`src/c64_test_harness/backends/vice_elevation.py:172`) probes
`x64sc -features`, which works, and is what capability decisions are made
from.

---

## 6. Emulation stops while the binary monitor stays responsive

**The one bug in this file whose trigger we have not identified.** It is
recorded because it was characterised precisely and because the next
person to hit it will otherwise spend a day on it, as two investigations
here already did.

Under host load, a running VICE stops emulating. Its binary monitor
thread stays completely healthy: it answers `read_registers`,
`read_memory`, `CHECKPOINT_LIST` and `resource_get`, and it acknowledges
every `EXIT`. The machine simply never runs again.

**Measured**, at a reproduced stall:

```
registers : PC=0xcf00 ... '00': 47, '01': 55   (banking normal)
memory at $CF00 : 584ccde5                     (a valid CLI; JMP $E5CD)
checkpoints     : []                           (via CHECKPOINT_LIST)
raster across 5 resumes, 0.2s apart:
    LIN=12 CYC=2   LIN=12 CYC=2   LIN=12 CYC=2   LIN=12 CYC=2   LIN=12 CYC=2
still pinned after 40 further resumes
event queue: 1328 entries over 442 resume generations
    0x31 REGISTER_INFO x443
    0x62 STOPPED       x443
    0x63 RESUMED       x442
    0x61 JAM           x0
JAMAction resource: 1 (continue)
```

> `JAMAction=1` was the harness pin when this was captured. The pin has
> since moved to `0` (DIALOG): VICE emits the `0x61` JAM event only under
> JAMAction 0 with the binary monitor connected (S `machine.c:131-139`),
> so a jam now stops the machine and is reported by `wait_for_stopped`
> instead of continuing silently. The dump above is as captured.

`LIN`/`CYC` are the raster position. They advance whenever the *machine*
is emulating, whether or not the 6510 is executing.

> **Correction, measured 2026-09-03** (`scripts/vice_raster_hold_probe.py`).
> This section used to conclude from the frozen raster that "nothing is
> being emulated at all — this is not the monitor holding the CPU." That
> inference does not hold, and it had never been measured. Eight
> consecutive `read_registers` with **no** resume between them — that is,
> a machine the monitor is simply holding — return `LIN=12 CYC=5` all
> eight times, one distinct position. The same machine resumed between
> reads gives three distinct positions across eight samples. A held CPU
> is therefore indistinguishable from the stall by raster alone, and the
> stall's own `LIN=12 CYC=2` is a value the *running* arm also returns.
>
> This does not refute bug 6. The other rows below still eliminate the
> mundane explanations, and 442 acknowledged resumes with no progress is
> not what a merely-held machine looks like. What it removes is one
> inference: the raster is a weak instrument here, and "frozen raster"
> should be read as "not observed to advance", not as proof the emulator
> stopped. Measured on an idle BASIC screen without host load, so it
> speaks to the inference, not to the stall's own conditions.

**And VICE reports that it resumed.** 442 resumes produced 442 `RESUMED`
events, including the ones issued while the raster sat frozen. VICE
acknowledges the `EXIT`, emits the state-transition event, and does not
perform the transition.

**What it is not** — each ruled out by measurement, not by argument:

| hypothesis | eliminated by |
|---|---|
| a slow screen / marginal timeout | the text is normally found in 0–1s against a 15s limit |
| a checkpoint leaked by an interrupted `jsr()` pinning the CPU | `CHECKPOINT_LIST` reports zero |
| a lost resume, or monitor nesting needing more exits | 40 acknowledged resumes, no movement |
| the harness left the CPU halted after a screen match (the issue #184 defect: `wait_for_text` returned a match from inside its `try` without resuming) | the capture issued 40 explicit acknowledged resumes at the point of measurement, so the machine was not merely un-resumed. Worth listing because it produces the *same* frozen-raster signature — see the correction above — so it is ruled out by the resume count, not by the raster. Fixed 2026-09-03; both waiters now resume in a `finally`. |
| the 6510 jammed on an illegal opcode | `$CF00` holds `584ccde5`, a valid CLI; and **no `0x61` JAM event** in 1328 queued events; and `JAMAction=1` (continue) would have kept the raster advancing anyway (true at capture time; the harness pin is now `0`, under which a jam stops the machine and surfaces as a `TransportError` from `wait_for_stopped` — so a stall captured today would be distinguished from a jam by that error, not by this row — and **not by the raster either**: measured 2026-09-02, a JAMAction-0 jam pins `LIN`/`CYC` exactly like this stall (`LIN=12 CYC=2` across ten resumes); the poll loop never calls `wait_for_stopped`, so the discriminator is the queued `0x61` event, which `_machine_failure_report` now scans for — see "Mode 2 resolved" below) |

**A CPU-contention experiment was deliberately not run**, and that is a
choice rather than an omission: with two failure modes sharing one
surface symptom, characterising both is worth more than testing a
trigger hypothesis for one of them. The hypothesis below stands
untested.

**Trigger: unidentified.** It is load-correlated — it does not reproduce
on an idle bench, and this bench routinely runs several `x64sc`
processes from unrelated projects at once. A plausible but **untested**
hypothesis is that VICE's emulation thread can starve under host CPU
contention while its network thread keeps servicing. That is a guess and
is recorded as one.

**Reproduction cost:** roughly 8 seconds per attempt. Loop
`pytest tests/test_vice_core.py -k Keyboard` while the machine is loaded;
it stalls within a few dozen attempts. A standalone probe that stalls it
and interrogates the halted machine reproduced it by cycle ~80 in about
half of its runs.

**Scope — this is one of two failure modes, not the only one.** The
keyboard and screen tests fail intermittently in two different ways that
look identical from the outside ("the text never appeared"), and the
raster check is what separates them:

| | raster | PC | screen | diagnosis |
|---|---|---|---|---|
| **stall (this bug)** | frozen | pinned, e.g. at `$CF00` | stale | VICE stopped emulating |
| **lost keystrokes** | advancing | cycling the BASIC idle loop `$E5CD-$E5D4` | `READY.` only, nothing typed | machine healthy; the injected keys never took effect |

Measured example of the second, which is **not** this bug: 15 poll
windows, 4 distinct PCs all inside the idle loop, stub intact, zero
checkpoints, and a screen carrying nothing but `READY.` — the machine ran
happily for fifteen seconds and never received the text
`send_text` had written to its keyboard buffer.

That second mode **is now root-caused, and it is a harness bug, not a
VICE one** — see "Mode 2 resolved" at the end of this entry. An earlier
revision of this note claimed a different root cause on a single capture;
that claim is withdrawn below and the history is kept because the false
trail is instructive. The mode is recorded here only because it shares a
surface symptom with the stall.

Bisected by dropping whole classes, under load, fresh VICE per run:

| selection | reproduced |
|---|---|
| whole module | 2/25 |
| `TestScreen or TestKeyboard` | 3/50 |
| `TestKeyboard` alone | **0/50** |

So `TestScreen` running first is the trigger, and the victim is always
`TestKeyboard::test_send_text_basic_command`, the first keyboard test.

Replaying that order with the keyboard buffer sampled between each step
gives the mechanism:

```
after send_text('PRINT 2+3')     $C6=10 $0277=5052494e5420322b330d
after wait_for('5') found=False  $C6= 0 $0277=00000000000000000000

screen:
   6| PRINT"HELLO VICE"
   7| H_..V.. 2+3
   9| READY.
```

`5052494e5420322b330d` is `PRINT 2+3\r` in PETSCII. **The keys arrive and
are consumed.** Line 7 is the failure: BASIC's `HELLO VICE` *output*
interleaved with the echo of `PRINT 2+3`, on the same line, producing
garbage that BASIC then fails to parse.

The cause is a **false-completion signal** of exactly the kind this repo
documents (c33b5c4, issue #138), in
`TestScreen::test_wait_for_text_after_print`. It types
`PRINT"HELLO VICE"` and waits for `HELLO VICE` — but the *echoed command
line* contains that substring the moment it is typed, so the wait returns
long before BASIC executes anything. The test passes on the echo,
`_restore_basic` jumps to MAINLOOP mid-execution, and BASIC's real output
lands on top of the next test's input.

**Two of my own earlier claims about this were wrong**, and both are
corrected above:

* "The keys never reached the C64." They do. The failure report samples
  fifteen seconds later, *after* BASIC has consumed the buffer, so
  `$C6=0` with an empty `$0277` at failure time says nothing about
  whether the keys arrived. Sampling immediately after the feed shows
  `$C6=10`.
* "199 cycles of the exact sequence never lost a keystroke." That probe
  reused **one** VICE across all cycles, so only the first was
  boot-faithful. With a fresh VICE per cycle — which is what pytest does
  — it reproduces at cycle 12 of 45.

**The fix based on this diagnosis did not work, and the diagnosis was
therefore incomplete.** Both layers were applied — every needle changed
so no echo can satisfy it, and `_restore_basic` extended to drop queued
keystrokes and wait for the screen to quiesce — and the reproduction rate
did not move: cycles 17, 29, 11 and 15 of 45, against 12 of 45 before.
The residual failures classify as **mode 2 by the raster check**, so they
are not the stall.

Later captures show states the collision hypothesis does not explain:

* `PRINT 2+3` echoed on screen but never executed, PC at `$FF09`
* PC at **`$0002`** — the 6510 executing zero page, with `$C6=3` while
  `$0277` reads all zeros, a count and a buffer that disagree
* a screen blank but for `READY.`

The line-collision capture that produced the original diagnosis was one
manifestation among several, and generalising from it was the error. What
is common to all of them is that the machine is *running* and BASIC does
not execute the typed line; what varies is where the CPU ends up. That is
not a false-completion problem, and no test-side wait can fix it.

The oracle and isolation changes are kept because they remove a genuine
defect of the documented false-completion class — three of the four
content-asserting tests in `test_vice_core.py` waited on a needle their
own echoed command contained — but they are **not** a fix for this
failure and are not described as one.

**Mode 2 resolved (issue #170): `_restore_basic` inherited the stack of
whatever the monitor pause interrupted.** The restore was `CLI; JMP
$E5CD` with SP untouched. `$E5CD` is *inside* CHRIN's call frame (`$E632`
pushes X and Y and falls into the idle loop; RETURN makes `$E676`
`PLA;TAX;PLA;TAY;…;RTS` back to INLIN), so it is only correct when SP
already points at that frame. The binary monitor pauses the CPU wherever
the per-frame poll catches it (`monitor_check_binary()` from
`monitor_vsync_hook()`, S `monitor.c:407`), and in 2 of 317 redirects
measured under load that was inside the KERNAL IRQ handler with the
interrupt frame still on the stack. Captured with CPU history
(`scripts/vice_keyecho_probe.py`, cycle 23 of 45):

```
redirect PC:=$CF00  paused at PC=$EA86 (the handler's RTI) SP=$F0
                    stack = 22 d4 e5 | 00 0a 14 e1 64 a5 ...
                            P  PCL PCH  X  Y  ret   ret
```

The fixture's own RETURN then popped `X:=$22, Y:=$D4` and the RTS landed
at `$00E5+1 = $00E6`, the screen line-link table. Disassembled from the
captured zero page with `scripts/dis6502.py` (illegal opcodes included —
the first version of that tool had none and desynchronised here):

```
00E6  86 86     STX $86        ; X = popped P = $22
00E8  86 86     STX $86
00EA  86 86     STX $86
00EC  86 87     STX $87
00EE  87 87     SAX $87        ; A & X = $3A & $22 = $22  (A = the ':' just returned)
00F0  87 87     SAX $87
00F2  87 00     SAX $00        ; $F3 = colour-ptr lo, $00 with the cursor on line 0:
                               ; hits the 6510 DDR, rewritten by IOINIT in the warm start
00F4  d8        CLD
00F5  00        BRK            ; $F5/$F6 KEYTAB = $0000: VICE never scanned a matrix key
                               ; (with KEYTAB = $EB81 it is STA ($EB,X) and BRK at $F7)
```

CHRGET's `SBC #$30; SEC` at `$0086-$0087` became `22 22`; BRK → `($0316)
= $FE66` → IOINIT, `CINT` cleared the screen, and the resulting `READY.`
satisfied the fixture's wait. The test's first typed line then hit opcode
`$22` (KIL) in CHRGET and the 6510 jammed: one `0x61` event queued, PC
`$0087`. The `$F3` value at RTS time is derived (the dump was taken after
the warm start), the rest is the captured bytes.

**A JAMAction-0 jam pins the raster — measured, against the source's
prediction.** Reproduced on 2026-09-02 with the deterministic test driven
to the jam: `_emulator_is_stalled()` sampled `LIN=12 CYC=2` four times
across four resumes, and six further resumes 0.3 s apart all read
`PC=$0087 LIN=12 CYC=2 SP=$F6`, with the `0x61` event queued and CPU
history ending at `0087 op=22`. The source reads the other way:
`6510core.c:2481-2484` sets `CPU_IS_JAMMED`, `REWIND_FETCH_OPCODE(CLK)`
(a no-op in x64sc, `c64cpusc.c:42`) and calls `JAM()`; on the next pass
`6510core.c:2388-2394` re-fetches the jam opcode (`SET_OPCODE(lastop)`),
`machine_jam` returns `JAM_NONE` because `is_jammed` (`machine.c:112-114`),
and `maincpu.c:625-626` does `CLK++` — so a re-executing jam loop *should*
advance `maincpu_clk` and with it `LIN`/`CYC` (`c64.c:1298-1302`). It does
not, so after the first `CMD_EXIT` the core is evidently not iterating;
**why is UNVERIFIED** (the `should_pause_on_exit_mon` path at
`monitor.c:3325` arms only when the UI is already paused, and was ruled
out). Consequence for diagnosis: a frozen raster does **not** separate a
jam from bug 6. The queued `0x61` and a PC sitting on a KIL opcode do,
and `_machine_failure_report` now checks for them.

Which of the capture shapes listed above this explains: "screen blank but
for `READY.`" and "PC in zero page" follow from the mechanism — the RTS
lands at `((X<<8)|Y)+1` of the interrupted code, or at `(P<<8|A)+1` one
frame deeper (`$2201` = `00`, BRK, a clean warm start: the fixture
*self-heals* and the cycle passes, also measured, 1 of 317). The
line-collision capture is the false-completion wait already fixed above.
`$FF09` and the `$C6=3` / empty-`$0277` disagreement are *consistent
with* a bogus RTS but were not re-derived. Reconciling the old evidence
row "no `0x61` in 1328 events, raster advancing": it was captured under
`JAMAction=1` (CONTINUE), where `machine_jam` returns `JAM_NONE` with no
`0x61` and the CPU spins at the KIL with the clock advancing — so that
row never excluded this mechanism.

Fix: the restore now goes through BASIC's warm-start vector — `CLI; JMP
($A002)` → `$E37B` → `JSR $A67A` (`LDX #$19; STX $16; PLA; TAY; PLA; LDX
#$FA; TXS; PHA; TYA; PHA; LDA #0; STA $3E; STA $10; RTS`: pops its own
return, resets SP, pushes it back, resets the temp-string stack, disables
CONT — no NEW/CLR, a loaded program survives) rebuilds the stack.
Deterministic reproduction, not a rate: `tests/test_vice_core.py::
TestRestoreBasicFromInterrupt` parks the CPU on `$EA86` with a checkpoint
and calls `_restore_basic`; RED = CHRGET `… e9 30 38 …` → `… e9 22 22 …`
in 3.6 s, GREEN after the fix. Under the issue's load recipe: 1 of 45
cycles before, 0 of 45 after -- with 3 of 315 redirects landing inside
the IRQ handler, each of which the rebuilt stack survived.

**Harness mitigation: detection, not recovery.** We cannot fix VICE. The
raster check is the discriminating signal — it distinguishes a stalled
emulator from every other failure and, unlike a PC sample, cannot
coincide by accident. `tests/test_vice_core.py` reports it, so a stall
says so instead of timing out on a screen assertion.

Deliberately **not** auto-restarted. A harness that silently rebuilds a
stalled emulator converts a reproducible upstream bug into an invisible
one.

---

## 7. Binary-monitor JAM response (`0x61`) documents a PC body it never sends

The manual documents the JAM event with a body. `doc/vice.texi`,
§ "JAM Response (0x61)" (`@subsection JAM Response (0x61)`, :23362):

```
Response body:
    PC PC
PC: 2 bytes: The current program counter position
```

The implementation computes that body and then transmits zero bytes of
it. `monitor_binary.c:382-392`, `monitor_binary_ui_jam_dialog()`:

```c
unsigned char response[2];
uint16_t addr = ... mon_register_get_val(e_comp_space, e_PC);
write_uint16(addr, response);                                   /* :387 — body filled */
monitor_binary_response(0, e_MON_RESPONSE_JAM, e_MON_ERR_OK,    /* :389 — length 0 */
                        MON_EVENT_ID, response);
```

The first argument of `monitor_binary_response()` is `length`
(`:339-355`): it is written into the header at `:345` and is the byte
count handed to `monitor_binary_transmit(body, length)` at `:353`. The
two sibling events that carry the same body pass `2` —
`monitor_binary_response_stopped()` at `:369` and
`monitor_binary_response_resumed()` at `:379`. So a STOPPED or RESUMED
frame carries the PC and a JAM frame carries a header announcing length
0 followed by nothing: documented body, computed body, never sent.

**Observed:** read off the source, on both the bottle and the local build
(same `monitor_binary.c`); the wire frame was not captured on this bench.
A client that trusts the manual and reads two body bytes after the JAM
header will block on (or misattribute) the next frame's bytes. A client
that trusts the header sees an empty body and has no PC.

The event is only emitted under `JAMAction=0` with the binary monitor
connected (S `machine.c:131-139`), which is why the harness pins that
value — see the note under bug 6.

**Harness mitigation: yes.** `BinaryViceTransport._jam_message(frame)`
(`src/c64_test_harness/backends/vice_binary.py`, commit `6579585`) parses
the PC from the body when the frame carries two or more bytes — so a
build that fixes this is used as documented — and otherwise falls back to
a `REGISTERS_GET` read. The fallback is issued after the transport lock is
released, because it goes through `_send_and_recv`, which takes that
same non-reentrant lock; raising from inside the receive loop deadlocked
the transport. Not reported upstream, like the others.

---

## Reproducer for bugs 3 and 4

Self-contained; takes about 40 seconds. Pass the `x64sc` to test.

```sh
#!/bin/sh
# VICE 3.10: x64sc -console SIGSEGVs when the config file fails its version
# check, because check_resource_file_version() calls ui_error() and console
# mode never initialised the UI.   Usage: sh repro.sh /path/to/x64sc
set -u
X64SC="${1:-x64sc}"
run() {
    desc="$1"; rc_body="$2"
    H=$(mktemp -d); mkdir -p "$H/.config/vice"
    [ -n "$rc_body" ] && printf '%b' "$rc_body" > "$H/.config/vice/vicerc"
    HOME="$H" "$X64SC" -console -warp +sound \
        -binarymonitor -binarymonitoraddress ip4://127.0.0.1:6599 \
        >/dev/null 2>"$H/err" &
    pid=$!; sleep 6
    if kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
        printf '%-40s OK (running)\n' "$desc"
    else
        wait "$pid" 2>/dev/null; printf '%-40s EXIT %s\n' "$desc" "$?"
        sed -n '1,2p' "$H/err" | sed 's/^/      /'
    fi
    rm -rf "$H"
}
run "no vicerc"                      ""
run "vicerc, correct ConfigVersion"  '[Version]\nConfigVersion=3.10\n\n[C64SC]\nSpeed=50\n'
run "vicerc, NO [Version] section"   '[C64SC]\nSpeed=50\n'
run "vicerc, ConfigVersion=3.9"      '[Version]\nConfigVersion=3.9\n\n[C64SC]\nSpeed=50\n'
run "vicerc, ConfigVersion= (empty)" '[Version]\nConfigVersion=\n\n[C64SC]\nSpeed=50\n'
run "vicerc, empty file"             '\n'
```

Expected on both builds — rows 1 and 2 survive, rows 3 to 6 die:

```
no vicerc                                OK (running)
vicerc, correct ConfigVersion            OK (running)
vicerc, NO [Version] section             EXIT 139
vicerc, ConfigVersion=3.9                EXIT 139
vicerc, ConfigVersion= (empty)           EXIT 139
vicerc, empty file                       EXIT 139
```

Row 5 is bug 4; rows 3, 4 and 6 are bug 3. Only rows 3, 4 and 6 print the
`Gtk-CRITICAL` line.

The script writes its vicerc into a throwaway `HOME`, so it never touches
`~/.config/vice/`.

## Reproducer for bug 2

The cart must be activated through an `-addconfig` rc. Passing
`-ethernetiodriver pcap` on a bare command line does **not** reach the
bug — it is rejected at parse time with `Argument 'pcap' not valid for
option '-ethernetiodriver'` and exit 255, because the driver's value set
is populated by `rawnet_arch_init()`, which only runs once the cart is
active. Verified: that shorter form exits 255, not 139.

```sh
#!/bin/sh
# VICE 3.10: activating the ethernet cart UNELEVATED dereferences a NULL
# rawnet_arch_driver in rawnet_arch_pre_reset() (S rawnetarch.c:251).
# Usage: sh repro-rawnet.sh /path/to/x64sc
set -u
X64SC="${1:-x64sc}"
RC=$(mktemp /tmp/vice_eth_XXXXXX.rc)
printf '[Version]\nConfigVersion=3.10\n\n[C64SC]\nETHERNETCART_ACTIVE=1\nEthernetCartMode=1\nSaveResourcesOnExit=0\n' > "$RC"
"$X64SC" -console -default -addconfig "$RC" \
    -ethernetioif feth0 -ethernetiodriver pcap \
    -binarymonitor -binarymonitoraddress ip4://127.0.0.1:6599 \
    +sound >/dev/null 2>&1 &
pid=$!; sleep 6
if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
    echo "unelevated: OK (running) -- bug not reproduced"
else
    wait "$pid" 2>/dev/null; echo "unelevated: EXIT $?"
fi
rm -f "$RC"
```

Prints `unelevated: EXIT 139` on both builds. Run the same launch under
`sudo -n` and it starts normally and attaches two BPF devices — see
`docs/bridge_networking.md` § "Issue #144 is refuted".

## Reproducer for bug 1

Any `DISPLAY_GET` over the binary monitor; compare the declared
`buffer_length` field against the number of pixel bytes that follow. See
`BinaryViceTransport.read_framebuffer()`.

---

## Related

- `docs/bridge_networking.md` — issue #144 (a *harness* bug, not a VICE
  one: the BPF-attach probe measured its own permission failure), the
  elevation gate, and the macOS test-author traps.
- `docs/vice_build_provenance.md` — how the two builds here were produced.

An unreproduced observation from the binary-monitor mapping is worth
re-reading against bug 6: after a client dropped its socket *while a
checkpoint had halted the monitor*, VICE kept `LISTEN`ing but never
served a fresh connect. It was guessed at the time to be
checkpoint-plus-halt. Bug 6 establishes that a different state exists and
is reachable — **emulation stopped while the monitor thread stays alive
and responsive** — which fits that observation without needing a
checkpoint to be involved at all. Neither has been tied to the other; the
point is only that the state is no longer hypothetical.

VICE's own case-sensitivity split is worth knowing but is **not** a bug —
it is documented behaviour that reads as one. Resource-table lookup is
case-insensitive (`util_strcasecmp`, `resources.c:243`), while the
command-line option table is case-sensitive *and* prefix-matching
(`cmdline.c:172-196`). An unambiguous prefix silently binds to a longer
option: `-eventsnapshot 1` sets `EventSnapshotDir="1"` and VICE starts
normally. Several harness flags were wrong for years because of it.
