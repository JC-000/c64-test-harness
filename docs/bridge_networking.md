# Bridge networking for two-VICE tests

This document describes how to set up and use the **two-VICE Linux
bridge** pattern for tests that need to exchange ethernet frames
between two C64 emulator instances.

## Overview

The pattern uses:

* `br-c64` -- a Linux network bridge
* `tap-c64-0` and `tap-c64-1` -- two TAP interfaces attached to the
  bridge, one per VICE instance
* Two `x64sc` processes, each launched with TFE-mode CS8900a ethernet
  bound to its TAP interface

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

# Configure both VICE instances with TFE ethernet on different TAPs
config_a = ViceConfig(
    port=port_a, warp=False, sound=False,
    ethernet=True, ethernet_mode="tfe",
    ethernet_interface="tap-c64-0",
    ethernet_driver="tuntap",
)
config_b = ViceConfig(
    port=port_b, warp=False, sound=False,
    ethernet=True, ethernet_mode="tfe",
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

## CS8900a register layout (TFE mode)

When VICE is launched with `ethernet_mode="tfe"`, the CS8900a is
mapped at base `$DE00` with the standard layout:

| Address       | Register   | Purpose                       |
|---------------|------------|-------------------------------|
| `$DE00/$DE01` | RTDATA     | Receive/transmit data FIFO    |
| `$DE04/$DE05` | TxCMD      | TX command register           |
| `$DE06/$DE07` | TxLength   | TX frame length               |
| `$DE0A/$DE0B` | PPPtr      | PacketPage pointer            |
| `$DE0C/$DE0D` | PPData     | PacketPage data               |

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

## Known limitations

### RR-Net mode is broken in VICE 3.10

VICE 3.10's `EthernetCartMode=1` (RR-Net) emulates a register layout
that does NOT match the standard CS8900a packet-page addressing.  In
particular, the `PPPtr`/`PPData` register pair does not function for
arbitrary PP register access.  Always use `ethernet_mode="tfe"` for
test code that touches packet-page registers.

This also means the **ip65 cs8900a driver cannot be used with VICE
3.10**.  ip65's driver is hard-coded for the original RR-Net register
shift (`rxtxreg := $DE08`), but VICE 3.10's RR-Net emulation uses a
different shift entirely.  The
`c64_test_harness.bridge_ping` module provides a minimal ICMP-aware
ethernet helper layer written against the standard TFE register
layout instead.

### TX-after-RX in the same JSR can be silently dropped

A 6502 routine that consumes an RX frame from the CS8900a and then
immediately attempts to TX a reply (in the same `jsr()` call) has
been observed to *not* emit the TX frame onto the wire under VICE
3.10's TFE emulation, even though the routine appears to complete
the standard TX command sequence successfully (`TxCMD`, `TxLen`,
`Rdy4TxNOW` poll, write N bytes).  Standalone TX from a fresh VICE
instance works correctly.  The exact cause is unknown -- possibly
the chip's TX queue is held off by leftover RX state that
SkipPacket (RxCFG bit 6) does not fully release in the emulation.

For tests that need a true ICMP round-trip between two VICE
instances, the workaround is to TX from one instance and verify the
**received frame contents** on the other instance via memory read,
rather than relying on a C64-side responder.  See
`tests/test_bridge_ping.py::TestBridgeIcmp::test_icmp_echo_request_received`
for an example.

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
  ICMP echo frames and 6502 RX/TX routines targetting TFE mode
* `scripts/setup-bridge-tap.sh` and `scripts/teardown-bridge-tap.sh`
* `scripts/verify_rrnet_registers.py` -- empirical CS8900a register
  probe used to confirm the TFE/RR-Net layout differences in VICE
