# c64-test-harness

Reusable test harness for Commodore 64 programs. Automates C64 programs via the VICE emulator's remote monitor, with an architecture that supports real hardware backends.

## Features

- **Transport abstraction** (`C64Transport` Protocol) — write tests once, run on VICE or hardware
- **Wrap-aware screen matching** — search for text that spans 40-column row boundaries
- **Fast keyboard injection** — batched writes to the keyboard buffer (10x faster than per-character)
- **Robust VICE monitor parsing** — works around known VICE text monitor bugs
- **Reliable large memory reads** — automatic chunking for reads >256 bytes
- **Little-endian helpers** — `read_word_le()` / `read_dword_le()` for 6502's native byte order
- **PRG binary verification** — compare runtime memory against a PRG file to detect corruption
- **Complete PETSCII/screen code tables** — full 256-entry mappings with extensibility
- **Test runner framework** — scenario-based testing with error recovery
- **Execution control** — load code into RAM, call subroutines via `jsr()`, set breakpoints, patch code at runtime
- **VICE label file parser** — load cc65/ACME/Kick Assembler label files

## Installation

```bash
pip install -e .
```

Requires Python 3.10+. Zero runtime dependencies.

## Quick Start

```python
from c64_test_harness import (
    ViceTransport, ViceProcess, ViceConfig,
    ScreenGrid, wait_for_text, send_text,
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

    # Send keyboard input
    send_text(transport, "HELLO\r")

    # Read screen content
    grid = ScreenGrid.from_transport(transport)
    print(grid.text())

    # Extract data between markers
    value = grid.extract_between("SCORE: ", " ")

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

# Write memory
write_bytes(transport, 0x1000, [0xDE, 0xAD, 0xBE, 0xEF])
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

## Architecture

```
C64Transport (Protocol)
  +-- ViceTransport      (VICE TCP monitor)
  +-- HardwareTransportBase  (extension point for real hardware)

Screen/Keyboard/Memory modules sit above the transport:
  ScreenGrid, wait_for_text, send_text, read_bytes, etc.
```

## Running Tests

```bash
pip install -e ".[dev]"
pytest
```

## License

MIT
