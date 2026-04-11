# Bridge networking for two-VICE tests

This document describes how to set up and use the **two-VICE Linux
bridge** pattern for tests that need to exchange ethernet frames
between two C64 emulator instances.

## Overview

The pattern uses:

* `br-c64` -- a Linux network bridge
* `tap-c64-0` and `tap-c64-1` -- two TAP interfaces attached to the
  bridge, one per VICE instance
* Two `x64sc` processes, each launched with RR-Net-mode CS8900a
  ethernet bound to its TAP interface

This setup gives two VICE instances a shared layer-2 segment.  The
host can also participate (the bridge's IP is `10.0.65.1`), so
captures via `tcpdump -i br-c64` will show all traffic between the
instances.

## Prerequisites

* `x64sc` (VICE 3.10) compiled with `tuntap` driver support
* Root privileges to create TAP devices and configure the bridge
  (only required for setup/teardown -- VICE itself runs unprivileged)
* `ip` (iproute2) and `iptables`
* The c64-test-harness package (`c64_test_harness.bridge_ping`)

## Setting up the bridge

```bash
sudo /home/someone/c64-test-harness/scripts/setup-bridge-tap.sh
```

This creates:
- `br-c64` bridge with IP `10.0.65.1/24`
- `tap-c64-0` and `tap-c64-1` TAP interfaces, both attached to the bridge
- iptables FORWARD rules permitting traffic on the bridge

To tear down:

```bash
sudo /home/someone/c64-test-harness/scripts/teardown-bridge-tap.sh
```

If something goes wrong, an emergency cleanup is available:

```bash
sudo /home/someone/c64-test-harness/scripts/cleanup-bridge-networking.sh
```

## Launching two VICE instances on the bridge

The simplest way is to use the `bridge_vice_pair` pytest fixture
defined in `tests/conftest.py`:

```python
def test_my_bridge_thing(bridge_vice_pair):
    transport_a, transport_b = bridge_vice_pair
    # both VICE instances are at READY, CS8900a initialised, MACs set
```

The fixture handles port allocation, VICE process lifecycle, BASIC
READY synchronization, CS8900a initialization (RxCTL + LineCTL), and
unique MAC programming.

To launch manually:

```python
from c64_test_harness.backends.vice_lifecycle import ViceConfig, ViceProcess
from c64_test_harness.backends.vice_manager import PortAllocator
from c64_test_harness.bridge_ping import (
    cs8900a_rxctl_code, cs8900a_read_linectl_code, cs8900a_write_linectl_code,
)
from c64_test_harness.ethernet import set_cs8900a_mac
from c64_test_harness.execute import jsr, load_code
from c64_test_harness.memory import read_bytes
from tests.conftest import connect_binary_transport

# Allocate two binary monitor ports
allocator = PortAllocator(port_range_start=6560, port_range_end=6580)
port_a = allocator.allocate()
port_b = allocator.allocate()

# Configure both VICE instances with RR-Net ethernet on different TAPs.
# Keep warp=False: ip65's DHCP flow has been observed to misbehave in
# warp mode, and normal speed is fast enough for ethernet tests.
config_a = ViceConfig(
    port=port_a, warp=False, sound=False,
    ethernet=True, ethernet_mode="rrnet",
    ethernet_interface="tap-c64-0",
    ethernet_driver="tuntap",
)
config_b = ViceConfig(
    port=port_b, warp=False, sound=False,
    ethernet=True, ethernet_mode="rrnet",
    ethernet_interface="tap-c64-1",
    ethernet_driver="tuntap",
)

vice_a = ViceProcess(config_a)
vice_b = ViceProcess(config_b)
vice_a.start()
vice_b.start()
transport_a = connect_binary_transport(port_a, proc=vice_a)
transport_b = connect_binary_transport(port_b, proc=vice_b)

# Wait for BASIC READY (omitted: see _bridge_wait_ready in tests/conftest.py)

# Initialise CS8900a on each instance: RxCTL = 0x00D8, LineCTL |= 0x00C0
# (see _bridge_init_cs8900a in tests/conftest.py for the exact sequence)

# Program unique MAC addresses
set_cs8900a_mac(transport_a, bytes.fromhex("02C640000001"))
set_cs8900a_mac(transport_b, bytes.fromhex("02C640000002"))

# ... use the transports ...

vice_a.stop()
vice_b.stop()
allocator.release(port_a)
allocator.release(port_b)
```

## MAC address assignment

Each VICE instance gets a unique MAC programmed at runtime via the
CS8900a Individual Address (IA) registers (`set_cs8900a_mac`).  The
locally-administered prefix `02:c6:40:00:00:xx` is used by convention.

VICE 3.10 has no command-line flag for setting the CS8900a MAC; the
chip starts with a default MAC and you MUST program the IA registers
through the binary monitor before exchanging frames.

## CS8900a register layout (RR-Net mode)

When VICE is launched with `ethernet_mode="rrnet"`, the CS8900a is
mapped at base `$DE00` with the RR-Net register layout that matches
the physical RR-Net cartridge and the ip65 `cs8900a.s` driver:

| Address       | Register   | Purpose                                       |
|---------------|------------|-----------------------------------------------|
| `$DE00/$DE01` | ISQ        | Interrupt status queue; **bit 0 of `$DE01` = RR clockport enable** |
| `$DE02/$DE03` | PPPtr      | PacketPage pointer                            |
| `$DE04/$DE05` | PPData     | PacketPage data                               |
| `$DE08/$DE09` | RTDATA     | RX/TX data FIFO                               |
| `$DE0C/$DE0D` | TxCMD      | TX command register                           |
| `$DE0E/$DE0F` | TxLength   | TX frame length                               |

**Critical:** before any other CS8900a access, you must set the RR
clockport enable bit (read `$DE01`, OR with `$01`, write back).
Without this, the chip silently drops every register read and write,
and the failure mode looks like "TX frames never reach the wire" or
"PPPtr/PPData don't return sensible values".  All code builders in
`c64_test_harness.bridge_ping` prepend this snippet automatically;
`set_cs8900a_mac()` in `c64_test_harness.ethernet` also does a
read-modify-write on `$DE01` before the first PP access.

Programming model:

* **TX**: write `TxCMD = 0x00C0`, `TxLength = N`, then poll BusST
  (PP `0x0138` bit 8) for `Rdy4TxNOW`, then write N bytes to RTDATA.
* **RX**: poll RxEvent (PP `0x0124` bit 8) for `RxOK`, then read 2
  bytes RxStatus + 2 bytes RxLength + N bytes frame data from RTDATA.
* **MAC**: write 3 words to IA registers (PP `0x0158` -- `0x015D`).
* **RxCTL** (PP `0x0104`): set to `0x00D8` to accept broadcast and
  IA-matching unicast frames.  See `cs8900a_rxctl_code()` in
  `c64_test_harness.bridge_ping`.
* **LineCTL** (PP `0x0112`): set bits 6 and 7 (`SerRxON` and
  `SerTxON`) to enable RX and TX.

## Capture-only sample (host tcpdump)

Once the bridge is up and two VICE instances are running on it, you
can observe all traffic on the host:

```bash
sudo tcpdump -nne -i br-c64
```

This is useful for debugging your test cases and for verifying that
frames you expect to be sent are actually leaving the chip.

## Timeouts: host-side wall clock

Bridge networking polling loops use a **host-side wall-clock pattern**
(see `src/c64_test_harness/poll_until.py`).  The 6502 side runs only a
small bounded "peek batch" routine -- a fixed number of CS8900a RxEvent
poll iterations -- and immediately RTSes whether or not a frame arrived.
Python owns the wall-clock deadline via `time.monotonic` and decides
whether to call the peek again.

Why not let the 6502 own the timeout?  Earlier versions used a
3-level inner counter (`DEC $F0/$F1/$F2`) to bound the poll to "about
5 seconds".  That budget is denominated in 6502 cycles, so it
evaporates in microseconds under VICE warp mode -- the C64 gives up
before any TAP frame can arrive.  CIA TOD ($DC08-$DC0B) is also not
usable as a wall-clock substitute: in our VICE 3.10 + `sound=False`
configuration both CIA1 and CIA2 TOD registers stay pinned at
`01:00:00.00` and never advance regardless of warp.

The host-side pattern works in **both** normal and warp modes (verified
10/10 each via `scripts/bridge_ping_demo.py [--warp]`) and is the same
orchestration shape that will drive future Ultimate 64 Elite UCI
networking tests -- a UCI peek routine would poll the socket-status
register at `$DF1C-$DF1F` instead of CS8900a RxEvent and
`poll_until_ready` would drive it identically.

### High-level entry points

* `bridge_ping.run_ping_and_wait(transport, ...)` -- transmit an echo
  request and poll for a matching reply.  Owns the wall-clock budget
  and re-polls on mismatched frames (e.g. host IPv6 multicast).
* `bridge_ping.run_icmp_responder(transport, ...)` -- wait for an
  echo request addressed to ``my_ip``, swap IPs/MACs, patch the ICMP
  checksum, and TX the reply -- all inside a single JSR after the
  Python-side poll reports a frame is waiting.
* `poll_until.poll_until_ready(transport, code_addr, result_addr, ...)` --
  the underlying generic primitive.  Backend-agnostic; any peek
  routine that follows the contract in its docstring works.

### Lower-level building blocks

* `bridge_ping.build_rx_peek_code(load_addr, result_addr, *, batch_size=500)`
  -- bounded CS8900a RxEvent peek (returns 0x01 ready / 0xFF batch
  exhausted).  Uses ZP `$F0/$F1` only (`$F2` is freed).
* `bridge_ping.build_read_and_match_echo_reply_code(...)` -- one-shot
  drain + ICMP echo-reply matcher (returns 0x01 match / 0x02 mismatch).
* `bridge_ping.build_read_and_respond_echo_request_code(...)` --
  one-shot drain + transform + TX reply (returns 0x01 done / 0x02 mismatch).

The legacy `build_icmp_responder_code` / `build_ping_and_wait_code` /
`build_rx_echo_reply_code` builders are retained for back-compat but
contain the broken cycle-budget poll loop and should not be used in
new code.

## Known limitations

### Warp mode and ip65 DHCP

This caveat applies only to **ip65-driven** ethernet tests (DHCP, full
TCP/IP).  ip65's DHCP state machine has been observed to misbehave in
warp mode independently of the poll-budget issue described above.  The
plain bridge ping tests in this directory work fine in warp mode --
the demo opts in via `--warp`.

### Frame minimum size

The CS8900a expects ethernet frames to be at least 60 bytes (minimum
data, before the 4-byte FCS that the chip auto-appends).  Smaller
payloads must be padded.  The `build_echo_request_frame` helper in
`c64_test_harness.bridge_ping` does this automatically.

## See also

* `tests/test_ethernet_bridge.py` -- raw L2 broadcast frame exchange
  (works fully end-to-end, both directions)
* `tests/test_bridge_ping.py` -- IP-layer ICMP exchange via the bridge
* `src/c64_test_harness/bridge_ping.py` -- helpers for building
  ICMP echo frames and 6502 RX/TX routines for RR-Net mode
  (register offsets match ip65's `cs8900a.s`)
* `scripts/setup-bridge-tap.sh` and `scripts/teardown-bridge-tap.sh`
* `tests/test_bridge_ping.py::TestBridgeIcmpRoundTrip` -- full
  round-trip test where B's 6502 responder swaps IPs/MACs and TXes
  an ICMP echo reply in the same JSR that consumed the request
* `scripts/bridge_ping_demo.py` -- visible two-VICE demo: launches
  both instances side by side (not minimized) and runs the ICMP
  round-trip in a loop with live per-screen status (ping counter +
  latest result, green/red). Run with
  `PYTHONPATH=src python3 scripts/bridge_ping_demo.py` (Ctrl+C to
  stop, or `--count N` to limit iterations).  Add `--warp` to verify
  the host-side wall-clock pattern under VICE warp mode.
