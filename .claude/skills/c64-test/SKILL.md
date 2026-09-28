---
name: c64-test
description: Write and run tests for Commodore 64 assembly programs using the c64-test-harness Python package. Covers VICE emulator lifecycle, Ultimate 64 hardware testing, backend-agnostic target selection via UnifiedManager, direct-memory testing via jsr(), UI-driven testing, parallel execution, cross-process device queueing, and common pitfalls.
user-invocable: true
allowed-tools: Bash, Read, Write, Edit, Grep, Glob
argument-hint: "[test-subject]"
---

# c64-test-harness Skill

You are an expert at writing and running tests for Commodore 64 assembly programs using the `c64-test-harness` Python package. This skill helps you write correct, reliable C64 tests on the first attempt.

## Platforms

The primary machine is **macOS (27.0, Apple Silicon)** with Homebrew VICE 3.10 (`x64sc -features`: `HAVE_RAWNET yes`, `HAVE_PCAP yes`, `HAVE_TUNTAP no`). Tests must work there or skip with a platform-specific reason. Bridge/ethernet tests dispatch all platform-specific constants through `tests/bridge_platform.py` (`IFACE_A`, `IFACE_B`, `BRIDGE_NAME`, `ETHERNET_DRIVER`, `SETUP_HINT`, `iface_present()`) — never hardcode `feth*` / `bridge10` or the Linux names. On macOS, `ViceProcess` wraps x64sc with `sudo -n` whenever `ethernet=True` (VICE selects a pcap driver only at `geteuid()==0`; `/dev/bpf*` permissions are not consulted, and unelevated it SIGSEGVs on reset). The NOPASSWD entry must name the exact x64sc path launched, never `bash`-wrapped; without it the launch raises `ViceElevationRequiredError` telling you what to run. `probe_vice_pcap_ok()` requires a real `/dev/bpf*` attach (read with `bpf_attached_interfaces()`, i.e. `netstat -B`) so those tests skip instead of passing vacuously. See `docs/development.md` for the full setup (VICE via Homebrew, bridge lifecycle scripts, sudoers recipe).

**Linux (Ubuntu 25+):** the same `bridge_platform` names resolve to `tap-c64-0`/`tap-c64-1`, `br-c64` and the ungated `tuntap` driver (no elevation). Two separate records: the installer (`scripts/setup-dev-env.sh`) passed end to end in a fresh Ubuntu 25.10 aarch64 VM on 2026-09-28, including `verify-dev-env.sh --smoke` (#500); the bridge path was last run 2026-04-10 (the two-VICE demo, 10/10), and its scripts changed afterwards (the 2026-09-28 VM run created `br-c64`/`tap-c64-*` but ran no ethernet test). Treat the Linux ethernet/bridge test path as unverified at the current head.

## Do not wedge the C64U (standing clause, in force until its firmware increments)

The C64 Ultimate (10.53.21.158, fw 1.1.0) is a **shared remote** device
with nobody physically present. Its firmware predates
GideonZ/1541ultimate#686, so it never collects the managed `/Temp`
attachments that every body-carrying REST call leaves behind — `POST
/v1/machine:writemem`, `runners:run_prg` / `load_prg` / `run_crt` /
`sidplay`, the JSON `POST /v1/configs` batch (`set_config_items_batch`),
multipart `mount_disk` and `drives:load_rom` (`modplay` is counted by the
harness too, though by source its body goes to REU memory, not `/Temp`).
Enough accumulation **crashes the device firmware**: REST and the UCI
bridge go down together and **only a physical power-cycle recovers it**.
The C64 FPGA keeps running, so the machine looks alive while the firmware
is dead — it stops answering the network *and* stops responding to the
physical menu button. Accumulated attachments fill `/Temp`, and a full
`/Temp` crashes the firmware (owner, 2026-09-15). The RAM disk is
16 MiB (`ramdisk.cc` sizes it from `__ram_disk_start`/`__ram_disk_limit`
in `target/u64{,ii}/riscv/ultimate/linker.x` at tag `1.1.0`; its "3 MB"
comment is stale — issue #261), and nobody knows how many uploads fill it
before the crash: no count is kept, and none is to be guessed or measured
(owner, 2026-09-15). The 2026-08/09 outage cost about two weeks of a
shared device and **was** this failure — owner-confirmed 2026-09-11 that
REST was down throughout, which rules out a UCI STATE-bit wedge. Budget
conservatively as though the limit were a file count — that is a choice
about which error to make, not a measured limit.

When you write a test that can point at the C64U:

- **Do not hand-roll cleanup.** `/Temp` hygiene belongs in the harness,
  below your test, and it is there (PR #259): on leak-prone
  firmware `Ultimate64Client` arms a hygiene pass by itself. It sweeps
  `/Temp` before the first attachment-creating request after this process
  takes the device's `DeviceLock` (or ever), and again before each one once
  an attachment is pending (budget 1 **per device**, shared by every client
  in the process; keep 1; mounted images kept; #295, #511). It also drains
  on `close()` and on `DeviceLock` release: a client that leaked drains its
  own, and one that leaked nothing sweeps inherited `/Temp` only while
  holding the device's lock (#264). A handover sweep that finds FTP off
  makes one attempt per process at enabling FTP File Service, then
  retries (owner decision 2026-09-28). **It can also refuse.** Once a sweep
  it needed has failed (FTP down), or when this process does not hold the
  device's `DeviceLock` at all (#513), every later attachment-creating
  request raises `Ultimate64TempHygieneError`, before sending anything, until a
  sweep succeeds (import it from
  `backends.ultimate64_client`; it is not a package-root export) rather
  than walking the device toward the wedge; `U64_TEMP_GC_REQUIRED=0`
  downgrades that to a WARNING and `temp_hygiene=False` disarms the pass.
  A client constructed with an explicit `write_mem_query_threshold=` never
  probes and so is **silently unarmed**.
  So if your test needs a manual GC call to be safe, the guard is missing
  a case one layer down — fix it there. Details: PATTERNS § "`/Temp`
  attachment hygiene" rule 1.
- **Prefer the non-leaking paths.** `write_memory` at or under the
  device's `write_mem_query_threshold` takes `PUT ?data=` and leaves
  nothing behind (measured exact and inclusive on the C64U: 128 B → zero
  attachments, 129 B → exactly one). The SocketDMA fast path
  (`transport.socket_dma`, TCP 64) is **disabled pending a stability
  review — do not enable it**; it would leave nothing behind, but that was
  only ever reasoned from the code path rather than measured, and it is
  not an option today, which leaves no fast bulk-write path on a C64U at
  all. The non-leaking bulk route is `write_bytes` /
  `run_prg_via_sys`. On an Ultimate transport these chunk at the
  transport's `rest_put_chunk_size` (the client threshold, capped at 128),
  so they stay on the PUT path on **every** grade (#252; on a post-safe
  device that means more, smaller requests, accepted by owner decision
  2026-09-15). But on a C64U that is ~128 round trips for 16 KiB. Measured
  on the U64E (fw 3.15, bce4535e, 2026-09-15, host on Wi-Fi (en0), link
  not instrumented, n=2-4 per arm, interleaved, #267), `write_bytes` runs
  about 1.3 KiB/s at 48-byte chunks and about 3.2 KiB/s at 128-byte chunks
  (median ~36-52 ms per PUT, observed 34-79 ms, dominated by the host
  link), and a single 16 KiB POST took ~0.1 s there. The rate is not
  measured on the C64U, whose link latency is unknown, so budget for it
  being slow. REST POST is the leaking path — a bulk write that falls back to
  REST is the one to watch.
- **Do not assume an API chunks because its name suggests it.**
  `execute.load_code()` is a bare alias for `transport.write_memory`, and
  `_execute_uci_routine` writes its routine through `transport.write_memory`
  as well. Most assembled blobs are over 128 bytes
  (UCI builders 139-176 B, with `build_socket_close` 118 and
  `build_uci_probe`/`build_uci_status_peek` 12 under it; 366-509 with
  `turbo_safe=True`; RR-Net routines up to 907, with `build_tx_code` at
  159-180 since #487 and 199-223 with `drain_first=True`). Since #252
  (PR #294), `transport.write_memory` chunks at the client threshold on any
  grade that is not post-safe. On a C64U a UCI routine and its payload
  through the transport are therefore all PUTs and cost **no** attachment;
  on a post-safe device each over-threshold write is one collected POST.
  A direct `client.write_mem` call still does not chunk. `enable_uci`/`disable_uci`
  cost nothing (bodyless config PUTs). Host-driven UCI is still several
  round trips per operation; the same protocol driven C64-side inside an
  uploaded PRG costs only the upload. `memory.write_bytes` chunks at the
  transport's `rest_put_chunk_size` on every grade, which is why
  `run_prg_via_sys` costs nothing on a C64U, while bare `client.run_prg()`
  costs one per call.
- **Never loop an upload.** A parametrised test or retry loop that
  re-uploads a PRG is the exact re-upload shape that wedged the device.
- **Hold the `DeviceLock` across the whole run**, hygiene included, and
  drain on the way out. That applies to scripts and hand-driven sessions.
  **Pytest runs are the exception:** conftest locks per live test, so a
  pytest run does not exclude other lanes between tests. That is
  deliberate. An outer hold would defer the lock-release `/Temp` drain,
  the only one that catches a client a test never closes, to the end of
  the run, and a client collected before then would never drain;
  `close()` still drains a leaking client (#324; `docs/device_locking.md`
  rule 1).
- **A `TempGCResult` with `.error` set is a failed hygiene pass, not a
  benign skip.** FTP File Service is off by default on 1.1.0, so the GC
  silently no-ops there unless enabled. Do not keep uploading after one.
- **Never `poweroff()`.** `reboot()` is the recovery verb, and it does
  not clear a UCI STATE-bit wedge.
- **Do not touch the device at all** when the C64U is not the point of
  your task. Live gates stay unset by default — keep them that way.

Full statement, and the switch that retires this clause
(`DeviceCapabilities.writemem_post_safe`), in CLAUDE.md § "Standing
hardware-safety clause" and `docs/u64_recovery.md`.

Device *state* in CLAUDE.md — which device is up, wedged, or reachable —
can lag reality; the firmware-conditional rules above do not. REST
answering is not health either, since a UCI STATE-bit wedge leaves REST
up. Check the device-hosts memory, or establish state yourself with a
bodyless `GET /v1/info`, before assuming a device is up or down.

## When to Use This Skill

Use this when:
- Writing new test suites for C64 assembly routines
- Debugging failing C64 tests
- Setting up VICE emulator instances for testing
- Setting up Ultimate 64 hardware as a test target
- Selecting a backend at runtime (VICE vs U64) via `UnifiedManager`
- Working with the direct-memory (jsr-based) test pattern
- Working with the UI-driven (menu navigation) test pattern
- Setting up parallel test execution across multiple VICE instances
- Running multiple agents against a shared U64 device (cross-process queueing)
- Testing CS8900a ethernet via two-VICE bridge networking (RR-Net mode)
- Driving a real RR-Net cartridge in the Ultimate 64's expansion port
- Testing UCI socket-level TCP/UDP on Ultimate 64 Elite (incl. turbo speeds)
- Writing shippable 6502 networking code that needs wall-clock timeouts (TOD helpers)
- SID file playback and audio capture on VICE or Ultimate 64

## Quick Reference

See the supporting files in this skill directory for detailed API reference:
- `REFERENCE.md` — Full API reference for all c64-test-harness modules
- `PATTERNS.md` — Battle-tested patterns, templates, and gotchas

## Working method (not optional)

Every change to a C64 test or to the harness goes through **red/green with
a mutation check, then an adversarial review**, before it merges:

1. Write the test and show it **red** against the code as it was (stash the
   source change, run, restore). A test whose expected value equals the
   system default passes whether or not the code ran — it is not a test
   until it has been red.
2. Make it green, then **mutate the code under test** at least once (drop
   the guard, return the default, swap the order) and record which tests
   fail. A surviving mutation is a missing test.
3. Run the **`adversarial-reviewer` agent** (`.claude/agents/`) on the
   branch before merge and answer every finding, nits included. Only a
   `MERGE` verdict merges.

The full standard, including how measurements must carry their conditions
and why validation here is local-only, is `docs/development.md`
§ "Review standard".

## Core Principles

1. **ALWAYS use `ViceInstanceManager`** for VICE tests — it handles port allocation, PID tracking, transport creation, and cleanup. Never use `ViceProcess` or `PortAllocator` directly. This prevents port collisions and PID conflicts when multiple Claude agents run in parallel.
2. **Use `UnifiedManager` / `create_manager()` for backend-agnostic tests** — it selects VICE or U64 at runtime via the `C64_BACKEND` env var. For U64, it automatically wraps access with `DeviceLock` for cross-process queueing.
3. **All U64 access MUST use `DeviceLock`** — multiple agents sharing a single U64 device will corrupt each other's tests without it. `UnifiedManager` handles this automatically; if using `Ultimate64Transport` directly (e.g., in pytest fixtures), wrap with `DeviceLock` in a module-scoped fixture.
4. **Binary monitor transport only for VICE** — `BinaryViceTransport` is the sole VICE transport. It uses a persistent TCP connection via VICE's binary monitor protocol (`-binarymonitor`). The optional secondary text monitor (`text_monitor_port=`) is only needed for text-monitor extras (`attach_drive`/`detach_drive`, `screenshot_to_file`, the profiler) — warp/speed control works without it (hybrid: real `warp on`/`off` via the text monitor when connected, `Speed`-resource pseudo-warp over the binary monitor otherwise).
5. **CPU auto-pauses on every command** — the binary monitor pauses the CPU when any command is sent. You must explicitly `resume()` to let the CPU run. `resume()` is non-destructive and does not close the connection.
6. **`wait_for_text()` works with binary transport** — it calls `resume()` between polls internally, so no workaround is needed. Pass `verbose=False` — the default is `True`, which dumps the whole screen on every poll. It also resumes in a `finally`, so the CPU is running on **every** exit path — match, timeout or exception. Two caveats, both in the docstring: the exit resume is *owed*, not unconditional (it is skipped on the timeout path, where the loop's own resume was the last thing to touch the machine, so hardware is not charged a second `PUT /v1/machine:resume` and VICE does not bump its resume generation and drop a queued JAM event — issues #189/#190); and it is *best-effort* — a `resume()` that raises is logged at WARNING and swallowed rather than replacing your exception, so on a transport that cannot resume the CPU is not in fact running (issue #191). `ScreenGrid.from_transport()` does **not** resume; a poll loop built out of it never advances the C64 and a running machine is indistinguishable from a hung one.
7. **Use `jsr()` for direct-memory tests** — calls the subroutine and waits for the breakpoint via event-based `wait_for_stopped()`. No polling, no `poll_interval` parameter. VICE-only — for the cross-backend equivalent that works on U64 too, use `run_subroutine(target, addr, *, poll_cadence=...)` (PATTERNS § "Cross-backend run_subroutine for short routines"). `preserve_state=True` (default) keeps a call that lands mid-interrupt from abandoning the handler's frame; pass `False` where the call's own flag or stack effects must survive. A routine that may hang: `jsr(..., recover_on_timeout=True)` restores SP and re-proves the trampoline, raising `RoutineHung` (a `TimeoutError`) with `recovered` so the suite can record the hang and continue in the same boot (PATTERNS § "Probing a routine that may hang").
8. **Use `Labels.from_file()`** — never hardcode addresses; use label names from the assembler's output. `Labels` is a `collections.abc.Mapping[str, int]` (v0.12.4+), so `dict(labels)` and `for name, addr in labels.items()` just work.
9. **After `jsr()` returns, the CPU is paused at the breakpoint** — safe to read memory. By default `jsr()` also writes the pre-call `PC`/`SP`/`FL` back before returning (`preserve_state=True`), so the machine is on its *pre-call* register file while the returned dict is on the routine's. Read the return value, not the machine. Pass `preserve_state=False` when the routine's own `SEI`/`CLI` or its `LDX #$FF / TXS` must survive the call.
10. **No size limits on VICE** — the binary transport handles arbitrarily large reads and writes (4096+ bytes verified). `read_bytes()` chunks above 256 bytes and `write_bytes()` at 84 bytes there, leftovers from the removed text monitor that are harmless. On an Ultimate transport `write_bytes()` chunking is deliberate: it chunks at `transport.rest_put_chunk_size` so every chunk is a PUT (#252).
11. **Persistent connection means no transient failures** — unlike per-command TCP connections, the binary transport maintains a single persistent connection. No retry wrappers needed around `jsr()`.
12. **Build before testing** — always `make clean && make` and verify the PRG exists.
13. **Use `inst.pid` and `inst.port` from the ViceInstance** — never hardcode ports, never use `vice.pid` from ViceProcess directly.
14. **Never `pkill x64sc`** — use PID-targeted cleanup only; other agents may have VICE instances running.
15. **Probe before connecting to U64** — `probe_u64(host)` checks ping + TCP + REST API with short timeouts. `Ultimate64InstanceManager.acquire()` does this automatically, skipping unreachable devices. It is a *reachability* check only: it reports `reachable=True` on a device whose `writemem` path is dead (issue #241). `probe_u64(host, check_write=True)` adds a free PUT round trip (`result.write_ok`) that catches a dead memory-write path but not a POST-only degradation; it mutates RAM at `$0334`, so hold the DeviceLock; `result.scratch_restored` reports the restore; `liveness_probe()` is the POST check and costs two `/Temp` attachments per call on leak-prone firmware (issue #250).
16. **Pass `turbo_safe=True` to UCI helpers at U64 speeds ≥ 4 MHz** — the FPGA behind `$DF1C-$DF1F` needs ~38 µs between accesses; without the fence, turbo-speed code double-latches writes and corrupts the UCI protocol. Every `uci_*` builder and helper accepts the kwarg. Default is `False` for backward compat.

17. **`DebugCapture` is only cycle-accurate at 1 MHz** — the U64E FPGA emits the UDP debug stream at a fixed ~1.02M entries/sec (NTSC U64E, #432; consistent with one entry per NTSC phi2 cycle — a rate match, n=3, Debug Stream Mode presumed at default, not recorded) regardless of CPU turbo speed. At 1 MHz you get an essentially complete trace; at 4 MHz you get 1/4 of cycles, at 48 MHz ~1/48 (uniformly sampled; turbo speed itself adds no sequence gaps, because the rate limit is at the FPGA source). For complete traces, drop to 1 MHz for the capture window with `restore_speed_defaults(client)` (both speed items back to the firmware default; `set_turbo_mhz(client, 1)` also runs at 1 MHz but writes `Turbo Control = Manual`, which is off its default — #365). Turbo-speed capture is only sound for aggregate statistics (PC distribution, frequency maps); it is not sound for call-graph reconstruction, exact cycle counts, or sequential bus-state analysis. Measurement: `tests/test_u64_debug_stream_speed_live.py`. **`packets_dropped` is not zero in general** (#424) — that holds only for what the speed sweep measured; the bench's own loss ran 0.4-45% of packets per 1 s capture, placed at the host's Wi-Fi downlink, load-dependent, with a small device-side residual not excluded (#356). **The sustained-workload UDP-rate degradation behind `DebugCapture.with_fresh_fpga(client)` is unverified (#431)**: the same Wi-Fi downlink reproduces its signature without any FPGA involvement, nobody recorded which host path the original runs used, and there has been no wired-host reproduction. `client.reboot()` was observed to restore delivery where a soft `reset()` did not — a remedy that helped, not a diagnosis.
18. **Ethernet bridge tests default to RR-Net mode** — `ViceConfig.ethernet_mode="rrnet"` matches ip65 and the physical RR-Net cart; `"tfe"` remains selectable. Always use the `bridge_vice_pair` fixture and `run_ping_and_wait` / `run_icmp_responder` orchestrators, which work in BOTH VICE normal and warp modes (VICE only — they `jsr()`; for a real RR-Net cartridge on the U64 see PATTERNS § "Hardware RR-Net on the U64"). For the `ethernet_interface` / `ethernet_driver` values, import from `tests/bridge_platform.py` (`IFACE_A`, `IFACE_B`, `ETHERNET_DRIVER`) — do not hardcode. On macOS, `ViceConfig.run_as_root` auto-resolves to True when `ethernet=True` and `ViceProcess` wraps the launch with `sudo -n`; broadcast-TX-then-RX-on-same-transport tests must drain the CS8900a RX FIFO (see `_drain_cs8900a_rx` in `tests/test_ethernet_bridge.py`) because libpcap self-delivers the sender's own frames on BPF.
19. **VICE TOD ≠ wall-clock** — VICE 3.10 CIA TOD is virtual-CPU-clocked (warp accelerates it ~31×). For code that must work in VICE warp, use the host-driven `run_ping_and_wait` / `poll_until_ready` orchestrators. For shippable pure-6502 code on real C64 / U64 / VICE normal, use `tod_timer.build_*` helpers.

20. **Use the public `target.client` / `transport.client` accessors** for U64 low-level operations not yet wrapped on the transport — e.g. `client.run_prg`, `client.mount_disk`, `client.reset`, `client.reboot`, `client.send_text`, `client.sid_play`. Never reach `target.transport._client` or `transport._client`; those private attrs are internal and may be renamed without notice.

21. **`lock_timeout` bounds against wedged/dead holders only — not healthy long-running peers.** `DeviceLock` heartbeats the lockfile mtime every ~15 s while held, and `acquire(progress_window=60.0)` (the default) extends a waiter's deadline indefinitely as long as the holder PID is alive AND the lockfile mtime is fresh. So a peer running a multi-hour suite does not time you out. Raising `lock_timeout` does not help you queue behind a healthy holder (its heartbeat already extends your deadline); it only lengthens how long you wait on a wedged or dead one. Use `lock.acquire_or_raise(timeout=...)` (and `_LockedU64Manager.acquire()` via `create_manager`) to surface a structured `DeviceLockTimeout` with `holder_pid`, `pid_alive`, `lockfile_age_seconds`, `device_reachable_rest`, and a diagnosed-state message ("queued behind live, progressing PID X" / "holder PID X is alive but the lockfile hasn't been touched in Ns" / "stale lock from dead PID X" / "no holder metadata found", plus a REST-reachability suffix). `DeviceLockTimeout` is exported from the top-level package and is a `TimeoutError` subclass. Pass `progress_window=None` to opt out of queue-aware behavior (legacy hard timeout). Never reboot the U64 in response to a `DeviceLockTimeout` without first checking `pid_alive` and `device_reachable_rest` — see PATTERNS § "Pattern 9a".

21a. **An unlocked `Ultimate64Client` now says so, once per process.** Constructing one while this process holds no `DeviceLock` for that host logs a WARNING naming the lockfile. It is a notice, not a refusal — the client still works — but if you see it in your own lane, you are the careless lane of issue #194: `run_prg` is a load-and-run that **resets the machine and replaces whatever program is on it**, so an unlocked one destroys a neighbouring lane's run and presents as device degradation. Silence it only when you mean it (`Ultimate64Client(host, warn_unlocked=False)`, `U64_UNLOCKED_CLIENT_WARNING=0`, or the thread-scoped `suppress_unlocked_warning()`); `create_manager` already suppresses it around the one construction that legitimately precedes the lock. To ask who holds a device **right now** without importing the manager machinery, use `device_lock_holder(host)` — **not** `read_info()`, which names whoever held it *last* and keeps naming a dead PID after every completed run, because `release()` deliberately does not unlink the lockfile. `device_lock_path(host)` gives the path. See `docs/device_locking.md`.

21b. **The acquire budget is `U64_DEVICE_LOCK_TIMEOUT` — a budget, not a gate.** Where a caller passes no timeout, the variable supplies it, read at call time; unset, `DeviceLock.acquire()` waits 30 s (`DEFAULT_ACQUIRE_TIMEOUT`) and `create_manager()` 60 s (`unified_manager.DEFAULT_LOCK_TIMEOUT`). An explicit `timeout=` / `lock_timeout=` wins. It changes how long a wait may last, never whether anything runs, so it is the knob for a longer queue without editing code; a live, progressing holder still extends the deadline regardless. A malformed, zero/negative or non-finite value raises `DeviceLockTimeoutConfigError` (a `ValueError` in `backends.device_lock`, not a package-root export — so `except TimeoutError` does not swallow a typo). A blocked acquire is not silent: it logs a periodic progress line (holder PID, lockfile age, queue depth; `STALE, holder may be wedged` only when acquire is not extending) and `acquire(..., on_wait=cb)` / `acquire_or_raise(..., on_wait=cb)` calls `cb(elapsed, holder_pid, lockfile_age, queue_depth)` from the waiting thread; an exception from `cb` abandons the wait. Another thread can rescue a wait on a lock this thread already holds only within the 2.0 s grace (`_SELF_HELD_WAIT_GRACE`): a longer timeout is capped with a WARNING, and a later release is too late (`acquire()` returns `False`). For one owner re-entering, pass `allow_nested=True` instead. See `docs/device_locking.md` § "The acquire budget", § "Seeing the wait" and § "Rescuing a self-held wait from another thread".

22. **For U64 unresponsive scenarios, know `recover(client)`'s blind spot before reaching for it.** `recover()` (in `ultimate64_helpers`) escalates `reset()` → probe → `reboot()` → probe — but its liveness probe is **REST-only**, so for wedges below REST (FPGA / REU / DMA / UCI state, where REST typically stays healthy) it returns `"reset"` without fixing anything. For FPGA-tier symptoms call `client.reboot()` directly. The UCI STATE-bit wedge (issue #112) survives even `reboot()` — fail fast and require a physical power-cycle. Never call `poweroff()` for recovery: it disconnects the device until manual physical power-cycle. Use `runner_health_check(client)` to detect the firmware's "Cannot open file" wedged-runner state before resorting to recovery. See PATTERNS § "Recovering a stuck Ultimate 64".

23. **`MemoryPolicy` guards every host→C64 `write_memory()` at the transport boundary.** The harness has fixed scratch addresses (authoritative list: `HARNESS_SCRATCH` in `memory_policy.py`, rendered into `docs/memory_safety.md` by `scripts/gen_memory_table.py`; highlights: `$0334` jsr trampoline, `$0360`+`$03F0`-`$03F1` `run_subroutine`, `$0277`/`$00C6` keyboard buffer, `$C000-$C3FF` UCI block, `$C400-$CAC1` UCI socket-write and multi-block read scratch, `$C000`+`$0339`+`$033C` SID player, `$CF00` test-suite BASIC-restore stub); if the consumer's PRG occupies those, host writes silently corrupt RAM (6502 has no MMU). The default policy is permissive (no behaviour change for existing tests). Opt in by passing `target.transport.memory_policy = MemoryPolicy.from_prg(prg)` (cheapest signal — reserves the PRG load span), or via `HarnessConfig.from_toml(...).memory_policy` + `UnifiedManager(..., memory_policy=cfg.memory_policy)`. Violations raise `MemoryPolicyError` before any byte crosses the wire; per-call `write_memory(..., override="reason")` bypasses for one call (logged at WARNING). `MemoryArbiter` is the allocator helper — ask it for scratch addresses and they're guaranteed to pass `check_write`, but the arbiter is NOT the safety mechanism (the policy on the transport is); by default it also withholds every non-transient `HARNESS_SCRATCH` entry (`exclude_harness_scratch=False` opts out, `is_free(addr)` queries). See PATTERNS § "Pattern 12" and `docs/memory_safety.md`.

24. **If `read_bytes()` returns surprising bytes that the C64-side math says it can't be, suspect the VICE binary monitor protocol, not your 6502.** PR #88 tightened the binary read path to validate `response_type` against `CMD_TO_RESPONSE_TYPE` — a wire-level desync now raises `TransportError` naming both expected and actual response types. As a diagnostic, use `read_bytes_verified(transport, addr, length, *, max_attempts=2)`: re-reads on disagreement and raises `FlakeyReadError` with all attempts captured. Standard `read_bytes()` everywhere else — `read_bytes_verified()` doubles wire traffic and is only worth it when a flake is actively suspected.

25. **For cross-backend CPU speed and reset control, use `target.transport.set_speed(...)` / `target.transport.reset(scope=...)` — not backend-branching.** Both are on the `C64Transport` protocol (PR #122). `set_speed(1)` is "1 MHz / warp off / turbo off"; `set_speed(None)` is "max speed" — VICE warp on / U64 the device's probed maximum (64 MHz on a C64 Ultimate, 48 on a U64E; 48 fallback when the preset probe is inconclusive). `reset(scope="cpu")` is a soft 6510 reset; `reset(scope="machine")` is the backend's fullest reset — loose on purpose: VICE hard reset, but on U64 a C64-level `reboot()` that leaves the firmware running, ~8 s settle; `reset(scope="drive", drive=...)` resets a specific drive (0..3 on VICE, "a"/"b" on U64). VICE raises `NotImplementedError` for any `set_speed` multiplier other than `1`/`None` (no native discrete CPU-speed steps); U64 accepts the cross-generation superset 2/3/4/5/6/8/10/12/14/16/20/24/32/40/48/64 (the U64E lacks 64 on both 3.14 and the bench's 3.15 builds; C64 Ultimate fw 1.1.0 lacks 5; the device's CPU-Speed presets are probed once per client and a generation-foreign speed raises `ValueError` locally — only when the probe is inconclusive does it reach the firmware, which rejects it with HTTP 400 before turbo is enabled). The legacy `set_turbo_mhz(client, mhz)` / `client.reset()` / `client.reboot()` calls still work and remain appropriate when you hold a raw `Ultimate64Client` rather than a transport. See PATTERNS § "Pattern 10 / Cross-backend speed/reset variant" and § "Gotcha 15".

26. **`watch_progress(transport, addresses=...)` is backend-agnostic** — it reads through `C64Transport.read_memory` (PR #123); import it as `from c64_test_harness import watch_progress, ProgressEvent`. Use it instead of hand-rolled `time.monotonic()` polling loops when watching a sentinel or progress counter; the generator emits `Advanced` / `Stalled` / `Finished` / `Timeout` / `PollError` events (default `poll_interval` is 10 s — set it) with elapsed timing and changed-region diffs. The legacy `from c64_test_harness.backends.ultimate64_helpers import watch_progress` path is preserved as a backwards-compat shim — new code should use the top-level import.

27. **Two Ultimate hardware generations exist — detect via `client.get_info()["product"]`, never assume.** `"Ultimate 64 Elite"` (fw 3.14/3.15) vs `"C64 Ultimate"` (fw 1.1.0). Live-verified asymmetries: CPU-speed enum (Elite has `" 5"` not `"64"`, C64U the reverse; foreign speeds raise `ValueError` locally via a cached preset probe, with the firmware's HTTP 400 as backstop when the probe is inconclusive), Cartridge presets (only U64E 3.14 had a `"REU"` preset; U64E 3.15 made `Cartridge` a `.crt` chooser and the C64U has no `"REU"` preset either — `set_reu`/`restore_state` probe and adapt; don't hand-write that config item), and the C64U's REST `POST writemem` degrading to ~6 s/request at ≥16 KiB. For bulk writes use `write_bytes` / `run_prg_via_sys` (chunked at the client threshold, capped at 128, so a PUT on every grade; #252) — **do not enable the SocketDMA write fast path (`transport.socket_dma`); it is disabled pending a stability review**. See PATTERNS § "Pattern 10 / Two device generations" and § "SocketDMA write fast path".

## Test File Template

```python
#!/usr/bin/env python3
"""test_<name>_direct.py — Direct-memory <Name> tests.

Usage:
    python3 tools/test_<name>_direct.py [--iterations N] [--seed S]
"""

import os
import random
import subprocess
import sys

from c64_test_harness import (
    Labels, ViceConfig, ViceInstanceManager,
    read_bytes, write_bytes, jsr, wait_for_text,
)


PROJECT_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
PRG_PATH = os.path.join(PROJECT_ROOT, "build", "<name>.prg")
LABELS_PATH = os.path.join(PROJECT_ROOT, "build", "labels.txt")


def run_tests(transport, labels, iterations):
    passed = failed = 0
    # ... test logic here ...
    return passed, failed


def main():
    os.chdir(PROJECT_ROOT)

    iterations = 10
    seed = random.randint(0, 2**32 - 1)
    # ... parse args ...
    random.seed(seed)
    print(f"Random seed: {seed} (reproduce with --seed {seed})")

    # Build
    if not os.environ.get("C64_SKIP_BUILD"):
        subprocess.run(["make", "clean"], capture_output=True)
        result = subprocess.run(["make"], capture_output=True, text=True)
        if result.returncode != 0:
            print(f"Build failed:\n{result.stderr}")
            sys.exit(1)

    labels = Labels.from_file(LABELS_PATH)
    # Verify required labels exist
    for name in ["label1", "label2"]:
        if labels.address(name) is None:
            print(f"FATAL: '{name}' label not found")
            sys.exit(1)

    config = ViceConfig(prg_path=PRG_PATH, warp=True, ntsc=True, sound=False)

    with ViceInstanceManager(config=config) as mgr:
        inst = mgr.acquire()
        print(f"VICE PID={inst.pid}, port={inst.port}")

        transport = inst.transport
        grid = wait_for_text(transport, "Q=QUIT", timeout=60.0, verbose=False)
        if grid is None:
            print("FATAL: Main menu did not appear")
            sys.exit(1)

        # Safety: write JMP $0339 at $0339 so CPU loops harmlessly
        # after jsr() returns (prevents crash when BASIC ROM is banked out)
        write_bytes(transport, 0x0339, bytes([0x4C, 0x39, 0x03]))

        passed, failed = run_tests(transport, labels, iterations)

        mgr.release(inst)

    total = passed + failed
    print(f"\nResults: {passed}/{total} passed")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
```

## Backend-Agnostic Test Template (VICE or U64)

```python
#!/usr/bin/env python3
"""test_<name>.py — Backend-agnostic <Name> tests.

Works on both VICE and Ultimate 64. Set C64_BACKEND=u64 and U64_HOST=... for hardware.
"""

import os
import sys

from c64_test_harness import (
    create_manager, read_bytes, write_bytes, wait_for_text,
)


def run_tests(transport, backend):
    passed = failed = 0

    # Memory round-trip (works on both backends)
    write_bytes(transport, 0xC100, bytes([0xDE, 0xAD, 0xBE, 0xEF]))
    result = read_bytes(transport, 0xC100, 4)
    assert result == bytes([0xDE, 0xAD, 0xBE, 0xEF])
    passed += 1

    # Screen read (works on both)
    codes = transport.read_screen_codes()
    assert len(codes) == 1000
    passed += 1

    # VICE-only features (jsr, breakpoints, registers)
    if backend == "vice":
        from c64_test_harness import jsr, Labels
        # ... jsr-based tests ...
        pass

    return passed, failed


def main():
    # create_manager() reads C64_BACKEND and U64_HOST from env
    with create_manager() as mgr:
        with mgr.instance() as target:
            print(f"Backend: {target.backend}, PID: {target.pid}")
            passed, failed = run_tests(target.transport, target.backend)

    print(f"\nResults: {passed}/{passed + failed} passed")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
```

## U64 Pytest Fixture Pattern (with DeviceLock)

All U64 live test files MUST use DeviceLock in their fixtures:

```python
import os
import pytest
from c64_test_harness import DeviceLock, DeviceLockTimeout
from c64_test_harness.backends.ultimate64 import Ultimate64Transport

_HOST = os.environ.get("U64_HOST")
_PW = os.environ.get("U64_PASSWORD")

pytestmark = pytest.mark.skipif(not _HOST, reason="U64_HOST not set")

@pytest.fixture(scope="module")
def transport():
    # allow_nested=True joins a hold this process already has (in this repo,
    # tests/conftest.py's device_lock_guard holds the lock around every
    # *_live.py test); without it a second DeviceLock queues behind it (#273).
    lock = DeviceLock(_HOST, allow_nested=True)
    try:
        lock.acquire_or_raise(timeout=120.0)
    except DeviceLockTimeout as e:
        # str(e) is a diagnosed-state message: queued vs wedged vs stale vs
        # unreachable. See PATTERNS Pattern 9a.
        pytest.skip(str(e))
    t = None
    try:
        t = Ultimate64Transport(host=_HOST, password=_PW, timeout=8.0)
        yield t
    finally:
        try:
            if t is not None:
                t.close()
        finally:
            lock.release()  # last, even if close() raised
```

Inside this repo, live fixtures use `tests/live_fixture_teardown.py` instead of hand-nesting: `failures = teardown_then_release(steps, lock.release)` in the `finally`, then `raise_teardown_failures(what, failures)` after it (every step attempted, lock released last, failures raised without masking a test exception — #334). A bare post-`yield` teardown in a `*_live.py` module is refused by `tests/test_live_fixture_teardowns.py`.

**Reconcile the device at entry, don't rely on the last run's teardown.** Inside the lock, before the tests get the transport, put the device at a known baseline with `apply_factory_baseline()` and then set only what the module needs; restore-on-exit is a courtesy, since no `finally` runs on a SIGKILL (issue #276, a real incident that left the bench at CPU Speed 8). Never reach for the raw `configs:reset_to_default` — it touches stores that cut SID socket power or drop the device's DHCP lease mid-request. **A `create_manager(backend="u64")` lane already does this and takes no argument for it**: since #285 the reset resolves from the device's generation at `acquire()` — on for the U64E, off for the C64U, off for an unreadable generation — with `U64_BASELINE_ON_ENTRY=1`/`=0` overriding either way. Pattern, reasons and a worked fixture: PATTERNS § "Known state on entry — reset, then set only what you need".

## Critical Gotchas

Read `PATTERNS.md` for the full list. The most common mistakes:

1. **Never use `ViceProcess` directly or `PortAllocator` manually** — always use `ViceInstanceManager`. It handles port allocation (cross-process safe via OS-level `bind()` + file-based `flock()` locks), PID tracking, transport creation, and cleanup. Without it, parallel Claude agents will collide on ports and kill each other's VICE instances.

2. **PETSCII filename case**: Use **lowercase** `c64_name` in `DiskImage.write_file()` so filenames match what the C64 keyboard generates (unshifted PETSCII $41-$5A).

3. **After direct jsr() tests, the program's RAM state is whatever the routines left** — `jsr()` restores `PC`/`SP`/`FL` by default (`preserve_state=True`), so the CPU resumes the interrupted instruction stream, but `A`/`X`/`Y` and every byte the routines wrote are not restored (and nothing is restored on timeout). To cross-validate with the UI from a known state, restart with `send_text(transport, "RUN")` + `send_key(transport, "\r")`.

4. **`jsr()` is event-based, not polling** — it uses `wait_for_stopped()` internally. There is no `poll_interval` parameter. The binary monitor pushes async breakpoint events when a checkpoint fires.

5. **SEQ file reading requires SA >= 2** — SETLFS secondary address 0 is LOAD mode, not sequential read.

6. **Use `inst.pid` for PID, `inst.port` for port, `inst.transport` for transport** — these come from the `ViceInstance` returned by `mgr.acquire()`. Never construct transports manually.

7. **`resume()` is safe to call repeatedly** — it does not close the connection. It simply resumes CPU execution. The persistent connection remains open throughout the test session.

8. **All U64 access must use DeviceLock** — creating `Ultimate64Transport` without a `DeviceLock` will cause test failures when another agent uses the same device concurrently. Use `UnifiedManager` (automatic locking) or wrap with `DeviceLock` in fixtures. Prefer `lock.acquire_or_raise(timeout=...)` over `lock.acquire(...)` so a timeout surfaces as a structured `DeviceLockTimeout` (with `holder_pid` / `pid_alive` / `lockfile_age_seconds` / `device_reachable_rest` and a diagnosed-state message) instead of a bare `False`. Never reboot the U64 just because acquire timed out — see PATTERNS § "Pattern 9a".

9. **Probe U64 before connecting** — `probe_u64(host)` checks ping + TCP + API. `is_u64_reachable(host)` for a quick boolean. `Ultimate64InstanceManager.acquire()` probes automatically and skips unreachable devices.

10. **UCI networking needs `turbo_safe=True` at ≥ 4 MHz** — without it, tests hang in the 6502-side wait for the UCI to go idle or return error 0x85 at 48 MHz. Scripts `scripts/probe_uci_network.py` / `tests/test_uci_tcp_echo_live.py` hand-write 6502 outside the builders and run at 1 MHz only.

11. **Ethernet: use RR-Net, not TFE** — `ViceConfig.ethernet_mode` defaults to `"rrnet"` (as ip65 and c64-https do); TX-after-RX and a full ICMP round-trip fail without the RR-Net clockport enable at `$DE01` bit 0.

12. **TOD footprint is `$F0`-`$F5`** — keep other zero-page use out of it. `bridge_ping`'s frame reader no longer touches `$F1-$F4` (issue #208), so frame reads inside a TOD loop are safe; the counter-timed builders (`build_rx_peek_code` and the non-TOD ping/responder routines) still use `$F0-$F2` as their own poll counters, so do not nest one inside a TOD loop.
13. **Hardware RR-Net on the U64 is not the VICE bridge** — set `Cartridge Preference = External` (`client.set_config_item(CARTRIDGE_SETTINGS_CATEGORY, CARTRIDGE_PREFERENCE_ITEM, "External")`; the constants are package-root exports since #221, and `snapshot_state`/`restore_state` carry the item), start PRGs with `run_prg_via_sys` (never `client.run_prg`/`load_prg`: the firmware's load path deselects the cartridge stickily across resets until the preference is re-PUT, which the helper does — #217), program the MAC from the 6510 with `cs8900a_set_mac_inline_code` (`set_cs8900a_mac` works under VICE and is useless on hardware), resolve before the first ping (`arp_frame_buf=` on the ping builders, `my_mac=` on the responders — #218), and use the `*_tod_code` builders via `run_subroutine` — `run_ping_and_wait`/`run_icmp_responder` need `jsr()` and are VICE-only. PATTERNS § "Hardware RR-Net on the U64".
14. **Measure chip registers on the 6510, on both backends — for different reasons.** Under VICE the host path is faithful (reads return the chip, writes reach it) but the monitor services commands once per frame from its vsync hook, so every read of a running machine lands at the same frame phase and a polled `$D012` never moves; on U64 hardware the host path never reaches the expansion port and window reads are neither meaningful nor reproducible.
15. **UCI needs `Cartridge Preference = Auto`; RR-Net (item 13) sets External.** With `Cartridge Preference` = External, or an external cartridge holding the bus (source-read, `c64.cc` `ConfigureU64SystemBus`: under Auto only a cartridge the detect lines see; #359's RR-Net under Auto did not), the Command Interface slot stays off the C64 bus after `enable_uci` + `reset()` + settle, even though `Command Interface` still reads Enabled. A config read-back cannot see it; `$DF1D` can. Measured on the U64E in #359 (fw 3.15, paired ABBAAB, n=3 per arm): Auto gave identifier `$C9` and a completed routine 3/3; External gave neither, 0/3. Not measured on the C64U. Every UCI routine used to time out at the sentinel; since PR #394 it raises `UCIInterfaceAbsentError` before writing anything, naming the preference. The harness does not change the preference for you. Remedy: `client.set_config_item(CARTRIDGE_SETTINGS_CATEGORY, CARTRIDGE_PREFERENCE_ITEM, "Auto")`, then `reset()` and a ~3 s settle before the first routine: the sequence #359 measured. By firmware source (bce4535e, `c64.cc` `setCartPref` → `effectuate_settings` → `ConfigureU64SystemBus`) the preference PUT alone puts the slot back on the bus — unmeasured; the reset is a precaution, not a known requirement (compare #270/#409 for the enable). So **RR-Net runs and UCI runs cannot share a device session without putting the preference back to Auto** (`restore_state` does it when the snapshot was taken on Auto). Passing the check proves only that the slot is on the bus: a `$C9` at `$DF1D` does not rule out a UCI STATE-bit wedge, which lives in `$DF1C` (sample it with `uci_wedge_probe`; `reset()`/`reboot()` do not clear it).

If the user provides `$ARGUMENTS`, focus the test writing on that subject area. Otherwise, ask what assembly routine or feature they want to test.
