#!/usr/bin/env python3
"""Port-range-scoped VICE process cleanup for emergency recovery.

Finds VICE binary-monitor TCP ports that still have listeners, resolves
each to a PID, verifies the PID belongs to x64sc, then sends SIGTERM
followed by SIGKILL after a short grace period. Only touches processes
bound to the specified port range -- never uses pkill.

Supported platforms:
    Linux -- resolves PIDs via /proc/net/tcp and verifies comm via
             /proc/<pid>/comm.
    macOS -- resolves PIDs via `lsof -nP -iTCP:<port> -sTCP:LISTEN -t`
             and verifies comm via `ps -p <pid> -o ucomm=` (accounting
             name, basename-of-argv0, 15-char cap -- same semantics as
             /proc/<pid>/comm on Linux).

This is the reference pattern for VICE lifecycle cleanup in the
c64-test-harness project. See docs/bridge_networking.md for the full
"Reference pattern for VICE agents" section and feedback_no_pkill.md
for rationale.

Usage:
    python3 scripts/cleanup_vice_ports.py --range 6511:6531,6560:6580
    python3 scripts/cleanup_vice_ports.py --range 6511:6531 --dry-run
    python3 scripts/cleanup_vice_ports.py --help

Exit codes:
    0 -- clean result (nothing to do, or all targets terminated cleanly)
    1 -- at least one target still alive after SIGKILL
    2 -- argument error / malformed range spec
    3 -- listener(s) found in range but /proc/<pid>/comm could not be
         read for ANY of them (insufficient privileges to verify the
         process is x64sc). Re-run with sudo.

Running unprivileged:
    On Linux systems where x64sc has file capabilities (cap_net_admin,
    cap_net_raw=ep), x64sc processes are non-dumpable and
    /proc/<pid>/comm returns EACCES for unprivileged callers.
    On macOS, lsof can only see sockets owned by other UIDs when run
    as root. In either case, the helper detects the condition, prints
    a warning to stderr, and exits with code 3 rather than silently
    reporting zero hits. Run with sudo (or via the platform-appropriate
    cleanup-bridge-*.sh wrapper) to signal VICE processes.
"""
from __future__ import annotations

import argparse
import os
import platform
import signal
import subprocess
import sys
import time
from typing import Iterable

IS_MACOS = platform.system() == "Darwin"
IS_LINUX = platform.system() == "Linux"

DEFAULT_COMM = "x64sc"
DEFAULT_GRACE = 2.0
DEFAULT_FALLBACK_RANGE = "6511:6531"

# Sentinel return value from kill_vice_ports() meaning: listener(s) were
# found in the requested ranges but /proc/<pid>/comm could not be read
# for ANY of them, so we could not verify whether the processes are
# x64sc. Mapped to CLI exit code 3 in main(). See module docstring.
EXIT_UNVERIFIABLE = 3


def _default_range_spec() -> str:
    """Return the default port range spec, pulling from HarnessConfig if possible."""
    try:
        from c64_test_harness.config import HarnessConfig  # type: ignore

        cfg = HarnessConfig()
        return f"{cfg.vice_port_range_start}:{cfg.vice_port_range_end}"
    except Exception:
        return DEFAULT_FALLBACK_RANGE


def parse_ranges(spec: str) -> list[tuple[int, int]]:
    """Parse a range spec like ``6511:6531`` or ``6511:6531,6560:6580``.

    Returns a list of (start, end) inclusive tuples. Raises ``ValueError``
    on malformed input.
    """
    if not spec or not spec.strip():
        raise ValueError("empty range spec")
    out: list[tuple[int, int]] = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            raise ValueError(f"empty range chunk in {spec!r}")
        if ":" not in chunk:
            raise ValueError(f"range chunk {chunk!r} missing ':'")
        lo_s, hi_s = chunk.split(":", 1)
        try:
            lo = int(lo_s)
            hi = int(hi_s)
        except ValueError as exc:
            raise ValueError(f"non-integer in range {chunk!r}: {exc}") from exc
        if lo <= 0 or hi <= 0 or lo > 65535 or hi > 65535:
            raise ValueError(f"port out of range in {chunk!r}")
        if lo > hi:
            raise ValueError(f"range {chunk!r} has start > end")
        out.append((lo, hi))
    return out


def _inline_find_pid_on_port(port: int) -> int | None:
    """Minimal fallback: parse /proc/net/tcp to find listener PID on *port*.

    Used when the c64_test_harness package cannot be imported.
    """
    try:
        with open("/proc/net/tcp", "r") as f:
            lines = f.readlines()[1:]
    except OSError:
        return None
    hex_port = f"{port:04X}"
    target_inode: str | None = None
    for line in lines:
        parts = line.split()
        if len(parts) < 10:
            continue
        local = parts[1]
        state = parts[3]
        # state 0A = LISTEN
        if state != "0A":
            continue
        if not local.endswith(":" + hex_port):
            continue
        target_inode = parts[9]
        break
    if target_inode is None or target_inode == "0":
        return None
    # Walk /proc/*/fd to find the owner.
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return None
    needle = f"socket:[{target_inode}]"
    for pid in pids:
        fd_dir = f"/proc/{pid}/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                link = os.readlink(f"{fd_dir}/{fd}")
            except OSError:
                continue
            if link == needle:
                try:
                    return int(pid)
                except ValueError:
                    return None
    return None


def _macos_find_pid_on_port(port: int) -> int | None:
    """Find the listener PID on *port* using ``lsof`` (macOS).

    Returns the first pid returned by ``lsof -nP -iTCP:<port> -sTCP:LISTEN -t``
    or ``None`` if no listener is found / lsof is unavailable.
    """
    try:
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    lines = [line.strip() for line in out.stdout.splitlines() if line.strip()]
    if not lines:
        return None
    try:
        return int(lines[0])
    except ValueError:
        return None


def _macos_comm_of(pid: int) -> str | None:
    """Return the short comm name of *pid* using ``ps`` (macOS).

    Uses ``ps -p <pid> -o ucomm=`` which returns the accounting name
    (basename of argv[0], 15-char cap) -- the same shape as Linux's
    ``/proc/<pid>/comm``. Returns ``None`` if the pid is gone or ps
    fails.
    """
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "ucomm="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    name = out.stdout.strip()
    return name or None


def _load_get_listener_pid():
    """Return a callable ``get_listener_pid(port) -> int | None``.

    On macOS uses the lsof-based resolver directly (ViceProcess.get_listener_pid
    is Linux-only and returns None on macOS). On Linux tries
    ``ViceProcess.get_listener_pid`` first, then falls back to the inline
    /proc/net/tcp parser.
    """
    if IS_MACOS:
        return _macos_find_pid_on_port
    try:
        from c64_test_harness.backends.vice_lifecycle import ViceProcess  # type: ignore

        return ViceProcess.get_listener_pid  # type: ignore[attr-defined]
    except Exception:
        return _inline_find_pid_on_port


get_listener_pid = _load_get_listener_pid()


def comm_of(pid: int) -> str | None:
    """Return the short command name of *pid*, or ``None`` if the pid is gone.

    Linux: reads ``/proc/<pid>/comm``.
    macOS: runs ``ps -p <pid> -o ucomm=``.
    """
    if IS_MACOS:
        return _macos_comm_of(pid)
    try:
        with open(f"/proc/{pid}/comm", "r") as f:
            return f.read().strip()
    except OSError:
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _iter_ports(ranges: Iterable[tuple[int, int]]) -> Iterable[int]:
    for lo, hi in ranges:
        for port in range(lo, hi + 1):
            yield port


def kill_vice_ports(
    ranges: list[tuple[int, int]],
    *,
    comm: str = DEFAULT_COMM,
    grace: float = DEFAULT_GRACE,
    dry_run: bool = False,
    quiet: bool = False,
) -> int:
    """Scoped VICE killer. Returns the number of processes still alive."""

    def log(msg: str) -> None:
        if not quiet:
            print(msg)

    # 1. Scan ranges for listeners whose comm matches.
    #
    # We track two counters here so we can detect a silent-failure
    # footgun: on this system x64sc is installed with file capabilities
    # (cap_net_admin,cap_net_raw=ep), which makes every x64sc process
    # non-dumpable. An unprivileged caller can still read /proc/net/tcp
    # (so get_listener_pid succeeds) but /proc/<pid>/comm returns
    # EACCES (so comm_of returns None and the verification gate below
    # silently rejects every candidate). Before this check landed, the
    # helper would report "no harness-bound x64sc processes found" in
    # that case -- misleading. We now detect listeners_found > 0 with
    # comm_verified == 0 and bail out with a loud warning instead.
    listeners_found = 0
    comm_verified = 0
    targets: list[tuple[int, int]] = []  # (port, pid)
    seen_pids: set[int] = set()
    for port in _iter_ports(ranges):
        pid = get_listener_pid(port)
        if pid is None:
            continue
        listeners_found += 1
        c = comm_of(pid)
        if c is None:
            continue
        comm_verified += 1
        if c != comm:
            continue
        targets.append((port, pid))
        seen_pids.add(pid)

    if not targets:
        if listeners_found > 0 and comm_verified == 0:
            # Silent-failure condition: we have candidate PIDs but no
            # way to verify them. Complain loudly on stderr and return
            # the sentinel so main() can map it to exit code 3.
            verify_source = "`ps -p <pid> -o ucomm=`" if IS_MACOS else "/proc/<pid>/comm"
            priv_note = (
                "lsof found the listener but ps could not report its ucomm "
                "(likely a permission or SIP-protected process)."
                if IS_MACOS
                else (
                    "x64sc has file capabilities that make processes "
                    "non-dumpable, so /proc/<pid>/comm returns EACCES."
                )
            )
            wrapper_script = (
                "scripts/cleanup-bridge-feth-macos.sh"
                if IS_MACOS
                else "scripts/cleanup-bridge-networking.sh"
            )
            print(
                f"[cleanup] WARNING: found {listeners_found} listener(s) in "
                "the requested range(s) but",
                file=sys.stderr,
            )
            print(
                f"[cleanup]          could not read {verify_source} for any "
                "of them.",
                file=sys.stderr,
            )
            print(
                f"[cleanup]          {priv_note}",
                file=sys.stderr,
            )
            print(
                "[cleanup]          Try:",
                file=sys.stderr,
            )
            print(
                "[cleanup]            sudo python3 scripts/cleanup_vice_ports.py ...",
                file=sys.stderr,
            )
            print(
                f"[cleanup]          Or invoke via `sudo {wrapper_script}`.",
                file=sys.stderr,
            )
            return EXIT_UNVERIFIABLE
        log("[cleanup] no harness-bound x64sc processes found")
        return 0

    # 2. SIGTERM each unique pid.
    sigtermed: set[int] = set()
    for port, pid in targets:
        if pid in sigtermed:
            continue
        if dry_run:
            log(f"[cleanup] port {port} pid {pid} ({comm}) -> SIGTERM (dry-run)")
            sigtermed.add(pid)
            continue
        log(f"[cleanup] port {port} pid {pid} ({comm}) -> SIGTERM")
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        sigtermed.add(pid)

    if dry_run:
        print(
            f"[cleanup] dry-run: would SIGTERM {len(sigtermed)} pid(s), "
            "no signals sent"
        )
        return 0

    # 3. Wait up to grace seconds, polling every 0.1s.
    deadline = time.monotonic() + grace
    remaining = set(sigtermed)
    while remaining and time.monotonic() < deadline:
        remaining = {p for p in remaining if _pid_alive(p)}
        if not remaining:
            break
        time.sleep(0.1)

    # 4. SIGKILL survivors.
    sigkilled: set[int] = set()
    for pid in list(remaining):
        # Find a port for this pid for logging
        port_for_pid = next((p for (p, q) in targets if q == pid), 0)
        log(
            f"[cleanup] port {port_for_pid} pid {pid} ({comm}) "
            "-> SIGKILL (still alive)"
        )
        try:
            os.kill(pid, signal.SIGKILL)
            sigkilled.add(pid)
        except ProcessLookupError:
            pass

    # 5. Final check after 0.2s.
    time.sleep(0.2)
    still_alive = {p for p in sigtermed if _pid_alive(p)}

    if still_alive:
        print(
            f"[cleanup] sent SIGTERM to {len(sigtermed)}, "
            f"SIGKILL to {len(sigkilled)}, "
            f"{len(still_alive)} still alive"
        )
    else:
        log(
            f"[cleanup] sent SIGTERM to {len(sigtermed)}, "
            f"SIGKILL to {len(sigkilled)}, all gone"
        )
    return len(still_alive)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Port-range-scoped VICE (x64sc) killer. Only touches processes "
            "bound to harness ports; never uses pkill."
        ),
    )
    parser.add_argument(
        "--range",
        dest="range_spec",
        default=_default_range_spec(),
        help=(
            "Port range(s) to scan. Single range 'LO:HI' or comma-separated "
            "list 'LO1:HI1,LO2:HI2'. Defaults to HarnessConfig's VICE port "
            f"range (fallback {DEFAULT_FALLBACK_RANGE})."
        ),
    )
    parser.add_argument(
        "--comm",
        default=DEFAULT_COMM,
        help=(
            "Required process short-name to match (Linux /proc/<pid>/comm or "
            f"macOS `ps -o ucomm=`). Default: {DEFAULT_COMM}"
        ),
    )
    parser.add_argument(
        "--grace-seconds",
        dest="grace",
        type=float,
        default=DEFAULT_GRACE,
        help=f"Seconds to wait after SIGTERM before SIGKILL (default: {DEFAULT_GRACE})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print intended kills without sending signals",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Only print errors and the final summary",
    )
    args = parser.parse_args(argv)

    try:
        ranges = parse_ranges(args.range_spec)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        survivors = kill_vice_ports(
            ranges,
            comm=args.comm,
            grace=args.grace,
            dry_run=args.dry_run,
            quiet=args.quiet,
        )
    except Exception as exc:  # pragma: no cover - defensive
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if survivors == EXIT_UNVERIFIABLE:
        return 3
    return 0 if survivors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
