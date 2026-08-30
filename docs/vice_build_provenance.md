# VICE source provenance — the audit's citation target

Produced by phase 0 of the VICE backend contract audit (2026-08-30).

Its purpose is narrow and durable: **to identify which VICE source tree every
`file:line` citation in this audit refers to**, and to let anyone re-obtain that
exact tree to check one. If a later reader questions a citation like
`src/arch/shared/rawnetarch.c:251`, this document is what tells them where to
look and how to reproduce the bytes.

Every factual claim carries one of three marks:

- **S** — a `path:line` citation in the pinned source tree. Openable.
- **T** — a transcript: a command and its actual output, reproducible by re-running it.
- **U** — explicitly unverified.

Recorded on macOS 26.6.2 (build 25G83), Apple Silicon (arm64), Homebrew 6.0.20.

---

## The S-citation target

> **Source tree: `/Users/someone/Documents/vice-src/3.10/`**

Every `S` mark in this audit is a path **relative to that directory**. Do not
cite a different tree.

The tree is **vanilla**: no patches applied. In particular Homebrew's
`patch :DATA` (a backport adding `#include <mach-o/dyld.h>` to
`src/arch/shared/macOS-launcher.c`) was not applied, and proved unnecessary — a
build from this tree completed without it (T).

---

## Release pinned

| | |
|---|---|
| Version | **VICE 3.10** |
| Released | 24 December 2025 |
| Tarball | `https://sourceforge.net/projects/vice-emu/files/releases/vice-3.10.tar.gz/download` |
| Size | 14 366 175 bytes |
| SHA-256 | `8e5bac18cbcb9f192380ad3ef881f8790f5b75c41d7b3da65d831985d864d6d1` |
| Local archive | `/Users/someone/Documents/vice-src/vice-3.10.tar.gz` |

**How this was established as the latest stable (T).** The upstream project page
`https://vice-emu.sourceforge.io/` states `(24 Dezember 2025) Version 3.10
released` and links `vice-3.10.tar.gz` as the current source download. The
SourceForge release directory contains no tarball newer than `vice-3.10.tar.gz`
(present: 2.3, 2.4, 3.0–3.10). The GitHub mirror `VICE-Team/svn-mirror` carries
only ad-hoc revision tags (`r46203`…`r46223`), which upstream labels "ad hoc
build … use this when reporting bugs" — snapshots, not stable releases.

**Checksum corroboration (T).** The SHA-256 above matches the `sha256` recorded
for the same URL in the Homebrew core formula
(`https://raw.githubusercontent.com/Homebrew/homebrew-core/master/Formula/v/vice.rb`)
— two independent sources agreeing on the tarball bytes.

3.10 is also the version Homebrew has installed, so citations into this tree
describe the binary the harness actually runs.

---

## Re-obtaining the tree

To check a citation, the tarball and an extract are sufficient — no build needed:

```sh
mkdir -p ~/Documents/vice-src && cd ~/Documents/vice-src
curl -L -o vice-3.10.tar.gz \
  "https://sourceforge.net/projects/vice-emu/files/releases/vice-3.10.tar.gz/download"
shasum -a 256 vice-3.10.tar.gz
# expect: 8e5bac18cbcb9f192380ad3ef881f8790f5b75c41d7b3da65d831985d864d6d1
tar xzf vice-3.10.tar.gz && mv vice-3.10 3.10 && cd 3.10
```

If a binary is genuinely needed (it is not, for citation checking — see
"Reference binary" below), this tree builds with the shipped `configure`:

```sh
./configure --prefix=<somewhere> \
  --disable-arch --disable-pdf-docs --enable-gtk3ui --enable-midi \
  --enable-ethernet --enable-cpuhistory \
  --with-flac --with-vorbis --with-gif --with-png \
  --disable-debug --disable-dependency-tracking
make -j"$(sysctl -n hw.ncpu)" && make install
```

Notes for anyone repeating that: `autoconf`/`automake`/`texinfo` are **not**
needed, because the shipped `configure` is used instead of `./autogen.sh`.
`libpcap` comes from the macOS SDK (`/usr/lib/libpcap.A.dylib`), not Homebrew.
`--enable-lame` appears in the Homebrew formula but is **not a valid 3.10
option** — configure emits `WARNING: invalid option: --enable-lame` (T) and it
is omitted above. Nothing had to be installed on this machine to build the tree.

---

## Reference binary

**The harness targets `/opt/homebrew/bin/x64sc`** (→
`/opt/homebrew/Cellar/vice/3.10/bin/x64sc`).

The audit established that the Homebrew bottle is fully ethernet-capable, so
there is nothing for a private build to add. Evidence (T), read off the bottle
itself:

- Its embedded configure string contains `--enable-ethernet`:
  `--disable-arch --disable-pdf-docs --enable-gtk3ui --enable-midi --enable-lame
  --enable-ethernet --enable-cpuhistory --with-flac --with-vorbis --with-gif
  --with-png --disable-debug --disable-dependency-tracking
  --prefix=/opt/homebrew/Cellar/vice/3.10 --libdir=…`
- `otool -L` shows `/usr/lib/libpcap.A.dylib` linked.
- `x64sc -features` reports `HAVE_RAWNET yes`, `HAVE_PCAP yes`,
  `HAVE_NETWORK yes`, `HAVE_TUNTAP no`.
- It behaves identically to a from-source `--enable-ethernet` build across every
  ethernet invocation tested (finding 2 below).

**Ethernet capability is now probed, not configured.**
`resolve_vice_executable()` probes `-features` on whatever binary it resolves, so
the `PATH` x64sc is verified rather than assumed. `VICE_ETHERNET_BIN`
(equivalently `HarnessConfig.vice_ethernet_executable`, TOML
`[vice] ethernet_executable`) is an **override, not a requirement — normally
leave it unset.**

**U (reported by `elevation-design`, not verified here):** only
`/opt/homebrew/bin/x64sc` carries a NOPASSWD sudoers rule on this bench, so an
elevated launch aimed at any other path would stop at a password prompt. This
document does not verify it — reading sudoers requires root; all that was
observed directly is that `sudo -n true` returns `sudo: a password is required`
(T).

A from-source `--enable-ethernet` build made during this phase still exists at
`~/.local/opt/vice-3.10-ethernet/`. It is **not maintained and not the harness
target**; treat it as scratch, not as configuration.

---

## Flag availability in 3.10

The flags the harness passes are not UI-toolkit-gated.

| Flag | Evidence | Gate |
|---|---|---|
| `-console` | **S** `src/initcmdline.c:421`, `src/main.c:268` | `#ifndef BEOS_COMPILE` only — present in every non-BeOS build regardless of toolkit |
| `-binarymonitor`, `-binarymonitoraddress` | **S** `src/monitor/monitor_binary.c:2086,2092` | inside `#ifdef HAVE_NETWORK` (opens `:72`, closes `:2168`) — needs network support, not a particular UI |
| `-ethernetcart`, `+ethernetcart`, `-tfe`, `-rrnet`, `-ethernetcartmode` | **S** `src/c64/cart/ethernetcart.c:437,440,443,446,449` | `--enable-ethernet` |
| `-ethernetcartbase` | **S** `src/c64/cart/ethernetcart.c:457` | `--enable-ethernet` |
| `-ethernetioif` | **S** `src/c64/cart/cs8900io.c:365` | `--enable-ethernet` |
| `-ethernetiodriver` | **S** `src/arch/shared/rawnetarch.c:193` | `--enable-ethernet` |

All confirmed present in `x64sc -help` at runtime (T).

**U:** whether an SDL2 or headless (`--enable-headlessui`) build changes any of
this was not tested. The source gates suggest not, for these particular flags,
but no such build was produced.

---

## Findings about VICE 3.10

These are properties of the pinned release, established with S/T evidence, and
verified to hold for **both** the Homebrew bottle and a from-source build. They
are not properties of any one binary.

### 1. On macOS the pcap rawnet driver requires euid 0 — `/dev/bpf*` permissions are irrelevant

`archdep_rawnet_capability()` tests only `geteuid() == 0`, plus Linux
`CAP_NET_RAW` under `#ifdef HAVE_CAPABILITIES` (**S**
`src/arch/shared/archdep_rawnet_capability.c:86` for the function, `:91` for the
euid check). macOS has no `HAVE_CAPABILITIES`, so it reduces to the euid test and
otherwise falls through to `return false`. **It never inspects `/dev/bpf*`.**

There is no macOS path to a working rawnet driver unelevated. Only two drivers
exist — the sole non-NULL assignments to `rawnet_arch_driver` are **S**
`src/arch/shared/rawnetarch.c:109` (pcap) and `:114` (tuntap):

- **tuntap** needs `HAVE_TUNTAP`, which configure defines only on finding the
  Linux-only header `linux/if_tun.h` (**S** `configure.ac:2504-2508`).
- **pcap** is gated by the capability call at every entry point: explicit
  selection (**S** `rawnetarch.c:108`), default selection (`:164`), driver
  enumeration (`:425`, which actively strips pcap from the list), and
  `rawnet_arch_get_standard_driver()` (`:469`).

Confirmed (T). `/dev/bpf0` on this machine is `crw----rw- root wheel` — world
read/write, i.e. user-openable — and yet as uid 501:

```
$ x64sc -console -default -ethernetiodriver pcap -limitcycles 200000
Argument 'pcap' not valid for option `-ethernetiodriver'.
Error parsing command-line options, bailing out. For help use '-help'
$ echo $?
255
```

This refutes any launcher heuristic that treats a user-openable `/dev/bpf*` as
grounds to skip elevation.

**U:** root-side behaviour was never observed. `sudo -n true` returns `sudo: a
password is required`, so no elevated run could be made non-interactively. Issue
#144's claim that capture silently fails *even as root* therefore remains
unsettled, and points the opposite way from this source reading.

### 2. Enabling the ethernet cart unelevated segfaults — upstream, not a packaging defect

Each invocation `x64sc -console -default -sounddev dummy <FLAGS> -limitcycles
200000`, as uid 501, unelevated:

```
flags under test                       exit   log lines
-------------------------------------------------------
(none — baseline)                         1        68
-ethernetcart                           139         0   SIGSEGV
-rrnet                                  139         0   SIGSEGV
-tfe                                    139         0   SIGSEGV
-ethernetioif en0                         1        69
-ethernetiodriver pcap                  255         3
-ethernetcart -rrnet -ethernetioif en0  139         0   SIGSEGV
```

**Identical results from the Homebrew bottle and a from-source
`--enable-ethernet` build, across all seven cases** (T). Exit 1 on the
non-crashing cases is the documented `-limitcycles` termination.

Symbolicated trace from `~/Library/Logs/DiagnosticReports/x64sc-*.ips` (T) —
`EXC_BAD_ACCESS (SIGSEGV)`, `KERN_INVALID_ADDRESS at 0x0000000000000008`:

```
rawnet_arch_pre_reset <- cs8900_reset <- cs8900_activate <- cs8900io_activate
  <- cs8900io_enable <- set_ethernetcart_enabled <- resources_set_value_internal
  <- cmdline_parse <- initcmdline_check_args <- main_program <- main
```

Root cause (**S** `src/arch/shared/rawnetarch.c:245-252`):

```c
void rawnet_arch_pre_reset(void)
{
    rawnet_arch_driver->pre_reset();      /* :251 — no NULL check */
}
```

`rawnet_arch_driver` is NULL whenever the driver is `"none"` (**S** `:103`),
which per finding 1 is the macOS unelevated default. Faulting address `0x8` is
the offset of the `pre_reset` member in the driver vtable. It happens during
command-line parsing, which is why the log is empty. Note `rawnet_arch_activate()`
at `:270` *does* NULL-check and returns -1 — `pre_reset` is the one that does not.

### 3. Binary monitor: two unsolicited events fire on every monitor *entry*

Not per connection, and not once per boot.

Emission path (**S**): `monitor_startup()` → `mon_event_opened()`
(`src/monitor/monitor.c:3151`) → `monitor_binary_event_opened()`
(`src/monitor/mon_util.c:179`, guarded by `#ifdef HAVE_NETWORK` and
`monitor_is_binary()`), which emits **register info then stopped** in that order
(`src/monitor/monitor_binary.c:489-492`). The mirror `monitor_close()` →
`mon_event_closed()` (`src/monitor/monitor.c:3286`) emits `RESUMED` (`0x63`)
(`src/monitor/monitor_binary.c:496-497`).

`0x31` is `e_MON_RESPONSE_REGISTER_INFO`, `0x62` is `e_MON_RESPONSE_STOPPED`
(**S** `src/monitor/monitor_binary.c:142,151`); both carry the sentinel request
id `0xffffffff` = `MON_EVENT_ID` (**S** `:292`).

The trigger is **first data from the client, not the TCP accept**:
`monitor_binary_data_available()` accepts a pending connection but leaves
`available` at 0 on that branch, so `monitor_check_binary()` calls
`monitor_startup_trap()` only once bytes arrive (**S** `:263-286`).

Confirmed (T), one emulator instance, two sequential connections:

```
CONNECTION 1
  accept-only: NOTHING received (accept does not emit)
  after 1st command: REGISTER_INFO(id=EVENT) -> STOPPED(id=EVENT) -> PING_REPLY(id=0xaaaa1111)
  after 2nd command (same connection): PING_REPLY(id=0xaaaa2222)
CONNECTION 2 (reconnect to same running emulator)
  after 1st command: REGISTER_INFO(id=EVENT) -> STOPPED(id=EVENT) -> PING_REPLY(id=0xbbbb1111)
  after 2nd command: PING_REPLY(id=0xbbbb2222)
```

Connecting alone emits nothing; the first command that re-enters the monitor is
preceded by two events; commands issued while already stopped are not. A client
must not treat the events as a once-per-connection preamble it can consume and
forget — any command after the emulator resumes will be preceded by them again.
The only sound rule is to dispatch on `request_id`: `0xffffffff` means
unsolicited event, anything else is the reply to that id. A client reading
exactly one framed message per request will desynchronise, intermittently,
depending on whether the emulator happened to be stopped.

### 4. `failed to retrieve executable path` warning is benign

Both binaries print `Error - failed to retrieve executable path, falling back to
getcwd() + argv[0]` at startup — `proc_pidpath()` fails (**S**
`src/arch/shared/archdep_program_path.c:204`). Cosmetic here: the correct
`share/vice/` ROM directory still resolves, verified for absolute-path *and*
bare-name-via-`PATH` invocation (T).
