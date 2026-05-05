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

### Caveats

- The Homebrew `vice` formula already passes `--enable-ethernet`, so `-ethernetcart` / `-ethernetioif` / `-ethernetiodriver` are available and `verify-dev-env.sh` reports the VICE section green.
- On macOS 26 (Tahoe), the VICE 3.10 bottle prints a cosmetic `Error - failed to retrieve executable path, falling back to getcwd() + argv[0]` on every launch. `-help`, `-features`, and normal emulator launches proceed past it and work correctly, including `-binarymonitor`. The one case that does *not* recover is `x64sc --version`, which exits 1 after the error because VICE's init-order bug hits a NULL `argv[0]` reference before the path is stashed. `verify-dev-env.sh` works around this by falling back to `brew list --versions vice`, the Cellar path, and finally a `-features` probe. File upstream if we want a real fix.
- On macOS 26 (Tahoe), launching the Homebrew `x64sc` 3.10 bottle with `-ethernetiodriver pcap -ethernetioif feth<N> -binarymonitor` additionally crashes during startup -- the process exits before the binary monitor becomes reachable, and the host raises a system crash reporter dialog for `x64sc`. Root cause is upstream and likely in the same init-order cluster as the `--version` crash above (the pcap init path runs before `archdep_program_path_set_argv0()` on this macOS release). The `tests/test_ethernet.py` fixture protects itself by importing `bridge_platform.probe_vice_pcap_ok()`, which launches a throwaway `x64sc` once per process to check whether the pcap driver survives startup; on failure the ethernet tests skip with a clear reason instead of torpedoing the suite. Override the probe with `MACOS_PCAP_DISABLED=1` (skip without probing) or `MACOS_PCAP_ENABLED=1` (trust the user without probing). Reproduce the crash interactively with `scripts/probe-vice-feth.sh`. File upstream once we have a small non-harness repro.
- `tests/test_port_lock.py` has three tests (`test_cross_process_exclusion`, `test_lock_released_on_process_exit`, `test_cleanup_does_not_break_held_lock`) that use `multiprocessing` with nested helper functions; these fail on macOS because the default multiprocessing start method is `spawn` (Linux defaults to `fork`) and nested functions can't be pickled. Non-blocking for dev work, but worth fixing if we want green CI on macOS.

## Follow-ups not in this PR

- Real fresh-VM validation of `setup-dev-env.sh` (the authoring was done via `--dry-run` only, on an already-set-up machine)
- Destructive validation pass that actually launches VICE and runs a tiny smoke test
- Distro detection so the fix hints can target more than just Ubuntu
- `--repair` mode that invokes only the stages verify-dev-env reports as missing
