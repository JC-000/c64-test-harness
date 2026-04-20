# c64-test-harness Patterns & Gotchas

## Pattern 1: VICE Instance Management (MANDATORY)

**ALWAYS use `ViceInstanceManager`** for all tests — single-instance or parallel. This is the only safe way to manage VICE instances when multiple Claude agents may be running concurrently.

### Why ViceInstanceManager is mandatory

- **Port safety**: `ViceInstanceManager` uses `PortAllocator` internally with OS-level `bind()` + file-based `flock()` locks. The file lock eliminates the TOCTOU gap between closing the reservation socket and VICE binding to the port. Without it, another agent can steal the port during that window.
- **PID verification**: After VICE starts, the manager verifies via `/proc/net/tcp` that the correct process is listening. PID mismatches trigger a retry.
- **Retry with backoff**: Failed acquisitions retry automatically (configurable `max_retries`, default 3) with exponential backoff.
- **PID tracking**: `inst.pid` reliably identifies your VICE process. Without it, agents resort to `pkill x64sc` which kills other agents' instances.
- **Binary transport creation**: `inst.transport` is a `BinaryViceTransport` pre-configured with the correct port. No manual construction needed. The manager uses a retry-connect pattern internally (TCP connect retries until VICE's binary monitor is ready).
- **Cleanup**: The context manager ensures VICE processes are stopped, file locks released, and ports freed, even on exceptions.

### Single-instance pattern

```python
from c64_test_harness import ViceConfig, ViceInstanceManager, Labels, wait_for_text, write_bytes

config = ViceConfig(prg_path="build/program.prg", warp=True, ntsc=True, sound=False)

with ViceInstanceManager(config=config) as mgr:
    inst = mgr.acquire()
    print(f"VICE PID={inst.pid}, port={inst.port}")

    transport = inst.transport  # BinaryViceTransport instance

    grid = wait_for_text(transport, "Q=QUIT", timeout=60.0, verbose=False)
    if grid is None:
        print("FATAL: Main menu did not appear")
        sys.exit(1)

    # Safety loop for jsr() (prevents crash when BASIC ROM is banked out)
    write_bytes(transport, 0x0339, bytes([0x4C, 0x39, 0x03]))

    # ... run tests using transport ...

    mgr.release(inst)
```

### WRONG — never do this

```python
# WRONG: Manual PortAllocator — race conditions with parallel agents
allocator = PortAllocator(port_range_start=6511, port_range_end=6530)
port = allocator.allocate()
reservation = allocator.take_socket(port)
reservation.close()
config = ViceConfig(prg_path=PRG_PATH, port=port, ...)
with ViceProcess(config) as vice:
    transport = BinaryViceTransport(port=port)  # WRONG: manual transport

# WRONG: Default port — collides with other agents
config = ViceConfig(prg_path=PRG_PATH, port=6502, ...)
with ViceProcess(config) as vice:
    transport = BinaryViceTransport(port=6502)  # WRONG: hardcoded port
```

---

## Pattern 2: Direct-Memory Testing (jsr-based)

The fastest and most reliable pattern. Write data to memory, call assembly routines via `jsr()`, read results from memory. Bypasses the C64 UI entirely.

```python
def test_routine(transport, labels, input_data):
    """Generic pattern for testing any assembly routine via direct memory."""
    # 1. Write input data
    write_bytes(transport, labels["input_buffer"], input_data)
    write_bytes(transport, labels["input_length"], bytes([len(input_data)]))

    # 2. Call routine(s) — sequential jsr() calls work perfectly
    #    Binary transport stays connected between calls (no reconnection)
    jsr(transport, labels["routine_init"], timeout=5.0)
    jsr(transport, labels["routine_process"], timeout=10.0)
    jsr(transport, labels["routine_finalize"], timeout=5.0)

    # 3. Read output (CPU is paused after last jsr)
    result = read_bytes(transport, labels["output_buffer"], 32)
    return result
```

### Why sequential jsr() calls work
The binary transport maintains a persistent TCP connection. After `jsr()` returns, the CPU is paused at the breakpoint. The breakpoint is deleted, but the connection stays open and the CPU remains paused. The next `jsr()` writes a new trampoline and resumes — no reconnection needed.

---

## Pattern 3: UI-Driven Testing

For testing user-facing flows (menu navigation, screen output, disk I/O).

`wait_for_text()` works correctly with binary transport -- it calls `resume()` between polls internally, so the C64 program continues updating the display while polling.

```python
def test_via_menu(transport, labels):
    # Navigate menu
    send_key(transport, "2")  # Select option 2
    grid = wait_for_text(transport, "ENTER TEXT", timeout=30.0, verbose=False)
    if grid is None:
        return False, "Menu prompt did not appear"

    time.sleep(0.1)  # Small delay for keyboard buffer
    send_text(transport, "HELLO WORLD")
    time.sleep(0.1)
    send_key(transport, "\r")

    # Wait for result
    grid = wait_for_text(transport, "Q=QUIT", timeout=30.0, verbose=False)
    if grid is None:
        return False, "Did not return to menu"

    # Read result from memory
    result = read_bytes(transport, labels["output"], 32)
    return True, f"Got: {result.hex()}"
```

---

## Pattern 4: Parallel Test Execution

For running many tests across a pool of VICE instances. `ViceInstanceManager` handles all port allocation and PID tracking.

```python
from c64_test_harness import ViceInstanceManager, ViceConfig, Labels, write_bytes
from concurrent.futures import ThreadPoolExecutor, as_completed

config = ViceConfig(prg_path="build/program.prg", warp=True, ntsc=True, sound=False)

with ViceInstanceManager(config=config) as mgr:
    num_workers = 4
    instances = []
    for i in range(num_workers):
        inst = mgr.acquire()
        print(f"Worker {i}: VICE PID={inst.pid}, port={inst.port}")
        instances.append(inst)

    # Wait for all instances to boot
    for inst in instances:
        wait_for_text(inst.transport, "Q=QUIT", timeout=60.0, verbose=False)
        write_bytes(inst.transport, 0x0339, bytes([0x4C, 0x39, 0x03]))

    # Distribute work
    def worker(wid, transport):
        # ... run tests ...
        return wid, passed, failed

    with ThreadPoolExecutor(max_workers=num_workers) as pool:
        futures = {pool.submit(worker, i, inst.transport): i
                   for i, inst in enumerate(instances)}
        for fut in as_completed(futures):
            wid, passed, failed = fut.result()

    for inst in instances:
        mgr.release(inst)
```

---

## Pattern 5: Cross-Validation (Direct + UI)

Run the same test through both paths to catch bugs in either:

```python
def cross_validate(transport, labels, message):
    input_bytes = message.encode("ascii")

    # Direct path
    direct_result = sha256_direct(transport, labels, input_bytes)

    # After direct test, CPU is paused — restart program
    # Resume CPU, inject RUN command, wait for program to boot
    transport.resume()
    send_text(transport, "RUN")
    time.sleep(0.1)
    send_key(transport, "\r")
    wait_for_text(transport, "Q=QUIT", timeout=60.0, verbose=False)

    # UI path
    enter_text_and_hash(transport, message)
    ui_result = read_bytes(transport, labels["sha256_hash"], 32)

    # Python reference
    reference = hashlib.sha256(input_bytes).digest()

    assert direct_result == ui_result == reference
```

---

## Pattern 6: Process Block Isolation

Test a single processing step (e.g., one SHA-256 block) without the full pipeline:

```python
def test_process_block(transport, labels, block_data):
    # Write block data BEFORE init (init doesn't touch the block buffer)
    write_bytes(transport, labels["sha256_block"], block_data)

    # Initialize state
    jsr(transport, labels["sha256_init"], timeout=5.0)

    # Call process_block directly (skipping update/padding logic)
    jsr(transport, labels["sha256_process_block"], timeout=10.0)

    # Finalize (copy internal state to output)
    jsr(transport, labels["sha256_final"], timeout=5.0)

    return read_bytes(transport, labels["sha256_hash"], 32)
```

---

## Pattern 7: Test Runner Structure with `run_all_tests.py` Integration

To add a new test suite to the parallel runner:

1. Create `tools/test_<name>_direct.py` with a `run_tests(transport, labels, iterations, cross_validate)` function
2. Define a `worker_run(wid, transport, labels, batches)` if using custom distribution
3. In `run_all_tests.py`, add to `ALL_REQUIRED_LABELS` and the `suites` list
4. Use `_import_test_module("test_<name>_direct")` for lazy loading

---

## Pattern 8: Ethernet / CS8900a Bridge Networking

For testing C64 networking code with the CS8900a ethernet cartridge. Two VICE instances share a Linux bridge (`br-c64`) and talk to each other via L2 + IP + ICMP — no TAP-to-host NAT is involved.

```python
config = ViceConfig(
    prg_path="build/network_app.prg",
    warp=True,                      # OK — new orchestrators are wall-clock-driven
    ethernet=True,                  # Enable CS8900a
    ethernet_mode="rrnet",          # RR-Net — matches ip65 and the physical cart
    ethernet_interface="tap-c64-a", # Host TAP (per-instance)
    ethernet_driver="tuntap",
)
```

`ViceInstanceManager` auto-generates unique MACs (`02:c6:40:xx:xx:xx`) per instance and programs the CS8900a Individual Address registers after connect — no manual MAC handling needed.

### Use the `bridge_vice_pair` fixture for two-VICE tests

```python
# tests/conftest.py already provides this:
def bridge_vice_pair(...) -> tuple[ViceInstance, ViceInstance]:
    # Brings up two VICE instances on br-c64, RR-Net mode, warp=False (opt-in warp),
    # unique MACs, CS8900a initialised (RxCTL + LineCTL + clockport).
```

Host setup lives in `scripts/setup-bridge-tap.sh` / `teardown-bridge-tap.sh` / `cleanup-bridge-networking.sh`. Cleanup uses the port-range-scoped `scripts/cleanup_vice_ports.py` — NEVER `pkill x64sc`.

### RR-Net mode is required (not TFE)

The harness previously used TFE. RR-Net (`ethernet_mode="rrnet"`, emits `-ethernetcartmode 1`) is now mandatory because the register layout matches ip65's `cs8900a.s` and the physical RR-Net cartridge. TFE looked simpler but made TX-after-RX and full ICMP round-trip fail in ways that were misattributed to "VICE 3.10 limitations" — PR #44 discovered the real fix is RR-Net + clockport enable.

### RR-Net register map (base $DE00)

| Address | Register | Purpose |
|---------|----------|---------|
| `$DE00` | ISQ | Interrupt status queue |
| `$DE01` bit 0 | Clockport enable | **MUST be set before any CS8900a access** |
| `$DE02` | PPPtr | PacketPage Pointer |
| `$DE04` | PPData | PacketPage Data |
| `$DE08` | RTDATA | RX/TX data port |
| `$DE0C` | TxCMD | TX command |
| `$DE0E` | TxLen | TX frame length |

### Use the orchestrators — don't hand-roll TX/RX

The harness ships high-level helpers that own the wall-clock deadline in Python and call bounded 6502 peek bursts. These work in BOTH normal and warp mode:

```python
from c64_test_harness import (
    build_echo_request_frame, build_ping_and_wait_code,
    build_icmp_responder_code, run_ping_and_wait, run_icmp_responder,
)

# One side: send ICMP echo, poll for the reply
frame = build_echo_request_frame(
    src_mac=mac_a, dst_mac=mac_b, src_ip=ip_a, dst_ip=ip_b,
    identifier=0x1234, sequence=1, payload=b"ping",
)
result = run_ping_and_wait(
    transport_a, tx_frame=frame, rx_buf=0xC200,
    result_addr=0xC100, identifier=0x1234, sequence=1,
    tx_frame_buf=0xC400, timeout_s=5.0,
)
# result: 0x01 on reply match, 0xFF on wall-clock timeout

# Other side: reply to any echo request addressed to us
result = run_icmp_responder(
    transport_b, rx_buf=0xC200, my_ip=ip_b,
    result_addr=0xC100, timeout_s=5.0,
)
```

### Shippable C64 applications — use TOD-timed variants

The host-driven orchestrators above need a Python harness. For code that must run unsupervised on a real C64, U64 Elite (any turbo speed), or VICE normal — but NOT VICE warp — use the CIA1 TOD-based variants. They measure wall-clock via the 6526's TOD counter instead of iteration counts:

```python
from c64_test_harness import (
    build_ping_and_wait_tod_code, build_icmp_responder_tod_code,
    build_rx_echo_reply_tod_code, build_tod_start_code,
)
```

VICE warp accelerates TOD (it's virtual-CPU-clocked, not wall-clock-driven — see gotcha below). On real hardware and VICE normal, TOD is a valid timeout source. Deadline cap is 599 tenths (59.9 s).

### Critical networking gotchas

1. **Set `$DE01` bit 0 (clockport enable) before any CS8900a access.** Without it the chip silently drops every register read and write. All harness code builders and `set_cs8900a_mac()` do this automatically; only relevant if you hand-roll.

2. **Initialize RxCTL + LineCTL before TX/RX.** Write `RxCTL` (PP `$0104` = `$00D8`) and enable `SerTxON | SerRxON` in `LineCTL` (PP `$0112 |= $00C0`). Without this the chip silently discards TX frames. `bridge_vice_pair` fixture does this; standalone setups must call it.

3. **VICE flag ordering.** `-ethernetioif` / `-ethernetiodriver` MUST come before `-ethernetcart` (VICE probes the interface on the cart flag; rejects if inaccessible). `ViceConfig` handles this.

4. **Combined TX+RX in one 6502 routine.** The binary monitor pauses the CPU between commands. VICE's CS8900a only processes incoming TAP frames while the CPU is running, so splitting TX and RX across two `jsr()` calls misses the reply during the pause. The `run_ping_and_wait` orchestrator works around this by driving the wall-clock from Python and running bounded `build_rx_peek_code` bursts.

5. **SEI/CLI around polling loops.** KERNAL IRQ fires ~60×/sec and corrupts zero-page timeout counters ($FD/$FE).

6. **Result addresses must not overlap code.** Combined routines can reach ~185 bytes; place result/meta buffers well above.

7. **Filter RX frames by EtherType.** Host kernel sends IPv6 MLDv2 multicast on TAP — drain non-matching frames before resuming.

8. **Use the `Asm` helper for 6502 branch offsets.** Hand-coded displacements are a major bug source.

9. **Multi-instance MAC uniqueness.** VICE has no CLI flag for CS8900a MACs. `ViceInstanceManager` auto-generates unique ones and calls `set_cs8900a_mac()` after connect. Standalone `ViceProcess` setups must call it manually.

**End-to-end validation:** `scripts/bridge_ping_demo.py [--warp]` runs a visible two-VICE ping with on-screen counters. Validated 10/10 in both normal and warp modes.

---

## Pattern 9: Backend-Agnostic Testing (UnifiedManager)

Write tests that work on both VICE and Ultimate 64 without code changes:

```python
from c64_test_harness import create_manager, read_bytes, write_bytes

# C64_BACKEND env var selects "vice" (default) or "u64"
# U64_HOST env var provides device address(es)
with create_manager() as mgr:
    with mgr.instance() as target:
        transport = target.transport  # Works with both backends

        # Memory I/O works identically on both
        write_bytes(transport, 0xC100, bytes([0xDE, 0xAD]))
        result = read_bytes(transport, 0xC100, 2)
        assert result == bytes([0xDE, 0xAD])

        # Screen/keyboard works identically
        codes = transport.read_screen_codes()
        assert len(codes) == 1000

        # VICE-only features gated by backend check
        if target.backend == "vice":
            from c64_test_harness import jsr
            regs = jsr(transport, 0xC000)
```

### Cross-process safety
`UnifiedManager` wraps U64 access with `DeviceLock` automatically. Multiple agents (separate OS processes) can call `create_manager()` simultaneously — the file lock queues them for the same physical device.

---

## Pattern 10: Ultimate 64 Hardware Testing

The U64 has NO CPU control (no `jsr()`, no registers, no breakpoints). To execute code on hardware, use the **DMA trampoline + main_loop hijack** pattern.

**All U64 access must use DeviceLock** for cross-process safety. Use `UnifiedManager` (automatic) or wrap with `DeviceLock` in pytest fixtures.

### DMA Trampoline Pattern
```python
from c64_test_harness.backends.device_lock import DeviceLock
from c64_test_harness.backends.ultimate64 import Ultimate64Transport
from c64_test_harness.backends.ultimate64_client import Ultimate64Client
from c64_test_harness.backends.ultimate64_helpers import set_turbo_mhz, set_reu, reboot
from c64_test_harness.memory import write_bytes, read_bytes

SENTINEL = 0x0350      # Scratch byte for completion signaling
TRAMPOLINE = 0x0360    # Scratch area for injected code
MAIN_LOOP = 0x082A     # Program's parking JMP (from labels)
TARGET_SUB = 0x1509    # Subroutine to call (from labels)

# Build trampoline: JSR target; LDA #$42; STA sentinel; JMP * (park)
trampoline = bytes([
    0x20, TARGET_SUB & 0xFF, (TARGET_SUB >> 8) & 0xFF,  # JSR target
    0xA9, 0x42,                                           # LDA #$42
    0x8D, SENTINEL & 0xFF, (SENTINEL >> 8) & 0xFF,       # STA sentinel
    0x4C, (TRAMPOLINE + 8) & 0xFF, ((TRAMPOLINE + 8) >> 8) & 0xFF,  # JMP *
])

# Write trampoline + zero sentinel via DMA
write_bytes(transport, TRAMPOLINE, trampoline)
write_bytes(transport, SENTINEL, bytes([0x00]))

# Hijack the program's main_loop with JMP to our trampoline
write_bytes(transport, MAIN_LOOP, bytes([0x4C, TRAMPOLINE & 0xFF, (TRAMPOLINE >> 8) & 0xFF]))

# Poll sentinel for completion
import time
deadline = time.monotonic() + 30.0
while time.monotonic() < deadline:
    if transport.read_memory(SENTINEL, 1)[0] == 0x42:
        break
    time.sleep(0.1)
else:
    raise TimeoutError("subroutine did not complete")

# Read results from memory
result = read_bytes(transport, RESULT_ADDR, 32)
```

### U64 Turbo Speed Changes (reboot required)
When switching turbo speeds between REU-heavy workloads, use `reboot()` not `reset()`:
```python
# WRONG: soft reset leaves stale REU DMA state → hangs at some speeds
client.reset()
set_turbo_mhz(client, 32)
client.run_prg(prg_data)  # may hang!

# RIGHT: full reboot clears all FPGA/DMA state
client.reboot()
time.sleep(8.0)  # FPGA reinit takes ~8s
set_reu(client, enabled=True, size="512 KB")  # re-enable after reboot
set_turbo_mhz(client, 32)
client.run_prg(prg_data)  # works reliably
```

### Verify Program Startup via Code Bytes (not screen text)
After `run_prg()`, screen RAM ($0400) may contain stale text from a prior run. Always verify startup by polling for known code bytes:
```python
# WRONG: stale screen text gives false positive
grid = wait_for_text(transport, "Q=QUIT", timeout=60.0)

# RIGHT: poll for known code at a program address
boot_deadline = time.monotonic() + 60.0
while time.monotonic() < boot_deadline:
    ml = transport.read_memory(MAIN_LOOP, 3)
    if ml == bytes([0x4C, 0x2A, 0x08]):  # expected JMP $082A
        break
    time.sleep(0.5)
```

---

## Pattern 11: UCI Networking on Ultimate 64 Elite

U64E cannot emulate CS8900a/RR-Net, but firmware exposes socket-level TCP/UDP via the **Ultimate Command Interface** at `$DF1C-$DF1F`. `c64_test_harness.uci_network` provides 6502 code builders + Python helpers.

```python
from c64_test_harness import (
    uci_probe, uci_get_ip, uci_tcp_connect,
    uci_socket_write, uci_socket_read, uci_socket_close,
)

# Enable UCI (must be on in firmware config — see gotcha below)
assert uci_probe(transport) == 0xC9   # UCI_IDENTIFIER

ip = uci_get_ip(transport)
print(f"U64 IP: {ip}")

sock = uci_tcp_connect(transport, host="192.168.1.10", port=80)
uci_socket_write(transport, sock, b"GET / HTTP/1.0\r\n\r\n")
data = uci_socket_read(transport, sock, max_bytes=1024)
uci_socket_close(transport, sock)
```

### Turbo-safe fence — use at U64 speeds ≥ 4 MHz

On real U64E, the FPGA behind `$DF1C-$DF1F` needs ~38 µs wall-clock between consecutive register accesses. At 1 MHz the bus cycle is slow enough; at 4/8/24/48 MHz the CPU outruns the FPGA → double-latched writes, stale reads, UCI protocol corruption. Every builder and helper accepts an opt-in `turbo_safe: bool = False`:

```python
# At U64 turbo speeds, pass turbo_safe=True to every UCI call
uci_probe(transport, turbo_safe=True)
uci_get_ip(transport, turbo_safe=True)
sock = uci_tcp_connect(transport, "host", 80, turbo_safe=True)
```

This emits a nested delay-loop fence (~52 µs at 48 MHz, ~2.5 ms at 1 MHz, 16 bytes per site), adds a 255-iteration settle after every `PUSH_CMD`, and converts short branches over fence expansions to `JMP` trampolines. Fence tuning is exposed as public constants: `UCI_FENCE_OUTER=5`, `UCI_FENCE_INNER=100`, `UCI_PUSH_SETTLE_ITERS=0xFF` — minimum is OUTER=3/INNER=122 (~38.4 µs), defaults provide 35% margin. Default is `turbo_safe=False` for backward compat.

**Scripts `scripts/probe_uci_network.py` and `scripts/test_uci_tcp_echo.py` are NOT turbo-safe** (they hand-write 6502 outside the builders) — they only run at stock 1 MHz.

### UCI gotchas

1. **UCI must be enabled in U64 config.** "C64 and Cartridge Settings" → "Command Interface" → "Enabled" (save to flash). Use `get_uci_enabled()` / `enable_uci()` / `disable_uci()` for transient activation in tests.
2. **UCI state survives soft reset.** Always send `CMD_ABORT` (`$04` to `$DF1C`) before code injection if you're unsure of the device state.
3. **`$DF1F` status reads require per-byte `CMD_NEXT_DATA` acknowledgment.** The harness helpers handle this.
4. **`GET_IP_ADDRESS` needs an interface index byte (0x00) as parameter.** Helper does this.
5. **Routine dispatch uses SYS + keyboard buffer.** `_execute_uci_routine` writes the routine, types `SYS <addr>`, and presses RETURN — it does NOT patch `IMAIN`. Don't assume IMAIN is touched.
6. **UCI builders emit `turbo_safe` delays that widen short branches.** If you copy builder output into your own routine, preserve the `JMP` trampolines over fence expansions.

### Reference

- Docs: `docs/uci_networking.md`
- Live tests: `tests/test_uci_turbo_live.py` (gated by `U64_HOST` + `U64_ALLOW_MUTATE`)
- The same fence approach is mirrored in c64-https (`src/net/uci/uci_regs.inc` — `uci_fence` macro with identical tuning).

---

## Common Gotchas

### 1. Never Use ViceProcess or PortAllocator Directly
**Wrong:**
```python
allocator = PortAllocator(port_range_start=6511, port_range_end=6530)
port = allocator.allocate()
config = ViceConfig(prg_path=PRG_PATH, port=port, ...)
with ViceProcess(config) as vice:
    transport = BinaryViceTransport(port=port)
```
**Right:**
```python
config = ViceConfig(prg_path=PRG_PATH, warp=True, ntsc=True, sound=False)
with ViceInstanceManager(config=config) as mgr:
    inst = mgr.acquire()
    transport = inst.transport  # port, PID, transport all managed
```
**Why:** Parallel Claude agents using `ViceProcess` directly will collide on ports (default 6502) and kill each other's VICE processes. `ViceInstanceManager` uses OS-level `bind()` + file-based `flock()` locks for cross-process-safe port reservation, verifies PID ownership after startup, and retries with backoff on failure.

### 2. PETSCII Filename Case
c1541 stores uppercase ASCII as shifted PETSCII ($C1-$DA), but the C64 keyboard generates unshifted ($41-$5A). Always use **lowercase** `c64_name`:
```python
# WRONG: C64 can't find "MYFILE" from keyboard
img.write_file("myfile.seq", data, c64_name="MYFILE")

# RIGHT: lowercase matches keyboard input
img.write_file("myfile.seq", data, c64_name="myfile")
```

### 3. Program State After jsr()
After `jsr()` returns, the CPU is paused at the NOP after the trampoline's JSR. The breakpoint is deleted, but the CPU remains paused (binary transport keeps the connection open). To return to the running program:
```python
transport.resume()  # Resume CPU — it will hit the second NOP and fall through
send_text(transport, "RUN")
time.sleep(0.1)
send_key(transport, "\r")
wait_for_text(transport, "Q=QUIT", timeout=60.0, verbose=False)
```

### 4. Large Memory Operations
`read_bytes()` and `write_bytes()` work for any size — binary transport has no limits (4096+ verified):
```python
write_bytes(transport, addr, large_data)  # Any size works
data = read_bytes(transport, addr, 512)   # Any size works
```

### 5. SEQ File Secondary Address
For reading sequential files on the C64, SETLFS secondary address must be >= 2:
```python
# SA=0 is LOAD mode, SA=1 is SAVE mode, SA>=2 is data channel
# In 6502: LDA #$02; LDX #$08; LDY #$02; JSR SETLFS
```

### 6. wait_for_text() Verbose Flag
Default `verbose=True` dumps the entire screen on every poll. Use `verbose=False` unless actively debugging:
```python
# WRONG: floods output with screen dumps
grid = wait_for_text(transport, "READY", timeout=30.0)

# RIGHT: quiet polling
grid = wait_for_text(transport, "READY", timeout=30.0, verbose=False)
```

### 7. Timing Between Keyboard Operations
The C64 keyboard buffer is only 10 characters. For longer text, `send_text()` auto-chunks, but add small delays between operations:
```python
send_text(transport, "HELLO")
time.sleep(0.1)  # Let buffer drain
send_key(transport, "\r")
```

### 8. Window Focus Stealing
VICE windows steal keyboard focus when launched, disrupting the user's work. `ViceConfig.minimize` defaults to `True`, passing `-minimized` to VICE. Do **not** set `minimize=False` unless the user explicitly needs visible windows:
```python
# Default — windows start minimized (correct for automated testing)
config = ViceConfig(prg_path="build/prog.prg", warp=True, sound=False)

# Only if user needs to see the VICE window
config = ViceConfig(prg_path="build/prog.prg", minimize=False)
```

### 9. Multi-Agent VICE Process Safety
When multiple agents run VICE in parallel, never `pkill x64sc` — it kills other agents' instances. Use the PID from `ViceInstance` to manage only your own processes:
```python
inst = mgr.acquire()
my_pid = inst.pid  # Track this PID

# Later, if you need to force-kill YOUR instance only:
import os, signal
if my_pid is not None:
    os.kill(my_pid, signal.SIGTERM)
```

After `run_parallel()`, each `SingleTestResult` has a `.pid` field identifying which VICE process ran it.

**Port allocation is cross-process safe.** `ViceInstanceManager` uses `PortAllocator` internally with dual-layer protection: OS-level `bind()` reservations and file-based `flock()` locks. The file lock bridges the TOCTOU gap between closing the reservation socket and VICE binding to the port, so overlapping startup from independent processes is completely safe — no stagger delay needed. After VICE starts, PID ownership is verified via `/proc/net/tcp` to ensure the correct VICE process is listening. Failed acquisitions retry with exponential backoff (configurable via `max_retries`, default 3).

**VICE startup crashes:** When 5+ VICE instances launch simultaneously, ~2 of 5 may crash with rc=1 due to X11/GTK resource contention. The manager detects this within ~1s and retries automatically. No special handling needed by callers.

**Validated:** 3 concurrent agents x 6 workers x 5 phases (lock-only, vice, mixed, crash, exhaustion), zero failures. See `scripts/stress_cross_process.py`.

### 10. jsr() Uses Events, Not Polling
`jsr()` uses binary monitor checkpoints and `wait_for_stopped()` — no polling overhead. It works reliably for both short and long-running computations, even in warp mode:
```python
# Event-based — works for any duration
regs = jsr(transport, labels["fast_fn"], timeout=5.0)
regs = jsr(transport, labels["slow_fn"], timeout=300.0)
```

### 11. Build Verification
Always build fresh and verify the PRG exists before testing:
```python
subprocess.run(["make", "clean"], capture_output=True, cwd=PROJECT_ROOT)
result = subprocess.run(["make"], capture_output=True, text=True, cwd=PROJECT_ROOT)
if result.returncode != 0:
    print(f"Build failed:\n{result.stderr}")
    sys.exit(1)
assert os.path.exists(PRG_PATH), f"{PRG_PATH} not found after build"
```

### 12. Label Verification
Check all required labels before starting tests:
```python
labels = Labels.from_file(LABELS_PATH)
required = ["sha256_init", "sha256_hash", ...]
for name in required:
    if labels.address(name) is None:
        print(f"FATAL: '{name}' label not found")
        sys.exit(1)
```

### 13. U64: Always Use DeviceLock for Cross-Process Safety
**Wrong:**
```python
# NO LOCK — another agent's tests will corrupt yours
transport = Ultimate64Transport(host="192.168.1.81")
transport.write_memory(0xC000, b"\xDE\xAD")
```
**Right (pytest fixture):**
```python
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
**Right (standalone script):**
```python
with create_manager(backend="u64", u64_hosts=["192.168.1.81"]) as mgr:
    with mgr.instance() as target:
        target.transport.write_memory(0xC000, b"\xDE\xAD")
```
**Why:** Without `DeviceLock`, multiple OS processes can send REST API commands to the same U64 simultaneously. One agent writes memory, another reads back garbage from the first agent's write. `DeviceLock` uses `fcntl.flock()` to serialize access across processes.

### 14. U64: Probe Before Connecting
```python
from c64_test_harness import probe_u64, is_u64_reachable

# Quick check
if not is_u64_reachable("192.168.1.81"):
    print("Device offline — skipping")

# Detailed probe (ping -> TCP -> API, fail-fast)
result = probe_u64("192.168.1.81")
if not result.reachable:
    print(f"Probe failed: {result.error}")
```
`Ultimate64InstanceManager.acquire()` probes automatically and skips unreachable devices.

### 15. U64: reset() vs reboot()
`reset()` is a soft C64 reset (6510 CPU only). `reboot()` reinitializes the entire FPGA including DMA controllers and REU. **Use `reboot()` when switching turbo speeds with REU-heavy workloads** — stale DMA state causes hangs. Allow ~8s for the device to become responsive after reboot.

### 16. U64: Stale Screen RAM After Reset
`run_prg()` does a soft reset internally, but screen memory at $0400 persists. Never use `wait_for_text()` alone to detect program startup — poll for known code bytes (e.g. main_loop JMP) instead. See Pattern 10.

### 17. U64: Runner Endpoints Use POST
All U64 runner endpoints (`run_prg`, `load_prg`, `run_crt`, `sidplay`, `modplay`) require HTTP POST. PUT returns HTTP 400 on firmware 3.14.

### 18. U64: REU Must Be Enabled for REU Programs
Programs that use the REU (e.g. x25519) need `set_reu(client, True, size="512 KB")` before loading. REU config may reset after `reboot()` — re-enable it after each reboot.

### 19. VICE Binary Monitor Must Not Use Port 6510
Port 6510 is VICE's default TEXT monitor port. VICE misbehaves when the BINARY monitor is bound there. `PortAllocator` starts at 6511 (range 6511-6531) and `ViceConfig.port` defaults to 6502 for this reason. Don't override to 6510.

### 20. VICE 3.10 WarpMode is Not a Resource
`resource_get("WarpMode")` returns error 0x1 — WarpMode is a static C variable in vsync.c, not in the resource system. Runtime warp toggle requires the text remote monitor (`warp on` / `warp off`). Same for `Speed=0`. The `ViceInstanceManager(enable_text_monitor=True)` flag allocates both ports automatically.

### 21. VICE 3.10 Ethernet Needs BOTH -addconfig AND -ethernetioif
CS8900a activation needs both `-addconfig <rc>` (with `ETHERNETCART_ACTIVE=1`) AND `-ethernetioif` / `-ethernetiodriver` on the CLI, with `-addconfig` FIRST. `ViceConfig` handles this when `ethernet=True`.

### 22. TOD Zero-Page Footprint is $F0-$F5 (Don't Collide)
The `tod_timer.py` helpers and every `bridge_ping.build_*_tod_code` routine claim:
- `$F0/$F1` — current elapsed tenths (LE16)
- `$F2/$F3` — deadline (LE16)
- `$F4` — BCD ones scratch
- `$F5` — BCD raw scratch

`bridge_ping._emit_read_frame` internally reuses `$F1-$F4` as temps — safe only because TOD loops fully release those slots before frame reads run. **Do NOT interleave TOD reads with frame reads inside one routine.**

### 23. VICE TOD is Virtual-CPU-Clocked — Warp Accelerates It
CIA TOD behavior differs by platform:
- **Real U64 Elite:** TOD is true wall-clock (verified: 5.0s wall → 5.1s TOD delta). Decoupled from CPU.
- **VICE 3.10:** TOD is virtual-CPU-clocked. Verified: warp mode 3.0s wall = 94.1s TOD (~31× acceleration).

**Consequence:** any 6502-side timeout mechanism in VICE is warp-accelerated regardless of source (iteration counter, CIA timer A/B, TOD, jiffy, raster). Code that must work in VICE warp AND on real U64 needs host-side wall-clock timeouts with bounded 6502 peek bursts — which is why `run_ping_and_wait` / `poll_until_ready` exist. On real U64 only, TOD is a valid pure-6502 timeout source (see `build_*_tod_code`).

### 24. U64 SID Source Isolation Uses Address Routing, Not Socket Type Switching
Route unwanted SID engines to Unmapped addresses (e.g. `$D500`) so they don't contribute audio. "SID Socket" config is just Enabled/Disabled; chip type is firmware auto-detected (read-only). **Disable "SID Player Autoconfig" and "Auto Address Mirroring"** before mapping multiple SIDs to adjacent addresses. SID address routing changes require device reset + ~3s settle for FPGA fabric reconfiguration. **Digital audio stream does NOT reflect Audio Mixer panning** (analog-only) — use frequency analysis (Goertzel DFT) not stereo separation for multi-SID validation.

### 25. Labels is a Mapping — Use It as a Dict
As of v0.12.4, `Labels` inherits from `collections.abc.Mapping[str, int]`. `dict(Labels.from_file(path))` works; `for name, addr in labels.items(): ...` iterates all entries. `.get()`, `__eq__`, and `__ne__` are inherited for free.

---

## Memory Map Conventions

Common C64 addresses used in testing:

| Address | Purpose |
|---------|---------|
| `$0334` | Cassette buffer — safe for jsr() trampoline after BASIC boot |
| `$0339` | JMP-self safety loop target (write `4C 39 03` here) |
| `$0400` | Screen RAM (40x25 = 1000 bytes) |
| `$0277` | Keyboard buffer (10 bytes) |
| `$00C6` | Keyboard buffer count |
| `$D020` | Border color |
| `$D021` | Background color |
