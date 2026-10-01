# c64-test-harness

Reusable test harness for Commodore 64 programs. One Python API drives two backends:

- the **VICE emulator** (`x64sc`, over its binary monitor on TCP), and
- **Ultimate hardware** over the firmware's `/v1/*` REST API: the Ultimate 64 / U64 Elite on the **3.x** firmware line, and the **C64 Ultimate** on the **1.x** (`u64ii`) line.

Tests written against the `C64Transport` protocol run unchanged on either one, and `UnifiedManager` / `create_manager()` hand you a ready transport for whichever backend you select. If you are driving an Ultimate device, start with [Choosing a backend](#choosing-a-backend).

## Getting started

Requirements: Python ≥ 3.10. The package has no runtime dependencies (`pytest` comes with the `dev` extra, `watchdog` with `notify`). The VICE backend needs **VICE 3.10** (`x64sc` and `c1541`), built with ethernet support if you want the CS8900a tests. The Ultimate backend needs only network reach to the device's REST API. VICE is not required for hardware-only use.

The canonical venv lives **outside the repo**, at `~/.local/share/c64-test-harness/venv`, on both platforms. The docs and helper scripts assume that path.

### macOS (Homebrew): the primary, current path

This is how the project's dev machine is set up. It was last checked on 2026-09-28:

- macOS 27.0 (build 26A428), Apple Silicon (arm64);
- Homebrew `vice` 3.10 at `/opt/homebrew/bin/x64sc` and `c1541`, where `x64sc -features` reports `HAVE_RAWNET yes` / `HAVE_PCAP yes`;
- the venv on Homebrew `python@3.13` (3.13.13).

```bash
brew install vice python@3.13

# Create the venv from a Python >= 3.10. The Xcode /usr/bin/python3 is 3.9 and too old.
/opt/homebrew/opt/python@3.13/bin/python3.13 -m venv --system-site-packages \
    ~/.local/share/c64-test-harness/venv
~/.local/share/c64-test-harness/venv/bin/pip install -e '.[dev]'

# Read-only check. --no-u64 keeps it off the network.
./scripts/verify-dev-env.sh --no-u64

# Optional: make the c64-test Claude Code skill available in every project.
./scripts/install-skill.sh
```

The Homebrew bottle already carries ethernet support, so you do not need a source build of VICE. The ethernet/bridge tests need three more steps. All are optional, and [docs/development.md § macOS (Homebrew)](docs/development.md#macos-homebrew) has them in full:

- `sudo ./scripts/setup-bridge-feth-macos.sh` creates `bridge10` + `feth0`/`feth1`, the macOS counterpart of Linux's `br-c64` + `tap-c64-{0,1}`. See [docs/bridge_networking.md](docs/bridge_networking.md).
- `sudo chmod o+rw /dev/bpf*` gives host-side capture access to the BPF nodes. The change is lost on reboot, and it must also cover `bpf4` and up on a machine where other rigs hold BPF nodes.
- A NOPASSWD sudoers drop-in for the three bridge scripts and for `/opt/homebrew/bin/x64sc`. On macOS, VICE's pcap driver needs **root**, and the harness launches it with `sudo -n`. Without the entry, an ethernet launch is refused up front with `ViceElevationRequiredError`, which prints the exact sudoers line to add.

`scripts/setup-dev-env.sh` also has a macOS branch (`brew install vice`, the venv, the feth bridge), added 2026-04-19. No run of that branch is recorded. The setup above is the one in use, not that script. The script builds the venv from whichever `python3` is first on `PATH`, so check that it is ≥ 3.10 before relying on it.

### Ubuntu 25: secondary, passed in a fresh VM 2026-09-28

> **Last run 2026-09-28: passed** in a fresh Ubuntu 25.10 aarch64 VM (lima, cloud image `ubuntu-25.10-server-cloudimg-arm64` 20260703, no host mounts, port forwarding off): install, idempotent re-run and `verify-dev-env.sh --smoke` all READY. That run first found six missing VICE build dependencies and a headless `--help` probe failure, now fixed ([#500](https://github.com/JC-000/c64-test-harness/issues/500)). A display-backed Ubuntu Desktop run is not recorded.

```bash
./scripts/setup-dev-env.sh --dry-run   # preview every action; changes nothing
./scripts/setup-dev-env.sh             # apt packages, VICE 3.10 source build with --enable-ethernet,
                                       # venv + editable install, bridge/TAP setup, final verify
```

Every stage is idempotent and can be skipped with a `--no-*` flag. The venv is mandatory on Ubuntu 23+, where PEP 668 blocks `pip install` against the system Python. Distro VICE packages usually lack `--enable-ethernet`, which is why the installer builds from source. Stage table, flags, recovery and the manual route are in [docs/development.md](docs/development.md#ubuntu-25-scriptssetup-dev-envsh).

### Verifying your dev environment

`./scripts/verify-dev-env.sh` is read-only by default and works on both platforms. Without `--smoke` it never starts an emulator session: it runs only `x64sc --version`, `--help` (retried as `x64sc -console --help` when a display-less GTK3 build prints no options), and `-features` as a macOS fallback, plus `c1541 --version`. `--smoke` is the one opt-in that does: it launches one headless VICE through the harness launcher, writes and reads back 8 bytes of RAM, and stops the process it started. It never runs pytest and never changes network state. It probes an Ultimate device (`GET /v1/version`) only when `U64_HOST` or `--u64-host` is set, and `--no-u64` turns that probe off. On macOS it checks `ifconfig`, that every existing `/dev/bpf*` node is other-rw (naming the ones that are not), NOPASSWD rules for the three bridge scripts and `/opt/homebrew/bin/x64sc`, and `bridge10`/`feth0`/`feth1`. On Linux it checks `ip`, `iptables`, `/dev/net/tun` and `br-c64`/`tap-c64-*`, and its install hints name `apt-get`, `dnf` or `pacman` from `/etc/os-release`. Output from the dev machine above (2026-09-28, bridge not set up). The `/dev/bpf*` row, and the ok/warn counts with it, come from the per-node check added for #502, run the same day on the same machine; the old `/dev/bpf0`-only check had reported that row ok:

```
[VICE]
  ✓ x64sc on PATH (/opt/homebrew/bin/x64sc)
  ✓ VICE version (VICE 3.10 [via brew; x64sc --version printed no version])
  ✓ ethernet cart support (ethernet flags found in --help)
  ✓ binary monitor support (-binarymonitor flag present)
  ✓ text monitor support (-remotemonitor flag present)
  ✓ c1541 on PATH (present)

[Python]
  ✓ python3 >= 3.10 (3.13.13 [harness venv: ~/.local/share/c64-test-harness/venv/bin/python3])
  ✓ c64_test_harness importable (version 0.11.3 [harness venv])
  ✓ pytest available (9.0.3)

[System tools]
  ✓ ifconfig command (/sbin/ifconfig)
  ⚠ /dev/bpf* other-rw (not other-rw: bpf4 (host-side packet capture and the ethernet TX/RX tests fail on any node they are handed that is root-only; VICE's pcap driver needs root regardless))
  ⚠ NOPASSWD sudo for setup-bridge-feth-macos.sh (no NOPASSWD entry (bridge setup will prompt for a password))
  ...
  ✓ NOPASSWD sudo for x64sc (NOPASSWD rule names /opt/homebrew/bin/x64sc)

[Bridge networking]
  ✗ bridge10 (not found)
  ...

Summary: 13 ok, 3 missing, 1 skipped, 4 warn
Overall: READY (with optional gaps)
```

The `x64sc --version printed no version` note is expected: the Homebrew build's `x64sc --version` exits early, so the script falls back to Homebrew's metadata. The harness version is read from the installed package metadata. This sample's `0.11.3` came from a venv installed before the current `0.13.0` in `pyproject.toml`, and it needs `pip install -e .` again to report the right number.

The VICE checks are **critical**, so a hardware-only machine without VICE reports NOT READY even though the Ultimate backend works. Options: `--quiet`, `--json`, `--no-u64`, `--u64-host HOST`, `--smoke`. Exit codes: `0` READY, `1` NOT READY, `2` script error. Details are in [docs/development.md](docs/development.md#quick-check-scriptsverify-dev-envsh).

## Choosing a backend

`create_manager()` reads `C64_BACKEND` (`vice`, the default, or `u64`). For hardware it also reads `U64_HOST` (comma-separated for a pool of devices) and `U64_PASSWORD`. You can pass the same things as arguments: `create_manager(backend="u64", u64_hosts="<device>")`.

```python
from c64_test_harness import create_manager, wait_for_text, send_text

# C64_BACKEND=u64 U64_HOST=<device> python3 my_test.py   (or unset for VICE)
with create_manager() as mgr:
    with mgr.instance() as target:            # target.backend is "vice" or "u64"
        wait_for_text(target.transport, "READY.", timeout=30)
        send_text(target.transport, "PRINT 2+2\r")
        wait_for_text(target.transport, " 4", timeout=10)
```

When the U64 backend is selected, the manager holds the device's cross-process `DeviceLock` for as long as you hold the target. On a U64E it also resets the covered config categories to the factory defaults on entry ([Unified Backend Manager](#unified-backend-manager)). A U64-only setup does not need VICE: the package imports and drives hardware without `x64sc` or `c1541` installed.

Hardware reading, in order:

- [Ultimate 64 Hardware Backend](#ultimate-64-hardware-backend): the transport, pooling, locking, config helpers and what is VICE-only.
- [docs/device_locking.md](docs/device_locking.md): the shared-device contract. Any tool that touches a shared device takes its lock first.
- [docs/u64_recovery.md](docs/u64_recovery.md): wedge tiers, recovery primitives, and the `/Temp` hygiene.
- **C64 Ultimate (firmware 1.1.0):** this firmware predates upstream 1541ultimate#686. It never collects the `/Temp` attachments that body-carrying REST calls leave behind, and a full `/Temp` crashes the firmware until someone power-cycles it. The harness arms its own hygiene pass on such firmware automatically; read [docs/u64_recovery.md](docs/u64_recovery.md) before looping uploads against one.

## Features

### Both backends

- **Transport abstraction** (`C64Transport` Protocol): write tests once, run them on VICE or hardware.
- **Backend-agnostic acquisition:** `UnifiedManager` / `create_manager()` return a `TestTarget` (`.transport`, `.backend`, `.pid`; `.client` returns the `Ultimate64Client` on U64 targets).
- **Wrap-aware screen matching:** search for text that spans 40-column row boundaries (`ScreenGrid`, `wait_for_text`, `wait_for_stable`).
- **Fast keyboard injection:** batched writes to the keyboard buffer, about 10x faster than one key at a time.
- **Memory helpers:** `read_bytes` / `write_bytes`, plus `read_word_le()` / `read_dword_le()` for the 6502's little-endian byte order.
- **`MemoryPolicy`:** a transport-level guard that refuses host writes into your program's memory ([below](#memory-safety-memorypolicy)).
- **PRG binary verification:** compare runtime memory against a PRG file to detect corruption.
- **Complete PETSCII/screen code tables:** full 256-entry mappings, extensible.
- **Test runner framework:** scenario-based testing with error recovery.
- **Parallel test execution:** `run_parallel()` distributes tests across a pool of VICE instances or Ultimate devices.
- **Cross-backend `run_subroutine()`:** one primitive for short routines. On VICE it uses `jsr()` (a binary-monitor checkpoint). On a U64 it uses a sentinel trampoline and a host poll (`poll_cadence` knob for sub-ms targets).
- **`run_prg_via_sys(target, prg)`:** loads a PRG with `write_memory` and starts it with a typed `SYS`, then resumes. The entry address comes from the BASIC stub (`parse_basic_sys_address`) or from `sys_addr=`. On the U64 this is the load path that keeps an external cartridge on the bus. The firmware's runner load (`run_prg` / `load_prg`) deselects it, stickily across resets, until `Cartridge Preference` is re-PUT, and this helper does that re-PUT (#211, #217).
- **SID playback:** `play_sid()` dispatches to VICE (IRQ stub) or the Ultimate's native `sidplay` runner. Includes a PSID/RSID parser.
- **Input simulation & display capture:** `inject_joystick` works on both backends (active-high: bit set = pressed; the U64 backend inverts internally for its active-low CIA ports). `inject_userport` is VICE-only. `read_framebuffer` + `read_palette` give raw VIC capture.
- **Cross-backend snapshots:** `extract_snapshot` / `restore_snapshot` move RAM (plus optional REU contents) between VICE and U64 through VICE's native `.vsf` format. Restore skips the live I/O window `$D000-$DFFF`, except color RAM. **The round trip has not been verified in both directions.** VICE-side extract was broken for the life of the feature: `read_memory(0x0000, 65536)` always raised, because VICE returns the MEM_GET payload length through a `uint16` and 65536 truncates to 0, so VICE had never actually produced a snapshot. That is fixed and covered by `tests/test_vice_binary.py::TestFullAddressSpaceRead`. The U64 extract side has no such live coverage yet, so treat U64→VICE as the direction this suite actually demonstrates. See [docs/snapshot_interop.md](docs/snapshot_interop.md).
- **Flexible configuration:** `HarnessConfig` with TOML file and environment variable support.

### VICE emulator

- **Binary monitor transport:** a persistent TCP connection over VICE's binary monitor protocol (~0.08 ms per command, no write size limits, async breakpoint events).
- **Execution control:** load code into RAM, call subroutines with `jsr()`, set breakpoints, and patch code at runtime.
- **Multi-instance VICE management:** run several emulators at once with thread-safe, cross-process port allocation.
- **VICE label file parser:** load cc65/ACME/Kick Assembler label files.
- **Debug utilities:** `dump_screen()` and `hex_dump()` for quick inspection during test runs.
- **Disk image management:** create, read and write D64/D71/D81 images via `c1541`, with auto-attach to VICE.
- **Ethernet / CS8900a:** RR-Net-mode ethernet cartridge emulation on TAP (Linux) or `feth` (macOS) interfaces, bridge networking for multi-VICE communication, and an auto-generated unique MAC per instance. A real RR-Net on the U64 is covered in [Ethernet / CS8900a Testing](#ethernet--cs8900a-testing).
- **Headless audio render:** `render_wav()` records WAV without a visible window.
- **Runtime warp toggle:** turn warp on or off at runtime. With the text monitor connected this is real warp; over the binary monitor alone it falls back to a `Speed`-resource pseudo-warp. `resource_get` / `resource_set` give general control of VICE resources.
- **Single-step / snapshots / trace:** `single_step` / `step_out`, conditional breakpoints (`set_condition`), instruction history (`cpu_history`, VICE 3.10+), `dump_snapshot` / `undump_snapshot`, and `banks_available` / `registers_available` introspection.
- **Deterministic test setup:**
  - Event replay: `event_snapshot_mode` → `EventStartMode` 0-3, plus `event_snapshot_dir` and `event_image_include`. Event *recording* has no CLI entry point in VICE 3.10.
  - `seed` for the RNG. It is emitted before VICE's pre-UI argv scan, the only place VICE honours it.
  - `sound_record_driver` / `_file` → `SoundRecordDeviceName` / `Arg`.
  - `exit_screenshot`.
  - There is no `load_snapshot` field, because VICE has no `-loadsnapshot` flag. Load a `.vsf` through the monitor's `undump_snapshot()`.
- **Text-monitor extras:** `detach_drive`, `attach_drive`, `screenshot_to_file`, and a 6502 profiler (`profile_start` / `profile_stop` / `profile_dump`).
- **PRG autostart mode pinned:** `ViceProcess` passes `-autostartprgmode 1` (inject into RAM) on every launch unless `extra_args` already carries `-autostartprgmode`. VICE's factory default is disk-image autostart.

### Ultimate hardware (U64 / U64E fw 3.x, C64 Ultimate fw 1.x)

- **REST transport:** `Ultimate64Transport` provides memory, screen, keyboard and reset over HTTP. `Ultimate64InstanceManager` pools several devices and works with `run_parallel()`.
- **Queue-aware device locking:** `DeviceLock` serializes access to a shared device across processes. It heartbeats while held, so a waiter queued behind a live, progressing holder keeps waiting instead of timing out. A chain of holders handing the lock on is capped (`_MAX_HOLDER_HANDOFFS`). `acquire_or_raise()` raises `DeviceLockTimeout`, whose message says whether you are queued, the holder looks wedged, or the lock is stale. `U64_DEVICE_LOCK_TIMEOUT` is a wait budget, not a gate. The optional `c64-test-harness[notify]` extra adds `watchdog`-based wakeups. Full contract in [docs/device_locking.md](docs/device_locking.md).
- **Known state on entry:** `apply_factory_baseline()` resets the covered config categories to the firmware's defaults and asserts `current == default`. The manager runs it at `acquire()` on a U64E ([below](#unified-backend-manager)).
- **Automatic `/Temp` hygiene on leak-prone firmware** (C64 Ultimate 1.1.0; any Ultimate-line < 3.15):
  - The client sweeps `/Temp` over FTP before the first upload after this process takes the device's `DeviceLock` (or its first upload ever), and before every further upload once one attachment is pending (budget 1, keep 1, mounted images kept; #511). It also sweeps on `close()` and on `DeviceLock` release.
  - If FTP is off at that first sweep it enables FTP File Service once per process and retries. It refuses body-carrying calls, before sending anything, if the sweep still cannot run, or if this process does not hold the device's `DeviceLock`.
  - The pass is prevention only. Once the firmware has crashed, FTP is gone too, and only a physical power-cycle recovers the device.
  - It stays disarmed on firmware that collects its own attachments (Ultimate-line ≥ 3.15).
  - Env knobs `U64_AUTO_TEMP_GC`, `U64_TEMP_GC_BUDGET`, `U64_TEMP_GC_KEEP` and `U64_TEMP_GC_REQUIRED=0` are covered, with the mechanism, in [docs/u64_recovery.md](docs/u64_recovery.md).
- **`Ultimate64Client` robustness:**
  - `send_text(text, *, finish_with_return=True)` waits for the KERNAL keyboard buffer to drain (`$C6 == 0`) before each chunk.
  - `run_prg(..., fallback_on_404=True)` sideloads via `write_mem` when a wedged runner answers HTTP 404 (observed on fw 3.14d).
  - Connection drops map to `Ultimate64TimeoutError`, and short `readmem` payloads raise `Ultimate64ProtocolError`.
  - `write_mem_query_threshold` is auto-detected per device: 128 on firmware without the Temp-folder fix, 48 on Ultimate-line ≥ 3.15.
- **Liveness probe:** `probe_u64()` / `is_u64_reachable()` check reachability (ping → TCP → REST). This is not a health check ([below](#liveness-probe)).
- **Configuration helpers:** turbo speed (cross-generation CPU-speed probing), REU size, SID sockets and addressing, disk mounting, PRG run/load, and snapshot/restore of device state.
- **Drive & disk fixtures:** `drive_on/off/reset/set_mode/load_rom`, blank-image creation with `create_d64/d71/d81/dnp`, `file_info`, `get_debug_register` / `set_debug_register` (`$D7FF`), `measure_bus_timing` (VCD), and batch `set_config_items_batch`.
- **U64 data streams over UDP, with gap detection:** audio (`capture_sid_u64()`, `capture_u64_audio()`, `AudioCapture`), VIC-II video frames (`VideoCapture`), and a cycle-accurate 6510/VIC bus trace (`DebugCapture`). `DebugCapture.with_fresh_fpga(client, *, capture_kwargs=None, reboot_settle_seconds=12.0)` reboots before each capture to recover from the debug-stream rate degradation that builds up under sustained workloads (issue #81).
- **UCI networking:** TCP/UDP sockets from C64 code through the firmware's lwIP stack ([below](#uci-networking-ultimate-command-interface)).
- **SocketDMA client:** `SocketDMAClient` on TCP 64 wraps capabilities REST does not expose: `inject_keys`, `reu_write`, `dma_load` / `dma_jump` / `dma_write`, `reset`, plus a UDP identify broadcast for LAN device discovery. The transport's SocketDMA *write fast path* is disabled pending a stability review ([below](#socketdma-write-fast-path)).
- **Syslog listener:** `U64SyslogListener` (`backends.u64_syslog`) consumes the firmware's UDP 514 raw-line syslog and offers `wait_for(predicate)` for assertion-driven tests.
- **Recovery:** `recover()` (`backends.ultimate64_helpers`) escalates reset → probe → `reboot()` → probe to clear CPU and REU/DMA stuck states, and never calls `poweroff()`. `runner_health_check()` raises `Ultimate64RunnerStuckError` on the firmware's "Cannot open file" wedged-runner signature. See [docs/u64_recovery.md](docs/u64_recovery.md).
- **`poweroff()` is guarded:**
  - `Ultimate64Client.poweroff()` raises `Ultimate64UnsafeOperationError` (from `backends.ultimate64_client`) unless it is called with `confirm_irrecoverable=True`.
  - After a power-off the device drops off the network, and only a physical power-cycle brings it back.
  - Use `reboot()` to recover a stuck device.

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

**Binary monitor note:** The binary monitor auto-pauses the CPU when any command is sent. Screen and keyboard operations need explicit `transport.resume()` calls between reads so the C64 can process keystrokes and update the screen. Use `wait_for_text()` or `wait_for_stable()` -- or `wait_for_memory()` for a flag byte -- which resume between polls *and* in a `finally`, so every exit path leaves the machine running. If you hand-roll a poll loop, resume in a `finally` — not just between polls, or a successful match hands back a stopped C64 that is indistinguishable from a hung one.

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

## Memory Safety (`MemoryPolicy`)

The harness writes DMA stubs to fixed C64 addresses (`$0334` for `jsr()` trampolines, `$C000-$CAC1` for UCI socket scaffolding and replies, etc.).  If your program also lives at any of those addresses, the writes silently collide — the 6502 has no MMU, so both writes succeed and the last one wins.  This has cost downstream consumers ~12 hours of bisection in at least one incident (issue #93).

`MemoryPolicy` is a transport-level write guard.  Declare your program's layout, attach the policy to the transport, and any `write_memory()` that would collide raises `MemoryPolicyError` *before* a byte crosses the wire:

```python
from c64_test_harness import MemoryPolicy, UnknownPolicy
from c64_test_harness.verify import PrgFile

prg = PrgFile.from_file("build/program.prg")
policy = MemoryPolicy.from_prg(prg, unknown=UnknownPolicy.WARN)
target.transport.memory_policy = policy
```

Or declare the layout in your `c64test.toml`:

```toml
[memory]
prg = "build/program.prg"
unknown_policy = "deny"
reserved_regions = [
    { range = "$4200-$50FF", note = "X25519 RODATA + BSS" },
]
```

Per-call `override="reason"` is the escape hatch for deliberate clobbers (logged at WARNING).  `MemoryArbiter` is the companion ergonomic — ask it for a scratch address and it returns one guaranteed to pass the policy (every candidate is verified via `check_write` before it is handed out).

Independent of the policy, reads and writes whose span runs past `$FFFF` raise `ValueError` at the transport instead of silently wrapping to `$0000` — `override=` does not bypass this; split the access at `$FFFF`.

See [docs/memory_safety.md](docs/memory_safety.md) for the full design, the harness's own scratch-address table, and the migration story.

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
| `goto(transport, addr, *, cold=False)` | Set PC and resume execution; `cold=True` also gives the target `SP=$FF` with `I`/`D` clear. The target runs only until the next monitor command -- wait on it with `wait_for_memory` (#514) |
| `set_breakpoint(transport, addr) -> int` | Set execution checkpoint, returns checkpoint ID |
| `delete_breakpoint(transport, bp_id)` | Remove a checkpoint |
| `wait_for_pc(transport, addr)` | Wait for CPU to stop at addr (uses async stopped events) |
| `jsr(transport, addr, *, preserve_state=True)` | Call a subroutine and wait for RTS (uses trampoline at `$0334`) |

`jsr()` writes a small trampoline (`JSR addr; NOP; NOP`) into the cassette buffer at `$0334`, sets a checkpoint after the `JSR`, resumes execution, and waits for the CPU to stop via async event. The CPU is paused when `jsr()` returns, so memory reads are safe. Works reliably even for long-running computations in warp mode.

`preserve_state=True`, the default, reads PC, SP and the status register before the hijack and writes them back after the routine returns. Without it, a call issued while the monitor happened to halt the CPU inside an interrupt abandons that handler's stack frame and leaves interrupts masked for the rest of the boot, stopping the jiffy clock and the keyboard scan with no error anywhere (issue #183). Consequences: the machine is left on its pre-call register file while the returned dict holds the routine's, so read the return value rather than the machine; `A`/`X`/`Y` are not restored; nothing is restored on timeout; and a routine whose point is its own `SEI`/`CLI` or `LDX #$FF / TXS` needs `preserve_state=False` or its work is undone. See `examples/direct_memory_test.py` for a complete demo.

`goto()` is the one-way counterpart and does **not** share that defect: there is no point at which control comes back, so there is nothing to restore and nowhere to put it — the abandoned frame is what `goto()` is for. And it cannot keep the machine running past the next monitor command: a `read_bytes()` after `goto()` halts the target again until `resume()`, so a bare read loop polls a frozen machine and a settle sleep only hides that for a target that finishes inside the sleep (#514). Wait with `wait_for_memory(transport, addr, expected, timeout=...)` (package root), which resumes between reads on VICE and never resumes the U64. What `goto()` does inherit is the halted machine's stack pointer and `I` flag. `cold=True` is the opt-in fix for that, writing `SP=$FF` and a status register with `I` and `D` clear in the same command as `PC`, so the target starts as if nothing had been running. It is not a reset: vectors, I/O and zero page are untouched, and a still-asserting interrupt source will fire as soon as `I` comes clear. The caller-side alternative, when the target should land back in BASIC rather than in your own code, is the warm-start `JMP ($A002)` idiom in `sid_player.py`.

**Host-side memory access reaches the chips under VICE but samples a machine the monitor has halted -- and never reaches a cartridge on the U64.** Under VICE, `read_memory()` / `write_memory()` go through the binary monitor's `default` bank, which is the 6510's current memory map: writes go through `mem_store` and are indistinguishable from CPU stores (a host write to `$D020`/`$D021` reads back on the 6510 as the new values), and reads go through the chips' side-effect-free *peek* path (`read_memory(0xD020)` returns the VIC's value with its unused-bit pattern; a CIA ICR peek does not clear it) -- measured on VICE 3.10. What the host cannot observe is *motion*: the monitor services commands once per frame from its vsync hook, so a command sent to a running machine always halts it at the same frame phase, and `read_memory(0xD012, 1)` returns the same raster line across resume-and-reread cycles (n=8: always 12), while a checkpoint halt reads whatever line the machine stopped on (60/140/220 after busy-waiting for those lines). So host-side *configuration* of static registers is fine; anything time-varying -- a raster position, a SID oscillator or envelope, a CIA timer or TOD -- must be sampled on the 6510 with the machine running: write the registers, sample into a RAM buffer, call it with `jsr()`, read only the buffer; `tests/test_sid_emulation_live.py` is the worked example. On the Ultimate 64 the reason is different and stronger: host `read_memory`/`write_memory` go through the REST/DMA path, which does not present the access on the expansion port -- the bytes a read of `$DE00` returns are neither meaningful nor reproducible, and a write does not read back (see [docs/bridge_networking.md](docs/bridge_networking.md#real-silicon-diverges-from-vice-in-three-ways)). Same advice, opposite mechanisms; a reader who learns only one will misjudge the other platform. It is still worth knowing before writing a probe, because the host-side version does not error -- it reports the same frozen answer for every configuration, including a healthy one.

`jsr(..., recover_on_timeout=True)` (issue #156) turns a hung routine into a `RoutineHung` (a `TimeoutError` subclass, so existing `except TimeoutError` callers are unaffected) instead of leaving the machine in an unknown state: on timeout it restores SP and probes the trampoline with an RTS to confirm the machine is callable again, exposing the outcome via `RoutineHung.recovered`, `.hung_pc`, `.elapsed`, and `.detail`.

`c64_test_harness.disasm` (`disassemble()`) is a small, dependency-free 6502/6510 disassembler covering all 256 opcodes, illegal ones included, for rendering a memory window around a stuck PC in failure messages — useful alongside `RoutineHung.hung_pc` above.

## Runtime Warp Mode Toggle

Toggle VICE warp mode at runtime to speed up long-running computations (e.g. crypto benchmarks) and then return to normal speed for screen verification. Warp control works on any `BinaryViceTransport` — no text monitor connection required:

```python
from c64_test_harness import BinaryViceTransport, ViceProcess, ViceConfig

config = ViceConfig(prg_path="build/app.prg")
with ViceProcess(config) as vice:
    transport = BinaryViceTransport(port=config.port)

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

The cross-backend equivalents `set_speed(None)` / `set_speed(1)` (warp on / off on VICE) therefore also work on default-constructed VICE targets.

**How it works:** VICE 3.10 exposes no warp control over the binary monitor (there is no `WarpMode` resource, and the `Speed` resource rejects the documented "unlimited" value 0), so `set_warp`/`get_warp` are hybrid. With a text monitor connected (`ViceConfig(text_monitor_port=...)`, or `ViceInstanceManager(enable_text_monitor=True)` to auto-allocate the port) they drive VICE's real `warp on`/`warp off` toggle — including detecting warp enabled via the `-warp` CLI flag. Without one, they fall back to setting the binary monitor's `Speed` resource to an effectively-unlimited percentage (pseudo-warp); on that path `get_warp` only reflects pseudo-warp set through the transport. The text monitor is otherwise only needed for the text-monitor extras (`attach_drive`/`detach_drive`, `screenshot_to_file`, the 6502 profiler).

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

`PortAllocator` manages thread-safe port assignment with dual-layer protection: OS-level `bind()` reservations and file-based `flock()` locks (`PortLock`). The file lock bridges the TOCTOU gap between closing the reservation socket and VICE binding to the port, making overlapping startup from independent processes completely safe. `ViceInstanceManager` handles the full lifecycle: allocate port, acquire file lock, launch VICE, connect binary transport with retries, verify PID ownership, and clean up on release. Failed acquisitions retry with exponential backoff (configurable via `max_retries`). Set `reuse_existing=True` to adopt already-running VICE instances instead of launching new ones. Any x64sc the harness launched and never stopped is stopped when the Python interpreter exits, and a parent-death watchdog sidecar (`backends/vice_watchdog.py`) terminates it when the interpreter is killed by a signal, SIGKILL included; call `ViceProcess.detach()` on one that must outlive it. On a sudo-wrapped (root) launch the watchdog can only signal the sudo wrapper, which relays SIGTERM; a root x64sc that ignores SIGTERM survives. Stress-tested with 3 concurrent agents × 6 workers across 5 phases (lock contention, VICE startup, mixed workloads, crash recovery, port exhaustion) with zero failures — see `scripts/stress_cross_process.py`.

Each `ViceInstance` exposes a `.pid` property (the OS process ID of the VICE process), and `SingleTestResult` includes the `.pid` of the instance that ran each test. This allows callers to track and manage only their own VICE processes — essential when multiple agents run tests concurrently.

`run_parallel()` is failure-isolated: an instance-acquire failure (e.g. port exhaustion) is recorded as a failed result for that one test instead of aborting the whole run and discarding completed results. When `max_workers` is omitted it defaults to `len(tests)` capped at 10 (the port-allocator budget cannot serve more concurrent instances anyway), and an empty test list returns an empty `ParallelTestResult`.

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
import sys
from c64_test_harness import TestRunner

runner = TestRunner()
runner.add_scenario("Full CSR", test_full_csr, recover_to_menu)
runner.add_scenario("CN only", test_cn_only, recover_to_menu)
results = runner.run_all()
runner.print_summary()
sys.exit(runner.exit_code)
```

`run_fn` returns `(ok, message)`, and `recovery_fn` is optional. If `run_fn` returns `ok=False` or raises (recorded as `FAIL` / `ERROR`), the runner calls the recovery function before it moves on to the next scenario.

## Configuration

`HarnessConfig` centralises all settings and can load from a TOML file or environment variables:

```python
from c64_test_harness import HarnessConfig

# From a TOML file
config = HarnessConfig.from_toml("c64_harness.toml")

# From environment variables: C64TEST_ + the upper-cased field name
# (C64TEST_VICE_PORT, C64TEST_VICE_HOST, ...)
config = HarnessConfig.from_env()

# Or construct directly with defaults
config = HarnessConfig(vice_port=6502, vice_warp=True)
```

Key fields: `vice_host`, `vice_port`, `vice_executable`, `vice_prg_path`, `vice_warp`, `vice_sound`, `vice_console`, `vice_minimize`, `screen_base`, `vice_port_range_start/end`, `vice_reuse_existing`, `vice_acquire_retries`, `exec_poll_interval`, `screen_poll_interval`.

**Window focus:** VICE launches headless by default (`ViceConfig.console = True`, passing `-console`): the full emulation runs — binary monitor, screen/VIC/SID state, `-exitscreenshot` — but no window is created, so VICE never activates and steals focus (on macOS the GTK3 build activates the app even when started `-minimized`). Set `console=False` in `ViceConfig` if you need a visible window; `minimize` (default True, `-minimized`) then controls whether that window starts minimized. (`HarnessConfig.vice_console` / `vice_minimize` exist as fields but nothing builds a `ViceConfig` from a `HarnessConfig`, so the TOML key and `C64TEST_VICE_CONSOLE` have no effect on the launch — set it on `ViceConfig`.)

**CPU jams (illegal opcodes):** what happens follows `ViceConfig.monitor`. With the binary monitor enabled (the default; `ViceInstanceManager` forwards its base config's value, in practice True) the harness passes `-jamaction 0`. VICE's gate is a *connected* client, not a configured one (`monitor_is_binary()` is `connected_socket != NULL`, S `monitor_binary.c:2110-2113`): while a binary-monitor client is connected VICE routes the "jam dialog" to it (S `machine.c:131-139`), the machine stops, and `wait_for_stopped()` raises `TransportError` naming the jammed address. With `monitor=False` the harness passes `-jamaction 1` instead, because under `0` a windowed VICE with no monitor client would open a blocking GTK jam dialog (S `machine.c:140`, `else if (!console_mode)`); under `1` (CONTINUE) the 6510 halts silently in place — the core's `JAM()` just does `CLK++` with no PC advance (S `maincpu.c:607-628`, the `default:` arm at `:625`) — so a jammed program looks like a hang, not an error. Under `monitor=True` with no client connected the same two outcomes apply: `console=True` halts silently (S `machine.c:140` is `else if (!console_mode)`), and `console=False` opens the GTK dialog. If you need jams reported, keep the monitor on and connected.

## Ultimate 64 Hardware Backend

`Ultimate64Transport` talks to an Ultimate 64 / U64 Elite (1541ultimate **Ultimate-line** firmware, versioned `3.x`) or a C64 Ultimate (**CBM-line**, versioned `1.x`) via its REST API over HTTP. No emulator, no TCP monitor — memory reads/writes and keyboard injection go through the device's DMA endpoints.

The two lines' firmware fixes land on separate schedules, so the harness never branches on a version-string prefix. `DeviceCapabilities` (`backends/u64_capabilities.py`) resolves each behaviour as a named capability with its own version rule, and reports `None` — not a guess — for anything the version string genuinely cannot settle (work merged after the `3.15` bump ships in builds that all report `"3.15"`). One gap to know about: `_CBM_WRITEMEM_FIXED_FROM` is `None`, so **no** CBM-line firmware can grade as carrying the Temp-folder fix — including the build that eventually ships it. The constant has to be set by hand when that firmware lands; the grade never changes on its own. What the harness does instead is say so: a C64U reporting firmware newer than `_CBM_LAST_KNOWN_UNFIXED` (1.1.0) while the constant is still `None` emits a `CbmFixConstantStaleWarning` (visible in pytest's warnings summary; escalate it with `-W error::c64_test_harness.CbmFixConstantStaleWarning`) plus a WARNING log line, once per version per process — and the device stays on the conservative threshold until someone checks that release and edits the constant (issue #248).

```python
from c64_test_harness import (
    Ultimate64Transport, ScreenGrid, send_text, send_key,
    wait_for_text, wait_for_stable,
)

transport = Ultimate64Transport(host="<device>")  # optional: password="..."
try:
    wait_for_text(transport, "READY.", timeout=10)
    send_text(transport, "PRINT 2+2\r")
    wait_for_stable(transport, timeout=5)
    grid = ScreenGrid.from_transport(transport)
    assert "4" in grid.text()
finally:
    transport.close()
```

**Large single-call `write_memory()` on hardware.**
- `Ultimate64Client.write_mem`'s POST form has no known upper bound.
- A controlled re-run read back 0/50 corrupted: 25 single 38,911-byte POSTs at `$0801` plus 25 sentinel-bed writes, on U64E fw 3.15 `4011c97c`, with the CPU paused and the payload below `$A000`.
- Issue #231 had reported one wrong byte in a 47,103-byte POST. It is closed as non-reproducing, not refuted: a running CPU and the ROM shadow above `$A000` explain it, and its build was not re-tested.
- Any bulk read-back check must pause the CPU and stay below `$A000` (see the `write_mem` docstring).

Multiple devices can be pooled with `Ultimate64InstanceManager` — the same pattern as `ViceInstanceManager`, compatible with `run_parallel()`:

```python
from c64_test_harness import Ultimate64Device, Ultimate64InstanceManager, run_parallel

devices = [
    Ultimate64Device(host="<device-a>"),
    Ultimate64Device(host="<device-b>"),
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

Environment variables: `C64_BACKEND` (`vice` or `u64`; `"auto"` with the variable unset means `vice`, so a hardware-only lane must set it or pass `backend="u64"`), `U64_HOST` (comma-separated for multiple devices), `U64_PASSWORD`.

When the U64 backend is selected, `UnifiedManager` automatically wraps device access with `DeviceLock` — an `fcntl.flock`-based cross-process lock that serializes access to each physical device. Multiple independent agents (separate OS processes) can safely target the same U64 without coordination; the lock file queues them automatically. This is the same kernel-enforced locking pattern used by `PortLock` for VICE port allocation.

**Known state on entry (issues #227, #285).** Setup can be verified; teardown cannot, because a killed run restores nothing. On a shared device, the previous lane's turbo, REU size, SID map or `Cartridge Preference` is what the next lane inherits.

- **What runs.** `acquire()` on the U64 backend runs `apply_factory_baseline(client)` after the `DeviceLock` is taken and before the target is handed out. That is a per-category `PUT /v1/configs/<category>:reset_to_default` over the stores in `BASELINE_CATEGORIES`: machine, SID addressing, audio, drive, tape, printer, LED, modem and UI, plus the C64U-only speaker mixer and keyboard lighting. It then asserts that every item reads `current == default`, using the firmware's own `default`.
- **Drift versus failure.** Drift found before the reset is logged at INFO. A mismatch after the reset raises `U64BaselineError`.
- **Memory only.** The reset leaves flash untouched.
- **What it never touches.** It never sends the global `configs:reset_to_default` route, and never touches a `BASELINE_NEVER_TOUCH` store: `Ethernet Settings`, `Network Settings`, the WiFi store, `SID Sockets Configuration`, `Clock Settings`. Each is refused with its reason before any request goes out. A PUT restoring detected SID-socket values would also apply socket voltage on a 6581 bench without the human 12 V approval (`u64_config.cc:698-705`).
- **When it runs.** This is resolved at `acquire()` from the device's generation (`BASELINE_ON_ENTRY_DEFAULT_BY_GENERATION`): on for the U64E, off for the C64U (WiFi device-loss risk, not `/Temp`), and off for an unreadable generation. `U64_BASELINE_ON_ENTRY=1`/`=0` overrides it either way, and an explicit `baseline_on_entry=` beats both. `HarnessConfig.u64_baseline_on_entry` is tri-state, with `None` meaning "nobody asked".
- **Previewing and exempting.** `apply_factory_baseline(client, dry_run=True)` returns the `BaselineReport` without writing. `exempt=[(category, item)]` skips a detection-derived item.

The reasons for each never-touch store, the precedence rules and the live coverage (`U64_BASELINE_LIVE`, `FLASH_BASELINE_LIVE`) are in [docs/development.md § Live tests reconcile at entry](docs/development.md#live-tests-reconcile-at-entry-they-do-not-rely-on-the-last-runs-teardown). `snapshot_state`/`restore_state` remain the courtesy on exit.

### Shared-device contract (`DeviceLock`)

**Any tool that touches a shared device must acquire that device's `DeviceLock` first, and hold it for the whole job.** This includes read-mostly tools: a reboot issued by somebody else invalidates a reader mid-measurement just as thoroughly as it invalidates a writer. The lockfile is machine-global (`$XDG_RUNTIME_DIR`, else `/tmp/c64-test-harness-<uid>`, keyed by the normalised host — case, scheme, path, trailing dot and `:80` fold to one lockfile since #434; a device on another port is `DeviceLock(device_key(host, port))`, from `backends.device_lock`; a hostname and its IP address are still two keys, so use one spelling per device — see [docs/device_locking.md](docs/device_locking.md)), so every checkout, venv, and downstream repo on the machine converges on the same lock without configuration.

The contract exists because unlocked access is not a theoretical problem. On 2026-07-19 a locked `c64-nist-curves` ECDSA bench was rebooted mid-sweep by two jobs that drove the same device without locking; the bench spent its per-primitive timeouts against a machine sitting at `READY` and the whole sweep was discarded.

```python
from c64_test_harness import DeviceLock, DeviceLockTimeout

lock = DeviceLock(host)
try:
    lock.acquire_or_raise(timeout=120.0)   # queue-aware: extends behind a live, progressing holder
except DeviceLockTimeout as exc:
    raise SystemExit(f"device busy: {exc}")  # the message says WHY: queued / wedged / stale / unreachable
try:
    ...  # the device is yours for the duration
finally:
    lock.release()
```

Top-level tools generally wrap that in an `acquire_device_lock_or_exit(host)` helper that stale-cleans first, prints the current holder before blocking, and exits non-zero on timeout rather than proceeding unlocked (see `c64-nist-curves`'s `tools/bench_u64_common.py` for a reference implementation).

Three mechanisms back the contract up:

| Mechanism | What it does |
|---|---|
| Advisory check in the client | `Ultimate64Client` checks, before every state-changing request (any non-GET: `machine:*`, runners, `writemem`, drive and config PUT/POSTs — which is how turbo and REU settings change) and before every destructive SocketDMA opcode, whether this process holds the device lock. If it doesn't, and another live process does, it logs a `WARNING` naming the holder PID. |
| `U64_REQUIRE_DEVICE_LOCK=1` | Turns that warning into a `DeviceLockContentionError` before the request goes out. Recommended for CI and for long benches on a shared device. |
| `device_lock_guard` fixture | Autouse fixture in `tests/conftest.py` that holds the lock around every `*_live.py` test for the resolved device host (module `_HOST` attribute, else `U64_HOST`), logging acquire and release. The harness's own live suite is a good citizen by construction; `U64_DEVICE_LOCK_TIMEOUT` overrides the 300 s queue timeout. |

The check is filesystem-only (one `open`, one non-blocking shared `flock`, one small read) and never touches the network. Liveness comes from the flock rather than the lockfile contents, because `release()` deliberately leaves the lockfile behind. **Single-user flows see nothing**: with no other live holder the check emits at most a `DEBUG` line, and a repeat offence against the same holder is logged once, not once per request.

A process that already holds a device's lock can re-enter the library — `create_manager()`, or a live test acquiring its own lock under the fixture — via `DeviceLock(host, allow_nested=True)`, which joins the existing hold (refcounted) instead of queueing behind itself. Without that flag two `DeviceLock` instances in one process contend exactly as two processes do, which remains the default.

#### The asymmetry the advisory check cannot cover

The advisory check only fires when the *other* lane took the lock. A lane that never touches this package takes no lock, is not a "live holder", and is therefore invisible to it — so the careful lane gets no protection and no way to detect the collision. On 2026-09-03 that cost a neighbouring project three test runs and nearly a physical power-cycle (issue #194): the unlocked lane's `run_prg` is a **load-and-run that replaces the running program**, not something that interleaves, so the displaced lane saw its own protocol answer nonsense and diagnosed device degradation.

Two things narrow the gap:

- **Constructing an `Ultimate64Client` with no lock held logs one `WARNING` per process per host**, naming the lockfile. Unlike the advisory check it needs no visible foreign holder — the point is to tell *you* that *you* are unlocked. Silence with `U64_UNLOCKED_CLIENT_WARNING=0`, `Ultimate64Client(host, warn_unlocked=False)`, or `suppress_unlocked_warning()` around a construction that is about to be followed by an acquire.
- **`device_lock_holder(host)` / `device_lock_path(host)`** are the cheap public "is anyone holding this right now?" query and the path it reads, for a runner that wants to check without adopting the package. If the import fails, **fail closed and refuse to run** — a silent unlocked run is the failure being guarded against.

**Do not build a wrapper on `read_info()`.** `release()` deliberately does not unlink the lockfile (unlinking would race a process that has opened the path and is about to `flock` it — flocks are per-inode), so **a lockfile naming a dead PID is the normal state after any completed run**, not a stale or wedged holder. `read_info()` names whoever held it *last*; `device_lock_holder()` / `DeviceLock.foreign_holder()` consult the flock and answer who holds it *now*. See [docs/device_locking.md](docs/device_locking.md) for the full contract, the fail-closed wrapper pattern, and why `Ultimate64Client` does not take the lock itself.

### Liveness Probe

Before connecting, probe whether a U64 device is reachable:

```python
from c64_test_harness import probe_u64, is_u64_reachable

# Quick boolean check
if is_u64_reachable("<device>"):
    print("Device is up")

# Detailed probe: ICMP ping -> TCP connect -> REST API check
result = probe_u64("<device>")
print(result.summary)  # "U64 at <device>:80 reachable (1.2ms)" or "... UNREACHABLE: <error>"
# result.reachable, result.ping_ok, result.port_ok, result.api_ok, result.latency_ms, result.error
```

The probe runs with short timeouts (2s ping, 2s TCP, 3s API) and fails fast — if ping fails, TCP and API checks are skipped. `Ultimate64InstanceManager.acquire()` uses the probe internally to skip unreachable devices and try the next one in the pool.

**It is a reachability probe, not a health check.** All three stages are read-only — ICMP ping, TCP connect, `GET /v1/version` (`ping_host`, `check_port`, `check_api` in `backends/ultimate64_probe.py`) — so a device that answers `/v1/version` while its memory-write path is dead reports `reachable=True` with every sub-check green (issue #241). Do not read a green `reachable` as "writes will work". Pass `check_write=True` to add a write-path check: it reads 8 bytes at `$0334`, writes their inverse with a query-string `PUT /v1/machine:writemem?data=` (no body, so no `/Temp` attachment on any firmware), reads them back and restores the original. It mutates RAM through the raw REST client, so hold the device's `DeviceLock` while it runs; `result.scratch_restored` says whether the bytes were put back. The verdict is `result.write_ok` (`None` when not asked), and `reachable` keeps its reads-only meaning. It checks the PUT path only. A device whose POST `writemem` alone is degraded can pass it, and `Ultimate64Client.liveness_probe()` remains the POST check — on leak-prone firmware that one is not free, since each call leaves `/Temp` attachments behind (issue #250), so reach for it deliberately rather than in a retry loop.

### Configuration helpers

Ergonomic wrappers over the firmware config API — turbo speed, REU size, SID sockets, disk mounting, PRG run/load, and full snapshot/restore:

```python
from c64_test_harness import (
    set_turbo_mhz, set_reu, set_sid_socket,
    mount_disk_file, unmount, run_prg_file, load_prg_file,
    snapshot_state, restore_state,
)

client = transport.client                # the helpers take the Ultimate64Client, not the transport

snap = snapshot_state(client)            # capture turbo/REU/SID config

set_turbo_mhz(client, 4)                 # superset: 1-6, 8, 10, 12, 14, 16, 20, 24, 32, 40, 48, 64
set_reu(client, True, size=16)           # MB int, or the enum string "16 MB"
mount_disk_file(client, "a", "build/disk.d64")   # drive, then path
run_prg_file(client, "build/app.prg")

# ... run tests ...
restore_state(client, snap)              # put device back as you found it
```

`mount_disk_file` and `run_prg_file` upload a body. On leak-prone firmware (the C64 Ultimate on 1.1.0) each such call leaves a `/Temp` attachment that the harness's hygiene pass has to collect. `run_prg_via_sys` loads a PRG with query-string PUTs instead, which leave no attachment.

CPU speeds are validated against the **superset** of enum values across device generations: the Ultimate 64 Elite supports 1–48 MHz including 5; the C64 Ultimate (fw 1.1.0) drops 5 and adds 64. `set_turbo_mhz` probes the connected device's actual `CPU Speed` presets (once, cached per client) and rejects a generation-foreign speed with a local `ValueError` before anything goes on the wire; only when the probe is inconclusive does the request reach the firmware, which rejects it with HTTP 400 (`Ultimate64Error`) — and because CPU Speed is written before Turbo Control is enabled, a rejected speed never leaves turbo half-enabled. The same probe backs `max_cpu_speed_mhz(client)`, so `transport.set_speed(None)` ("max speed") resolves to the device's true maximum — 64 MHz on a C64 Ultimate, 48 MHz on a U64 Elite, with a 48 fallback when the probe is inconclusive. See `tests/test_turbo_contract_live.py` (gated by `TURBO_CONTRACT_LIVE=1` + `U64_HOST` + `U64_ALLOW_MUTATE=1`).

`set_reu` is also cross-generation aware: U64E firmware 3.14 exposed `Cartridge` as an enum with a `"REU"` preset that had to be written alongside enabling the REU item; firmware 3.15 replaced it with a `.crt` file chooser (`presets: [""]`; firmware `c64.cc:73`) and the REU is controlled by `RAM Expansion Unit`/`REU Size` alone (its `Cartridge` value does not mirror REU state — it stays `""` with the REU enabled); the C64 Ultimate has no `"REU"` preset either (its `Cartridge` value merely mirrors REU state — writing it back is rejected with HTTP 400). The helper probes the device's `Cartridge` presets once per enable and writes the preset only where it exists — ordered first, so a firmware rejection can never leave the REU half-enabled. `restore_state` applies the same probe and the same Cartridge-first ordering when restoring a snapshotted state, so a C64U snapshot/restore round-trip is safe. `REU Size` read-back is trustworthy (issue #168, live-verified on U64E 3.15, `tests/test_reu_size_readback_live.py`): it is stable across quiet reads and reflects `set_reu` immediately, so two reads that disagree straddled a config write (another lane's `set_reu`/`restore_state`, `reset_config_to_default`), a reload from flash (`load_config_from_flash`), or a reboot/power-cycle — config PUTs are volatile until `save_config_to_flash` and a boot reloads flash (firmware `route_configs.cc:239, :329, :374`). Measured on the bench: flash held the item default `"2 MB"` while RAM held `512 KB`, the reporter's exact pair. Not firmware staleness.

See `examples/ultimate64_hello.py` for a full BASIC round-trip demo and `scripts/probe_u64.py` for device capability discovery.

### SocketDMA write fast path

> **Do not enable SocketDMA writes.** The write fast path is disabled pending a stability review; prefer `write_bytes` / `run_prg_via_sys` for bulk data. On an Ultimate transport they chunk at the transport's `rest_put_chunk_size` — the client's `write_mem_query_threshold`, capped at 128 — so every chunk takes the non-leaking `PUT ...?data=` path on **every** firmware grade, leak-prone, unknown and post-safe alike; VICE and other transports keep the fixed 84 bytes (`memory.py`, the text-monitor limit). On a post-safe device that means more, smaller requests (48-byte PUTs where it used to send 84-byte POSTs, roughly 1.75x as many), a cost accepted by owner decision 2026-09-15 (#252). The description below documents the mechanism for when it is re-enabled.

The Ultimate firmware serves a binary "SocketDMA" channel on TCP port 64 (on the C64 Ultimate it ships disabled — enable **Network Settings → "Ultimate DMA Service"** — and a refused connect simply falls back to REST). `Ultimate64Transport` can route bulk `write_memory` calls through it:

```python
transport.socket_dma = True                # opt-in (default False)
transport.socket_dma_min_bytes = 8192      # payloads >= this use DMAWRITE
transport.write_memory(0x4000, blob)       # 16 KiB in ~150 ms instead of >6 s
```

Motivation: on C64 Ultimate fw 1.1.0 the REST `POST writemem` path degrades sharply at 16 KiB (~6 s per request, measured; ≤12 KiB is fine). The fast path chunks at 32 KiB and passes the same `MemoryPolicy` checks as the REST path. `DMAWRITE` itself is fire-and-forget (no per-command ack), so the write finishes with an in-band `IDENTIFY` completion barrier — the firmware services commands on a connection strictly in order, so the `IDENTIFY` reply proves every preceding chunk was consumed and applied (the same barrier the REU `reu_write` path uses; its recv timeout scales with payload size). A tail read-back over REST follows as a post-barrier sanity check (budget `transport.socket_dma_verify_timeout`, default 2 s). Any connect failure, send failure, barrier failure, or verify miss logs a WARNING and falls back to REST; connect failures latch the fast path off for the transport's lifetime. **Firmware from commit fdb521a5 (2026-05-10) on — the v3.15 line and the U64E's fork — closes an idle SocketDMA connection after 1 s** (`socket_dma.cc` sets `SO_RCVTIMEO = 1 s` on the accepted socket and closes on the first `recv <= 0`; measured on the U64E fw 3.15: an IDENTIFY after a 0.90 s gap answers 3/3, after 1.00 s the peer has closed 3/3; v3.14d and the C64U's 1.1.0 have no socket timeout, where the reconnect below is a harmless spare handshake). A command written into that socket is accepted by the host kernel and never read, so a reused connection lost the first `DMAWRITE` after any gap of a second or more and the barrier then failed with `connection closed by peer` (issue #223: 50/50 barrier failures at a 1.5 s inter-write gap, 0/50 at 0.2 s, idle and under REST load alike; the failed write's bytes were in RAM 0/50 times). Two layers now cover it: `SocketDMAClient` reopens a connection idle for `IDLE_RECONNECT_SECONDS` (0.8 s) before the next command (`idle_reconnect=None` disables that), and the transport retries a failed send or barrier once on a fresh connection before the REST fallback — re-sending is idempotent for RAM (same bytes, same address; a double write read back identical 3/3) and is refused for any span touching the I/O window `$D000-$DFFF` (a second DMA there is a second register write), which goes straight to REST. Live: `tests/test_socketdma_barrier_live.py` (`SOCKETDMA_LIVE=1`). The raw client (`SocketDMAClient`: DMA load/run, REU write, keyboard inject, reset, identify) is exported from the package root for direct use.

### Reset vs Reboot

The Ultimate 64 has two reset modes, available as `Ultimate64Client.reset()` / `.reboot()` and as the helpers `reset(client)` / `reboot(client)` in `c64_test_harness.backends.ultimate64_helpers` (not exported at the package root):

- **`reset(client)`** — Soft C64 reset (6510 CPU only). Fast, but does not re-initialise the cartridge and REU state that `reboot()` does.
- **`reboot(client)`** — C64-level reset with cartridge and REU re-initialisation (`C64::start_cartridge(NULL)`). **Not a firmware reboot**: the firmware itself keeps running, so config held in firmware RAM, accumulated `/Temp` attachments and the lwIP/UCI stack state all survive it — which is why a reboot clears neither `/Temp` nor a UCI STATE-bit wedge. Takes ~8 seconds. **Required when switching turbo speeds between REU-heavy workloads** — stale DMA state from a prior turbo speed causes hangs after a soft reset.

### Recovery and wedge diagnosis

The U64 firmware has three independent wedge tiers (REST/writemem, runner subsystem, UCI STATE bits) and a separate probe + recovery primitive for each. Mis-diagnosing the tier — e.g. calling `recover()` for a UCI wedge — is the most common failure mode, because `recover()`'s liveness check is REST-only. See [docs/u64_recovery.md](docs/u64_recovery.md) for the diagnosis flow, the tier-to-recovery-primitive mapping, and the confirmed cases where physical power-cycle is the only option. Temp-folder accumulation is what upstream 1541ultimate#686 removes, and that merge is an ancestor of the `v3.15` tag — so on an Ultimate-line 3.15 build these tiers are diagnostic history; they remain live on the C64 Ultimate at 1.1.0. ("Root cause" overstates it: #686 removes the accumulation and with it the wedge, but the mechanism connecting the two is not established.) On a leak-prone device, recovery is the wrong frame — see the `/Temp` hygiene bullet above: the pass is prevention, and once the firmware has crashed only a physical power-cycle is left.

### DMA Trampoline Pattern (executing code without jsr)

The U64 has no CPU register control, so code is injected and triggered with DMA writes. For a routine that returns with `RTS`, `run_subroutine(target, addr)` packages this: a sentinel trampoline plus a host poll on the U64, and `jsr()` on VICE. Hand-roll the pattern below only when you need to hijack a program's own main loop:

```python
# SENTINEL/TRAMPOLINE are free RAM you pick. main_loop belongs to the
# program under test: read it from that build's ld65 label listing rather
# than hard-coding it — a copied literal went stale and cost a full session
# of uploads to notice (issue #439; see `_resolve_labels` in
# tests/test_u64_turbo_bench_live.py).
SENTINEL = 0x0350
MAIN_LOOP = labels["main_loop"]           # the program's parking JMP main_loop
# Same low byte as MAIN_LOOP, so the hijack below rewrites one byte (#426).
# Page $CD: $CD00 | lo plus the trampoline ends by $CE0A for any lo, outside
# HARNESS_SCRATCH ($0360-$036D, the old trampoline address here, is inside it,
# and so is $CF00, which $CE00 | lo reached for lo >= $F5; #477).
TRAMPOLINE = 0xCD00 | (MAIN_LOOP & 0xFF)
park = TRAMPOLINE + 8

# Write trampoline: JSR target; LDA #$42; STA sentinel; JMP * (park)
trampoline = bytes([0x20, target & 0xFF, target >> 8, 0xA9, 0x42,
                    0x8D, SENTINEL & 0xFF, SENTINEL >> 8,
                    0x4C, park & 0xFF, park >> 8])
assert TRAMPOLINE + len(trampoline) <= 0xCF00, "the trampoline runs into $CF00 scratch"
write_bytes(transport, TRAMPOLINE, trampoline)
write_bytes(transport, SENTINEL, bytes([0x00]))
# Only once MAIN_LOOP reads back as JMP MAIN_LOOP (the program is parked):
# rewrite the high operand byte alone. The 6510 is executing that JMP while
# the write lands, and a two-byte rewrite can be fetched torn (old low byte,
# new high byte) -- #426: 8 of 14 paired runs failed that way, 0 of 14 with
# the one-byte write (U64E fw bce4535e, 2026-09-22/23, paired).
write_bytes(transport, MAIN_LOOP + 2, bytes([TRAMPOLINE >> 8]))
# Poll sentinel for completion -- with a deadline, and through wait_for_memory
# so the same code is safe on VICE, where a bare read loop freezes the CPU (#514)
if wait_for_memory(transport, SENTINEL, 0x42, timeout=30.0, poll_interval=0.1) is None:
    raise TimeoutError("trampoline never set the sentinel")
```

### Turbo Benchmark

`scripts/bench_x25519_u64_turbo.py` benchmarks X25519 scalar multiplication across all turbo speeds. The figures below are a **dated measurement**, taken on an Ultimate 64 Elite running fw 3.14d with the script's old jiffy readout and a pre-c64-x25519-#35 build (2026-04-22), inferred from the jiffy readout having ticked. They have not been re-taken on a 3.15-line build; the script now reports CIA1 cycles (#477):

| MHz | C64 Time (jiffy readout, pre-c64-x25519-#35 build, 3.14d) | Speedup |
|-----|----------|---------|
| 48 | 12.0s | 13.6x |
| 32 | 13.4s | 12.2x |
| 16 | 18.1s | 9.1x |
| 8 | 28.2s | 5.8x |
| 4 | 48.3s | 3.4x |
| 2 | 81.3s | 2.0x |
| 1 | 163.7s | 1.0x |

**Limitations on hardware:** The REST API does not expose CPU registers or breakpoints. `jsr()`, `wait_for_pc()`, `set_breakpoint()`, and `set_register()` are VICE-only — they are not available on `Ultimate64Transport`. Tests that need register-precise execution control must use the VICE backend. To call a subroutine on both backends, use `run_subroutine(target, addr)`. Memory read/write, screen capture, keyboard injection, and screen-text waiting all work identically to VICE.

## SID Playback

Unified SID file playback API that works on both backends — same call, different plumbing underneath:

```python
from c64_test_harness import SidFile, play_sid
sid = SidFile.load("song.sid")
play_sid(transport, sid, song=0)  # works with BinaryViceTransport or Ultimate64Transport
```

`SidFile` parses PSID v1-v4 and RSID headers; `build_test_psid()` synthesizes minimal valid PSIDs for tests. `play_sid()` dispatches on transport type — VICE installs an 18-byte 6502 IRQ wrapper stub at `$C000` (configurable via `DEFAULT_STUB_ADDR`) that repoints the KERNAL IRQ vector at `$0314/$0315` through a `JSR play; JMP $EA31` trampoline, driving the tune from the KERNAL jiffy IRQ (~60 Hz). Ultimate 64 hands the `.sid` bytes to the native `POST /v1/runners:sidplay` firmware endpoint.

**VICE limitations:** PSID only — no IRQ-driven RSID support in the stub (a PSID whose `load_addr` is `0x0000` is fine: the embedded load address is used); `play_addr` must be non-zero (the VICE wrapper cannot host sample-driven tunes that have no play routine). Call `stop_sid_vice(transport)` to cleanly silence the SID and restore the original KERNAL IRQ vector.

**Ultimate 64:** the native `sidplay` runner accepts anything the firmware supports (PSID and RSID, including sample-driven tunes), so on hardware the `play_sid()` call just forwards the file bytes.

See `examples/play_sid.py` (supports `--vice` / `--u64 HOST` modes, plus `--self-test` which uses a built-in sentinel-counter PSID to verify init/play executed) and `scripts/play_scale_u64.py` for a full demo that builds a scale PSID on the fly, DMA-loads it, and plays it on hardware.

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

**On a real CS8900a** (external RR-Net on the U64E expansion port, live-verified fw 3.15) five things differ from VICE: set `C64 and Cartridge Settings -> Cartridge Preference = External` (on `Auto` the cartridge does not answer the identity read; the raw `$DE00` bytes are not reproducible); start the program with `run_prg_via_sys(target, prg)` -- the firmware's runner load path (`run_prg` and `load_prg` alike) deselects the external cartridge, stickily across every `reset()`, until `Cartridge Preference` is PUT again, which `run_prg_via_sys` now does (#217); program the MAC from the 6510 with `cs8900a_set_mac_inline_code(mac)` -- host-side `set_cs8900a_mac` never reaches the expansion port (#209); and resolve before the first ping -- a macOS host with no complete neighbour entry for the C64 holds every reply (entry absent 0/8, present 8/8; #212, #218 — the stale-entry case is inferred, not measured), so the ping builders take `arp_frame_buf=` and the responders answer ARP with `my_mac=` (#218); and drain the chip's RX queue before the first exchange with `drain_first=True` — the REST `reset()` does not reset the chip, so a "fresh" session inherits whatever arrived while idle and the chip then discards the reply for lack of buffer (paired n=6: 3/6 without a drain, 6/6 with; #222; the default is `False` and keeps existing routines byte-identical). Full register-level detail spans a section and its subsection of [docs/bridge_networking.md](docs/bridge_networking.md#real-silicon-diverges-from-vice-in-three-ways) — that heading's "three ways" is an accurate count of its own section, and most of the list above is in the subsection under it, [§ Driving a cartridge on the U64](docs/bridge_networking.md#driving-a-cartridge-on-the-u64).

**What a TX builder's result byte means** (full table in [docs/bridge_networking.md](docs/bridge_networking.md#tx-builder-hazards-and-what-now-guards-them-issues-234-235-236-238)): `0x01` is a *completion* flag, not a *delivery* flag — confirm delivery out of band, from a host-side counter or capture (#235). `0x04` (`RESULT_TX_NOT_READY`) means `Rdy4TxNOW` never asserted within the bounded poll (#236) and nothing was copied. Its measured cause on silicon is a TX buffer starved by unread RX frames (#303, provoked); unprovoked `0x04`s are attributed to it, and #234 is inferred to be the same state. Every TX site already SkipNows up to 8 received frames before that poll (#487); for a deeper queue pass `drain_first=True` or run `build_cs8900a_reset_code`.

**Shippable-application 6502 timeouts via CIA1 TOD** (`tod_timer` module): the host-driven `build_ping_and_wait_code` helpers above are great for tests but not for a real C64 application. For code that ships on a disk and runs standalone, use `c64_test_harness.tod_timer`, which emits pure 6502 poll loops that drive their own deadlines off the CIA1 Time-of-Day clock:

```python
from c64_test_harness import (
    build_tod_start_code, build_tod_read_tenths_code,
    build_poll_with_tod_deadline_code,
)

# Poll a CS8900a RxEvent register with a 5 s TOD-based timeout.
peek = bytes([0xAD, 0x05, 0xDE, 0x29, 0x0D])  # LDA $DE05; AND #$0D (CS8900A_RXEVENT_MASK)
code = build_poll_with_tod_deadline_code(
    load_addr=0xC000, peek_check_snippet=peek,
    result_addr=0xC1F0, deadline_tenths=50,   # 5.0 s
)
```

TOD runs at wall-clock rate on real C64, on Ultimate 64 Elite (flat 1.0x across the full 1-48 MHz turbo range), and on VICE 3.10 normal mode. It does **not** work under VICE warp mode, where TOD is virtual-CPU clocked; use the host-driven helpers in that case. Deadline cap is 599 tenths (59.9 s) per single call; longer waits require a caller loop. Zero-page footprint: `$F0`-`$F5`. See [docs/bridge_networking.md](docs/bridge_networking.md#test-harness-vs-shippable-application) for the full split.

For common ICMP scenarios the bridge_ping module ships higher-level wrappers that combine the TX/RX logic with a TOD-gated poll loop in one routine: `build_ping_and_wait_tod_code`, `build_icmp_responder_tod_code`, and `build_rx_echo_reply_tod_code`. They are drop-in shippable counterparts of the host-driven `build_ping_and_wait_code` / `build_icmp_responder_code` / `build_rx_echo_reply_code`. See `tests/test_bridge_ping_tod.py` for a full two-VICE bridge round trip using these variants on VICE normal mode, plus a live U64 TOD primitive test at 1 / 8 / 24 / 48 MHz turbo speeds (gated by `U64_HOST` and `U64_ALLOW_MUTATE=1`, since it changes CPU speed and restores it to the device defaults).

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

client = Ultimate64Client(host="<device>")
sid = SidFile.load("tune.sid")
result = capture_sid_u64(client, sid, out_wav="/tmp/u64_audio.wav", duration_seconds=10.0)
print(f"{result.packets_received} packets, {result.packets_dropped} dropped")
```

For low-level control, use `AudioCapture` directly — or `capture_u64_audio()`, which brings the stream up and down around an arbitrary run without resetting the machine (`capture_sid_u64()` resets in its `finally`, which destroys a host-driven run):

```python
from c64_test_harness import (
    capture_u64_audio, run_subroutine, wait_for_text, U64_NTSC_AUDIO_RATE_HZ,
)

with capture_u64_audio(target.client, "/tmp/run.wav",
                       sample_rate=U64_NTSC_AUDIO_RATE_HZ) as captured:
    run_subroutine(target, 0xC000)
    wait_for_text(target.transport, "DONE")
assert captured[0].time_base_intact      # every lost packet was zero-filled in place (#410)
assert captured[0].fill_fraction < 0.05  # zeros are not signal: bound them, or skip filled_frame_ranges
# A late packet overwrites its own fill and a duplicate is discarded, so
# neither is a drop; packets_reordered counts them (#205, #430).
```

**Three traps in this area produce plausible data rather than an error**, all documented with source citations in [docs/sid_audio.md](docs/sid_audio.md):

- `ViceConfig.sound=False` (the default) disables SID *emulation*, not just output — `$D41B`/`$D41C` then return `maincpu_clk % 256`, a clean ramp that looks like a working oscillator. Use `headless_sid_config()` for SID measurement. `sounddev="dummy"` is not a substitute; it stalls the SID instead.
- VICE discards the sample buffer under warp, so a warped audio capture is a well-formed *empty* WAV. `-soundrecdev` does not rescue it; warp must be off. `render_wav()` enforces this.
- The U64's NTSC stream is `2109375/44 = 47940.34` Hz, not 48000 — 1244 ppm, ~75 ms of slip per minute. `DEFAULT_SAMPLE_RATE` keeps its nominal value; pass `U64_NTSC_AUDIO_RATE_HZ` (an exact `Fraction`) for timing-sensitive work. `phi2 : audio` locks at exactly `64 : 3` — harness-verified on hardware to +8.8 ppm over a 60 s window (#205, `tests/test_audio_rate_lock_live.py`); runs with any dropped packet are meaningless and the test discards them.

Remapping SIDs for a comparison run needs `isolated_sid_addressing()`: the device ships with `Auto Address Mirroring` enabled, and distinct base addresses alone are **not** enough to stop one chip answering for another. The helper reads the whole map back and raises `Ultimate64Error` on a mismatch (#204, hardware-verified: distinct decode 27/27 with mirroring off, aliasing with it on; a mocked client whose category read does not reflect writes now raises).

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
ip = uci_get_ip(transport)   # e.g. "192.0.2.64" (dotted quad)

# TCP socket roundtrip
sock_id = uci_tcp_connect(transport, "example.com", 80)
uci_socket_write(transport, sock_id, b"GET / HTTP/1.0\r\n\r\n")
data = uci_socket_read(transport, sock_id)
uci_socket_close(transport, sock_id)
```

The module also supports UDP sockets (`uci_udp_connect`), TCP listeners (`uci_tcp_listen_start`/`uci_tcp_listen_state`/`uci_tcp_listen_socket`/`uci_tcp_listen_stop`), and low-level assembly builders (`build_uci_probe`, `build_tcp_connect`, etc.) for custom 6502 routines. DNS resolution is handled by the firmware — hostnames work directly.

`uci_socket_read` takes `max_len` up to 1472 (above 253 it drains the firmware's Data More blocks; above 893 it needs firmware carrying upstream #802, and refuses locally otherwise — see [docs/uci_networking.md](docs/uci_networking.md#datagram-size-limits)). A multi-block reply shorter than its header raises `UCISocketReadTruncatedError`. A C64 reset closes every UCI socket, and a read, write or close on a handle the firmware does not own raises `UCISocketNotOwnedError`; both are `UCIError`s exported at the package root.

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

TestTarget: backend-agnostic handle (.transport, .backend, .pid; .client on U64)
create_manager(): factory from env vars (C64_BACKEND, U64_HOST)

Screen/Keyboard/Memory modules sit above the transport:
  ScreenGrid, wait_for_text, send_text, read_bytes, etc.

Ethernet:
  ethernet.py: generate_mac, set_cs8900a_mac (CS8900a IA programming, host-side: VICE)
  bridge_ping.py: cs8900a_set_mac_inline_code / cs8900a_set_mac_code (6510-side IA programming, required on hardware)
  ViceConfig: ethernet=True, ethernet_mac auto-assigned by manager

U64 Data Streams (UDP capture):
  AudioCapture  -> CaptureResult          (port 11001, stereo PCM, 47940.34 Hz on NTSC)
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
| `scripts/run_all_tests.py` | Legacy phased runner over a hard-coded list of 22 test files, not the full suite (see [Running Tests](#running-tests)) |
| `scripts/stress_port_allocation.py` | Cross-process port allocation stress test |
| `scripts/stress_cross_process.py` | Multi-agent VICE instance management stress test (5 phases) |
| `scripts/probe_u64.py` | Probe an Ultimate 64 device (firmware, endpoints, config surface) |
| `scripts/play_scale_u64.py` | Build + play a C-major scale PSID on an Ultimate 64 |
| `scripts/bench_x25519_u64_turbo.py` | X25519 benchmark across U64 turbo speeds (1–48 MHz) |
| `scripts/stress_u64_queue.py` | Cross-process DeviceLock stress test (N workers × M rounds) |
| `scripts/run_u64_parallel_locked.py` | Run all U64 live tests in parallel files, per-test DeviceLock via conftest |
| `scripts/play_chromatic_u64.py` | Build + play a C3-C5 chromatic scale PSID on an Ultimate 64 (`--sid 6581\|8580` instrument parameters, `--save` writes the PSID) |
| `scripts/run_all_u64_live.py` / `scripts/run_sid_u64_live.py` | Run the live U64 test modules (or the SID module) against a device you name |
| `scripts/setup-bridge-tap.sh` | Create bridge + 2 TAP interfaces for multi-VICE ethernet |
| `scripts/teardown-bridge-tap.sh` | Tear down bridge + TAP interfaces |
| `scripts/cleanup-bridge-networking.sh` | Emergency bridge recovery (scoped VICE kill + iptables/TAP teardown) |
| `scripts/setup-bridge-feth-macos.sh` / `teardown-bridge-feth-macos.sh` / `cleanup-bridge-feth-macos.sh` | macOS counterparts: `bridge10` + `feth0`/`feth1` setup, teardown, and emergency recovery |
| `scripts/probe-vice-feth.sh` | macOS smoke test that VICE's pcap driver launches against a `feth` interface and serves the binary monitor |
| `scripts/cleanup_vice_ports.py` | Port-range-scoped VICE killer (resolves PIDs via `/proc/net/tcp` on Linux, `lsof` on macOS; verifies the process name, then SIGTERM, then SIGKILL — never `pkill`) |
| `scripts/setup-tap-networking.sh` | Create single TAP interface with NAT for VICE ethernet |
| `scripts/teardown-tap-networking.sh` | Tear down single TAP interface |
| `scripts/validate_ping.py` | End-to-end ARP + ICMP ping through VICE CS8900a + TAP |
| `scripts/bridge_ping_demo.py` | Visible two-VICE bridge ping demo (RR-Net, live on-screen counters; supports `--warp` via host-side wall-clock orchestrators) |
| `scripts/verify_tod_warp.py` | Empirical CIA TOD behavior probe in normal vs warp mode (regression check for the wall-clock timeout design) |
| `scripts/verify-dev-env.sh` | Non-destructive dev environment check (VICE build flags, Python harness, bridge interfaces, optional U64 probe) |
| `scripts/setup-dev-env.sh` | Fresh-machine installer. On Ubuntu 25: apt packages, VICE 3.10 source build, harness venv, bridge setup, final verify run. It also has a macOS/Homebrew branch, with no recorded run. Idempotent and `--dry-run` safe. The Ubuntu path passed end to end in a fresh Ubuntu 25.10 aarch64 VM on 2026-09-28 (see [Getting started](#getting-started), #500) |
| `scripts/install-skill.sh` | Symlink the `c64-test` Claude Code skill into `~/.claude/skills/` (`--dry-run`, `--uninstall`) |
| `scripts/gen_memory_table.py` | Regenerate / check (`--write` / `--check`) the scratch-address table in `docs/memory_safety.md` from `HARNESS_SCRATCH` |

The remaining scripts in `scripts/` are one-off probes and diagnostics behind specific issues. Each one's docstring says what it measures.

## Running Tests

The gate is plain pytest from the canonical venv. The repo has no CI, so the local run is the only check.

```bash
PYTEST=~/.local/share/c64-test-harness/venv/bin/pytest

$PYTEST                                   # the whole suite (testpaths = tests)
$PYTEST tests/test_ultimate64_client.py   # one file
$PYTEST --collect-only -q                 # collection sanity check, runs nothing
```

A bare `pytest` is **not** "unit tests only". It collects every module, and the VICE tests skip only when `x64sc` is not on `PATH`, so on a machine with VICE installed it spawns emulators. Do not run two VICE-spawning suites at once on one machine. Ultimate live tests skip unless `U64_HOST` is set, and the ethernet/bridge tests skip unless their elevated prerequisites are present. Pytest prints an `ELEVATION REQUIRED` section with the exact remedy at session end. To turn silent skips into failures, set `C64_REQUIRE_VICE=1` / `C64_REQUIRE_ELEVATION=1` ([docs/development.md](docs/development.md#live-test-gates-c64_require_vice--c64_require_elevation)).

`scripts/run_all_tests.py` predates most of the suite. It runs a hard-coded list of 22 test files in three phases (unit / `c1541` / VICE) and is not a full-suite runner.

```bash
# Ultimate 64 live tests. Each needs U64_HOST; suites that change device config
# also need U64_ALLOW_MUTATE=1 (resets, RAM writes and stream start/stop run on
# U64_HOST alone, #333). No script or live module has a default host: name the
# device or it refuses with exit 2 / skips (#243, #275).
U64_HOST=<device> U64_ALLOW_MUTATE=1 $PYTEST tests/test_u64_feature_parity_live.py -v
TURBO_CONTRACT_LIVE=1 U64_HOST=<device> U64_ALLOW_MUTATE=1 $PYTEST tests/test_turbo_contract_live.py -v

# 12 run_prg uploads per session: each body-carrying upload leaves a /Temp
# attachment on leak-prone firmware, so point this at a post-safe device.
U64_HOST=<device> U64_ALLOW_MUTATE=1 X25519_PRG=/path/to/x25519.prg $PYTEST tests/test_u64_turbo_bench_live.py -v

# All U64 live modules, per-test DeviceLock via conftest
python3 scripts/run_u64_parallel_locked.py <device>

# Stress the cross-process queueing (6 workers, 5 rounds each)
python3 scripts/stress_u64_queue.py <device> --workers 6 --rounds 5
```

Every opt-in gate (`*_LIVE=1`, `U64_DESTRUCTIVE`, `BRIDGE_CLEANUP_LIVE`, ...) is listed with what it needs and what it pins in [docs/development.md § Hardware and network live gates](docs/development.md#hardware-and-network-live-gates-all-opt-in-skip-cleanly-when-unset).

Every live test runs inside the autouse `device_lock_guard` fixture, so `DeviceLock` serializes access to the physical device whether or not the test asks for it. Separate OS processes can run tests in parallel safely, because the lock file queues them. See [Shared-device contract](#shared-device-contract-devicelock) for what that obliges non-test tools to do. Set `U64_REQUIRE_DEVICE_LOCK=1` to make an unlocked destructive call an error instead of a warning.

`U64_ALLOW_MUTATE=1` covers device config changes only; resets, RAM writes and stream start/stop run on `U64_HOST` alone (owner decision 2026-09-15, #333). Live suites that change device config (`test_multi_sid_parallel_live.py`, the turbo and SocketDMA suites) are double-gated behind `U64_HOST` **and** `U64_ALLOW_MUTATE=1`, and with either unset they skip cleanly. A few suites also skip their resets or RAM writes without the gate (`test_u64_feature_parity_live.py`, `test_ultimate64_client_writemem_live.py`, `test_socketdma_barrier_live.py`), which is stricter than the contract requires. The UCI UDP live probes (`test_uci_udp_send_live.py`, `test_uci_udp_send_large_live.py`) take the device address from `U64_HOST`. They also need their `UCI_UDP_LIVE=1` gate, and `U64_ALLOW_MUTATE=1` because they enable the Command Interface, a config write (#268). None of them hardcodes an IP.

## Contributing

Every change to this repo goes through **red/green with mutation checks and an adversarial review** before merge: write or change the test and show it failing against the code as it was; make it pass, then break the code under test on purpose and record which tests catch it; then run the standing `adversarial-reviewer` agent (`.claude/agents/adversarial-reviewer.md`) on the branch and answer every finding to a MERGE verdict. Follow-ups are filed as issues, not left as notes in a PR body. Full statement in [docs/development.md](docs/development.md#review-standard-adversarial-review-and-redgreen-every-change).

## Claude Code Skill

A project-level Claude Code skill lives at `.claude/skills/c64-test/`. When Claude Code runs inside this repo it is auto-discovered, giving the agent battle-tested patterns, a full API reference, and the gotcha list up front — no one-shot re-discovery of harness conventions.

- `SKILL.md` — when to use it, core principles, test-file templates
- `REFERENCE.md` — module-by-module API reference
- `PATTERNS.md` — patterns (VICE management, jsr-based testing, parallel, U64 DMA trampoline, bridge networking, UCI) + a numbered gotcha list

The skill is versioned alongside the code, so every merge updates it with the harness. If you maintain a user-level copy at `~/.claude/skills/c64-test/`, the project-level version takes precedence when Claude Code is launched from this repo.

## License

MIT
