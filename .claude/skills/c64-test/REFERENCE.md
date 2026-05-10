# c64-test-harness API Reference

## Installation

```bash
pip install -e /path/to/c64-test-harness
```

The package is `c64_test_harness`. All public symbols are re-exported from the top-level `__init__.py`. The package version is exposed as `c64_test_harness.__version__` (via `importlib.metadata`).

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

### `BackendManager` (Protocol)
Structural protocol satisfied by both `ViceInstanceManager` and `Ultimate64InstanceManager`:
- `acquire() -> Any`
- `release(instance) -> None`
- `shutdown() -> None`

### `create_manager(backend="auto", **kwargs) -> UnifiedManager`
Factory function. `backend="auto"` reads `C64_BACKEND` env var (defaults to `"vice"`).

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
- `read_memory(addr: int, length: int) -> bytes` -- Read raw bytes
- `write_memory(addr: int, data: bytes | list[int]) -> None` -- Write bytes to C64 memory
- `read_screen_codes() -> list[int]` -- Read raw screen code bytes (cols * rows values)
- `inject_keys(petscii_codes: list[int]) -> None` -- Inject PETSCII key codes into keyboard buffer
- `read_registers() -> dict[str, int]` -- Returns `{"A": ..., "X": ..., "Y": ..., "SP": ..., "PC": ...}`
- `resume() -> None` -- Resume CPU execution
- `close() -> None` -- Release resources / close connection

Properties:
- `screen_cols -> int` -- Number of screen columns (typically 40)
- `screen_rows -> int` -- Number of screen rows (typically 25)

### Exceptions
- `TransportError` -- Base exception
- `ConnectionError` -- TCP connection to VICE monitor failed (port not ready)
- `TimeoutError` -- Operation timed out

---

## Module: memory

All functions take `transport` as first arg (stateless).

- `read_bytes(transport, addr, length) -> bytes` -- Read bytes from addr. Contains legacy auto-chunking at 256 bytes (unnecessary with binary transport but harmless).
- `read_bytes_chunked(transport, addr, length, chunk_size=128) -> bytes` -- Explicitly chunked read for large regions
- `write_bytes(transport, addr, data) -> None` -- Write data to addr (accepts bytes or list[int]). Contains legacy auto-chunking at 84 bytes (unnecessary with binary transport but harmless).
- `read_word_le(transport, addr) -> int` -- Read 16-bit little-endian
- `read_dword_le(transport, addr) -> int` -- Read 32-bit little-endian
- `hex_dump(transport, addr, length) -> str` -- Formatted hex dump string

---

## Module: execute

All functions take `transport: BinaryViceTransport` as first arg (stateless). These functions use the binary monitor's native checkpoint and register commands.

- `load_code(transport, addr, code) -> None` -- Write executable bytes (semantic alias for write_memory)
- `set_register(transport, name, value) -> None` -- Set CPU register via `transport.set_registers({name: value})`
- `goto(transport, addr) -> None` -- Set PC via `transport.set_registers({"PC": addr})` then `transport.resume()`
- `set_breakpoint(transport, addr) -> int` -- Calls `transport.set_checkpoint(addr)`, returns checkpoint ID
- `delete_breakpoint(transport, bp_id) -> None` -- Calls `transport.delete_checkpoint(bp_id)`
- `wait_for_pc(transport, addr, timeout=5.0) -> dict` -- Calls `transport.wait_for_stopped()` then verifies PC; returns register dict; CPU is **paused** on return
- `jsr(transport, addr, timeout=5.0, *, scratch_addr=0x0334) -> dict` -- Call subroutine via trampoline, wait for RTS; CPU is **paused** on return. Works reliably for both short and long-running computations (event-based, no polling).

### `jsr()` internals
1. Writes trampoline at `scratch_addr`: `JSR $addr; NOP; NOP` (5 bytes)
2. Sets checkpoint at `scratch_addr + 3`
3. Sets PC to `scratch_addr` and resumes CPU
4. Calls `wait_for_stopped()` until checkpoint fires, verifies PC
5. Deletes checkpoint

---

## Module: screen

- `ScreenGrid` -- Parsed screen state (40x25 character grid)
- `wait_for_text(transport, text, timeout=60.0, poll_interval=2.0, verbose=True) -> ScreenGrid | None` -- Poll screen RAM until text appears. Returns `None` on timeout. **Note:** `verbose` defaults to `True` (dumps screen on every poll) — pass `verbose=False` for quiet operation.
- `wait_for_stable(transport, timeout=10.0, poll_interval=0.5, stable_count=3) -> ScreenGrid | None` -- Wait for screen to stop changing. Returns `None` on timeout.

---

## Module: keyboard

- `send_text(transport, text) -> None` -- Type text into C64 keyboard buffer (max 10 chars at a time, auto-chunks)
- `send_key(transport, key) -> None` -- Send single keypress (e.g., `"\r"` for RETURN)

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
- `vice_minimize` (default True) -- passes `-minimized` to VICE
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
```python
ViceConfig(
    executable="x64sc",
    prg_path="build/program.prg",
    port=6502,              # Overridden by ViceInstanceManager
    warp=True,
    ntsc=True,
    sound=False,
    minimize=True,          # Start VICE minimized (prevents focus stealing)
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
    run_as_root=None,       # None = auto: True on Darwin when ethernet=True
)
```

**Platform behavior — `run_as_root`**: Tri-state. `None` (the default) auto-resolves to `True` on macOS when `ethernet=True`, `False` elsewhere. Set explicitly to override. When True, `ViceProcess` launches x64sc via `sudo -n`, tracks the root-owned child via `_find_x64sc_child_pid()`, and routes `stop()` through `sudo -n kill` because an unprivileged parent cannot signal a root child. Required on macOS because VICE's pcap driver on `feth(4)` segfaults when the kernel refuses the non-root BPF attach (symptom: NULL deref in `rawnet_arch_pre_reset+8` during `cs8900_activate`). See `docs/development.md` macOS caveats for the full story and the NOPASSWD sudoers recipe.

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
- `ViceProcess.kill_on_port(port) -> bool` -- Kill process listening on port (Linux /proc)
- `ViceProcess.get_listener_pid(port) -> int | None` -- Return PID of process listening on port (Linux /proc)

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
- `write_memory(addr, data) -> None`
- `read_screen_codes() -> list[int]`
- `inject_keys(petscii_codes) -> None`
- `read_registers() -> dict[str, int]`
- `resume() -> None`
- `close() -> None`
- `screen_cols -> int` (property)
- `screen_rows -> int` (property)

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
- CPU auto-pauses on every command -- `wait_for_text()` uses a resume-between-polls pattern so the screen updates

---

## Module: parallel

- `run_parallel(manager, tests, max_workers=None) -> ParallelTestResult`
  - `tests`: list of `(name, fn)` where `fn(transport) -> (passed: bool, message: str)`
- `ParallelTestResult` -- `.results`, `.all_passed`, `.exit_code`, `.print_summary()`
- `SingleTestResult` -- `.name`, `.passed`, `.message`, `.duration`, `.pid` (VICE process PID)

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
### `DirEntry` -- `.name`, `.file_type`, `.size_blocks`

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
Program the CS8900a Individual Address registers via PPPtr/PPData at PP offsets 0x0158-0x015D. CPU must be stopped (normal after binary monitor connect).

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

### Skip-gate idiom

```python
from tests.bridge_platform import (
    IFACE_A, IFACE_B, BRIDGE_NAME, SETUP_HINT, iface_present,
)

missing = [n for n in (IFACE_A, IFACE_B, BRIDGE_NAME) if not iface_present(n)]
if missing:
    pytest.skip(f"bridge down ({', '.join(missing)}); {SETUP_HINT}")
```

Check **all three** (both peers + bridge), not just the peers — on macOS the bridge is created separately from the feth pair and may be absent after a partial teardown.

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

- `dump_screen(transport, label="debug") -> None` -- Save screen contents to file for debugging

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
- `write_memory(addr, data) -> None`
- `read_screen_codes() -> list[int]`
- `inject_keys(petscii_codes) -> None`
- `read_registers() -> dict[str, int]`
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

**C64Transport methods** (all DMA-backed, no CPU pause):
- `read_memory(addr, length) -> bytes`
- `write_memory(addr, data) -> None`
- `read_screen_codes() -> list[int]`
- `inject_keys(petscii_codes) -> None`
- `read_registers()` -- raises `NotImplementedError` (no CPU register access on U64)
- `resume() -> None`
- `close() -> None`

**Not available on hardware** (require VICE binary monitor):
- `jsr()`, `wait_for_pc()`, `set_breakpoint()`, `set_registers()`, `set_checkpoint()`, `wait_for_stopped()`
- Design tests to self-report results via memory writes + sentinel polling

### `Ultimate64Client`
REST API wrapper for U64 firmware endpoints.
```python
from c64_test_harness.backends.ultimate64_client import Ultimate64Client
client = Ultimate64Client(host="192.168.1.81", password=None, timeout=10.0)
```

**Machine control:**
- `client.reset()` -- Soft reset the C64 (6510 CPU only, does NOT reinitialize FPGA/DMA)
- `client.reboot()` -- Full reboot of the Ultimate device (reinitializes FPGA, DMA, REU). Required when switching turbo speeds with REU-heavy workloads. Allow ~8s settle after reboot.
- `client.pause()` -- Halt the emulated CPU
- `client.resume()` -- Resume the emulated CPU

**PRG/runner endpoints** (all use POST, not PUT — fw 3.14):
- `client.run_prg(data)` -- Load and RUN a PRG (resets C64 internally)
- `client.load_prg(data)` -- Load a PRG into memory without running
- `client.run_crt(data)` -- Start a cartridge image
- `client.sid_play(data, songnr=0)` -- Play a .sid tune
- `client.mod_play(data)` -- Play a .mod file

**Memory (DMA-backed):**
- `client.read_mem(address, length) -> bytes`
- `client.write_mem(address, data)` -- data is hex-encoded in query param

**Config:**
- `client.get_version() -> dict`
- `client.get_info() -> dict`
- `client.list_configs() -> list[str]`
- `client.get_config_category(name) -> dict`
- `client.get_config_item(category, item) -> dict`
- `client.set_config_items(category, items_dict)` -- iterates per-item (no batch endpoint)

### `ultimate64_helpers` key functions
```python
from c64_test_harness.backends.ultimate64_helpers import (
    set_turbo_mhz, get_turbo_mhz, get_turbo_enabled,
    set_reu, get_reu_config,
    snapshot_state, restore_state,
    reset, reboot,
    recover, runner_health_check,
    run_prg_file, load_prg_file,
)
```

- `set_turbo_mhz(client, mhz)` -- Set turbo to given MHz (int), or `None` to disable
- `get_turbo_mhz(client) -> int | None` -- Current speed, or None if turbo off
- `set_reu(client, enabled, size=None)` -- Enable/disable REU; size as str ("512 KB") or int (MB)
- `snapshot_state(client) -> U64StateSnapshot` -- Capture turbo + REU + cartridge state
- `restore_state(client, snap)` -- Restore a snapshot
- `reset(client)` -- Soft reset (CPU only)
- `reboot(client)` -- Full FPGA reboot (clears DMA state, ~8s settle)
- `recover(client, *, reset_settle_seconds=2.0, reboot_settle_seconds=12.0, escalate_to_reboot=True) -> str` -- Escalate `reset()` -> probe -> `reboot()` -> probe; returns `"reset"` or `"reboot"`. Raises `Ultimate64UnreachableError` on total failure. Never calls `poweroff()`.
- `runner_health_check(client) -> None` -- Post a tiny no-op PRG; raises `Ultimate64RunnerStuckError` on the firmware's "Cannot open file" wedged-runner signature.

### `ultimate64_schema` constants
- `CPU_SPEED_VALUES` -- tuple of 16 speed enum strings (" 1" through "48")
- `CPU_SPEED_BY_MHZ` -- dict mapping int MHz to enum string
- `REU_SIZE_VALUES`, `REU_ENABLED_VALUES`, `TURBO_CONTROL_VALUES`

---

## Module: backends.device_lock

### `DeviceLock`
Cross-process exclusive lock for hardware devices using `fcntl.flock()`. The key difference from `PortLock`: `acquire()` is blocking with a timeout, so multiple agents queue for the same device.

```python
from c64_test_harness import DeviceLock

lock = DeviceLock("192.168.1.81")
if lock.acquire(timeout=30.0):  # Blocks up to 30s
    try:
        # Device is exclusively ours across all OS processes
        ...
    finally:
        lock.release()

# Or as context manager (raises RuntimeError on timeout)
with DeviceLock("192.168.1.81") as lock:
    ...
```

- `acquire(timeout=30.0) -> bool` -- Blocking acquire (polls with LOCK_NB every 0.1s). Writes JSON metadata (PID, timestamp, device_host). Verifies inode after flock.
- `release()` -- Release flock. Does NOT delete lockfile (inode race safety, same as PortLock).
- `read_info() -> dict | None` -- Read metadata without locking (diagnostics).
- `cleanup_stale(lock_dir=None) -> int` -- Class method; removes lockfiles from dead PIDs.
- `.device_host` / `.held` properties
- Context manager support

Lockfiles at `$XDG_RUNTIME_DIR/c64-test-harness/device-{sanitized_host}.lock`. Same directory as PortLock. Kernel auto-releases locks on process exit (crash-safe).

**When to use:** `UnifiedManager` uses `DeviceLock` automatically for U64 backends. Use directly only when creating `Ultimate64Transport` outside of `UnifiedManager` (e.g., in pytest fixtures for live tests).

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

## Module: sid

### `SidFile`
Parsed PSID/RSID file with all header fields and the raw bytes.
```python
from c64_test_harness import SidFile

sid = SidFile.from_file("tune.sid")
sid.name          # str — title from header
sid.author        # str
sid.songs         # int — number of sub-tunes
sid.init_addr     # int — 6502 init entry point
sid.play_addr     # int — 6502 play entry point (0 = uses IRQ)
sid.c64_data      # bytes — just the 6502 code/data
sid.effective_load_addr  # int — resolved load address
sid.song_is_60hz(0)      # bool — True if CIA-timed (60 Hz)
```

### `build_test_psid(load_addr, init_addr, play_addr, data, ...) -> bytes`
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

**Key gotcha:** After `jsr()` installs the stub/runs init, the play routine's PC must jump back to BASIC warm-start (`JMP ($A002)`). Otherwise the CPU runs into stale NOPs or hits BRK, resetting IRQ vectors and killing playback.

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
- `.irq -> bool`, `.nmi -> bool`, `.ba -> bool` — True when asserted (active-low signals inverted)
- `.game -> bool`, `.exrom -> bool`, `.rom -> bool` — cartridge/ROM signals
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
- `uci_probe(transport, *, timeout=5.0, turbo_safe=False) -> int` — returns `0xC9` if UCI present
- `uci_get_ip(transport, *, timeout=5.0, turbo_safe=False) -> str` — dotted-quad IP
- `uci_get_interface_count(transport, *, turbo_safe=False) -> int`
- `uci_tcp_connect(transport, host, port, *, turbo_safe=False) -> int` — returns socket handle
- `uci_udp_connect(transport, host, port, *, turbo_safe=False) -> int`
- `uci_socket_write(transport, sock, data, *, turbo_safe=False) -> int`
- `uci_socket_read(transport, sock, *, max_bytes, turbo_safe=False) -> bytes`
- `uci_socket_close(transport, sock, *, turbo_safe=False) -> None`
- `uci_tcp_listen_start(transport, port, *, turbo_safe=False) -> int`
- `uci_tcp_listen_state(transport, listener, *, turbo_safe=False) -> int` — NOT_LISTENING / LISTENING / CONNECTED / BIND_ERROR / PORT_IN_USE
- `uci_tcp_listen_socket(transport, listener, *, turbo_safe=False) -> int`
- `uci_tcp_listen_stop(transport, listener, *, turbo_safe=False) -> None`
- `get_uci_enabled(client) -> bool` / `enable_uci(client)` / `disable_uci(client)` — config-side helpers, take an `Ultimate64Client`

### 6502 code builders (return raw bytes — `load_code()` + `jsr()`)
- `build_uci_probe(*, turbo_safe=False) -> bytes`
- `build_uci_command(cmd, target, params, *, turbo_safe=False) -> bytes`
- `build_get_ip(*, turbo_safe=False) -> bytes`
- `build_tcp_connect(host, port, *, turbo_safe=False) -> bytes`
- `build_udp_connect(host, port, *, turbo_safe=False) -> bytes`
- `build_socket_write(sock, data, *, turbo_safe=False) -> bytes`
- `build_socket_read(sock, max_bytes, *, turbo_safe=False) -> bytes`
- `build_socket_close(sock, *, turbo_safe=False) -> bytes`

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

Bridge networking helpers — two VICE instances on a Linux bridge talking L2 + IP + ICMP via CS8900a. See Pattern 8 in `PATTERNS.md`.

### High-level orchestrators (own the wall-clock deadline in Python)
- `run_ping_and_wait(transport, *, tx_frame, rx_buf, result_addr, identifier, sequence, tx_frame_buf, timeout_s=5.0, peek_addr=..., consume_addr=...) -> int` — returns `0x01` on matched reply, `0xFF` on timeout
- `run_icmp_responder(transport, *, rx_buf, my_ip, result_addr, timeout_s=5.0, peek_addr=..., consume_addr=...) -> int` — reply to any echo request addressed to `my_ip`

### Frame + code builders
- `build_echo_request_frame(src_mac, dst_mac, src_ip, dst_ip, identifier, sequence, payload) -> bytes`
- `build_bridge_tx_code(...)` — transmit a pre-built frame via CS8900a
- `build_rx_peek_code(...)` — bounded peek into RX FIFO (drives orchestrator polling)
- `build_rx_echo_reply_code(...)` — full-routine echo-reply match (legacy, virtual-cycle timing)
- `build_read_and_match_echo_reply_code(...)` — read a pending frame, match against expected reply
- `build_read_and_respond_echo_request_code(...)` — read request, respond with reply in one pass
- `build_ping_and_wait_code(...)` — legacy TX+RX combined, virtual-cycle timing
- `build_icmp_responder_code(...)` — legacy responder, virtual-cycle timing
- `cs8900a_rxctl_code(...)` / `cs8900a_write_linectl_code(...)` / `cs8900a_read_linectl_code(...)` — init helpers

### TOD-timed variants (shippable on real C64 / U64E / VICE normal; NOT usable under VICE warp)
- `build_ping_and_wait_tod_code(...)`
- `build_icmp_responder_tod_code(...)`
- `build_rx_echo_reply_tod_code(...)`

### Dataclasses
- `EchoRequest` — parsed ICMP echo request fields

**Test fixture:** `bridge_vice_pair` in `tests/conftest.py` brings up two VICE instances on `br-c64`, RR-Net mode, unique MACs, CS8900a initialised.

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
- `runner.add_scenario(name, setup, run, verify, recover=None, timeout=None)`
- `runner.run_all() -> list[TestResult]`
- `runner.results -> list[TestResult]`
- `runner.all_passed -> bool`
- `runner.exit_code -> int`
- `runner.print_summary()`

### `TestScenario` — dataclass for a single scenario
### `TestResult` — dataclass with `.status: TestStatus` and metadata
### `TestStatus` — enum: `PASSED`, `FAILED`, `SKIPPED`, `ERROR`

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
