# Ultimate 64 recovery primitives

Ultimate firmware without the #686 Temp-folder cleanup (Ultimate-line 3.14d, C64U 1.1.0) has several distinct wedge modes, each
with its own observable shape and its own recovery path. The harness
exposes a probe primitive and a recovery primitive for each, plus a hard
guard around `poweroff()` (which is irrecoverable over the network).

The most common failure mode is **mis-diagnosing the layer**: calling
`recover()` for a UCI-side wedge does nothing useful, because `recover()`
declares success the moment REST responds — and the FPGA's UCI command
processor lives below REST. Consumers that escalate through the wrong
primitive end up at `reboot()`, see `reachable=True`, and then watch the
next test wedge identically.

The harness recognises three independent layers, listed in escalation
order. Each layer has its own probe + recovery primitive.

## Status: root cause and upstream fix

This whole wedge family (issues #112, #129, #137) was traced to firmware
Temp-folder accumulation after the tiers below were first characterised,
and the mitigations here are **temporary**. `POST /v1/machine:writemem`
uploads arrive as multipart attachments that land in Temp — visible in the
firmware route table itself, `software/api/route_machine.cc` at tag `1.1.0`:
`API_CALL(POST, machine, writemem, &attachment_writer, ...)`, where all
eleven other `machine:*` registrations pass `NULL`. So POST `writemem` is
the only attachment-carrying route in that family, from firmware source
rather than from observed behaviour. (Two limits on what this citation
buys: it covers the `machine:*` family only — the `runners:*` routes and
the multipart `mount_disk`/`drives:load_rom` bodies are registered
elsewhere and are not evidenced by it — and it establishes that
attachments are *created*, not that accumulation crashes the firmware,
which remains unestablished as stated below.) and without
garbage collection the accumulation produces the latency drift and
eventual wedge described in every tier below.

Two cautions about how far that goes, both expanded under "The hygiene
pass is prevention, not recovery" below. The accumulation does not fill
`/Temp` — the wedge arrives at under 6% of a **16 MiB** RAM disk — it
**crashes the device firmware**: the C64 FPGA keeps running while the firmware
stops answering the network and stops responding to the physical menu
button. And "root cause" overstates it: upstream #686 removes the
accumulation and thereby the wedge, but the mechanism connecting the two
is not established, and this document names no cause.

The fix is upstream in
[GideonZ/1541ultimate#686 "Add automatic cleanup of Temp folder"](https://github.com/GideonZ/1541ultimate/pull/686)
(merged 2026-04-26). **That merge is an ancestor of the `v3.15` tag**, so
every Ultimate-line 3.15 build carries it. The bench U64E runs
`v3.15-85` and is fixed: measured 2026-09-02 with the harness GC off
(`U64_AUTO_TEMP_GC` unset), `/Temp` held zero managed attachments before
and after fifteen `run_prg` uploads. `u64_capabilities` encodes the same
fact (`writemem_post_safe=True` for Ultimate-line ≥ 3.15, POST cutoff 48
bytes). **The C64 Ultimate on 1.1.0 is not fixed** (`_CBM_WRITEMEM_FIXED_FROM`
is `None`); everything below applies as written only on that generation,
and on any Ultimate-line device still on 3.14.

Two practical consequences on unfixed firmware:

- Prefer `PUT /v1/machine:writemem?data=<hex>` over `POST` for heavy
  write loads. POST is the leaking path; PUT does not accumulate Temp
  files.
- Once both devices run fixed firmware, the tier-1 mitigations and the
  tier-3 `uci_wedge_probe` become diagnostic history rather than
  operating procedure. Re-validate against the new firmware before
  deleting anything — the deterministic repro in issue #112 is the
  intended verification.

### Harness-side mitigation: FTP `/Temp` GC (issue #153)

On unfixed firmware, `run_prg` (and any other endpoint that carries a
body — `writemem` above the device's threshold, `load_prg`, `run_crt`,
`sidplay`, `modplay`, the multipart `mount_disk`/`load_rom` bodies) leaks a managed attachment
(`temp0000`, `temp0001`, ...) per call. Keyboard injection is **not** on
that list, despite an earlier revision of this line saying so:
`send_text` writes at most `KEYBUF_MAX = 10` bytes
(`ultimate64_client.py:862-866`), which is under either threshold and so
always takes the bodyless `PUT ?data=` form. Ultimate-line 3.15 collects them
on-device (#686); the C64U on 1.1.0 does not. This is shared 1541ultimate firmware behaviour, not
specific to either device generation. `ultimate64_temp_gc.gc_temp_folder(host, ...)`
deletes those files over FTP, oldest-first, keeping the youngest N
(default 2) — mirroring the policy 1541ultimate#686 applies on-device on
Ultimate-line ≥ 3.15. It is best-effort: any FTP/network failure is captured
in the returned `TempGCResult.error` rather than raised, so a hygiene
pass can never fail a test run.

Verified live on both device generations: originally on the U64E, and
on the C64U (10.53.21.158, firmware 1.1.0) on 2026-08-21 —
`tests/test_temp_gc_live.py` passed end-to-end (repeated `run_prg`
leaks `temp####` attachments, `gc_temp_folder` trims them to the
keep-count and is idempotent on a re-run) using the same anonymous-FTP,
`/Temp`-path defaults as the U64E; no generation-specific credentials or
path were needed.

#### The hygiene pass is prevention, not recovery

Everything below runs on a **healthy** device to keep it healthy. This is
not a conservative assumption but a consequence of the failure mode: what
wedges is the device firmware itself, and **the FTP server is part of
that firmware**. So the GC is unavailable exactly when a device is
wedged — there is no cleaning up afterwards, and a physical power-cycle
is the only instrument left. Never reach for `gc_temp_folder` as a
recovery step.

The corollary is what justifies the refusal below: a *failed* hygiene
pass is more serious than it looks, because there is no second chance
later. Once hygiene is unavailable on a leak-prone device, declining to
upload is not a cautious default — it is the only lever still attached.

#### The hygiene pass is integral, not an env flag

`U64_AUTO_TEMP_GC` used to be the only thing that armed the pass, and it
armed exactly one call site (`run_prg`). Both halves of that were wrong
for the device that needs it: the flag was almost never set, and
`load_prg` / `run_crt` / `sidplay` / POST `writemem` / `drives:mount`
leaked just the same. `Ultimate64Client` now decides for itself.

**Arming** — in order: the `temp_hygiene=` constructor argument; then
`U64_AUTO_TEMP_GC` (truthy forces the pass on for *any* device, falsy
forces it off — the documented escape hatch); then the device's firmware,
via `DeviceCapabilities.runner_wedge_possible` — the inverse of
`writemem_post_safe`, and the honest name for the question being asked
("can this device wedge under write load"). `False` (the upstream
collector is present: Ultimate-line ≥ 3.15) disarms; `True` or `None`
arms, because unknown firmware resolves conservatively. Note what this is
*not* keyed on: the device's address. Consumer lanes reach the C64U
through `$U64_HOST`/`--host` and it moves between addresses, so an IP
allowlist would miss the real path; capabilities come from `GET /v1/info`
and the protection follows the device. One exception keeps the unit suite off
the network and is worth knowing: a client whose capability probe never
got an answer (`firmware_version is None`) stays disarmed — there is no
device on the far end, so there is no `/Temp` to collect. A client
constructed with an explicit `write_mem_query_threshold` never probes at
all and is in that state too; arm it with `temp_hygiene=True`.

**Which calls count.** The firmware's route table settles it: a route
either binds `&attachment_writer`, which streams the request body into a
managed `/Temp` file, or binds `NULL`, which ditches the body. In
`software/api/route_*.cc` **every POST route** binds a writer
(`configs`, `drives:mount`, `drives:load_rom`, `machine:writemem`,
`runners:{run_prg,load_prg,run_crt,sidplay}`) and **every PUT route**
binds `NULL`. So the harness counts a leak for exactly *body + POST*,
checked in `Ultimate64Client._request` — one choke point, so anything
added later is covered by construction. `PUT machine:writemem?data=<hex>`
and the config PUTs are free, which is also why the pass can enable FTP
File Service without leaking an attachment to do it. Two deliberate
conservatisms: `runners:modplay` (`&attachment_reu` — the body goes to
the REU) and `machine:input` (`&input_json_writer`) are counted anyway;
and the route table read is a 3.15-line checkout, so the C64U's 1.1.0
verb/handler pairing is assumed identical.

Measured live, and the boundary is exact (C64U 10.53.21.158, fw 1.1.0,
`writemem_post_safe=False`, threshold 128, 2026-09-10, under the
`DeviceLock`, n=1 per arm, every write read back and byte-compared):
`write_mem` at 64 B and at **exactly 128 B** takes the PUT path and
creates **zero** managed attachments; at **129 B** it takes the POST path
and creates **exactly one** (`temp0000`). `gc_temp_folder(keep=0)` then
deleted it, `ok=True`, `/Temp` back to zero. So the ceiling is inclusive,
a POST costs exactly one attachment, and the FTP pass works end-to-end on
the C64U. (FTP File Service read `current=Enabled` / `default=Disabled`
on that device, so the enable-then-refuse path was not exercised there —
it remains required for the general case.)

The path that makes this urgent has no `run_prg` in it at all:
`Ultimate64Transport.write_memory` does not chunk, so a payload above the
threshold goes straight to the body-POST path. That path is also the one
with an open correctness question: `write_mem`'s docstring says the POST
form "has no upper bound verified at 2048 bytes"
(`Ultimate64Client.write_mem`'s docstring, `ultimate64_client.py:1371`),
and a 47,103-byte
single-call write on the U64E (fw `v3.15-78-g71480a9d`, n=2, under the
`DeviceLock`) came back with **exactly one wrong byte, at a different
offset each time** (2715, then 3181 — both past 2048), while the same
bytes through `write_bytes`'s 84-byte chunks were byte-exact 2/2. Not
truncation and not a fixed boundary; unexplained. So chunking is the
better path for two independent reasons, not one —
[#231](https://github.com/JC-000/c64-test-harness/issues/231). Callers that route through
`write_bytes` (84-byte chunks) stay on the PUT path and never leak; so
does the SocketDMA fast path — but **SocketDMA writes are disabled
pending a stability review**, so it is not a route off the POST path that
anyone may take today. `socket_dma` defaults to `False`
(`backends/ultimate64.py:79`); leave it there. Note also that a SocketDMA
write which falls back to REST *does* leak, and the fallback latches:
`_socket_dma_unusable` is never cleared, not even by `close()`
(`backends/ultimate64.py:106`, `:299`), so one connect failure routes every
later write on that transport to REST for its lifetime after a single
WARNING.

**On a C64U, a code write is normally a POST.** The 128-byte ceiling is
below most of the harness's own generated blobs — measured host-side by
`len()` (no device traffic, 2026-09-10): `build_uci_command` 133,
`build_get_ip` 138, `build_socket_read` 149, `build_tcp_connect` /
`build_udp_connect` 159, `build_socket_write` 170 (payload-independent —
the data lives separately at `data_addr`), `build_rx_echo_reply_code` 193,
`build_ping_and_wait_code` 256 (362 with ARP + drain),
`build_rx_echo_reply_tod_code` 317, `build_ping_and_wait_tod_code` 486,
`build_icmp_responder_code` 630, `build_icmp_responder_tod_code` 754.
`build_uci_probe` / `build_uci_status_peek` (12) and `build_socket_close`
(112) fit under the ceiling — but `turbo_safe=True` roughly triples every
one of these, which pushes even `build_socket_close` to 341.

So a UCI socket write (`uci_network.py:1936-1943`) costs **one**
attachment for its always-POST 170-byte routine code, plus a **second
only when the payload itself exceeds the ceiling** — the 800/892-byte
large-send tests do, a small write stays on PUT. Its `socket_id` (1 byte)
and `data_len` (2 bytes) writes are PUTs and cost nothing, and
`enable_uci` / `disable_uci` are `set_config_items` — bodyless, zero
attachments. Each RR-Net ping/responder load costs one. The budget counts
*attachments* rather than logical operations, which is the right unit
precisely because nobody has to maintain that table: every one of those
distinctions falls out of the same choke point.

**Where the protocol is driven decides the exposure.** UCI driven from
host Python costs an attachment per `_execute_uci_routine` code write
*when the emitted routine exceeds the threshold* — which the builders
mostly do (133-170 B), though probe, peek and `socket_close` do not — so an
operation made of many `socket_read`s is many attachments; the same
protocol driven C64-side from inside an uploaded PRG costs only the one
upload. That is leak *elimination*, not hygiene, and it is the first
thing to reach for — the GC is what covers the traffic you cannot move.

These arrive via `execute.load_code()`, which is a bare alias for
`transport.write_memory` and does **not** chunk despite the name
(`execute.py:104-110`); `_execute_uci_routine` writes directly the same way
(`uci_network.py:1735`). That is the third independent confirmation that
hooking the *request* is the only workable choke point: a budget keyed on
runner verb names sees none of this traffic.

Making `load_code` chunk through the same 84-byte PUT path `write_bytes`
uses would eliminate this class of leak rather than clean up after it,
which is strictly better where it is available — but it is a separate
change, not a docs note: it converts one POST into up to nine round trips
for a 754-byte blob, on paths with live timing constraints (the ip65
"≥ 0.2 s after `ip65_init`" rule, the SocketDMA barrier). It needs its own
red/green and its own live verification. Filed as
[#252](https://github.com/JC-000/c64-test-harness/issues/252).

**Cadence.** A per-client budget of
`ultimate64_temp_gc.DEFAULT_LEAK_BUDGET` = **6** attachment-creating
calls, then the pass runs before the call that would overrun it, and a
successful pass resets the count.

Why 6. One measurement and one upstream precedent bound it from above —
not two enforced limits. The precedent is the firm one: the firmware's
own post-#686 collector keeps at most **10** managed files
(`kManagedTempMaxFiles`), upstream's own statement of a safe resident
count for this folder on this device family, needing no conditions. The
measurement is far weaker than it is usually quoted as being: one U64E on
3.14d wedged at ~15 uploads of a 63 KB PRG, n unrecorded, for reasons
never established — and it is not a capacity measurement at all. The RAM
disk is **16 MiB**: `ramdisk.cc:25` computes its size as
`__ram_disk_limit - __ram_disk_start`, and at tag `1.1.0` both
`target/u64/riscv/ultimate/linker.x` and
`target/u64ii/riscv/ultimate/linker.x` set those symbols to `0x02000000`
and `0x03000000` — a 16 MiB span. (The `// 3 * 1024 * 1024` on that same
line is a stale trailing comment, not the value; a "~3 MB RAM disk"
propagated through five documents on the strength of it. Corrected in
[#261](https://github.com/JC-000/c64-test-harness/issues/261).) So 15
uploads of a 63 KB PRG is 967,680 bytes, or **~5.8%** of the disk, with
15 directory entries used; neither free clusters nor directory slots were
anywhere near exhaustion, so "`/Temp` filled" does not describe that
wedge under *either* model — and at 5.8% it describes it far less well
than the ~31% this paragraph used to claim. The correction strengthens
the conclusion rather than softening it. Treat 15 as "a device once wedged here", not as a limit,
and size the budget against an **unknown mechanism**. Consumer call counts (recounted
across all six consumer lanes, 2026-09-10; reported, not verified here)
bound it from below and show a low budget costs normal consumers nothing:

| Consumer shape | Leaking POSTs per default run |
|---|---|
| Ordinary runner-verb consumers (~14 sites) | 1–2 |
| One host-Python UCI driver | 4–8 |
| `bench_p256_u64.py` / `bench_p384_u64.py` `ALL_SPEEDS` sweep | **17** |
| A wireguard soak loop, one PRG per iteration | N (`--soak 15`+ wedges) |
| Two multi-hundred-write lanes | lane bugs, to be chunked onto PUT |

**The 17-per-run sweep is the case this budget exists for.** It would
otherwise accumulate 17 attachments *in a single invocation*, and it is
pure `run_prg` — it cannot be moved off the POST path by chunking or by
driving the protocol C64-side, so hygiene is the only fix available to
that consumer. At a budget of 6 the pass fires on that run's 7th and 13th
calls, holding resident attachments at budget + keep = 8, inside
upstream's own figure. A budget of 10 or more would let that sweep run to
completion with nothing having happened; that is the reason for a low
number rather than a generous one. Three
c64-https rigs already run a lane-local GC keeping 2 — this design should
make those redundant, and does not conflict with them (both delete
oldest-first by the same pattern).

**What actually fails is the firmware, not the folder.** On a wedged
machine the C64 FPGA keeps running while the device firmware is dead: it
stops answering the network *and* stops responding to the physical menu
button on the case. So the question this section used to ask — is the
limit a file count or a byte budget? — was a category error on both
sides. Both asked about `/Temp`'s capacity, and capacity is not what
fails; at **~5.8%** full with 15 directory entries (16 MiB disk —
`ramdisk.cc:25` plus the `1.1.0` linker symbols, see above), nothing was
remotely near exhaustion, which is why no capacity story ever fit the
arithmetic.

Accumulation crashes the firmware. The **cause is not established** and
should not be asserted: pre-fix, `attachment_writer` created
`/Temp/temp%04x` from a static counter and never deleted the files, but
`TempfileWriter`'s destructor *does* free both the `strdup`'d filenames
and the buffers, so a naive per-request heap-leak story does not hold on
its face. Heap fragmentation, per-entry allocation in directory
traversal, and FileManager bookkeeping growth are all candidates, none
run down. Since the trigger threshold is unknown, the budget is a choice
about which error to make. Do **not** resolve it experimentally — the
experiment is "upload until the firmware crashes", on a device nobody can
power-cycle remotely; it becomes safely measurable only with someone
physically present.

With the default keep-count of 2 the steady
state is at most 8 resident. Override with `U64_TEMP_GC_BUDGET` or
`temp_gc_budget=`. The pass also runs as a **drain** on `client.close()`
and when the device's `DeviceLock` is released (registered via
`device_lock.register_release_callback`, fired while the flock is still
held so the device is still exclusively ours) — so a lane hands the
device to the next one clean. `machine:reboot` does **not** reset the
count: it is a C64-level reset, and `/Temp` is a firmware RAM disk
(`software/filesystem/ramdisk.cc`) that only a firmware power-on clears.

**A `run_prg` that takes the 404 fallback costs two attachments**, not
one — on the reading that the firmware writes an attachment for the
runner POST *before* deciding to answer 404. That is the conservative
reading and the one the accounting follows; it has not been measured, and
if the firmware rejects before attaching, the fallback costs one. Either
way the choke point counts what was actually issued, so no special case
is needed. Note the shape regardless: a 404 from `runners:run_prg` is
itself a wedge symptom, so the path that may cost double fires exactly
when the device is closest to the edge. Whether a nearly-exhausted budget
should decline the fallback and fail loudly instead is an open question,
deliberately not decided here.

**The grading is logged on every run**, armed or not: one INFO line per
client naming the host, the graded firmware and generation,
`writemem_post_safe`, the resulting `write_mem_query_threshold` and
whether hygiene armed (`Ultimate64Client.log_device_grading`). This is
deliberate and not diagnostic noise. The hazard is structurally invisible
from a machine carrying the fix — the multi-hundred-write lane passed
review because it was developed against a 3.15 U64E, where it is
harmless — and a hygiene pass that silently does the right thing would
preserve exactly that blindness. The line makes "which device am I on"
answerable from any run's log, including the ones where nothing went
wrong.

**When hygiene cannot run at all**, on a leak-prone device, the client
stops rather than walking the device to the wedge: one attempt is made
to enable `Network Settings > FTP File Service` over REST and the pass is
retried; if it still fails, further body-carrying POSTs raise
`Ultimate64TempHygieneError` naming the remedy. Bodyless calls (`reset`,
`reboot`, config PUTs, `readmem`) keep working, so a blocked client can
still drive recovery. Opt out with `U64_TEMP_GC_REQUIRED=0` (downgrades
to a warning) or `temp_hygiene=False` (disarms the pass entirely). None
of this arms on a `writemem_post_safe=True` device: no FTP, no config
mutation, no refusal.

Other knobs: `U64_TEMP_GC_KEEP` (keep-count) and
`U64_TEMP_GC_FTP_USER` / `U64_TEMP_GC_FTP_PASSWORD` (bench devices run
anonymous FTP; override for a device with FTP credentials configured).
Call `client.gc_temp_folder()` directly for a manual pass regardless of
arming. On firmware carrying #686 (Ultimate-line ≥ 3.15) the pass is a
no-op that finds nothing to delete — verified on the U64E 2026-09-02 —
so all of this matters only for the C64U until its firmware catches up.

**Correction (issue #153 comment, 2026-08-21):** the firmware's
attachment counter is hex, not decimal — `temp0009` is followed by
`temp000A`. `gc_temp_folder` matches `^temp[0-9a-fA-F]+$` and sorts by
the suffix parsed as base-16 (a decimal-only pattern silently leaves
every lettered name uncollected — this exact bug was already found and
fixed in the sibling `c64-https` repo, `tools/uci/_temp_gc.py` at
`a4f4c46`). Separately, the C64U ships `FTP File Service: Disabled` by
default (the U64E has it enabled); `gc_temp_folder` detects a refused
FTP connection and reports that the setting may need enabling via
`Network Settings > FTP File Service` — a runtime-only REST config
write, so it lives in firmware RAM until `save_config_to_flash` and a
firmware **power-on** reverts it. That revert is benign, because `/Temp`
is a RAM disk (`software/filesystem/ramdisk.cc`) and the same power-on
empties it.

**`machine:reboot` does neither.** This sentence used to say a reboot
"both reverts it and empties `/Temp`"; it is false on both halves.
`machine:reboot` is a C64-level reset — config in firmware RAM survives
it, and so do the attachments. Measured on the C64U: one POST leaves
`temp0008`, then `reboot()` plus a 6 s settle leaves `temp0008` still
there. Do not treat a reboot as cross-run `/Temp` protection.

## Wedge tiers

| Tier | Symptom | Probe | Recovery | Fallback when recovery fails |
|---|---|---|---|---|
| 1. REST / writemem | `POST /v1/machine:writemem` returns 404 or RST; TCP stack may wedge after repeated POSTs | [`liveness_probe`](../src/c64_test_harness/backends/ultimate64_probe.py) | [`recover`](../src/c64_test_harness/backends/ultimate64_helpers.py) (`reset` → `reboot`) | Physical power-cycle |
| 2. Runner | `run_prg` response body contains `"Cannot open file"`; REST otherwise healthy | [`runner_health_check`](../src/c64_test_harness/backends/ultimate64_helpers.py) | `client.reboot()` (typically) | Physical power-cycle |
| 3. UCI STATE bit | the wait-idle spin hangs ~161 s after sustained `SOCKET_WRITE`; queued datagram silently dropped; REST stays healthy throughout | [`uci_wedge_probe`](../src/c64_test_harness/uci_network.py) | None over the network | Physical power-cycle (only) |

### Tier 1 — REST writemem / TCP stack

Canonical evidence:

- `POST /v1/machine:writemem` returns `HTTP 404` ("Could not read data from
  attachment") on any body shape, while `PUT ?data=<hex>` still works.
- After repeated malformed POSTs, the firmware's TCP stack itself wedges
  and connect attempts time out.
- `GET /v1/version` and `GET /v1/info` continue to answer until the TCP
  stack tips over.

What we've ruled out: payload size and request count are not the trigger;
the trigger is `POST writemem` latency (~165–180 ms) under sustained
firmware load. Idle does not recover the writemem-degraded state.
`reset()` / `reboot()` return HTTP 200 but do not always clear it.

`liveness_probe` tags the failure mode (`"writemem_404"`,
`"writemem_timeout"`, `"tcp_stack_wedged"`, `"connection_reset"`,
`"unreachable"`, `"unknown"`). Do not retry the probe in a tight loop —
repeated POSTs against a degraded endpoint are the documented TCP-wedge
trigger.

**On leak-prone firmware the probe costs two `/Temp` attachments per
call** (measured on the C64U, fw 1.1.0, 2026-09-10: `/Temp` 0 → 2 for one
call, with a bodyless `read_mem` control at 0). Its docstring says the
probe issues "exactly one" writemem POST; that counts the probe write and
not the restore. It writes a 128-byte pattern at `$0334` by POST
(`ultimate64_probe.py:536-547`), then writes the original bytes back
through `_restore_quiet` — a second POST (`:712`, `:729`). Two bodies, two
attachments. `assert_healthy()` wraps it, and the 128-byte payload sits
exactly at the C64U's PUT ceiling but is sent by the probe's own raw REST
client, which does not consult the client's threshold.

The consequence inverts the obvious procedure: **the health check you
reach for when you already suspect a wedge spends two of a small budget,
and probing again on a bad result converges on the wedge you are
diagnosing.** Under the conservative count reading, seven or eight health
checks are the whole budget. Diagnose with bodyless calls first —
`get_info()`, `get_version()` and `read_mem()` all cost nothing — and
reach for `liveness_probe` deliberately, once, knowing the price. Tracked
as [#250](https://github.com/JC-000/c64-test-harness/issues/250).

### Tier 2 — Runner subsystem

Canonical evidence:

- `client.run_prg(b"\x01\x08\x60")` (load $0801 + RTS) returns a non-2xx
  response whose body contains the string `"Cannot open file"`.
- REST is otherwise healthy: `/v1/version`, `/v1/info`, `readmem`,
  `writemem` all answer normally.

What we've ruled out: this is not a C64-side state — `run_prg` resets the
6510 — and it is not REST-tier. The firmware's PRG-loader subsystem is
wedged.

`runner_health_check(client)` posts the no-op PRG, returns silently on
success, and raises `Ultimate64RunnerStuckError` on the wedged-runner
signature. Other failures (auth, timeout, generic `Ultimate64Error`)
pass through unchanged. The escalation is `client.reboot()` (a C64-level
reset that re-initialises cartridge and REU, ~8 s before the device is
reachable again); `client.reset()` is insufficient.

### Tier 3 — UCI STATE bit

Canonical evidence:

- After 2–3 successful `SOCKET_WRITE` test runs in a session, the next
  run hangs for ~161 s in the wait-idle spin. ("`uci_wait_idle`" is the
  consumer-side name for this pattern in issue #112 and in
  c64-wireguard; it is not a harness symbol. The harness emits the
  equivalent spin inline from `_build_wait_idle` / `_build_push_and_wait`
  in `uci_network.py`, which is exactly the unbounded loop
  `uci_wedge_probe` exists to avoid.)
- `UCI_STATUS` at `$DF1C` reads with the STATE bits (`$30` mask) stuck
  non-idle; the in-flight UDP datagram is silently dropped while STATE is
  stuck.
- After ~161 s the FPGA clears STATE on its own and subsequent commands
  resume — but the TX window for the dropped datagram is long gone.
- `client.reboot()` followed by a settle wait reports REST healthy. The
  next run wedges identically. **Reboot does not clear this state.**

What we've ruled out: this is not the 6510 (`run_prg` resets it every
run); it is not REST (`liveness_probe` and `runner_health_check` both
return healthy throughout the wedge); it is not the consumer's command
sequence (the canonical `build_socket_write` driver hits the same wedge
under sustained use). The wedge is in the FPGA-side UCI command
processor's STATE bits and is not reachable from any documented REST
endpoint.

`uci_wedge_probe(transport)` takes a short window of non-blocking reads
of `$DF1C` and classifies them as `"idle"`, `"busy_transient"`, or
`"wedged"`. It is observation-only — there is no over-the-network
primitive that clears this state.

## Diagnosis

The recommended order is cheapest-to-most-targeted: REST first (Tier 1
will mask any other layer), runner second, UCI last.

Start from the right instrument. **`probe_u64()` cannot see a dead write
path**: it is an ICMP ping, a TCP connect and `GET /v1/version`, all
read-only, so a device that answers every GET while rejecting memory
writes reports `reachable=True`, `api_ok=True`, `error=None` — exactly
the Tier-1 shape above
([#241](https://github.com/JC-000/c64-test-harness/issues/241)).
`liveness_probe()` is the one that exercises a write, which is also why
it costs two `/Temp` attachments on leak-prone firmware (see Tier 1).
Those are the two halves of the same trade: the free check is blind to
this failure, and the check that sees it is the expensive one.

```python
from c64_test_harness import Ultimate64Client, liveness_probe, uci_wedge_probe
from c64_test_harness.backends.ultimate64_helpers import runner_health_check
from c64_test_harness.backends.ultimate64_client import Ultimate64RunnerStuckError

host, port, password = "10.43.23.81", 80, None

# 1. REST liveness — catches Tier 1 (writemem-degraded / TCP wedge)
result = liveness_probe(host, port, password)
if not result.healthy:
    # result.failure is one of:
    #   "unreachable", "writemem_404", "writemem_timeout",
    #   "tcp_stack_wedged", "connection_reset", "unknown"
    # result.recommendation has the next-step hint.
    ...

# 2. Runner health — catches Tier 2 once REST is up
client = Ultimate64Client(host=host, port=port, password=password)
try:
    runner_health_check(client)
except Ultimate64RunnerStuckError:
    client.reboot()
    # then re-probe before declaring recovered
    ...

# 3. UCI state — catches Tier 3
probe = uci_wedge_probe(target.transport)
if probe.is_wedged:
    # No automated recovery: see "When power-cycle is the only option".
    raise RuntimeError("UCI STATE wedged; physical power-cycle required")
```

Each step asserts a strict superset of the previous one's healthiness, so
a failure at step N means the wedge lives at tier N (or, very rarely, the
device transitioned between probes). Do not skip tiers — a UCI wedge with
the writemem path also degraded looks like a Tier 1 failure to a probe
that only checks Tier 3.

## Recovery primitives

`reset()` — `PUT /v1/machine:reset`. Soft 6510 reset; instant; over the
wire. Restarts the CPU only: it does not re-initialise the cartridge or
the REU, which is the line between it and `reboot()` below. Does not
clear writemem-degraded state on its own. Does not clear UCI STATE-bit
wedges.

`reboot()` — `PUT /v1/machine:reboot`. **A C64-level reset, not a
firmware reboot**, despite the endpoint's name: it is
`C64::start_cartridge(NULL)`, so the C64 side restarts with cartridge and
REU re-initialised while **the firmware itself keeps running** (see
`Ultimate64Client.reboot`'s docstring, `ultimate64_client.py:1200-1206`).
~8 s; over the wire. That re-initialisation is why it recovers REU/DMA
stuck state and clears most runner-tier wedges — and the firmware's
survival is why it clears neither `/Temp` (a firmware RAM disk; only a
power-on empties it) nor lwIP/UCI stack state.
**Does not clear UCI STATE-bit wedges** (verified against repeated repro
in issue #112: reboot + 12 s settle returns REST healthy, the next test
wedges identically).

`recover()` — composite. Issues `reset()` + settle, probes for REST
reachability with `is_u64_reachable`, escalates to `reboot()` + settle
only if REST is still down, and raises `Ultimate64UnreachableError` if
both fail. Returns `"reset"` or `"reboot"` to indicate which step
restored reachability. **Short-circuits on REST liveness**: if the
underlying wedge is UCI-tier (Tier 3) and REST stays healthy throughout,
`recover()` declares success after `reset()` without ever calling
`reboot()`, and the next test wedges identically. For UCI wedges,
`recover()` is not the right primitive.

`poweroff()` — `PUT /v1/machine:poweroff`. See "The poweroff guard"
below; under the default `confirm_irrecoverable=False` the method raises
`Ultimate64UnsafeOperationError` instead of firing the request.

For "the device looks stuck, recover it" scenarios that are not UCI-tier,
prefer `client.reboot()` directly over `recover()` — the latter's
REST-only liveness check is fine for Tier 1 but masks Tier 3.

## The poweroff guard

`Ultimate64Client.poweroff()` is irrecoverable over the network. After
the call, the device drops off the network entirely (no ICMP, no TCP, no
HTTP) and only a physical power-cycle restores it. The method requires
`confirm_irrecoverable=True`; without it, it raises
`Ultimate64UnsafeOperationError` rather than firing the request.

Do not reach for `poweroff()` as a generic recovery primitive. For
REU/DMA stuck state and the other issues a reboot does clear,
`client.reboot()` is the right call. Multiple agents have called `poweroff()` thinking it was a
benign reset, then mis-diagnosed the unreachable state as a "hung
device" — wasting troubleshooting cycles each time.

## When power-cycle is the only option

**Cost, stated plainly:** a `/Temp` firmware crash on the C64 Ultimate put
the device out of service for about two weeks in August–September 2026,
because nobody was physically present to power-cycle it. (Recorded here as
a UCI STATE-bit wedge until 2026-09-11, when the owner confirmed REST was
down throughout — see below.) No REST endpoint
restarts the firmware — `machine:reboot` is `C64::start_cartridge(NULL)`,
a C64-level reset, and no `machine:*` route restarts the firmware itself —
so nothing re-initialises lwIP or clears stack-level state remotely.
(The route list was last enumerated at firmware 8fb73523 as menu_button,
reset, pause, resume, poweroff, writemem and debugreg. That enumeration was
**never complete**: at tag `1.1.0` `software/api/route_machine.cc` has twelve
registrations across ten route names (`writemem` and `debugreg` each
register two verbs), including the `GET machine:readmem` the harness uses constantly and
a `GET machine:measure` nothing in this repo mentions. The conclusion is
unaffected — none of them restarts the firmware — but the seven-route list
was wrong when written, not outdated: `GET machine:readmem` is the route
the harness has called since 3.14d and existed at 8fb73523 too. Do not
re-enumerate at each firmware bump; the method was the problem.) Before running a sustained UCI `SOCKET_WRITE` load on a device
that only a remote agent is using, ask whether anyone can reach its power
switch this week; if not, do not run it.

**Was that outage a `/Temp` consequence? Yes — settled by the owner,
2026-09-11.** This section previously recorded the two-week outage as a UCI
STATE-bit wedge, and the Status section above attributes the whole wedge
family to Temp-folder accumulation. The two labels were never reconciled.
The owner has now settled it from direct observation: **REST was down**
during those two weeks.

That is decisive, because this document already records the discriminator.
A UCI STATE-bit wedge leaves REST *healthy* — Tier 3's "What we've ruled
out" states that `liveness_probe` and `runner_health_check` both return
healthy throughout. A `/Temp` crash takes REST and the UCI bridge down
together with the firmware. REST being down rules the UCI wedge out and
matches the `/Temp` crash exactly.

The owner's account of the presentation, which matches the crash model
recorded above: the FPGA keeps running and the C64 keyboard stays
responsive, so the machine looks alive, but the firmware is dead — the
button on the side of the case will neither soft-power-off nor bring up
the firmware menu.

**Consequences.**

* The two-week cost **does** belong to the `/Temp` regime and may be cited
  as such. An earlier revision of this scrub removed that citation from
  CLAUDE.md and the skill files on the grounds that it belonged to a
  different mechanism; that removal was based on this document's own
  mislabelling and has been reversed.
* This section's "UCI STATE-bit wedge" label on the 2026-08/09 outage is
  **wrong** and is corrected above. Tier 3's UCI STATE-bit wedge is a real
  and separate failure (#112) — it simply is not what cost two weeks.
* What is still **not** established is the crash mechanism. The owner's
  operating theory is space exhaustion in the RAM disk. That is plausible
  for this outage, whose `/Temp` occupancy nobody recorded, but note it is
  not what the one measured reproduction shows: the U64E/3.14d repro
  wedged with ~5.8% of a 16 MiB RAM disk used (#261). Those are different
  devices, different firmware and different events, so the repro does not
  refute exhaustion here — but at least one wedge happened nowhere near
  full, so "ran out of space" cannot be the whole story for the failure
  class. Accumulation triggers the crash; why it crashes remains open.

The currently confirmed cases where physical power-cycle is the **only**
documented recovery:

- **UCI STATE-bit wedge after sustained `SOCKET_WRITE`** (issue #112).
  Verified by the repro author: `client.reboot()` + 12 s settle reports
  REST healthy, the next test wedges identically. No REST endpoint
  clears the FPGA-side STATE bits.
- **TCP stack wedge after repeated malformed `POST writemem`**.
  Verified empirically on fw 3.14d: `reset()` / `reboot()` return
  HTTP 200 but the writemem-degraded state persists, and further probing
  in a tight loop tips the TCP stack over for good.

Consumers should fail-fast when they detect either case rather than
attempt automated reboot. A `uci_wedge_probe(...).is_wedged == True`
result or a `liveness_probe(...).failure == "tcp_stack_wedged"` result
should propagate as an error that requires human-mediated power-cycle,
not be papered over with `reboot()` in a retry loop.

The fix for both cases is firmware-side. When firmware exposes a
UCI-state reset or a writemem-state clear endpoint, the corresponding
fail-fast can be swapped for a direct recovery call.

## An unparseable `address` is not rejected — it writes to `$0000`

Not a wedge mode, but the same class of surprise and it belongs next to
them. `PUT /v1/machine:writemem?address=0xZZZZ&data=...` returns **HTTP
200** and writes the payload at zero page (measured on the C64U, fw
1.1.0, 2026-09-10). The firmware does not validate an address it could
not parse; it uses zero.

`Ultimate64Client.write_mem` validates `0..0xFFFF` before it builds the
query, so ordinary callers are safe. Anyone assembling the query string
themselves, or calling `_request` directly, gets a silent zero-page
clobber — `$0000`/`$0001` are the 6510 CPU port — reported to them as
success. Tracked as
[#251](https://github.com/JC-000/c64-test-harness/issues/251).

## Cross-references

- Issue [#112](https://github.com/JC-000/c64-test-harness/issues/112) — UCI STATE-bit wedge after sustained `SOCKET_WRITE`
- [`docs/uci_networking.md`](uci_networking.md) — UCI command interface, `$DF1C` STATE bits, send-size constraints
- [`docs/bridge_networking.md`](bridge_networking.md) — VICE-side ethernet pathways (separate from U64 recovery, but adjacent when porting consumers across backends)
- [`src/c64_test_harness/backends/ultimate64_probe.py`](../src/c64_test_harness/backends/ultimate64_probe.py) — `liveness_probe`, `probe_u64`, `LivenessResult`
- [`src/c64_test_harness/backends/ultimate64_helpers.py`](../src/c64_test_harness/backends/ultimate64_helpers.py) — `recover`, `runner_health_check`
- [`src/c64_test_harness/backends/ultimate64_client.py`](../src/c64_test_harness/backends/ultimate64_client.py) — `reset`, `reboot`, `poweroff`, `Ultimate64RunnerStuckError`, `Ultimate64UnsafeOperationError`, `Ultimate64UnreachableError`
- [`src/c64_test_harness/uci_network.py`](../src/c64_test_harness/uci_network.py) — `uci_wedge_probe`, `UCI_CONTROL_STATUS_REG` (`$DF1C`), STATE-bit masks
- Issue [#153](https://github.com/JC-000/c64-test-harness/issues/153) — automatic FTP `/Temp` GC to defuse the writemem-accumulation wedge before it starts

- [`src/c64_test_harness/backends/ultimate64_temp_gc.py`](../src/c64_test_harness/backends/ultimate64_temp_gc.py) — `gc_temp_folder`, `TempGCResult`, `auto_gc_override`, `hygiene_required`, `leak_budget`
