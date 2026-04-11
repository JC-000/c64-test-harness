# Development environment

This page describes the dev-environment expectations for `c64-test-harness` and how to bring a fresh Ubuntu Desktop 25 box online using `scripts/setup-dev-env.sh`. The companion `scripts/verify-dev-env.sh` is the non-destructive diagnostic that the installer calls at the end.

## Fresh-machine install: `scripts/setup-dev-env.sh`

On a clean Ubuntu Desktop 25 machine, one command gets you from zero to "verify-dev-env.sh says READY":

```bash
./scripts/setup-dev-env.sh
```

Always preview first with `--dry-run` — it prints every action it would take without touching the system, then runs `verify-dev-env.sh` so you can see the current state:

```bash
./scripts/setup-dev-env.sh --dry-run
```

### Stages

The installer runs six stages. Every stage is **idempotent** (safe to re-run) and **opt-out** via `--no-*` flags.

| # | Stage | What it does | Opt-out flag |
|---|-------|--------------|--------------|
| 1 | `system packages` | `sudo apt-get install` build toolchain, VICE build deps (SDL2, GTK3, libpcap, pulse/alsa, flac/vorbis/mpg123/lame), harness tooling (python3, pip, iproute2, iptables). If a bulk install fails, retries per-package to report which names drifted. | `--no-system-packages` |
| 2 | `VICE 3.10 build` | Downloads the VICE 3.10 tarball from SourceForge into `~/.cache/c64-test-harness/build/`, extracts, `./configure --enable-ethernet --disable-html-docs --enable-native-gtk3ui`, `make -j$(nproc)`, `sudo make install`. Skips entirely if `x64sc --version` already reports VICE 3.10 with ethernet support. Optional `--sha256 HEX` pin. | `--no-vice` |
| 3 | `Python harness` | `pip install --user -e '.[dev]'` from the repo root. Skipped if the currently-importable `c64_test_harness` already points at this checkout. | `--no-harness` |
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
2. **Install the Python harness in editable mode**: `pip install -e '.[dev]'` from the repo root.
3. **Set up bridge networking** (only required for multi-VICE ethernet tests): `sudo ./scripts/setup-bridge-tap.sh`. Teardown: `sudo ./scripts/teardown-bridge-tap.sh`. Emergency cleanup: `sudo ./scripts/cleanup-bridge-networking.sh`.
4. **Optional Ultimate 64**: set `U64_HOST=<ip>` in the environment to enable hardware-backed live tests.

Re-run `./scripts/verify-dev-env.sh` after each step to confirm progress.

## Follow-ups not in this PR

- Real fresh-VM validation of `setup-dev-env.sh` (the authoring was done via `--dry-run` only, on an already-set-up machine)
- Destructive validation pass that actually launches VICE and runs a tiny smoke test
- Distro detection so the fix hints can target more than just Ubuntu
- `--repair` mode that invokes only the stages verify-dev-env reports as missing
