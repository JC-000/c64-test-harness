"""Live scoping test for scripts/cleanup-bridge-feth-macos.sh.

macOS counterpart to ``tests/test_cleanup_vice_ports_live.py``.
Empirically validates that the port-range-scoped cleanup pipeline only
touches VICE processes bound to the harness port ranges
(``6511:6531,6560:6580``) and leaves an out-of-range "sentinel" VICE
alone -- the same load-bearing claim, but exercising the macOS
feth/bridge10 stack via ``ifconfig`` rather than the Linux sysfs/iproute2
stack.  The Linux variant is the authoritative reference for layout;
this file is a parallel, intentionally not a refactor.

Opt in with ``BRIDGE_CLEANUP_LIVE=1``.  Requires ``x64sc`` on PATH and
passwordless ``sudo -n`` access (the test drives the setup/teardown
bridge scripts AND ``ViceConfig`` flips ``run_as_root=True`` on macOS
when ``ethernet=True``, so the launch itself is wrapped with sudo).

Probe shape vs. the Linux test:
  * /sys/class/net/<name>            -> ``ifconfig <name>`` returncode
  * /proc/<pid>/comm                 -> ``ps -p <pid> -o ucomm=``
                                        (we reuse ``_macos_comm_of``
                                         from ``scripts.cleanup_vice_ports``
                                         when importable)
  * /proc/<pid>/status zombie filter -> ``os.kill(pid, 0)`` plus a
                                        ``ViceProcess._proc.poll()``
                                        post-cleanup so the kernel
                                        reaps before we re-check
  * ip -o link                       -> ``ifconfig -l``
  * iptables snapshots               -> OMITTED (pf is not touched
                                        by the macOS scripts)
  * /proc/sys/net/ipv4/ip_forward    -> OMITTED (no sysctl is
                                        touched by the macOS scripts)
"""
from __future__ import annotations

import glob
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from c64_test_harness.backends.vice_lifecycle import ViceConfig, ViceProcess
from c64_test_harness.backends.vice_manager import PortAllocator

try:  # pragma: no cover - import shape varies with sys.path on CI
    from scripts.cleanup_vice_ports import _macos_comm_of as _imported_comm_of
except Exception:  # pragma: no cover
    _imported_comm_of = None

try:  # pragma: no cover - optional helper, gracefully degrade if missing
    from tests.bridge_platform import probe_vice_pcap_ok
except Exception:  # pragma: no cover
    try:
        from bridge_platform import probe_vice_pcap_ok  # type: ignore[no-redef]
    except Exception:
        probe_vice_pcap_ok = None  # type: ignore[assignment]


REPO_ROOT = Path(__file__).resolve().parent.parent
CLEANUP_SCRIPT = REPO_ROOT / "scripts" / "cleanup-bridge-feth-macos.sh"
SETUP_SCRIPT = REPO_ROOT / "scripts" / "setup-bridge-feth-macos.sh"

SENTINEL_PORT = 7031  # outside every harness range (6511-6531, 6560-6580)

BRIDGE_NAME = "bridge10"
FETH0 = "feth0"
FETH1 = "feth1"


def _pcap_probe_skip_reason() -> str | None:
    """Return a non-empty skip reason iff the pcap probe says VICE is broken.

    Wrapped so the skipif lambda below is cheap to evaluate when the
    helper is unavailable -- in that case we just don't add the
    fourth marker's effect.
    """
    if probe_vice_pcap_ok is None:
        return None
    try:
        ok, reason = probe_vice_pcap_ok()
    except Exception:  # pragma: no cover - probe is best-effort
        return None
    if ok:
        return None
    return reason or "VICE pcap driver is broken on this host"


pytestmark = [
    pytest.mark.skipif(
        sys.platform != "darwin",
        reason=(
            "Uses ifconfig, ifconfig -l, and ps -o ucomm= to snapshot host "
            "network/process state -- macOS-only.  The Linux counterpart "
            "lives in tests/test_cleanup_vice_ports_live.py and uses /proc, "
            "/sys/class/net, ip link, and iptables instead."
        ),
    ),
    pytest.mark.skipif(
        os.environ.get("BRIDGE_CLEANUP_LIVE") != "1",
        reason="live bridge cleanup test -- opt in with BRIDGE_CLEANUP_LIVE=1",
    ),
    pytest.mark.skipif(
        shutil.which("x64sc") is None,
        reason="x64sc not on PATH",
    ),
    # NOTE: we deliberately do NOT add a pcap-probe skipif here.  The
    # probe in tests/bridge_platform.py gates on a pre-existing feth
    # interface, but this test brings the bridge up itself in step 1.
    # Running the probe before setup would skip every clean-host run.
    # If pcap is genuinely broken, the test will fail loudly at the
    # bridge VICE spawn step, which is the right signal.
]


# ---------------------------------------------------------------------------
# Helpers (live-test-local, intentionally not in conftest)
# ---------------------------------------------------------------------------


def _run_sudo_script(script_path: Path, *, timeout: float = 30.0) -> subprocess.CompletedProcess:
    """Run a repo script via ``sudo -n``.  Raises on non-zero exit.

    Invokes the script directly (relying on its shebang) rather than
    wrapping it in ``bash``.  NOPASSWD sudoers entries are typically
    scoped to the exact program path, so ``sudo -n /path/script.sh``
    matches but ``sudo -n bash /path/script.sh`` does not -- the latter
    asks sudo to run ``bash``, which is a different program.
    """
    cmd = ["sudo", "-n", str(script_path)]
    return subprocess.run(
        cmd, check=True, capture_output=True, text=True, timeout=timeout
    )


def _macos_comm_of(pid: int) -> str | None:
    """Short comm name of *pid* via ``ps -p <pid> -o ucomm=``.

    Prefers the helper exported by ``scripts/cleanup_vice_ports.py``
    (so test and production agree on parsing rules); falls back to an
    inline subprocess call if that import was unavailable at module
    load time (e.g. ``scripts`` not on ``sys.path``).
    """
    if _imported_comm_of is not None:
        try:
            return _imported_comm_of(pid)
        except Exception:
            pass
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "ucomm="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    name = out.stdout.strip()
    return name or None


def _pid_alive(pid: int) -> bool:
    """True iff *pid* refers to a running (non-zombie) process.

    Zombie handling: any ``ViceProcess`` we launched in this pytest has
    pytest as its parent, so after the cleanup script SIGKILLs it the
    kernel keeps the PID around as a zombie until we ``waitpid()`` it.
    macOS has no ``/proc/<pid>/status``; we use ``ps -p <pid> -o stat=``
    instead -- BSD-style ps reports the process state as e.g. ``S+``,
    ``R+``, or (for zombies) ``Z+``.  A leading ``Z`` means dead-but-
    not-reaped, which we treat as not alive.

    Note: ``ps -o ucomm=`` is NOT a zombie indicator on macOS -- the
    accounting name is preserved through the zombie state, so an
    earlier version of this helper that used ``comm == ''`` as a
    zombie signal silently reported zombies as alive.  ``stat=`` is
    the right field.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Different uid (e.g. we sudo'd VICE) -- the kernel still
        # answered, so the pid exists.  Fall through to the stat
        # check, which can read other-uid processes' state.
        pass
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        # Couldn't ask -- the kernel said the pid exists; trust that.
        return True
    if out.returncode != 0:
        # ps refused to report on this pid -- it's gone.
        return False
    stat = out.stdout.strip()
    if not stat:
        return False
    return stat[0] != "Z"


def _is_x64sc(pid: int) -> bool:
    return _macos_comm_of(pid) == "x64sc"


def _interface_exists(name: str) -> bool:
    return (
        subprocess.run(
            ["ifconfig", name],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


def _snapshot_net_state() -> dict:
    """Coarse snapshot for pre/post diff.  Only resources we control.

    We deliberately do NOT snapshot pf (the macOS cleanup script does
    not touch pf) or any sysctl (likewise untouched).  Capturing them
    just adds noise that varies with unrelated host activity.
    """
    out = subprocess.run(
        ["ifconfig", "-l"], capture_output=True, text=True, check=True
    )
    interfaces = frozenset(out.stdout.split())
    return {"interfaces": interfaces}


def _best_effort_kill(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        pass


def _resolve_x64sc_child(
    parent_pid: int, *, attempts: int = 6, delay: float = 0.5
) -> int | None:
    """Find an ``x64sc`` descendant of *parent_pid*.

    When ``ViceConfig.ethernet=True`` on macOS, ``ViceProcess`` flips
    ``run_as_root=True`` and wraps the launch in ``sudo -n``, so the
    Popen we record is the **sudo wrapper**.  Its ``pid`` returns
    ``"sudo"`` from ``ps -o ucomm=`` and would fail an ``_is_x64sc``
    sanity check.  Walk ``pgrep -P`` to find the x64sc child; sudo
    fork+execs the target so it should be a direct child, but we
    retry briefly to absorb spawn races (the caller already sleeps
    before calling us, but pgrep can still miss the child by a hair).
    """
    for _ in range(attempts):
        try:
            out = subprocess.run(
                ["pgrep", "-P", str(parent_pid), "x64sc"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        for line in out.stdout.splitlines():
            line = line.strip()
            if line.isdigit():
                return int(line)
        time.sleep(delay)
    return None


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


class TestBridgeCleanupScopingMacos:
    def test_scoped_cleanup_preserves_out_of_range_vice(self):
        # Ensure the port we're about to use for the sentinel is free.
        import socket as _sock
        probe = _sock.socket()
        try:
            rc = probe.connect_ex(("127.0.0.1", SENTINEL_PORT))
        finally:
            probe.close()
        if rc == 0:
            pytest.skip(f"sentinel port {SENTINEL_PORT} already in use")

        pre_state = _snapshot_net_state()

        # 1. Bring the bridge up.
        _run_sudo_script(SETUP_SCRIPT)

        sentinel_vice: ViceProcess | None = None
        sentinel_pid: int | None = None
        bridge_procs: list[ViceProcess] = []
        bridge_pids: list[int] = []
        cleanup_ran = False
        allocator: PortAllocator | None = None
        bridge_ports: list[int] = []

        try:
            # 2. Sentinel VICE on an out-of-range port, no ethernet.
            sentinel_config = ViceConfig(
                port=SENTINEL_PORT,
                warp=False,
                sound=False,
                minimize=True,
            )
            sentinel_vice = ViceProcess(sentinel_config)
            sentinel_vice.start()
            time.sleep(2.0)
            sentinel_pid = sentinel_vice.pid
            assert sentinel_pid is not None, "sentinel has no pid"
            assert _pid_alive(sentinel_pid), "sentinel failed to start"
            assert _is_x64sc(sentinel_pid), (
                f"sentinel PID {sentinel_pid} comm is not x64sc"
            )

            # 3. Two bridge VICE instances on harness bridge ports
            #    (6560-6580) attached to the feth pair via VICE's
            #    pcap driver.  ViceConfig auto-elevates this launch
            #    on macOS (run_as_root=True when ethernet=True), so
            #    the spawn itself goes through ``sudo -n`` and
            #    ``ViceProcess.pid`` is the sudo wrapper.  We resolve
            #    and record the actual ``x64sc`` child PID so the
            #    pre/post-cleanup ``_is_x64sc`` and ``_pid_alive``
            #    checks evaluate against the real VICE process.
            allocator = PortAllocator(
                port_range_start=6560, port_range_end=6580
            )
            for iface_idx in (0, 1):
                port = allocator.allocate()
                res = allocator.take_socket(port)
                if res is not None:
                    res.close()
                bridge_ports.append(port)
                cfg = ViceConfig(
                    port=port,
                    warp=False,
                    sound=False,
                    minimize=True,
                    ethernet=True,
                    ethernet_mode="rrnet",
                    ethernet_interface=f"feth{iface_idx}",
                    ethernet_driver="pcap",
                )
                p = ViceProcess(cfg)
                p.start()
                bridge_procs.append(p)
                time.sleep(2.0)
                sudo_wrapper_pid = p.pid
                assert sudo_wrapper_pid is not None
                x64sc_pid = _resolve_x64sc_child(sudo_wrapper_pid)
                assert x64sc_pid is not None, (
                    f"could not resolve x64sc child of sudo wrapper "
                    f"PID {sudo_wrapper_pid} (port {port})"
                )
                bridge_pids.append(x64sc_pid)

            # All three should be alive before cleanup.
            for pid in [sentinel_pid, *bridge_pids]:
                assert _pid_alive(pid), f"PID {pid} not alive pre-cleanup"
                assert _is_x64sc(pid), f"PID {pid} is not x64sc pre-cleanup"

            # 4. Run the real cleanup script.  This is the core action.
            _run_sudo_script(CLEANUP_SCRIPT, timeout=60.0)
            cleanup_ran = True
            # Settle + reap.  The cleanup script SIGKILLed the bridge
            # VICEs, but since pytest is their parent, their PIDs stay
            # as zombies until we waitpid() them.  Calling Popen.poll()
            # reaps non-blocking; combined with the ``ps -o ucomm=``
            # empty-comm fallback in ``_pid_alive`` this gives a clean
            # dead-vs-alive signal on macOS.
            time.sleep(0.5)
            for bp_proc in bridge_procs:
                try:
                    if bp_proc._proc is not None:
                        bp_proc._proc.poll()
                except Exception:
                    pass
            # Reap the sentinel too: if it was killed by an in-range
            # mis-scoping (the failure mode this test is designed to
            # catch), it's now a zombie under pytest's parentage and
            # ``_pid_alive``'s ``os.kill(pid, 0)`` would still succeed.
            # Polling forces a non-blocking ``waitpid`` so the next
            # liveness check sees ``ProcessLookupError`` cleanly.
            try:
                if sentinel_vice is not None and sentinel_vice._proc is not None:
                    sentinel_vice._proc.poll()
            except Exception:
                pass

            # 5a. Bridge VICE PIDs must be dead.
            for pid in bridge_pids:
                assert not _pid_alive(pid), (
                    f"bridge VICE PID {pid} still alive after cleanup -- "
                    "scoping or kill failed"
                )

            # 5b. Sentinel VICE PID must still be alive.  THE key proof.
            assert _pid_alive(sentinel_pid), (
                f"sentinel VICE PID {sentinel_pid} was killed by cleanup -- "
                "scoping FAILED; cleanup bled outside its intended range"
            )
            assert _is_x64sc(sentinel_pid), (
                f"sentinel PID {sentinel_pid} no longer looks like x64sc"
            )

            # 5c. Bridge network resources must be gone.  Only check the
            #     three interfaces the macOS scripts own; bridge0/en0/etc.
            #     are system interfaces and out of scope.
            assert not _interface_exists(BRIDGE_NAME), (
                f"{BRIDGE_NAME} still exists after cleanup"
            )
            assert not _interface_exists(FETH0), (
                f"{FETH0} still exists after cleanup"
            )
            assert not _interface_exists(FETH1), (
                f"{FETH1} still exists after cleanup"
            )

            # 5d. /tmp/vice_eth_*.rc must be gone.
            leftover_rc = glob.glob("/tmp/vice_eth_*.rc")
            assert not leftover_rc, f"stale vicerc files: {leftover_rc}"

        finally:
            # Kill the sentinel manually -- cleanup correctly left it
            # alive, but we don't want it lingering.
            if sentinel_pid is not None and _pid_alive(sentinel_pid):
                _best_effort_kill(sentinel_pid)
                # Reap via ViceProcess handle if we still own it.
                if sentinel_vice is not None and sentinel_vice._proc is not None:
                    try:
                        sentinel_vice._proc.wait(timeout=5)
                    except Exception:
                        pass

            # Release any bridge_procs -- processes should already be
            # dead from the cleanup, but close handles cleanly.
            for p in bridge_procs:
                try:
                    if p._proc is not None and p._proc.poll() is None:
                        p._proc.kill()
                        p._proc.wait(timeout=5)
                except Exception:
                    pass

            # Defensive final cleanup so next test starts clean.  Swallow
            # errors -- assertions above have already recorded truth.
            try:
                _run_sudo_script(CLEANUP_SCRIPT, timeout=60.0)
                cleanup_ran = True
            except Exception:
                pass

            # Release allocator ports (harmless if already freed).
            if allocator is not None:
                for port in bridge_ports:
                    try:
                        allocator.release(port)
                    except Exception:
                        pass

        assert cleanup_ran, "cleanup script never completed"

        # 6. Coarse diff: final state should match baseline.
        post_state = _snapshot_net_state()
        new_ifaces = post_state["interfaces"] - pre_state["interfaces"]
        gone_ifaces = pre_state["interfaces"] - post_state["interfaces"]
        assert not new_ifaces and not gone_ifaces, (
            f"interfaces diverged: new={new_ifaces} gone={gone_ifaces}"
        )
