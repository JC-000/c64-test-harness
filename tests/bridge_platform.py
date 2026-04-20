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
import time

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

    x64sc = shutil.which("x64sc")
    if x64sc is None:
        _PROBE_CACHE = (False, "x64sc not on PATH")
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

    port = _probe_port()
    args = [
        x64sc,
        "-binarymonitor",
        "-binarymonitoraddress", f"ip4://127.0.0.1:{port}",
        "-ethernetiodriver", "pcap",
        "-ethernetioif", iface,
        "+sound",
        "-minimized",
        "-default",
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
        deadline = time.monotonic() + timeout
        monitor_up = False
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                # Exited during startup -- pcap is broken on this host.
                break
            if _wait_for_tcp("127.0.0.1", port, min(deadline, time.monotonic() + 0.5)):
                monitor_up = True
                break
        if monitor_up:
            _PROBE_CACHE = (
                True,
                f"VICE pcap+{iface} reached the binary monitor",
            )
        elif proc.poll() is not None:
            _PROBE_CACHE = (
                False,
                (
                    f"VICE (x64sc) exited during pcap+{iface} startup "
                    f"(code={proc.returncode}); pcap driver is broken on "
                    "this host.  See docs/development.md macOS caveats "
                    "and scripts/probe-vice-feth.sh for a deeper probe."
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
        # Always clean up, even if we decide pcap is OK -- the probe
        # process is throwaway either way.
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

    return _PROBE_CACHE
