# Memory safety: the `MemoryPolicy` write guard

## What this solves

The 6502 has no MMU and no segfault.  When the harness uploads a DMA
stub via `transport.write_memory()`, the C64 happily accepts the write
even if it lands inside a region the consumer's loaded program is also
using.  Both writes succeed, the last one wins, and any subsequent read
of the clobbered bytes returns wrong-but-deterministic results.  Tests
fail on a downstream symptom (cryptographic mismatch, garbled screen,
timing drift) and the bisection ends up nowhere near the actual root
cause.

`MemoryPolicy` is the safety net.  It lives at the transport boundary,
inspects every `write_memory()` call before any byte crosses the wire,
and surfaces collisions as `MemoryPolicyError` with the offending
region's name attached.  See harness issue #93 and the c64-https Phase
C.5 incident (~12 hours bisecting a silent collision at `$4200-$50FF`)
for the failure shape this is designed to catch.

## The invariant

> Every host-originated write to C64 RAM is either (a) inside a region
> the consumer has declared safe for harness use, or (b) accompanied by
> an explicit per-call override that says "I know what I'm doing".

The policy is **allow-list** at heart, with a tri-state for "unknown"
addresses (neither declared safe nor reserved):

| `unknown_policy` | Meaning | When to use |
|---|---|---|
| `allow` | Pass.  Migration default. | No declarations yet — same behaviour as pre-policy. |
| `warn` | Emit `UserWarning`, pass. | Stable layout, want visibility on stray writes. |
| `deny` | Raise `MemoryPolicyError`. | Locked-down test suites where every write should be deliberate. |

A write that overlaps any `reserved_region` always raises, regardless
of `unknown_policy`.

Separately from the region logic, a write whose span runs past the top
of the 16-bit address space (`addr + length > $10000`) is rejected with
`ValueError` — by `check_write` itself *and* by the transports'
`read_memory`/`write_memory`.  Previously such a span silently wrapped
back to `$0000` on the wire, which could clobber page zero after the
policy had approved the un-wrapped range.  `override=` does **not**
bypass this check; split the access at `$FFFF`.

## Three ways to construct a policy

### From a PRG file (cheapest accurate signal)

```python
from c64_test_harness import MemoryPolicy, UnknownPolicy
from c64_test_harness.verify import PrgFile

prg = PrgFile.from_file("build/program.prg")
policy = MemoryPolicy.from_prg(prg, unknown=UnknownPolicy.WARN)
target.transport.memory_policy = policy
```

This auto-reserves the PRG's load span (`[load_address, end_address)`)
as a reserved region.  Cheap, zero-effort, and catches the most common
collision class.  Doesn't catch BSS or runtime tables that aren't in
the load image — see "What this doesn't solve" below.  If the load
span overlaps one of the harness's own scratch addresses (table below)
the constructor emits a `UserWarning` naming the entry and the kwarg
that moves it.

### From a TOML config

```toml
# c64test.toml

[memory]
prg = "build/program.prg"            # auto-reserves load span
unknown_policy = "deny"

safe_regions = [
    { range = "$0334-$03FB", note = "cassette buffer (default harness scratch)" },
    { range = "$C000-$CFFF", note = "harness scratch page (superset of every $Cxxx entry in the table)" },
]

reserved_regions = [
    { range = "$4200-$50FF", note = "X25519 RODATA + BSS" },
    { range = "$A000-$BFFF", note = "SHADOW_BSS under BASIC ROM" },
]
```

`HarnessConfig.from_toml(path)` parses the `[memory]` section into
`cfg.memory_policy`.  Pass `memory_policy=cfg.memory_policy` to
`UnifiedManager` and every acquired transport receives the policy:

```python
cfg = HarnessConfig.from_toml("c64test.toml")
with UnifiedManager(backend="vice", memory_policy=cfg.memory_policy) as mgr:
    with mgr.instance() as target:
        # target.transport.memory_policy is set
        ...
```

### Programmatically

```python
from c64_test_harness import MemoryPolicy, MemoryRegion, UnknownPolicy

policy = (
    MemoryPolicy.permissive()
    .with_reserved(MemoryRegion.parse("$4200-$50FF", note="X25519"))
    .with_safe(MemoryRegion.parse("$C000-$CFFF", note="scratch"))
    .with_unknown(UnknownPolicy.WARN)
)
target.transport.memory_policy = policy
```

`MemoryPolicy` is frozen — every `with_*` returns a new instance.

## What happens on a collision

```python
target.transport.write_memory(0x4200, b"\xAA\xBB")
# MemoryPolicyError: write_memory($4200, 2 B) → $4201 blocked: overlaps
# reserved region $4200-$50FF (X25519 RODATA + BSS). Pass override="<reason>"
# to bypass for a single call, or update the harness MemoryPolicy.
```

No byte crosses the wire.  The exception names the offending address,
the violated region, and the bypass mechanism.

## The override escape hatch

A small number of tests legitimately need to clobber memory — fault
injection, "what happens if the data is wrong" coverage, etc.  Pass
`override="<reason>"` for one call:

```python
target.transport.write_memory(
    0x4200, corrupt_payload, override="fault-injection: corrupt X25519 RODATA"
)
```

The bypass is logged at `WARNING` level with the reason string.  Empty
or `None` overrides do not count.

## The harness's own scratch addresses

These are the fixed C64 RAM addresses the harness writes to as part of
normal operation.  If your consumer program uses any of them, either
move the harness (the *Configurable* column names the kwarg or
constant), declare the span as a `reserved_region` and expect the
policy to fire, or use `MemoryArbiter`, which withholds every
non-transient entry below by default.

The table is **generated** from `c64_test_harness.HARNESS_SCRATCH`
(`src/c64_test_harness/memory_policy.py`) by
`scripts/gen_memory_table.py`, and `tests/test_memory_table_doc.py`
fails the suite if the two disagree.  Edit the Python list, then run
`scripts/gen_memory_table.py --write` — never the markdown.  Each
entry was verified against the code that performs the write (the
*Owner* column); where an older version of this table disagreed with
the code, the code won (issue #169).

<!-- BEGIN HARNESS_SCRATCH TABLE (generated by scripts/gen_memory_table.py — do not edit by hand) -->
| Address(es) | Bytes | Owner | Purpose | Configurable |
|---|---:|---|---|---|
| `$0000-$0001` | 2 | `snapshot.restore_snapshot` | 6510 CPU port direction/data re-asserted after the RAM restore (override="snapshot-restore") | restore-time only; carries override= |
| `$00C6` | 1 | `uci_network._execute_uci_routine`, `backends.ultimate64.Ultimate64Transport.inject_keys`, `backends.ultimate64_client.Ultimate64Client.send_text` | KERNAL NDX — keyboard-buffer fill count set after a SYS/text injection | KERNAL-mandated (keybuf_count_addr= on U64 transport) |
| `$0277-$0280` | 10 | `uci_network._execute_uci_routine`, `backends.ultimate64.Ultimate64Transport.inject_keys`, `backends.ultimate64_client.Ultimate64Client.send_text` | KERNAL KEYD — 10-byte keyboard buffer receiving "SYS<addr>\r" or injected text | KERNAL-mandated (keybuf_addr= on U64 transport) |
| `$0314-$0315` | 2 | `sid_player.stop_sid_vice` | RAM IRQ vector (CINV) restored to $EA31; the installer stub also patches it from 6502 code | hardcoded |
| `$0334-$0338` | 5 | `execute.jsr` | JSR addr / NOP / NOP trampoline; checkpoint at +3 | scratch_addr= |
| `$0334-$03B3` † | 128 | `backends.ultimate64_probe.liveness_probe` | 128-byte writemem POST round-trip payload via the raw REST client (bypasses the transport MemoryPolicy); original bytes written back on success only — the readback-failure branches leave the pattern in place | hardcoded |
| `$0339-$033B` | 3 | `sid_player.play_sid_vice` | park JMP ($A002) executed after the installer so resume() lands in BASIC warm start | _PARK_ADDR constant |
| `$033C-$0341` | 6 | `sid_player.play_sid_vice` | song trampoline: LDA #song / JSR init / RTS | _SONG_TRAMPOLINE_ADDR constant |
| `$0360-$036D` | 14 | `execute.run_subroutine (U64 path)` | 14-byte sentinel trampoline; on VICE the 5-byte jsr() trampoline is written here instead | trampoline_addr= |
| `$03F0-$03F1` | 2 | `execute.run_subroutine (U64 path)` | running / done flag bytes polled by the host | hardcoded |
| `$0800-$87FF` † | 32768 | `snapshot.extract_reu_contents` | 32 KiB REU→C64 DMA staging window (opt-in include_reu=True, override="reu-snapshot-staging"). Filled by REC DMA with the CPU running (unpaused is mandatory on hardware) — MemoryPolicy cannot see the fill; prior contents written back afterwards, but code executing there meanwhile runs REU data | hardcoded |
| `$C000-$C011` | 18 | `sid_player.play_sid_vice` | 18-byte IRQ installer + wrapper stub | stub_addr= (DEFAULT_STUB_ADDR) |
| `$C000-$C03F` | 64 | `bridge_ping.run_ping_and_wait / run_icmp_responder` | 64-byte CS8900a RX peek routine | peek_addr= |
| `$C000-$C3FF` | 1024 | `uci_network._execute_uci_routine / build_uci_command` | UCI stub block: code $C000, data $C100, response $C200, status $C300, lengths $C3F0-$C3F3, sentinel $C3FE, error $C3FF | code_addr= for the routine; buffers hardcoded |
| `$C100-$C348` | 585 | `bridge_ping.run_ping_and_wait / run_icmp_responder` | TX / echo-match / echo-respond routines (largest: the ARP-answering echo-respond routine of run_icmp_responder(my_mac=...), 585 bytes; 349 without my_mac) | consume_addr= |
| `$C400-$C402` | 3 | `uci_network.build_socket_write` | 16-bit inner-loop countdown (lo, hi) + Y save slot across the turbo fence | hardcoded |
| `$C403` | 1 | `uci_network.uci_socket_write` | socket-id slot for the lifted-cap write routine | hardcoded |
| `$C500-$C87D` | 894 | `uci_network.uci_socket_write` | data buffer (up to 892 bytes) followed by the 2-byte LE length | hardcoded |
| `$CF00-$CF03` | 4 | `tests/test_vice_core.py::_restore_basic (also scripts/vice_keyecho_probe.py + scripts/vice_stall_probe.py)` | CLI; JMP $E5CD stub returning the CPU to BASIC MAINLOOP before every screen/keyboard test — test-suite scratch, not library | hardcoded |

† *transient* — the prior contents are written back afterwards (best-effort for the liveness probe: only on success). It does NOT mean the span is safe to execute from while the operation runs: the REU window is filled by REC DMA with the CPU live and `MemoryPolicy` cannot see that fill; on Ultimate transports `extract_reu_contents` warns when the policy declares RAM inside it (VICE's monitor holds the machine, so no warning there). Declared like every other write, but not withheld by `MemoryArbiter` by default.
<!-- END HARNESS_SCRATCH TABLE -->

Reading the table:

* **Overlaps are expected.**  Several features share the cassette
  buffer (`$0334`-`$03FB`) and the `$C000` page; the entries are
  per-writer, not a partition.  The `$C000-$CFFF` "harness scratch
  page" used in the examples above is the conventional *superset* a
  consumer grants — it covers every `$Cxxx` entry here, including the
  `$CF00` stub the old table omitted — while the code only ever writes
  the spans listed.
* **KERNAL-mandated addresses** (`$00C6`, `$0277-$0280`, `$0314-$0315`)
  cannot be moved; keyboard injection and the SID player write them on
  either backend.
* **Not listed, deliberately:** I/O-register writes in `$D000-$DFFF`
  (CIA joystick ports, the REC at `$DF01-$DF0A`, the UCI abort at
  `$DF1C`, CS8900a registers at `$DExx`) — those are not RAM and the
  policy models only the RAM plane.  Screen RAM `$0400-$07E7` is *not*
  written by the library (the `Ultimate64InstanceManager` docstring's
  `write_memory(0x0400, b"HELLO")` is a usage example); only tests and
  scripts poke it.
* `backends.ultimate64_probe.liveness_probe` issues its 128-byte write
  through the raw REST client, so the transport-level policy never
  sees it.  It writes the original bytes back on success (the readback-
  failure branches leave the pattern in place), hence *transient*.
* *Transient* is a narrow promise: the prior bytes are written back
  afterwards.  It does **not** mean the span is safe to execute from
  while the operation runs.  The REU staging window is the sharp case:
  `extract_reu_contents` fills `$0800-$87FF` by REC DMA — the host only
  programs `$DF01-$DF0A`, so `MemoryPolicy` never sees the clobber —
  with the CPU running (unpaused is mandatory on Ultimate hardware,
  see `docs/snapshot_interop.md`).  A program executing from
  `$0801-$87FF` runs REU data during the extract, and the write-back
  does not undo PC/stack/side effects.  On Ultimate transports
  `extract_reu_contents` emits a `UserWarning` when the policy declares
  a region inside the window; stop the program or keep it out of the
  window first.  On VICE the binary monitor holds the machine during
  memory commands, so the write-back really is transient and no
  warning is raised.

`MemoryPolicy.from_prg()` warns at construction when the load image
overlaps a non-transient entry — the collision would otherwise surface
only as a `MemoryPolicyError` from the first `jsr()` deep inside a
test.  `policy.harness_scratch_overlaps()` returns the same
`(reserved_region, scratch_entry)` pairs for any policy (pass
`include_transient=True` to see the REU staging window too).  The
policy does **not** add these regions to its own lists: as reserved
they would block the harness's writes into its own scratch — which are
the whole point — and as safe they would change what `unknown_policy`
means for every other write.

## `MemoryArbiter` — the ergonomic complement

When a test author needs a scratch address and would rather not
hand-pick one, :class:`MemoryArbiter` walks the policy's free space:

```python
from c64_test_harness import MemoryArbiter

arbiter = MemoryArbiter(policy=cfg.memory_policy)
trampoline_addr = arbiter.alloc(117, name="trampoline")
sentinel_addr = arbiter.alloc(16, name="sentinel")
# Both addresses are guaranteed to pass policy.check_write.
```

The "guaranteed to pass" contract is enforced, not assumed: `alloc()`
runs every candidate span through `policy.check_write` before returning
it, so an allocated address can never trip the transport-level check.
This matters for reserved-only policies with `unknown="deny"` — their
"free" intervals would still be refused by `check_write`, and the
arbiter now rejects such candidates instead of handing them out (with
an explicit diagnostic in `MemoryArbiterError.trace` when nothing is
allocatable because no `safe_regions` are declared).

By default the arbiter also treats every non-transient entry of
`HARNESS_SCRATCH` as taken, so it never returns the byte `jsr()` is
about to put its trampoline on (issue #169 measured exactly that:
`MemoryArbiter(MemoryPolicy.permissive())` reported `$0334`, `$C000`
and the rest all free).  `arbiter.is_free(addr, length=1)` answers the
question directly.  Consequence of the exclusion: with a permissive
policy the first 256-byte request now lands at `$03F2-$04F1`, half of
it in screen RAM `$0400-$07E7` (which the arbiter does not know about —
the KERNAL scrolls it); callers who want RAM clear of the screen should
pass a policy whose `safe_regions` name their own window.  Pass
`exclude_harness_scratch=False` for raw
allocation over the policy alone — appropriate when the caller *is*
the harness, or has relocated every scratch address via the kwargs in
the table.  Transient entries (the REU staging window, the liveness
probe payload) are not withheld either way; `arbiter.reserve(...)`
them if a test needs that.

The arbiter is **not** the safety mechanism — the policy on the
transport is.  Even code that bypasses the arbiter and hands a
hardcoded address to `write_memory` is checked.

If you want the arbiter's allocations to become visible to subsequent
policy checks (useful for catching a second piece of code that didn't
go through the arbiter and tries to write to an arbiter-owned
address):

```python
target.transport.memory_policy = arbiter.policy_with_allocations()
```

## What this doesn't solve

The policy lives at the host→device boundary.  It cannot see:

* **Writes from 6502 code itself** — once a trampoline runs, the CPU
  can `STA` anywhere.  The mitigation here is `PrgFile.verify_region`
  as a post-test structural check, not a runtime guard.
* **Banking transitions** — toggling `$01` to expose RAM under ROM
  means the same address refers to different physical bytes at
  different times.  Declaring `$A000-$BFFF` as reserved is over-
  conservative (covers both ROM and RAM-under-ROM views) but safe.
* **REU / DMA / cartridge overlays** — a future version will model
  these as separate address planes; today the policy only sees the
  16-bit main-RAM plane.
* **Dynamic growth** — if a program extends a table into the scratch
  region at runtime, the policy won't notice.  A `policy.refresh()`
  hook would address this; deferred until a consumer hits it.

## Migration

Existing tests need no changes.  The default `MemoryPolicy()` is
permissive — empty regions, `unknown=allow` — so every write passes
exactly as before.  Opt in at your own pace:

1. Start by passing the policy with `unknown="warn"` to see what the
   harness is writing where.
2. Add `reserved_regions` for the parts of your program you know about.
3. Add `safe_regions` for the scratch areas you've reserved.
4. Tighten `unknown_policy` to `"deny"` once the layout is stable.

## See also

* `src/c64_test_harness/memory_policy.py` — the policy itself.
* `src/c64_test_harness/memory_arbiter.py` — the allocator helper.
* `tests/test_memory_policy.py` — policy semantics by example.
* `tests/test_transport_memory_policy.py` — confirms the transport
  wiring fires before any byte crosses the wire.
* `scripts/gen_memory_table.py` — renders the scratch table above from
  `HARNESS_SCRATCH` (`--check` / `--write`); `tests/test_harness_scratch.py`
  pins each entry's bounds to the emitting code and
  `tests/test_memory_table_doc.py` fails on drift.
