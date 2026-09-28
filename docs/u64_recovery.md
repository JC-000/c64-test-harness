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
uploads arrive as attachments that land in Temp — visible in the firmware
route table itself: at tag `1.1.0`, `software/api/route_machine.cc`
registers `API_CALL(POST, machine, writemem, &attachment_writer, ...)`, and
the same `&attachment_writer` binding appears on the `configs`,
`drives:mount`, `drives:load_rom` and `runners:{sidplay,load_prg,run_prg,run_crt}`
POST routes (`runners:modplay` binds `&attachment_reu` instead; the full
list is in `Ultimate64Client._creates_temp_attachment`'s docstring). The
route table establishes that attachments are *created*; that their
accumulation crashes the firmware is the owner's settled account
(2026-09-15), not the citation's. Without garbage collection the
accumulation produces the latency drift and eventual wedge described in
every tier below.

Two cautions about how far that goes, both expanded under "The hygiene
pass is prevention, not recovery" below. The accumulation fills `/Temp`,
and a full `/Temp` **crashes the device firmware** (owner, 2026-09-15): the
C64 FPGA keeps running while the firmware stops answering the network and
stops responding to the physical menu button. And nobody knows how many
uploads that takes: no count of uploads before the crash is kept here, and
why a full `/Temp` crashes the firmware, rather than failing writes, is not
established.

The fix is upstream in
[GideonZ/1541ultimate#686 "Add automatic cleanup of Temp folder"](https://github.com/GideonZ/1541ultimate/pull/686)
(merged 2026-04-26). **That merge is an ancestor of the `v3.15` tag**, so
every Ultimate-line 3.15 build carries it. The bench U64E is on a
3.15-line fork build (it reports `git_commit_hash` bce4535e
since 2026-09-15) and is fixed: measured 2026-09-02, on its then build
7f6fcb51, with the harness GC off
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
- Once the C64U runs fixed firmware, the tier-1 mitigations and the
  tier-3 `uci_wedge_probe` become diagnostic history rather than
  operating procedure. Re-validate against the new firmware before
  deleting anything — the deterministic repro in issue #112 is the
  intended verification.

### Harness-side mitigation: FTP `/Temp` GC (issue #153)

On unfixed firmware, `run_prg` (and any other endpoint that carries a
body — `writemem` above the device's threshold, `load_prg`, `run_crt`,
`sidplay`, `set_config_items_batch` (`POST /v1/configs`), the multipart
`mount_disk`/`load_rom` bodies) leaks a managed attachment
(`temp0000`, `temp0001`, ...) per call. Keyboard injection is **not** on
that list: `Ultimate64Client.send_text` writes at most `KEYBUF_MAX = 10`
bytes per request, which is under either threshold and so always takes
the bodyless `PUT ?data=` form. Ultimate-line 3.15 collects them
on-device (#686); the C64U on 1.1.0 does not. This is shared 1541ultimate firmware behaviour, not
specific to either device generation. `ultimate64_temp_gc.gc_temp_folder(host, ...)`
deletes those files over FTP, oldest-first, keeping the youngest N
(default 2) — mirroring the policy 1541ultimate#686 applies on-device on
Ultimate-line ≥ 3.15. It is best-effort: any FTP/network failure is captured
in the returned `TempGCResult.error` rather than raised, so a hygiene
pass can never fail a test run.

**Mounted images are excluded from the sweep (#418).** The `temp####`
pattern does not only match leaked attachments. An image uploaded as a
**raw** body, or as a multipart part with no `filename=`, keeps the
firmware's managed name and is then mounted *from that file* — on 1.1.0
`route_drives.cc` passes the full `/Temp/tempXXXX` path into `api_mount`
and `c1541.cc` stores it as the drive's `mount_file_name` — so an
oldest-first sweep could delete a mounted image's backing store. Harness
uploads are not exposed (`mount_disk` sends a named `image.<type>` part
since #311); other clients' raw uploads are. Before deleting anything,
the sweep therefore reads `GET /v1/drives` (bodyless, zero `/Temp` cost)
and skips every managed name a drive has mounted, matching on
*basenames* so both `/Temp/tempXXXX` and a bare `tempXXXX` are caught.
Those names come back in `TempGCResult.mounted_excluded`. The exclusion
can only ever **shrink** the delete set, and the listing is requested
only when the sweep would otherwise delete something.

**Evidence grade for the exclusion: unit-tested only — it has never run
against a device.** The mount path is source-read at tag `1.1.0`, and the
mounted name was observed once (n=1) on the U64E, fw 3.15 `bce4535e`,
2026-09-15, where a raw mount reported `image_file` =
`/Temp/cache/upload/temp0082`. The deletion this prevents has **not** been
reproduced. Note that the live verification recorded in the next paragraph
is dated 2026-08-21 and predates #418: it covers the keep-count sweep, not
the exclusion.

**A failed drives listing is not a failed hygiene pass.** If the listing
cannot be read the sweep proceeds on the keep-count alone, records why in
`TempGCResult.mounted_probe_error`, and leaves `.error`/`.ok` untouched —
so it does *not* trip the refusal described below. That asymmetry is
deliberate: skipping the sweep would trade a recoverable data hazard (a
deleted image can be re-uploaded) for the unrecoverable one this whole
mechanism exists to prevent. What the 1541 emulation does when a mounted
read-write image's backing file disappears is not established.

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

`Ultimate64Client` decides for itself whether to run the pass, and it
guards every attachment-creating request, not only `run_prg`.

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
File Service without leaking an attachment to do it. The route table was
read at tag `1.1.0` (the C64U's firmware), and the 3.15 line pairs verbs
and handlers the same way. Two deliberate conservatisms: `runners:modplay`
(`&attachment_reu` — the body goes to the REU) and, on the 3.15 line,
`machine:input` (`&input_json_writer`) are both counted anyway.

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

**Writes through the transport do not leak.** Most of the harness's own
generated 6502 blobs (the UCI builders, the `bridge_ping` routines) are
longer than the 128-byte PUT ceiling; `len()` of a builder's output gives
its size. They reach the device through `Ultimate64Transport.write_memory`
— directly, via `execute.load_code()` (a bare alias for it), or via
`uci_network._execute_uci_routine` — and since
[#252](https://github.com/JC-000/c64-test-harness/issues/252) that method
splits every write into chunks of at most the transport's
`rest_put_chunk_size` (the client's `write_mem_query_threshold`, capped at
128) on any device not graded `writemem_post_safe` — leak-prone,
unprobed, or unknown. Every chunk is a bodyless PUT, so a routine upload,
a UCI socket write's payload and its socket-id and length bytes all cost
**nothing** on the C64U. A post-safe device keeps the single POST, which
that firmware collects. The price on the leak-prone grade is one round
trip per chunk, and the 6510 runs between chunks, so no chunked write is
atomic; the timing-sensitive paths (the ip65 "≥ 0.2 s after `ip65_init`"
rule) are **not live-verified** chunked on a C64U. `write_bytes` also
chunks at `rest_put_chunk_size` on an Ultimate transport, on every grade
(owner decision 2026-09-15, #252); VICE and other transports keep 84.
`enable_uci` / `disable_uci` are `set_config_items` — bodyless, zero
attachments.

What still leaks on the C64U: a direct `client.write_mem` above the
threshold, `run_prg` / `load_prg` / `run_crt` / `sid_play`,
`set_config_items_batch` (`POST /v1/configs`; `route_configs.cc:251`
binds `&attachment_writer` at `1.1.0`), the multipart `mount_disk` /
`drive_load_rom` bodies, and `liveness_probe` (two per call, see Tier 1).
`mod_play` is counted too, although its body goes to the REU. The budget counts
*attachments* at the one request choke point, so none of these needs a
table of its own.

**Where the protocol is driven still decides the round-trip count.** UCI
driven from host Python costs a routine upload (several chunked PUTs on a
leak-prone device) per operation, so a fetch made of many `socket_read`s
is many requests. The same protocol driven C64-side, from inside an
uploaded PRG, costs only the one upload. For a caller that bypasses the
transport it is also the only route that avoids a leak — leak
*elimination*, not hygiene, and the first thing to reach for.

The large POST form itself carries an unresolved question
([#231](https://github.com/JC-000/c64-test-harness/issues/231)). A 47,103-byte
single-call write at `$0801` on the U64E (fork build
`71480a9d`), with the machine running from `READY.`, read back
with exactly one wrong byte in both of n=2 trials, at offsets 2715 and
3181. The same bytes written in 84-byte chunks were byte-exact 2/2. A
controlled re-run on the U64E at `4011c97c` (range `$0801-$9FFF`, single
POST, 25 trials into a sentinel bed) found 0/50 corrupted writes, 25
payload and 25 bed, with the CPU paused. With the CPU running, the
payloads were 0/25 corrupted, but one bed write showed 2 bad bytes just
after reset, which fits the machine writing its own RAM. So a running CPU
is a sufficient confounder for the original signature, and a readback
past `$A000` reads BASIC ROM. The builds differ, and the issue is closed
as non-reproducing, not refuted. It reopens only on a mismatch with the
CPU paused and the payload entirely below `$A000`.

**SocketDMA writes are disabled pending a stability review**, so the
SocketDMA fast path is not a route off the POST path that anyone may take
today. `Ultimate64Transport`'s `socket_dma` defaults to `False`; leave it
there. A SocketDMA write which falls back to REST is chunked like any
other write on a non-post-safe grade, and the fallback latches:
`_socket_dma_unusable` is never cleared, not even by `close()`, so one
connect failure routes every later write on that transport to REST for its
lifetime after a single WARNING.

**Cadence.** A per-device budget of
`ultimate64_temp_gc.DEFAULT_LEAK_BUDGET` = **6** attachment-creating
calls, counted across every client of that host in the process (issue
#295). The pass runs before the call that would overrun it, and only a
successful pass resets the count.

**The budget is per device only *within a process*; across processes the
bound is the lock** (owner decision 2026-09-22, recorded on
[#433](https://github.com/JC-000/c64-test-harness/issues/433): accept and
document; counting in the lockfile or on the device was declined).
`TempLedger` is a module-level registry, so two processes driving one
leak-prone device keep two ledgers and each spends its own budget: the
worst peak before a sweep is `budget x processes`, not `budget`. Two
things bound the cross-process case today, and it is worth being precise
about which case each one covers:

- the **lock-release drain**, which sweeps the device while the releasing
  process still holds its `DeviceLock`, so the next lane inherits a clean
  `/Temp`;
- the **inherited sweep** ([#264](https://github.com/JC-000/c64-test-harness/issues/264)),
  by which an armed client that leaked nothing still collects what it
  found while holding the lock.

Both act at a hand-off. **Neither bounds two processes uploading
concurrently.** Held as intended the lock prevents that — the uploads are
destructive, and taking turns is what the lock is for — but the lock is
advisory ([`device_locking.md`](device_locking.md)), and
`run_u64_parallel_locked.py` interleaves tests from several processes on
one device, releasing between them. So the cross-process bound is the
`DeviceLock` serialising uploads plus the lock-release sweep, and **the
residual is a process that uploads without holding the lock**.

Why 6. Upstream is the only firm bound: the firmware's
own post-#686 collector keeps at most **10** managed files
(`kManagedTempMaxFiles`), upstream's own statement of a safe resident
count for this folder on this device family, needing no conditions.
Nothing bounds it from the other side. How many uploads an unpatched
device survives before `/Temp` fills and the firmware crashes has never
been measured, and the earlier guessed wedge count (one U64E on 3.14d,
n unrecorded) is retired (owner, 2026-09-15): do not size against it.
The RAM disk is **16 MiB**: `ramdisk.cc:25` computes its size as
`__ram_disk_limit - __ram_disk_start`, and at tag `1.1.0` both
`target/u64/riscv/ultimate/linker.x` and
`target/u64ii/riscv/ultimate/linker.x` set those symbols to `0x02000000`
and `0x03000000` — a 16 MiB span. (The `// 3 * 1024 * 1024` on that same
line is a stale trailing comment, not the value; a "~3 MB RAM disk"
propagated through five documents on the strength of it. Corrected in
[#261](https://github.com/JC-000/c64-test-harness/issues/261).) That size
is not a count of uploads. Consumer call counts (recounted
across all six consumer lanes, 2026-09-10; reported, not verified here)
bound it from below and show a low budget costs normal consumers nothing:

| Consumer shape | Leaking POSTs per default run |
|---|---|
| Ordinary runner-verb consumers (~14 sites) | 1–2 |
| One host-Python UCI driver | 4–8 |
| `bench_p256_u64.py` / `bench_p384_u64.py` `ALL_SPEEDS` sweep | **17** |
| A wireguard soak loop, one PRG per iteration | N (a long soak fills `/Temp`) |
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

**Why the budget counts attachments.** On a wedged machine the C64 FPGA
keeps running while the device firmware is dead: it stops answering the
network *and* stops responding to the physical menu button on the case,
and the owner's account (2026-09-15) is that a full `/Temp` does this. The
RAM disk's byte size (16 MiB — `ramdisk.cc:25` plus the `1.1.0` linker
symbols, see above) does not translate into a count of uploads for any
workload, so the budget counts attachments conservatively instead.

A full `/Temp` crashes the firmware. **Why** it crashes, rather than
failing writes, is not established and should not be asserted: pre-fix, `attachment_writer` created
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
device to the next one clean. The two drain cases differ:

- **The client leaked:** the ordinary pass runs, on `close()` or lock
  release. If FTP is refused, it makes one attempt to enable
  `Network Settings > FTP File Service` — a config write that persists
  until a firmware power-on (`machine:reboot` does not clear it) — and if
  the pass still fails, later uploads on that client refuse. A lane that
  leaked may make that write (owner decision on #263): it is the one
  sanctioned write into a `BASELINE_NEVER_TOUCH` store, whose contract
  covers the entry-baseline reset (`apply_factory_baseline`) only.
- **The client leaked nothing** (issue #264): the wedge is per device and
  `gc_temp_folder` sweeps `/Temp` device-wide, so the drain still sweeps
  inherited attachments — but only **under the device lock** (the
  lock-release callback, or `close()` while this process holds the lock),
  because it deletes files other lanes created. A failed inherited sweep
  writes no config and blocks nothing: it logs a WARNING that `/Temp` may
  still hold an earlier lane's attachments and that FTP File Service must
  be enabled by hand, or the device power-cycled, before uploading.

**The budget is per device, within one process** (issue #295). The count,
the refusal state and the one FTP-enable attempt live in a process-wide
`TempLedger` (`ultimate64_temp_gc.py`), keyed by the normalised host.
Normalising folds together case, scheme, a trailing dot and the
spellings of one IP address. It does **not** fold a name with its
address, because that needs DNS; use one spelling per device. A
**non-default port is kept**: only `:80` folds (`DEFAULT_REST_PORT`, the
client's own default), because `host:80` and `host` name one device while
`gw:8080` and `gw:8081` do not — `DeviceLock` keys those apart, so a
ledger that merged them would let a failed pass against one refuse
requests to the other. So:

- a fresh client per upload no longer resets the budget;
- two clients of one device spend one budget between them;
- a lock release drains **once per host**, however many clients were built.
  The ledger is what registers the release callback, and it picks one
  client to drain: armed with a leak of its own first, then armed.
- A leaking client whose pass fails blocks attachment-creating calls on
  **every** armed client of that device, until any client's sweep succeeds.
  Bodyless calls, `temp_hygiene=False` clients and `U64_TEMP_GC_REQUIRED=0`
  are not blocked.
- Whether a client may take the leaking-lane path (the FTP-enable attempt)
  is decided by that client's own uncollected share, on **both** routes
  into the pass. **A client that leaked nothing never writes config** —
  not on the drain, where the inherited sweep writes none, and not on the
  budget path either, which a client whose own share is zero can reach
  precisely because the budget counts the *device*: another client's
  attachments, or a `temp_hygiene=False` client's, can be what crosses it.
  Such a client still sweeps, and still blocks the device if its sweep
  fails; it just makes no `Network Settings` write.
- **A counted attachment is in flight until its request returns.** The
  count happens before the send, so that the budget check and the count
  are one atomic step, which leaves a window where a sweep could otherwise
  zero a count the attachment is about to land into. A successful sweep
  therefore resets the count to the still-in-flight reservations rather
  than to zero, and whatever a reservation never sent is refunded when it
  ends. **The carry is approximate in one direction, and deliberately so:**
  if a sweep lands after the attachment has been written but before the
  reservation ends, `collected()` carries a reservation whose attachment
  that sweep already collected, and the count reads one higher than the
  device holds. That is the conservative direction — the cost is an extra
  hygiene pass nobody needed, never an uncounted attachment — but it means
  `pending_temp_attachments` is an **upper bound** on what the device
  holds, not a reading of the device. Do not treat it as device truth; the
  FTP listing is the only thing that is.
- **A leak outlives its client.** Release callbacks hold nothing strongly,
  so before the ledger a client that leaked and was garbage-collected
  before the lock release never drained. The ledger outlives its clients:
  if no live client attempts a drain, but armed clients counted
  attachments that are still pending, the ledger sweeps the device itself
  with the default FTP settings.
  - On failure it writes no config, logs a WARNING and blocks later
    attachment-creating requests until a sweep succeeds.
  - Attachments counted only by disarmed or post-safe clients are never
    swept this way.

`machine:reboot` does **not** reset the count: it is a C64-level reset, and
`/Temp` is a firmware RAM disk (`software/filesystem/ramdisk.cc`) that only
a firmware power-on clears.

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

**Attachment names are hex.** The firmware's counter is hex, not decimal —
`temp0009` is followed by `temp000A` (#153) — so `gc_temp_folder` matches
`^temp[0-9a-fA-F]+$` and sorts by the suffix parsed as base-16; a
decimal-only pattern would leave every lettered name uncollected. The C64U
ships `FTP File Service: Disabled` by default (the U64E has it enabled);
`gc_temp_folder` detects a refused FTP connection and reports that the
setting may need enabling via `Network Settings > FTP File Service` — a
runtime-only REST config write that lives in firmware RAM until
`save_config_to_flash`, reverted only by a firmware **power-on**. That
revert is benign, because `/Temp` is a RAM disk
(`software/filesystem/ramdisk.cc`) and the same power-on empties it.

**`machine:reboot` does neither.** It is a C64-level reset: config in
firmware RAM survives it, and so do the attachments. Measured on the C64U:
one POST leaves `temp0008`, then `reboot()` plus a 6 s settle leaves
`temp0008` still there. Do not treat a reboot as cross-run `/Temp`
protection.

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
call, with a bodyless `read_mem` control at 0). It writes a 128-byte
pattern at `$0334` by POST, then writes the original bytes back through
`_restore_quiet` — a second POST (`backends/ultimate64_probe.py`). Two
bodies, two attachments. `assert_healthy()` wraps it, and the 128-byte payload sits
exactly at the C64U's PUT ceiling but is sent by the probe's own raw REST
client, which does not consult the client's threshold.

The consequence inverts the obvious procedure: **the health check you
reach for when you already suspect a wedge spends two of a small budget,
and probing again on a bad result converges on the wedge you are
diagnosing.** Three health checks spend the whole per-device budget of 6
(`DEFAULT_LEAK_BUDGET`, per process). Diagnose with bodyless calls first —
`get_info()`, `get_version()` and `read_mem()` all cost nothing — and
reach for `liveness_probe` deliberately, once, knowing the price.

**The client accounts for it** (#250). `Ultimate64Client.liveness_probe`
(and so `assert_healthy`) routes both POSTs through the client's
`/Temp` accounting — `LIVENESS_PROBE_TEMP_ATTACHMENTS = 2` — and reserves
both before sending anything: if the budget cannot hold two, the hygiene
pass runs first, and if hygiene has been proven impossible it raises
`Ultimate64TempHygieneError` without touching the device, so the restore
is never the refused request.

**The free spelling is accounted too** (#450). The module-level
`ultimate64_probe.liveness_probe(host, ...)` — and so the
`c64_test_harness.liveness_probe` re-export — holds no client, but it
reserves the same two attachments against the same per-device ledger
(#295) once its own bodyless `GET /v1/info` has graded the firmware, and
before it reads or writes any RAM. Same budget, same sweep, same refusal:
a device whose hygiene pass has been proven impossible cannot be
health-checked by reaching for the free spelling instead. What it does
*not* do is write config — the FTP-enable write on a failed sweep belongs
to a client that leaked attachments of its own (#263), and this function
has no client. Whichever spelling you use, the count is the device's.

Three consequences worth knowing before you reach for the free spelling.
It **can raise** `Ultimate64TempHygieneError` — that is the refusal, and
it is the point, but it is an exception from a root export that did not
raise before #450. It **can sweep**,
and a sweep deletes `temp%04x` files, which per #418 can include a raw or
filename-less image another lane mounted; that trade was already accepted
for every client path and here it fires only on a budget crossing. And it
**says once per process and host when nothing in this process holds the
device's `DeviceLock`** (#194, #460) — a notice, not a refusal, because the
probe writes `$0334-$03B3` and writes it back under whoever else is using
the machine. An unknown firmware version arms rather than disarms: by then
step 1 has already reported the device reachable, so a `/v1/info` that
times out or cannot be parsed is the half-wedged device, not an empty
address.

**A refused restore does not make the probe unhealthy** (owner decision on
#328, 2026-09-15). `healthy` is decided by the probe write and its
read-back. If the restore POST then answers non-2xx or raises, the result
is `healthy=True, failure=None, scratch_restored=False`, with a WARNING
naming `$0334-$03B3`, and `assert_healthy()` passes. That is the same
404 that yields `failure="writemem_404"` when it lands on the first POST,
so read `scratch_restored` whenever the span, or that signal, matters.
No retry POST is added either way (#107).

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

Start from the right instrument. **`probe_u64()` on its defaults cannot see
a dead write path**: it is an ICMP ping, a TCP connect and `GET /v1/version`, all
read-only, so a device that answers every GET while rejecting memory
writes reports `reachable=True`, `api_ok=True`, `error=None` — exactly
the Tier-1 shape above
([#241](https://github.com/JC-000/c64-test-harness/issues/241)).
`probe_u64(..., check_write=True)` adds a write round trip: 8 bytes at
`$0334` are read, overwritten with their inverse by a query-string
`PUT writemem`, read back and restored, and the verdict is `write_ok`. It
carries no body, so it costs no `/Temp` attachment on any firmware. But it
exercises the PUT path only. A device whose POST `writemem` alone is
degraded can still pass it; the #241 report had `PUT ?data=` answering
200. `liveness_probe()` is the one that exercises POST, which is also why
it costs two `/Temp` attachments on leak-prone firmware (see Tier 1).

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
`Ultimate64Client.reboot`'s docstring).
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
because nobody was physically present to power-cycle it. No REST endpoint
restarts the firmware — `machine:reboot` is `C64::start_cartridge(NULL)`,
a C64-level reset, and none of the `machine:*` routes at tag `1.1.0`
(`software/api/route_machine.cc`) restarts the firmware itself — so
nothing re-initialises lwIP or clears stack-level state remotely. Before
running a sustained UCI `SOCKET_WRITE` load on a device that only a remote
agent is using, ask whether anyone can reach its power switch this week;
if not, do not run it.

**That outage was the `/Temp` crash, not a UCI STATE-bit wedge** (owner,
2026-09-11). It was first recorded here as a UCI wedge; the owner settled
it from direct observation that **REST was down** throughout. That is
decisive: a UCI STATE-bit wedge leaves REST *healthy* (Tier 3's "What
we've ruled out"), while a `/Temp` crash takes REST and the UCI bridge
down together with the firmware. The presentation matches the crash
model: the FPGA keeps running and the C64 keyboard stays responsive, so
the machine looks alive, but the firmware is dead — the button on the side
of the case will neither soft-power-off nor bring up the firmware menu.
Tier 3's UCI STATE-bit wedge is a real and separate failure (#112); it is
not what cost two weeks.

* **The driver is settled**: `writemem` attachment accumulation (owner,
  2026-09-11). Upstream removed it — GideonZ/1541ultimate#686, "Add
  automatic cleanup of Temp folder" (chrisgleissner, merged 2026-04-26,
  building on Gee-64's #474), which keeps the youngest 10 *managed* files
  and deletes older ones. The harness's own defence attacks the same
  driver one step earlier: staying at or below `write_mem_query_threshold`
  uses the bodyless `PUT ...?data=` path, which creates no attachment at
  all.
* **The mechanism is settled** (owner, 2026-09-15): "When /Temp is filled then the device firmware crashes. the c64 appears to continue to run but the rest api becomes unresponsive and the local firmware menu switch no longer has an effect. This is the outcome of the writemem garbage collection issue on unpatched firmware."
  How many uploads it takes is not known, and no count is kept here.
* **Why a full `/Temp` crashes the firmware**, rather than failing writes,
  is not established, and #686 does not settle it: it documents no crash,
  hang or exhaustion, and it **removed a disk-use trigger in favour of a
  file count**. Upstream's remedy is count-shaped, but that is a design
  choice in the cleaner, not evidence about what fails.

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
clobber on the C64U — `$0000`/`$0001` are the 6510 CPU port — reported
to them as success
([#251](https://github.com/JC-000/c64-test-harness/issues/251)). The
upstream fix is strict hex parsing in `readmem`/`writemem`/`debugreg`,
which answers HTTP 400 "Invalid address". The U64E's bce4535e build is
the merge commit of 1541ultimate PR #884 on the `test-merge` branch, so
it carries the fix. PR #888 replays the same change onto `master` and is
not an ancestor of bce4535e; a `u64ii` release cut from `master` after
#888 would carry it. The C64U's 1.1.0 predates both.

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
