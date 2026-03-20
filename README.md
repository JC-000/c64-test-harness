# c64-test-harness

Reusable test harness for Commodore 64 programs. Automates C64 programs via the VICE emulator's remote monitor, with an architecture that supports real hardware backends.

## Features

- **Transport abstraction** (`C64Transport` Protocol) — write tests once, run on VICE or hardware
- **Wrap-aware screen matching** — search for text that spans 40-column row boundaries
- **Fast keyboard injection** — batched writes to the keyboard buffer (10x faster than per-character)
- **Robust VICE monitor parsing** — works around known VICE text monitor bugs
- **Reliable large memory access** — automatic chunking for reads >256 bytes and writes >84 bytes
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
- **Flexible configuration** — `HarnessConfig` with TOML file and environment variable support

## Installation

```bash
pip install -e .
```

Requires Python 3.10+. Zero runtime dependencies.

## Quick Start

```python
from c64_test_harness import (
    ViceTransport, ViceProcess, ViceConfig,
    ScreenGrid, wait_for_text, wait_for_stable, send_text, send_key,
    read_bytes, read_word_le, write_bytes,
)

# Launch VICE
config = ViceConfig(prg_path="build/mygame.prg")
with ViceProcess(config) as vice:
    vice.wait_for_monitor()
    transport = ViceTransport(port=config.port)

    # Wait for the title screen
    grid = wait_for_text(transport, "PRESS START")
    assert grid is not None

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

    # Wait for screen to stabilise (stops changing)
    stable = wait_for_stable(transport, timeout=10, stable_count=3)

    # Read memory (auto-chunks for large reads)
    data = read_bytes(transport, 0x4000, 512)

    # Read 6502 little-endian values
    length = read_word_le(transport, 0xC000)
```

## Memory Helpers

```python
from c64_test_harness import read_bytes, read_word_le, read_dword_le, write_bytes

# read_bytes auto-chunks reads >256 bytes for reliability
der_data = read_bytes(transport, der_buf_addr, 512)

# Little-endian readers for 6502's native byte order
length = read_word_le(transport, length_addr)     # 16-bit
counter = read_dword_le(transport, counter_addr)   # 32-bit

# write_bytes auto-chunks writes >84 bytes (VICE text monitor limit)
write_bytes(transport, 0x1000, [0xDE, 0xAD, 0xBE, 0xEF])
write_bytes(transport, 0xC000, bytes(256))  # large writes handled transparently
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
```

## Execution Control

Load 6502 machine code directly into VICE memory, execute subroutines, and inspect results — no PRG files needed:

```python
from c64_test_harness import (
    ViceProcess, ViceConfig, ViceTransport,
    wait_for_text, load_code, jsr, read_bytes,
    set_breakpoint, delete_breakpoint, set_register, goto,
)

# 6502 subroutine: load byte from $C100, double it, store at $C101
code = bytes([0xAD, 0x00, 0xC1,   # LDA $C100
              0x0A,                 # ASL A
              0x8D, 0x01, 0xC1,   # STA $C101
              0x60])                # RTS

config = ViceConfig(warp=True, sound=False)
with ViceProcess(config) as vice:
    vice.start()
    transport = ViceTransport(port=config.port)
    wait_for_text(transport, "READY.", timeout=30)

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
```

### Functions

| Function | Description |
|----------|-------------|
| `load_code(transport, addr, code)` | Write machine code into memory (semantic alias for `write_memory`) |
| `set_register(transport, name, value)` | Set a CPU register (A/X/Y/SP/PC) |
| `goto(transport, addr)` | Set PC and resume execution |
| `set_breakpoint(transport, addr) -> int` | Set execution breakpoint, returns breakpoint ID |
| `delete_breakpoint(transport, bp_id)` | Remove a breakpoint |
| `wait_for_pc(transport, addr)` | Poll until PC reaches addr (with timeout) |
| `jsr(transport, addr)` | Call a subroutine and wait for RTS (uses trampoline at `$0334`) |

`jsr()` writes a small trampoline (`JSR addr; NOP; NOP`) into the cassette buffer at `$0334`, sets a breakpoint after the `JSR`, and polls until the subroutine returns. The CPU is paused when `jsr()` returns, so memory reads are safe. See `examples/direct_memory_test.py` for a complete demo.

Both `jsr()` and `wait_for_pc()` accept a `poll_interval` parameter (default 0.2s) that controls how often the monitor is polled. For long-running computations, increase this to reduce overhead from monitor connections pausing the CPU:

```python
# Long computation — poll less often to reduce ~13% overhead
regs = jsr(transport, labels["slow_routine"], timeout=300, poll_interval=2.0)

# Or use HarnessConfig to set it globally
config = HarnessConfig(exec_poll_interval=2.0)
regs = jsr(transport, addr, timeout=300, poll_interval=config.exec_poll_interval)
```

## Multi-Instance VICE & Parallel Testing

Run tests across multiple concurrent VICE instances:

```python
from c64_test_harness import (
    ViceInstanceManager, ViceConfig, run_parallel,
)

config = ViceConfig(prg_path="build/mygame.prg", warp=True)

with ViceInstanceManager(config, port_range_start=6510, port_range_end=6515) as mgr:
    # Context-managed instance (auto-release)
    with mgr.instance() as inst:
        grid = wait_for_text(inst.transport, "READY")

    # Or run tests in parallel across the pool
    tests = [
        ("test_a", lambda t: (True, "ok")),
        ("test_b", lambda t: (True, "ok")),
    ]
    result = run_parallel(mgr, tests, max_workers=3)
    result.print_summary()
```

`PortAllocator` manages thread-safe port assignment with dual-layer protection: OS-level `bind()` reservations and file-based `flock()` locks (`PortLock`). The file lock bridges the TOCTOU gap between closing the reservation socket and VICE binding to the port, making overlapping startup from independent processes completely safe. `ViceInstanceManager` handles the full lifecycle: allocate port, acquire file lock, launch VICE, verify PID ownership, connect transport, and clean up on release. Failed acquisitions retry with exponential backoff (configurable via `max_retries`). When multiple VICE instances launch simultaneously, some may crash due to X11/GTK resource contention — `wait_for_monitor()` detects early process exit within ~1 second and the retry logic recovers automatically. Set `reuse_existing=True` to adopt already-running VICE instances instead of launching new ones. Stress-tested with 3 concurrent agents × 6 workers across 5 phases (lock contention, VICE startup, mixed workloads, crash recovery, port exhaustion) with zero failures — see `scripts/stress_cross_process.py`.

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
    vice.wait_for_monitor()
    # VICE drive 8 is attached with correct drive type (1541/1571/1581)
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
config = HarnessConfig(vice_port=6510, vice_warp=True)
```

Key fields: `vice_host`, `vice_port`, `vice_executable`, `vice_prg_path`, `vice_warp`, `vice_sound`, `vice_minimize`, `screen_base`, `vice_port_range_start/end`, `vice_reuse_existing`, `vice_acquire_retries`, `exec_poll_interval`, `screen_poll_interval`.

**Window focus:** VICE windows start minimized by default (`ViceConfig.minimize = True`) to prevent focus stealing during automated test runs. Set `minimize=False` in `ViceConfig` (or `vice_minimize = false` in TOML / `C64TEST_VICE_MINIMIZE=0` in env) if you need visible windows.

## Architecture

```
C64Transport (Protocol)
  +-- ViceTransport      (VICE TCP monitor)
  +-- HardwareTransportBase  (extension point for real hardware)

ViceInstanceManager
  +-- PortAllocator      (thread-safe port range + file locks)
  +-- PortLock           (fcntl.flock cross-process lock per port)
  +-- ViceInstance        (port + process + transport + lock handle)

Screen/Keyboard/Memory modules sit above the transport:
  ScreenGrid, wait_for_text, send_text, read_bytes, etc.

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

Additional scripts in `scripts/`:

| Script | Description |
|--------|-------------|
| `scripts/run_parallel_sha256.py` | 3 concurrent VICE instances running SHA-256 validation |
| `scripts/three_windows.py` | Interactive demo writing user input across 3 VICE windows |
| `scripts/run_all_tests.py` | Parallel test runner for the full test suite |
| `scripts/stress_port_allocation.py` | Cross-process port allocation stress test |
| `scripts/stress_cross_process.py` | Multi-agent VICE instance management stress test (5 phases) |

## Running Tests

```bash
pip install -e ".[dev]"

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

The test runner organises 23 test files into three phases:
1. **Unit tests** (19 files) — run in parallel, no external dependencies
2. **Integration tests** (1 file) — needs `c1541` on PATH
3. **VICE integration tests** (3 files) — needs `x64sc` + `c1541`, runs serially

Suites with missing tools are skipped automatically. You can also run tests directly with pytest:

```bash
pytest                                   # unit tests only (no VICE needed)
pytest tests/test_disk_vice.py -v        # VICE disk I/O integration tests
pytest tests/test_vice_core.py -v        # VICE core module integration tests
pytest tests/test_vice_transport.py -v   # VICE transport protocol tests
```

## License

MIT
