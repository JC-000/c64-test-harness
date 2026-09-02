"""Detecting a live BPF capture must work against a *root-owned* VICE.

This is the test that settles issue #144.

``probe_vice_pcap_ok()`` demands a ``/dev/bpf*`` attach as proof that
VICE is really capturing, rather than merely emulating CS8900a registers
while no host packet moves.  That demand is right.  The instrument was
not: it shelled out to ``lsof -nP -p <pid>`` **unelevated**, and an
unprivileged ``lsof`` cannot read a root-owned process's file-descriptor
table at all.  Measured here: against a root x64sc it returns *zero
lines total* — not zero ``bpf`` lines, zero lines.

On macOS every pcap ethernet launch elevates (``archdep_rawnet_capability()``
is ``geteuid() == 0``; an unelevated launch SIGSEGVs rather than
degrading).  So the probe always elevated, ``_bpf_fds()`` always returned
``[]``, and the probe always concluded "attached no /dev/bpf* device".
Its own diagnostic named that the Homebrew-build signature, and issue
#144 was written from it.  The probe was reporting its own permission
failure as a property of the emulator.

What an elevated VICE actually does, measured with ``netstat -B``::

    bpf1  ap1    p---IO------  x64sc.4326
    bpf2  feth0  p---IO------  x64sc.4326

Two descriptors, one bound to the requested interface, in promiscuous
mode — and the count of ``/dev/bpf*`` nodes this process could still open
dropped by exactly two.  Capture works when elevated.

``netstat -B`` is the correct instrument: it reports the device, the
bound interface and the owning command, it needs no privilege, and it
reads root-owned processes.
"""

from __future__ import annotations

import contextlib
import os
import platform
import shutil
import socket
import subprocess
import tempfile
import time

import pytest

from bridge_platform import bpf_attached_interfaces

IFACE = "feth0"
X64SC = "/opt/homebrew/bin/x64sc"

pytestmark = [
    pytest.mark.skipif(
        platform.system() != "Darwin", reason="BPF/netstat -B probe is macOS-only"
    ),
    pytest.mark.skipif(
        not os.path.exists(X64SC),
        reason="needs the sudoers-listed /opt/homebrew/bin/x64sc",
    ),
]


def _iface_up(name: str) -> bool:
    out = subprocess.run(["ifconfig", name], capture_output=True, text=True)
    return out.returncode == 0


# Deliberately the production function rather than a local probe.
#
# The local probe this replaced ran ``sudo -n -l -- <binary>`` and took
# rc=0 as "NOPASSWD".  On a machine whose sudoers carries ``(ALL) ALL``
# that is rc=0 for *anything that exists*, so it was a file-existence
# check wearing a sudo costume.  Measured here: ``/bin/ls`` and
# ``/usr/sbin/lsof`` both "authorised", as was the ethernet build that
# has no rule at all.
#
# That is the same defect ``vice_elevation.sudo_can_run`` was corrected
# for -- it parses the ``NOPASSWD:`` entries out of the listing -- so the
# fixed and broken versions were coexisting in one branch.  Importing it
# keeps one implementation of the question, and means a future fix to it
# reaches this gate too.
from c64_test_harness.backends.vice_elevation import sudo_can_run


requires_bench = pytest.mark.skipif(
    not shutil.which("netstat")
    or not _iface_up(IFACE)
    or not sudo_can_run(X64SC),
    reason=(
        f"needs {IFACE} up (scripts/setup-bridge-feth-macos.sh) and a "
        f"NOPASSWD sudoers rule for {X64SC}"
    ),
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextlib.contextmanager
def _elevated_vice(*, ethernet: bool):
    """Run a root-owned x64sc, with or without the ethernet cart.

    Both arms of the attachment control need an identical launch that
    differs *only* in whether a BPF device gets bound, so the launch lives
    here rather than being written out twice and drifting.
    """
    rc_path = None
    args = ["sudo", "-n", X64SC, "-console", "-default"]
    if ethernet:
        rc_path = tempfile.mktemp(prefix="vice_eth_", suffix=".rc", dir="/tmp")
        with open(rc_path, "w") as f:
            f.write(
                "[Version]\nConfigVersion=3.10\n\n[C64SC]\n"
                "ETHERNETCART_ACTIVE=1\nEthernetCartMode=1\n"
                f'ETHERNET_INTERFACE="{IFACE}"\nETHERNET_DRIVER="pcap"\n'
                "SaveResourcesOnExit=0\n"
            )
        args += ["-addconfig", rc_path]
    port = _free_port()
    args += [
        "-binarymonitor", "-binarymonitoraddress", f"ip4://127.0.0.1:{port}",
        "+sound", "-warp", "-limitcycles", "200000000",
    ]
    wrapper = subprocess.Popen(
        args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    child: int | None = None
    try:
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if wrapper.poll() is not None:
                pytest.fail(f"elevated VICE exited rc={wrapper.returncode}")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.25)
        else:
            pytest.fail("elevated VICE never opened its binary monitor")

        # Resolve from OUR OWN sudo wrapper.  Never `pgrep -n x64sc`:
        # other projects on this bench run x64sc concurrently, and -n
        # takes the newest match, which could be someone else's emulator.
        out = subprocess.run(
            ["pgrep", "-P", str(wrapper.pid), "x64sc"],
            capture_output=True, text=True, timeout=5,
        )
        pids = [int(p) for p in out.stdout.split()]
        assert pids, "could not resolve the x64sc child of our sudo wrapper"
        child = pids[0]

        owner = subprocess.run(
            ["ps", "-o", "user=", "-p", str(child)],
            capture_output=True, text=True,
        ).stdout.strip()
        assert owner == "root", f"expected a root-owned child, got {owner!r}"
        yield child
    finally:
        if child is not None:
            subprocess.run(
                ["sudo", "-n", "kill", "-TERM", str(child)],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        try:
            wrapper.terminate()
            wrapper.wait(timeout=8)
        except Exception:
            wrapper.kill()
        if rc_path and os.path.exists(rc_path):
            os.unlink(rc_path)


@requires_bench
def test_attach_is_detected_for_a_root_owned_vice():
    """An elevated VICE on *IFACE* must be seen as attached.

    The launch is the real one: cart active, pcap driver, elevated.  The
    ``lsof`` implementation cannot pass this — it sees nothing at all of
    a root process — while ``netstat -B`` reports the bound interface.
    """
    with _elevated_vice(ethernet=True) as child:
        attached = bpf_attached_interfaces(child)
        assert IFACE in attached, (
            f"elevated VICE (pid {child}) reported as attached to "
            f"{attached!r}; expected {IFACE!r}"
        )


@requires_bench
def test_no_attach_reported_for_a_root_owned_vice_without_the_cart():
    """The control that varies attachment while holding privilege fixed.

    The old negative control used ``os.getpid()`` — this process, which
    is user-owned *and* unattached — against a positive that is root
    *and* attached.  Privilege and attachment moved together, so the pair
    only ever sampled two diagonal corners of a 2x2 and neither test
    could say which variable the instrument was responding to.  That
    matters here more than usual, because the bug this module exists to
    refute was an instrument responding to *privilege* (unelevated
    ``lsof`` reading nothing of a root process) while appearing to report
    on attachment.

    This is the same root-owned launch as the test above with the cart
    left off, so the only thing that changes is whether a BPF device is
    bound.
    """
    with _elevated_vice(ethernet=False) as child:
        attached = bpf_attached_interfaces(child)
        assert attached == [], (
            f"a root-owned VICE with no ethernet cart reported BPF "
            f"attachments {attached!r} — the probe is responding to "
            f"something other than attachment"
        )


def test_no_attach_reported_for_a_process_that_holds_none():
    """Cheap negative control: a process with no BPF descriptor reports none.

    Kept because it needs no bench and no emulator, so it still runs
    where ``requires_bench`` skips.  It cannot separate privilege from
    attachment on its own — that is what the root-owned pair above is
    for.
    """
    assert bpf_attached_interfaces(os.getpid()) == []
