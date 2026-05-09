# c64-test-harness

Reusable Python test harness for Commodore 64 programs. Drives the VICE emulator via its binary monitor protocol (`-binarymonitor`), with a transport abstraction that also supports real Ultimate 64 hardware. Library-only, no CLI.

## Purpose
Automate C64 program testing: load PRG files, call subroutines, set breakpoints, read/write memory, scrape screen (wrap-aware, PETSCII + screen code), inject keyboard, capture audio/video, run multiple VICE instances in parallel, and exchange ethernet frames between emulated CS8900a ethernet carts over a host bridge.

## Upstream repo
`JC-000/c64-test-harness` (private). Develop on `master`.

## Platform context
Originally developed for Ubuntu Desktop 25. The scripts in `scripts/setup-dev-env.sh` and most of `scripts/*bridge*` assume Linux (apt-get, iproute2, iptables, `/dev/net/tun`). The harness itself is portable — only a small slice of tests (5 of ~65 files) depend on Linux-specific bridge/TAP networking for CS8900a peer-to-peer ethernet tests.

## Repo layout (top level)
- `src/c64_test_harness/` — the Python package
- `tests/` — ~65 pytest files; many launch real VICE instances
- `scripts/` — dev env setup, VICE/bridge lifecycle helpers, probes, stress runners
- `docs/` — `development.md` (env setup), `bridge_networking.md` (bridge internals)
- `examples/` — small reference programs
- `pyproject.toml` — hatchling build, `src/` layout, pytest config
