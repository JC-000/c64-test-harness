# Tech stack

- **Language**: Python 3.10+ (currently verified on 3.13). Type hints throughout.
- **Build**: `hatchling` (`pyproject.toml`, `[build-system]`). Package layout is `src/c64_test_harness/` with `[tool.hatch.build.targets.wheel]`.
- **Runtime deps**: **none**. The harness is zero-dependency by design.
- **Dev deps**: `pytest>=7.0` only, via `[project.optional-dependencies] dev`.
- **Install mode**: editable (`pip install -e '.[dev]'`), usually into a dedicated venv at `~/.local/share/c64-test-harness/venv` created with `--system-site-packages`.
- **External binaries required at runtime**:
  - `x64sc` (VICE 3.10, built with `--enable-ethernet` for ethernet cart tests)
  - `c1541` (VICE disk image tool)
- **Optional external systems**:
  - Linux TAP/bridge networking (`br-c64`, `tap-c64-0`, `tap-c64-1`, iproute2, iptables, `/dev/net/tun`) for the ~5 ethernet peer-to-peer tests
  - Ultimate 64 hardware reachable via `U64_HOST` env var for live hardware-backed tests
- **No linters, formatters, or type checkers configured** in the repo. No ruff/black/mypy/flake8 config files. Tests are the primary gate.

## Package module map (src/c64_test_harness/)
- `runner.py` — scenario-based test runner w/ error recovery
- `transport.py` — `C64Transport` protocol abstraction
- `backends/` — concrete backends (VICE binary monitor, Ultimate 64)
- `memory.py`, `execute.py`, `debug.py` — low-level memory/code/breakpoint ops
- `screen.py`, `keyboard.py` — text-mode IO, wrap-aware screen search
- `disk.py` — c1541 wrapper for D64/D71/D81 image mgmt
- `ethernet.py`, `bridge_ping.py`, `uci_network.py` — CS8900a and U64 networking
- `sid.py`, `sid_player.py` — SID playback, PSID/RSID parser
- `tod_timer.py` — TOD clock testing
- `labels.py` — cc65/ACME/Kick Assembler label file parser
- `poll_until.py` — host deadline polling helper for 6502 roundtrips
- `parallel.py` — `run_parallel()`, port allocation across multiple VICE instances
- `config.py` — `HarnessConfig` (TOML + env var overrides)
- `verify.py` — PRG binary-vs-memory diff
- `encoding/` — PETSCII / screen code tables
