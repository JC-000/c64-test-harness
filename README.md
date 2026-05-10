# c64-test-harness

Reusable test harness for Commodore 64 programs. Automates C64 programs via the VICE emulator's binary monitor protocol, with an architecture that supports real hardware backends.

## Features

- **Transport abstraction** (`C64Transport` Protocol) — write tests once, run on VICE or hardware
- **Binary monitor transport** — persistent TCP connection via VICE's binary monitor protocol (~0.08ms per command, no write size limits, async breakpoint events)
- **Wrap-aware screen matching** — search for text that spans 40-column row boundaries
- **Fast keyboard injection** — batched writes to the keyboard buffer (10x faster than per-character)
- **Little-endian helpers** — `read_word_le()` / `read_dword_le()` for 6502's native byte order
- **PRG binary verification** — compare runtime memory against a PRG file to detect corruption
- **Complete PETSCII/screen code tables** — full 256-entry mappings with extensibility
- **Disk image management** — create/read/write D64/D71/D81 images via c1541, auto-attach to VICE
- **Test runner framework** — scenario-based testing with error recovery
- **Execution control** — load code into RAM, call subroutines via `jsr()`, set breakpoints, patch code at runtime
- **Multi-instance VICE management** — run multiple emulators concurrently with thread-safe port allocation
- **Parallel test execution** — distribute tests across a pool of VICE instances via `run_parallel()`
- **VICE label file parser** — load cc65/ACME/Kick Assembler label files
- **Debug utilities** — `dump_screen()` and `hex_dump()` for quick inspection during test runs
- **Ethernet / CS8900a** — RR-Net-mode ethernet cartridge emulation with TAP interfaces, bridge networking for multi-VICE communication, auto-generated unique MAC addresses per instance
- **SID playback** — cross-backend `play_sid()` dispatches to VICE (IRQ stub) or Ultimate 64 (native firmware endpoint); PSID/RSID parser
- **Audio capture** — headless WAV recording via VICE (`render_wav()`) and U64 UDP audio stream (`capture_sid_u64()`, `AudioCapture`)
- **U64 data streams** — cycle-accurate 6510/VIC bus trace (`DebugCapture`), VIC-II video frame capture (`VideoCapture`), audio capture — all over UDP with gap detection
- **Runtime warp toggle** — enable/disable VICE warp mode at runtime via dual-monitor (binary + text); `resource_get`/`resource_set` for general VICE resource control
- **VICE single-step / snapshots / trace** — `single_step` / `step_out`, conditional breakpoints (`set_condition`), instruction history (`cpu_history`, VICE 3.10+), `dump_snapshot`/`undump_snapshot`, `banks_available` / `registers_available` introspection
- **VICE input simulation & display capture** — `inject_joystick`, `inject_userport`; `read_framebuffer` + `read_palette` for raw VIC capture
- **VICE deterministic test setup** — `ViceConfig.load_snapshot`, event recording / replay (`event_recording_start`, `event_image`, `event_snapshot_mode`/`_dir`), `seed` for RNG, `sound_record_driver`/`_file`, `exit_screenshot`
- **VICE text-monitor extras** — `detach_drive`, `attach_drive`, `screenshot_to_file`, 6502 profiler (`profile_start`/`profile_stop`/`profile_dump`)
- **U64 drive & disk fixtures** — `drive_on/off/reset/set_mode/load_rom`, `create_d64/d71/d81/dnp` blank-image creation, `file_info`, `get_debug_register`/`set_debug_register` ($D7FF), `measure_bus_timing` (VCD), batch `set_config_items_batch`
- **U64 SocketDMA client** — `SocketDMAClient` on TCP 64 wraps capabilities REST does not expose: `inject_keys`, `reu_write`, `dma_load`/`dma_jump`/`dma_write`, `reset`, plus UDP identify-broadcast for LAN device discovery
- **U64 syslog listener** — `U64SyslogListener` consumes UDP 514 raw-line syslog from the firmware; `wait_for(predicate)` for assertion-driven tests
- **Flexible configuration** — `HarnessConfig` with TOML file and environment variable support

## Getting started (fresh Ubuntu 25 machine)

Supported platforms: **Ubuntu Desktop 25** (primary) and **macOS** (Homebrew-based; Apple Silicon, Tahoe 26.x verified).

On a clean Ubuntu Desktop 25 box, one command gets you from zero to a working dev environment:

```bash
./scripts/setup-dev-env.sh
```

The installer runs six stages — system packages, VICE 3.10 source build with `--enable-ethernet`, editable harness install, bridge networking, optional Ultimate 64 probe, and a final `verify-dev-env.sh` run — and every stage is idempotent and opt-out via `--no-*` flags. Pass `--dry-run` first to preview exactly what it will do without touching anything:

```bash
./scripts/setup-dev-env.sh --dry-run
```

See [docs/development.md](docs/development.md) for the full stage breakdown, opt-out flags, and how to recover if a stage fails.

**macOS:** there is no one-shot installer — the flow is a short manual sequence (`brew install vice`, create the venv at `~/.local/share/c64-test-harness/venv`, `pip install -e .`, then `sudo scripts/setup-bridge-feth-macos.sh` if you want the bridge tests). See [docs/development.md#macos-homebrew](docs/development.md#macos-homebrew) for the step-by-step, and [docs/bridge_networking.md](docs/bridge_networking.md) for the `feth0`/`feth1` + `bridge10` layout that is the macOS counterpart to `tap-c64-{0,1}` + `br-c64`.

## Installation

On Ubuntu 23+ (including Ubuntu 25), PEP 668 / `externally-managed-environment` blocks `pip install` against system Python, so install into a venv:

```bash
python3 -m venv --system-site-packages ~/.local/share/c64-test-harness/venv
~/.local/share/c64-test-harness/venv/bin/pip install -e .
source ~/.local/share/c64-test-harness/venv/bin/activate
```

Requires Python 3.10+. Zero runtime dependencies. (If you used `scripts/setup-dev-env.sh`, the venv is already created for you at the same path; just `source` its `activate`.)

## Verifying your dev environment

Before running the full test suite (which needs VICE 3.10 built with ethernet support, bridge networking, and optionally an Ultimate 64 device), run the non-destructive environment check:

```bash
./scripts/verify-dev-env.sh
```

The script is **read-only**: it never launches VICE (only `--version` / `--help`), never runs pytest, never mutates networking, and never touches anything outside the repo. It reports presence of `x64sc`/`c1541`, whether VICE was built with `--enable-ethernet` (the main deployability blocker — distro packages usually omit it), Python harness import, bridge interfaces (`br-c64`, `tap-c64-*`), and optionally probes an Ultimate 64 over `/v1/version` when `U64_HOST` is set.

```
c64-test-harness dev environment check
=======================================

[VICE]
  ✓ x64sc on PATH (/usr/local/bin/x64sc)
  ✓ VICE version (VICE 3.10)
  ✓ ethernet cart support (ethernet flags found in --help)
  ✓ binary monitor support (-binarymonitor flag present)
  ✓ text monitor support (-remotemonitor flag present)
  ✓ c1541 on PATH (present)

[Python]
  ✓ python3 >= 3.10 (3.13.7)
  ✓ c64_test_harness importable (version unknown)
  ✓ pytest available (8.3.5)

[Bridge networking]
  ✗ br-c64 bridge (not found)
  ✗ tap-c64-0 (not found)
  ✗ tap-c64-1 (not found)

[Fix hints]
  -> Run: sudo ./scripts/setup-bridge-tap.sh

Summary: 15 ok, 3 missing, 1 skipped
Overall: READY (with optional gaps)
```

Options: `--quiet` (failures + summary only), `--json` (machine-readable), `--no-u64` (skip the U64 probe), `--u64-host HOST` (override `$U64_HOST`). Exit codes: `0` READY (optional gaps OK), `1` NOT READY (a critical check failed), `2` script error. See [docs/development.md](docs/development.md) for details.

## Quick Start

```python
import time
from c64_test_harness import (
    BinaryViceTransport, ViceProcess, ViceConfig,
    ScreenGrid, send_text, send_key,
    read_bytes, read_word_le, write_bytes,
)

# Launch VICE (always uses binary monitor protocol)
config = ViceConfig(prg_path="build/mygame.prg")
with ViceProcess(config) as vice:
    # Connect with retries (binary monitor needs a moment to start)
    transport = None
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            transport = BinaryViceTransport(port=config.port)
            break
        except Exception:
            time.sleep(1)

    # Wait for the title screen (binary monitor auto-pauses CPU,
    # so resume between screen reads to let the C64 run)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        grid = ScreenGrid.from_transport(transport)
        if grid.has_text("PRESS START"):
            break
        transport.resume()
        time.sleep(1.0)

    # Send keyboard input (batched)
    send_text(transport, "HELLO\r")

    # Send a single key (character or raw PETSCII code)
    send_key(transport, "\r")
    send_key(transport, 0x91)  # cursor up

    # Read screen content
    grid = ScreenGrid.from_transport(transport)
    print(grid.text())

    # Extract data between markers
    value = grid.extract_between("SCORE: ", " ")

    # Read memory (no chunking needed — binary monitor has no size limits)
    data = read_bytes(transport, 0x4000, 512)

    # Read 6502 little-endian values
    length = read_word_le(transport, 0xC000)

    transport.close()
```

**Binary monitor note:** The binary monitor auto-pauses the CPU when any command is sent. Screen and keyboard operations need explicit `transport.resume()` calls between reads so the C64 can process keystrokes and update the screen. The `_wait_for_text_binary()` pattern shown in `tests/test_vice_core.py` demonstrates this.

## Memory Helpers

```python
from c64_test_harness import read_bytes, read_word_le, read_dword_le, write_bytes

# read_bytes — no chunking needed with binary monitor
der_data = read_bytes(transport, der_buf_addr, 512)

# Little-endian readers for 6502's native byte order
length = read_word_le(transport, length_addr)     # 16-bit
counter = read_dword_le(transport, counter_addr)   # 32-bit

# write_bytes — no size limits with binary monitor
write_bytes(transport, 0x1000, [0xDE, 0xAD, 0xBE, 0xEF])
write_bytes(transport, 0xC000, bytes(4096))  # large writes handled natively
```

## PRG Binary Verification

Compare runtime C64 memory against the original PRG file to detect code or data corruption:

```python
from c64_test_harness import PrgFile, Labels

prg = PrgFile.from_file("build/mygame.prg")
labels = Labels.from_file("build/labels.txt")

# Verify that SHA-256 constants are intact in memory
ok, diffs = prg.verify_region(transport, labels["sha256_k"], 256)
assert ok, f"{diffs} bytes corrupted"

# Find the first difference
result = prg.first_diff(transport, labels["process_block"], 1024)
if result:
    offset, expected, actual = result
    print(f"Diff at +{offset}: expected {expected:02x}, got {actual:02x}")

# Labels is a read-only Mapping[str, int] — iterate or convert to dict
for name, addr in labels.items():
    print(f"{name} = ${addr:04x}")
all_labels = dict(labels)
```

## Execution Control

Load 6502 machine code directly into VICE memory, execute subroutines, and inspect results — no PRG files needed:

```python
import time
from c64_test_harness import (
    BinaryViceTransport, ViceProcess, ViceConfig,
    ScreenGrid, load_code, jsr, read_bytes,
    set_breakpoint, delete_breakpoint, set_register, goto,
)

# 6502 subroutine: load byte from $C100, double it, store at $C101
code = bytes([0xAD, 0x00, 0xC1,   # LDA $C100
              0x0A,                 # ASL A
              0x8D, 0x01, 0xC1,   # STA $C101
              0x60])                # RTS

config = ViceConfig(warp=True, sound=False)
with ViceProcess(config) as vice:
    # Connect binary transport with retry
    transport = None
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            transport = BinaryViceTransport(port=config.port)
            break
        except Exception:
            time.sleep(1)

    # Load code and data directly into RAM
    load_code(transport, 0xC000, code)
    transport.write_memory(0xC100, bytes([42]))

    # Execute subroutine and read result
    regs = jsr(transport, 0xC000)
    result = read_bytes(transport, 0xC101, 1)[0]
    assert result == 84  # 42 * 2

    # Patch code at runtime: change ASL to LSR (divide by 2)
    transport.write_memory(0xC003, bytes([0x4A]))
    transport.write_memory(0xC100, bytes([84]))
    jsr(transport, 0xC000)
    assert read_bytes(transport, 0xC101, 1)[0] == 42

    transport.close()
```

### Functions

| Function | Description |
|----------|-------------|
| `load_code(transport, addr, code)` | Write machine code into memory (semantic alias for `write_memory`) |
| `set_register(transport, name, value)` | Set a CPU register (A/X/Y/SP/PC) via `set_registers()` |
| `goto(transport, addr)` | Set PC and resume execution |
| `set_breakpoint(transport, addr) -> int` | Set execution checkpoint, returns checkpoint ID |
| `delete_breakpoint(transport, bp_id)` | Remove a checkpoint |
| `wait_for_pc(transport, addr)` | Wait for CPU to stop at addr (uses async stopped events) |
| `jsr(transport, addr)` | Call a subroutine and wait for RTS (uses trampoline at `$0334`) |

`jsr()` writes a small trampoline (`JSR addr; NOP; NOP`) into the cassette buffer at `$0334`, sets a checkpoint after the `JSR`, resumes execution, and waits for the CPU to stop via async event. The CPU is paused when `jsr()` returns, so memory reads are safe. Works reliably even for long-running computations in warp mode. See `examples/direct_memory_test.py` for a complete demo.

## Runtime Warp Mode Toggle

Toggle VICE warp mode at runtime to speed up long-running computations (e.g. crypto benchmarks) and then return to normal speed for screen verification. VICE 3.10 does not expose `WarpMode` as a binary monitor resource, so warp control requires a secondary text remote monitor connection:

```python
from c64_test_harness import BinaryViceTransport, ViceProcess, ViceConfig

# Enable both binary and text monitors
config = ViceConfig(prg_path="build/app.prg", text_monitor_port=6510)
with ViceProcess(config) as vice:
    transport = BinaryViceTransport(
        port=config.port,
        text_monitor_port=config.text_monitor_port,
    )

    # Toggle warp at runtime
    transport.set_warp(True)    # enable warp — CPU runs at maximum speed
    # ... run long computation ...
    transport.set_warp(False)   # disable warp — back to normal speed

    # Query current warp state
    if transport.get_warp():
        print("Warp is ON")

    # General VICE resource access (binary monitor protocol)
    value = transport.resource_get("Sound")       # returns int or str
    transport.resource_set("Sound", 0)            # mute audio

    transport.close()
```

`ViceInstanceManager` can auto-allocate the text monitor port:

```python
from c64_test_harness import ViceInstanceManager, ViceConfig

config = ViceConfig(prg_path="build/app.prg")
with ViceInstanceManager(config, enable_text_monitor=True) as mgr:
    with mgr.instance() as inst:
        inst.transport.set_warp(True)
        # ... run tests in warp ...
        inst.transport.set_warp(False)
```

**Why dual monitors?** The binary monitor protocol (commands `0x51`/`0x52`) handles most VICE resources, but VICE 3.10's binary monitor returns error code `0x01` for `WarpMode`. The text remote monitor's `warp on`/`warp off` command works reliably. Both monitors run simultaneously on separate TCP ports.

## Multi-Instance VICE & Parallel Testing

Run tests across multiple concurrent VICE instances:

```python
from c64_test_harness import (
    ViceInstanceManager, ViceConfig, run_parallel,
)

config = ViceConfig(prg_path="build/mygame.prg", warp=True)

with ViceInstanceManager(config, port_range_start=6511, port_range_end=6516) as mgr:
    # Context-managed instance (auto-release)
    with mgr.instance() as inst:
        # inst.transport is a BinaryViceTransport
        regs = inst.transport.read_registers()

    # Or run tests in parallel across the pool
    tests = [
        ("test_a", lambda t: (True, "ok")),
        ("test_b", lambda t: (True, "ok")),
    ]
    result = run_parallel(mgr, tests, max_workers=3)
    result.print_summary()
```

`PortAllocator` manages thread-safe port assignment with dual-layer protection: OS-level `bind()` reservations and file-based `flock()` locks (`PortLock`). The file lock bridges the TOCTOU gap between closing the reservation socket and VICE binding to the port, making overlapping startup from independent processes completely safe. `ViceInstanceManager` handles the full lifecycle: allocate port, acquire file lock, launch VICE, connect binary transport with retries, verify PID ownership, and clean up on release. Failed acquisitions retry with exponential backoff (configurable via `max_retries`). Set `reuse_existing=True` to adopt already-running VICE instances instead of launching new ones. Stress-tested with 3 concurrent agents × 6 workers across 5 phases (lock contention, VICE startup, mixed workloads, crash recovery, port exhaustion) with zero failures — see `scripts/stress_cross_process.py`.

Each `ViceInstance` exposes a `.pid` property (the OS process ID of the VICE process), and `SingleTestResult` includes the `.pid` of the instance that ran each test. This allows callers to track and manage only their own VICE processes — essential when multiple agents run tests concurrently.

See `scripts/run_parallel_sha256.py` for a full integration example running 3 concurrent VICE instances with SHA-256 validation, or `scripts/three_windows.py` for an interactive demo that writes user input directly into screen memory across 3 simultaneous VICE windows.

## Disk Image Management

Create and manipulate CBM disk images (D64/D71/D81) using VICE's `c1541` tool:

```python
from c64_test_harness import DiskImage, DiskFormat, FileType, ViceConfig, ViceProcess

# Create a new disk image
disk = DiskImage.create("test.d64", name="MYDATA", disk_id="01")

# Write files into the image
disk.write_file("keys.bin", "KEYS")
disk.write_file("data.bin", "SEQDATA", file_type=FileType.SEQ)  # sequential file
disk.write_file("extra.bin", "USRDATA", file_type=FileType.USR)  # user file
disk.overwrite_file("updated.bin", "KEYS")

# Read files back
data = disk.read_file_bytes("KEYS")

# List directory
for entry in disk.list_files():
    print(f"{entry.name:16s} {entry.blocks:>4d} {entry.file_type.value}")

# Attach disk image to VICE automatically
config = ViceConfig(prg_path="build/app.prg", disk_image=disk)
with ViceProcess(config) as vice:
    # VICE drive 8 is attached with correct drive type (1541/1571/1581)
    ...
```

Requires `c1541` (included with VICE). No additional Python dependencies. Filenames and disk names are validated against the CBM 16-character limit — a `ValueError` is raised immediately for names that are too long, rather than passing them to c1541. The `FileType` enum covers all standard CBM types: `PRG`, `SEQ`, `USR`, `REL`, and `DEL`. Parent directories are created automatically when calling `DiskImage.create()`.

**PETSCII filename note:** `c1541` stores uppercase ASCII as shifted PETSCII ($C1-$DA), but the C64 keyboard produces unshifted codes ($41-$5A). When writing files that will be LOADed by typing on the C64, use **lowercase** `c64_name` values (e.g. `"testprg"` not `"TESTPRG"`) so the PETSCII codes match.

## Debug Utilities

```python
from c64_test_harness import dump_screen, hex_dump

# Capture and print screen content (useful in test failures)
output = dump_screen(transport, label="after login")

# Hex dump a memory region
print(hex_dump(transport, 0x0400, 64))
# $0400: 05 18 10 20 0b 05 19 3a 20 37 03 20 06 04 20 03
# $0410: ...
```

## Test Runner

The `TestRunner` executes named scenarios sequentially with optional recovery between tests:

```python
from c64_test_harness import TestRunner

runner = TestRunner()
runner.add_scenario("Full CSR", test_full_csr, recover_to_menu)
runner.add_scenario("CN only", test_cn_only, recover_to_menu)
results = runner.run_all()
runner.print_summary()
sys.exit(runner.exit_code)
```

Each scenario is a `(name, run_fn, recovery_fn)` tuple. If a test raises an exception, the runner calls the recovery function before continuing with the next scenario.

## Configuration

`HarnessConfig` centralises all settings and can load from a TOML file or environment variables:

```python
from c64_test_harness import HarnessConfig

# From a TOML file
config = HarnessConfig.from_toml("c64_harness.toml")

# From environment variables (C64_VICE_PORT, C64_VICE_HOST, etc.)
config = HarnessConfig.from_env()

# Or construct directly with defaults
config = HarnessConfig(vice_port=6502, vice_warp=True)
```

Key fields: `vice_host`, `vice_port`, `vice_executable`, `vice_prg_path`, `vice_warp`, `vice_sound`, `vice_minimize`, `screen_base`, `vice_port_range_start/end`, `vice_reuse_existing`, `vice_acquire_retries`, `exec_poll_interval`, `screen_poll_interval`.

**Window focus:** VICE windows start minimized by default (`ViceConfig.minimize = True`) to prevent focus stealing during automated test runs. Set `minimize=False` in `ViceConfig` (or `vice_minimize = false` in TOML / `C64TEST_VICE_MINIMIZE=0` in env) if you need visible windows.

## Ultimate 64 Hardware Backend

`Ultimate64Transport` talks to an Ultimate 64 (or Ultimate II+) running 1541ultimate firmware 3.11+ via its REST API over HTTP. No emulator, no TCP monitor — memory reads/writes and keyboard injection go through the device's DMA endpoints.

```python
from c64_test_harness import (
    Ultimate64Transport, ScreenGrid, send_text, send_key,
    wait_for_text, wait_for_stable,
)

transport = Ultimate64Transport(host="192.168.1.81")  # optional: password="..."
try:
    wait_for_text(transport, "READY.", timeout=10)
    send_text(transport, "PRINT 2+2\r")
    wait_for_stable(transport, timeout=5)
    grid = ScreenGrid.from_transport(transport)
    assert "4" in grid.text()
finally:
    transport.close()
```

Multiple devices can be pooled with `Ultimate64InstanceManager` — the same pattern as `ViceInstanceManager`, compatible with `run_parallel()`:

```python
from c64_test_harness import Ultimate64Device, Ultimate64InstanceManager, run_parallel

devices = [
    Ultimate64Device(host="192.168.1.81"),
    Ultimate64Device(host="192.168.1.82"),
]
with Ultimate64InstanceManager(devices) as mgr:
    with mgr.instance() as inst:
        inst.transport.read_memory(0x0400, 40)
    # run_parallel(mgr, tests, max_workers=2)
```

### Unified Backend Manager

`UnifiedManager` provides backend-agnostic test target acquisition — agents specify `"vice"` or `"u64"` (or `"auto"` to read the `C64_BACKEND` env var) and get back a `TestTarget` with a ready-to-use transport:

```python
from c64_test_harness import create_manager

# Reads C64_BACKEND and U64_HOST from environment
with create_manager() as mgr:
    with mgr.instance() as target:
        target.transport.write_memory(0xC000, b"\xDE\xAD")
        print(f"Backend: {target.backend}, PID: {target.pid}")
```

Environment variables: `C64_BACKEND` (`vice` or `u64`), `U64_HOST` (comma-separated for multiple devices), `U64_PASSWORD`.

When the U64 backend is selected, `UnifiedManager` automatically wraps device access with `DeviceLock` — an `fcntl.flock`-based cross-process lock that serializes access to each physical device. Multiple independent agents (separate OS processes) can safely target the same U64 without coordination; the lock file queues them automatically. This is the same kernel-enforced locking pattern used by `PortLock` for VICE port allocation.

### Liveness Probe

Before connecting, probe whether a U64 device is reachable:

```python
from c64_test_harness import probe_u64, is_u64_reachable

# Quick boolean check
if is_u64_reachable("192.168.1.81"):
    print("Device is up")

# Detailed probe: ICMP ping -> TCP connect -> REST API check
result = probe_u64("192.168.1.81")
print(result.summary)  # "U64 at 192.168.1.81: reachable (ping=1.2ms, port=0.8ms, api=5.3ms)"
# result.reachable, result.ping_ok, result.port_ok, result.api_ok, result.latency_ms, result.error
```

The probe runs with short timeouts (2s ping, 2s TCP, 3s API) and fails fast — if ping fails, TCP and API checks are skipped. `Ultimate64InstanceManager.acquire()` uses the probe internally to skip unreachable devices and try the next one in the pool.

### Configuration helpers

Ergonomic wrappers over the firmware config API — turbo speed, REU size, SID sockets, disk mounting, PRG run/load, and full snapshot/restore:

```python
from c64_test_harness import (
    set_turbo_mhz, set_reu, set_sid_socket,
    mount_disk_file, unmount, run_prg_file, load_prg_file,
    snapshot_state, restore_state,
)

set_turbo_mhz(transport, 4)            # 1, 2, 3, 4, 6, 8, 12, 16, 20, 24, 28, 32, 40, 48
set_reu(transport, enabled=True, size_mb=16)
mount_disk_file(transport, "build/disk.d64", drive="a")
run_prg_file(transport, "build/app.prg")

snap = snapshot_state(transport)        # capture turbo/REU/SID config
# ... run tests ...
restore_state(transport, snap)          # put device back as you found it
```

See `examples/ultimate64_hello.py` for a full BASIC round-trip demo and `scripts/probe_u64.py` for device capability discovery.

### Reset vs Reboot

The Ultimate 64 has two reset modes:

- **`reset(client)`** — Soft C64 reset (6510 CPU only). Fast, but does NOT reinitialize the FPGA or DMA controllers.
- **`reboot(client)`** — Full device reboot. Reinitializes the entire FPGA including DMA controllers and REU. Takes ~8 seconds. **Required when switching turbo speeds between REU-heavy workloads** — stale DMA state from a prior turbo speed causes hangs after a soft reset.

### DMA Trampoline Pattern (executing code without jsr)

Since the U64 has no CPU register control, use DMA writes to inject and trigger code:

```python
SENTINEL, TRAMPOLINE, MAIN_LOOP = 0x0350, 0x0360, 0x082A

# Write trampoline: JSR target; LDA #$42; STA sentinel; JMP * (park)
trampoline = bytes([0x20, target & 0xFF, target >> 8, 0xA9, 0x42,
                    0x8D, 0x50, 0x03, 0x4C, 0x68, 0x03])
write_bytes(transport, TRAMPOLINE, trampoline)
write_bytes(transport, SENTINEL, bytes([0x00]))
# Hijack the program's parking loop
write_bytes(transport, MAIN_LOOP, bytes([0x4C, 0x60, 0x03]))
# Poll sentinel for completion
while transport.read_memory(SENTINEL, 1)[0] != 0x42:
    time.sleep(0.1)
```

### Turbo Benchmark

`scripts/bench_x25519_u64_turbo.py` benchmarks X25519 scalar multiplication across all turbo speeds. Results on Ultimate 64 Elite (fw 3.14d):

| MHz | C64 Time | Speedup |
|-----|----------|---------|
| 48 | 12.0s | 13.6x |
| 32 | 13.4s | 12.2x |
| 16 | 18.1s | 9.1x |
| 8 | 28.2s | 5.8x |
| 4 | 48.3s | 3.4x |
| 2 | 81.3s | 2.0x |
| 1 | 163.7s | 1.0x |

**Limitations on hardware:** The REST API does not expose CPU registers or breakpoints. `jsr()`, `wait_for_pc()`, `set_breakpoint()`, and `set_register()` are VICE-only — they are not available on `Ultimate64Transport`. Tests that need register-precise execution control must use the VICE backend. Memory read/write, screen capture, keyboard injection, and screen-text waiting all work identically to VICE.

## SID Playback

Unified SID file playback API that works on both backends — same call, different plumbing underneath:

```python
from c64_test_harness import SidFile, play_sid
sid = SidFile.load("song.sid")
play_sid(transport, sid, song=0)  # works with BinaryViceTransport or Ultimate64Transport
```

`SidFile` parses PSID v1-v4 and RSID headers; `build_test_psid()` synthesizes minimal valid PSIDs for tests. `play_sid()` dispatches on transport type — VICE installs an 18-byte 6502 IRQ wrapper stub at `$C000` (configurable via `DEFAULT_STUB_ADDR`) that repoints the KERNAL IRQ vector at `$0314/$0315` through a `JSR play; JMP $EA31` trampoline, driving the tune at 50Hz. Ultimate 64 hands the `.sid` bytes to the native `POST /v1/runners:sidplay` firmware endpoint.

**VICE limitations:** PSID only — no IRQ-driven RSID support in the stub; `load_addr` must be explicit (the 0x0000 "load-address-in-data" form is not supported here); `play_addr` must be non-zero (the VICE wrapper cannot host sample-driven tunes that have no play routine). Call `stop_sid_vice(transport)` to cleanly silence the SID and restore the original KERNAL IRQ vector.

**Ultimate 64:** the native `sidplay` runner accepts anything the firmware supports (PSID and RSID, including sample-driven tunes), so on hardware the `play_sid()` call just forwards the file bytes.

See `examples/play_sid.py` (supports `--vice` / `--u64 HOST` modes, plus `--self-test` which plays a synthesized C-major scale) and `scripts/play_scale_u64.py` for a full demo that builds a scale PSID on the fly, DMA-loads it, and plays it on hardware.

## Ethernet / CS8900a Testing

Test C64 networking code using VICE's CS8900a ethernet cartridge emulation. On Linux this uses TAP interfaces (`tap-c64-*`) with VICE's `tuntap` driver; on macOS the equivalent layout is `feth*` peers with the `pcap` driver (see [docs/bridge_networking.md](docs/bridge_networking.md) and `tests/bridge_platform.py` for the cross-platform dispatch):

```python
from c64_test_harness import ViceConfig, ViceInstanceManager

config = ViceConfig(
    prg_path="build/network_app.prg",
    warp=False,                 # warp causes timing issues with ethernet
    ethernet=True,
    ethernet_mode="rrnet",      # RR-Net mode — matches ip65 cs8900a.s layout
    ethernet_interface="tap-c64",
    ethernet_driver="tuntap",
)

with ViceInstanceManager(config=config) as mgr:
    inst = mgr.acquire()
    # CS8900a is ready — unique MAC auto-assigned to this instance
    transport = inst.transport
    # ... test networking code ...
    mgr.release(inst)
```

**MAC address uniqueness:** VICE has no CLI flag for CS8900a MAC addresses. When multiple instances share a bridge, `ViceInstanceManager` auto-generates unique locally-administered MACs (`02:c6:40:xx:xx:xx`) per instance by programming the CS8900a Individual Address registers after transport connects. For manual control:

```python
from c64_test_harness import set_cs8900a_mac, generate_mac, parse_mac

mac = generate_mac(0)                    # b"\x02\xc6\x40\x00\x00\x00"
mac = parse_mac("02:c6:40:00:00:42")     # explicit MAC
set_cs8900a_mac(transport, mac)           # program CS8900a IA registers
```

**Bridge setup** for multi-VICE networking: `sudo scripts/setup-bridge-tap.sh` (creates `br-c64` + `tap-c64-0` + `tap-c64-1`). Teardown: `sudo scripts/teardown-bridge-tap.sh`. Single TAP: `sudo scripts/setup-tap-networking.sh`. Emergency recovery: `sudo scripts/cleanup-bridge-networking.sh` (port-range-scoped VICE kill via `scripts/cleanup_vice_ports.py` — never pkill). See the **Reference pattern for VICE agents** section in [docs/bridge_networking.md](docs/bridge_networking.md) for the canonical lifecycle.

**IP-layer ICMP exchange between two VICE instances** is supported via the `bridge_ping` module and the `bridge_vice_pair` pytest fixture (in `tests/conftest.py`). The fixture launches two VICE instances on the bridge, initialises the CS8900a, and programs unique MACs. Tests can build IP/ICMP frames in Python with `build_echo_request_frame()` and verify reception via 6502 RX routines. The harness uses RR-Net register offsets that match ip65's `cs8900a.s` driver (PPPtr=`$DE02`, PPData=`$DE04`, RTDATA=`$DE08`, TxCMD=`$DE0C`, TxLen=`$DE0E`) and automatically emits the RR clockport enable (`$DE01 |= $01`) before every CS8900a access. See `tests/test_bridge_ping.py` for both a one-way IP exchange and a full round-trip where the peer's 6502 responder swaps IPs/MACs and TXes an echo reply in the same JSR, plus [docs/bridge_networking.md](docs/bridge_networking.md) for the register layout and setup steps.

**Shippable-application 6502 timeouts via CIA1 TOD** (`tod_timer` module): the host-driven `build_ping_and_wait_code` helpers above are great for tests but not for a real C64 application. For code that ships on a disk and runs standalone, use `c64_test_harness.tod_timer`, which emits pure 6502 poll loops that drive their own deadlines off the CIA1 Time-of-Day clock:

```python
from c64_test_harness import (
    build_tod_start_code, build_tod_read_tenths_code,
    build_poll_with_tod_deadline_code,
)

# Poll a CS8900a RxEvent register with a 5 s TOD-based timeout.
peek = bytes([0xAD, 0x05, 0xDE, 0x29, 0x01])  # LDA $DE05; AND #$01
code = build_poll_with_tod_deadline_code(
    load_addr=0xC000, peek_check_snippet=peek,
    result_addr=0xC1F0, deadline_tenths=50,   # 5.0 s
)
```

TOD runs at wall-clock rate on real C64, on Ultimate 64 Elite (flat 1.0x across the full 1-48 MHz turbo range), and on VICE 3.10 normal mode. It does **not** work under VICE warp mode, where TOD is virtual-CPU clocked; use the host-driven helpers in that case. Deadline cap is 599 tenths (59.9 s) per single call; longer waits require a caller loop. Zero-page footprint: `$F0`-`$F5`. See [docs/bridge_networking.md](docs/bridge_networking.md#test-harness-vs-shippable-application) for the full split.

For common ICMP scenarios the bridge_ping module ships higher-level wrappers that combine the TX/RX logic with a TOD-gated poll loop in one routine: `build_ping_and_wait_tod_code`, `build_icmp_responder_tod_code`, and `build_rx_echo_reply_tod_code`. They are drop-in shippable counterparts of the host-driven `build_ping_and_wait_code` / `build_icmp_responder_code` / `build_rx_echo_reply_code`. See `tests/test_bridge_ping_tod.py` for a full two-VICE bridge round trip using these variants on VICE normal mode, plus a live U64 TOD primitive test at 1 / 8 / 24 / 48 MHz turbo speeds (gated by `U64_HOST`).

## Audio Capture

### VICE — Headless WAV Render

Record audio from a C64 program to WAV without a visible VICE window:

```python
from c64_test_harness import render_wav

result = render_wav(
    prg_path="build/sid_player.prg",
    out_wav="/tmp/output.wav",
    duration_seconds=10.0,
    sample_rate=44100,
    mono=True,
    pal=True,
)
print(f"Wrote {result.wav_path} ({result.duration_seconds:.1f}s)")
```

### Ultimate 64 — Network Audio Capture

Capture SID audio from a U64 via its UDP audio stream:

```python
from c64_test_harness import capture_sid_u64, SidFile, Ultimate64Client

client = Ultimate64Client(host="192.168.1.81")
sid = SidFile.from_file("tune.sid")
result = capture_sid_u64(client, sid, out_wav="/tmp/u64_audio.wav", duration_seconds=10.0)
print(f"{result.packets_received} packets, {result.packets_dropped} dropped")
```

For low-level control, use `AudioCapture` directly:

```python
from c64_test_harness import AudioCapture

cap = AudioCapture(port=11001)
cap.start()
# ... play SID on U64 ...
result = cap.stop(wav_path="/tmp/capture.wav")
```

## U64 Data Streams

The Ultimate 64 can stream three types of data over UDP, all controllable via the REST API. The test harness provides receivers for all three:

### Debug Stream — Cycle-Accurate Bus Trace

Capture every 6510 CPU bus cycle with full address/data/control signals:

```python
from c64_test_harness import DebugCapture, BusCycle

cap = DebugCapture(port=11002)
cap.start()
# ... run code on the C64 ...
result = cap.stop()

for cycle in result.trace:
    if cycle.is_cpu and cycle.is_write and cycle.address == 0xD020:
        print(f"Border color write: ${cycle.data:02X}")
    if cycle.is_cpu and cycle.irq:
        print(f"IRQ active at ${cycle.address:04X}")
```

Each `BusCycle` exposes: `.address` (16-bit), `.data` (8-bit), `.is_cpu`/`.is_vic`, `.is_read`/`.is_write`, `.irq`, `.nmi`, `.ba`, `.game`, `.exrom`, `.rom`. Five debug modes available: 6510-only, VIC-only, 6510+VIC interleaved, 1541-only, 6510+1541 interleaved.

### Video Stream — VIC-II Frame Capture

Capture the actual VIC-II display output (including sprites, raster effects, borders):

```python
from c64_test_harness import VideoCapture, VIC_PALETTE

cap = VideoCapture(port=11000)
cap.start()
# ... wait for frames ...
result = cap.stop()

for frame in result.frames:
    color = frame.pixel_at(160, 100)       # center pixel color index
    r, g, b = VIC_PALETTE[color]           # RGB lookup
    print(f"Frame {frame.frame_number}: {frame.width}x{frame.height}")
```

PAL: 384x272 @ 50fps. NTSC: 384x240 @ 60fps. 4-bit VIC-II color indices with `VIC_PALETTE` for RGB conversion.

### Stream Configuration

Configure stream destinations and debug mode via the REST API:

```python
from c64_test_harness import (
    get_data_streams_config, set_stream_destination,
    set_debug_stream_mode, DEBUG_MODE_6510_VIC,
)

config = get_data_streams_config(client)   # all stream destinations + mode
set_stream_destination(client, "debug", "10.0.0.5:11002")
set_debug_stream_mode(client, DEBUG_MODE_6510_VIC)
```

## UCI Networking (Ultimate Command Interface)

The `uci_network` module provides TCP/UDP socket networking from C64 programs running on Ultimate 64 hardware via the UCI registers at `$DF1C`–`$DF1F`. The firmware's lwIP stack handles TCP/IP internally — C64 code just opens sockets, reads, and writes.

**Prerequisite:** Enable the Command Interface in U64 settings: *C64 and Cartridge Settings → Command Interface → Enabled*.

```python
from c64_test_harness import uci_probe, uci_get_ip, uci_tcp_connect
from c64_test_harness import uci_socket_write, uci_socket_read, uci_socket_close

# Check UCI is available (returns 0xC9)
ident = uci_probe(transport)

# Query assigned IP address
ip = uci_get_ip(transport)   # e.g. "192.168.1.81"

# TCP socket roundtrip
sock_id = uci_tcp_connect(transport, "example.com", 80)
uci_socket_write(transport, sock_id, b"GET / HTTP/1.0\r\n\r\n")
data = uci_socket_read(transport, sock_id)
uci_socket_close(transport, sock_id)
```

The module also supports UDP sockets (`uci_udp_connect`), TCP listeners (`uci_tcp_listen_start`/`uci_tcp_listen_state`/`uci_tcp_listen_socket`/`uci_tcp_listen_stop`), and low-level assembly builders (`build_uci_probe`, `build_tcp_connect`, etc.) for custom 6502 routines. DNS resolution is handled by the firmware — hostnames work directly.

### UCI at U64 turbo speeds (`turbo_safe=True`)

On real Ultimate 64 Elite hardware the FPGA behind `$DF1C`-`$DF1F` needs ~38 µs of wall-clock settling time between consecutive register accesses. At 1 MHz the 6502 bus cycle is naturally slow enough; at turbo speeds (4/8/16/24/48 MHz) the CPU outruns the FPGA and UCI corrupts. Every builder and helper accepts an opt-in `turbo_safe: bool = False` keyword that emits a nested delay-loop fence (~52 µs at 48 MHz) after each UCI access. When you switch the U64 into turbo mode (`set_turbo_mhz(client, 48)`), pass `turbo_safe=True` to every UCI call:

```python
from c64_test_harness.backends.ultimate64_helpers import set_turbo_mhz

set_turbo_mhz(client, 48)
ident = uci_probe(transport, turbo_safe=True)   # 0xC9 at 48 MHz
sock  = uci_tcp_connect(transport, "example.com", 80, turbo_safe=True)
```

At 1 MHz the default (`turbo_safe=False`) path is strictly faster and just as correct. See [`docs/uci_networking.md`](docs/uci_networking.md) for the full fence design, tuning constants (`UCI_FENCE_OUTER`, `UCI_FENCE_INNER`, `UCI_PUSH_SETTLE_ITERS`), and the c64-https reference implementation this was ported from.

## Architecture

```
C64Transport (Protocol)
  +-- BinaryViceTransport  (VICE binary monitor, persistent TCP)
  +-- Ultimate64Transport  (Ultimate 64 REST API, HTTP/DMA)
  +-- HardwareTransportBase  (extension point for real hardware)

UnifiedManager (backend-agnostic)
  +-- ViceInstanceManager   (VICE emulator pool)
  |     +-- PortAllocator   (thread-safe port range + file locks)
  |     +-- PortLock        (fcntl.flock cross-process lock per port)
  |     +-- ViceInstance    (port + process + transport + lock handle)
  +-- _LockedU64Manager     (U64 hardware pool + cross-process queue)
        +-- Ultimate64InstanceManager  (in-process thread-safe pool)
        +-- DeviceLock      (fcntl.flock cross-process lock per device)
        +-- probe_u64()     (ping + TCP + API liveness check)

TestTarget: backend-agnostic handle (.transport, .backend, .pid)
create_manager(): factory from env vars (C64_BACKEND, U64_HOST)

Screen/Keyboard/Memory modules sit above the transport:
  ScreenGrid, wait_for_text, send_text, read_bytes, etc.

Ethernet:
  ethernet.py: generate_mac, set_cs8900a_mac (CS8900a IA programming)
  ViceConfig: ethernet=True, ethernet_mac auto-assigned by manager

U64 Data Streams (UDP capture):
  AudioCapture  -> CaptureResult          (port 11001, 48kHz stereo PCM)
  VideoCapture  -> VideoCaptureResult      (port 11000, 4-bit VIC-II frames)
  DebugCapture  -> DebugCaptureResult      (port 11002, cycle-accurate bus trace)

Audio Pipeline:
  render_wav()      -> RenderResult        (VICE headless WAV via -limitcycles)
  capture_sid_u64() -> U64CaptureResult    (U64 SID -> UDP -> WAV)

SID Playback:
  play_sid() dispatches on transport type:
    BinaryViceTransport -> play_sid_vice() (IRQ stub at $C000)
    Ultimate64Transport -> play_sid_ultimate64() (POST /v1/runners:sidplay)

Parallel execution:
  run_parallel() -> ParallelTestResult
```

## Examples

The `examples/` directory contains runnable demos:

| Script | Description |
|--------|-------------|
| `examples/wait_for_text.py` | Basic screen text matching |
| `examples/direct_memory_test.py` | Load and execute 6502 code directly in RAM |
| `examples/drive_menu.py` | Navigate disk drive menus |
| `examples/custom_backend.py` | Implement a custom `C64Transport` backend |
| `examples/ultimate64_hello.py` | End-to-end BASIC round-trip on an Ultimate 64 |
| `examples/play_sid.py` | Play a SID file on VICE or Ultimate 64 (`--vice` / `--u64 HOST`) |

Additional scripts in `scripts/`:

| Script | Description |
|--------|-------------|
| `scripts/run_parallel_sha256.py` | 3 concurrent VICE instances running SHA-256 validation |
| `scripts/three_windows.py` | Interactive demo writing user input across 3 VICE windows |
| `scripts/run_all_tests.py` | Parallel test runner for the full test suite |
| `scripts/stress_port_allocation.py` | Cross-process port allocation stress test |
| `scripts/stress_cross_process.py` | Multi-agent VICE instance management stress test (5 phases) |
| `scripts/probe_u64.py` | Probe an Ultimate 64 device (firmware, endpoints, config surface) |
| `scripts/play_scale_u64.py` | Build + play a C-major scale PSID on an Ultimate 64 |
| `scripts/bench_x25519_u64_turbo.py` | X25519 benchmark across U64 turbo speeds (1–48 MHz) |
| `scripts/stress_u64_queue.py` | Cross-process DeviceLock stress test (N workers × M rounds) |
| `scripts/run_u64_parallel_locked.py` | Run all U64 live tests in parallel with cross-process locking |
| `scripts/play_chromatic_u64.py` | Chromatic scale capture through 4 SID configs on U64 |
| `scripts/setup-bridge-tap.sh` | Create bridge + 2 TAP interfaces for multi-VICE ethernet |
| `scripts/teardown-bridge-tap.sh` | Tear down bridge + TAP interfaces |
| `scripts/cleanup-bridge-networking.sh` | Emergency bridge recovery (scoped VICE kill + iptables/TAP teardown) |
| `scripts/cleanup_vice_ports.py` | Port-range-scoped VICE killer (resolves PIDs via `/proc/net/tcp`, verifies `comm`, SIGTERM then SIGKILL — never `pkill`) |
| `scripts/setup-tap-networking.sh` | Create single TAP interface with NAT for VICE ethernet |
| `scripts/teardown-tap-networking.sh` | Tear down single TAP interface |
| `scripts/validate_ping.py` | End-to-end ARP + ICMP ping through VICE CS8900a + TAP |
| `scripts/bridge_ping_demo.py` | Visible two-VICE bridge ping demo (RR-Net, live on-screen counters; supports `--warp` via host-side wall-clock orchestrators) |
| `scripts/verify_tod_warp.py` | Empirical CIA TOD behavior probe in normal vs warp mode (regression check for the wall-clock timeout design) |
| `scripts/verify-dev-env.sh` | Non-destructive dev environment check (VICE build flags, Python harness, bridge interfaces, optional U64 probe) |
| `scripts/setup-dev-env.sh` | Fresh-Ubuntu-25 installer: apt packages, VICE 3.10 source build, harness install, bridge setup, final verify run (idempotent, `--dry-run` safe) |

## Running Tests

```bash
# Install into a venv (PEP 668 compliant — see Installation above)
python3 -m venv --system-site-packages ~/.local/share/c64-test-harness/venv
~/.local/share/c64-test-harness/venv/bin/pip install -e ".[dev]"
source ~/.local/share/c64-test-harness/venv/bin/activate

# Run the full test suite (parallel by default)
python3 scripts/run_all_tests.py

# Unit tests only (no external tools needed)
python3 scripts/run_all_tests.py --unit-only

# Sequential with full pytest output
python3 scripts/run_all_tests.py --serial --verbose

# Control parallelism or filter tests
python3 scripts/run_all_tests.py --workers 4
python3 scripts/run_all_tests.py -k "test_config"
```

The test runner organises test files into three phases:
1. **Unit tests** — run in parallel, no external dependencies
2. **Integration tests** — needs `c1541` on PATH
3. **VICE integration tests** — needs `x64sc` + `c1541`, runs serially

Suites with missing tools are skipped automatically. You can also run tests directly with pytest:

```bash
pytest                                   # unit tests only (no VICE needed)
pytest tests/test_disk_vice.py -v        # VICE disk I/O integration tests
pytest tests/test_vice_core.py -v        # VICE core module integration tests
pytest tests/test_vice_binary.py -v      # VICE binary monitor protocol tests

# Ultimate 64 live tests (requires U64_HOST)
U64_HOST=192.168.1.81 pytest tests/test_u64_feature_parity_live.py -v
U64_HOST=192.168.1.81 U64_ALLOW_MUTATE=1 pytest tests/test_u64_turbo_bench_live.py -v

# Run all U64 live tests in parallel (DeviceLock serializes access)
python3 scripts/run_u64_parallel_locked.py 192.168.1.81

# Stress test the cross-process queueing (6 workers, 5 rounds each)
python3 scripts/stress_u64_queue.py 192.168.1.81 --workers 6 --rounds 5
```

All U64 live tests use `DeviceLock` to serialize access to the physical device. Multiple agents (separate OS processes) can safely run tests in parallel — the lock file queues them automatically.

## Claude Code Skill

A project-level Claude Code skill lives at `.claude/skills/c64-test/`. When Claude Code runs inside this repo it is auto-discovered, giving the agent battle-tested patterns, a full API reference, and the gotcha list up front — no one-shot re-discovery of harness conventions.

- `SKILL.md` — when to use it, core principles, test-file templates
- `REFERENCE.md` — module-by-module API reference
- `PATTERNS.md` — patterns (VICE management, jsr-based testing, parallel, U64 DMA trampoline, bridge networking, UCI) + 25 common gotchas

The skill is versioned alongside the code, so every merge updates it with the harness. If you maintain a user-level copy at `~/.claude/skills/c64-test/`, the project-level version takes precedence when Claude Code is launched from this repo.

## License

MIT
