# CLAUDE.md

Standing brief for AI agents working in this repository. Goal: short pointers, not exhaustive docs. When something here gets long, move it to `docs/` and leave a pointer.

## What this repo is

Python test harness for Commodore 64 assembly programs. Tests run against either:
- **VICE** emulator (`x64sc` from the Homebrew/Linux build, driven via the binary monitor on TCP)
- **Ultimate 64** real hardware (driven via UCI/HTTP)

Backend selection is unified through `c64_test_harness.UnifiedManager`. See the `c64-test` skill (`.claude/skills/c64-test/SKILL.md` + `PATTERNS.md` + `REFERENCE.md`) for the canonical testing patterns — read them before authoring new tests.

## Operational facts you cannot derive from the code

These bite agents who skip recon:

- **Canonical venv lives outside the repo** at `~/.local/share/c64-test-harness/venv/` — created by `scripts/setup-dev-env.sh`. Do not create a new venv inside the repo or a worktree; use the existing path. Pytest is at `~/.local/share/c64-test-harness/venv/bin/pytest`.
- **Live tests are opt-in** via environment variables. The most common gates:
  - `BRIDGE_CLEANUP_LIVE=1` — the macOS/Linux bridge cleanup live tests
  - Other live-test gates are documented in `docs/bridge_networking.md` and the live test files themselves
  - When unset, live tests skip cleanly. Treat unset as the default state.
- **Sudoers are configured for direct script invocation only**, not `bash <script>` wrapping. See `docs/bridge_networking.md` § "macOS test-author traps" for the full breakdown. Short version: helpers that shell out to `sudo -n` must invoke scripts via their shebang, not via `bash`.

## Tests as reference patterns

When in doubt about how to structure a test:

- `tests/test_cleanup_vice_ports_live.py` (Linux) and `tests/test_cleanup_vice_ports_macos_live.py` (macOS) are the paired reference for live tests that mutate host network state. They follow the same skeleton with platform-specific probes (sysfs/iptables on Linux, `ifconfig`/`ps` on macOS).
- `scripts/cleanup_vice_ports.py` is the cross-platform reference for the VICE-process-cleanup pattern. New cleanup helpers should match its shape (port-scoped, comm-verified, SIGTERM-then-SIGKILL).

## Platform-specific test gotchas (macOS)

Three traps agents hit when porting Linux tests to macOS — full details with code snippets in `docs/bridge_networking.md` § "macOS test-author traps":

1. `subprocess.run(["sudo", "-n", "bash", script])` fails because NOPASSWD matches the program (sudo's first non-flag argv), and `bash` is not in the allowlist. Drop the `bash` wrapper.
2. `ViceConfig(ethernet=True)` auto-elevates the launch via sudo. `ViceProcess.pid` is the sudo wrapper, not x64sc. Resolve the actual child via `pgrep -P <sudo_pid> x64sc`.
3. macOS BSD `ps -o ucomm=` keeps the comm name on zombies. Use `ps -o stat=` and check for leading `Z` to detect dead-but-not-reaped processes.

## Permission notes for agents using the harness

- `Bash(python3 *)` is broadly allowlisted in local settings. When setting an env var prefix breaks an allowlist match (e.g., `Bash(.../pytest *)` does not match `BRIDGE_CLEANUP_LIVE=1 .../pytest …`), wrap the call in `python3 -c "import os, sys, subprocess; os.environ['VAR']='1'; sys.exit(subprocess.call([…]))"`.
- The sudo'd setup/teardown/cleanup scripts are allowlisted at their canonical repo paths. They are NOT allowlisted at worktree paths under `.claude/worktrees/agent-*/scripts/…` — live network tests must run from the canonical repo.
- Spawned subagents under `mode: bypassPermissions` may still be denied file mutations to the canonical repo (Edit/Write/Bash cp). When that happens, the supervising agent applies file edits directly and resumes the subagent for command execution. Authoring of new test logic still belongs to subagents; mechanical fixes the supervisor diagnosed are fine to apply directly.

## Destructive U64E endpoints and the poweroff guard

The `Ultimate64Client` exposes the full `/v1/machine:*` family. All are marked DESTRUCTIVE in the docstrings, but they are NOT all equally recoverable:

| Endpoint | Recovers via | Guard |
|---|---|---|
| `reset()` | instant, over the wire | none |
| `reboot()` | ~8s, over the wire | none |
| `pause()` / `resume()` | over the wire | none |
| `menu_button()` | over the wire | none |
| **`poweroff()`** | **physical power-cycle only** | **`confirm_irrecoverable=True` kwarg required** |

`poweroff()` is the special case: after the call, the device drops off the network entirely (no ICMP, no TCP) and the API cannot bring it back. Multiple agents have called it thinking it was a benign reset, then misdiagnosed the unreachable state as a "hung device" — wasting troubleshooting cycles.

**Default behavior** when an agent (or its caller) invokes `client.poweroff()`: raises `Ultimate64UnsafeOperationError`. To actually power the device off, the caller must pass `client.poweroff(confirm_irrecoverable=True)` AND have physical access to power-cycle it later.

**For "device looks stuck, I want to recover" scenarios, use `reboot()`** — it reinitializes the FPGA fully (recovers REU/DMA stuck state) and the device comes back in ~8s, all over the network. `reset()` is finer-grained: 6510-only, leaves FPGA state.

If you find yourself reaching for `poweroff()` to "make sure it's really off", you almost certainly want `reboot()` instead.

## Things to skip

- Do not run `scripts/setup-dev-env.sh` end-to-end unless the user asked for a fresh-machine setup. It installs system packages and brings up bridges; it is not a "make the venv work" shortcut.
- Do not create CLAUDE.md-style memory or planning documents in the repo unless asked. Per-session memory belongs in `~/.claude/projects/-Users-someone-Documents-c64-test-harness/memory/`.
- Do not commit `.claude/worktrees/` content. The `.gitignore` already excludes it; if `git status` shows worktree paths as untracked, the gitignore is wrong, not the worktree.
