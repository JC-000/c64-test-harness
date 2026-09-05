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
# NOTE: feth0/feth1 are intentionally NOT added as members of bridge10.
# The feth peer mechanism already provides L2 between them; adding the
# peers as bridge members creates a duplicate forwarding path and causes
# duplicate / looped frames. bridge10 exists only to hold the host-side
# IP. (The setup script actively removes feth members if a prior botched
# run added them.)
ifconfig bridge10 inet 10.0.65.1 netmask 255.255.255.0 up
```

Prerequisites:

* `x64sc` — **the Homebrew bottle is the right binary**; no separately
  built VICE is needed. It reports `HAVE_RAWNET yes` / `HAVE_PCAP yes`
  (`HAVE_TUNTAP no`) to `x64sc -features` and links libpcap. Leave
  `$VICE_ETHERNET_BIN` unset; it is an override, not a requirement.
* Root privileges for `ifconfig create`/`addm` (setup/teardown) **and for
  VICE itself**. VICE registers its pcap driver only when
  `archdep_rawnet_capability()` holds — `geteuid() == 0` on macOS — so an
  unelevated ethernet launch has no driver at all and SIGSEGVs on reset.
  The harness refuses such a launch rather than crashing; see trap 2
  below.
* A NOPASSWD sudoers rule naming the exact x64sc path, for unattended
  runs (`/opt/homebrew/bin/x64sc` — the literal path, not its Cellar
  symlink target). Being *permitted* to sudo is not enough: a launch that
  stops at a password prompt is a failed launch.
* `/dev/bpf*` permissions are **not** a prerequisite for VICE. It never
  reads those nodes, and `chmod o+rw /dev/bpf*` changes nothing it
  consults — running as root is what makes *its* capture work. They
  **are** the prerequisite for the harness's own unelevated host-side
  capture (`c64_test_harness.capture`, the TX/RX tests) — see
  "Host-side capture" below.
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

### Host-side capture on macOS (issue #158)

`tests/test_ethernet.py`'s TX and RX tests check that a frame the C64
transmits actually reaches the wire, and inject a frame for it to
receive. On Linux that is an `AF_PACKET` socket; macOS has none, so until
`c64_test_harness.capture` existed those two tests skipped on the primary
bench and nothing verified emitted frames host-side at all.

`open_capture(iface)` returns the platform's `PacketCapture`
(`recv(timeout, match=...)`, `send(frame)`): `AfPacketCapture` on Linux,
`BpfCapture` on macOS. `BpfCapture` opens the lowest `/dev/bpfN` this
process may, then `BIOCIMMEDIATE`, `BIOCSHDRCMPLT` (injected source MACs
are left alone), `BIOCSSEESENT`, `BIOCSETIF`, `BIOCPROMISC`; reads are
runs of `bpf_hdr` records split by `parse_bpf_records()` (pinned by hand-
built headers in `tests/test_capture.py`). It needs **no elevation** —
only a node the process can open.

What the bench looks like (measured 2026-09-01, uid 501):

* `/dev/bpf0-3` are `crw----rw-` — a manual `chmod o+rw`; there is no
  ChmodBPF LaunchDaemon and no `access_bpf` group, so **the mode does not
  survive a reboot**. `/dev/bpf4-7` are root-only, and macOS creates
  further nodes on demand *only for root*.
* A root VICE takes the lowest two free nodes per instance — i.e. exactly
  the ones the chmod opened. With one VICE up and one stray holder
  (`netstat -B` lists them), one node is left for the harness.
* Full unelevated sequence verified: open `/dev/bpf1`, `BIOCGBLEN`=4096,
  `BIOCSETIF feth0`, `BIOCGDLT`=1 (EN10MB), `BIOCPROMISC`.

When nothing can be opened, `open_capture` raises `CaptureUnavailable`
whose message carries the operator remedy verbatim, and the two tests
skip **with that message as the reason** — only then. When a capture is
open, a silent wire is a **failure**: `run_tx_scenario` raises when no
frame with the test ethertype arrives, and `run_rx_scenario` raises on a
failed host send or a C64 poll timeout (the old code swallowed the send
error and skipped on the timeout). `tests/test_ethernet_capture_wiring.py`
proves both with fakes.

Remedy after a reboot or when the pool is short (no sudoers change):

```bash
sudo chmod o+rw /dev/bpf*
```

Order matters after a reboot: devfs exposes only `bpf0-3` until a root
process opens more — macOS creates `bpf4+` on demand *for root only*
(e.g. the next elevated VICE launch takes `bpf0`+`bpf1`, then `bpf2`…). A
`chmod` run before that widens nothing beyond the four that exist, so
either launch VICE first and `chmod` afterwards, or accept that with one
VICE up only the nodes it did not take (`bpf2-3`) are open to the
harness.

The tests open the capture *after* the module's VICE fixture has taken
its nodes, so the one `open_capture()` call reflects the pool this
process really has. Its exception is classified: **skip** only on genuine
absence (no nodes, no `CAP_NET_RAW`, no backend); **fail**, remedy in the
message, when the path exists but is broken — all writable nodes `EBUSY`
while VICE is live (pool eaten), `BIOCSETIF` failing on the interface the
platform helper just found, a non-ethernet DLT, a Linux bind failure, an
unclassified errno — **and** every node root-only while a root VICE is up:
that is this bench's state after every reboot (the chmod is not
persisted), and with an elevated ethernet VICE already running it is a
misconfigured bench, not a missing capability
(`capture_failure_disposition(..., vice_live=True)`). No
`tcpdump` NOPASSWD rule exists or is needed; if an operator prefers
sudoers over chmod, `someone ALL=(root) NOPASSWD: /usr/sbin/tcpdump`
would enable a subprocess path the harness does not currently implement.

**Direction assumption and the peer knob.** `feth0`/`feth1` are a peer
pair (`ifconfig feth0` reports `peer: feth1`). A frame VICE injects on
`feth0` is *outgoing* there and *incoming* on `feth1`; a frame the host
writes to `feth0`'s BPF emerges from `feth1`. By default the harness
binds capture and send to VICE's interface and relies on `BIOCSSEESENT`
(TX) and the driver's write-path tap (RX). If that assumption is wrong
the failure message says so without a re-run: it ends with `netstat -B`
counters for the interface, and VICE's own descriptor reads
**`Written=1`** while nothing was captured — the chip put the frame on
the interface, the capture was on the wrong side. **`Written=0`** means
the frame died inside the emulated CS8900a (chip fault; check the
routine's RxCTL/LineCTL enable, `cs8900a_enable_inline_code`). Pivot to
the peer without a code change:

```bash
C64_ETH_CAPTURE_IFACE=feth1 pytest tests/test_ethernet.py   # capture + send on the peer
C64_ETH_SEND_IFACE=feth1    pytest tests/test_ethernet.py   # send on the peer only
```

(`C64_ETH_SEND_IFACE` follows `C64_ETH_CAPTURE_IFACE` unless set
separately; a second interface costs a second BPF node.) The knob is
`ethernet_scenarios.resolve_capture_ifaces()`.

**Linux behaviour change.** The TX test used to accept the *first* frame
`AF_PACKET` returned within 5 s and compare it; it now discards frames
whose ethertype is not `0x88B5` until the deadline, so stray traffic on
the TAP (IPv6 multicast, ARP) can no longer fail the test by arriving
first — and can no longer *pass* it either, since the compared frame is
always ours.

### macOS test-author traps (live tests only)

These three gotchas are not present on the Linux side. They were
surfaced empirically while landing
`tests/test_cleanup_vice_ports_macos_live.py`; that file is the canonical
working reference for any new live test that drives the macOS bridge.

**1. NOPASSWD is scoped to the exact program path, not `bash <script>`.**
The project's sudoers grant NOPASSWD for the cleanup/setup/teardown
script paths directly. A test helper that does
`subprocess.run(["sudo", "-n", "bash", script_path])` asks sudo to run
`bash` (a different program from the matcher's POV) and fails with
"password required". Always invoke scripts directly via their shebang:

```python
def _run_sudo_script(script_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sudo", "-n", str(script_path)],   # NOT ["sudo", "-n", "bash", ...]
        check=True, capture_output=True, text=True,
    )
```

The Linux equivalent (`tests/test_cleanup_vice_ports_live.py`) uses the
`bash` wrapper because Linux sudoers there are configured permissively;
do not copy that helper verbatim onto macOS.

**2. `ViceConfig.ethernet=True` needs root on macOS, and `/dev/bpf*` has
nothing to do with it.**
VICE admits an ethernet driver only when `archdep_rawnet_capability()`
holds. That function is, in full: `geteuid() == 0`, plus a Linux-only
`CAP_NET_RAW` branch (`src/arch/shared/archdep_rawnet_capability.c`). It
never inspects `/dev/bpf*`. The result gates *driver selection* in
`rawnetarch.c` (`set_ethernet_driver()` and `rawnet_arch_resources_init()`),
so an unelevated macOS VICE leaves `rawnet_arch_driver` NULL and
dereferences it in `rawnet_arch_pre_reset()` — **SIGSEGV with no log
output at all**. It does not degrade to "no traffic"; it dies.

**The `10.0.65.0/24` range belongs to the harness.** The setup scripts put
the host at `.1` (`BRIDGE_ADDR`) and the ethernet tests answer on `.2` and
`.3`. Those three addresses are reserved; they are defined once in
`tests/bridge_platform.py` (`BRIDGE_HOST_IP`, `BRIDGE_IP_A`,
`BRIDGE_IP_B`) rather than repeated as literals, and the whole range can
be moved with `C64_BRIDGE_SUBNET=10.77.1` when the harness has to coexist
with a rig that already owns `10.0.65/24`.

**Consumer rigs built on this bridge must stay clear of `.1`-`.3`.** A rig
that reuses the harness's bridge and runs its own services should take
`.100` upward. This is not hypothetical: c64-https's `rig-up-macos.sh`
calls the harness setup script, then moves the host address to `feth1`
while keeping `.1`, and runs a dnsmasq DHCP pool of `.2-.10` — handing out
exactly the addresses the harness's two-VICE tests hardcode. Nothing
detects that clash; the bridge tests simply fail or behave oddly while the
consumer rig is up.

Verified live that the euid gate, not `/dev/bpf*` permissions, is what
VICE checks: with `/dev/bpf0` at `crw----rw-` (world read/write) and uid
501, `-ethernetiodriver pcap` is still rejected.

> An earlier version of this section claimed a rig that ran
> `sudo chmod o+rw /dev/bpf*` needed no elevation. That rule was wrong —
> it modelled libpcap's requirements rather than VICE's gate, and
> `bpf_capture_available()` has been removed.
>
> The **pool observation is not retracted**: one VICE holding two
> `/dev/bpf*` nodes and a second instance dying `rc=255` was recorded
> correctly, under `sudo`, and only its context was lost. Re-verified
> 2026-08-30 with `netstat -B`: a single elevated x64sc holds exactly two
> BPF peers — one bound to the requested `feth`, one to another host
> interface — and the count of nodes an unprivileged process can still
> open drops by exactly two. That is the old `BPF_NODES_PER_VICE = 2`,
> measured again. As root macOS creates further nodes on demand, so the
> pool does not block a second instance the way it blocks an unprivileged
> one, but a pool exhausted by another capturing process can still bite a
> multi-instance run.

### Issue #144 is refuted: the Homebrew bottle captures fine

#144 recorded that *as root*, VICE answers the binary monitor while
attaching no BPF device — i.e. that the Homebrew build's ethernet was
silently non-functional and a separate build was required. **That is
false.** Measured 2026-08-30, elevated, cart active, with the interface
and driver supplied only through the `-addconfig` rc:

```
wrapper=4636  x64sc=4637  owner=root  monitor=up
  ETHERNET_DRIVER      = 'pcap'
  ETHERNET_INTERFACE   = 'feth0'
  ETHERNETCART_ACTIVE  = 1
netstat -B:
  bpf1  ap1    p---IO------  x64sc.4637
  bpf2  feth0  p---IO------  x64sc.4637
```

Two BPF descriptors, one bound to the requested interface, in
promiscuous mode.

The claim came from the harness's own measurement. `probe_vice_pcap_ok()`
demanded a `/dev/bpf*` attach as proof of real capture — correctly — but
read it with `lsof -nP -p <pid>` run **unelevated**. An unprivileged
`lsof` cannot read a root-owned process's descriptor table at all: it
returns *zero lines*, not zero `bpf` lines. Since every macOS pcap launch
elevates, the helper returned `[]` every time, and the probe published
that as a defect in the emulator build. Its own diagnostic string is what
#144 was written from.

### The rc alone is sufficient; the ethernet CLI flags are redundant

The same elevated run settles a second question. `ViceProcess` writes an
`-addconfig` rc *and* passes `-ethernetioif` / `-ethernetiodriver`, and
two rc keys were misspelled (`EthernetIOIF` / `EthernetIODriver` — not
VICE resources in any casing; the real names are `ETHERNET_INTERFACE`
and `ETHERNET_DRIVER`). That was first recorded as harmless, because the
CLI flags carried the same settings.

It was worse than that. With the corrected names and **no ethernet CLI
flags at all**, the rc on its own produces `ETHERNET_DRIVER='pcap'`,
`ETHERNET_INTERFACE='feth0'`, `ETHERNETCART_ACTIVE=1` and two attached
BPF peers. The rc is sufficient by itself, so the CLI flags are
redundant rather than load-bearing — and the misspelling was harmless
only on paths that happened to pass both. Any path relying on the rc
alone was silently unconfigured. Hence the fix writes the real names
rather than dropping the lines.

The instrument is now `netstat -B` (`bpf_attached_interfaces()` in
`tests/bridge_platform.py`), which reports device, bound interface and
owning command, needs no privilege, and reads root-owned processes.
`lsof` is the wrong tool for this measurement at any privilege level.
Regression test: `tests/test_bpf_attach_detection.py`, which launches a
real elevated VICE and asserts the attach is seen — it fails against the
`lsof` implementation.

So on macOS every pcap ethernet launch elevates. The harness refuses to
launch one it cannot elevate: `plan_vice_launch()` (in
`c64_test_harness.backends.vice_elevation`) parses the `NOPASSWD:` rules
out of plain `sudo -n -l` (`sudo_can_run` → `parse_sudo_listing`; a
per-command `sudo -n -l -- <x64sc>` probe exits 0 for anything a
`(ALL) ALL` user may run and proves nothing) and, when no rule names the
exact binary, raises `ViceElevationRequiredError` carrying
the exact command to run and a NOPASSWD line naming that exact binary
path — never `bash`-wrapped, since sudoers matches sudo's first non-flag
argument. `VICE_ETHERNET_ALLOW_UNELEVATED=1` downgrades the refusal to a
warning for a host that grants the capability some way we cannot see
(Linux file capabilities, say).

Linux is unaffected: the `tuntap` driver is selected without consulting
the capability, so the Linux bridge suite needs no elevation.

When elevation *does* fire, `ViceProcess` wraps the launch in
`sudo -n x64sc …`, so the recorded `ViceProcess.pid` is the **sudo
wrapper's** PID, not the actual `x64sc` process. `ps -p <sudo_pid> -o
ucomm=` returns `"sudo"`, which breaks any `_is_x64sc(pid)` sanity check.
Check `ViceProcess.is_sudo_child` rather than assuming either shape.

Resolve the real x64sc descendant before asserting:

```python
def _resolve_x64sc_child(parent_pid: int) -> int | None:
    out = subprocess.run(
        ["pgrep", "-P", str(parent_pid), "x64sc"],
        capture_output=True, text=True, check=False,
    )
    for line in out.stdout.splitlines():
        if line.strip().isdigit():
            return int(line.strip())
    return None
```

Sentinel VICEs spawned without `ethernet=True` are NOT sudo-wrapped, so
`ViceProcess.pid` is correct for them — the resolver only applies to
ethernet-enabled bridge VICEs.

`ViceProcess` now ships this resolver: check `proc.is_sudo_child` and
call `proc.resolve_vice_pid()` to get the actual x64sc PID (equal to
`proc.pid` for plain launches) instead of hand-rolling the `pgrep`
snippet above.

**3. macOS `ps -o ucomm=` preserves the comm name on zombies. Use `stat=`.**
A SIGKILL'd-not-yet-reaped process retains its `ucomm` value, so a
liveness helper that does `os.kill(pid, 0)` and then treats `comm == ""`
as a zombie signal will silently report dead processes as alive. The
right zombie indicator is the BSD STAT field — leading `Z` means zombie:

```python
def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    out = subprocess.run(
        ["ps", "-p", str(pid), "-o", "stat="],
        capture_output=True, text=True, check=False,
    )
    if out.returncode != 0:
        return False
    stat = out.stdout.strip()
    return bool(stat) and stat[0] != "Z"
```

Pair this with `Popen.poll()` calls in the test body to actually reap
zombies whose parent is pytest. Without poll, the kernel keeps the PID
alive until pytest exits; with poll, the next `os.kill(pid, 0)` raises
`ProcessLookupError` cleanly. The Linux `_pid_alive` reads
`/proc/<pid>/status State:` for the same purpose; macOS just gets the
state via `ps` instead.

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

# Initialise CS8900a on each instance: RxCTL = CS8900A_RXCTL_VALUE,
# LineCTL |= 0x00C0
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

* **TX**: write `TxCMD = CS8900A_TXCMD_VALUE` (`0x00C9`: transmit-after-full-frame
  `0x00C0` plus the register number `0x09` in the read-only low 6 bits -- a bare
  `0x00C0` is the same omission as the old RxCTL `0x00D8`), `TxLength = N`, then poll BusST
  (PP `0x0138` bit 8) for `Rdy4TxNOW`, then write N bytes to RTDATA.
* **RX**: poll the high byte of RxEvent (PP `0x0124`) masked with
  `CS8900A_RXEVENT_MASK` (`0x0D` = RxOK | IndividualAdr | Broadcast, ip65's mask;
  the old `AND #$01` missed frames the chip signalled without RxOK), then read 2
  bytes RxStatus + 2 bytes RxLength + N bytes frame data from RTDATA.
  **Read the two header words high-half-first** -- see "Real silicon"
  below; low-half-first works under VICE and desynchronises a real chip.
* **MAC**: write 3 words to IA registers (PP `0x0158` -- `0x015D`).
* **RxCTL** (PP `0x0104`): set to `CS8900A_RXCTL_VALUE` (`0x0D85`, PromiscuousA
  set -- accepts every frame on the segment; use `CS8900A_RXCTL_VALUE_IP65`
  (`0x0D05`) for broadcast + IA-matching unicast only).  See
  `cs8900a_rxctl_code()` in `c64_test_harness.bridge_ping`.
* **LineCTL** (PP `0x0112`): set bits 6 and 7 (`SerRxON` and
  `SerTxON`) to enable RX and TX.

## Real silicon diverges from VICE in three ways

All three were found bringing an external RR-Net cartridge up on a U64E
expansion port; all three are invisible to the two-VICE bridge suite,
because VICE is more forgiving than the chip.

**1. RxCTL's low 6 bits are read-only** (issue #207).  On a real CS8900a
every control/status register reports its own register number in the low
6 bits -- measured reset values say so throughout: RxCTL `0x0005`,
LineCTL `0x0013`, SelfCTL `0x0015`, BusCTL `0x0017`, BusST `0x0018`.  The
harness's old `0x00D8` reads back as **`0x00C5`** (bits 3-4 replaced by
the register number).  The read-back is the tell, not the cause: the
cause is that `0x00D8` never contained `RxOKA` (`0x0100`) at all, and
without `RxOKA` the receiver accepts nothing.  It appeared to work under
VICE because `cs8900.c`'s acceptance filter never consults `RxOKA` -- it
accepts on the address filter alone.  `CS8900A_RXCTL_VALUE` is now `0x0D85`
(PromiscuousA | RxOKA | IndividualA | BroadcastA + register number);
`CS8900A_RXCTL_VALUE_IP65` (`0x0D05`) is the same without promiscuous,
which is what ip65 programs and what you want on a busy segment.

**2. RTDATA half ordering is not free choice** (issue #210).  The two
header words (RxStatus, RxLength) must be read **high half (`$DE09`)
first**; the data body is then read low half first.  That is ip65's
`cs8900a.s` ordering.  Reading `$DE08` before `$DE09` for the header
desynchronises the FIFO by one byte: `RxLength` comes back garbage and
every data word arrives byte-swapped, so every offset-based check fails
against a frame that is perfectly correct on the wire.  VICE's `cs8900.c`
implements the datasheet order as well (`:817-828`, citing §4.10.9) but
advances its RX pointer on the *low* read, so a low-first header still
left the body aligned there -- the four discarded header bytes were wrong
even under VICE, and nothing checked them.  Real silicon advances
differently and the body desynchronises.  `tests/test_cs8900a_frame_reader.py`
pins the ordering as a unit test precisely because no VICE test can fail
on it.

**3. Host-side access never reaches a hardware cartridge -- but it does
reach VICE's emulated one.**  On the U64, `transport.write_memory(0xDE02,
...)` and `read_memory(0xDE00, ...)` go through the machine's REST/DMA
path, which does not present the access on the expansion port: the bytes
a read returns are not meaningful and not reproducible (one session read
a constant `0A` x16; another read values that changed between
back-to-back reads and a write that did not round-trip -- U64E fw 3.15
`4011c97c`, `Cartridge Preference = Auto`, no PRG, at READY, cartridge
fitted and linked; n=4 reads, n=1 write, with a control write to RAM at
`$0340` round-tripping normally; c64-wireguard project measurement).
Under VICE the same host path goes through bank `default`, which is the
6510's current memory map (`c64mem.c:1422-1427` -> `mem_store` ->
`c64io_de00_store`), so it **does** reach the emulated CS8900a:
`set_cs8900a_mac(02:c6:40:00:00:77)` reads back on the 6510 as exactly
that (measured, VICE 3.10, n=1), and `ViceInstanceManager` relies on it
to assign per-instance MACs.  So `ethernet.set_cs8900a_mac()` is
**useless on hardware**, not "VICE-only by design": on a real cartridge
the MAC must be written by a 6502 routine --
`cs8900a_set_mac_inline_code(mac)` (no RTS; prepend
`cs8900a_enable_inline_code()`, which does the clockport enable) or the
callable `cs8900a_set_mac_code(mac)`, both in
`c64_test_harness.bridge_ping`.  Same advice on both platforms for
opposite reasons; a reader who learns only one will draw the wrong
conclusion on the other.  `CS8900A_RXCTL_VALUE` keeps PromiscuousA only
to preserve the behaviour existing bridge tests and downstream consumers
were written against -- IA filtering does work under VICE, and
`CS8900A_RXCTL_VALUE_IP65` (`0x0D05`) is the value to reach for.

**Two more alignments with ip65** landed in the same change and are not
silicon-vs-VICE failures -- neither caused a measured fault:
`CS8900A_TXCMD_VALUE` is `0x00C9` (the register number in the low 6 bits,
not a bare `0x00C0`), and the RxEvent poll masks `CS8900A_RXEVENT_MASK` =
`0x0D` (RxOK | IndividualAdr | Broadcast) instead of RxOK alone.

### Driving a cartridge on the U64

Two more hardware-only facts, from issues #209, #211 and #217:

* The cartridge is invisible unless `C64 and Cartridge Settings` ->
  **`Cartridge Preference` = `External`**.  On the default `Auto` the
  cartridge does not answer the identity read, which looks exactly like
  an empty expansion port.  A `Cartridge Preference` PUT leaves a
  running 6510 alone (marker and jiffy clock intact 6/6, #217), so the
  re-PUT in `run_prg_via_sys` is safe with `reset=False` too.  `Bus Operation Mode` is irrelevant.  **Raw `$DE00`
  bytes are not a presence test from either side.**  A host-side
  `read_memory` of the I/O window returns bytes that do not depend on the
  cartridge and are not reproducible -- three observers reported three
  different patterns for the same physical cartridge under the same
  stated conditions (item 3 above).  A 6510 read is no better as a raw
  byte pattern: zeros were seen with `Preference = Auto` and with no
  cartridge, but a deselected cartridge after `client.run_prg()` gave
  `fb fb`, `06 fb`, `7c 00`, `ff ff` and `06 60` at PP `$0000` across
  runs (#217), and a working one shows the CS8900a's registers (`FF FF`
  at `$DE00/$DE01`, then PPPtr/PPData residue; measured 2026-09-04) --
  so neither "zeros" nor "non-zero" means anything.  The only valid test is the one
  ip65's `eth_init` performs (`drivers/cs8900a.s:133-137`): clockport
  enable, `PPPtr = $0000`, `PPData == $630E`, executed **on the 6510**.
* **Do not start the program with `client.run_prg()`** -- the firmware's
  runner load path deselects the external cartridge.  Isolated in #217
  (U64E, n=3 per arm, interleaved, re-PUT + reset before every arm):
  `run_prg` and `load_prg` alone both leave the cartridge absent; the
  REST `reset()` alone and host DMA writes (REST or SocketDMA) followed
  by a typed `SYS` leave it present; after a `run_prg` the deselection
  is **sticky across every `reset()`** and only a re-PUT of `Cartridge
  Preference` (same value, no reset needed) reselects it.  A deselected
  cartridge does not read as zeros reliably -- PP `$0000` came back
  `fb fb`, `06 fb`, `7c 00`, `ff ff` -- only `!= $630E` means anything.
  Use `run_prg_via_sys(target, prg)`, which writes the PRG into RAM,
  types `SYS`, and on a U64 whose preference reads `External` re-PUTs
  it first (`reselect_cartridge=False` opts out).  Stock ip65 `ping.prg`
  reports `INIT DRIVER: FAILED` under `run_prg` and pings normally under
  `run_prg_via_sys`.  Live matrix:
  `tests/test_run_prg_cartridge_visibility_live.py` (`RRNET_LIVE=1`).
* **A complete RX read releases the frame without SkipNow, but the next
  header appears only after RxEvent's high byte is read** (#219, U64E,
  n=3 per variant, two host-queued frames): after reading all RxLength
  bytes, an immediate RTDATA read gives `$0000`; reading `$DE05`
  (RxEvent high, PP `$0124`) first presents frame 2 -- zero poll
  iterations, no skip.  A partial read does not advance: RTDATA keeps
  delivering the rest of the same frame until SkipNow.  Delays of
  100 µs-10 ms, PP `$0000` reads, PPTR writes, the RxEvent *low* byte,
  `$DE00/01`, and PP `$0400` do not present it; only the high-byte read
  does -- exactly ip65's poll sequence; reading the ISQ (PP `$0120`)
  does not present it either (3/3).  The chip holds up to **three**
  100-byte frames and keeps the **newest** (8 queued -> frames 6, 7, 8
  delivered, RxMISS 5; n=2 per depth).  An earlier "buffers two, third
  dropped" reading was a leftover half-read frame behind a blind
  SkipNow drain, retracted on #219.  `_emit_read_frame` keeps its skip
  because its fixed 60-byte body read is a partial read.  Live:
  `tests/test_cs8900a_fifo_live.py` (`RRNET_LIVE=1`, `RRNET_IFACE`).
* **Resolve before the first exchange with a host: pass the ARP frame,
  or use the responder, which now answers ARP (issue #218).**  Until #218
  the harness's ping routines neither sent nor answered ARP, and macOS
  holds every reply while it has no *complete* neighbour entry for the
  C64 (its own ARP request goes unanswered and the entry sits
  `incomplete`; entry absent 0/8, entry present 8/8 -- #218 paired
  rounds; the "stale entry behind revalidation" case is inferred, not
  measured, because the `arp -S` control needs root) -- so a routine
  that only pinged got 0/8 with
  the requests visibly leaving the wire and the replies still sitting on
  the host, and 6/6 once an ARP request preceded the ping (issue #212,
  closed invalid: it was never a chip fault).  ip65 is immune because
  `icmp_ping` ARPs first and `arp_process` answers requests.  The harness
  now does the same, opt-in:
  - **Pinging:** `build_arp_request_frame(src_mac, src_ip, target_ip)`
    (60 bytes, RFC 826 at ip65's `ap_*` offsets) into RAM, then
    `build_ping_and_wait_code(..., arp_frame_buf=ADDR)` /
    `build_ping_and_wait_tod_code(..., arp_frame_buf=ADDR)` transmit it
    before the echo request in the same run and drain the ARP reply as a
    non-matching frame.  `run_ping_and_wait` (VICE-only) does this by
    default (`arp=True`), deriving the request from the echo frame's own
    MAC/IPs.
  - **Responding:** `build_icmp_responder_code` /
    `build_icmp_responder_tod_code` /
    `build_read_and_respond_echo_request_code` with `my_mac=` answer an
    ARP request for `my_ip` from the received frame in place and go back
    to waiting for the echo (`run_icmp_responder(my_mac=...)`; the
    consume routine reports `RESULT_ARP_REPLY_SENT = 0x03`).  Without
    `my_mac` -- and without `arp_frame_buf` -- every builder's output is
    byte-identical to before, so nothing sized to the old routines moves;
    with ARP on they are larger (consume 585 B, responder 630 B, TOD
    responder 754 B, ping-and-wait 319 B; the 480-byte `$C000-$C1DF`
    window does not fit an ARP-enabled responder).
  - `parse_arp(frame) -> ArpPacket | None` reads either direction back
    from a buffer or a capture.

  **Measured under VICE and on a simulated CS8900a only** so far: the
  ARP behaviour is proven by `tests/test_cs8900a_arp.py` (default suite;
  runs the emitted 6502 on `tests/cs8900a_sim.py`) and by the two-VICE
  `tests/test_bridge_arp.py`; the 0/8 -> 6/6 figure above is the only
  hardware measurement, and it was taken with a hand-built ARP frame and
  `build_tx_code`, not with these builders.  A U64E + RR-Net pass of the
  new parameters is still owed.  Pinning a static neighbour entry on the
  host remains a valid workaround for code that cannot change.

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
orchestration shape.  (UCI networking has since landed separately -- see
`docs/uci_networking.md` and `tests/test_uci_*.py`; it does not go through
`poll_until_ready`.)

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

### ip65's shipped config is not zero

ip65 ships `cfg_ip` pre-initialised to **192.168.1.64**, `cfg_netmask` to
255.255.255.0, `cfg_gateway` to 192.168.1.1 and `cfg_mac` to
`00:80:10:00:51:00` (`ip65/ip65/config.s:17-22`; only `cfg_dns` is zero).
A DHCP check written as "`cfg_ip` is non-zero" therefore passes with the
cable unplugged, and a "`cfg_mac` is non-zero" check proves nothing about
whether `eth_init` ran -- the driver programs the chip's IA from its own
table (`drivers/cs8900a.s:338-349`), and `ip65_init` copies that into
`cfg_mac` afterwards.  Assert the lease for what it must be: an unpinned
lease must fall **inside the configured DHCP pool**; a `dhcp-host`
reservation must **equal the reservation exactly** (reservations normally
sit outside the pool by design, so "inside the pool" is the wrong test for
them).  In both branches reject the shipped default **by name**, with a
message distinct from the zero case, because they mean opposite things:
`192.168.1.64` = the DHCP code never ran; `0.0.0.0` = it ran and cleared
the address.  (Found by the c64-wireguard project's red-green review.)

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
under **VICE normal mode** and **VICE warp mode** (for fast automated
test runs).  It is **VICE-only**: every orchestrator on it
(`run_ping_and_wait`, `run_icmp_responder`, `poll_until_ready`) calls
`jsr()`, which needs the binary monitor's register and checkpoint
commands; `Ultimate64Transport` has no `jsr` (issue #209).  On the U64 use
the `*_tod_code` builders below, started with `run_subroutine`.

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
  is `LDA $DE05 / AND #$0D` (`CS8900A_RXEVENT_MASK`), for a UCI response-ready bit it would
  read the UCI status register, etc.  This is the generalization
  boundary for eventual UCI support.

Zero-page footprint: `$F0`-`$F5`.  Deadline cap: **599 tenths
(59.9 s)** -- for longer waits, loop in the caller.

### Which pattern should I use?

| Scenario                                        | Use |
| ----------------------------------------------- | --- |
| Pytest test on VICE normal mode                 | Either (test path is simpler) |
| Pytest test on VICE warp mode                   | Test path (host-driven) |
| Pytest test on Ultimate 64                      | **Shippable path** (TOD builders via `run_subroutine`; `jsr` is VICE-only) |
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
