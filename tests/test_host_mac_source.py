"""``host_mac`` must never hand back the redaction placeholder.

Red/green for :func:`bridge_platform.host_mac` (issue #444).  macOS
redacts the ``ether`` line to ``02:00:00:00:00:00`` for a caller that is
not entitled to see hardware identifiers, and a frame addressed to that
placeholder reaches nobody -- the host never replies and the miss looks
exactly like a dead link or an absent cartridge.

Every branch is driven by a faked ``subprocess.run``: no device, no VICE,
no network.  ``monkeypatch`` restores the patch, because a leaked patch on
a module-scope helper is the #385 shape.
"""
from __future__ import annotations

import platform
import subprocess
from types import SimpleNamespace

import pytest

import bridge_platform as bp

REAL = "c0:56:27:b1:16:38"
IFCONFIG_REDACTED = f"\tether {bp.REDACTED_MAC}\n\tinet 169.254.234.68 netmask 0xffff0000\n"
IFCONFIG_REAL = f"\tether {REAL}\n\tinet 169.254.234.68 netmask 0xffff0000\n"
NS_OK = f"Ethernet Address: {REAL} (Device: en4)\n"
NS_UPPER = f"Ethernet Address: {REAL.upper()} (Device: en4)\n"
NS_REDACTED = f"Ethernet Address: {bp.REDACTED_MAC} (Device: en4)\n"
NS_UNRESOLVABLE = "** Error: The parameters were not valid.\n"


def _no_skip(fn, *args, **kwargs):
    """Call *fn*; an unexpected ``skip`` here is a failure, not a pass.

    Without this, a regression that makes the helper skip instead of
    returning the real MAC leaves these tests green -- the vacuous-pass
    shape that let two mutants survive the first battery.
    """
    try:
        return fn(*args, **kwargs)
    except pytest.skip.Exception as exc:  # pragma: no cover - failure path
        pytest.fail(f"unexpected skip: {exc}")


@pytest.fixture
def fake_run(monkeypatch):
    """Install a faked ``subprocess.run``; returns the recorded argv list."""
    calls: list[list[str]] = []

    def install(*, networksetup="", ifconfig="", ip=""):
        def _run(cmd, **kwargs):
            calls.append(list(cmd))
            if "networksetup" in cmd[0]:
                if isinstance(networksetup, BaseException):
                    raise networksetup
                return SimpleNamespace(stdout=networksetup, returncode=0)
            if cmd[0] == "ifconfig":
                return SimpleNamespace(stdout=ifconfig, returncode=0)
            if cmd[0] == "ip":
                return SimpleNamespace(stdout=ip, returncode=0)
            return SimpleNamespace(stdout="", returncode=1)

        monkeypatch.setattr(subprocess, "run", _run)
        return calls

    return install


@pytest.fixture
def darwin(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Darwin")


def test_networksetup_beats_a_redacted_ifconfig(fake_run, darwin):
    """The defect itself: ifconfig lies, networksetup does not."""
    fake_run(networksetup=NS_OK, ifconfig=IFCONFIG_REDACTED)
    assert _no_skip(bp.host_mac, "en4") == bp.parse_mac(REAL)


def test_unresolvable_networksetup_never_returns_the_placeholder(fake_run, darwin):
    """feth/bridge/lo/awdl give rc=4; the fallback must not hand back the lie."""
    fake_run(networksetup=NS_UNRESOLVABLE, ifconfig=IFCONFIG_REDACTED)
    with pytest.raises(pytest.skip.Exception, match="placeholder"):
        bp.host_mac("feth0")


def test_missing_networksetup_skips_rather_than_raising(fake_run, darwin):
    """A missing binary must not escape a module-scope fixture as an error."""
    fake_run(networksetup=FileNotFoundError("no networksetup"),
             ifconfig=IFCONFIG_REDACTED)
    with pytest.raises(pytest.skip.Exception):
        bp.host_mac("en4")


def test_missing_networksetup_still_falls_back_to_a_real_ifconfig(fake_run, darwin):
    """ifconfig is a genuine fallback, not merely a 'printed nothing' path."""
    fake_run(networksetup=FileNotFoundError("no networksetup"), ifconfig=IFCONFIG_REAL)
    assert _no_skip(bp.host_mac, "en4") == bp.parse_mac(REAL)


def test_uppercase_networksetup_output_is_accepted(fake_run, darwin):
    """A case-sensitive regex would miss and fall through to the redacted line."""
    fake_run(networksetup=NS_UPPER, ifconfig=IFCONFIG_REDACTED)
    assert _no_skip(bp.host_mac, "en4") == bp.parse_mac(REAL)


def test_networksetup_returning_the_placeholder_is_refused(fake_run, darwin):
    """Pins the guard itself: the source does not matter, the value does."""
    fake_run(networksetup=NS_REDACTED, ifconfig=IFCONFIG_REAL)
    with pytest.raises(pytest.skip.Exception, match="placeholder"):
        bp.host_mac("en4")


def test_non_darwin_never_invokes_networksetup(fake_run, monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    calls = fake_run(ip=f"    link/ether {REAL} brd ff:ff:ff:ff:ff:ff\n")
    assert _no_skip(bp.host_mac, "zz0") == bp.parse_mac(REAL)
    assert not any("networksetup" in c[0] for c in calls), calls


def test_nothing_readable_skips(fake_run, darwin):
    fake_run(networksetup="", ifconfig="")
    with pytest.raises(pytest.skip.Exception, match="cannot read the MAC"):
        bp.host_mac("en4")
