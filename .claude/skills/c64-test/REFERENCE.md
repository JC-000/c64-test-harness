# c64-test-harness API Reference

## Installation

```bash
pip install -e /path/to/c64-test-harness
```

The package is `c64_test_harness`. All public symbols are re-exported from the top-level `__init__.py`. The package version is exposed as `c64_test_harness.__version__` (via `importlib.metadata`).

---

## Memory Safety (`MemoryPolicy`)

The harness has fixed scratch addresses (authoritative list: `HARNESS_SCRATCH` in `memory_policy.py`, rendered into `docs/memory_safety.md` by `scripts/gen_memory_table.py`; highlights: `$0334` jsr trampoline, `$0360`+`$03F0`-`$03F1` `run_subroutine`, `$0277`/`$00C6` keyboard buffer, `$C000-$C3FF` UCI block, `$C400-$C87D` UCI socket-write scratch, `$C000`+`$0339`+`$033C` SID player, `$CF00` test-suite BASIC-restore stub). Any host-side `write_memory()` into a region the consumer also uses silently collides — the 6502 has no MMU. `MemoryPolicy` enforces an allow-list / deny-list at the transport boundary; violations raise `MemoryPolicyError` before any byte crosses the wire.

```python
from c64_test_harness import MemoryPolicy, MemoryRegion, UnknownPolicy
from c64_test_harness.verify import PrgFile

# Cheapest signal — auto-reserve the PRG's load span:
prg = PrgFile.from_file("build/program.prg")
target.transport.memory_policy = MemoryPolicy.from_prg(prg, unknown=UnknownPolicy.WARN)

# Or fully programmatic:
policy = (
    MemoryPolicy.permissive()
    .with_reserved(MemoryRegion.parse("$4200-$50FF", note="X25519 RODATA"))
    .with_safe(MemoryRegion.parse("$C000-$CFFF", note="harness scratch"))
    .with_unknown(UnknownPolicy.DENY)
)
target.transport.memory_policy = policy

# Override escape hatch (logged at WARNING):
target.transport.write_memory(0x4200, payload, override="fault injection")
```

`UnifiedManager(memory_policy=cfg.memory_policy)` stamps the policy onto every acquired transport. `HarnessConfig.from_toml(...)` parses `[memory]` sections automatically.

`MemoryArbiter` is the ergonomic complement — it walks the policy's free space and hands out addresses guaranteed to pass `check_write` (`alloc()` verifies every candidate via `policy.check_write` before returning it):

```python
arbiter = MemoryArbiter(policy=cfg.memory_policy)
trampoline_addr = arbiter.alloc(117, name="trampoline")
```

The arbiter is NOT the safety mechanism (the policy on the transport is). Even code that bypasses the arbiter and hardcodes an address is still checked. By default it withholds every non-transient `HARNESS_SCRATCH` entry (issue #169) — `MemoryArbiter(policy, exclude_harness_scratch=False)` opts out; `arbiter.is_free(addr, length=1)` asks whether a span would be handed out. `MemoryPolicy.from_prg()` warns when the load image overlaps harness scratch; `policy.harness_scratch_overlaps()` lists the pairs. Full design in `docs/memory_safety.md`.

---

## Backend-Agnostic Testing (RECOMMENDED)

For tests that should work on both VICE and Ultimate 64, use `UnifiedManager` / `create_manager()`. It selects the backend at runtime and handles all cross-process locking automatically.

### `UnifiedManager`
```python
from c64_test_harness import create_manager, UnifiedManager, TestTarget

# From environment (C64_BACKEND, U64_HOST, U64_PASSWORD)
with create_manager() as mgr:
    with mgr.instance() as target:  # -> TestTarget
        target.transport  # C64Transport (BinaryViceTransport or Ultimate64Transport)
        target.backend    # "vice" or "u64"
        target.pid        # VICE PID or None for hardware

# Explicit backend
mgr = UnifiedManager(backend="u64", u64_hosts=["192.168.1.81"])
target = mgr.acquire()
mgr.release(target)
mgr.shutdown()
```

Environment variables:
- `C64_BACKEND` — `"vice"` (default) or `"u64"`
- `U64_HOST` — hostname/IP (comma-separated for multiple devices)
- `U64_PASSWORD` — optional device password

### `TestTarget` (dataclass)
- `.transport: C64Transport` — the live transport
- `.backend: str` — `"vice"` or `"u64"`
- `.pid: int | None` — VICE OS process PID, or `None` for hardware
- `.client -> Ultimate64Client` (property) — returns the underlying `Ultimate64Client` on U64-backed targets. Raises `AttributeError` on VICE-backed targets. Use this to reach U64-only endpoints (e.g. `target.client.reboot()`, `target.client.send_text(...)`) from cross-backend code that already has a `TestTarget`.

### `BackendManager` (Protocol)
Structural protocol satisfied by both `ViceInstanceManager` and `Ultimate64InstanceManager`:
- `acquire() -> Any`
- `release(instance) -> None`
- `shutdown() -> None`

### `create_manager(backend="auto", *, lock_timeout=60.0, **kwargs) -> UnifiedManager`
Factory function. `backend="auto"` reads `C64_BACKEND` env var (defaults to `"vice"`).

- `lock_timeout: float` — cross-process device-lock timeout in seconds (U64 only); threaded through `UnifiedManager` to `_LockedU64Manager` (which now calls `lock.acquire_or_raise(timeout=lock_timeout)` and raises `DeviceLockTimeout` on failure — see below). Default 60.0; bounds the wait against **wedged or dead** holders only — healthy holders heartbeat the lockfile every ~15 s and extend the deadline implicitly, so widening this past ~120 s is rarely useful (see PATTERNS § "Pattern 9a: Queueing for the U64").

**U64 cross-process safety:** When the U64 backend is selected, `UnifiedManager` automatically wraps device access with `DeviceLock` via `_LockedU64Manager`. Multiple agents (separate OS processes) queue for the same physical device automatically.

---

## VICE Instance Management (MANDATORY for VICE-only tests)

**ALWAYS use `ViceInstanceManager`** to launch and manage VICE instances. Never use `ViceProcess`, `BinaryViceTransport`, or `PortAllocator` directly. This is critical for safety when multiple Claude agents run in parallel.

### Standard Pattern (single instance)

```python
from c64_test_harness import ViceConfig, ViceInstanceManager, wait_for_text, write_bytes

config = ViceConfig(prg_path="build/program.prg", warp=True, ntsc=True, sound=False)

with ViceInstanceManager(
    config=config,
    port_range_start=6511,
    port_range_end=6531,
) as mgr:
    inst = mgr.acquire()
    print(f"VICE PID={inst.pid}, port={inst.port}")

    transport = inst.transport
    wait_for_text(transport, "Q=QUIT", timeout=60.0, verbose=False)
    write_bytes(transport, 0x0339, bytes([0x4C, 0x39, 0x03]))  # safety loop

    # ... use transport for testing ...

    mgr.release(inst)
```

### Why not ViceProcess directly?

- **Port collisions**: `ViceConfig` defaults to port 6502. Multiple agents using `ViceProcess` directly will fight over the same port.
- **PID conflicts**: Without `ViceInstanceManager`, agents use `pkill x64sc` to clean up "zombie" instances, killing other agents' active VICE processes.
- **Manual plumbing**: `ViceInstanceManager` handles `PortAllocator.allocate()` -> `take_socket()` -> `close()` -> `ViceProcess.start()` -> retry-connect `BinaryViceTransport` -- all automatically.

---

## Module: backends.vice_manager

### `ViceInstanceManager`
Manages a pool of VICE instances with cross-process-safe port allocation.
```python
with ViceInstanceManager(
    config=ViceConfig(prg_path="build/prog.prg", warp=True),
    port_range_start=6511,
    port_range_end=6531,
    max_retries=3,  # retry with exponential backoff on failure
) as mgr:
    inst = mgr.acquire()  # -> ViceInstance
    # inst.port, inst.transport, inst.process, inst.pid
    mgr.release(inst)
```

### `ViceInstance`
Returned by `mgr.acquire()`. This is the primary handle for all VICE interaction.
- `.port: int` -- The allocated port (from PortAllocator)
- `.transport: BinaryViceTransport` -- Pre-configured binary transport on the correct port
- `.process: ViceProcess` -- The underlying VICE process
- `.pid: int | None` -- PID of the VICE OS process (`None` if adopted/unmanaged)

### `PortAllocator`
Cross-process-safe port allocator. **Used internally by `ViceInstanceManager`** -- do not use directly.

Uses OS-level `bind()` + file-based `flock()` locks to hold ports so concurrent processes cannot claim the same port. The file lock bridges the TOCTOU gap between closing the reservation socket and VICE binding.
- `allocate(allow_in_use=False) -> int` -- Reserve next free port (held via `bind()` + file lock)
- `take_socket(port) -> socket | None` -- Retrieve and remove the reservation socket (close it before VICE starts)
- `take_lock(port) -> PortLock | None` -- Retrieve and remove the file lock (caller becomes responsible for releasing)
- `release(port)` -- Free port and close any held socket/lock
- `allocated_ports -> frozenset[int]` -- Snapshot of allocated ports
- `is_port_in_use(port) -> bool` -- Check for active TCP listener (static method)

### `PortLock`
File-based cross-process lock using `fcntl.flock()`. **Used internally by `PortAllocator`** -- do not use directly.

Lockfiles are stored in `$XDG_RUNTIME_DIR/c64-test-harness/` (fallback: `/tmp/c64-test-harness-{uid}/`). The kernel auto-releases locks when the process exits (crash-safe).
- `acquire() -> bool` -- Non-blocking exclusive lock; writes metadata (PID, timestamp)
- `release()` -- Unlock fd (best-effort). Does NOT delete lockfile (inode race safety)
- `update_vice_pid(pid)` -- Update metadata with VICE process PID
- `read_info() -> dict | None` -- Read metadata without locking (diagnostics)
- `cleanup_stale(lock_dir=None) -> int` -- Class method; removes lockfiles from dead PIDs
- Context manager support (`with PortLock(port):`)
- `.port` / `.held` properties

---

## Module: transport

### `C64Transport` (Protocol)
Abstract transport interface. Concrete implementations: `BinaryViceTransport`, `Ultimate64Transport`, `HardwareTransportBase`.

Methods:
- `read_memory(addr: int, length: int) -> bytes` -- Read raw bytes. A span that would run past `$FFFF` raises `ValueError` (no silent wrap to `$0000`).
- `write_memory(addr: int, data: bytes | list[int], *, override: str | None = None) -> None` -- Write bytes to C64 memory. `override="reason"` bypasses `MemoryPolicy` for one call (logged at WARNING). A span that would run past `$FFFF` raises `ValueError` — `override` does not bypass that check.
- `read_screen_codes() -> list[int]` -- Read raw screen code bytes (cols * rows values)
- `inject_keys(petscii_codes: list[int]) -> None` -- Inject PETSCII key codes into keyboard buffer
- `inject_joystick(port: int, value: int) -> None` -- Joystick port 1 or 2; value bits 0-4 = up/down/left/right/fire. **Active-high protocol convention**: bit set = pressed (VICE `JOYPORT_SET` polarity); backends with active-low hardware (U64 CIA ports) invert internally. Persistence differs: VICE holds the state until the next call; the U64 write is one-shot (KERNAL rewrites the CIA ports at ~60 Hz), so sustained input needs re-injection or a paused machine.
- `set_speed(multiplier: int | None) -> None` -- Backend-agnostic CPU-speed control. `multiplier=1` is native 1 MHz (warp off / turbo off); `multiplier=None` is max speed (VICE warp on / U64 probed device maximum — 64 MHz on C64U, 48 on U64E, 48 fallback). U64 accepts discrete turbo multipliers (2–48, plus 64 on the C64 Ultimate generation; generation-foreign speeds raise `ValueError` locally when the preset probe succeeded); VICE raises `NotImplementedError` for any multiplier other than `1` / `None` because the 6510 has no discrete CPU-speed steps natively.
- `get_speed() -> int | None` -- Current multiplier; `None` means "as fast as possible" (VICE warp / unrecognised U64 turbo step).
- `reset(scope: str = "cpu", *, drive: str | int | None = None) -> None` -- `scope="cpu"` soft-resets the 6510; `scope="machine"` is a full reset (VICE hard reset / U64 `reboot()`); `scope="drive"` requires `drive=` (0..3 on VICE, "a"/"b" on U64).
- `read_framebuffer() -> dict` -- Captures one frame. Backend-specific layout (`debug_rect`, `inner_rect`, `bpp`, `palette`, `bytes`); on U64 captures one UDP video frame, on VICE pulls via the binary-monitor display command.
- `read_palette() -> list[tuple[int,int,int]]` -- Active VIC palette as 16 RGB triples.
- `resume() -> None` -- Resume CPU execution
- `close() -> None` -- Release resources / close connection

Properties:
- `screen_cols -> int` -- Number of screen columns (typically 40)
- `screen_rows -> int` -- Number of screen rows (typically 25)

`read_registers()` is **VICE-only** — see `BinaryViceTransport` below. It was removed from the cross-backend protocol in PR #121 (ab49939) because the U64 REST API has no CPU-register endpoint.

### Exceptions
- `TransportError` -- Base exception
- `ConnectionError` -- TCP connection to VICE monitor failed (port not ready)
- `TimeoutError` -- Operation timed out

---

## Module: memory

All functions take `transport` as first arg (stateless).

- `read_bytes(transport, addr, length) -> bytes` -- Read bytes from addr. Contains legacy auto-chunking at 256 bytes (unnecessary with binary transport but harmless).
- `read_bytes_chunked(transport, addr, length, chunk_size=128) -> bytes` -- Explicitly chunked read for large regions. A chunk that comes back short is retried once, then raises `ShortReadError` instead of silently returning a truncated, misaligned result.
- `ShortReadError` -- Raised by `read_bytes_chunked` on a persistent short chunk. Attributes: `.addr`, `.requested`, `.got`. (Module-level export, not re-exported from the package root.)
- `read_bytes_verified(transport, addr, length, *, max_attempts=2) -> bytes` -- Re-reads on disagreement until two consecutive reads match; raises `FlakeyReadError` if `max_attempts` reads all disagree pairwise. Diagnostic added in PR #88 for downstream tests suspecting issue-#88-style flakey reads (VICE binary monitor response-type misrouting). Doubles wire traffic per read — only use when a flake is actively suspected; the PR also tightened the read path to raise `TransportError` on `response_type` mismatch, which is usually the faster diagnostic.
- `FlakeyReadError` -- Raised by `read_bytes_verified` on persistent disagreement. Attributes: `.addr`, `.length`, `.attempts: list[bytes]` (the disagreeing reads in order). Inspect to distinguish structured corruption (every-other-byte ±1) from random truncation.
- `write_bytes(transport, addr, data) -> None` -- Write data to addr (accepts bytes or list[int]). Contains legacy auto-chunking at 84 bytes (unnecessary with binary transport but harmless). Subject to `MemoryPolicy` enforcement at the transport (see "Memory Safety" section above).
- `read_word_le(transport, addr) -> int` -- Read 16-bit little-endian
- `read_dword_le(transport, addr) -> int` -- Read 32-bit little-endian
- `hex_dump(transport, addr, length) -> str` -- Formatted hex dump string

---

## Module: execute

All functions take `transport: BinaryViceTransport` as first arg (stateless). These functions use the binary monitor's native checkpoint and register commands.

- `load_code(transport, addr, code) -> None` -- Write executable bytes (semantic alias for write_memory)
- `set_register(transport, name, value) -> None` -- Set CPU register via `transport.set_registers({name: value})`
- `goto(transport, addr, *, cold=False) -> None` -- Set PC via `transport.set_registers({"PC": addr})` then `transport.resume()`. A **one-way** jump: control never returns, so nothing is restored. This is **not** the `jsr()` defect of #183 despite that issue's wording (corrected in a comment there; see #192) -- a restore needs a moment when control comes back, and `goto()` has none. By default the target inherits the stack frame and `I` flag of whatever the monitor halted, interrupt handler included. `cold=True` writes `SP=$FF` and `FL=$20` (`I` and `D` clear) alongside `PC` in the same register command: "start this as if nothing was running". It does **not** reset the machine (vectors, I/O and zero page are as the previous program left them) and does **not** acknowledge a pending interrupt, so a target entered with `I` newly cleared may take that interrupt at once. It needs `SP` and `FL` in the transport's register map; a map without them refuses the whole write, leaving the CPU halted where it was. The alternative is caller-side: rebuild `SP` in the target, e.g. the warm-start `JMP ($A002)` idiom in `sid_player.py` (which wants BASIC running afterwards and so cannot use `cold`). Not the same knob as `jsr()`'s `preserve_state`, which asks for the opposite.
- `set_breakpoint(transport, addr) -> int` -- Calls `transport.set_checkpoint(addr)`, returns checkpoint ID
- `delete_breakpoint(transport, bp_id) -> None` -- Calls `transport.delete_checkpoint(bp_id)`
- `wait_for_pc(transport, addr, timeout=5.0) -> dict` -- Calls `transport.wait_for_stopped()` then verifies PC; returns register dict; CPU is **paused** on return
- `jsr(transport, addr, timeout=5.0, *, scratch_addr=0x0334, override=None, recover_on_timeout=False, preserve_state=True) -> dict` -- Call subroutine via trampoline, wait for RTS; CPU is **paused** on return. Works reliably for both short and long-running computations (event-based, no polling). With `recover_on_timeout=True`, a routine that never returns raises `RoutineHung` (a `TimeoutError` subclass) after restoring SP and proving the trampoline live with an `RTS` probe; `recovered`, `elapsed`, `addr`, `hung_pc`, `detail` carry the outcome (PATTERNS § "Probing a routine that may hang"). Default `False`: a bare `TimeoutError`, as before. `preserve_state=True` (default) reads the pre-call `PC`/`SP`/`FL` before the hijack and writes them back after the `RTS`, so a call that landed mid-interrupt no longer abandons the handler's frame or leaves `I` set forever. `A`/`X`/`Y` are **not** put back, and **nothing** is put back on timeout. Pass `preserve_state=False` where the routine's own flag or stack effects must survive.
- `RoutineHung(TimeoutError)` -- Raised by `jsr(..., recover_on_timeout=True)` when the routine hangs. `recovered=False` means the next call on that transport is not safe. Re-exported from the package root.
- `RECOVERY_PROBE_TIMEOUT = 5.0` -- Seconds the recovery `RTS` probe waits for its landing.
- `run_subroutine(target, addr, *, timeout=30.0, poll_cadence=0.005, trampoline_addr=0x0360, override=None) -> None` -- Cross-backend "call sub and wait for RTS". Takes a `TestTarget` (not a transport). On VICE wraps `jsr()`. On U64 installs a 14-byte sentinel trampoline at `trampoline_addr` (default `$0360`, cassette buffer; flag bytes at `$03F0`/`$03F1`), triggers it via `SYS <addr>` keystroke (assumes BASIC READY), and host-polls the done flag every `poll_cadence` seconds — sub-millisecond cadence is permitted and useful for short routines (issue #82). Raises `TimeoutError` on U64 only; the message distinguishes "never started" (running flag still `0x00`) from "started but never returned" (running flag `0x01`, done flag never `0x02`). Re-exported from the package root.
- `parse_basic_sys_address(prg, *, basic_start=0x0801) -> int | None` -- Walks the tokenised BASIC stub line by line and returns the `SYS` operand (`10 SYS2061` as cc65 emits it; `SYS(2061)` accepted; text inside quotes and after `REM` ignored), or `None`. A PRG not loading at *basic_start* is not BASIC and yields `None` whatever bytes it holds.
- `run_prg_via_sys(target, prg, *, sys_addr=None, reset=True, boot_timeout=25.0, verify_timeout=10.0, settle_after_ready=None) -> int` -- Load a PRG by `write_memory`, re-verify its head by read-back until the post-`READY.` settle has elapsed (default 6 s on a U64 after a reset, 0 otherwise — issue #216: a reset-triggered event zeroes `$0801/$0802` 2–5 s after the banner; the re-verification is the guarantee, the settle an optimisation), start it with a typed `SYS`, then resume; returns the entry address used. Raises `TransportError` if the head never reads back intact `verify_timeout` s past the window. Takes a `TestTarget` or a bare transport; works on both backends. Exists because the firmware's runner load path (`Ultimate64Client.run_prg()`/`load_prg()`) deselects an external cartridge, stickily across every `reset()`, until `Cartridge Preference` is PUT again (#217); on a U64 whose preference reads `External` the helper re-PUTs it before writing (`reselect_cartridge=False` opts out; a failed PUT is a WARNING). `reset=True` resets and waits for `READY.` (`TimeoutError` if it never appears); `sys_addr` defaults to the stub's own `SYS` (`ValueError` if none). Re-exported from the package root. Unit test: `tests/test_run_prg_via_sys.py`.

### `jsr()` internals
0. Reads `PC`/`SP`/`FL`, unless `preserve_state=False` or the transport has no `read_registers`
1. Writes trampoline at `scratch_addr`: `JSR $addr; NOP; NOP` (5 bytes)
2. Sets checkpoint at `scratch_addr + 3`
3. Sets PC to `scratch_addr` and resumes CPU
4. Calls `wait_for_stopped()` until checkpoint fires, verifies PC
5. Deletes checkpoint
6. Writes `PC`/`SP`/`FL` back. **Success path only** — the restore sits outside the `try`/`finally`, so a timeout leaves the register file alone for the recovery probe to read. Note this is the opposite structural choice from the screen waiters, which resume in a `finally` precisely so every path is uniform; here a hung routine must stay visibly hung.

Step 0/6 preserve `PC`/`SP`/`FL`. Recovery (below) restores `A`/`X`/`Y`/`SP`/`FL`. The sets differ deliberately: recovery is putting a *usable* machine back, step 6 is putting the *interrupted* one back, and only the latter needs `PC`.

With `recover_on_timeout=True`, step 4 timing out adds: read registers (binmon answers while the CPU spins), restore the register file (A, X, Y, SP, FL) captured before step 1 in one command, rewrite the trampoline as `20 lo hi EA 60` — `JSR scratch_addr+4; NOP; RTS`, the spare second `NOP` byte becoming the `RTS` — and run steps 2-5 against it with `RECOVERY_PROBE_TIMEOUT`, require PC == `scratch_addr + 3`, then raise `RoutineHung`. Recovery writes nothing outside the five trampoline bytes the call already owns.

---

## Module: disasm

- `disassemble(mem, base, *, upper=True) -> list[str]` -- Render a memory window (`bytes`) as one line per 6502/6510 instruction in classic monitor layout (`C000  AD 01 DE  LDA $DE01`), starting at address `base`. All 256 opcodes decode, illegal ones included, each with its real length, so a listing never desynchronises at an illegal instruction — the exact place a `RoutineHung.hung_pc` diagnostic looks. Dependency-free; not wired into `jsr()`/`RoutineHung` itself, but useful alongside `.hung_pc` for rendering what the CPU was executing. `scripts/dis6502.py` is a thin CLI over this module (no opcode table of its own).
- `instruction_length(opcode) -> int` -- Byte length of one instruction from its opcode.

---

## Module: screen

- `ScreenGrid` -- Parsed screen state (40x25 character grid)
- `wait_for_text(transport, needle, timeout=60.0, poll_interval=2.0, verbose=True, on_progress=None) -> ScreenGrid | None` -- Poll screen RAM until text appears. Returns `None` on timeout. **Note:** `verbose` defaults to `True` (dumps screen on every poll) — pass `verbose=False` for quiet operation. The CPU is **running** on return, on every exit path; the grid was captured before that resume, so if you need the machine halted alongside the grid your next read halts it again. Best-effort: a `resume()` that raises is logged at WARNING on `c64_test_harness.screen` and swallowed (issue #191).
- `wait_for_stable(transport, timeout=10.0, poll_interval=0.5, stable_count=3) -> ScreenGrid | None` -- Wait for screen to stop changing. Returns `None` on timeout. The CPU is **running** on return, on every exit path, same caveat as `wait_for_text()`.

---

## Module: keyboard

- `send_text(transport, text) -> None` -- Type text into C64 keyboard buffer (max 10 chars at a time, auto-chunks)
- `send_key(transport, char_or_code) -> None` -- Send single keypress: a one-char `str` (e.g., `"\r"` for RETURN) or a PETSCII `int`

---

## Module: labels

### `Labels`
Inherits from `collections.abc.Mapping[str, int]` (v0.12.4+). Parses both standard `al C:XXXX .name` lines and ld65's address-space-neutral `al XXXXXX .name` form (labels with addresses above 0xFFFF — e.g. REU offsets).

- `Labels.from_file(path) -> Labels` -- Parse VICE-format label file
- `labels.address(name) -> int | None` -- Lookup address by label name
- `labels.name(addr) -> str | None` -- Reverse lookup
- `labels[name] -> int` -- Dict-style access (raises `KeyError`)
- `name in labels` -- Membership test
- `len(labels)` / `iter(labels)` / `for name, addr in labels.items()`
- `labels.keys()` / `.values()` / `.items()` / `.get(name, default)` -- inherited from `Mapping`
- `dict(labels)` -- round-trip to plain dict (main reason the Mapping inheritance was added)

---

## Module: config

### `HarnessConfig`
Dataclass with all settings. Load from:
- `HarnessConfig.from_toml(path)` -- TOML config file
- `HarnessConfig.from_env(prefix="C64TEST_")` -- Environment variables

Key fields:
- `vice_executable` (default "x64sc")
- `vice_port` (default 6502)
- `vice_warp` (default True)
- `vice_console` / `vice_minimize` (default True) -- recorded but **not wired**: nothing builds a `ViceConfig` from a `HarnessConfig`, so these do not reach the launch. `-console` / `-minimized` are controlled by `ViceConfig(console=..., minimize=...)`.
- `vice_prg_path` (default "")
- `vice_acquire_retries` (default 3) -- retry count for `ViceInstanceManager.acquire()` on startup failure
- `screen_poll_interval` (default 2.0) -- poll interval for `wait_for_text()`/`wait_for_stable()`. Decrease for graphics-heavy tests.
- `vice_ethernet` (default False) -- enable CS8900a ethernet cartridge emulation
- `vice_ethernet_mode` (default "rrnet") -- "rrnet" or "tfe"
- `vice_ethernet_interface` (default "") -- host network interface. Values typically come from `tests/bridge_platform.py` (`tap-c64-0`/`tap-c64-1` on Linux, `feth0`/`feth1` on macOS).
- `vice_ethernet_driver` (default "") -- `"tuntap"` on Linux, `"pcap"` on macOS (see `bridge_platform.ETHERNET_DRIVER`).
- `vice_ethernet_base` (default 0xDE00) -- I/O base address

**Note:** `HarnessConfig` does not include `vice_ethernet_mac`. MAC addresses are configured directly via `ViceConfig.ethernet_mac` or auto-generated by `ViceInstanceManager`.

---

## Module: backends.vice_lifecycle

### `ViceConfig`
Dataclass for VICE configuration. **Pass to `ViceInstanceManager`, not `ViceProcess` directly.**

**Jams follow `monitor`.** With `monitor=True` (default; `ViceInstanceManager` forwards its base config's value) the harness emits `-jamaction 0`; while a binary-monitor client is *connected* (S `monitor_binary.c:2110-2113`) VICE hands the jam to it (S `machine.c:131-139`), the machine stops, and `wait_for_stopped()` raises `TransportError` naming the jammed PC. With `monitor=False` it emits `-jamaction 1`: `0` would open a blocking GTK jam dialog in a windowed VICE with no monitor client (S `machine.c:140`), and under `1` the 6510 halts silently in place — `JAM()` does `CLK++` and never advances the PC (S `maincpu.c:607-628`) — so a jam looks like a hang. With `monitor=True` but no client connected, `console=True` halts silently and `console=False` opens the GTK dialog. Keep the monitor on and connected if you need jams reported.
```python
ViceConfig(
    executable="x64sc",
    prg_path="build/program.prg",
    port=6502,              # Overridden by ViceInstanceManager
    warp=True,
    ntsc=True,
    sound=False,
    monitor=True,           # Binary monitor on; also selects -jamaction 0 (jam -> TransportError) vs 1 (silent halt)
    console=True,           # Headless (-console): no window, no focus stealing
    minimize=True,          # Only when console=False: start window minimized
    extra_args=[],
    disk_image=None,        # DiskImage instance
    drive_unit=8,
    # Ethernet / CS8900a
    ethernet=False,         # Enable CS8900a ethernet cartridge
    ethernet_mode="rrnet",  # "rrnet" (matches ip65 + physical cart) or "tfe"
    ethernet_interface="",  # Host interface; use bridge_platform.IFACE_A / IFACE_B
    ethernet_driver="",     # "tuntap" (Linux) or "pcap" (macOS); see bridge_platform.ETHERNET_DRIVER
    ethernet_base=0xDE00,   # I/O base address
    ethernet_mac=b"",       # 6-byte MAC (empty = auto-generated by manager)
    # Platform: launch VICE as root via `sudo -n` (macOS BPF attach needs it)
    run_as_root=None,       # None = auto: ethernet + root-gated driver + no capability
)
```

**Platform behavior — `run_as_root`**: Tri-state. `None` (the default) resolves to `True` when `ethernet=True`, the driver is one VICE gates behind `archdep_rawnet_capability()` (pcap — every macOS launch; Linux `tuntap` is ungated), and this process lacks that capability (`geteuid()==0`, or `CAP_NET_RAW` on Linux). `/dev/bpf*` permissions are **not** part of it — `bpf_capture_available()` has been removed. Setting `False` on a launch that needs root is *refused* (`ViceElevationRequiredError`), because unelevated VICE SIGSEGVs rather than degrading; `VICE_ETHERNET_ALLOW_UNELEVATED=1` opts out. When True, `ViceProcess` launches x64sc via `sudo -n`, tracks the root-owned child via `_find_x64sc_child_pid()`, and routes `stop()` through `sudo -n kill` because an unprivileged parent cannot signal a root child; the NOPASSWD entry must name the exact x64sc path launched.

A `rawnet_arch_pre_reset+8` NULL deref during `cs8900_activate` means **no driver was selected**, which on macOS means the launch was not root: `archdep_rawnet_capability()` gates driver selection on `geteuid()==0` and never reads `/dev/bpf*`. (Issue #144 read the same crash as "ethernet compiled out"; that was wrong — Homebrew's bottle has the full rawnet surface.) The harness now refuses such a launch instead of crashing; see `docs/development.md` macOS caveats.

**RR-Net is the default** (changed from TFE in PR #44). The register layout matches ip65's `cs8900a.s` and the physical RR-Net cartridge. Requires setting `$DE01` bit 0 (clockport enable) before any CS8900a access — all harness code builders do this automatically.

**MAC address handling**: VICE has no CLI flag for CS8900a MAC addresses. When `ethernet=True` and `ethernet_mac` is empty, `ViceInstanceManager` auto-generates a unique locally-administered MAC (`02:c6:40:xx:xx:xx`) per instance and programs the CS8900a Individual Address registers after transport connects. Set `ethernet_mac` explicitly to override.

### `ViceProcess`
**Used internally by `ViceInstanceManager`** -- do not use directly.

Context manager for VICE lifecycle. Always launches VICE with `-binarymonitor`.
```python
# INTERNAL USE ONLY -- use ViceInstanceManager instead
with ViceProcess(config) as vice:
    # vice.pid -> int | None
    # vice.stop() called automatically on __exit__
    pass
```

`ViceInstanceManager._start_or_adopt()` uses a retry-connect pattern to establish a `BinaryViceTransport` connection after starting VICE, rather than a dedicated wait method.

Static methods:
- `ViceProcess.kill_on_port(port) -> bool` -- Kill process listening on port (Linux `/proc/net/tcp`, macOS `lsof`)
- `ViceProcess.get_listener_pid(port) -> int | None` -- Return PID of process listening on port (Linux `/proc/net/tcp`, macOS `lsof`)

---

## Module: backends.vice_binary

### `BinaryViceTransport`
The sole VICE transport backend, using VICE's binary monitor protocol (`-binarymonitor`). Provides a persistent TCP connection with ~0.08ms per command.

```python
# INTERNAL USE -- get transport from ViceInstance instead
transport = BinaryViceTransport(host="127.0.0.1", port=6502, timeout=5.0)
```

**TCP semantics**: Single persistent connection. CPU does NOT pause on connect. Commands are binary-framed with request IDs. `resume()` (Exit 0xAA) keeps the connection open. VICE pushes async Stopped events when checkpoints fire.

**C64Transport protocol methods:**
- `read_memory(addr, length) -> bytes`
- `write_memory(addr, data, *, override=None) -> None`
- `read_screen_codes() -> list[int]`
- `inject_keys(petscii_codes) -> None`
- `inject_joystick(port, value) -> None`
- `set_speed(multiplier)` / `get_speed()` -- maps `None`/`1` to VICE warp on/off; other multipliers raise `NotImplementedError` (no discrete CPU-speed steps natively). Works on default targets — warp control is hybrid: real `warp on`/`off` via the text monitor when connected (sees CLI `-warp` too), `Speed`-resource pseudo-warp (`Speed=1000000` / restore `Speed=100`) over the binary monitor otherwise; on the fallback path `get_warp` only reflects pseudo-warp set through this transport.
- `reset(scope="cpu", *, drive=None)` -- dispatches to VICE `CMD_RESET` 0 (cpu) / 1 (machine) / 8+idx (drive).
- `read_framebuffer() -> dict`
- `read_palette() -> list[tuple[int,int,int]]`
- `resume() -> None`
- `close() -> None`
- `screen_cols -> int` (property)
- `screen_rows -> int` (property)

**VICE-only methods (not on the cross-backend protocol):**
- `read_registers() -> dict[str, int]` -- every register VICE advertises at connect (`PC`, `A`, `X`, `Y`, `SP`, `FL`, `LIN`, `CYC`, …); `jsr()`'s `preserve_state` relies on `FL` being present. The U64 REST API has no CPU-register endpoint, so this was removed from `C64Transport` in PR #121.

**Binary monitor methods (used by execute.py functions):**
- `set_checkpoint(addr, *, temporary=False, stop_when_hit=True, enabled=True) -> int` -- Set execution checkpoint, returns checkpoint number
- `delete_checkpoint(num) -> None` -- Delete checkpoint by number
- `set_registers(regs: dict[str, int]) -> None` -- Set multiple registers at once (e.g. `{"PC": 0xC000, "A": 0x42}`)
- `wait_for_stopped(timeout=None) -> int` -- Wait for VICE Stopped event, returns PC value

**Key characteristics:**
- No write size limit (4096+ bytes verified)
- No reconnection overhead (persistent connection)
- Async checkpoint events (no polling needed for `jsr()`/`wait_for_pc()`)
- `resume()` does NOT destroy the connection
- CPU auto-pauses on every command -- `wait_for_text()` resumes between polls so the screen updates, and again in a `finally` whenever a read has halted the machine since, so it never hands back a stopped machine

---

## Module: parallel

- `run_parallel(manager, tests, max_workers=None) -> ParallelTestResult`
  - `manager`: any `BackendManager` — `ViceInstanceManager`, `Ultimate64InstanceManager`, or `UnifiedManager` (PR #124 / commit 940b711 generalised this from VICE-only).
  - `tests`: list of `(name, fn)`. What `fn` receives depends on the manager:
    - `ViceInstanceManager` → `fn(transport: BinaryViceTransport)` — legacy signature preserved by unwrapping `instance.transport` before invoking the callable.
    - Any other manager → `fn(instance)` — receives the acquired instance directly. For `UnifiedManager` that's a `TestTarget`; for `Ultimate64InstanceManager` it's an `Ultimate64Instance`. Both expose `.transport`.
- `ParallelTestResult` -- `.results`, `.all_passed`, `.exit_code`, `.print_summary()`
- `SingleTestResult` -- `.name`, `.passed`, `.message`, `.duration`, `.pid: int | None` (VICE process PID; `None` on hardware).
- Failure isolation: an `acquire()` failure (e.g. port exhaustion) is recorded as a failed `SingleTestResult` for that test — it does not abort the run or discard completed results. `max_workers=None` defaults to `len(tests)` capped at 10 (the port-allocator budget can't serve more concurrent instances); an empty `tests` list returns an empty `ParallelTestResult`.

---

## Module: disk

### `DiskImage`
Wraps VICE's `c1541` CLI for D64/D71/D81 image management.
```python
img = DiskImage.create("/tmp/test.d64", name="TEST DISK", fmt=DiskFormat.D64)
img.write_file("hello.seq", c64_name="hello", file_type=FileType.SEQ)
data = img.read_file_bytes("hello")
entries = img.list_files()  # -> list[DirEntry]
```

**Important**: Use **lowercase** `c64_name` so PETSCII filenames match C64 keyboard input.

### `DiskFormat` -- Enum: `D64`, `D71`, `D81`
### `FileType` -- Enum: `PRG`, `SEQ`, `USR`, `REL`
### `DirEntry` -- `.name`, `.blocks`, `.file_type`

---

## Module: ethernet

Helpers for CS8900a MAC address management. VICE has no CLI flag for MAC — must be programmed at runtime via PacketPage registers. `set_cs8900a_mac()` does a read-modify-write on `$DE01` (clockport enable) before the first PP access.

### `generate_mac(index: int) -> bytes`
Deterministic locally-administered MAC: `02:c6:40:xx:xx:xx` from index (0-16777215).

### `parse_mac(mac_str: str) -> bytes`
Parse `"02:c6:40:00:00:01"` (colon or dash separated) → 6 bytes.

### `format_mac(mac: bytes) -> str`
6 bytes → `"02:c6:40:00:00:01"`.

### `set_cs8900a_mac(transport, mac, base=0xDE00)`
Program the CS8900a Individual Address registers via PPPtr/PPData at PP offsets 0x0158-0x015D with host-side `write_memory`. CPU must be stopped (normal after binary monitor connect). **Works under VICE** (host `write_memory` reaches the emulated chip; measured 2026-09-05 by 6510 read-back of the IA) and is **useless on hardware**: a U64 host write to `$DE02`/`$DE04` never reaches the expansion port and host reads of the window are neither meaningful nor reproducible (issue #209) — use `bridge_ping.cs8900a_set_mac_inline_code(mac)` / `cs8900a_set_mac_code(mac)` from the 6510 there.

```python
from c64_test_harness import set_cs8900a_mac, generate_mac, parse_mac

# Auto-generate
mac = generate_mac(0)  # b"\x02\xc6\x40\x00\x00\x00"
set_cs8900a_mac(transport, mac)

# Explicit
mac = parse_mac("02:c6:40:00:00:42")
set_cs8900a_mac(transport, mac, base=0xDE00)
```

**Note**: `ViceInstanceManager` calls `set_cs8900a_mac()` automatically for ethernet-enabled instances. Manual use is only needed for standalone `ViceProcess` setups or non-standard base addresses.

---

## Module: capture

Host-side raw ethernet capture/injection for TX/RX ethernet tests, platform-selected: Linux `AF_PACKET`/`SOCK_RAW` (needs `CAP_NET_RAW`/root), macOS `/dev/bpf*` via `BIOCSETIF` (issue #158 — before this module the TX/RX tests skipped on macOS entirely; needs a world-rw BPF node, see `docs/bridge_networking.md` § "macOS test-author traps" item 4 for the chmod/reboot caveats). `open_capture(iface) -> PacketCapture` returns the platform implementation or raises `CaptureUnavailable` naming the remedy verbatim — skip tests with that message, not a paraphrase. `parse_bpf_records()` is the pure BPF-buffer parser, pinned without a device by `tests/test_capture.py`. Full design in `docs/bridge_networking.md`.

---

## Module: tests.bridge_platform

Platform-dispatch module for bridge/ethernet tests. **Tests MUST import from here instead of hardcoding `tap-c64-*`, `br-c64`, `/sys/class/net/...`, or `tuntap`.** Lives in `tests/` so it's importable from pytest fixtures and conftest without touching the library package.

### Constants (platform-dispatched at import time)

| Name | Linux | macOS |
|------|-------|-------|
| `IFACE_A` | `tap-c64-0` | `feth0` |
| `IFACE_B` | `tap-c64-1` | `feth1` |
| `BRIDGE_NAME` | `br-c64` | `bridge10` |
| `ETHERNET_DRIVER` | `tuntap` | `pcap` |
| `SETUP_HINT` | `"run sudo scripts/setup-bridge-tap.sh"` | `"run sudo scripts/setup-bridge-feth-macos.sh"` |

### Helpers

- `iface_present(name: str) -> bool` — dispatches `/sys/class/net/<name>` (Linux) vs `ifconfig <name>` (macOS). Use in skip gates.
- `first_available_ethernet_iface() -> str | None` — returns the first interface whose name prefix matches the platform (`tap` / `feth`), or `None`.
- `probe_vice_pcap_ok(iface: str | None = None, timeout: float = 3.0) -> tuple[bool, str]` — **macOS-only** active probe. Launches a throwaway x64sc via `sudo -n` using the production `-addconfig` invocation to see whether the pcap driver survives cart activation; returns `(ok, reason)` where `reason` is a human-readable skip message on failure. Cached per-process — cheap to call many times.

### Env overrides for `probe_vice_pcap_ok`

- `MACOS_PCAP_DISABLED=1` — skip the probe, return `(False, ...)`. Use on hosts where pcap is known broken.
- `MACOS_PCAP_ENABLED=1` — skip the probe, return `(True, ...)`. Use on hosts where pcap is known working and you want to save the ~3s probe cost per test session.

### Skip-gate idiom: `elevation(kind, **kwargs)`, not ad hoc `skipif`

Don't hand-roll `iface_present`/`sudo_can_run` checks per module. `tests/conftest.py` registers one marker, `@pytest.mark.elevation(kind, **kwargs)`, covering four elevated prerequisites — each probed **at most once per session** (cached) and, on a miss, skipped with the remedy verbatim (the exact `sudo`/`visudo` line, or `sudo chmod o+rw /dev/bpf*`):

| `kind` | Checks | Default probe |
|---|---|---|
| `"vice_root"` | NOPASSWD sudo for the exact `x64sc` path | resolved ethernet binary; override with `binary="/path/to/x64sc"` |
| `"bridge_scripts"` | NOPASSWD sudo for the bridge lifecycle scripts | the platform's setup/teardown/cleanup trio at their **canonical repo path** (never a worktree's); override with `scripts=("name.sh", ...)` |
| `"bpf_nodes"` | a usable `/dev/bpf*` node (macOS only; always satisfied elsewhere) | opens and immediately closes at most one node |
| `"bridge_iface"` | the bridge interface(s) present and up | `IFACE_A`, `IFACE_B`, `BRIDGE_NAME`; override with `ifaces=(...)` for a subset |

```python
pytestmark = [
    pytest.mark.vice_live,
    pytest.mark.elevation("bridge_iface"),  # IFACE_A + IFACE_B + BRIDGE_NAME
]
```

Keep a module-specific gate (e.g. `probe_vice_pcap_ok`'s active launch-and-crash probe, or `BRIDGE_CLEANUP_LIVE` opt-in) as its own `skipif` alongside the marker — only the four *static* prerequisites above belong to `elevation(...)`.

A fixture that launches `ViceProcess(ethernet=True)` (rather than a test carrying the marker) should route through `start_vice_or_skip(config, request.node.nodeid)` (in `tests/conftest.py`) instead of `with ViceProcess(config) as vice:` — it converts a mid-launch `ViceElevationRequiredError` (e.g. a bypassed preflight probe) into the same skip/fail + record, so the session-end notice covers it too.

`C64_REQUIRE_ELEVATION=1` mirrors `C64_REQUIRE_VICE=1` (the env var that fails a run instead of silently certifying the VICE backend from mocks when no `vice_live` test executes): a missing elevation prerequisite fails at setup instead of skipping, and `pytest_sessionfinish` always prints an `ELEVATION REQUIRED: N test(s) skipped` section — kind, count, remedy — whenever anything was skipped for elevation, without needing `-rs`. See `docs/development.md` "Live test gates" for the full writeup of both knobs.

---

## Module: verify

### `PrgFile`
Parse and verify C64 .PRG files:
```python
prg = PrgFile.from_file("build/program.prg")
# prg.load_address -> int
# prg.data -> bytes
```

---

## Module: debug

- `dump_screen(transport, label="") -> str` -- Save screen contents to file for debugging; returns the dump text

---

## Module: backends.hardware

### `HardwareTransportBase`
Optional base class for hardware backends. Provides default screen dimensions. Subclasses must implement all methods of the `C64Transport` protocol.

```python
class Ultimate64Transport(HardwareTransportBase):
    def read_memory(self, addr, length):
        return self._serial.read_mem(addr, length)
    # ... etc
```

Methods (all raise `NotImplementedError` -- subclasses must override):
- `read_memory(addr, length) -> bytes`
- `write_memory(addr, data, *, override=None) -> None`
- `read_screen_codes() -> list[int]`
- `inject_keys(petscii_codes) -> None`
- `inject_joystick(port, value) -> None`
- `set_speed(multiplier)` / `get_speed()`
- `reset(scope, *, drive=None)`
- `read_framebuffer() -> dict`
- `read_palette() -> list[tuple[int,int,int]]`
- `resume() -> None`
- `close() -> None`

---

## Module: backends.ultimate64

### `Ultimate64Transport`
Hardware transport for Ultimate 64 via REST API. Implements `C64Transport` protocol. All memory I/O is DMA-backed (no CPU pause needed).

```python
from c64_test_harness import Ultimate64Transport
transport = Ultimate64Transport(host="192.168.1.81", password=None, timeout=10.0)
```

**C64Transport methods** (memory ops DMA-backed, no CPU pause):
- `read_memory(addr, length) -> bytes`
- `write_memory(addr, data, *, override=None) -> None` -- Opt-in SocketDMA fast path for bulk writes: set `transport.socket_dma = True` (constructor kwarg or attribute; default False) and payloads >= `socket_dma_min_bytes` (default 8192) route via DMAWRITE on TCP 64 instead of REST (~150 ms vs >6 s for 16 KiB on C64U fw 1.1.0, whose POST writemem degrades at >=16 KiB). Same `MemoryPolicy` checks. DMAWRITE is fire-and-forget, so the write finishes with an in-band IDENTIFY completion barrier (FIFO command servicing means the reply proves every chunk was consumed and applied; recv timeout scales with payload size — same pattern as `reu_write`), then a REST tail read-back as a post-barrier sanity check (up to `socket_dma_verify_timeout`, 2 s). WARNING + REST fallback on connect/send/barrier/verify failure (connect failure latches the fast path off). Requires the device's DMA service on TCP 64 (C64U: enable Network Settings → "Ultimate DMA Service", ships disabled; U64E fw 3.14 availability untested — connect failure just falls back to REST).
- `read_screen_codes() -> list[int]`
- `inject_keys(petscii_codes) -> None`
- `inject_joystick(port, value) -> None` -- Active-high protocol value (bit set = pressed); inverts bits 0-4 before the CIA write (`$DC01` port 1 / `$DC00` port 2, active-low hardware) and routes through `write_memory` with `override="inject-joystick"` so it stays `MemoryPolicy`-visible. One-shot: the KERNAL keyboard scan rewrites the CIA ports at ~60 Hz, so sustained input needs periodic re-injection or a paused machine (VICE holds injected state instead).
- `set_speed(multiplier)` -- maps `None` → the device's probed maximum turbo speed (`max_cpu_speed_mhz`: 64 on a C64 Ultimate, 48 on a U64E; 48 fallback when the probe is inconclusive), `1` → turbo off, other supported MHz (cross-generation superset: 2/3/4/5/6/8/10/12/14/16/20/24/32/40/48/64; U64E fw 3.14 lacks 64, C64 Ultimate fw 1.1.0 lacks 5) → set Turbo Control to "Manual" at that MHz. Wraps `set_turbo_mhz`. Multipliers outside the superset raise `ValueError` locally (no request hits the wire); a speed valid only on the *other* device generation also raises `ValueError` locally when the device's CPU-Speed preset probe succeeded (probed once, cached per client) — only when the probe is inconclusive does it reach the wire, where the firmware rejects it with HTTP 400 (`Ultimate64Error`) before Turbo Control is enabled. See `tests/test_turbo_contract_live.py`.
- `get_speed() -> int | None` -- `1` when turbo off; integer MHz when turbo on at a known step; `None` only when turbo is on but the CPU-Speed enum is unrecognised (treated same as VICE warp).
- `reset(scope="cpu", *, drive=None)` -- `scope="cpu"` → `client.reset()` (soft 6510); `scope="machine"` → `client.reboot()` (full FPGA reinit; ~8s before reachable); `scope="drive"` → `client.drive_reset(drive)` (drive `"a"` / `"b"` or `0` / `1`).
- `read_framebuffer() -> dict` -- captures one frame via the U64 UDP video stream. Auto-detects local IP that can reach the device. Raises `TransportError` with diagnostics if no frame arrives within `timeout`. Layout matches `BinaryViceTransport`: `debug_rect`, `inner_rect`, `bpp`, `palette`, `bytes`.
- `read_palette() -> list[tuple[int,int,int]]` -- canonical 16-entry VIC-II palette via `u64_video_capture.VIC_PALETTE`.
- `resume() -> None`
- `close() -> None`

**Not available on hardware** (require VICE binary monitor):
- `read_registers()`, `jsr()`, `wait_for_pc()`, `set_breakpoint()`, `set_registers()`, `set_checkpoint()`, `wait_for_stopped()`
- Design tests to self-report results via memory writes + sentinel polling

### `Ultimate64Client`
REST API wrapper for U64 firmware endpoints.
```python
from c64_test_harness.backends.ultimate64_client import Ultimate64Client
client = Ultimate64Client(host="192.168.1.81", password=None, timeout=10.0)

# Optional: override the per-instance write_mem PUT/POST cutoff (bytes).
# When omitted, auto-detected from firmware capabilities: 128 on firmware without the
# Temp-folder fix (C64U 1.1.0, or Ultimate-line < 3.15); 48 on Ultimate-line >= 3.15.
client = Ultimate64Client(host="192.168.1.81", write_mem_query_threshold=128)
```

Exception mapping: timeouts, unreachable device, and connection drops mid-request (`ConnectionResetError` / `BrokenPipeError` / truncated HTTP response — fw 3.14d drops connections under load) all raise `Ultimate64TimeoutError`; unparseable or wrong-shaped responses raise `Ultimate64ProtocolError`. Catch `Ultimate64Error` to cover the whole hierarchy — raw socket exceptions no longer escape.

**Machine control:**
- `client.reset()` -- Soft reset the C64 (6510 CPU only, does NOT reinitialize FPGA/DMA)
- `client.reboot()` -- Full reboot of the Ultimate device (reinitializes FPGA, DMA, REU). Required when switching turbo speeds with REU-heavy workloads. Allow ~8s settle after reboot.
- `client.pause()` -- Halt the emulated CPU
- `client.resume()` -- Resume the emulated CPU

**PRG/runner endpoints** (all use POST, not PUT — fw 3.14):
- `client.run_prg(data, *, fallback_on_404=True)` -- Load and RUN a PRG (resets C64 internally). When `fallback_on_404=True` (default) and the runner endpoint returns HTTP 404 (fw 3.14d wedged-runner symptom), the call transparently sideloads via `write_mem(load_addr, body)` using the PRG's first two header bytes as the load address (little-endian) and triggers via `send_text("RUN")` for load address `$0801` (BASIC-stub PRG — `SYS 2049` would execute the BASIC line-link bytes as opcodes) or `send_text("SYS <addr>")` for any other load address (pure ML). A `logging.warning` naming the trigger is emitted when the fallback fires. Pass `fallback_on_404=False` to surface the 404 as a plain `Ultimate64Error`.
- `client.load_prg(data)` -- Load a PRG into memory without running
- `client.run_crt(data)` -- Start a cartridge image
- `client.sid_play(data, songnr=0)` -- Play a .sid tune
- `client.mod_play(data)` -- Play a .mod file

**Keyboard injection:**
- `client.send_text(text, *, finish_with_return=True) -> None` -- PETSCII-encode `text` and write into the KERNAL keyboard buffer at `$0277` (count byte at `$00C6`); appends a CR (`0x0D`) when `finish_with_return=True`. Canonical for triggering `SYS <addr>` after `run_prg` lands at READY. Respects the buffer's 10-byte hardware limit by polling `$00C6` and waiting for the buffer to drain **to empty** (`$C6 == 0`) before writing each chunk at offset 0 — topping up a partially-full buffer races the KERNAL's dequeue (three HTTP round-trips apart) and can corrupt offset and count. Raises `Ultimate64Error` if the buffer never drains. (`Ultimate64Transport.inject_keys` uses the same drain-to-empty convention.)

**Memory (DMA-backed):**
- `client.read_mem(address, length) -> bytes` -- Raises `Ultimate64ProtocolError` when the device returns a payload shorter or longer than requested (prevents silently short/misaligned chunked reads).
- `client.write_mem(address, data)` -- DMA-backed write. Uses the legacy `PUT ?data=<hex>` form for payloads `<= self.write_mem_query_threshold` bytes, the `POST` raw-byte form above. Threshold is per-instance and auto-detected at construction from `DeviceCapabilities.writemem_post_safe` (128 on firmware without the Temp-folder fix — C64U 1.1.0, or Ultimate-line < 3.15; 48 on Ultimate-line ≥ 3.15); override via the `write_mem_query_threshold=` constructor kwarg.

**Config:**
- `client.get_version() -> dict`
- `client.get_info() -> dict`
- `client.list_configs() -> list[str]`
- `client.get_config_category(name) -> dict`
- `client.get_config_item(category, item) -> dict` -- the **item's own map**, unwrapped from the REST envelope (issue #214): `{'current': 'External', 'values': ['Auto', 'Internal', 'External', 'Manual'], 'default': 'Auto'}`. The key set is the item's type (firmware `emit_store`): `"values"` = enum (never empty), `"presets"` = preset-file item such as `Cartridge` (may be empty), `"min"`/`"max"`/`"format"` = integer range, only `"current"`/`"default"` = free string — so "no `values` key" means non-enum, never empty enum. Names resolve the way the firmware matches them (exact, then case-insensitive; a `*`/`?` glob only when it matches exactly one category and one item). Raises `Ultimate64ProtocolError` if the item is absent (message lists the keys present), a glob matches several, or the envelope's `errors` is non-empty; an unknown category is `Ultimate64ProtocolError` on stock firmware (HTTP 200, no category key) but a plain `Ultimate64Error` with `status == 404` on the 3.15 fork.
- `client.get_config_value(category, item) -> Any` -- the item's `"current"` value; the accessor for snapshot-then-restore (`prev = get_config_value(...)`, `set_config_item(..., prev)` puts the original back).
- `client.get_config_choices(category, item) -> list[str]` -- `"values"` of an enum item, else `"presets"` of a preset-file item (may be `[]`); a string or range item **raises** `Ultimate64ProtocolError` rather than answering `[]`, so an `if allowed and prev not in allowed` guard cannot be switched off silently.
- `client.get_config_item_raw(category, item) -> dict` -- the untouched envelope `{'<category>': {'<item>': {...}}, 'errors': []}` for callers that had adapted to it, and the only accessor for a multi-match pattern.
- `client.set_config_item(category, item, value) -> None` -- `PUT /v1/configs/<category>/<item>?value=<value>`; the call for one-off items such as `("C64 and Cartridge Settings", "Cartridge Preference", "External")`. Volatile until `save_config_to_flash()`.
- `client.set_config_items(category, items_dict)` -- iterates per-item (no batch endpoint)
- `client.save_config_to_flash() -> None` -- `PUT /v1/configs:save_to_flash` (DESTRUCTIVE). Config PUTs are otherwise volatile; a reboot/power-cycle reloads flash, not the RAM-side value.
- `client.load_config_from_flash(category=None) -> None` -- `PUT /v1/configs:load_from_flash` (DESTRUCTIVE). Discards unsaved in-memory changes. With `category` given, reloads only that category (`PUT /v1/configs/<category>:load_from_flash`; path depth >1 is HTTP 400).

### `ultimate64_helpers` key functions
```python
from c64_test_harness.backends.ultimate64_helpers import (
    set_turbo_mhz, get_turbo_mhz, get_turbo_enabled,
    set_reu, get_reu_config,
    set_badline_timing, get_badline_timing,
    set_bus_operation_mode, get_bus_config,
    snapshot_state, restore_state,
    reset, reboot,
    recover, runner_health_check,
    check_measurement_environment,
    run_prg_file, load_prg_file,
)
```

- `set_turbo_mhz(client, mhz)` -- Set turbo to given MHz (int), or `None` to disable. Probes the device's CPU-Speed presets (once, cached per client) and raises `ValueError` locally for a generation-foreign speed when the probe is conclusive; an inconclusive probe preserves the legacy firmware-side HTTP 400 rejection.
- `get_turbo_mhz(client) -> int | None` -- Current speed, or None if turbo off
- `max_cpu_speed_mhz(client) -> int` -- The device's maximum turbo speed from the same preset probe (64 on C64U fw 1.1.0, 48 on U64E fw 3.14; 48 fallback when inconclusive). Backs `Ultimate64Transport.set_speed(None)`. Module-level export, not re-exported from the package root.
- `set_reu(client, enabled, size=None)` -- Enable/disable REU; size as str ("512 KB") or int (MB). Cross-generation: probes the device's `Cartridge` presets and writes `Cartridge: "REU"` only where offered (U64E fw 3.14 yes; U64E 3.15 no — `Cartridge` is a `.crt` file chooser there and the REU is controlled by `RAM Expansion Unit` alone; C64 Ultimate no — its Cartridge value mirrors REU state and rejects the write with HTTP 400). The preset write is ordered first so a rejection never half-enables the REU. `restore_state` applies the same probe to the snapshotted cartridge value.
- `set_badline_timing(client, enabled)` / `get_badline_timing(client) -> bool` -- VIC-II badline DMA, which costs the 6510 ~20-25% of its cycles at 1 MHz. Disabling it is the clean way to measure badline cost while holding the PRG byte-identical (vs `$D011` blanking, which changes the shipped image). **Runtime-only state that persists until power cycle on a queue-shared device** — a run that disables badlines and dies leaves every later run quietly ~20-25% fast. `snapshot_state`/`restore_state` cover it and `check_measurement_environment` fails closed on it. Live-verified on U64E fw 3.14d (`"Enabled"`/`"Disabled"`); the C64U spelling is **unverified** — the helpers raise rather than silently no-op if the item is absent. See issue #150.
- `get_bus_config(client) -> BusConfig` / `set_bus_operation_mode(client, mode)` -- Cartridge-port `Bus Operation Mode` (`"Quiet"`, `"Writes"`, `"Dynamic"`, `"Dyn. & Writes"`; default `"Quiet"`) plus the four read-only `Bus Sharing - *` values in `BusConfig.sharing`. A plausible input to the REU-DMA-bound wall-clock floor, so record `get_bus_config()` alongside any benchmark artifact. Same runtime-only caveat as the REU helpers. See issue #145.
- `snapshot_state(client) -> U64StateSnapshot` -- Capture turbo + REU + cartridge + badline + bus-mode state. The two newest fields default to `""`, and `restore_state` skips empty values, so snapshots taken before they existed still restore.
- `restore_state(client, snap)` -- Restore a snapshot. Orders the `Cartridge` item first in its config batch — the same abort-before-half-applied invariant as `set_reu` (a firmware rejection of the Cartridge write aborts the batch before the REU is half-enabled).
- `reset(client)` -- Soft reset (CPU only)
- `reboot(client)` -- Full FPGA reboot (clears DMA state, ~8s settle)
- `recover(client, *, reset_settle_seconds=2.0, reboot_settle_seconds=12.0, escalate_to_reboot=True) -> str` -- Escalate `reset()` -> probe -> `reboot()` -> probe; returns `"reset"` or `"reboot"`. Raises `Ultimate64UnreachableError` on total failure. Never calls `poweroff()`.
- `runner_health_check(client) -> None` -- Post a tiny no-op PRG; raises `Ultimate64RunnerStuckError` on the firmware's "Cannot open file" wedged-runner signature.
- `check_measurement_environment(client) -> None` -- Assert turbo is off (1 MHz) **and** badline DMA is enabled; raises `Ultimate64MeasurementEnvironmentError` if a prior session left either dirty. Turbo is checked first. The badline check is skipped (not failed) when the device does not expose `Badline Timing`, since an unreadable item is not evidence of a dirty environment. Call before any CIA-timer benchmark. See GitHub issues #102 and #150.

### `ultimate64_schema` constants
- `CPU_SPEED_VALUES` -- tuple of 17 speed enum strings (" 1" through "48", plus "64") — the cross-generation superset; a given device offers a subset (probed at runtime by the helpers above)
- `CPU_SPEED_BY_MHZ` -- dict mapping int MHz to enum string
- `REU_SIZE_VALUES`, `REU_ENABLED_VALUES`, `TURBO_CONTROL_VALUES`

---

## Module: snapshot

Cross-backend VICE/U64 snapshot interop using VICE's native `.vsf` format as the on-disk wire. **Phase A** (PR #115 / commit 45a5844): RAM + CPU port round-trip — explicitly RAM + color RAM, I/O window excluded on restore: extract captures I/O-view bytes for `$D000-$DFFF`, but restore skips that window except color RAM `$D800-$DBFF` (blind register writes could fire spurious REU DMA via `$DF01`), so the round-trip guarantee is `$0000-$CFFF` + `$D800-$DBFF` + `$E000-$FFFF`. **REU layer** (issue #134): `reu_size_bytes` + `reu_contents` capture/restore, live-validated byte-exact on C64U fw 1.1.0 (2026-07-21). Later phases will add CIA/VIC/SID register state, drive images, and cartridge bytes — see `docs/snapshot_interop.md` for the per-layer asymmetry matrix and the U64-side limitations (REST cannot read cart bytes / disk images / REU memory back; 6510 registers aren't directly observable; 28 of 32 SID registers are write-only on hardware).

### `Snapshot` (frozen dataclass)
- `.ram: bytes` -- 64 KB image as seen through the CPU view (`$D000-$DFFF` holds I/O-view bytes, not RAM under I/O)
- `.cpu_port_data: int` -- value at `$0001`
- `.cpu_port_dir: int` -- value at `$0000`
- `.exrom: int` (default 1), `.game: int` (default 1) -- cartridge control lines
- `.reu_size_bytes: int | None`, `.reu_contents: bytes | None` -- REU layer; travels only in the sidecar bundle (`reu.bin`), never in the `.vsf`

### Functions
- `extract_snapshot(transport, *, include_reu=False, reu_size_bytes=None, reu_settle=0.05) -> Snapshot` -- Reads RAM + CPU port via the protocol; `include_reu=True` adds the staging-window REU extract (`reu_size_bytes=None` auto-detects from the U64 config). Same code path for VICE and U64. Re-exported from the package root.
- `restore_snapshot(transport, snap, *, override_memory_policy=True, restore_reu=True) -> None` -- Writes RAM back in three slices (`$0000-$CFFF`, color RAM `$D800-$DBFF`, `$E000-$FFFF` — the live I/O window is skipped, see above) plus the CPU port; when `snap.reu_contents` is present, enables the REU via generation-aware `set_reu` and writes contents via `Ultimate64Transport.socket_dma_reu_write` (SocketDMA `REUWRITE` — **no REST fallback**; unavailable DMA service raises `Ultimate64Error`, VICE-shaped transports raise `SnapshotRestoreError`; `restore_reu=False` opts out). Logs one WARNING per restore and threads `override="snapshot-restore"` through every `write_memory` so the bulk restore crosses `MemoryPolicy`-reserved regions without raising. Re-exported from the package root.
- `extract_reu_contents(transport, size_bytes, *, settle=0.05, pause=False) -> bytes` -- Staging-window REU readback ($0800–$87FF, 32 KB banks via REC-programmed REU→C64 transfers). **Must run unpaused on Ultimate hardware** — `machine:pause` freezes the machine clock including REC DMA (live-verified C64U fw 1.1.0), so a paused extract returns stale RAM. Capture is therefore not atomic.
- `Snapshot.to_bundle(path)` / `Snapshot.from_bundle(path)` -- Sidecar directory round-trip: `snapshot.vsf` + `manifest.json` + `reu.bin`.
- `Snapshot.to_vsf() -> bytes` -- Emit a complete VICE-consumable `.vsf`. A bundled ~180 KB template captured from VICE 3.10 at BASIC READY supplies the ~30 modules VICE 3.10 requires (MAINCPU, CIA1/2, SID, VIC-II, GLUE, drives, joyports, ...); the codec overwrites only the C64MEM module body.
- `Snapshot.from_vsf(data: bytes) -> Snapshot` -- Parse a VICE-emitted `.vsf` back into RAM + CPU port.
- `SnapshotFormatError` -- Raised on malformed `.vsf` input.

### Naming history
The top-level helpers were originally named `extract_state` / `restore_state`. They were renamed in PR #126 (commit 373da4e) to `extract_snapshot` / `restore_snapshot` to avoid colliding with the U64 helper `snapshot_state` / `restore_state` in `ultimate64_helpers` — a different object that captures turbo + REU + cartridge config (not RAM). Both APIs coexist.

---

## Module: progress

Backend-agnostic live memory watcher ("pexpect for DMA"). Polls memory addresses and yields `ProgressEvent` instances (kinds: `Advanced`, `Stalled`, `Finished`) until a sentinel matches or a timeout fires. Originally bound to `Ultimate64Client.read_mem`; lifted to the `C64Transport.read_memory` protocol in PR #123 (commit 9e6dd29).

- `watch_progress(transport, addresses, *, poll_interval=10.0, idle_timeout=120.0, overall_timeout=5400.0, stop_when=<never>) -> Iterator[ProgressEvent]` -- canonical entry point (defaults are tuned for hour-long DMA benches — pass `poll_interval` explicitly for anything interactive). Re-exported from the package root.
- `ProgressEvent` -- frozen dataclass with `.kind` ("Advanced" / "Stalled" / "Finished" / "Timeout" / "PollError"), `.elapsed`, `.changed` (dict of label → `(old, new)` byte deltas), `.values` (dict of label → current bytes), `.error`.

```python
from c64_test_harness import watch_progress

for event in watch_progress(
    target.transport,
    addresses={"sentinel": (0xC000, 1), "counter": (0xC001, 2)},
    poll_interval=0.5,
    overall_timeout=30.0,
    stop_when=lambda v: v["sentinel"] == b"\xFF",
):
    if event.kind == "Finished":
        break
```

A legacy `from c64_test_harness.backends.ultimate64_helpers import watch_progress` shim is preserved for backwards compatibility (it wraps a client in a tiny `read_memory` adapter). New code should use the top-level import.

---

## Module: backends.device_lock

### `DeviceLock`
Cross-process exclusive lock for hardware devices using `fcntl.flock()`. The key difference from `PortLock`: `acquire()` is blocking with a timeout, so multiple agents queue for the same device.

```python
from c64_test_harness import DeviceLock, DeviceLockTimeout

# Preferred: structured diagnostics on timeout.
lock = DeviceLock("192.168.1.81")
try:
    lock.acquire_or_raise(timeout=120.0)
except DeviceLockTimeout as e:
    # str(e) is a diagnosed-state message; e.holder_pid / e.pid_alive /
    # e.lockfile_age_seconds / e.device_reachable_rest are the fields.
    raise
try:
    ...
finally:
    lock.release()

# Bool API (legacy) is still supported and unchanged.
lock = DeviceLock("192.168.1.81")
if lock.acquire(timeout=30.0):
    try:
        ...
    finally:
        lock.release()
```

- `__init__(device_host, lock_dir=None, *, heartbeat_interval=15.0, allow_nested=False)` -- `heartbeat_interval` is the cadence (seconds) at which a daemon thread bumps the lockfile mtime while held, so queue-aware waiters see this holder as "progressing". `None`/`0`/negative disables the heartbeat (rarely useful outside unit tests that need deterministic mtime control).
- `acquire(timeout=30.0, *, progress_window=60.0) -> bool` -- Blocking acquire (polls with LOCK_NB every 0.1s). Writes JSON metadata (PID, timestamp, device_host). Verifies inode after flock. Starts the heartbeat thread on success.
  - **Queue-aware semantics (default).** `timeout` bounds time spent waiting on **wedged or dead** holders only. A live holder whose lockfile mtime is within `progress_window` seconds is "progressing"; the waiter's deadline is reset on every poll iteration. With the heartbeat, healthy holders stay "progressing" indefinitely. Pass `progress_window=None` for legacy hard-timeout behavior.
  - **Optional `watchdog` wakeup.** When the optional `c64-test-harness[notify]` extra is installed (`watchdog>=3.0`), queued acquirers wake on lockfile fs-events instead of waiting the full poll interval. The release path emits a cooperative `os.utime(lockfile)` so other waiters wake immediately. The 100 ms polling backstop is preserved for kernel-released flocks (`kill -9` holders where `release()` never ran and no fs-event fires).
- `acquire_or_raise(timeout=30.0, *, progress_window=60.0) -> None` -- Wraps `acquire()` and raises `DeviceLockTimeout` on timeout with structured diagnostics (holder PID, liveness, lockfile age, REST reachability). Prefer this over `acquire()` for live tests.
- `release()` -- Release flock and stop the heartbeat thread. Does NOT delete lockfile (inode race safety, same as PortLock). Touches lockfile mtime via `os.utime` to wake `watchdog`-based waiters cooperatively (best-effort).
- `read_info() -> dict | None` -- Read metadata without locking (diagnostics).
- `queue_depth -> int | None` -- Property, lazily computed. Number of **live** waiters currently blocked in `acquire()` for this device (holder not counted). `0` = empty queue; `None` = unobservable (sidecar path unreadable). Read-only: never touches the flock.
- `peek_queue_depth(device_host, lock_dir=None) -> int | None` -- Class method; same semantics as `queue_depth` but needs no instance and no lock — the pre-flight "should I even try to queue?" check for CI bots. Mechanism: each waiter registers an intent file in a `<lockfile>.queue/` sidecar directory before blocking (removed on exit from `acquire()`); dead-PID entries are excluded and garbage-collected, so crashed waiters don't inflate the count.
- `cleanup_stale(lock_dir=None) -> int` -- Class method; removes lockfiles from dead PIDs.
- `held_by_this_process(device_host, lock_dir=None) -> bool` -- Class method; whether *this OS process* holds the device lock (dict lookup — no filesystem, no network). Backs the advisory check below.
- `foreign_holder(device_host, lock_dir=None) -> dict | None` -- Class method; metadata for **another live process** holding the device, else `None`. Liveness comes from a non-blocking shared `flock` probe, not the lockfile contents, so the leftover lockfile `release()` keeps is correctly reported as "not held".
- `allow_nested=True` (constructor) -- acquire joins an existing hold by this same process (refcounted) instead of blocking on the flock. For re-entering the library while already holding the lock — a pytest fixture holding it while the test calls `create_manager()`. Default `False` keeps two in-process `DeviceLock` instances contending exactly as two processes do. Two worker threads sharing a device are concurrent users, not nested ones: don't use it there.
- `.device_host` / `.held` properties
- Context manager support

Lockfiles at `$XDG_RUNTIME_DIR/c64-test-harness/device-{sanitized_host}.lock`. Same directory as PortLock. Kernel auto-releases locks on process exit (crash-safe).

### `DeviceLockTimeout(TimeoutError)`
Raised by `DeviceLock.acquire_or_raise()` and by `_LockedU64Manager.acquire()` (i.e. `create_manager(backend="u64", ...)`) on lock-acquire timeout. Exported from the top-level package.

Fields:
- `.device_host: str`
- `.holder_pid: int | None` — PID from the lockfile metadata, or None if unreadable.
- `.pid_alive: bool | None` — whether the holder PID is still alive (`os.kill(pid, 0)`).
- `.lockfile_age_seconds: float | None` — seconds since the lockfile mtime was last bumped.
- `.device_reachable_rest: bool | None` — quick `GET /v1/version` probe (3 s budget); `True` on 2xx, `False` on connection/timeout failure, `None` if the probe was skipped.
- `.timeout: float` — the timeout passed to `acquire_or_raise`.
- `.progress_window: float | None`

`str(e)` produces one of four stable diagnosed-state phrases — "queued behind live, progressing PID X" / "holder PID X is alive but the lockfile hasn't been touched in Ns; holder may be wedged" / "stale lock from dead PID X; will be cleaned on next acquire — retry" / "no holder metadata found" — suffixed with "; device REST API responsive" or "; device REST API unreachable". See PATTERNS § "Pattern 9a: Queueing for the U64" for the worked example, branch table, and anti-patterns.

**When to use:** `UnifiedManager` uses `DeviceLock` automatically for U64 backends. Use directly only when creating `Ultimate64Transport` outside of `UnifiedManager` (e.g., in pytest fixtures for live tests).

### `advisory_lock_check(device_host, operation, *, lock_dir=None, logger=None)`
Advisory enforcement of the shared-device contract (issue #136). Called by `Ultimate64Client` before every non-GET request and by `SocketDMAClient` before every destructive opcode; call it directly only when adding a new transport that mutates a device.

- This process holds the lock → silent.
- Nobody holds it → `DEBUG` line. **Single-user flows never see a warning.**
- Another live process holds it → `WARNING` naming the holder PID, once per holder (not once per request).
- `U64_REQUIRE_DEVICE_LOCK=1` → raises `DeviceLockContentionError` instead, before the request goes out.

Filesystem-only (one `open` + one non-blocking `flock` + one small read), never networked, and swallows its own errors — an advisory check must never be why a device call fails.

### `DeviceLockContentionError(RuntimeError)`
Raised by `advisory_lock_check` under `U64_REQUIRE_DEVICE_LOCK=1`. Fields: `.device_host`, `.holder_pid`. Deliberately **not** an `Ultimate64Error` subclass — a caller that blanket-catches device errors to retry must not swallow it, because the fix is to acquire the lock, not to retry.

### Live-test fixture: `device_lock_guard`
Autouse fixture in `tests/conftest.py`. Holds the device lock around every `*_live.py` test for the host it resolves (module `_HOST`/`HOST`/`U64_HOST` attribute, else the `U64_HOST` env var), logging acquire and release to `c64_test_harness.tests.device_lock`. Non-live tests return immediately. `U64_DEVICE_LOCK_TIMEOUT` overrides the 300 s queue timeout. New live tests need no locking code of their own; a live test that still acquires its own `DeviceLock` must pass `allow_nested=True`.

---

## Module: backends.ultimate64_probe

### `ProbeResult` (frozen dataclass)
- `.host: str`, `.port: int`
- `.reachable: bool` -- True if all executed checks passed
- `.ping_ok: bool | None` -- None if skipped
- `.port_ok: bool | None` -- None if skipped
- `.api_ok: bool | None` -- None if skipped
- `.latency_ms: float | None` -- Fastest successful check
- `.error: str | None` -- Human-readable failure message, None on success
- `.summary: str` -- One-line status string (property)

### Functions

- `ping_host(host, timeout=2.0) -> tuple[bool, float | None]` -- ICMP ping via subprocess
- `check_port(host, port=80, timeout=2.0) -> tuple[bool, float | None]` -- TCP connect
- `check_api(host, port=80, timeout=3.0, password=None) -> tuple[bool, dict | None]` -- GET /v1/version
- `probe_u64(host, port=80, password=None, ping_timeout=2.0, tcp_timeout=2.0, api_timeout=3.0, skip_ping=False, skip_api=False) -> ProbeResult` -- Full probe, fail-fast
- `is_u64_reachable(host, port=80, password=None) -> bool` -- Quick boolean check

**Fail-fast:** If ping fails, TCP and API checks are skipped. If TCP fails, API is skipped.

**Integration:** `Ultimate64InstanceManager.acquire()` calls `probe_u64(..., skip_api=True)` before creating a transport. Unreachable devices are rotated to the end of the pool and the next device is tried. If all devices fail, raises `Ultimate64PoolExhaustedError` with collected probe errors.

### Error message examples
- `"U64 at 192.168.1.81 unreachable (ping failed, timeout 2.0s)"`
- `"U64 at 192.168.1.81 port 80 not responding (TCP connect failed, timeout 2.0s)"`
- `"U64 at 192.168.1.81 API not responding (GET /v1/version failed: <reason>)"`

---

## Module: backends.ultimate64_manager

### `Ultimate64InstanceManager`
Pool-based manager for multiple Ultimate 64 devices. Analogous to `ViceInstanceManager` but allocates from a fixed device list. Thread-safe with blocking acquire.
```python
from c64_test_harness import (
    Ultimate64Device, Ultimate64InstanceManager, Ultimate64PoolExhaustedError,
)

devices = [Ultimate64Device(host="10.0.0.10"), Ultimate64Device(host="10.0.0.11")]
with Ultimate64InstanceManager(devices, acquire_timeout=30.0) as mgr:
    with mgr.instance() as inst:
        inst.transport  # Ultimate64Transport
        inst.device     # Ultimate64Device
        inst.pid        # Always None (hardware)
```

### `Ultimate64Device` (frozen dataclass)
- `.host: str`, `.password: str | None`, `.port: int` (default 80), `.timeout: float` (default 10.0), `.name: str`
- `.label -> str` — human-readable name (falls back to host)

### `Ultimate64Instance`
- `.device: Ultimate64Device`, `.transport: Ultimate64Transport`, `.pid -> None`
- `.stop()` — close transport (idempotent)

### Exceptions
- `Ultimate64ManagerError` — base class
- `Ultimate64PoolExhaustedError` — all devices busy or unreachable

---

## Module: backends.u64_socket_dma

Binary "SocketDMA" command channel on TCP port 64, distinct from REST. Covers capabilities REST lacks (keyboard inject, REU write, raw reset, DMA load/jump) and is the transport behind the `Ultimate64Transport.socket_dma` bulk-write fast path. Wire format: 2-byte LE opcode + 2-byte LE length + payload; mostly fire-and-forget (no reply — confirmation is the connection staying open), so pair every write with a read-back where correctness matters. Commands on one connection are serviced strictly in order, so a replying command (IDENTIFY) doubles as a completion barrier — `reu_write` does this by default, and the transport's `DMAWRITE` bulk-write fast path finishes with the same barrier before its tail read-back sanity check. On the C64 Ultimate the service ships disabled (enable Network Settings → "Ultimate DMA Service"); U64E fw 3.14 availability untested. Both classes are package-root exports.

### `SocketDMAClient(host, port=64, password=None, timeout=5.0)`
Context manager — `with` opens one connection reused across commands; outside `with`, each call opens/closes its own (slow for chains; every connect re-authenticates when a network password is set).
- `identify() -> dict` — `{"title": "*** C64 Ultimate (V1.49) 1.1.0 ***"}`-style; cheap protocol/liveness probe
- `dma_write(addr, data)` — raw memory write (opcode 0xFF06), no autostart; payload ≤ 65535 bytes/command
- `dma_load(addr, data, run=False)` — PRG-style load (0xFF01), `run=True` → DMARUN (0xFF02) autostart
- `dma_jump(addr)` — 0xFF09
- `reu_write(offset, data, *, sync=True)` — 0xFF07; 24-bit LE offset, ≤16 MB REU space; chunks transparently at `REU_WRITE_MAX_CHUNK` (65 532) data bytes/command. `sync=True` ends with an in-band IDENTIFY completion barrier — REUWRITE has no per-command ack and the C64U fw 1.1.0 drains large bursts erratically (0.4–19 s live-measured for 96 KiB), so without the barrier an immediate read-back sees stale contents; the barrier's recv timeout scales with payload size. No REST readback exists on any firmware — extraction goes through the snapshot staging window (issue #134, wired).
- `inject_keys(text)` — 0xFF03; firmware DMAs into the 10-byte keyboard buffer
- `reset()` — 0xFF04; recoverable, menu-equivalent
- `authenticate()` — 0xFF1F; required first on password-protected devices (fw 3.12+)
- Raises `Ultimate64Error` on connect/send/recv failure

### `SocketDMAIdentifyUDP`
- `identify(host="<broadcast>", timeout=2.0, port=64, probe=b"json") -> list[dict]` — static; UDP/64 discovery, one dict per responding device; JSON reply with `probe=b"json"`, else `"<echo>,<hostname>,<menu_header>"`. Governed by the separate "Ultimate Ident Service" config item on the C64U (TCP identify works even with it disabled).

---

## Module: sid

### `SidFile`
Parsed PSID/RSID file with all header fields and the raw bytes.
```python
from c64_test_harness import SidFile

sid = SidFile.load("tune.sid")        # or SidFile.from_bytes(raw)
sid.name          # str — title from header
sid.author        # str
sid.songs         # int — number of sub-tunes
sid.init_addr     # int — 6502 init entry point
sid.play_addr     # int — 6502 play entry point (0 = uses IRQ)
sid.c64_data      # bytes — just the 6502 code/data
sid.effective_load_addr  # int — resolved load address
sid.song_is_60hz(0)      # bool — True if CIA-timed (60 Hz)
```

### `build_test_psid(load_addr=0x1000, init_code=b"", play_code=b"", name="TEST", author="HARNESS", released="2026", version=2, songs=1) -> bytes`
Build a minimal PSID v2 binary for testing.

### Exceptions
- `SidError` — base
- `SidFormatError` — parse failure (bad magic, truncated header, etc.)

---

## Module: sid_player

Cross-backend SID playback dispatcher.

- `play_sid(transport, sid, song=0, stub_addr=0xC000)` — dispatches to VICE or U64 based on transport type
- `play_sid_vice(transport, sid, song=0, stub_addr=0xC000)` — VICE: loads SID data, installs IRQ stub, calls init via `jsr()`
- `play_sid_ultimate64(transport, sid, song=0)` — U64: uses native `POST /v1/runners:sidplay` endpoint
- `stop_sid_vice(transport)` — restores KERNAL IRQ vector ($EA31) on VICE
- `build_vice_stub(play_addr, stub_addr=0xC000) -> bytes` — builds 18-byte IRQ installer + wrapper
- `DEFAULT_STUB_ADDR = 0xC000`
- `SidPlaybackError` — raised on dispatch or execution failure

**Key gotcha:** After `jsr()` installs the stub/runs init, the play routine's PC must jump back to BASIC warm-start (`JMP ($A002)`). `play_sid_vice` sets PC explicitly rather than relying on whatever `jsr()` leaves behind; before `preserve_state` that was load-bearing, since the CPU would otherwise run into stale NOPs or hit BRK, resetting IRQ vectors and killing playback. It is now belt and braces, and still correct.

---

## Module: backends.render_wav

Headless VICE audio capture to WAV files.

### `render_wav(prg_path, out_wav, duration_seconds, sample_rate=44100, mono=True, pal=True, config=None, timeout=None) -> RenderResult`
Launch VICE with `-sounddev wav`, run for the specified duration via `-limitcycles`, write WAV.

### `RenderResult` (dataclass)
- `.wav_path: Path`, `.pid: int | None`, `.exit_code: int`
- `.duration_seconds: float`, `.cycles: int`, `.sample_rate: int`

### Constants
- `PAL_CLOCK_HZ = 985248`
- `NTSC_CLOCK_HZ = 1022727`

---

## Module: backends.render_wav_u64

Capture SID audio from Ultimate 64 hardware to WAV via UDP audio stream.

### `capture_sid_u64(client, sid, out_wav, duration_seconds, song=0, sample_rate=48000, listen_port=11001, ...) -> U64CaptureResult`
End-to-end: configure U64 audio stream destination, play SID, capture UDP packets, write WAV.

### `U64CaptureResult` (dataclass)
- `.wav_path: Path`, `.duration_seconds: float`, `.sample_rate: int`
- `.total_samples: int`, `.packets_received: int`, `.packets_dropped: int`

---

## Module: backends.u64_audio_capture

Low-level UDP audio stream receiver for Ultimate 64.

### `AudioCapture`
Background-thread UDP receiver.
```python
from c64_test_harness import AudioCapture

cap = AudioCapture(port=11001, sample_rate=48000)
cap.start()
# ... play SID, wait ...
result = cap.stop(wav_path="output.wav")  # -> CaptureResult
```

### `CaptureResult` (dataclass)
- `.wav_path: Path`, `.duration_seconds: float`, `.sample_rate: int`
- `.total_samples: int`, `.packets_received: int`, `.packets_dropped: int`

### `write_wav(path, pcm_data, sample_rate=48000, channels=2, sample_width=2) -> Path`
Write raw PCM data to a WAV file.

### Constants
- `DEFAULT_AUDIO_PORT = 11001`
- `DEFAULT_SAMPLE_RATE = 48000`
- `CHANNELS = 2` (stereo)
- `SAMPLE_WIDTH = 2` (16-bit)

---

## Module: backends.u64_debug_capture

Cycle-accurate 6510/VIC bus trace capture from U64 debug stream over UDP.

**Rate cap — read this before designing any turbo-speed test that relies on the trace.** The U64E FPGA emits the debug stream at a fixed rate of roughly **~850k entries/sec** (≈ 2,400 UDP packets/sec) regardless of CPU turbo speed. This matches the native 6510 rate at 1 MHz, so at 1 MHz the trace is essentially complete. At higher turbo speeds you get a **uniformly sampled 1/N view** of the real bus (1/4 of cycles at 4 MHz, 1/48 at 48 MHz). `packets_dropped` stays at zero at every speed because the rate limit is at the source, not in the UDP path. Drop to `set_turbo_mhz(client, 1)` for the capture window if you need a complete trace; turbo-speed capture is only sound for uniform-sample aggregate statistics (PC-hit distribution, frequency maps). Measurement lives in `tests/test_u64_debug_stream_speed_live.py`.

### `BusCycle` (frozen dataclass)
Parsed 32-bit bus cycle entry. Properties:
- `.is_cpu -> bool` / `.is_vic -> bool` — PHI2 clock phase
- `.is_read -> bool` / `.is_write -> bool` — R/W# line
- `.address -> int` — 16-bit address bus
- `.data -> int` — 8-bit data bus
- `.irq -> bool`, `.nmi -> bool` — True when asserted (active-low firmware signals at bits 26/25 inverted).
- `.ba -> bool` — True when bus is available to the CPU (active-HIGH at bit 27; **NOT** inverted). Fixed in PR #125 (0ff4e15); earlier harness versions read bit 28 incorrectly.
- `.game -> bool`, `.exrom -> bool` — cartridge control lines (active-low bits 30/29 inverted).
- `.cart_rom_active -> bool` — True when at least one cartridge ROM line is asserted (firmware-derived `not (ROMH# AND ROML#)` at bit 28, active-HIGH; the firmware already applies the negation). Preferred name introduced in PR #125. `.rom` is preserved as a backwards-compatible alias for the same bit.
- `.raw -> int` — original 32-bit word

### `DebugCapture`
Background-thread UDP receiver. Same pattern as `AudioCapture`.
```python
from c64_test_harness import DebugCapture

cap = DebugCapture(port=11002)
cap.start()
# ... run code on the C64 ...
result = cap.stop()  # -> DebugCaptureResult

for cycle in result.trace:
    if cycle.is_cpu and cycle.is_write and cycle.address == 0xD020:
        print(f"Border color write: {cycle.data}")
```

Accumulates raw bytes in the recv loop; parses into `BusCycle` objects on `stop()` for performance at ~32 Mbps.

**Classmethod constructor for per-routine FPGA refresh:**
- `DebugCapture.with_fresh_fpga(client, *, capture_kwargs=None, reboot_settle_seconds=12.0) -> DebugCapture` -- Calls `client.reboot()`, sleeps `reboot_settle_seconds` (default 12.0s, matches `recover()`'s reboot-settle), then constructs and returns a fresh `DebugCapture` instance with `**(capture_kwargs or {})` forwarded to `__init__` (e.g. `port`, `multicast_group`, `max_bytes`, `filter`). Caller still has to call `.start()`. Use this before each routine in a multi-routine bench to recover from the FPGA UDP-stream rate degradation that builds up under sustained workload (issue #81). Never calls `poweroff()` — `reboot()` is the right primitive for clearing FPGA state.

### `DebugCaptureResult` (dataclass)
- `.trace: list[BusCycle]`, `.duration_seconds: float`
- `.packets_received: int`, `.packets_dropped: int`, `.total_cycles: int`

### Constants
- `DEFAULT_DEBUG_PORT = 11002`
- `ENTRIES_PER_PACKET = 360`

### Debug stream modes (set via config helpers)
- `DEBUG_MODE_6510 = "6510 Only"` — 6510 CPU cycles only
- `DEBUG_MODE_VIC = "VIC Only"` — VIC access cycles only
- `DEBUG_MODE_6510_VIC = "6510 & VIC"` — interleaved, distinguished by `cycle.is_cpu`
- `DEBUG_MODE_1541 = "1541 Only"` — 1541 drive CPU
- `DEBUG_MODE_6510_1541 = "6510 & 1541"` — interleaved

---

## Module: backends.u64_video_capture

VIC-II video frame capture from U64 video stream over UDP.

### `VideoFrame` (frozen dataclass)
An assembled frame with 1-byte-per-pixel color indices (0-15).
- `.frame_number: int`, `.width: int`, `.height: int`
- `.pixels: bytes` — `width × height` bytes, VIC-II color indices
- `.pixel_at(x, y) -> int` — color at position
- `.row(y) -> bytes` — one row of pixel data

### `VideoCapture`
Background-thread UDP receiver. Assembles packets into complete frames.
```python
from c64_test_harness import VideoCapture, VIC_PALETTE

cap = VideoCapture(port=11000)
cap.start()
# ... wait for frames ...
result = cap.stop()  # -> VideoCaptureResult

for frame in result.frames:
    # Check border color at a known border pixel
    color_idx = frame.pixel_at(10, 10)
    r, g, b = VIC_PALETTE[color_idx]
```

PAL: 384×272 @ 50fps (68 packets/frame). NTSC: 384×240 @ 60fps. 4-bit packed pixels (2 per byte, low nibble first).

### `VideoCaptureResult` (dataclass)
- `.frames: list[VideoFrame]`, `.duration_seconds: float`
- `.packets_received: int`, `.packets_dropped: int`
- `.frames_completed: int`, `.frames_dropped: int`

### `VIC_PALETTE`
Tuple of 16 `(R, G, B)` tuples — standard VIC-II colors (index 0=black, 1=white, ..., 15=light grey).

### Constants
- `DEFAULT_VIDEO_PORT = 11000`

---

## Data Streams Configuration

Config helpers for the "Data Streams" category (in `ultimate64_helpers`).

### Functions
- `get_data_streams_config(client) -> dict[str, str]` — read all stream config items
- `set_stream_destination(client, stream_type, destination)` — set default dest for `"video"`, `"audio"`, or `"debug"`
- `get_debug_stream_mode(client) -> str` — current debug stream mode
- `set_debug_stream_mode(client, mode)` — set mode (validates against `DEBUG_MODES`)

### Constants
- `DEBUG_MODES` — tuple of 5 valid mode strings
- `CAT_DATA_STREAMS = "Data Streams"` — config category name

---

## Module: uci_network

Ultimate Command Interface (UCI) socket-level TCP/UDP networking for U64 Elite. Registers at `$DF1C-$DF1F`; firmware handles TCP/IP via lwIP. **Every builder and helper accepts `turbo_safe: bool = False`** — set to `True` on real U64E at speeds ≥ 4 MHz. See `docs/uci_networking.md` and Pattern 11 in `PATTERNS.md`.

### High-level helpers (take a `C64Transport`)
- `uci_probe(transport, *, timeout=10.0, turbo_safe=False) -> int` — returns `0xC9` if UCI present
- `uci_get_ip(transport, *, timeout=10.0, turbo_safe=False) -> str` — dotted-quad IP
- `uci_get_interface_count(transport, *, timeout=10.0, turbo_safe=False) -> int`
- `uci_tcp_connect(transport, host, port, *, timeout=10.0, turbo_safe=False) -> int` — returns socket handle
- `uci_udp_connect(transport, host, port, *, timeout=10.0, turbo_safe=False) -> int`
- `uci_socket_write(transport, socket_id, data, *, timeout=10.0, turbo_safe=False) -> None` -- `data` must be at most 892 bytes (`SOCKET_WRITE_MAX_BYTES`; empirical firmware ceiling, theoretical 893 truncates by one byte on the wire). For UDP, one call == one datagram (no firmware coalescing). Larger payloads must be split into multiple calls; each emits its own datagram. See `docs/uci_networking.md § Datagram size limits`.
- `uci_socket_read(transport, socket_id, max_len=255, *, timeout=10.0, turbo_safe=False) -> bytes` — `max_len` above `SOCKET_READ_MAX_BYTES` (253) raises `ValueError`; one call drains one reply block and returns only the payload
- `uci_socket_close(transport, socket_id, *, timeout=10.0, turbo_safe=False) -> None`
- `uci_tcp_listen_start(transport, port, *, timeout=10.0, turbo_safe=False) -> None`
- `uci_tcp_listen_state(transport, *, timeout=10.0, turbo_safe=False) -> int` — NOT_LISTENING / LISTENING / CONNECTED / BIND_ERROR / PORT_IN_USE (one listener per device; no handle argument)
- `uci_tcp_listen_socket(transport, *, timeout=10.0, turbo_safe=False) -> int`
- `uci_tcp_listen_stop(transport, *, timeout=10.0, turbo_safe=False) -> None`
- `get_uci_enabled(client) -> bool` / `enable_uci(client)` / `disable_uci(client)` — config-side helpers, take an `Ultimate64Client`

### 6502 code builders (return raw bytes — `load_code()` + `jsr()`)
All address arguments default to the `$C000` UCI block (`code_addr=0xC000`, data `$C100`, `resp_addr=0xC200`, `status_addr=0xC300`, `resp_len_addr=0xC3F0`, `stat_len_addr=0xC3F2`, `sentinel_addr=0xC3FE`, `error_addr=0xC3FF`); `turbo_safe` is a plain keyword, not keyword-only. Host/socket arguments are the RAM addresses the high-level helpers write the value to, not the value itself.
- `build_uci_probe(result_addr=0xC200, sentinel_addr=0xC3FE, code_addr=0xC000, turbo_safe=False) -> bytes`
- `build_uci_command(target=3, cmd=2, params=b"", resp_addr=..., status_addr=..., resp_len_addr=..., stat_len_addr=..., error_addr=..., sentinel_addr=..., code_addr=..., turbo_safe=False) -> bytes`
- `build_get_ip(result_addr=0xC200, ..., turbo_safe=False) -> bytes`
- `build_tcp_connect(host_addr=0xC100, port=80, result_addr=0xC200, ..., turbo_safe=False) -> bytes` — `host_addr` holds the 4-byte IP
- `build_udp_connect(host_addr=0xC100, port=53, ..., turbo_safe=False) -> bytes`
- `build_socket_write(socket_id_addr=0xC100, data_addr=0xC101, data_len_addr=0xC1FF, status_addr=0xC300, ..., turbo_safe=False) -> bytes`
- `build_socket_read(socket_id_addr=0xC100, result_addr=0xC200, max_len=255, actual_len_addr=0xC3F0, ..., turbo_safe=False) -> bytes`
- `build_socket_close(socket_id_addr=0xC100, ..., turbo_safe=False) -> bytes`

### Fence tuning (public constants)
- `UCI_FENCE_OUTER = 5` — outer-loop iterations (minimum: 3)
- `UCI_FENCE_INNER = 100` — inner-loop iterations (minimum: 122 at OUTER=3)
- `UCI_PUSH_SETTLE_ITERS = 0xFF` — post-`PUSH_CMD` settle iterations

### Register + protocol constants
- Registers: `UCI_DEVICE_REG`, `UCI_CONTROL_STATUS_REG`, `UCI_CMD_DATA_REG`, `UCI_RESP_DATA_REG`, `UCI_STATUS_DATA_REG`
- Status bits: `BIT_DATA_AV`, `BIT_STAT_AV`, `BIT_ERROR`, `BIT_CMD_BUSY`
- States: `STATE_BITS`, `STATE_IDLE`, `STATE_BUSY`, `STATE_LAST_DATA`, `STATE_MORE_DATA`
- Commands: `CMD_PUSH`, `CMD_NEXT_DATA`, `CMD_ABORT`, `CMD_CLR_ERR`
- Targets: `TARGET_DOS1`, `TARGET_DOS2`, `TARGET_NETWORK`, `TARGET_CONTROL`
- Net commands: `NET_CMD_IDENTIFY`, `NET_CMD_GET_INTERFACE_COUNT`, `NET_CMD_GET_NETADDR`, `NET_CMD_GET_IPADDR`, `NET_CMD_SET_IPADDR`, `NET_CMD_TCP_CONNECT`, `NET_CMD_UDP_CONNECT`, `NET_CMD_SOCKET_CLOSE`, `NET_CMD_SOCKET_READ`, `NET_CMD_SOCKET_WRITE`, `NET_CMD_TCP_LISTENER_START/STOP`, `NET_CMD_GET_LISTENER_STATE/SOCKET`
- Queue limits: `DATA_QUEUE_MAX`, `STATUS_QUEUE_MAX`
- Listener states: `NOT_LISTENING`, `LISTENING`, `CONNECTED`, `BIND_ERROR`, `PORT_IN_USE`
- ID: `UCI_IDENTIFIER = 0xC9`

### Exceptions
- `UCIError` — raised on protocol/timeout/error-bit conditions

---

## Module: bridge_ping

Bridge networking helpers — two VICE instances on a host bridge (Linux TAP + `br-c64`, or macOS `feth` + `bridge10`) talking L2 + IP + ICMP via CS8900a. See Pattern 8 in `PATTERNS.md`.

### High-level orchestrators (own the wall-clock deadline in Python; **VICE-only** — they drive the 6510 with `jsr()`)
- `run_ping_and_wait(transport, *, tx_frame, rx_buf, result_addr, identifier, sequence, tx_frame_buf, timeout_s=5.0, peek_addr=..., consume_addr=..., arp=True) -> int` — returns `0x01` on matched reply, `0xFF` on timeout. `arp=True` (default, #218) first transmits an ARP request derived from `tx_frame`'s source MAC / source IP / destination IP through the same buffer; `ValueError` if `tx_frame` is not IPv4 — pass `arp=False` to send it raw.
- `run_icmp_responder(transport, *, rx_buf, my_ip, result_addr, timeout_s=5.0, peek_addr=..., consume_addr=..., my_mac=None) -> int` — reply to any echo request addressed to `my_ip`; with `my_mac` also answers ARP requests for `my_ip` while waiting (re-polls on `RESULT_ARP_REPLY_SENT`)

### Frame + code builders
- `build_echo_request_frame(src_mac, dst_mac, src_ip, dst_ip, identifier=0x1234, sequence=1, payload=b"PING_FROM_C64") -> EchoRequest` — pass `.frame` (padded to 60 bytes, word-aligned) as `tx_frame`
- `build_arp_request_frame(src_mac, src_ip, target_ip) -> bytes` — broadcast "who has *target_ip*" (#218); `ARP_FRAME_LEN` = 60 bytes, RFC 826 at ip65's `ap_*` offsets. Feed to `build_bridge_tx_code` or `arp_frame_buf=` below
- `build_arp_reply_frame(src_mac, src_ip, target_mac, target_ip) -> bytes` — unicast reply; the host-side twin of what the responders emit
- `parse_arp(frame) -> ArpPacket | None` — `None` unless ethernet/IPv4 ARP (accepts the unpadded 42-byte packet). Both builders raise `ValueError` on a MAC/IP of the wrong length
- `build_bridge_tx_code(...)` — transmit a pre-built frame via CS8900a
- `build_rx_peek_code(...)` — bounded peek into RX FIFO (drives orchestrator polling)
- `build_rx_echo_reply_code(...)` — full-routine echo-reply match (legacy, virtual-cycle timing)
- `build_read_and_match_echo_reply_code(...)` — read a pending frame, match against expected reply
- `build_read_and_respond_echo_request_code(..., my_mac=None)` — read request, respond with reply in one pass; with `my_mac` an ARP request for `my_ip` is answered and the result is `RESULT_ARP_REPLY_SENT` (`0x03`, re-poll)
- `build_ping_and_wait_code(..., arp_frame_buf=None, arp_frame_len=None)` — legacy TX+RX combined, virtual-cycle timing; with `arp_frame_buf` the ARP frame there (length defaults to `ARP_FRAME_LEN`) is transmitted before the echo request in the same run, and the ARP reply is drained as a non-match (#218)
- `build_icmp_responder_code(..., my_mac=None)` — legacy responder, virtual-cycle timing; with `my_mac` answers ARP requests for `my_ip` while waiting, then keeps waiting for the echo
- ARP defaults (`None`) keep every builder byte-identical to its pre-#218 output (pinned by SHA-256 in `tests/test_cs8900a_arp.py`). ARP on makes them larger — consume 585 B, responder 630 B, TOD responder 754 B, ping-and-wait 319 B, TOD ping 443 B — so size code windows accordingly. Proven under VICE and on the simulated chip (`tests/cs8900a_sim.py`); a hardware pass is still owed.
- Init helpers (blob = ends in `RTS`; inline = no `RTS`, prepend to a routine). All enable the RR clockport first.
  - `cs8900a_enable_inline_code()` — RxCTL = `CS8900A_RXCTL_VALUE`, then LineCTL |= SerRxON|SerTxON; everything a fresh chip needs
  - `cs8900a_rxctl_inline_code(value=CS8900A_RXCTL_VALUE)` / `cs8900a_rxctl_code()`
  - `cs8900a_linectl_or_inline_code(mask=CS8900A_LINECTL_ENABLE)` / `cs8900a_write_linectl_code(lo_value, hi_value)` / `cs8900a_read_linectl_code(dest_addr)`
  - `cs8900a_set_mac_inline_code(mac)` / `cs8900a_set_mac_code(mac)` — program the Individual Address (PP `0x0158-0x015D`) from the 6510; the only route that reaches a hardware cartridge (issue #209). `ValueError` unless `len(mac) == 6`.
- Constants (import from `c64_test_harness.bridge_ping`; not package-root exports): `CS8900A_RXCTL_VALUE = 0x0D85` (PromiscuousA|RxOKA|IndividualA|BroadcastA + regnum), `CS8900A_RXCTL_VALUE_IP65 = 0x0D05` (same, non-promiscuous — ip65's value), `CS8900A_TXCMD_VALUE = 0x00C9`, `CS8900A_RXEVENT_MASK = 0x0D`, `CS8900A_LINECTL_ENABLE = 0x00C0`. The low 6 bits of every CS8900a control register are the read-only register number (issue #207) — the old `0x00D8`/`0x00C0` only ever worked under VICE.

### TOD-timed variants (shippable on real C64 / U64E / VICE normal; NOT usable under VICE warp)
- `build_ping_and_wait_tod_code(..., deadline_tenths=50, *, arp_frame_buf=None, arp_frame_len=None)` — same ARP-first option as the counter variant
- `build_icmp_responder_tod_code(..., deadline_tenths=50, *, my_mac=None)` — same ARP answer as the counter variant, against the same TOD deadline
- `build_rx_echo_reply_tod_code(...)`

### Dataclasses
- `EchoRequest` — `.frame`, `.identifier`, `.sequence`, `.payload`; what `build_echo_request_frame` returns
- `ArpPacket` — `.dst_mac`, `.src_mac`, `.opcode`, `.sender_mac`, `.sender_ip`, `.target_mac`, `.target_ip`, `.is_request`, `.is_reply`; what `parse_arp` returns. Package-root exports: `ArpPacket`, `build_arp_request_frame`, `build_arp_reply_frame`, `parse_arp`; `ARP_FRAME_LEN`, `RESULT_ARP_REPLY_SENT`, `ETHERTYPE_ARP` live in `c64_test_harness.bridge_ping`

**Test fixture:** `bridge_vice_pair` in `tests/conftest.py` brings up two VICE instances on `BRIDGE_NAME` (`br-c64` / `bridge10`), RR-Net mode, warp off, unique MACs, CS8900a initialised.

---

## Module: tod_timer

CIA1 TOD-based 6502 timeout helpers for **shippable C64 applications**. Works correctly on real C64, real U64 Elite (any turbo speed), and VICE normal. **NOT usable under VICE warp** — VICE TOD is virtual-CPU-clocked, not wall-clock (see gotcha #23 in `PATTERNS.md`).

Zero-page footprint: `$F0`-`$F5` (see gotcha #22 — don't interleave TOD reads with `bridge_ping` frame reads in one routine).

### Builders
- `build_tod_start_code(load_addr) -> bytes` — reset CIA1 TOD to 00:00:00.0 and start it
- `build_tod_read_tenths_code(load_addr, result_addr) -> bytes` — store elapsed tenths (LE16) at `result_addr`; `$FFFF` if > 59.9s elapsed
- `build_poll_with_tod_deadline_code(load_addr, peek_check_snippet, result_addr, deadline_tenths) -> bytes` — poll a device-specific "ready?" snippet with a TOD deadline; returns result byte + timeout sentinel

### Constants
- `MAX_DEADLINE_TENTHS = 599` — 59.9s cap (CIA1 TOD minutes field trips after this)

---

## Module: poll_until

Host-side wall-clock polling helper. Works in BOTH VICE normal and VICE warp because the deadline lives in Python.

### `poll_until_ready(transport, code_addr, result_addr, *, timeout_s=5.0, batch_timeout_s=5.0) -> int`
Calls a pre-loaded 6502 peek routine at `code_addr` repeatedly via `jsr`. Between calls, zeroes `result_addr`, runs the peek, then reads back the result byte. Loops until:
- Result is `0x01` → success, returns `0x01`
- Result is any non-`0xFF` value → device-specific sentinel, returned as-is
- Wall-clock has passed `timeout_s` → returns `0xFF`

Caller is responsible for loading the peek routine before calling. The peek routine should be bounded (a few hundred 6502 cycles) — `build_rx_peek_code` is one example.

---

## Module: runner

Scenario-based sequential test runner with recovery functions.

### `TestRunner`
- `runner.add_scenario(name, run_fn, recovery_fn=None)` — `run_fn: () -> (bool, str)`, `recovery_fn: () -> bool`
- `runner.run_all() -> list[TestResult]`
- `runner.results -> list[TestResult]`
- `runner.all_passed -> bool`
- `runner.exit_code -> int`
- `runner.print_summary()`

### `TestScenario` — dataclass for a single scenario
### `TestResult` — dataclass with `.status: TestStatus` and metadata
### `TestStatus` — enum: `PASS`, `FAIL`, `ERROR`, `SKIP`

---

## Module: encoding

C64 character encoding tables. Imported as `c64_test_harness.encoding`.

### Screen codes
- `SCREEN_CODE_TABLE` — tuple of 256 unicode chars indexed by C64 screen code
- `screen_code_to_char(code) -> str`

### PETSCII
- `char_to_petscii(ch) -> int | None`
- `register_petscii(ch, code)` — extend the encoder with project-specific chars
- Named codes: `PETSCII_RETURN`, `PETSCII_HOME`, `PETSCII_CLR`, `PETSCII_DEL`, `PETSCII_F1/F3/F5/F7`, `PETSCII_CRSR_{DOWN,RIGHT,UP,LEFT}`, `PETSCII_RUN_STOP`
