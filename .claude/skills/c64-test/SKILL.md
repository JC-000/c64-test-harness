---
name: c64-test
description: Write and run tests for Commodore 64 assembly programs using the c64-test-harness Python package. Covers VICE emulator lifecycle, Ultimate 64 hardware testing, backend-agnostic target selection via UnifiedManager, direct-memory testing via jsr(), UI-driven testing, parallel execution, cross-process device queueing, and common pitfalls.
user-invocable: true
allowed-tools: Bash, Read, Write, Edit, Grep, Glob
argument-hint: "[test-subject]"
---

# c64-test-harness Skill

You are an expert at writing and running tests for Commodore 64 assembly programs using the `c64-test-harness` Python package. This skill helps you write correct, reliable C64 tests on the first attempt.

## Platforms

The harness runs on **Ubuntu 25+** and **macOS 26 Tahoe (Apple Silicon)**. Full parity is a maintained property — tests must work on both or explicitly skip with a platform-specific reason. Bridge/ethernet tests dispatch all platform-specific constants through `tests/bridge_platform.py` (`IFACE_A`, `IFACE_B`, `BRIDGE_NAME`, `ETHERNET_DRIVER`, `SETUP_HINT`, `iface_present()`) — never hardcode `tap-c64-*` / `br-c64` / `/sys/class/net/...`. On macOS, `ViceProcess` auto-wraps x64sc with `sudo -n` when `ethernet=True` because VICE's pcap driver on `feth(4)` requires root; this needs a NOPASSWD sudoers entry for `/opt/homebrew/bin/x64sc`. See `docs/development.md` for the full setup (VICE via Homebrew, bridge lifecycle scripts, sudoers recipe).

## When to Use This Skill

Use this when:
- Writing new test suites for C64 assembly routines
- Debugging failing C64 tests
- Setting up VICE emulator instances for testing
- Setting up Ultimate 64 hardware as a test target
- Selecting a backend at runtime (VICE vs U64) via `UnifiedManager`
- Working with the direct-memory (jsr-based) test pattern
- Working with the UI-driven (menu navigation) test pattern
- Setting up parallel test execution across multiple VICE instances
- Running multiple agents against a shared U64 device (cross-process queueing)
- Testing CS8900a ethernet via two-VICE bridge networking (RR-Net mode)
- Testing UCI socket-level TCP/UDP on Ultimate 64 Elite (incl. turbo speeds)
- Writing shippable 6502 networking code that needs wall-clock timeouts (TOD helpers)
- SID file playback and audio capture on VICE or Ultimate 64

## Quick Reference

See the supporting files in this skill directory for detailed API reference:
- `REFERENCE.md` — Full API reference for all c64-test-harness modules
- `PATTERNS.md` — Battle-tested patterns, templates, and gotchas

## Core Principles

1. **ALWAYS use `ViceInstanceManager`** for VICE tests — it handles port allocation, PID tracking, transport creation, and cleanup. Never use `ViceProcess` or `PortAllocator` directly. This prevents port collisions and PID conflicts when multiple Claude agents run in parallel.
2. **Use `UnifiedManager` / `create_manager()` for backend-agnostic tests** — it selects VICE or U64 at runtime via the `C64_BACKEND` env var. For U64, it automatically wraps access with `DeviceLock` for cross-process queueing.
3. **All U64 access MUST use `DeviceLock`** — multiple agents sharing a single U64 device will corrupt each other's tests without it. `UnifiedManager` handles this automatically; if using `Ultimate64Transport` directly (e.g., in pytest fixtures), wrap with `DeviceLock` in a module-scoped fixture.
4. **Binary monitor transport only for VICE** — `BinaryViceTransport` is the sole VICE transport. It uses a persistent TCP connection via VICE's binary monitor protocol (`-binarymonitor`). There is no text monitor support.
5. **CPU auto-pauses on every command** — the binary monitor pauses the CPU when any command is sent. You must explicitly `resume()` to let the CPU run. `resume()` is non-destructive and does not close the connection.
6. **`wait_for_text()` works with binary transport** — it calls `resume()` between polls internally, so no workaround is needed. Pass `verbose=False` (default) or `verbose=True` for debug output.
7. **Use `jsr()` for direct-memory tests** — calls the subroutine and waits for the breakpoint via event-based `wait_for_stopped()`. No polling, no `poll_interval` parameter. VICE-only (not available on U64).
8. **Use `Labels.from_file()`** — never hardcode addresses; use label names from the assembler's output. `Labels` is a `collections.abc.Mapping[str, int]` (v0.12.4+), so `dict(labels)` and `for name, addr in labels.items()` just work.
9. **After `jsr()` returns, the CPU is paused at the breakpoint** — safe to read memory.
10. **No size limits** — binary transport handles arbitrarily large reads and writes (4096+ bytes verified). The `read_bytes()` and `write_bytes()` wrappers still contain chunking logic from the removed text monitor, but this is a no-op concern with binary transport — any size works.
11. **Persistent connection means no transient failures** — unlike per-command TCP connections, the binary transport maintains a single persistent connection. No retry wrappers needed around `jsr()`.
12. **Build before testing** — always `make clean && make` and verify the PRG exists.
13. **Use `inst.pid` and `inst.port` from the ViceInstance** — never hardcode ports, never use `vice.pid` from ViceProcess directly.
14. **Never `pkill x64sc`** — use PID-targeted cleanup only; other agents may have VICE instances running.
15. **Probe before connecting to U64** — `probe_u64(host)` checks ping + TCP + REST API with short timeouts. `Ultimate64InstanceManager.acquire()` does this automatically, skipping unreachable devices.
16. **Pass `turbo_safe=True` to UCI helpers at U64 speeds ≥ 4 MHz** — the FPGA behind `$DF1C-$DF1F` needs ~38 µs between accesses; without the fence, turbo-speed code double-latches writes and corrupts the UCI protocol. Every `uci_*` builder and helper accepts the kwarg. Default is `False` for backward compat.

17. **`DebugCapture` is only cycle-accurate at 1 MHz** — the U64E FPGA emits the UDP debug stream at a fixed ~850k entries/sec regardless of CPU turbo speed. At 1 MHz you get an essentially complete trace; at 4 MHz you get 1/4 of cycles, at 48 MHz ~1/48 (uniformly sampled, `packets_dropped` stays at zero because the rate limit is at the FPGA source). For complete traces, drop to `set_turbo_mhz(client, 1)` for the capture window. Turbo-speed capture is only sound for aggregate statistics (PC distribution, frequency maps); it is not sound for call-graph reconstruction, exact cycle counts, or sequential bus-state analysis. Measurement: `tests/test_u64_debug_stream_speed_live.py`.
18. **Ethernet bridge tests default to RR-Net mode** — `ViceConfig.ethernet_mode="rrnet"` matches ip65 and the physical RR-Net cart; TFE mode is kept only for backward compat. Always use the `bridge_vice_pair` fixture and `run_ping_and_wait` / `run_icmp_responder` orchestrators, which work in BOTH VICE normal and warp modes. For the `ethernet_interface` / `ethernet_driver` values, import from `tests/bridge_platform.py` (`IFACE_A`, `IFACE_B`, `ETHERNET_DRIVER`) — do not hardcode. On macOS, `ViceConfig.run_as_root` auto-resolves to True when `ethernet=True` and `ViceProcess` wraps the launch with `sudo -n`; broadcast-TX-then-RX-on-same-transport tests must drain the CS8900a RX FIFO (see `_drain_cs8900a_rx` in `tests/test_ethernet_bridge.py`) because libpcap self-delivers the sender's own frames on BPF.
19. **VICE TOD ≠ wall-clock** — VICE 3.10 CIA TOD is virtual-CPU-clocked (warp accelerates it ~31×). For code that must work in VICE warp, use the host-driven `run_ping_and_wait` / `poll_until_ready` orchestrators. For shippable pure-6502 code on real C64 / U64 / VICE normal, use `tod_timer.build_*` helpers.

## Test File Template

```python
#!/usr/bin/env python3
"""test_<name>_direct.py — Direct-memory <Name> tests.

Usage:
    python3 tools/test_<name>_direct.py [--iterations N] [--seed S]
"""

import os
import random
import subprocess
import sys

from c64_test_harness import (
    Labels, ViceConfig, ViceInstanceManager,
    read_bytes, write_bytes, jsr, wait_for_text,
)


PROJECT_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
PRG_PATH = os.path.join(PROJECT_ROOT, "build", "<name>.prg")
LABELS_PATH = os.path.join(PROJECT_ROOT, "build", "labels.txt")


def run_tests(transport, labels, iterations):
    passed = failed = 0
    # ... test logic here ...
    return passed, failed


def main():
    os.chdir(PROJECT_ROOT)

    iterations = 10
    seed = random.randint(0, 2**32 - 1)
    # ... parse args ...
    random.seed(seed)
    print(f"Random seed: {seed} (reproduce with --seed {seed})")

    # Build
    if not os.environ.get("C64_SKIP_BUILD"):
        subprocess.run(["make", "clean"], capture_output=True)
        result = subprocess.run(["make"], capture_output=True, text=True)
        if result.returncode != 0:
            print(f"Build failed:\n{result.stderr}")
            sys.exit(1)

    labels = Labels.from_file(LABELS_PATH)
    # Verify required labels exist
    for name in ["label1", "label2"]:
        if labels.address(name) is None:
            print(f"FATAL: '{name}' label not found")
            sys.exit(1)

    config = ViceConfig(prg_path=PRG_PATH, warp=True, ntsc=True, sound=False)

    with ViceInstanceManager(config=config) as mgr:
        inst = mgr.acquire()
        print(f"VICE PID={inst.pid}, port={inst.port}")

        transport = inst.transport
        grid = wait_for_text(transport, "Q=QUIT", timeout=60.0, verbose=False)
        if grid is None:
            print("FATAL: Main menu did not appear")
            sys.exit(1)

        # Safety: write JMP $0339 at $0339 so CPU loops harmlessly
        # after jsr() returns (prevents crash when BASIC ROM is banked out)
        write_bytes(transport, 0x0339, bytes([0x4C, 0x39, 0x03]))

        passed, failed = run_tests(transport, labels, iterations)

        mgr.release(inst)

    total = passed + failed
    print(f"\nResults: {passed}/{total} passed")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
```

## Backend-Agnostic Test Template (VICE or U64)

```python
#!/usr/bin/env python3
"""test_<name>.py — Backend-agnostic <Name> tests.

Works on both VICE and Ultimate 64. Set C64_BACKEND=u64 and U64_HOST=... for hardware.
"""

import os
import sys

from c64_test_harness import (
    create_manager, read_bytes, write_bytes, wait_for_text,
)


def run_tests(transport, backend):
    passed = failed = 0

    # Memory round-trip (works on both backends)
    write_bytes(transport, 0xC100, bytes([0xDE, 0xAD, 0xBE, 0xEF]))
    result = read_bytes(transport, 0xC100, 4)
    assert result == bytes([0xDE, 0xAD, 0xBE, 0xEF])
    passed += 1

    # Screen read (works on both)
    codes = transport.read_screen_codes()
    assert len(codes) == 1000
    passed += 1

    # VICE-only features (jsr, breakpoints, registers)
    if backend == "vice":
        from c64_test_harness import jsr, Labels
        # ... jsr-based tests ...
        pass

    return passed, failed


def main():
    # create_manager() reads C64_BACKEND and U64_HOST from env
    with create_manager() as mgr:
        with mgr.instance() as target:
            print(f"Backend: {target.backend}, PID: {target.pid}")
            passed, failed = run_tests(target.transport, target.backend)

    print(f"\nResults: {passed}/{passed + failed} passed")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
```

## U64 Pytest Fixture Pattern (with DeviceLock)

All U64 live test files MUST use DeviceLock in their fixtures:

```python
import os
import pytest
from c64_test_harness.backends.device_lock import DeviceLock
from c64_test_harness.backends.ultimate64 import Ultimate64Transport

_HOST = os.environ.get("U64_HOST")
_PW = os.environ.get("U64_PASSWORD")

pytestmark = pytest.mark.skipif(not _HOST, reason="U64_HOST not set")

@pytest.fixture(scope="module")
def transport():
    lock = DeviceLock(_HOST)
    if not lock.acquire(timeout=120.0):
        pytest.skip(f"Could not acquire device lock for {_HOST}")
    t = Ultimate64Transport(host=_HOST, password=_PW, timeout=8.0)
    yield t
    t.close()
    lock.release()
```

## Critical Gotchas

Read `PATTERNS.md` for the full list. The most common mistakes:

1. **Never use `ViceProcess` directly or `PortAllocator` manually** — always use `ViceInstanceManager`. It handles port allocation (cross-process safe via OS-level `bind()` + file-based `flock()` locks), PID tracking, transport creation, and cleanup. Without it, parallel Claude agents will collide on ports and kill each other's VICE instances.

2. **PETSCII filename case**: Use **lowercase** `c64_name` in `DiskImage.write_file()` so filenames match what the C64 keyboard generates (unshifted PETSCII $41-$5A).

3. **After direct jsr() tests, program state is lost** — CPU ends up in BASIC. To cross-validate with UI, restart with `send_text(transport, "RUN")` + `send_key(transport, "\r")`.

4. **`jsr()` is event-based, not polling** — it uses `wait_for_stopped()` internally. There is no `poll_interval` parameter. The binary monitor pushes async breakpoint events when a checkpoint fires.

5. **SEQ file reading requires SA >= 2** — SETLFS secondary address 0 is LOAD mode, not sequential read.

6. **Use `inst.pid` for PID, `inst.port` for port, `inst.transport` for transport** — these come from the `ViceInstance` returned by `mgr.acquire()`. Never construct transports manually.

7. **`resume()` is safe to call repeatedly** — it does not close the connection. It simply resumes CPU execution. The persistent connection remains open throughout the test session.

8. **All U64 access must use DeviceLock** — creating `Ultimate64Transport` without a `DeviceLock` will cause test failures when another agent uses the same device concurrently. Use `UnifiedManager` (automatic locking) or wrap with `DeviceLock` in fixtures.

9. **Probe U64 before connecting** — `probe_u64(host)` checks ping + TCP + API. `is_u64_reachable(host)` for a quick boolean. `Ultimate64InstanceManager.acquire()` probes automatically and skips unreachable devices.

10. **UCI networking needs `turbo_safe=True` at ≥ 4 MHz** — without it, tests hang in `uci_wait_idle` or return error 0x85 at 48 MHz. Scripts `probe_uci_network.py` / `test_uci_tcp_echo.py` hand-write 6502 outside the builders and run at 1 MHz only.

11. **Ethernet: use RR-Net, not TFE** — previously documented as "TFE only"; harness now defaults to RR-Net and c64-https uses it too. TFE was dropped because TX-after-RX and full ICMP round-trip fail without the RR-Net clockport enable at `$DE01` bit 0.

12. **TOD footprint is `$F0`-`$F5`** — don't interleave `tod_timer` reads with `bridge_ping._emit_read_frame` inside one routine; they share `$F1`-`$F4`.

If the user provides `$ARGUMENTS`, focus the test writing on that subject area. Otherwise, ask what assembly routine or feature they want to test.
