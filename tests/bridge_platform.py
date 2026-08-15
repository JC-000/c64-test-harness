"""Platform-specific bridge networking constants for the ethernet test suite.

On Linux, the ethernet tests expect ``tap-c64-0``/``tap-c64-1`` TAP devices
bridged via ``br-c64`` (set up by ``scripts/setup-bridge-tap.sh``), and VICE
attaches via its ``tuntap`` driver.

On macOS, no ``/dev/net/tun`` exists and there is no iproute2. The equivalent
layout is ``feth0``/``feth1`` pseudo-ethernet peers bridged via ``bridge10``
(set up by ``scripts/setup-bridge-feth-macos.sh``), and VICE attaches via its
``pcap`` driver. The feth interfaces are BPF-visible, so ``libpcap`` can open
them once ``/dev/bpf*`` is user-readable (via Wireshark's ChmodBPF helper or
a one-shot ``sudo chmod 666 /dev/bpf*``).

This module is imported by the test fixtures so the tests remain platform-
portable without duplicating OS dispatch everywhere.  It also hosts the
cached ``probe_vice_pcap_ok()`` helper used to skip the pcap-driver tests
cleanly on macOS hosts where the VICE 3.10 Homebrew bottle crashes at
startup with the pcap driver attached (see the function docstring).
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

from c64_test_harness.backends.vice_lifecycle import (
    ETHERNET_VICE_BIN_ENV,
    bpf_capture_available,
    ethernet_vice_binary,
)

# ---------------------------------------------------------------------------
# Bridge IP space — reserved for the harness's own ethernet tests
# ---------------------------------------------------------------------------
#
# The harness owns this /24: the setup scripts put the host at ``.1``
# (``BRIDGE_ADDR`` in setup-bridge-feth-macos.sh / setup-tap-networking.sh)
# and the two emulated C64s answer on ``.2`` and ``.3``.
#
# These were previously duplicated as literals across five test modules,
# which made the reservation invisible -- a consumer rig built on top of
# the harness's bridge (c64-https) ran a DHCP pool of ``.2-.10`` over
# exactly the addresses the tests hardcode, and moved the host address to
# a different interface while keeping ``.1``. Nothing detected the clash.
#
# Override the whole range with ``C64_BRIDGE_SUBNET`` (first three octets)
# when the harness has to coexist with a rig that already owns 10.0.65/24.
# Consumers wanting their own services on the harness bridge should stay
# clear of ``.1``-``.3`` and use ``.100`` upward.
BRIDGE_SUBNET = os.environ.get("C64_BRIDGE_SUBNET", "10.0.65").strip().rstrip(".")


def bridge_ip(host_octet: int) -> bytes:
    """4-byte IP in the harness's reserved bridge range."""
    octets = [int(part) for part in BRIDGE_SUBNET.split(".")]
    if len(octets) != 3:
        raise ValueError(
            f"C64_BRIDGE_SUBNET must be three octets (e.g. '10.0.65'), "
            f"got {BRIDGE_SUBNET!r}"
        )
    return bytes(octets + [host_octet])


def bridge_ip_str(host_octet: int) -> str:
    """Dotted-quad form of :func:`bridge_ip`."""
    return ".".join(str(b) for b in bridge_ip(host_octet))


#: Host side of the bridge (assigned by the setup scripts).
BRIDGE_HOST_IP = bridge_ip(1)
#: First emulated C64.
BRIDGE_IP_A = bridge_ip(2)
#: Second emulated C64.
BRIDGE_IP_B = bridge_ip(3)


if sys.platform == "darwin":
    ETHERNET_DRIVER = "pcap"
    IFACE_A = "feth0"
    IFACE_B = "feth1"
    BRIDGE_NAME = "bridge10"
    SETUP_HINT = "run sudo scripts/setup-bridge-feth-macos.sh"

    def iface_present(name: str) -> bool:
        return (
            subprocess.run(
                ["ifconfig", name],
                capture_output=True,
                check=False,
            ).returncode
            == 0
        )

    def first_available_ethernet_iface() -> str | None:
        """Return the first feth* interface present, or None."""
        try:
            out = subprocess.run(
                ["ifconfig", "-l"], capture_output=True, check=False, text=True
            ).stdout
        except OSError:
            return None
        for name in out.split():
            if name.startswith("feth"):
                return name
        return None
else:
    ETHERNET_DRIVER = "tuntap"
    IFACE_A = "tap-c64-0"
    IFACE_B = "tap-c64-1"
    BRIDGE_NAME = "br-c64"
    SETUP_HINT = "run sudo scripts/setup-bridge-tap.sh"

    def iface_present(name: str) -> bool:
        return os.path.isdir(f"/sys/class/net/{name}")

    def first_available_ethernet_iface() -> str | None:
        """Return the first tap-* interface present, or None."""
        try:
            for iface in os.listdir("/sys/class/net"):
                if iface.startswith("tap"):
                    return iface
        except OSError:
            pass
        return None


# ---------------------------------------------------------------------------
# macOS-only: probe whether VICE's pcap driver survives startup
# ---------------------------------------------------------------------------
#
# On macOS 26 Tahoe the Homebrew VICE 3.10 bottle crashes immediately when
# launched with ``-ethernetiodriver pcap -ethernetioif feth<N>``, producing
# a system crash dialog and exiting before the binary monitor becomes
# usable.  The root cause is upstream (likely the same cluster of init-order
# bugs that also breaks ``x64sc --version``; see docs/development.md macOS
# caveats).  Rather than gate every ethernet run behind an opt-in env var
# (which means running the suite on a fresh machine has to wade through a
# crash dialog to learn to set the env var), we actively probe once per
# process: launch VICE in a throwaway mode, watch for either the binary
# monitor accepting a TCP connection (probe succeeded) or the process
# exiting within a short window (probe failed, pcap is broken here).
#
# The probe is cached at module level so the real fixture launch is never
# preceded by a redundant extra VICE invocation during a single pytest run.


_PROBE_CACHE: tuple[bool, str] | None = None


def _probe_port() -> int:
    """Pick an unused high TCP port on loopback for the probe.

    Bind + getsockname + close is the standard portable way to ask the
    kernel for a free ephemeral port.  Race-prone in theory, harmless in
    practice for a ~3s probe on loopback.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


def _wait_for_tcp(host: str, port: int, deadline: float) -> bool:
    """Return True once *port* accepts a TCP connection, False at *deadline*."""
    while time.monotonic() < deadline:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.25)
        try:
            s.connect((host, port))
            s.close()
            return True
        except OSError:
            try:
                s.close()
            except OSError:
                pass
            time.sleep(0.1)
    return False


def _x64sc_pid(proc: subprocess.Popen, elevated: bool) -> int | None:
    """Resolve the real x64sc PID (``proc`` is the sudo wrapper when elevated)."""
    if not elevated:
        return proc.pid
    try:
        out = subprocess.run(
            ["pgrep", "-P", str(proc.pid), "x64sc"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in out.stdout.split():
        try:
            return int(line)
        except ValueError:
            continue
    return None


def _bpf_fds(pid: int) -> list[str]:
    """``/dev/bpf*`` nodes held open by *pid* — the proof of a live capture."""
    try:
        out = subprocess.run(
            ["lsof", "-nP", "-p", str(pid)],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [
        line.split()[-1]
        for line in out.stdout.splitlines()
        if "/dev/bpf" in line
    ]


def probe_vice_pcap_ok(
    iface: str | None = None,
    timeout: float = 3.0,
) -> tuple[bool, str]:
    """Actively probe whether VICE's pcap driver works on this host.

    Launches a short-lived ``x64sc`` with ``-ethernetiodriver pcap
    -ethernetioif <iface> -binarymonitor`` on a throwaway port, then either
    (a) observes the binary monitor accept a TCP connection (ok) or
    (b) observes the process exit within *timeout* seconds (broken).  The
    child is always cleaned up with ``SIGTERM`` followed by ``SIGKILL``;
    stdout/stderr are swallowed so the crash reporter has no visible
    terminal to write to.

    Returns ``(ok, reason)`` where *reason* is a human-readable string
    suitable for a ``pytest.mark.skipif`` message.  The result is cached
    in a module-level variable, so it is safe (and cheap) to call many
    times per process.

    Env overrides (both Darwin-only, checked before launching anything):
      * ``MACOS_PCAP_DISABLED=1`` -- skip the probe, return (False, ...).
        Use this on hosts where you already know pcap is broken and want
        to avoid the second startup.
      * ``MACOS_PCAP_ENABLED=1``  -- skip the probe, return (True, ...).
        Use this on hosts where you know pcap works and want to avoid
        paying ~3s for the probe on every test session.

    On non-Darwin platforms this is always ``(True, "non-darwin")`` --
    Linux uses the ``tuntap`` driver, which has its own failure modes but
    no known crash-on-startup pattern.
    """
    global _PROBE_CACHE
    if _PROBE_CACHE is not None:
        return _PROBE_CACHE

    if sys.platform != "darwin":
        _PROBE_CACHE = (True, "non-darwin (pcap probe not applicable)")
        return _PROBE_CACHE

    if os.environ.get("MACOS_PCAP_DISABLED") == "1":
        _PROBE_CACHE = (
            False,
            "MACOS_PCAP_DISABLED=1 (probe skipped by env override)",
        )
        return _PROBE_CACHE
    if os.environ.get("MACOS_PCAP_ENABLED") == "1":
        _PROBE_CACHE = (
            True,
            "MACOS_PCAP_ENABLED=1 (probe skipped by env override)",
        )
        return _PROBE_CACHE

    # Probe the binary the ethernet tests will actually launch: an
    # ethernet-capable build when one is configured ($VICE_ETHERNET_BIN),
    # otherwise the PATH x64sc.  Probing PATH while the tests run a
    # different binary would report on the wrong emulator entirely.
    x64sc = ethernet_vice_binary() or shutil.which("x64sc")
    if x64sc is None:
        _PROBE_CACHE = (False, "x64sc not on PATH")
        return _PROBE_CACHE
    if not os.access(x64sc, os.X_OK):
        _PROBE_CACHE = (
            False,
            f"{x64sc} (from ${ETHERNET_VICE_BIN_ENV}) is not executable",
        )
        return _PROBE_CACHE

    if iface is None:
        iface = first_available_ethernet_iface()
    if iface is None:
        _PROBE_CACHE = (False, f"no feth* interface present ({SETUP_HINT})")
        return _PROBE_CACHE

    # Precondition: the interface must be UP. If it's down, VICE's pcap
    # init will fail -- and on macOS 26 the failure path has been observed
    # to trigger the system crash reporter (spurious dialog for the user).
    # A quick ifconfig scrape lets us short-circuit to a clean skip before
    # launching VICE at all. "UP" appears in the flags= line of ifconfig
    # output on both macOS (BSD ifconfig) and Linux (iproute2 ifconfig
    # compat).
    try:
        ifconfig_out = subprocess.run(
            ["ifconfig", iface],
            capture_output=True,
            check=False,
            text=True,
        ).stdout
    except OSError:
        ifconfig_out = ""
    if "<UP" not in ifconfig_out and "UP," not in ifconfig_out:
        _PROBE_CACHE = (
            False,
            (
                f"{iface} exists but is not UP (ifconfig flags lack UP); "
                f"pcap would fail and macOS may show a crash dialog. "
                f"{SETUP_HINT} to bring it up, or run "
                f"'sudo ifconfig {iface} up' for a minimal ad-hoc fix."
            ),
        )
        return _PROBE_CACHE

    # Build the same ``-addconfig`` vicerc + CLI flag combination that
    # ``ViceProcess`` uses in production (see
    # ``src/c64_test_harness/backends/vice_lifecycle.py``).  This is the only
    # invocation VICE 3.10 accepts for ethernet activation, so anything else
    # would probe a flag pattern VICE rejects unconditionally and would
    # tell us nothing about the pcap driver's actual health.
    #
    # Why NOT ``-ethernetiodriver pcap`` on a bare cmdline: the option is
    # advertised in ``-help`` but its value set is populated by
    # ``rawnet_arch_init()``, which the Homebrew build only runs when the
    # cart is activated.  At parse time with no ``-addconfig`` loaded, the
    # driver list is empty and VICE rejects ``pcap`` with
    # ``Argument 'pcap' not valid``.  So the probe MUST go through
    # ``-addconfig``.
    rc_body = (
        "[Version]\nConfigVersion=3.10\n\n"
        "[C64SC]\n"
        "ETHERNETCART_ACTIVE=1\n"
        "EthernetCartMode=1\n"
        f'EthernetIOIF="{iface}"\n'
        'EthernetIODriver="pcap"\n'
        "SaveResourcesOnExit=0\n"
    )
    fd, rc_path = tempfile.mkstemp(prefix="probe_pcap_", suffix=".rc")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(rc_body)

        port = _probe_port()
        # Elevate on exactly the same rule production uses, so the probe
        # measures the configuration the tests will actually run under.
        # ``/dev/bpf*`` is root-only by default; rigs that open it up
        # (``chmod o+rw``) let VICE capture unprivileged, verified by a
        # non-root build attaching /dev/bpf1+2 on feth0.
        elevated = not bpf_capture_available()
        args = (["sudo", "-n"] if elevated else []) + [
            x64sc,
            "-addconfig", rc_path,
            "-ethernetioif", iface,
            "-ethernetiodriver", "pcap",
            "-binarymonitor",
            "-binarymonitoraddress", f"ip4://127.0.0.1:{port}",
            "+sound",
            "-minimized",
        ]

        try:
            proc = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
        except OSError as e:
            _PROBE_CACHE = (False, f"could not spawn x64sc: {e}")
            return _PROBE_CACHE

        try:
            # The crash mode on broken hosts (VICE 3.10 Homebrew bottle on
            # macOS 26) is SIGSEGV inside ``cs8900_activate`` during the
            # ``-addconfig`` rc-file load, which happens before the binary
            # monitor socket is opened.  So ``proc.poll() != None`` very
            # shortly after spawn is the signature of a broken host; a host
            # that works surfaces the monitor within ~1-2s.
            deadline = time.monotonic() + timeout
            monitor_up = False
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    break
                if _wait_for_tcp(
                    "127.0.0.1", port, min(deadline, time.monotonic() + 0.5)
                ):
                    monitor_up = True
                    break
            if monitor_up:
                # Reaching the monitor is necessary but NOT sufficient. A
                # VICE whose rawnet driver never attached still emulates the
                # CS8900 registers, so register-level assertions (product
                # ID, TxCMD readback) pass while zero host packets move --
                # a silently vacuous ethernet suite. The Homebrew 3.10
                # bottle does exactly this when launched as root: alive,
                # monitor up, and no /dev/bpf* handle. Demand the BPF
                # attach as the proof of real capture.
                bpf_pid = _x64sc_pid(proc, elevated)
                held = _bpf_fds(bpf_pid) if bpf_pid else []
                if held:
                    _PROBE_CACHE = (
                        True,
                        f"VICE pcap+{iface} attached {', '.join(held)}",
                    )
                else:
                    _PROBE_CACHE = (
                        False,
                        (
                            f"VICE reached the binary monitor on {iface} but "
                            "attached no /dev/bpf* device, so it captures "
                            "nothing -- ethernet tests would pass vacuously "
                            "against emulated CS8900 registers with no host "
                            "traffic. This is the Homebrew x64sc signature "
                            "(see issue #144); point the tests at an "
                            "ethernet-enabled build."
                        ),
                    )
            elif proc.poll() is not None:
                _PROBE_CACHE = (
                    False,
                    (
                        f"VICE (x64sc) exited during pcap+{iface} cart "
                        f"activation (code={proc.returncode}); pcap driver "
                        "is broken on this host.  Root cause: the "
                        "EthernetIODriver resource setter does not populate "
                        "rawnet_arch_driver before cs8900_activate runs, so "
                        "cs8900_activate segfaults on a NULL driver vtable. "
                        "See docs/development.md macOS caveats and "
                        "scripts/probe-vice-feth.sh for a standalone probe."
                    ),
                )
            else:
                _PROBE_CACHE = (
                    False,
                    (
                        f"VICE pcap+{iface} did not open its binary monitor "
                        f"within {timeout:.1f}s; assuming pcap is broken here."
                    ),
                )
        finally:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        pass
            except OSError:
                pass
    finally:
        try:
            os.unlink(rc_path)
        except OSError:
            pass

    return _PROBE_CACHE
