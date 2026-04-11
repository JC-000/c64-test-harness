"""Live scoping test for scripts/cleanup-bridge-networking.sh.

Empirically validates that the port-range-scoped cleanup pipeline only
touches VICE processes bound to the harness port ranges
(``6511:6531,6560:6580``) and leaves an out-of-range "sentinel" VICE
alone.  This is the real-world counterpart to the mocked unit tests in
``tests/test_cleanup_vice_ports.py``.

Opt in with ``BRIDGE_CLEANUP_LIVE=1``.  Requires ``x64sc`` on PATH and
passwordless ``sudo -n`` access (the test drives the setup/teardown
bridge scripts).  The test brings the bridge up itself (via
``scripts/setup-bridge-tap.sh``) rather than relying on it being
pre-created, because the whole point is to observe a clean setup ->
cleanup -> torn-down-state cycle.
"""
from __future__ import annotations

import glob
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

from c64_test_harness.backends.vice_lifecycle import ViceConfig, ViceProcess
from c64_test_harness.backends.vice_manager import PortAllocator


REPO_ROOT = Path(__file__).resolve().parent.parent
CLEANUP_SCRIPT = REPO_ROOT / "scripts" / "cleanup-bridge-networking.sh"
SETUP_SCRIPT = REPO_ROOT / "scripts" / "setup-bridge-tap.sh"

SENTINEL_PORT = 7031  # outside every harness range (6511-6531, 6560-6580)


pytestmark = [
    pytest.mark.skipif(
        os.environ.get("BRIDGE_CLEANUP_LIVE") != "1",
        reason="live bridge cleanup test -- opt in with BRIDGE_CLEANUP_LIVE=1",
    ),
    pytest.mark.skipif(
        shutil.which("x64sc") is None,
        reason="x64sc not on PATH",
    ),
]


# ---------------------------------------------------------------------------
# Helpers (live-test-local, intentionally not in conftest)
# ---------------------------------------------------------------------------


def _run_sudo_script(script_path: Path, *, timeout: float = 30.0) -> subprocess.CompletedProcess:
    """Run a repo bash script via ``sudo -n``.  Raises on non-zero exit."""
    cmd = ["sudo", "-n", "bash", str(script_path)]
    return subprocess.run(
        cmd, check=True, capture_output=True, text=True, timeout=timeout
    )


def _pid_alive(pid: int) -> bool:
    """True iff *pid* refers to a running (non-zombie) process.

    Note: ``os.kill(pid, 0)`` alone is insufficient because it returns
    success for zombies -- and any ``ViceProcess`` we launched in this
    pytest has us as its parent, so after the cleanup script SIGKILLs
    it the kernel keeps the PID around as a zombie until we reap it.
    We must check ``/proc/<pid>/status`` for the state to distinguish
    "still running" from "dead but not reaped yet".
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("State:"):
                    state_char = line.split()[1]
                    # R, S, D, T = running/sleeping/disk-sleep/stopped
                    # Z = zombie, X = dead
                    return state_char not in ("Z", "X")
    except OSError:
        return False
    return True


def _is_x64sc(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/comm") as f:
            return f.read().strip() == "x64sc"
    except OSError:
        return False


def _interface_exists(name: str) -> bool:
    return os.path.isdir(f"/sys/class/net/{name}")


def _read_ip_forward() -> str:
    with open("/proc/sys/net/ipv4/ip_forward") as f:
        return f.read().strip()


def _snapshot_net_state() -> dict:
    """Coarse snapshot for pre/post diff.  Only resources we control."""
    link = subprocess.run(
        ["ip", "-o", "link"], capture_output=True, text=True, check=True
    )
    interfaces = frozenset(
        line.split(":", 2)[1].strip().split("@")[0]
        for line in link.stdout.strip().splitlines()
        if ":" in line
    )
    filt = subprocess.run(
        ["sudo", "-n", "iptables", "-S"],
        capture_output=True, text=True, check=False,
    ).stdout
    nat = subprocess.run(
        ["sudo", "-n", "iptables", "-t", "nat", "-S"],
        capture_output=True, text=True, check=False,
    ).stdout
    return {
        "interfaces": interfaces,
        "iptables_filter": filt,
        "iptables_nat": nat,
        "ip_forward": _read_ip_forward(),
    }


def _best_effort_kill(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        pass


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


class TestBridgeCleanupScoping:
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
            #    (6560-6580) with RR-Net ethernet -- same pattern as the
            #    bridge_vice_pair fixture, but we skip all CS8900a/MAC
            #    init because this test is about process lifecycle, not
            #    frame exchange.
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
                    ethernet_interface=f"tap-c64-{iface_idx}",
                    ethernet_driver="tuntap",
                )
                p = ViceProcess(cfg)
                p.start()
                bridge_procs.append(p)
                time.sleep(2.0)
                bp = p.pid
                assert bp is not None
                bridge_pids.append(bp)

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
            # reaps non-blocking and lets ``_pid_alive`` (which reads
            # /proc/<pid>/status) see the dead state cleanly.
            time.sleep(0.5)
            for bp in bridge_procs:
                try:
                    if bp._proc is not None:
                        bp._proc.poll()
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

            # 5c. Bridge network resources must be gone.
            assert not _interface_exists("br-c64"), (
                "br-c64 still exists after cleanup"
            )
            assert not _interface_exists("tap-c64-0"), (
                "tap-c64-0 still exists after cleanup"
            )
            assert not _interface_exists("tap-c64-1"), (
                "tap-c64-1 still exists after cleanup"
            )

            # 5d. ip_forward must be unchanged.
            post_ip_forward = _read_ip_forward()
            assert post_ip_forward == pre_state["ip_forward"], (
                f"ip_forward changed: was {pre_state['ip_forward']}, "
                f"now {post_ip_forward}"
            )

            # 5e. /tmp/vice_eth_*.rc must be gone.
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
        assert post_state["iptables_filter"] == pre_state["iptables_filter"]
        assert post_state["iptables_nat"] == pre_state["iptables_nat"]
        assert post_state["ip_forward"] == pre_state["ip_forward"]
