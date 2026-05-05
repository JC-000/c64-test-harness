# Bridge networking for two-VICE tests

This document describes how to set up and use the **two-VICE bridge**
pattern for tests that need to exchange ethernet frames between two C64
emulator instances. Both Linux (TAP + Linux bridge) and macOS (feth +
BSD bridge) are supported; the cross-platform dispatch module lives in
`tests/bridge_platform.py` (constants `ETHERNET_DRIVER`, `IFACE_A`,
`IFACE_B`, `BRIDGE_NAME`, `SETUP_HINT`).

## Overview

The pattern uses (Linux naming shown; macOS equivalents in parentheses):

* `br-c64` (macOS: `bridge10`) -- a host network bridge
* `tap-c64-0` / `tap-c64-1` (macOS: `feth0` / `feth1`) -- two
  bridge-member interfaces, one per VICE instance
* Two `x64sc` processes, each launched with RR-Net-mode CS8900a
  ethernet bound to its interface (VICE's `tuntap` driver on Linux,
  `pcap` driver on macOS)

This setup gives two VICE instances a shared layer-2 segment.  The
host can also participate (the bridge's IP is `10.0.65.1` on both
platforms), so captures via `tcpdump -i br-c64` (Linux) or
`tcpdump -i bridge10` (macOS) will show all traffic between the
instances.

## Reference pattern for VICE agents

When an agent working on c64-test-harness needs two VICE instances that
can exchange ethernet frames, use this canonical lifecycle:

1. **Setup** (once per session, as root):

   ```bash
   sudo scripts/setup-bridge-tap.sh            # Linux
   sudo scripts/setup-bridge-feth-macos.sh     # macOS
   ```

2. **Acquire VICE instances** via the `bridge_vice_pair` pytest fixture
   in `tests/conftest.py`, or the equivalent `ViceProcess`-based pattern
   for non-pytest code. See `tests/test_bridge_ping.py` for full fixture
   usage, and `scripts/bridge_ping_demo.py` for a standalone script
   reference.

3. **Run your code**. The fixture handles CS8900a init, MAC programming,
   and clean VICE shutdown on context exit.

4. **Teardown** (after the last session completes, as root):

   ```bash
   sudo scripts/teardown-bridge-tap.sh           # Linux
   sudo scripts/teardown-bridge-feth-macos.sh    # macOS
   ```

5. **Recovery** (only if a session died uncleanly, leaving residue):

   ```bash
   sudo scripts/cleanup-bridge-networking.sh     # Linux
   sudo scripts/cleanup-bridge-feth-macos.sh     # macOS
   ```

### Rules for VICE lifecycle

- **Never `pkill x64sc`.** It kills every VICE on the host including
  unrelated instances.  Use `scripts/cleanup_vice_ports.py` instead,
  which is scoped to the harness's known port ranges and verifies each
  target's `/proc/<pid>/comm` before sending any signal.  See
  `feedback_no_pkill.md`.
- **The Python harness owns VICE lifecycle in the happy path.** Let
  `ViceProcess.__exit__` / `ViceInstanceManager.release()` stop VICE
  cleanly.  The cleanup script is only for the "my session crashed"
  case.
- **Setup and teardown are symmetric.** On Linux they touch exactly
  these resources: the `br-c64` bridge, `tap-c64-0` / `tap-c64-1` TAP
  devices, six FORWARD iptables rules, and `/tmp/vice_eth_*.rc` stale
  files.  They never touch `/proc/sys/net/ipv4/ip_forward` — the host
  default is preserved.  On macOS the scope is `bridge10` + `feth0` /
  `feth1` + `/tmp/vice_eth_*.rc`; no pf/iptables state is involved.
- **Interface names are canonical per platform.** The fixture, setup
  script, teardown script, and cleanup script all agree on
  `br-c64` / `tap-c64-{0,1}` (Linux) or `bridge10` / `feth{0,1}`
  (macOS).  The single source of truth is `tests/bridge_platform.py`;
  don't drift — update that module and all four scripts in lockstep if
  you ever need to rename.
- **Port ranges for harness VICE instances**: `6511-6531` and
  `6560-6580` (per `HarnessConfig.vice_port_range_start/end` and the
  bridge fixture respectively).  The cleanup helper scopes to these
  ranges by default.

### Recovery helper

Both platforms ship a standalone sudo cleanup script
(`scripts/cleanup-bridge-networking.sh` on Linux,
`scripts/cleanup-bridge-feth-macos.sh` on macOS) and share
`scripts/cleanup_vice_ports.py` for the scoped VICE-kill step. The
Python helper is cross-platform: it discovers harness-port listeners via
`/proc/net/tcp` on Linux and `lsof`/`ps` on macOS, and `ViceProcess`'s
port-based introspection (`get_listener_pid`, `kill_on_port`) likewise
supports both platforms natively.

`scripts/cleanup_vice_ports.py` is the port-range-scoped VICE killer:

```bash
python3 scripts/cleanup_vice_ports.py --range 6511:6531,6560:6580
python3 scripts/cleanup_vice_ports.py --range 6511:6531 --dry-run
python3 scripts/cleanup_vice_ports.py --help
```

It resolves listeners in the requested ranges to PIDs, verifies the
process is `x64sc` (comm check via `/proc/<pid>/comm` on Linux or `ps`
on macOS), then SIGTERMs, waits a grace period (default 2 s), and
SIGKILLs survivors.  Safe to run while unrelated VICE instances
(outside the harness port ranges) are alive — they won't be touched.
Exit code is `0` on a clean result, `1` if any process is still alive
after SIGKILL, `2` on argument error, and `3` if listener(s) were found
but comm could not be read for any of them (insufficient privileges —
re-run with `sudo`).

On Linux specifically, exit 3 is typically caused by `x64sc` file
capabilities (`cap_net_admin,cap_net_raw=ep`) making unprivileged
`/proc/<pid>/comm` reads fail; the helper detects this and flags it
instead of silently reporting zero.

The scoping is empirically verified by `tests/test_cleanup_vice_ports_live.py::TestBridgeCleanupScoping::test_scoped_cleanup_preserves_out_of_range_vice` (opt in with `BRIDGE_CLEANUP_LIVE=1`).

## Prerequisites (Linux)

* `x64sc` (VICE 3.10) compiled with `tuntap` driver support
* Root privileges to create TAP devices and configure the bridge
  (only required for setup/teardown -- VICE itself runs unprivileged)
* `ip` (iproute2) and `iptables`
* The c64-test-harness package (`c64_test_harness.bridge_ping`)

## Setting up the bridge (Linux)

```bash
sudo ./scripts/setup-bridge-tap.sh
```

This creates:
- `br-c64` bridge with IP `10.0.65.1/24`
- `tap-c64-0` and `tap-c64-1` TAP interfaces, both attached to the bridge
- iptables FORWARD rules permitting traffic on the bridge

To tear down:

```bash
sudo ./scripts/teardown-bridge-tap.sh
```

If something goes wrong, an emergency cleanup is available:

```bash
sudo ./scripts/cleanup-bridge-networking.sh
```

## macOS (feth + BSD bridge)

The macOS path is a drop-in replacement for the Linux TAP layout. It
uses `feth0`/`feth1` (a kernel "fake ethernet" peer pair) bridged via
the BSD `bridge10` pseudo-device, all driven through `ifconfig`. VICE
attaches with its `pcap` driver instead of `tuntap`, because macOS has
no `/dev/net/tun` and `libpcap`-over-BPF is the portable path.

```
   host (10.0.65.1 on bridge10)
              |
         +----+----+
         | bridge10|
         +----+----+
          /        \
      feth0      feth1        (peered; frames pass through bridge10)
        |          |
      VICE A     VICE B       (-ethernetiodriver pcap -ethernetioif fethN)
```

Lifecycle (see the reference patterns below — do not inline the ifconfig
steps in agent code; call the scripts):

```bash
sudo ./scripts/setup-bridge-feth-macos.sh       # create bridge10 + feth0/feth1
sudo ./scripts/teardown-bridge-feth-macos.sh    # symmetric teardown
sudo ./scripts/cleanup-bridge-feth-macos.sh     # emergency recovery (scoped VICE kill)
```

The setup script is idempotent. Internally it runs, roughly:

```bash
ifconfig feth0 create
ifconfig feth1 create
ifconfig feth0 peer feth1
ifconfig feth0 up && ifconfig feth1 up
ifconfig bridge10 create
ifconfig bridge10 addm feth0
ifconfig bridge10 addm feth1
ifconfig bridge10 inet 10.0.65.1 netmask 255.255.255.0 up
```

Prerequisites:

* `x64sc` (VICE 3.10 Homebrew bottle — pre-built with `--enable-ethernet`
  and the pcap driver)
* Root privileges for `ifconfig create`/`addm` (only for setup/teardown;
  VICE itself runs unprivileged)
* `/dev/bpf*` readable by your user — install Wireshark's **ChmodBPF**
  helper (recommended) or `sudo chmod 666 /dev/bpf*` for a one-shot
  (resets on reboot). Without this, VICE's pcap driver fails to attach
  `feth0`/`feth1` with a `pcap_open_live` / BPF permission error.
* The c64-test-harness package (`c64_test_harness.bridge_ping`)

Notes:

* The macOS setup does **not** configure a host firewall. There is no
  pf ruleset or NAT layer analogous to the Linux `iptables FORWARD`
  rules — the BSD bridge driver forwards freely between its members,
  and no outside-host routing is involved. Teardown therefore has no
  pf state to reverse.
* We deliberately use `bridge10` rather than `bridge0`. `bridge0` is a
  pre-existing system bridge on macOS (Thunderbolt / Internet Sharing)
  that may already have system interfaces as members; attaching `feth`
  peers or assigning our IP to it would pollute it.
* VICE attachment: each instance is launched with
  `-ethernetiodriver pcap -ethernetioif feth0` (or `feth1`). The
  `ViceConfig` mapping handles this automatically when
  `ethernet_driver="pcap"` is set — see `tests/bridge_platform.py` for
  the `ETHERNET_DRIVER` constant that the fixtures read.

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

To launch manually (Linux values shown; on macOS substitute
`ethernet_interface="feth0"`/`"feth1"` and `ethernet_driver="pcap"` —
or pull both from `tests/bridge_platform.py`):

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

Why not let the 6502 own the timeout in the test harness?  Earlier
versions used a 3-level inner counter (`DEC $F0/$F1/$F2`) to bound the
poll to "about 5 seconds".  That budget is denominated in 6502 cycles,
so it evaporates in microseconds under VICE warp mode -- the C64 gives
up before any TAP frame can arrive.  For **shippable applications**
that do not run under warp, 6502-owned timeouts are appropriate and
supported via CIA1 TOD (see "Test harness vs shippable application"
below); warp-mode test runs must use the host-driven pattern
described here.

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

The older `build_icmp_responder_code` / `build_ping_and_wait_code` /
`build_rx_echo_reply_code` builders remain the right choice for tests
that run under VICE warp mode, because their polling budget is owned
by the host-side `poll_until_ready` wrapper rather than by an in-6502
counter.  For **shippable applications** (real C64, Ultimate 64 Elite,
VICE normal mode) use the `*_tod_code` variants in the "Test harness
vs shippable application" section below instead.

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

## Test harness vs shippable application

The bridge networking helpers in this project come in **two flavours**
that solve different problems:

### 1. Test-orchestration path (host-driven)

Used by `tests/test_bridge_ping.py` and `scripts/bridge_ping_demo.py`.
The Python test harness owns the wall clock: it pauses the 6502
between iterations via the VICE binary monitor, checks host-side
monotonic time, and decides when to time out.  This pattern works
under **VICE normal mode**, **VICE warp mode** (for fast automated
test runs), and for **Ultimate 64**-backed tests.

Relevant helpers: `build_tx_code`, `build_rx_echo_reply_code`,
`build_ping_and_wait_code`, `build_icmp_responder_code` in
`c64_test_harness.bridge_ping`.

**This path is not shippable.**  A real C64 networking application
running on bare iron or a standalone Ultimate 64 Elite has no Python
driving a binary-monitor socket on the other side, so the 6502 code
cannot rely on the host to enforce timeouts.

### 2. Shippable-application path (6502-driven TOD)

Used by the lower-level code builders in
`c64_test_harness.tod_timer`.  The 6502 owns its own deadlines by
reading **CIA1 Time-of-Day** and comparing against a pre-computed
"tenths-since-start-of-poll" value.  This is pure 6502 code; it runs
standalone on:

* Real Commodore 64 hardware (TOD at wall-clock rate).
* Real Ultimate 64 Elite, at any turbo speed from 1 to 48 MHz (TOD
  is flat 1.0x across the full turbo range -- verified empirically).
* VICE 3.10 normal mode (TOD at ~1.0x wall).

It does **not** work under VICE warp mode, where CIA1 TOD is virtual-
CPU clocked and accelerates with the CPU (~31x wall on VICE 3.10);
the 6502 timeout would expire ~31x too fast.  Shippable applications
do not run under warp anyway; only automated tests do, and those use
the test-orchestration path above.

The TOD poll core lives in `src/c64_test_harness/tod_timer.py` and
exposes three code builders:

* `build_tod_start_code(load_addr)` -- start CIA1 TOD at 00:00:00.0.
* `build_tod_read_tenths_code(load_addr, result_addr)` -- read TOD
  and store elapsed tenths since start as an LE16 value.
* `build_poll_with_tod_deadline_code(load_addr, peek_snippet,
  result_addr, deadline_tenths)` -- generic poll loop that calls a
  user-supplied 6502 "ready?" snippet and bails out when the TOD
  deadline elapses.  `peek_snippet` is raw 6502 bytes that must
  leave `Z=0` when the device is ready -- for CS8900a RxEvent this
  is `LDA $DE05 / AND #$01`, for a UCI response-ready bit it would
  read the UCI status register, etc.  This is the generalization
  boundary for eventual UCI support.

Zero-page footprint: `$F0`-`$F5`.  Deadline cap: **599 tenths
(59.9 s)** -- for longer waits, loop in the caller.

### Which pattern should I use?

| Scenario                                        | Use |
| ----------------------------------------------- | --- |
| Pytest test on VICE normal mode                 | Either (test path is simpler) |
| Pytest test on VICE warp mode                   | Test path (host-driven) |
| Pytest test on Ultimate 64                      | Test path (host-driven) |
| Validate a 6502 ping routine end-to-end         | Either |
| Ship a `.prg` on disk to a real C64 user        | **Shippable path** (TOD) |
| Run on a standalone U64E with no host           | **Shippable path** (TOD) |
| Run on VICE warp to burn CI budget              | Test path (host-driven) |

The two paths are **additive** -- neither replaces the other.  The
higher-level `build_*_tod_code` variants in `bridge_ping.py` wrap the
TOD poll core for common ICMP scenarios:

* `build_ping_and_wait_tod_code` -- pure-6502 ping-and-wait that
  TXes an echo request, polls RX with a TOD deadline, reads the
  reply, and verifies identifier/sequence.
* `build_icmp_responder_tod_code` -- pure-6502 responder that polls
  RX with a TOD deadline, receives one ICMP echo request for a
  given IP, transforms it into an echo reply in place, and TXes it.
* `build_rx_echo_reply_tod_code` -- pure-6502 echo reply receiver
  that polls RX with a TOD deadline and drains frames into a
  buffer until one matches the expected identifier/sequence.

All three are drop-in counterparts of the host-driven
`build_ping_and_wait_code` / `build_icmp_responder_code` /
`build_rx_echo_reply_code` and take the same arguments plus
`deadline_tenths` (1..599).  See `tests/test_bridge_ping_tod.py` for a
full two-VICE bridge round trip using these variants on VICE normal
mode, plus a live Ultimate 64 TOD primitive test at 1 / 8 / 24 / 48
MHz turbo speeds (gated by `U64_HOST`).

## See also

* `tests/test_ethernet_bridge.py` -- raw L2 broadcast frame exchange
  (works fully end-to-end, both directions)
* `tests/test_bridge_ping.py` -- IP-layer ICMP exchange via the bridge
* `src/c64_test_harness/bridge_ping.py` -- helpers for building
  ICMP echo frames and 6502 RX/TX routines for RR-Net mode
  (register offsets match ip65's `cs8900a.s`)
* `src/c64_test_harness/tod_timer.py` -- CIA1 TOD-based 6502 timeout
  helpers for the shippable-application path (see "Test harness vs
  shippable application" above)
* `tests/test_tod_timer.py` -- unit tests for the TOD code builders
* `tests/test_bridge_ping_tod.py` -- live TOD-based bridge ping round
  trip on VICE normal mode (shippable-application path) plus live
  U64 TOD primitive test across turbo speeds
* `scripts/setup-bridge-tap.sh` / `scripts/teardown-bridge-tap.sh` /
  `scripts/cleanup-bridge-networking.sh` (Linux)
* `scripts/setup-bridge-feth-macos.sh` /
  `scripts/teardown-bridge-feth-macos.sh` /
  `scripts/cleanup-bridge-feth-macos.sh` (macOS)
* `tests/bridge_platform.py` — cross-platform constants
  (`ETHERNET_DRIVER`, `IFACE_A`, `IFACE_B`, `BRIDGE_NAME`, `SETUP_HINT`)
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
