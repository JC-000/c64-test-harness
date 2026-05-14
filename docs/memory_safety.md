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
the load image — see "What this doesn't solve" below.

### From a TOML config

```toml
# c64test.toml

[memory]
prg = "build/program.prg"            # auto-reserves load span
unknown_policy = "deny"

safe_regions = [
    { range = "$0334-$03FB", note = "cassette buffer (default harness scratch)" },
    { range = "$C000-$CFFF", note = "harness-claimed scratch page" },
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

For reference, these are the addresses the harness writes to as part
of normal operation.  If your consumer program uses any of them,
either declare them as `reserved_regions` (and pass the harness's
scratch addresses to functions that accept them via kwarg), or use
:class:`MemoryArbiter` to pick fresh ones.

| Address(es) | Used by | Configurable |
|---|---|---|
| `$0334-$0338` | `execute.jsr()` default trampoline | `scratch_addr=` kwarg |
| `$0339`, `$033C` | `sid_player` park JMP + song trampoline | module-level constants |
| `$0360-$036D` | `execute.run_subroutine()` default trampoline | `trampoline_addr=` kwarg |
| `$03F0`, `$03F1` | `execute.run_subroutine()` U64 flag bytes | hardcoded |
| `$0277`, `$00C6` | UCI keyboard dispatch (KERNAL-mandated) | not changeable |
| `$C000-$C3FF` | UCI stub block (code/data/response/status) | partially configurable |
| `$C000` | `sid_player` default code stub | `DEFAULT_STUB_ADDR` override |

A consumer that declares these in `safe_regions` will let the harness
operate normally; one that puts code/data at any of these addresses
without declaring overrides should expect the policy to fire.

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
