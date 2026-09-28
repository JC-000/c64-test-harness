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
| OS | macOS 26.6.2, arm64 (Apple Silicon), at the time of the reproductions; the bench is now on macOS 27.0 and they have not been re-run there |
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

**Harness mitigation: the shortfall is measured and surfaced.**
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

Do not confuse this with `-ethernetiodriver pcap` on a bare command line,
which **is** rejected at parse time when unelevated (exit 255,
`Argument 'pcap' not valid`): the option sets `ETHERNET_DRIVER`, whose
setter selects pcap only when `archdep_rawnet_capability()` holds (S
`rawnetarch.c:108`) and otherwise returns -1, which S `cmdline.c:262-264`
reports as that error — with or without an `-addconfig` rc. The *cart*
options are accepted and crash.

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
(`src/c64_test_harness/backends/vice_elevation.py:180`) probes
`x64sc -features`, which works, and is what capability decisions are made
from.

---

## 6. Emulation stops while the binary monitor stays responsive

**The one bug in this file whose trigger we have not identified.** It is
recorded because it was characterised precisely and because the next
person to hit it will otherwise spend a day on it, as two investigations
here already did.

> **Evidence record.** This section is a summary. The full investigation
> as it stood at 0e56950 — the #170 capture (pause at `$EA86`, `SP=$F0`,
> the stack bytes and the `$00E6-$00F5` disassembly), the bisect table,
> the redirect counts, the JAMAction-0 source trace, the mode-2 example and
> the RED CHRGET signature — is kept verbatim in
> [issue #170, comment 5873925412](https://github.com/JC-000/c64-test-harness/issues/170#issuecomment-5873925412).

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

The dump was captured under `JAMAction=1`. The harness now pins
`-jamaction 0` (DIALOG) whenever `ViceConfig.monitor` is on
(`ViceProcess.start()` in `backends/vice_lifecycle.py`), because VICE emits the `0x61` JAM event only
under JAMAction 0 with the binary monitor connected (S
`machine.c:131-139`).

**VICE reports that it resumed.** 442 resumes produced 442 `RESUMED`
events, including the ones issued while the raster sat frozen. VICE
acknowledges the `EXIT`, emits the state-transition event, and does not
perform the transition. That — acknowledged resumes with no progress — is
the discriminator; the raster alone is not:

- **A held machine also freezes the raster** (measured 2026-09-03,
  `scripts/vice_raster_hold_probe.py`): eight `read_registers` with no
  resume between them read `LIN=12 CYC=5` eight times, while the same
  machine resumed between reads gave three distinct positions in eight.
  The monitor services commands once per frame, so it always halts at the
  same frame phase.
- **A JAMAction-0 jam pins the raster too** (measured 2026-09-02): ten
  resumes 0.3 s apart all read `PC=$0087 LIN=12 CYC=2`, with the `0x61`
  event queued. The source predicts the opposite (`6510core.c:2388-2394`
  re-fetches the KIL opcode and `maincpu.c:625-626` does `CLK++`, which
  should advance `LIN`/`CYC` via `c64.c:1298-1302`); **why it does not is
  unverified** (the `should_pause_on_exit_mon` path at `monitor.c:3325`
  was ruled out). A jam is told apart from this bug by the queued `0x61`
  and a PC sitting on a KIL opcode, which `_machine_failure_report`
  in `tests/test_vice_core.py` checks for.

**What it is not** — each ruled out by measurement, not by argument:

| hypothesis | eliminated by |
|---|---|
| a slow screen / marginal timeout | the text is normally found in 0–1s against a 15s limit |
| a checkpoint leaked by an interrupted `jsr()` pinning the CPU | `CHECKPOINT_LIST` reports zero |
| a lost resume, or monitor nesting needing more exits | 40 acknowledged resumes, no movement |
| the harness left the CPU halted after a screen match (#184, fixed 2026-09-03: both waiters now resume in a `finally`) | the capture issued 40 explicit acknowledged resumes at the point of measurement |
| the 6510 jammed on an illegal opcode | `$CF00` holds a valid CLI, no `0x61` in 1328 queued events, and under the `JAMAction=1` in force at capture a jam keeps the clock running. Under today's pin, use the `0x61` check above |

**Trigger: unidentified.** It is load-correlated — it does not reproduce
on an idle bench, and this bench routinely runs several `x64sc`
processes from unrelated projects at once. A plausible but **untested**
hypothesis is that VICE's emulation thread can starve under host CPU
contention while its network thread keeps servicing. A CPU-contention
experiment was deliberately not run.

**Reproduction cost:** roughly 8 seconds per attempt. Loop
`pytest tests/test_vice_core.py -k Keyboard` while the machine is loaded;
it stalls within a few dozen attempts. A standalone probe that stalls it
and interrogates the halted machine reproduced it by cycle ~80 in about
half of its runs.

**A second failure mode shares the symptom and is not this bug.** The
keyboard and screen tests can fail in two ways that look identical from
the outside ("the text never appeared"):

| | raster across resumes | PC | screen | diagnosis |
|---|---|---|---|---|
| **stall (this bug)** | constant | pinned, e.g. at `$CF00` | stale | VICE stopped emulating |
| **lost keystrokes** | constant too: `LIN=12`, `CYC` 0-2 (the monitor's frame phase, #504) — the raster separates nothing here | cycling the BASIC idle loop `$E5CD-$E5D4`, or on a KIL opcode with `0x61` queued | `READY.` only, nothing typed | a harness defect, fixed (#170) |

The second mode was the harness's own `_restore_basic` fixture
in `tests/test_vice_core.py`, which returned to BASIC with `CLI; JMP
$E5CD` and SP untouched. `$E5CD` sits inside CHRIN's call frame, and the
monitor pauses the CPU wherever the per-frame poll catches it (S
`monitor.c:407`) — in 2 of 317 redirects measured under load, inside the
KERNAL IRQ handler with the interrupt frame still on the stack. The next
RETURN then popped the wrong frame and RTS'd into zero page, which
corrupted CHRGET and, depending on where it landed, left a cleared screen,
a jam on a KIL opcode, or a self-healing warm start (captured with CPU
history via `scripts/vice_keyecho_probe.py`, disassembled with
`scripts/dis6502.py`). The fixture now re-enters BASIC through the warm
start, `CLI; JMP ($A002)`, which rebuilds SP. Deterministic regression:
`tests/test_vice_core.py::TestRestoreBasicFromInterrupt` parks the
CPU on the handler's RTI at `$EA86` and calls `_restore_basic`. Under the
issue's load recipe: 1 of 45 cycles failed before, 0 of 45 after. When
diagnosing, sample `$C6`/`$0277` immediately after the feed, not at
failure time, and use a fresh VICE per trial — a probe that reuses one
VICE is one trial.

**Harness mitigation: detection, not recovery.** We cannot fix VICE.
`_machine_failure_report` in `tests/test_vice_core.py` classifies the
machine across acknowledged resumes (`_machine_progress`, #504) and names
exactly one cause. The jiffy clock (`$A0-$A2`) or the PC moving means
running. If neither moves, CIA1 Timer A (`$DC04`) decides whether the
machine is clocked at all: frozen means VICE stopped emulating (this
bug); moving means a jammed 6510 (a KIL byte at the PC, or a `0x61`
queued while sampling) or, with no jam, code spinning with IRQs masked.
The raster is reported but decides nothing: sampled through the monitor
it reads `LIN=12` every time with `CYC` 0-2. Measured on VICE 3.10
(2026-09-28, n=8 per arm, warp on and off): Timer A moved in every trial
of BASIC idle, `SEI; JMP *`, a 9-cycle `SEI` loop and a KIL jam alike,
and the classifier returned running / masked spin / masked spin / jammed
8/8 each. With I/O banked out of the CPU's view (`$01=$34`) the `$DC04`
peek reads RAM, so a frozen value there is reported inconclusive (8/8
live); a program that stopped Timer A (`$DC0E` bit 0 clear) still reads
as a stopped emulator, and the label says to check `$DC0E`. The
stopped-emulator verdict rests on the fake plus that measurement; this
bug has not yet been caught live with Timer A sampled.
Deliberately **not** auto-restarted: a harness
that silently rebuilds a stalled emulator converts a reproducible
upstream bug into an invisible one.

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
option '-ethernetiodriver'` and exit 255, because unelevated the
`ETHERNET_DRIVER` setter refuses pcap (S `rawnetarch.c:108`; reported by
S `cmdline.c:262-264`). Verified: that shorter form exits 255, not 139.

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
