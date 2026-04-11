# Development environment

This page describes the dev-environment expectations for `c64-test-harness`. A full installer is planned (see the deployability-automation roadmap); for now, the `scripts/verify-dev-env.sh` helper lets you confirm what's present and what's missing.

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

## Setting up from scratch

A proper installer is on the roadmap (targeting Ubuntu Desktop 25). Until then, the high-level steps for a fresh Ubuntu 25 box are:

1. **Build VICE 3.10 from source with `--enable-ethernet`** — distro packages generally omit the flag, so `verify-dev-env.sh` will flag this as a critical failure if you install from `apt`. Install to `/usr/local/bin`.
2. **Install the Python harness in editable mode**: `pip install -e '.[dev]'` from the repo root.
3. **Set up bridge networking** (only required for multi-VICE ethernet tests): `sudo ./scripts/setup-bridge-tap.sh`. Teardown: `sudo ./scripts/teardown-bridge-tap.sh`. Emergency cleanup: `sudo ./scripts/cleanup-bridge-networking.sh`.
4. **Optional Ultimate 64**: set `U64_HOST=<ip>` in the environment to enable hardware-backed live tests.

Re-run `./scripts/verify-dev-env.sh` after each step to confirm progress.

## Follow-ups not in this PR

- Automated installer that fixes what `verify-dev-env.sh` reports missing
- Destructive validation pass that actually launches VICE and runs a tiny smoke test
- Distro detection so the fix hints can target more than just Ubuntu
- `--repair` mode that invokes the installer for just the missing pieces
