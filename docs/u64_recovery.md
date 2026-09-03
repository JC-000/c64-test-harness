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

This whole wedge family (issues #112, #129, #137) was root-caused after
the tiers below were first characterised, and the mitigations here are
**temporary**. The underlying cause is firmware Temp-folder accumulation:
`POST /v1/machine:writemem` uploads arrive as multipart attachments that
land in Temp, and without garbage collection the accumulation produces
the latency drift and eventual wedge described in every tier below.

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
body — `writemem`, `load_prg`, keyboard-inject) leaks a managed attachment
(`temp0000`, `temp0001`, ...) per call. Ultimate-line 3.15 collects them
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

`Ultimate64Client.run_prg` calls this automatically before uploading
when the `U64_AUTO_TEMP_GC` env var is set (off by default — unit tests
that construct a client against a fake host never make a real network
call). Knobs: `U64_TEMP_GC_KEEP` (keep-count override) and
`U64_TEMP_GC_FTP_USER` / `U64_TEMP_GC_FTP_PASSWORD` (bench devices run
anonymous FTP; override for a device with FTP credentials configured).
Call `client.gc_temp_folder()` (or the module function) directly for
other attachment-heavy paths, or to run a manual pass regardless of the
env var. On firmware carrying #686 (Ultimate-line ≥ 3.15) this is a
no-op that finds nothing to delete — verified on the U64E 2026-09-02 —
so it matters only for the C64U until its firmware catches up.

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
write, so a reboot both reverts it and empties `/Temp` (the revert is
benign).

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

`liveness_probe` issues exactly one writemem POST and tags the failure
mode (`"writemem_404"`, `"writemem_timeout"`, `"tcp_stack_wedged"`,
`"connection_reset"`, `"unreachable"`, `"unknown"`). Do not retry the
probe in a tight loop — repeated POSTs against a degraded endpoint are
the documented TCP-wedge trigger.

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
pass through unchanged. The escalation is `client.reboot()` (full FPGA
reinit, ~8 s); `client.reset()` is insufficient.

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

```python
from c64_test_harness import (
    Ultimate64Client,
    liveness_probe,
    runner_health_check,
    uci_wedge_probe,
    Ultimate64RunnerStuckError,
)

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
wire. Does not reinitialise the FPGA. Does not clear writemem-degraded
state on its own. Does not clear UCI STATE-bit wedges.

`reboot()` — `PUT /v1/machine:reboot`. Full FPGA reinit; ~8 s; over the
wire. Recovers REU/DMA stuck state and clears most runner-tier wedges.
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
FPGA-state issues that ARE reboot-clearable, `client.reboot()` is the
right call. Multiple agents have called `poweroff()` thinking it was a
benign reset, then mis-diagnosed the unreachable state as a "hung
device" — wasting troubleshooting cycles each time.

## When power-cycle is the only option

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

## Cross-references

- Issue [#112](https://github.com/JC-000/c64-test-harness/issues/112) — UCI STATE-bit wedge after sustained `SOCKET_WRITE`
- [`docs/uci_networking.md`](uci_networking.md) — UCI command interface, `$DF1C` STATE bits, send-size constraints
- [`docs/bridge_networking.md`](bridge_networking.md) — VICE-side ethernet pathways (separate from U64 recovery, but adjacent when porting consumers across backends)
- [`src/c64_test_harness/backends/ultimate64_probe.py`](../src/c64_test_harness/backends/ultimate64_probe.py) — `liveness_probe`, `probe_u64`, `LivenessResult`
- [`src/c64_test_harness/backends/ultimate64_helpers.py`](../src/c64_test_harness/backends/ultimate64_helpers.py) — `recover`, `runner_health_check`
- [`src/c64_test_harness/backends/ultimate64_client.py`](../src/c64_test_harness/backends/ultimate64_client.py) — `reset`, `reboot`, `poweroff`, `Ultimate64RunnerStuckError`, `Ultimate64UnsafeOperationError`, `Ultimate64UnreachableError`
- [`src/c64_test_harness/uci_network.py`](../src/c64_test_harness/uci_network.py) — `uci_wedge_probe`, `UCI_CONTROL_STATUS_REG` (`$DF1C`), STATE-bit masks
- Issue [#153](https://github.com/JC-000/c64-test-harness/issues/153) — automatic FTP `/Temp` GC to defuse the writemem-exhaustion wedge before it starts
- [`src/c64_test_harness/backends/ultimate64_temp_gc.py`](../src/c64_test_harness/backends/ultimate64_temp_gc.py) — `gc_temp_folder`, `TempGCResult`, `auto_gc_enabled`
