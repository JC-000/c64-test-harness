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
- **Disk image management** — create/read/write D64/D71/D81 images via c1541, auto-attach to VICE
- **Test runner framework** — scenario-based testing with error recovery
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

## Disk Image Management

Create and manipulate CBM disk images (D64/D71/D81) using VICE's `c1541` tool:

```python
from c64_test_harness import DiskImage, DiskFormat, FileType, ViceConfig, ViceProcess

# Create a new disk image
disk = DiskImage.create("test.d64", name="MYDATA", disk_id="01")

# Write files into the image
disk.write_file("keys.bin", "KEYS")
disk.write_file("data.bin", "seqdata", FileType.SEQ)  # sequential file
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

Requires `c1541` (included with VICE). No additional Python dependencies.

**PETSCII filename note:** `c1541` stores uppercase ASCII as shifted PETSCII ($C1-$DA), but the C64 keyboard produces unshifted codes ($41-$5A). When writing files that will be LOADed by typing on the C64, use **lowercase** `c64_name` values (e.g. `"testprg"` not `"TESTPRG"`) so the PETSCII codes match.

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
pytest                                   # unit tests only (no VICE needed)
pytest tests/test_disk_vice.py -v        # VICE disk I/O integration tests
```

Unit tests run without VICE. Integration tests (`test_disk_vice.py`) require both
`x64sc` and `c1541` on PATH and are automatically skipped if either is missing.

## License

MIT
