# c64-test-harness

Reusable test harness for Commodore 64 programs. Automates C64 programs via the VICE emulator's remote monitor, with an architecture that supports real hardware backends.

## Features

- **Transport abstraction** (`C64Transport` Protocol) — write tests once, run on VICE or hardware
- **Wrap-aware screen matching** — search for text that spans 40-column row boundaries
- **Fast keyboard injection** — batched writes to the keyboard buffer (10x faster than per-character)
- **Robust VICE monitor parsing** — works around known VICE text monitor bugs
- **Complete PETSCII/screen code tables** — full 256-entry mappings with extensibility
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
```

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
