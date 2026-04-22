# Development environment

This page describes the dev-environment expectations for `c64-test-harness`. Two platforms are supported: **Ubuntu Desktop 25** (primary target; one-shot `scripts/setup-dev-env.sh` installer) and **macOS** (Apple Silicon, Tahoe 26.x verified; Homebrew-based manual setup described below). The companion `scripts/verify-dev-env.sh` is the non-destructive diagnostic that works on both platforms and is called at the end of the Ubuntu installer.

## Fresh-machine install: `scripts/setup-dev-env.sh`

On a clean Ubuntu Desktop 25 machine, one command gets you from zero to "verify-dev-env.sh says READY":

```bash
./scripts/setup-dev-env.sh
```

Always preview first with `--dry-run` — it prints every action it would take without touching the system, then runs `verify-dev-env.sh` so you can see the current state:

```bash
./scripts/setup-dev-env.sh --dry-run
```

After a successful run, the harness lives in a dedicated venv at `~/.local/share/c64-test-harness/venv` (this avoids the PEP 668 `externally-managed-environment` error that Ubuntu 23+ raises against system Python). Activate it with:

```bash
source ~/.local/share/c64-test-harness/venv/bin/activate
```

or invoke the venv python directly:

```bash
~/.local/share/c64-test-harness/venv/bin/python -m pytest tests/
```

### Stages

The installer runs six stages. Every stage is **idempotent** (safe to re-run) and **opt-out** via `--no-*` flags.

| # | Stage | What it does | Opt-out flag |
|---|-------|--------------|--------------|
| 1 | `system packages` | `sudo apt-get install` build toolchain, VICE build deps (SDL2, GTK3, libpcap, pulse/alsa, flac/vorbis/mpg123/lame), harness tooling (python3, pip, iproute2, iptables). If a bulk install fails, retries per-package to report which names drifted. | `--no-system-packages` |
| 2 | `VICE 3.10 build` | Downloads the VICE 3.10 tarball from SourceForge into `~/.cache/c64-test-harness/build/`, extracts, `./configure --enable-ethernet --disable-html-docs --enable-native-gtk3ui`, `make -j$(nproc)`, `sudo make install`. Skips entirely if `x64sc --version` already reports VICE 3.10 with ethernet support. Optional `--sha256 HEX` pin. | `--no-vice` |
| 3 | `Python harness` | Creates a dedicated venv at `~/.local/share/c64-test-harness/venv` (with `--system-site-packages`) and runs `pip install -e .` into it. This avoids the PEP 668 / externally-managed-environment error that Ubuntu 23+ raises for `pip install --user` against system Python. Skipped if the venv already exists and its `c64_test_harness` import resolves to this checkout. After success, prints the `source .../activate` command to run. | `--no-harness` |
| 4 | `bridge networking` | Runs `sudo ./scripts/setup-bridge-tap.sh` to create `br-c64` + `tap-c64-0` + `tap-c64-1`. Skipped if all three interfaces already exist. | `--no-bridge` |
| 5 | `Ultimate 64 probe` | Only runs if `U64_HOST` is set in env or `--u64-host HOST` is passed. `curl`s `/v1/version` and reports reachability; never a failure. | `--no-u64` |
| 6 | `verify-dev-env.sh` | Final sanity check. The installer's exit code mirrors this: `0` READY, `1` NOT READY, `3` verify-script broken. | (always runs) |

### CLI

```
setup-dev-env.sh [OPTIONS]

  --dry-run             Print actions without executing; still runs verify
  --force               Skip the Ubuntu 25 version check
  --no-system-packages  Skip stage 1
  --no-vice             Skip stage 2
  --no-harness          Skip stage 3
  --no-bridge           Skip stage 4
  --no-u64              Skip stage 5
  --u64-host HOST       Probe this U64 host (overrides $U64_HOST)
  --build-dir DIR       VICE source cache dir (default ~/.cache/c64-test-harness/build)
  --sha256 HEX          Pin the VICE tarball checksum
  -h, --help            Print usage
```

### Recovery

If a stage fails, re-run the installer — each stage is idempotent, so it will skip anything that's already done and pick up where it left off. If a specific stage is blocking progress on an unrelated concern (e.g. the VICE build is slow and you want to iterate on the harness), skip it with the matching `--no-*` flag.

If `verify-dev-env.sh` reports NOT READY at the end, its own output lists exactly which checks failed plus fix hints.

## Quick check: `scripts/verify-dev-env.sh`

## Quick check: `scripts/verify-dev-env.sh`

```bash
./scripts/verify-dev-env.sh
```

This is a **pure read-only diagnostic**. It never launches VICE (beyond `--version` / `--help`, which exit immediately), never runs pytest, never mutates network state, and never writes outside the repo. It is safe to run while other test agents hold VICE instances open.

### What it checks

| Section | Checks |
|---------|--------|
| VICE | `x64sc` / `c1541` on `PATH`, VICE 3.10 version, `-ethernetcart` / `-ethernetioif` / `-ethernetiodriver` advertised in `--help` (this is the key deployability gate — distro-packaged VICE usually lacks `--enable-ethernet`), `-binarymonitor` and `-remotemonitor` advertised |
| Python | `python3` >= 3.10, `c64_test_harness` importable, `pytest` available |
| System tools | `ip`, `iptables`, `/dev/net/tun`, passwordless sudo (informational) |
| Bridge networking | `br-c64`, `tap-c64-0`, `tap-c64-1` interfaces present |
| Ultimate 64 (optional) | HTTP GET `/v1/version` if `U64_HOST` is set (or `--u64-host HOST` passed) and `--no-u64` is not |
| Repo | Running inside a `c64-test-harness` checkout; current git branch + short SHA |

### CLI

```
verify-dev-env.sh [--quiet] [--json] [--no-u64] [--u64-host HOST]
```

- `--quiet` — suppress section headers; print only failures plus the final summary
- `--json` — emit a single JSON object (uses `python3` for clean serialization)
- `--no-u64` — skip the Ultimate 64 probe even if `U64_HOST` is set
- `--u64-host HOST` — override `$U64_HOST`

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | READY — all critical checks passed (optional gaps like missing bridge or U64 are allowed) |
| `1` | NOT READY — at least one critical check failed (missing/wrong VICE build, Python harness broken, etc.) |
| `2` | Script error — bad argument, running outside a repo |

Critical checks are: VICE presence/version/ethernet/binary-monitor, `c1541`, Python >= 3.10, `c64_test_harness` import. Everything else (bridge, U64, bridge tools, text monitor) is reported but does not fail the overall check.

## Manual setup (if you're not on Ubuntu 25)

`scripts/setup-dev-env.sh` targets Ubuntu Desktop 25 specifically. On other distros, pass `--force` to bypass the OS check (system-package names may drift), or do it by hand:

1. **Build VICE 3.10 from source with `--enable-ethernet`** — distro packages generally omit the flag, so `verify-dev-env.sh` will flag this as a critical failure if you install from `apt`. Install to `/usr/local/bin`.
2. **Install the Python harness in editable mode into a venv**: `python3 -m venv --system-site-packages ~/.local/share/c64-test-harness/venv && ~/.local/share/c64-test-harness/venv/bin/pip install -e .` from the repo root. On Ubuntu 23+ / PEP-668 distros this is mandatory — `pip install --user` against system Python is blocked. Activate with `source ~/.local/share/c64-test-harness/venv/bin/activate` before running tests, or invoke `~/.local/share/c64-test-harness/venv/bin/python -m pytest tests/` directly.
3. **Set up bridge networking** (only required for multi-VICE ethernet tests): `sudo ./scripts/setup-bridge-tap.sh`. Teardown: `sudo ./scripts/teardown-bridge-tap.sh`. Emergency cleanup: `sudo ./scripts/cleanup-bridge-networking.sh`.
4. **Optional Ultimate 64**: set `U64_HOST=<ip>` in the environment to enable hardware-backed live tests.

Re-run `./scripts/verify-dev-env.sh` after each step to confirm progress.

## macOS (Homebrew)

The harness runs natively on macOS (Apple Silicon, Tahoe 26.x verified). Bridge networking is supported via the BSD bridge driver plus `feth` peer interfaces (see `docs/bridge_networking.md` and `tests/bridge_platform.py` for the cross-platform dispatch module). VICE attaches to `feth` via its `pcap` driver rather than `tuntap` because macOS has no `/dev/net/tun`.

Unlike the Ubuntu flow there is no one-shot installer — `scripts/setup-dev-env.sh` targets Ubuntu 25 specifically. The macOS flow is manual but short:

1. **Install Homebrew** if you don't have it yet: <https://brew.sh>.

2. **Install VICE 3.10** (ships `x64sc` and `c1541`, pre-built with `--enable-ethernet`):

   ```bash
   brew install vice
   ```

3. **Create the harness venv** at the same path Linux uses so scripts and docs stay uniform, and `pip install -e .` into it:

   ```bash
   python3 -m venv --system-site-packages ~/.local/share/c64-test-harness/venv
   ~/.local/share/c64-test-harness/venv/bin/pip install -e '.[dev]'
   source ~/.local/share/c64-test-harness/venv/bin/activate
   ```

4. **Verify** with the cross-platform checker:

   ```bash
   ./scripts/verify-dev-env.sh
   ```

   Bridge checks report as optional gaps until step 5 runs. Non-bridge tests should pass:

   ```bash
   ~/.local/share/c64-test-harness/venv/bin/python -m pytest tests/test_vice_core.py tests/test_vice_binary.py
   ```

5. **Bridge networking for ethernet tests** — creates `bridge10` + `feth0`/`feth1`, the macOS-native counterpart to `br-c64` + `tap-c64-{0,1}`:

   ```bash
   sudo ./scripts/setup-bridge-feth-macos.sh
   ```

   Teardown:

   ```bash
   sudo ./scripts/teardown-bridge-feth-macos.sh
   ```

   Emergency recovery (scoped VICE kill + bridge/feth destroy; does not touch the system `bridge0`):

   ```bash
   sudo ./scripts/cleanup-bridge-feth-macos.sh
   ```

   See [docs/bridge_networking.md](bridge_networking.md) for the full lifecycle.

6. **BPF permission for the VICE pcap driver.** VICE's `pcap` ethernet driver opens `/dev/bpf*`, which is root-only on a fresh macOS install. Grant user access via one of:

   - Install Wireshark and run its **ChmodBPF** helper (recommended — persists across reboots and is the standard Wireshark path).
   - One-shot `sudo chmod 666 /dev/bpf*` (resets on the next boot).

   Without this, VICE errors out with a `pcap_open_live` / BPF permission message when you try to attach `feth0`/`feth1`.

7. **Passwordless sudo for bridge lifecycle AND the VICE ethernet tests.** Two things on macOS need root and are driven non-interactively by the harness, so both need NOPASSWD sudoers entries:

   - **Bridge lifecycle** — setup, teardown, and cleanup of `bridge10` + `feth0`/`feth1` all require root (`ifconfig create`, `addm`, `up`, `inet` assignment). The harness invokes these three scripts from tests and CI.
   - **`x64sc` itself** — on macOS 26, VICE's `pcap` driver cannot attach to a `feth` interface without root (see the "Caveats" section below for why `chmod 666 /dev/bpf*` is not sufficient on macOS 26). `ViceProcess.start()` wraps its argv with `sudo -n` when ethernet is enabled, so any ethernet test needs `/opt/homebrew/bin/x64sc` to be runnable without a password prompt. **This is required for the ethernet suite to pass on macOS 26**; the three bridge scripts alone are not enough.

   Install a sudoers drop-in so it doesn't conflict with the main `/etc/sudoers`:

   ```bash
   sudo visudo -f /etc/sudoers.d/c64-test-harness
   ```

   Paste (substituting your username for `YOURUSER` and the repo path for `/path/to/c64-test-harness` if it lives elsewhere):

   ```
   # c64-test-harness -- passwordless sudo for bridge networking lifecycle
   # and for the VICE ethernet tests (macOS 26 requires root for pcap on feth).
   # Scoped to four binaries by absolute path; does NOT grant general sudo.
   #
   # SECURITY NOTE: the three bridge scripts live under a user-writable repo
   # path, so anyone who can write there can effectively run commands as root
   # via sudo. /opt/homebrew/bin/x64sc is owned by the Homebrew-admin user
   # (the current login on a single-user Mac), so the login user can
   # effectively run x64sc as root. Both caveats are acceptable on a
   # single-user dev workstation; do NOT deploy this entry on a shared
   # or multi-user host.

   YOURUSER ALL=(root) NOPASSWD: /path/to/c64-test-harness/scripts/setup-bridge-feth-macos.sh, \
                                 /path/to/c64-test-harness/scripts/teardown-bridge-feth-macos.sh, \
                                 /path/to/c64-test-harness/scripts/cleanup-bridge-feth-macos.sh, \
                                 /opt/homebrew/bin/x64sc
   ```

   `visudo -f` syntax-checks the file before writing — a typo won't lock you out. Verify with:

   ```bash
   sudo -n -l /path/to/c64-test-harness/scripts/setup-bridge-feth-macos.sh
   sudo -n -l /opt/homebrew/bin/x64sc
   ```

   each of which should print the NOPASSWD match instead of prompting for a password. `scripts/verify-dev-env.sh` checks each of the three bridge scripts and `/opt/homebrew/bin/x64sc` for a NOPASSWD entry and reports `warn` for any that are missing.

### Caveats

- The Homebrew `vice` formula already passes `--enable-ethernet`, so `-ethernetcart` / `-ethernetioif` / `-ethernetiodriver` are available and `verify-dev-env.sh` reports the VICE section green.
- On macOS 26 (Tahoe), the VICE 3.10 bottle prints a cosmetic `Error - failed to retrieve executable path, falling back to getcwd() + argv[0]` on every launch. `-help`, `-features`, and normal emulator launches proceed past it and work correctly, including `-binarymonitor`. The one case that does *not* recover is `x64sc --version`, which exits 1 after the error because VICE's init-order bug hits a NULL `argv[0]` reference before the path is stashed. `verify-dev-env.sh` works around this by falling back to `brew list --versions vice`, the Cellar path, and finally a `-features` probe. File upstream if we want a real fix.
- On macOS 26 (Tahoe), VICE's `pcap` ethernet driver requires **root** to attach to a `feth` interface. `/dev/bpf*` being world-readable/writable (mode 666, whether via `sudo chmod 666` or Wireshark's ChmodBPF helper) is NOT sufficient: the macOS 26 kernel enforces an additional per-process BPF-device attach check that is root-only. When a non-root `x64sc` hits that check, `pcap_open_live()` fails; VICE's error path then leaves `rawnet_arch_driver` as NULL, and the subsequent `cs8900_activate` derefs it. The observed symptom is a SIGSEGV at `rawnet_arch_pre_reset+8` inside `cs8900_activate`:

  ```
  rawnet_arch_pre_reset + 8        <- NULL deref at offset 8
    cs8900_reset
    cs8900_activate
    cs8900io_activate
    cs8900io_enable
    set_ethernetcart_enabled
    resources_read_item_from_file  (when -addconfig is used)
    resources_load
    cmdline_parse
  ```

  Earlier investigations mistook this for an upstream VICE init-order bug, because the crash looks identical to a missing-driver init race. The actual cause is the privilege check — running the same `x64sc` invocation under `sudo -n` makes everything Just Work end-to-end (pcap attaches to `feth0`, binary monitor reachable, `ETHERNETCART_ACTIVE=1` holds).

  The harness works around this automatically:

  - `ViceConfig.run_as_root: bool | None = None` auto-detects to `True` on Darwin when `ethernet=True`, and can be pinned explicitly on either platform.
  - `ViceProcess.start()` wraps the argv with `sudo -n` when `run_as_root` is true; `ViceProcess.stop()` routes signals via `sudo -n kill` for the root-owned child.
  - `tests/bridge_platform.py:probe_vice_pcap_ok()` launches its throwaway probe under `sudo -n` too, so the probe result matches what the tests will see.

  The `sudo -n` wrap is **non-interactive**: it requires a passwordless sudoers entry for `/opt/homebrew/bin/x64sc` on macOS dev hosts. See the "Passwordless sudo for bridge lifecycle" subsection above — the same sudoers drop-in that covers the three bridge scripts now also needs an `x64sc` entry. `scripts/verify-dev-env.sh` reports a `warn` row if the entry is missing.

  Escape hatches still work the same way: `MACOS_PCAP_DISABLED=1` force-skips the ethernet suite without running the probe, and `MACOS_PCAP_ENABLED=1` trusts the user and skips the probe. Reproduce the working launch interactively with `scripts/probe-vice-feth.sh` (which runs VICE under `sudo -n` by default; `--no-sudo` opts out for future hosts where ChmodBPF/TCC changes make non-root pcap work).

  Quality-of-life upstream item (not blocking): the "silent NULL driver on pcap init failure" is worth reporting to VICE — a clean error-and-exit instead of a deferred NULL deref would have shortened this debugging loop considerably. File when convenient.
- **feth peers MUST NOT also be bridge members.** `scripts/setup-bridge-feth-macos.sh` creates `feth0` + `feth1`, pairs them via `ifconfig feth0 peer feth1`, AND creates `bridge10` — but it deliberately leaves `feth0`/`feth1` OUT of the bridge (zero members on `bridge10`). Rationale: the `peer` relation is already a point-to-point L2 link (TX on `feth0` = RX on `feth1` and vice versa); adding them as bridge members creates a SECOND forwarding path between the same two nodes, which empirically broke B→A reply delivery (A→B first-hops stayed fine). `bridge10` still exists so the tests' `iface_present(BRIDGE_NAME)` precondition keeps passing and the host-side `10.0.65.1` address has a stable home; if any future test needs the host to participate at L2 it can `addm` a THIRD interface (e.g. a fresh feth pair or a vlan) but not the existing peered pair. The cleanup script `cleanup-bridge-feth-macos.sh` is a peers-only reconverge too — if a previous setup run happened to add the peers as members, the setup script now calls `deletem` idempotently to restore the intended topology.
- **libpcap over BPF self-delivers broadcasts on macOS.** `libpcap` sets `BIOCSSEESENT=1` whenever `pcap_set_promisc(1)` is on, which means the sender's own pcap handle receives its own outbound broadcast frames back as inbound. VICE's `pcap` driver feeds those back into the CS8900a RX FIFO, so after a TX phase the sender's CS8900a has its own just-sent frame queued for read. Any test that TX's then later RX's on the same transport (e.g. `test_bidirectional_exchange` in `tests/test_ethernet_bridge.py`) must drain the FIFO first or it will read the stale self-frame. `_drain_cs8900a_rx` in `tests/test_ethernet_bridge.py` is the 6502 helper that handles this; the `_build_rx_code(..., expected_src_mac=...)` src-MAC filter is the defence-in-depth layer. Unicast tests (e.g. the ICMP suite which addresses frames to the peer's MAC) are not affected because the CS8900a discards non-matching unicast under CS8900a's built-in IA filter.
- `tests/test_port_lock.py` has three tests (`test_cross_process_exclusion`, `test_lock_released_on_process_exit`, `test_cleanup_does_not_break_held_lock`) that use `multiprocessing` with nested helper functions; these fail on macOS because the default multiprocessing start method is `spawn` (Linux defaults to `fork`) and nested functions can't be pickled. Non-blocking for dev work, but worth fixing if we want green CI on macOS.

## Making the `c64-test` Claude Code skill available globally

The harness ships a Claude Code skill at `.claude/skills/c64-test/` that teaches agents how to use the Python package (see the files next to it — `SKILL.md`, `REFERENCE.md`, `PATTERNS.md`). By default the skill is only discovered when Claude Code starts inside the harness repo. Most users work across multiple C64 projects (e.g. `c64-https`, `c64-x25519`, `c64-ChaCha20-Poly1305`) and want the skill loaded in those sessions too.

The recommended install is a **user-scope symlink** pointing at the repo's copy, so the skill stays in version control in a single place but loads globally:

```bash
./scripts/install-skill.sh
```

The script is idempotent — re-running is a no-op. It creates `~/.claude/skills/c64-test` as a symlink to `<this-repo>/.claude/skills/c64-test`. Any update committed to the repo is visible to every Claude Code session on this machine the moment you pull the change.

To check without touching anything:

```bash
./scripts/install-skill.sh --dry-run
```

To remove:

```bash
./scripts/install-skill.sh --uninstall
```

The uninstaller only removes the symlink if it points at this repo's copy — it will refuse to touch a symlink someone else installed (e.g., pointing at a fork or a different checkout), and it will never touch a real file/directory.

### Why a symlink, not a copy

- **Single source of truth.** The three skill files are long; keeping N divergent copies around the filesystem is a maintenance trap.
- **Auto-update on pull.** You don't need to re-run the installer after a PR merges — the symlink resolves to whatever is on disk now.
- **Repo-side review.** Skill changes go through the harness repo's PR flow like any code change. Irrelevant projects (e.g., X68000 sessions) just don't invoke the skill because its `description` field controls when Claude fires it.

### Alternative approaches (not recommended)

- **Per-project symlink** into each C64 project's `.claude/skills/` — works, but N symlinks to maintain and each needs `.gitignore`ing since the target path is machine-specific.
- **Committed copy per project** — three copies drift instantly; hard pass.
- **Claude Code plugin + marketplace** — the "official" distribution model, but overkill for a single-user multi-repo setup. Consider if the skill grows beyond this repo's audience.

## Follow-ups not in this PR

- Real fresh-VM validation of `setup-dev-env.sh` (the authoring was done via `--dry-run` only, on an already-set-up machine)
- Destructive validation pass that actually launches VICE and runs a tiny smoke test
- Distro detection so the fix hints can target more than just Ubuntu
- `--repair` mode that invokes only the stages verify-dev-env reports as missing
